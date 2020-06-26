import json
import logging
import warnings

import sqlalchemy
import sqlalchemy.exc
import time
from sqlalchemy import and_, or_, false
from sqlalchemy.orm import Session
from typing import Text, Optional, List, Dict, Any, Union, NoReturn
from sqlalchemy.exc import IntegrityError
import uuid

import rasa.cli.utils as rasa_cli_utils
import rasax.community.utils as rasa_x_utils
from rasa.core.constants import REQUESTED_SLOT
from rasa.core.events import UserUttered, deserialise_events
from rasa.core.trackers import DialogueStateTracker, EventVerbosity
from rasa.core.training.structures import Story
from rasa.utils import endpoints
from rasa.utils.endpoints import EndpointConfig
from rasax.community import config, telemetry
from rasax.community.constants import SHARE_YOUR_BOT_CHANNEL_NAME, DEFAULT_CHANNEL_NAME
from rasax.community.database.analytics import (
    ConversationActionStatistic,
    ConversationPolicyStatistic,
    ConversationIntentStatistic,
    ConversationEntityStatistic,
    ConversationStatistic,
    conversation_statistics_dict,
    ConversationSession,
)
from rasax.community.database.conversation import (
    Conversation,
    ConversationEvent,
    ConversationIntentMetadata,
    ConversationActionMetadata,
    ConversationPolicyMetadata,
    ConversationMessageCorrection,
    ConversationEntityMetadata,
    ConversationTag,
    MessageLog,
)
from rasax.community.database.domain import DomainIntent

from rasax.community.database.service import DbService
from rasax.community.database import utils as db_utils
from rasax.community.services.data_service import DataService
import rasax.community.tracker_utils as tracker_utils
from rasax.community.services.intent_service import (
    INTENT_MAPPED_TO_KEY,
    INTENT_NAME_KEY,
    IntentService,
)
from rasax.community.utils import get_text_hash, update_log_level, QueryResult

logger = logging.getLogger(__name__)

CORRECTED_MESSAGES_KEY = "corrected_messages"
_REVISION_CHECK_DELAY = 2  # Seconds


class EventService(DbService):
    def __init__(self, session: Optional[Session] = None):
        self._import_process_id = None
        super().__init__(session)

    def get_conversation_events(
        self,
        conversation_id: Text,
        until_time: Optional[float] = None,
        since_time: Optional[float] = None,
        rasa_environment_query: Optional[Text] = None,
    ) -> List[ConversationEvent]:

        since_time = since_time or 0
        filter_query = [
            ConversationEvent.conversation_id == conversation_id,
            ConversationEvent.timestamp > since_time,
        ]

        if until_time:
            filter_query.append(ConversationEvent.timestamp <= until_time)

        if rasa_environment_query:
            filter_query.append(
                ConversationEvent.rasa_environment == rasa_environment_query
            )

        return (
            self.query(ConversationEvent)
            .filter(*filter_query)
            .order_by(ConversationEvent.timestamp.asc())
            .all()
        )

    def get_tracker_for_conversation(
        self,
        conversation_id: Text,
        until_time: Optional[float] = None,
        since_time: Optional[float] = None,
        rasa_environment_query: Optional[Text] = None,
    ) -> Optional[DialogueStateTracker]:
        conversation_events = self.get_conversation_events(
            conversation_id, until_time, since_time, rasa_environment_query
        )

        if conversation_events or self._conversation_exists(conversation_id):
            events = [e.as_rasa_dict() for e in conversation_events]
            return DialogueStateTracker.from_dict(conversation_id, events)
        else:
            logger.debug(
                f"Tracker for conversation with ID '{conversation_id}' not " f"found."
            )
            return None

    def _conversation_exists(self, conversation_id: Text) -> bool:
        query = self.query(Conversation).filter(
            Conversation.sender_id == conversation_id
        )

        return (
            self.query(sqlalchemy.literal(True)).filter(query.exists()).scalar() is True
        )

    def get_events_count(self) -> int:
        """Return the summed total number of events in all conversations.

        Returns:
            Integer representing the total number of events in all stored conversations.
        """
        return self.query(ConversationEvent).count()

    def _track_import_process(self, process_id: Optional[Text]) -> None:
        """Track import process with ID `process_id`.

        Only the first event of a `rasa export` call triggers the tracking.

        Args:
            process_id: Unique ID associated with this import process.

        """
        if process_id and process_id != self._import_process_id:
            logger.debug(f"Updated current import process ID to {process_id}.")
            self._import_process_id = process_id

            telemetry.track_conversations_imported(process_id)

    def save_event(
        self,
        body: Union[Text, bytes],
        sender_id: Optional[Text] = None,
        event_number: Optional[int] = None,
        origin: Optional[Text] = None,
        import_process_id: Optional[Text] = None,
    ) -> ConversationEvent:
        """Update a conversation and save a new event to database.

        Args:
            body: Event to be logged.
            sender_id: Conversation ID sending the event.
            event_number: Event number associated with the event.
            origin: Rasa environment origin of the event.
            import_process_id: Unique ID if the event comes from a `rasa export`
                process.

        Returns:
           `ConversationEvent` of the successfully saved event.
        """
        logger.debug(f"Saving event from origin '{origin}' to event service:\n{body}")
        event = json.loads(body)

        if sender_id:
            event["sender_id"] = sender_id

        self._track_import_process(import_process_id)

        self._update_conversation_metadata(event)

        return self._save_conversation_event(event, event_number, origin=origin)

    def _save_conversation_event(
        self,
        event: Dict[Text, Any],
        event_number: Optional[int] = None,
        origin: Optional[Text] = None,
    ) -> ConversationEvent:
        type_name = event.get("event")
        sender_id = event.get("sender_id")
        intent = event.get("parse_data", {}).get("intent", {}).get("name")
        action = event.get("name")
        policy = self.extract_policy_base_from_event(event)
        timestamp = event.get("timestamp")
        slot_name = None
        slot_value = None

        if type_name == "slot" and event.get("name") != REQUESTED_SLOT:
            slot_name = event.get("name")
            slot_value = json.dumps(event.get("value"), sort_keys=True)

        if tracker_utils.is_user_event(event):
            telemetry.track_message_received(sender_id, event.get("input_channel"))

        new_event = ConversationEvent(
            conversation_id=sender_id,
            type_name=type_name,
            timestamp=timestamp,
            intent_name=intent,
            action_name=action,
            data=json.dumps(event),
            policy=policy,
            rasa_environment=origin,
            slot_name=slot_name,
            slot_value=slot_value,
        )

        self._store_conversation_event(new_event)
        self._update_statistics_from_event(new_event.as_rasa_dict(), event_number)

        return new_event

    def _store_conversation_event(self, event: ConversationEvent) -> None:
        self.add(event)
        self.flush()  # flush to obtain ID

    def _get_latest_session(
        self, conversation_id: Text
    ) -> Optional[ConversationSession]:
        """Get the latest `ConversationSession` for `conversation_id`.

        Args:
            conversation_id: Conversation ID to search for.

        Returns:
            The latest `ConversationSession` if it exists, otherwise `None`.
        """
        return (
            self.query(ConversationSession)
            .filter(ConversationSession.conversation_id == conversation_id)
            .order_by(ConversationSession.session_id.desc())
            .first()
        )

    def _is_current_session_in_training_data(self, conversation_id: Text) -> bool:
        """Determine whether the current conversation session is in training data.

        Args:
            conversation_id: Conversation ID to search for.

        Returns:
            `True` if the current session is in training data, otherwise `False`.
        """
        latest_session = self._get_latest_session(conversation_id)
        if latest_session:
            return latest_session.in_training_data
        else:
            return False

    def _session_ids_in_training_data(self, conversation_id: Text) -> List[int]:
        """Get the session IDs that are in training data.

        Args:
            conversation_id: Conversation ID to search for.

        Returns:
            IDs of `ConversationSession`s that are entirely in training data.
        """
        # noinspection PyTypeChecker
        session_ids = (
            self.query(ConversationSession.session_id).filter(
                ConversationSession.conversation_id == conversation_id,
                ConversationSession.in_training_data,
            )
        ).all()

        return [_id for (_id,) in session_ids]

    def _current_session_id(self, conversation_id: Text) -> Optional[int]:
        """Get the current `ConversationSession`'s ID.

        Args:
            conversation_id: Conversation ID to search for.

        Returns:
            ID of the current `ConversationSession`s, `None` if it doesn't exist.
        """
        latest_session = self._get_latest_session(conversation_id)
        if latest_session:
            return latest_session.session_id

    def _has_completed_session_in_training_data(self, conversation_id: Text) -> bool:
        """Determine whether there exists a fully completed `ConversationSession`
        that is in the training data.

        Args:
            conversation_id: Conversation ID to search for.

        Returns:
            `True` if such a session exists, `False` otherwise.
        """
        indexes_in_training_data = self._session_ids_in_training_data(conversation_id)

        if len(indexes_in_training_data) > 1:
            # there's more than one session in training data
            return True

        if (
            len(indexes_in_training_data) == 1
            and self._current_session_id(conversation_id)
            not in indexes_in_training_data
        ):
            # there is exactly one, this may or may not be the current one
            return True

        return False

    def _number_of_conversation_sessions(self, conversation_id: Text) -> int:
        """Get the number of `ConversationSession`s for `conversation_id`.

        Args:
            conversation_id: Conversation ID to search for.

        Returns:
            Number of saved `ConversationSession`s.
        """
        # noinspection PyTypeChecker
        return (
            self.query(ConversationSession.session_id).filter(
                ConversationSession.conversation_id == conversation_id
            )
        ).count()

    def _update_conversation_in_training_data(
        self, conversation: Conversation, policy: Optional[Text]
    ) -> None:
        """Update `in_training_data` property of `conversation`.

        A conversation is considered to be in training data
        (`in_training_data` = `True`) if at least one of its constituent sessions had
        all its actions predicted through a memoization policy.

        Args:
            conversation: Conversation to update.
            policy: Policy used to predict the incoming event.
        """
        number_of_conversation_sessions = self._number_of_conversation_sessions(
            conversation.sender_id
        )
        if not number_of_conversation_sessions:
            # we've received the first event, do nothing
            return

        has_in_training_data_session = self._has_completed_session_in_training_data(
            conversation.sender_id
        )
        if has_in_training_data_session:
            # there has been a complete session
            conversation.in_training_data = True
            return

        if self._is_current_session_in_training_data(
            conversation.sender_id
        ) and tracker_utils.is_predicted_event_in_training_data(policy):
            # there wasn't a complete session yet, but the current one is in training
            # data
            # we also need to ensure that the new incoming event is in training data
            conversation.in_training_data = True
            return

        # everything else means the conversation is not in training data
        conversation.in_training_data = False

    def _update_conversation_metadata(self, event: Dict[Text, Any]) -> None:
        sender_id = event.get("sender_id")
        conversation = (
            self.query(Conversation).filter(Conversation.sender_id == sender_id).first()
        )

        if not conversation:
            conversation = self.create_new_conversation(sender_id)
            # Flush to obtain row id
            self.flush()

        conversation.latest_event_time = event.get("timestamp")

        if tracker_utils.is_action_event(event):
            self._update_conversation_from_action(conversation, event)
            self._update_conversation_in_training_data(
                conversation, event.get("policy")
            )
        elif tracker_utils.is_user_event(event):
            self._update_conversation_from_user_event(conversation, event)

        self._update_policy_metadata(conversation, event)

    def _update_conversation_from_action(
        self, conversation: Conversation, event: Dict[Text, Any]
    ) -> None:
        self._update_action_confidences(conversation, event.get("confidence"))
        self._update_unique_actions(conversation, event.get("name"))

    @staticmethod
    def _update_action_confidences(
        conversation, action_confidence: Optional[float]
    ) -> None:
        if not action_confidence:
            return

        compared_min = rasa_x_utils.coalesce(
            conversation.minimum_action_confidence, 1.0
        )
        compared_max = rasa_x_utils.coalesce(
            conversation.maximum_action_confidence, 0.0
        )
        conversation.minimum_action_confidence = min(action_confidence, compared_min)
        conversation.maximum_action_confidence = max(action_confidence, compared_max)

    def _update_unique_actions(
        self, conversation: Conversation, action_name: Optional[Text]
    ) -> None:
        # noinspection PyTypeChecker
        unique_actions = (a.action for a in conversation.unique_actions)
        if action_name and action_name not in unique_actions:
            self.add(
                ConversationActionMetadata(
                    conversation_id=conversation.sender_id, action=action_name
                )
            )

    def _update_conversation_from_user_event(
        self, conversation: Conversation, event: Dict[Text, Any]
    ) -> None:
        conversation.number_user_messages += 1
        self._set_latest_input_channel(conversation, event.get("input_channel"))

        parse_data = event.get("parse_data", {})
        intent = parse_data.get("intent", {})
        self._update_unique_intents(conversation, intent.get("name"))
        self._update_intent_confidences(conversation, intent.get("confidence"))
        self._add_entity_metadata(conversation, parse_data.get("entities", []))

    @staticmethod
    def _set_latest_input_channel(
        conversation: Conversation, input_channel: Optional[Text]
    ) -> None:
        # The input channel of interactive conversation can't change over time
        if input_channel and not conversation.interactive:
            conversation.latest_input_channel = input_channel

    def _update_unique_intents(
        self, conversation: Conversation, intent_name: Optional[Text]
    ) -> None:
        if not intent_name:
            return

        # noinspection PyTypeChecker
        unique_intents = (i.intent for i in conversation.unique_intents)
        if intent_name not in unique_intents:
            self.add(
                ConversationIntentMetadata(
                    conversation_id=conversation.sender_id, intent=intent_name
                )
            )

    @staticmethod
    def _update_intent_confidences(
        conversation: Conversation, intent_confidence: Optional[float]
    ) -> None:
        if not intent_confidence:
            return

        compared_min = rasa_x_utils.coalesce(
            conversation.minimum_intent_confidence, 1.0
        )
        compared_max = rasa_x_utils.coalesce(
            conversation.maximum_intent_confidence, 0.0
        )
        conversation.minimum_intent_confidence = min(intent_confidence, compared_min)
        conversation.maximum_intent_confidence = max(intent_confidence, compared_max)

    def _add_entity_metadata(
        self, conversation: Conversation, entities: List[Dict]
    ) -> None:
        entities = [e.get("entity") for e in entities]
        for e in entities:
            existing = (
                self.query(ConversationEntityMetadata)
                .filter(
                    ConversationEntityMetadata.conversation_id
                    == conversation.sender_id,
                    ConversationEntityMetadata.entity == e,
                )
                .first()
            )
            if not existing:
                self.add(
                    ConversationEntityMetadata(
                        conversation_id=conversation.sender_id, entity=e
                    )
                )

    def _update_policy_metadata(
        self, conversation: Conversation, event: Dict[Text, Any]
    ) -> None:
        policy = self.extract_policy_base_from_event(event)

        # noinspection PyTypeChecker
        if policy and policy not in [p.policy for p in conversation.unique_policies]:
            self.add(
                ConversationPolicyMetadata(
                    conversation_id=conversation.sender_id, policy=policy
                )
            )

    def create_new_conversation(
        self,
        conversation_id: Optional[Text] = None,
        interactive: bool = False,
        created_by: Optional[Text] = None,
    ) -> Conversation:
        """Create a new, empty conversation.

        Args:
            conversation_id: ID of the new conversation.
            interactive: Whether this conversation was created during interactive
                learning.
            created_by: Name of the user who created the conversation.

        Raises:
            ValueError: If a conversation with this `conversation_id` cannot be saved.

        Returns:
            The created conversation.
        """
        conversation_id = conversation_id or uuid.uuid4().hex
        try:
            new = Conversation(
                sender_id=conversation_id,
                interactive=interactive,
                latest_event_time=time.time(),
                latest_input_channel=DEFAULT_CHANNEL_NAME,
                created_by=created_by,
            )
            self.add(new)
            self.flush()
            return new
        except IntegrityError as e:
            self.rollback()
            # warning message will be logged by the caller
            raise ValueError(
                f"Conversation ID '{conversation_id}' cannot "
                f"be saved due to the following error: {e}."
            )

    def copy_events_from_conversation(
        self, conversation_id: Text, until: float
    ) -> List[Dict]:
        """Copy conversation events from an existing conversation.

        Args:
            conversation_id: The conversation ID from which the events should be copied.
            until: Defines until which point in time events from the existing
                conversation are copied. `Until` is interpreted as
                `less than or equal to`.

        Returns:
            The events of the created conversation. Note that while the conversation is
            already stored in the database, the copied events aren't saved until the
            event service picks them up from the event broker.

        Raises:
            ValueError: If the conversation ID to copy from does not exist or
            if no events to be copied are found in the tracker of the existing
            conversation ID.
        """
        tracker_to_copy = self.get_tracker_for_conversation(
            conversation_id, until_time=until
        )
        if not tracker_to_copy:
            raise ValueError(
                f"A tracker for conversation ID '{conversation_id}' does "
                f"not exist. Copying events from this conversation "
                f"failed."
            )

        events = tracker_to_copy.current_state(EventVerbosity.APPLIED)["events"]

        if not events:
            raise ValueError("No events were found to be copied.")

        # Remove timestamp so that Rasa Open Source assigns new ones
        for event in events:
            event.pop("timestamp", None)

        return events

    @staticmethod
    def extract_policy_base_from_event(event: Dict[Text, Any]) -> Optional[Text]:
        """Given an ActionExecuted event, extracts the base name of

        the policy used. Example: event with `"policy": "policy_1_KerasPolicy"`
        will return `KerasPolicy`."""
        if event.get("policy"):
            return event["policy"].split("_")[-1]

        return None

    def _update_statistics_from_event(
        self, event: Dict[Text, Any], event_number: Optional[int]
    ) -> None:
        event_name = event.get("event")
        statistic = self.query(ConversationStatistic).first()

        if not statistic:
            statistic = ConversationStatistic(
                project_id=config.project_name,
                total_bot_messages=0,
                total_user_messages=0,
            )
            self.add(statistic)

        statistic.latest_event_timestamp = event["timestamp"]
        statistic.latest_event_id = event_number or statistic.latest_event_id
        if tracker_utils.is_user_event(event_name):
            statistic.total_user_messages += 1
            self._update_user_event_statistic(event)
        elif tracker_utils.is_bot_event(event_name):
            statistic.total_bot_messages += 1
        elif tracker_utils.is_action_event(
            event_name
        ) and not tracker_utils.is_action_listen(event_name):
            self._update_action_event_statistic(event)

    def _update_user_event_statistic(self, event: Dict[Text, Any]) -> None:
        intent = event.get("parse_data", {}).get("intent", {}).get("name")
        if intent:
            existing = (
                self.query(ConversationIntentStatistic)
                .filter(ConversationIntentStatistic.intent == intent)
                .first()
            )
            if existing:
                existing.count += 1
            else:
                self.add(
                    ConversationIntentStatistic(
                        project_id=config.project_name, intent=intent
                    )
                )

        entities = event.get("parse_data", {}).get("entities", [])
        for entity in entities:
            entity_name = entity.get("entity")
            existing = (
                self.query(ConversationEntityStatistic)
                .filter(ConversationEntityStatistic.entity == entity_name)
                .first()
            )
            if existing:
                existing.count += 1
            else:
                self.add(
                    ConversationEntityStatistic(
                        project_id=config.project_name, entity=entity_name
                    )
                )

    def _update_action_event_statistic(self, event: Dict[Text, Any]) -> None:
        existing = (
            self.query(ConversationActionStatistic)
            .filter(ConversationActionStatistic.action == event["name"])
            .first()
        )
        if existing:
            existing.count += 1
        else:
            self.add(
                ConversationActionStatistic(
                    project_id=config.project_name, action=event["name"]
                )
            )
        policy = self.extract_policy_base_from_event(event)
        if policy:
            existing = (
                self.query(ConversationPolicyStatistic)
                .filter(ConversationPolicyStatistic.policy == policy)
                .first()
            )
            if existing:
                existing.count += 1
            else:
                self.add(
                    ConversationPolicyStatistic(
                        project_id=config.project_name, policy=policy
                    )
                )

    def get_evaluation(self, conversation_id: Text) -> Dict[Text, Any]:
        conversation = self.get_conversation(conversation_id)

        if conversation and conversation.evaluation:
            return json.loads(conversation.evaluation)
        else:
            return {}

    def get_conversations_for_user(self, created_by: Text) -> Optional[Conversation]:
        """Get conversation for user `created_by`

        Args:
            created_by: Name of the user who created the conversation.

        Return:
            Conversation or `None` if the conversation is not found.
        """
        return (
            self.query(Conversation)
            .filter(Conversation.created_by == created_by)
            .first()
        )

    def get_conversation(self, conversation_id: Text) -> Optional[Conversation]:
        """Get conversation by `conversation_id`

        Args:
            conversation_id: ID of the conversation.

        Return:
            Conversation or `None` if the conversation with the specified ID is not found.
        """
        return (
            self.query(Conversation)
            .filter(Conversation.sender_id == conversation_id)
            .first()
        )

    def update_evaluation(
        self, conversation_id: Text, evaluation: Dict[Text, Any]
    ) -> None:
        conversation = self.get_conversation(conversation_id)

        if conversation:
            conversation.evaluation = json.dumps(evaluation)
            conversation.in_training_data = (
                evaluation.get("in_training_data_fraction", 0) == 1
            )
            self.commit()
        else:
            raise ValueError(f"No conversation found for id '{conversation_id}'.")

    def delete_evaluation(self, sender_id: Text) -> None:
        conversation = (
            self.query(Conversation).filter(Conversation.sender_id == sender_id).first()
        )

        if conversation:
            conversation.evaluation = None
            self.commit()

    def get_statistics(self) -> Dict[Text, Union[int, List[Text]]]:
        statistic = self.query(ConversationStatistic).first()
        if statistic:
            return statistic.as_dict()
        else:
            return self.user_statistics_dict()

    @staticmethod
    def user_statistics_dict(
        n_user_messages: Optional[int] = None,
        n_bot_messages: Optional[int] = None,
        top_intents: Optional[List[Text]] = None,
        top_actions: Optional[List[Text]] = None,
        top_entities: Optional[List[Text]] = None,
        top_policies: Optional[List[Text]] = None,
    ) -> Dict[Text, Union[int, List[Text]]]:
        return conversation_statistics_dict(
            n_user_messages,
            n_bot_messages,
            top_intents,
            top_actions,
            top_entities,
            top_policies,
        )

    def get_conversation_metadata_for_conversation_id(
        self, conversation_id: Text
    ) -> Optional[Dict[Text, Any]]:
        """Return conversation metadata for given conversation ID.

        Args:
            conversation_id: Id of conversation to retrieve metadata for.

        Returns:
            Dictionary with metadata.
        """
        conversation = self.get_conversation(conversation_id)

        if conversation:
            return conversation.as_dict()
        else:
            return None

    def get_unique_actions(self) -> List[str]:
        # noinspection PyTypeChecker
        actions = self.query(ConversationActionStatistic.action).distinct().all()

        return [a for (a,) in actions]

    def get_unique_policies(self) -> List[str]:
        # noinspection PyTypeChecker
        policies = self.query(ConversationPolicyStatistic.policy).distinct().all()

        return [p for (p,) in policies]

    def get_unique_intents(self) -> List[str]:
        # noinspection PyTypeChecker
        intents = (
            self.query(ConversationIntentStatistic.intent)
            .distinct()
            .filter(DomainIntent.intent == ConversationIntentStatistic.intent)
            .all()
        )
        return [i for (i,) in intents]

    def get_unique_entities(self) -> List[str]:
        # noinspection PyTypeChecker
        entities = self.query(ConversationEntityStatistic.entity).distinct().all()

        return [e for (e,) in entities]

    def get_unique_slot_names(self) -> List[Text]:
        """Return a list of unique slot names used in all stored conversations.

        Returns:
            List of unique slot names.
        """
        # noinspection PyTypeChecker,PyTypeChecker
        slot_names = (
            self.query(ConversationEvent.slot_name)
            .distinct()
            .filter(ConversationEvent.slot_name.isnot(None))
            .all()
        )

        return [name for (name,) in slot_names]

    def get_unique_slot_values(
        self, slot: Optional[Text] = None, query: Optional[Text] = None
    ) -> List[Text]:
        """Return a list of unique slot values used in all stored conversations.
        A slot name can be specified; if it is, then only values for that slot
        will be returned. Additionally, it is possible to filter the results
        using a search term.

        Args:
            slot: Name of the slot to search values for. If `None` is
                specified, return values for all slots.
            query: Text value to use for filtering slot values.

        Returns:
            List of unique slot values.
        """
        # noinspection PyTypeChecker
        slot_values = (
            self.query(ConversationEvent.slot_value)
            .distinct()
            .filter(ConversationEvent.slot_value.isnot(None))
        )

        if slot:
            slot_values = slot_values.filter(ConversationEvent.slot_name == slot)

        if query:
            slot_values = slot_values.filter(
                ConversationEvent.slot_value.ilike(f"%{query}%")
            )

        return [str(json.loads(value)) for (value,) in slot_values.all()]

    def get_unique_input_channels(self) -> List[Text]:
        """Return a list of unique input channels used in all stored conversations.

        Returns:
            List of channel names.
        """
        # noinspection PyTypeChecker
        input_channels = (
            self.query(Conversation.latest_input_channel)
            .distinct()
            .filter(Conversation.latest_input_channel.isnot(None))
        )

        return [channel for (channel,) in input_channels.all()]

    def get_conversation_metadata_for_all_clients(
        self,
        start: Optional[float] = None,
        until: Optional[float] = None,
        minimum_confidence: Optional[float] = None,
        maximum_confidence: Optional[float] = None,
        minimum_nlu_confidence: Optional[float] = None,
        maximum_nlu_confidence: Optional[float] = None,
        minimum_user_messages: Optional[int] = None,
        policies: Optional[Text] = None,
        in_training_data: Optional[bool] = None,
        intent_query: Optional[Text] = None,
        entity_query: Optional[Text] = None,
        action_query: Optional[Text] = None,
        sort_by_date: bool = True,
        sort_by_confidence: bool = False,
        text_query: Optional[Text] = None,
        rasa_environment_query: Optional[Text] = None,
        only_flagged: bool = False,
        include_interactive: bool = True,
        exclude: Optional[List[Text]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        conversations_tags_filter_any: Optional[Text] = None,
        slots: Optional[List[Text]] = None,
        input_channels: Optional[List[Text]] = None,
        created_by: Optional[Text] = None,
    ) -> QueryResult:
        """Returns list of conversations that match all given query parameters.

        Args:
            start: Earliest date of conversation.
            until: Latest date of conversation.
            minimum_confidence: Minimum confidence of predicted action.
            maximum_confidence: Maximum confidence of predicted action.
            minimum_nlu_confidence: Minimum confidence of predicted intent.
            maximum_nlu_confidence: Maximum confidence of predicted intent.
            minimum_user_messages: Minimum number of messages from the user.
            policies: List of policies, ANY of which was used in the conversation.
            in_training_data: Identifies if conversation is in training data.
            intent_query: List of intents, ANY of which was in the conversation.
            entity_query: List of entities, ANY of which was in the conversation.
            action_query: List of actions, ANY of which was in the conversation.
            sort_by_date: Identifies if the conversations should be sorted by date.
            sort_by_confidence: Identifies if the conversations should be
                sorted by confidence.
            text_query: List of partial messages, ANY of which was in the conversation.
            rasa_environment_query: Environment in which RasaX was deployed.
            only_flagged: Only return conversations that have flagged messages.
            include_interactive: Include conversations from interactive learning.
            exclude: Conversation IDs to exclude from the result.
            limit: Limit number of conversations in the result.
            offset: Applies offset to the conversations in the result.
            conversations_tags_filter_any: List of tags, ANY of which is assigned
                to the conversation.
            slots: List of slots names with corresponding values, ANY of which
                were in the conversation. Each element of the list must be a
                string with the format NAME:VALUE.
            input_channels: Return only conversations which have their
                `latest_input_channel` set to ANY of these values.
            created_by: Name of the user who created the conversation.

        Returns:
            List of conversations.
        """
        conversations = self.query(Conversation)

        query = True
        if start:
            query = Conversation.latest_event_time >= start

        if until:
            query = and_(query, Conversation.latest_event_time <= until)

        if minimum_user_messages:
            query = and_(
                query, Conversation.number_user_messages >= minimum_user_messages
            )

        if in_training_data is not None:
            query = and_(query, Conversation.in_training_data.is_(in_training_data))

        if only_flagged:
            query = and_(query, Conversation.events.any(ConversationEvent.is_flagged))

        if minimum_confidence is not None:
            query = and_(
                query, Conversation.minimum_action_confidence >= minimum_confidence
            )

        if maximum_confidence is not None:
            query = and_(
                query, Conversation.maximum_action_confidence <= maximum_confidence
            )

        if minimum_nlu_confidence is not None:
            query = and_(
                query, Conversation.minimum_intent_confidence >= minimum_nlu_confidence
            )

        if maximum_nlu_confidence is not None:
            query = and_(
                query, Conversation.maximum_intent_confidence <= maximum_nlu_confidence
            )

        if policies:
            policies = policies.split(",")
            query = and_(
                query,
                Conversation.unique_policies.any(
                    ConversationPolicyMetadata.policy.in_(policies)
                ),
            )

        if intent_query:
            intents = intent_query.split(",")
            query = and_(
                query,
                Conversation.unique_intents.any(
                    ConversationIntentMetadata.intent.in_(intents)
                ),
            )

        if action_query:
            actions = action_query.split(",")
            query = and_(
                query,
                Conversation.unique_actions.any(
                    ConversationActionMetadata.action.in_(actions)
                ),
            )

        if entity_query:
            entities = entity_query.split(",")
            query = and_(
                query,
                Conversation.unique_entities.any(
                    ConversationEntityMetadata.entity.in_(entities)
                ),
            )

        if text_query:
            text_query = f"%{text_query}%"
            query = and_(
                query,
                Conversation.events.any(
                    ConversationEvent.message_log.has(MessageLog.text.ilike(text_query))
                ),
            )

        if rasa_environment_query:
            query = and_(
                query,
                Conversation.events.any(
                    ConversationEvent.rasa_environment == rasa_environment_query
                ),
            )

        if not include_interactive:
            query = and_(query, Conversation.interactive == false())

        if exclude:
            query = and_(
                query,
                or_(
                    Conversation.created_by.is_(None),
                    Conversation.created_by.notin_(exclude),
                ),
            )

        if conversations_tags_filter_any:
            query = and_(
                query,
                Conversation.tags.any(
                    ConversationTag.id.in_(conversations_tags_filter_any)
                ),
            )

        if created_by:
            query = and_(query, Conversation.created_by == created_by)

        if slots:
            slot_query = false()

            for slot in slots:
                parts = slot.split(":")
                if len(parts) != 2 or not all(parts):
                    # Value with invalid format, ignore it
                    continue

                # Join all slot conditions with OR...
                slot_query = or_(
                    slot_query,
                    Conversation.events.any(
                        and_(
                            ConversationEvent.slot_name == parts[0],
                            ConversationEvent.slot_value.ilike(f"%{parts[1]}%"),
                        )
                    ),
                )

            # ...and then AND them with the current query
            query = and_(query, slot_query)

        if input_channels:
            query = and_(query, Conversation.latest_input_channel.in_(input_channels))

        conversations = conversations.filter(query)
        number_conversations = conversations.count()

        if in_training_data is False and sort_by_confidence:
            conversations = conversations.order_by(
                Conversation.minimum_action_confidence.desc()
            )
        elif sort_by_date:
            conversations = conversations.order_by(
                Conversation.latest_event_time.desc()
            )

        conversations = conversations.offset(offset).limit(limit).all()

        return QueryResult([c.as_dict() for c in conversations], number_conversations)

    def conversation_ids(self) -> List[Text]:
        # noinspection PyTypeChecker
        conversation_ids = self.query(Conversation.sender_id).distinct().all()

        return [i for i, in conversation_ids]

    def story_for_conversation_id(self, conversation_id: Text) -> Text:
        # noinspection PyTypeChecker
        events = (
            self.query(ConversationEvent.data)
            .filter(ConversationEvent.conversation_id == conversation_id)
            .order_by(ConversationEvent.id.asc())
            .all()
        )

        events = deserialise_events([json.loads(e) for e, in events])

        story = Story.from_events(events, conversation_id)
        return story.as_story_string(flat=True)

    def add_flagged_message(self, sender_id: Text, message_timestamp: float) -> None:
        event = (
            self.query(ConversationEvent)
            .filter(
                ConversationEvent.conversation_id == sender_id,
                ConversationEvent.timestamp == message_timestamp,
            )
            .first()
        )

        if not event:
            logger.warning(
                f"Event with timestamp '{message_timestamp}' for sender id "
                f"'{sender_id}' was not found."
            )
        else:
            event.is_flagged = True
            self.commit()

    def delete_flagged_message(self, sender_id: Text, message_timestamp: float) -> None:
        event = (
            self.query(ConversationEvent)
            .filter(
                ConversationEvent.conversation_id == sender_id,
                ConversationEvent.timestamp == message_timestamp,
            )
            .first()
        )

        if not event:
            logger.warning(
                f"Event with timestamp '{message_timestamp}' for sender id "
                f"'{sender_id}' was not found."
            )
        else:
            event.is_flagged = False
            self.commit()

    def correct_message(
        self,
        sender_id: Text,
        message_timestamp: float,
        new_intent: Dict[str, str],
        user: Dict[str, str],
        project_id: Text,
    ) -> None:
        """Corrects a message and adds it to the training data.

        Raises:
            ValueError: If a user event with the given timestamp could not be found.

        Args:
            sender_id: The sender id of the conversation.
            message_timestamp: The timestamp of the user message which should
                               be corrected.
            new_intent: Intent object which describes the new intent of the
                        message.
            user: The user who owns the training data.
            project_id: The project id of the training data.

        """
        intent_service = IntentService(self.session)
        existing_intents = intent_service.get_permanent_intents(project_id)
        intent_id = new_intent[INTENT_NAME_KEY]
        is_temporary = intent_id not in existing_intents
        mapped_to = None

        if is_temporary:
            intent_service.add_temporary_intent(new_intent, project_id)
            temporary_intent = intent_service.get_temporary_intent(
                intent_id, project_id
            )
            mapped_to = None
            if temporary_intent:
                temporary_intent.get(INTENT_MAPPED_TO_KEY)

        # noinspection PyTypeChecker
        event = (
            self.query(ConversationEvent)
            .filter(
                ConversationEvent.conversation_id == sender_id,
                ConversationEvent.type_name == UserUttered.type_name,
                ConversationEvent.timestamp == message_timestamp,
            )
            .first()
        )

        if event is None:
            raise ValueError("A user event for this timestamp does not exist.")

        event = json.loads(event.data)
        if not is_temporary or mapped_to is not None:
            data_service = DataService(self.session)
            event["parse_data"]["intent"]["name"] = mapped_to or intent_id
            example = data_service.save_user_event_as_example(user, project_id, event)
            example_hash = example.get("hash")
            intent_service.add_example_to_temporary_intent(
                new_intent[INTENT_NAME_KEY], example_hash, project_id
            )
        else:
            logger.debug(
                "Message correction was not added to training data, "
                "since the intent was temporary and not mapped to "
                "an existing intent."
            )

        correction = ConversationMessageCorrection(
            conversation_id=sender_id,
            message_timestamp=message_timestamp,
            intent=intent_id,
        )
        self.add(correction)
        self.commit()

    def delete_message_correction(
        self, sender_id: Text, message_timestamp: float, project_id: Text
    ) -> None:
        """Deletes a message correction and the related training data.

        Raises:
            ValueError: If a user event with the given timestamp could not be found.

        Args:
            sender_id: The sender id of the conversation.
            message_timestamp: The timestamp of the corrected user message.
            project_id: The project id of the stored training data.

        """

        correction = (
            self.query(ConversationMessageCorrection)
            .filter(
                ConversationMessageCorrection.conversation_id == sender_id,
                ConversationMessageCorrection.message_timestamp == message_timestamp,
            )
            .first()
        )

        if correction:
            # noinspection PyTypeChecker
            event = (
                self.query(ConversationEvent)
                .filter(
                    ConversationEvent.conversation_id == sender_id,
                    ConversationEvent.type_name == UserUttered.type_name,
                    ConversationEvent.timestamp == message_timestamp,
                )
                .first()
            )

            if event is None:
                raise ValueError("A user event for this timestamp does not exist.")

            # delete example from training data
            event = json.loads(event.data)
            message_text = event.get("parse_data", {}).get("text")
            message_hash = get_text_hash(message_text)

            data_service = DataService(self.session)
            data_service.delete_example_by_hash(project_id, message_hash)

            # delete example from possible temporary intent
            intent_service = IntentService(self.session)
            intent_service.remove_example_from_temporary_intent(
                correction.intent, message_hash, project_id
            )

            self.delete(correction)
            self.commit()

    @classmethod
    def modify_event_time_to_be_later_than(
        cls,
        minimal_timestamp: float,
        events: List[Dict[Text, Any]],
        minimal_timedelta: float = 0.001,
    ) -> List[Dict[Text, Any]]:
        """Changes the event times to be after a certain timestamp."""

        if not events:
            return events

        events = sorted(events, key=lambda e: e["timestamp"])
        oldest_event = events[0].get("timestamp", minimal_timestamp)
        # Make sure that the new timestamps are greater than the minimal
        difference = minimal_timestamp - oldest_event + minimal_timedelta

        if difference > 0:

            delta = minimal_timedelta

            for event in events:
                event["timestamp"] = minimal_timestamp + delta
                delta += minimal_timedelta

        return events

    def get_messages_for(
        self,
        conversation_id: Text,
        until_time: Optional[float] = None,
        event_verbosity: EventVerbosity = EventVerbosity.ALL,
    ) -> Optional[Dict[Text, Any]]:
        """Gets tracker including a field `messages` which lists user / bot events."""

        tracker = self._get_flagged_tracker_dict(
            conversation_id, until_time, event_verbosity=event_verbosity
        )

        if tracker is None:
            return None

        messages = []
        for i, e in enumerate(tracker["events"]):
            if e.get("event") in {"user", "bot", "agent"}:
                m = e.copy()
                m["data"] = m.get("data", m.get("parse_data", None))
                m["type"] = m["event"]
                del m["event"]
                messages.append(m)

        tracker["messages"] = messages

        return tracker

    def get_tracker_with_message_flags(
        self,
        conversation_id: Text,
        until_time: Optional[float] = None,
        since_time: Optional[float] = None,
        event_verbosity: EventVerbosity = EventVerbosity.ALL,
        rasa_environment_query: Optional[Text] = None,
        exclude_leading_action_session_start: bool = False,
    ) -> Optional[Dict[Text, Any]]:
        """Gets tracker including message flags.

        Args:
            conversation_id: Name of the conversation tracker.
            until_time: Include only events until the given time.
            since_time: Include only events after the given time.
            event_verbosity: Verbosity of the returned tracker.
            rasa_environment_query: Origin Rasa host of the tracker events.
            exclude_leading_action_session_start: Whether to exclude a leading
                `action_session_start` from the tracker events.

        Returns:
            Tracker for the conversation ID.

        """
        tracker = self._get_flagged_tracker_dict(
            conversation_id,
            until_time,
            since_time,
            event_verbosity,
            rasa_environment_query,
        )

        if exclude_leading_action_session_start:
            tracker_utils.remove_leading_action_session_start_from(tracker)

        return tracker

    def _get_flagged_tracker_dict(
        self,
        conversation_id: Text,
        until_time: Optional[float] = None,
        since_time: Optional[float] = None,
        event_verbosity: EventVerbosity = EventVerbosity.AFTER_RESTART,
        rasa_environment_query: Optional[Text] = None,
    ) -> Optional[Dict[Text, Any]]:
        tracker = self.get_tracker_for_conversation(
            conversation_id, until_time, since_time, rasa_environment_query
        )

        if not tracker:
            return None

        tracker_dict = tracker.current_state(event_verbosity)

        for e in tracker_dict["events"]:
            # TODO: This should be part of the event metadata, not the message
            # itself, so this copy should not be carried out.
            e["is_flagged"] = e["metadata"]["rasa_x_flagged"]

        return tracker_dict

    @staticmethod
    def get_sender_name(conversation: Conversation) -> Text:
        if conversation.latest_input_channel == SHARE_YOUR_BOT_CHANNEL_NAME:
            return SHARE_YOUR_BOT_CHANNEL_NAME
        else:
            return conversation.sender_id

    def add_conversation_tags(
        self, conversation_id: Text, tags: List[Dict[Text, Any]]
    ) -> List[Dict[Text, Any]]:
        """Assign tags to given conversation.
        If the tag doesn't exist yet, it will be created.

        Args:
            conversation_id: Conversation to assign tag to.
            tags: Tags to add.

        Returns:
            List of tags with information about conversations they're assigned to.
        """

        conversation = self.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(
                f"No conversation found for conversation ID '{conversation_id}'."
            )

        conversation_tags = [
            self._process_tag(tag, conversation).as_dict() for tag in tags
        ]

        return conversation_tags

    def _process_tag(
        self, tag: Dict[Text, Any], conversation: Conversation
    ) -> ConversationTag:
        """Takes in JSON input from user that has tags inside and further decides
        if it's "assign existing tag id", or "assign and possibly create tag by value".

        Args:
            tag: Raw JSON input from user.
            conversation: Existing conversation.

        Returns:
            List of tags with information about conversations they're assigned to.
        """
        tag_id = tag.get("id")

        if tag_id:
            existing_tag = self._get_tag_by_id(tag_id)
            if existing_tag is None:
                raise ValueError(f"Tag {tag_id} does not exist.")
        elif "value" in tag and "color" in tag:
            existing_tag = self._get_tag_by_value(tag["value"])
            if existing_tag is None:
                existing_tag = self._insert_tag(tag["value"], tag["color"])
            tag_id = existing_tag.id

        if not tag_id:
            raise ValueError(
                f'Both "value" and "color" fields are expected in element: {tag}'
            )

        if tag_id not in conversation.tags_set():
            conversation.tags.append(existing_tag)

        existing_tag: ConversationTag = existing_tag

        return existing_tag

    def _get_tag_by_id(self, tag_id: int) -> Optional[ConversationTag]:
        return self.query(ConversationTag).filter(ConversationTag.id == tag_id).first()

    def _get_tag_by_value(self, tag_value: Text) -> Optional[ConversationTag]:
        return (
            self.query(ConversationTag)
            .filter(ConversationTag.value == tag_value)
            .first()
        )

    def _insert_tag(self, tag_value: Text, color: Text) -> ConversationTag:
        new_tag = ConversationTag(value=tag_value, color=color)
        self.add(new_tag)
        self.flush()

        return new_tag

    def remove_conversation_tag(self, conversation_id: Text, tag_id: int) -> None:
        """Removes mapping from conversation_id -> tag_id.

        Args:
            conversation_id: Conversation to remove the tag from.
            tag_id: Tag to be removed.
        """
        conversation = self.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(
                f"No conversation found for conversation ID '{conversation_id}'."
            )

        existing_tag = self._get_tag_by_id(tag_id)
        if not existing_tag:
            raise ValueError(
                f"Tag with id {tag_id} is not assigned to conversation "
                f"{conversation_id}."
            )

        conversation.tags.remove(existing_tag)

    def delete_conversation_tag_by_id(self, tag_id: int) -> None:
        """Delete conversation tag with the given <tag_id>

        Args:
            tag_id: ID of the the tag to be deleted.
        """
        tag = self._get_tag_by_id(tag_id)
        if not tag:
            raise ValueError(f"Tag with id '{tag_id}' was not found.")

        self.delete(tag)
        self.commit()

    def get_all_conversation_tags(self) -> List[Dict[Text, Any]]:
        """Returns all existing conversations tags from database along
        with conversations IDs they are assigned to.

        Returns:
            List of existing conversations tags.
        """
        tags = self.query(ConversationTag).all()
        return [t.as_dict() for t in tags]

    def get_tags_for_conversation_id(
        self, conversation_id: Text
    ) -> List[Dict[Text, Any]]:
        """Returns conversations tags assigned to the given conversation ID.

        Returns:
            List of assigned conversations tags.
        """

        conversation = self.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(
                f"No conversation found for conversation ID '{conversation_id}'."
            )

        return [t.as_dict() for t in conversation.tags]

    def delete_conversation_by_id(self, conversation_id: Text) -> None:
        """Deletes a conversation with specified ID.

        Args:
            conversation_id: ID of a conversation to be deleted.
        """
        conversation = self.get_conversation(conversation_id)
        if not conversation:
            raise ValueError(
                f"No conversation found for conversation ID '{conversation_id}'."
            )

        conversation_session = (
            self.query(ConversationSession)
            .filter(ConversationSession.conversation_id == conversation_id)
            .all()
        )
        if conversation_session:
            self.delete_all(conversation_session)

        self.delete(conversation)
        self.commit()


def continuously_consume(
    endpoint_config: endpoints.EndpointConfig,
    wait_between_failures: int = 5,
    should_run_liveness_endpoint: bool = False,
) -> NoReturn:
    """Consume event consumer continuously.

    Args:
        endpoint_config: Event consumer endpoint config.
        wait_between_failures: Wait time between retries when the consumer throws an
            error during initialisation or consumption.
        should_run_liveness_endpoint: If `True`, runs a simple Sanic server as a
            background process that can be used to probe liveness of this service.
            The service will be exposed at a port defined by the
            `SELF_PORT` environment variable (5673 by default).

    """
    from rasax.community.services.event_consumers.event_consumer import (  # pytype: disable=pyi-error
        EventConsumer,
    )
    import rasax.community.services.event_consumers.utils as consumer_utils  # pytype: disable=pyi-error

    while True:
        consumer: Optional[EventConsumer] = None
        # noinspection PyBroadException
        try:
            consumer = consumer_utils.from_endpoint_config(
                endpoint_config, should_run_liveness_endpoint
            )
            if not consumer:
                rasa_cli_utils.print_error_and_exit(
                    f"Could not configure valid event consumer. Exiting."
                )

            with consumer:
                consumer.consume()

        except ValueError as e:
            warnings.warn(str(e))
            rasa_cli_utils.print_error_and_exit(
                f"Could not configure valid event consumer. Exiting."
            )
        except Exception:
            logger.exception(
                f"Caught an exception while consuming events. "
                f"Will retry in {wait_between_failures} s."
            )
            time.sleep(wait_between_failures)
        finally:  # kill consumer endpoint process
            if consumer:
                consumer.kill_liveness_endpoint_process()


def read_event_broker_config(
    endpoint_config_path: Text = config.endpoints_path,
) -> "EndpointConfig":
    """Read `event_broker` subsection of endpoint config file.

    The endpoint config contains lots of other sections, but only the `event_broker`
    section is relevant. This function creates a temporary file with only that
    section.

    """
    event_broker_key = "event_broker"
    endpoint_config: Optional["EndpointConfig"] = None

    try:
        endpoint_config = rasa_x_utils.extract_partial_endpoint_config(
            endpoint_config_path, event_broker_key
        )
    except ValueError as e:
        warnings.warn(
            f"Could not find endpoint file at path '{config.endpoints_path}':\n{e}."
        )
    except KeyError:
        warnings.warn(
            f"Could not find '{event_broker_key}' section in "
            f"endpoint config file at path '{config.endpoints_path}'."
        )
    except TypeError:
        warnings.warn(
            f"Endpoint config file at path '{endpoint_config_path}' does not contain "
            f"valid sections."
        )
    except Exception as e:
        logger.error(
            f"Encountered unexpected error reading endpoint config "
            f"file at path {config.endpoints_path}:\n{e}"
        )

    return endpoint_config


def wait_for_migrations() -> None:
    """Loop continuously until all database migrations have been executed."""

    logger.info("Waiting until database migrations have been executed...")

    migrations_pending = True
    while migrations_pending:
        try:
            with db_utils.session_scope() as session:
                migrations_pending = not db_utils.is_db_revision_latest(session)
        except Exception as e:
            logger.warning(f"Could not retrieve the database revision due to: {e}.")

        if migrations_pending:
            logger.warning(
                f"Database revision does not match migrations' latest, trying again"
                f" in {_REVISION_CHECK_DELAY} seconds."
            )
            time.sleep(_REVISION_CHECK_DELAY)

    logger.info("Check for database migrations completed.")


def main(should_run_liveness_endpoint: bool = True) -> None:
    """Start an event consumer and consume continuously.

    Args:
        should_run_liveness_endpoint: If `True`, runs a simple Sanic server as a
            background process that can be used to probe liveness of this service.
            The service will be exposed at a port defined by the
            `SELF_PORT` environment variable (5673 by default).

    In server mode a simple Sanic server is run exposing a `/health` endpoint as a
    background process that can be used to probe liveness of this service.
    The endpoint will be exposed at a port defined by the
    `SELF_PORT` environment variable (5673 by default).

    """
    update_log_level()

    logger.info(
        f"Starting event service (standalone: "
        f"{config.should_run_event_consumer_separately})."
    )

    endpoint_config = read_event_broker_config() if not config.LOCAL_MODE else None

    if config.should_run_event_consumer_separately:
        # Before reading/writing from the database (telemetry config and conversation
        # events), make sure migrations have run first. Only do this if we're running
        # as a separate service.
        wait_for_migrations()

        # Start the telemetry process for this service.
        with db_utils.session_scope() as session:
            telemetry.initialize_from_db(session, overwrite_configuration=False)

    continuously_consume(
        endpoint_config, should_run_liveness_endpoint=should_run_liveness_endpoint
    )


if __name__ == "__main__":
    # Override this configuration value, as we know for certain that the event
    # service has been started as a separate service.
    config.should_run_event_consumer_separately = True

    main()
