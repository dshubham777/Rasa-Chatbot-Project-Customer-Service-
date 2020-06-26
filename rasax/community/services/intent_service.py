import logging
from typing import Text, Dict, List, Union, Optional, Set, Tuple

from sqlalchemy import and_
from sqlalchemy.orm import Session

from rasax.community.database.intent import Intent, TemporaryIntentExample
from rasax.community.database.service import DbService
from rasax.community.services.data_service import DataService
from rasax.community.services.domain_service import DomainService
from rasax.community.services.logs_service import LogsService
from rasax.community.services.user_goal_service import GOAL_INTENTS_KEY, GOAL_NAME_KEY
from rasax.community.services.user_goal_service import UserGoalService

logger = logging.getLogger(__name__)

INTENT_NAME_KEY = "intent"
INTENT_MAPPED_TO_KEY = "mapped_to"
INTENT_USER_GOAL_KEY = "user_goal"
INTENT_IS_TEMPORARY_KEY = "is_temporary"
INTENT_EXAMPLES_KEY = "example_hashes"
INTENT_SUGGESTIONS_KEY = "suggestions"


class IntentService(DbService):
    def __init__(self, session: Session):
        self.domain_service = DomainService(session)
        self.data_service = DataService(session)
        super().__init__(session)

    def add_temporary_intent(self, intent: Dict[str, str], project_id: Text) -> None:
        """Creates a new temporary intent.

        Args:
            intent: The intent object which should be added.
            project_id: The project id which the intent should belong to.
        """
        mapped_to = intent.get(INTENT_MAPPED_TO_KEY)
        permanent_intents = self.get_permanent_intents(project_id)

        if mapped_to and mapped_to not in permanent_intents:
            raise ValueError(
                "Intent cannot be mapped to '{}' since this intent"
                " does not exist.".format(mapped_to)
            )

        intent_name = intent[INTENT_NAME_KEY]
        existing = self._get_temporary_intent(intent_name, project_id)

        if existing is not None:
            examples = existing.temporary_examples
            if INTENT_MAPPED_TO_KEY in intent.keys() and mapped_to is None:
                for e in examples:
                    self.data_service.delete_example_by_hash(project_id, e.example_hash)
            else:
                self.data_service.update_intent(
                    mapped_to or intent_name,
                    [e.example_hash for e in examples],
                    project_id,
                )

        temporary = Intent(
            name=intent_name,
            mapped_to=mapped_to,
            project_id=project_id,
            is_temporary=True,
        )
        if existing:
            temporary.id = existing.id
            self.merge(temporary)
        else:
            self.add(temporary)

    def get_temporary_intents(
        self, project_id: Text, include_example_hashes: bool = True
    ) -> List[Dict[str, str]]:
        """Returns all temporary intents.

        Args:
            project_id: Project id of the temporary intents.
            include_example_hashes: If `True` include the hashes of the examples for
                                    each intent.

        Returns:
            List of intent objects.
        """

        temporary_intents = (
            self.query(Intent)
            .filter(and_(Intent.project_id == project_id, Intent.is_temporary))
            .all()
        )

        return [t.as_dict(include_example_hashes) for t in temporary_intents]

    def get_temporary_intent(
        self, intent_id: Text, project_id: Text
    ) -> Optional[Dict[Text, Text]]:
        """Returns a single temporary intent matching the name.

        Args:
            intent_id: Name of the temporary intent which is searched for.
            project_id: Project id which the temporary intent belongs to.

        Returns:
            The intent object if the temporary intent was found, otherwise
            `None`.
        """

        intent = self._get_temporary_intent(intent_id, project_id)

        if intent:
            return intent.as_dict()
        else:
            return None

    def _get_temporary_intent(self, intent_id: Text, project_id: Text) -> Intent:
        return (
            self.query(Intent)
            .filter(
                and_(
                    Intent.project_id == project_id,
                    Intent.is_temporary,
                    Intent.name == intent_id,
                )
            )
            .first()
        )

    def update_temporary_intent(self, intent_id: Text, intent: Dict, project_id: Text):
        """Update an existing temporary intent.

        Args:
            intent_id: Temporary intent which should be updated.
            intent: The intent object which is used to update the
                    existing intent.
            project_id: Project id of the temporary intent.
        """
        if intent.get(INTENT_NAME_KEY) != intent_id:
            raise ValueError(
                "Intent name '{}' cannot be changed to '{}'."
                "".format(intent_id, intent.get(INTENT_NAME_KEY))
            )

        self.add_temporary_intent(intent, project_id)

    def delete_temporary_intent(self, intent_id: Text, project_id: Text):
        """Deletes a temporary intent and related training data.

        Args:
            intent_id: Name of the temporary intent which should be deleted.
            project_id: Project id of the intent.
        """
        existing = self._get_temporary_intent(intent_id, project_id)

        if existing:
            for e in existing.temporary_examples:
                self.data_service.delete_example_by_hash(project_id, e.example_hash)

            self.delete(existing)

    def get_permanent_intents(self, project_id: Text) -> Set[Text]:
        """Returns a list of all permanent (not temporary) intents

        Args:
            project_id: Project id which these intents belong to.

        Returns:
            Set of unique permanent intents.
        """
        intents = self.domain_service.get_intents_from_domain(project_id)
        intents |= {
            i[INTENT_NAME_KEY] for i in self.data_service.get_intents(project_id)
        }

        return intents  # pytype: disable=bad-return-type

    def get_intents(
        self,
        project_id: Text,
        include_temporary_intents: bool = True,
        fields_query: List[Tuple[Text, bool]] = False,
    ) -> List[Dict[str, Union[str, List[str]]]]:
        """Returns all intents including related data.

        Args:
            project_id: Project id which these intents belong to.
            include_temporary_intents: If `False` exclude temporary intents.
            fields_query: Query to select which fields should be included/excluded.

        Returns:
            List of intent objects including lists of `suggestions`,
            related `training_data`, and `user_goal`.
        """

        fields_query = fields_query or []
        include_examples = (INTENT_EXAMPLES_KEY, False) not in fields_query
        intents = self.data_service.get_intents(project_id, include_examples)
        domain_intents = self.domain_service.get_intents_from_domain(project_id)

        flat_intents = [i[INTENT_NAME_KEY] for i in intents]

        for intent_name in domain_intents:
            if intent_name not in flat_intents:
                intents.append({INTENT_NAME_KEY: intent_name})

        include_suggestions = (INTENT_SUGGESTIONS_KEY, False) not in fields_query
        if include_suggestions:
            self._add_suggestion_to_intents(intents)

        if include_temporary_intents:
            intents += self.get_temporary_intents(project_id, include_examples)

        include_user_goals = (INTENT_USER_GOAL_KEY, False) not in fields_query
        if include_user_goals:
            self._add_user_goals_to_intents(intents, project_id)

        return intents

    def _add_suggestion_to_intents(
        self, intents: List[Dict[str, Union[str, List[str]]]]
    ) -> None:
        logs_service = LogsService(self.session)
        for i in intents:
            suggestions, _ = logs_service.get_suggestions(
                intent_query=i[INTENT_NAME_KEY], fields_query=[("hash", True)]
            )
            i[INTENT_SUGGESTIONS_KEY] = [s.get("hash") for s in suggestions]

    def _add_user_goals_to_intents(
        self, intents: List[Dict[str, Union[str, List[str]]]], project_id: Text
    ) -> None:
        # merge user goals
        user_goal_service = UserGoalService(self.session)
        user_goals = user_goal_service.get_user_goals(project_id)
        for g in user_goals:
            for i in intents:
                if i[INTENT_NAME_KEY] in g[GOAL_INTENTS_KEY]:
                    i[INTENT_USER_GOAL_KEY] = g[GOAL_NAME_KEY]

    def add_example_to_temporary_intent(
        self, intent: Text, example_hash: Text, project_id: Text
    ) -> None:
        """Adds a training example to a temporary intent.

        Args:
            intent: Name of the intent which the example should be added to.
            example_hash: Text hash of the training example.
            project_id: Project id of the temporary intent.
        """

        intent = self._get_temporary_intent(intent, project_id)
        if intent:
            example = TemporaryIntentExample(
                intent_id=intent.id, example_hash=example_hash
            )
            self.add(example)

        else:
            logger.warning(
                "Intent '{}' was not found. Hence, the example with "
                "hash '{}' could not be deleted."
                "".format(intent, example_hash)
            )

    def remove_example_from_temporary_intent(
        self, intent: Text, example_hash: Text, project_id: Text
    ) -> None:
        """Remove a training example to a temporary intent.

        Args:
            intent: Name of the intent which the example should be removed
                from.
            example_hash: Text hash of the training example.
            project_id: Project id of the temporary intent.
        """

        temporary_intent = self._get_temporary_intent(intent, project_id)
        example = (
            self.query(TemporaryIntentExample)
            .filter(
                and_(
                    TemporaryIntentExample.intent_id == temporary_intent.id,
                    TemporaryIntentExample.example_hash == example_hash,
                )
            )
            .first()
        )
        if example:
            self.delete(example)
