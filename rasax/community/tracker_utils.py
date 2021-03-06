import logging

import itertools
import typing
from typing import Text, Optional, List, Dict, Any, Union, Type

from rasa.core.actions.action import ACTION_LISTEN_NAME, ACTION_SESSION_START_NAME
from rasa.core.events import SessionStarted, ActionExecuted, UserUttered, BotUttered
from rasa.core.policies import SimplePolicyEnsemble

if typing.TYPE_CHECKING:
    from rasa.core.events import Event

logger = logging.getLogger(__name__)


def is_action_listen(event: Union[Dict[Text, Any], Text, None]) -> bool:
    """Determine whether `event` is an `action_listen`.

    Args:
        event: Event or event type name to check.

    Returns:
        `True` if `event` is an `action_listen` event, otherwise `False`.
    """
    if not event:
        return False

    if isinstance(event, str):
        return event == ACTION_LISTEN_NAME

    return event.get("name") == ACTION_LISTEN_NAME


def is_action_session_start(event: Union[Dict[Text, Any], Text, None]) -> bool:
    """Determine whether `event` is an `action_session_start`.

    Args:
        event: Event or event type name to check.

    Returns:
        `True` if `event` is an `action_session_start` event, otherwise `False`.
    """
    if not event:
        return False

    if isinstance(event, str):
        return event == ACTION_SESSION_START_NAME

    return event.get("name") == ACTION_SESSION_START_NAME


def _is_event_of_type(
    event: Union[Dict[Text, Any], Text, None], target_type: Type["Event"]
) -> bool:
    """Check whether an event is of type `event_type`.

    Args:
        event: Event or event type name to check.
        target_type: Target event type to compare against.

    Returns:
        `True` if `event` is of type `event_type`, otherwise `False`.
    """
    if not event:
        return False

    if isinstance(event, str):
        return event == target_type.type_name

    return event.get("event") == target_type.type_name


def is_user_event(event: Union[Dict[Text, Any], Text, None]) -> bool:
    """Check whether an event is a `UserUttered` event.

    Args:
        event: Event or event type name to check.

    Returns:
        `True` if `event` is a `UserUttered` event, otherwise `False`.
    """
    return _is_event_of_type(event, UserUttered)


def is_action_event(event: Union[Dict[Text, Any], Text, None]) -> bool:
    """Check whether an event is an `ActionExecuted` event.

    Args:
        event: Event or event type name to check.

    Returns:
        `True` if `event` is an `ActionExecuted` event, otherwise `False`.
    """
    return _is_event_of_type(event, ActionExecuted)


def is_bot_event(event: Union[Dict[Text, Any], Text, None]) -> bool:
    """Check whether an event is a `BotUttered` event.

    Args:
        event: Event or event type name to check.

    Returns:
        `True` if `event` is a `BotUttered` event, otherwise `False`.
    """
    return _is_event_of_type(event, BotUttered)


def is_session_started_event(event: Union[Dict[Text, Any], Text, None]) -> bool:
    """Check whether an event is a `SessionStarted` event.

    Args:
        event: Event or event type name to check.

    Returns:
        `True` if `event` is a `SessionStarted` event, otherwise `False`.
    """
    return _is_event_of_type(event, SessionStarted)


def remove_leading_action_session_start_from(tracker: Dict[Text, Any]) -> None:
    """Remove a leading `ACTION_SESSION_START` if present in `tracker`."""

    if tracker["events"] and is_action_session_start(tracker["events"][0]):
        tracker["events"] = tracker["events"][1:]


def index_of_session_start_sequence_end(events: List[Dict[Text, Any]]) -> Optional[int]:
    """Determine the index of the end of the session start sequence in `events`.

    A session start sequence is a sequence of

        [`action_session_start`, `session_started`, ..., `action_listen`],

    where '...' can be any list of events excluding `action_listen` events.

    Args:
        events: Serialised events to inspect.

    Returns:
        The index of the first `ACTION_LISTEN` after a session start sequence if
        present, `None` otherwise.
    """

    # there must be at least three events if `events` begins with a session start
    # sequence
    if len(events) < 3:
        return None

    # for this to be a session start, the first two events must be
    # `action_session_start` and `session_started`
    if not (is_action_session_start(events[0]) and is_session_started_event(events[1])):
        return None

    # now there may be any number of events, but the session start sequence will end
    # with the first `ACTION_LISTEN`
    # start the enumeration at 2 as the first two events are
    # `action_session_start` and `session_started`
    return next(
        (i for i, event in enumerate(events[2:], 2) if is_action_listen(event)), None
    )


def timestamp_of_session_start(events: List[Dict]) -> Optional[float]:
    """Return timestamp of first `ACTION_LISTEN` event if the tracker
    contains a session start sequence and only one `ACTION_LISTEN`."""

    end_of_session_start = index_of_session_start_sequence_end(events)

    if end_of_session_start is not None:
        return events[end_of_session_start]["timestamp"]

    return None


def timestamp_of_first_action_listen(events: List[Dict]) -> float:
    """Return timestamp of first tracker event if the tracker contains only one
    event that is an ACTION_LISTEN event, 0 otherwise."""

    if events and len(events) == 1:
        event = events[0]
        if is_action_listen(event):
            return event["timestamp"]

    return 0


def is_predicted_event_in_training_data(policy: Optional[Text]):
    """Determine whether event predicted by `policy` was in the training data.

    A predicted event is considered to be in training data if it was predicted by
    the `MemoizationPolicy` or the `AugmentedMemoizationPolicy`.

    Args:
        policy: Policy of the predicted event.

    Returns:
        `True` if the event was not predicted, otherwise `True` if it was not
        predicted by a memo policy, else `False`.
    """
    if not policy:
        # event was not predicted by a policy
        return True

    return not SimplePolicyEnsemble.is_not_memo_policy(policy)
