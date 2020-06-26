import json
import logging
import uuid
from typing import Text, Optional, List, Dict, Any, Union, Tuple

import math
import time
from sqlalchemy import func, false, distinct, Integer, cast, Float
from sqlalchemy.util import KeyedTuple

from rasax.community import config, utils as rasa_x_utils
import rasax.community.tracker_utils as tracker_utils
from rasax.community.database.analytics import ConversationSession, AnalyticsCache
from rasax.community.database.conversation import (
    MessageLog,
    ConversationEvent,
    Conversation,
)
from rasax.community.database.service import DbService
from rasax.community.services.user_service import UserService

logger = logging.getLogger(__name__)

CACHED_ANALYTICS_CONFIG = {
    "12m": {"window": "P1M", "range": "P12M"},
    "30d": {"window": "P1D", "range": "P1M"},
    "24h": {"window": "PT1H", "range": "P1D"},
}

EMPTY_BUCKET_RESULT = KeyedTuple(
    [0, 0, 0, 0, 0, 0, 0],
    labels=[
        "new_users",
        "conversations",
        "user_messages",
        "bot_messages",
        "sessions_per_user",
        "conversation_length",
        "conversation_steps",
    ],
)


class AnalyticsService(DbService):
    def analytics_result_with_cutoff_time(
        self,
        cache_key: Text,
        include_platform_users: bool = False,
        cutoff_time: Optional[float] = None,
    ) -> Optional[Dict[Text, Any]]:
        """Returns analytics result if timestamp meets a cutoff condition.

        The result timestamp has to be greater than or equal to cutoff_time.
        If not specified, cutoff_time is defined as the current time minus the
        bin width of the result."""

        result = self.get_cached_analytics_result(cache_key, include_platform_users)

        if not result:
            return None

        if not cutoff_time:
            cutoff_time = time.time() - result["result"]["bin_width"]

        if result["timestamp"] >= cutoff_time:
            return result["result"]

        return None

    def get_cached_analytics_result(
        self, cache_key: Text, include_platform_users: bool = False
    ) -> Optional[Dict[Text, Any]]:
        result = (
            self.query(AnalyticsCache)
            .filter(
                AnalyticsCache.cache_key == cache_key,
                AnalyticsCache.includes_platform_users == include_platform_users,
            )
            .first()
        )

        if result:
            return result.as_dict()
        else:
            return None

    @staticmethod
    def run_analytics_caching() -> None:
        import rasax.community.database.utils as database_utils

        with database_utils.session_scope() as session:
            # Use new session for all sql operations
            analytics_service = AnalyticsService(session)

            now = time.time()
            user_service = UserService(session)
            platform_users = user_service.fetch_all_users(config.team_name)
            platform_user_ids = [u["username"] for u in platform_users]

            for k, v in CACHED_ANALYTICS_CONFIG.items():
                window = rasa_x_utils.duration_to_seconds(v["window"])
                start = now - rasa_x_utils.duration_to_seconds(v["range"])

                for include_platform_users in [False, True]:
                    result = analytics_service.calculate_analytics(
                        start, now, window, platform_user_ids
                    )
                    analytics_service._persist_analytics(
                        k, result, include_platform_users
                    )

    def calculate_analytics(
        self,
        start: Optional[float] = None,
        until: Optional[float] = None,
        window: Optional[float] = None,
        sender_ids_to_exclude: List[Text] = None,
    ) -> Dict[Text, Any]:
        """Retrieves analytics between `start` and `until`.

        Accumulates the result in bins of size `window`, and returns 10 bins
        otherwise. Excludes `sender_ids_to_exclude` if specified."""

        n_buckets, start, until, window = self._get_histogram_parameters(
            start, until, window
        )

        bucket_number = cast(
            (ConversationSession.session_start - start) / window, Integer
        )

        result = (
            self.query(
                bucket_number,
                # New users in this bucket
                func.sum(ConversationSession.is_new_user).label("new_users"),
                # Total number of conversations in bucket
                func.count(distinct(Conversation.sender_id)).label("conversations"),
                # Total number of user messages in bucket
                func.sum(ConversationSession.user_messages).label("user_messages"),
                # Total number of bot messages in bucket
                func.sum(ConversationSession.bot_messages).label("bot_messages"),
                # Average number of sessions per user in bucket
                (
                    func.count()
                    / cast(  # cast to float since it's otherwise an integer division
                        func.count(distinct(Conversation.sender_id)), Float
                    )
                ).label("sessions_per_user"),
                # Average session length in bucket
                func.avg(ConversationSession.session_length).label(
                    "conversation_length"
                ),
                # Average user messages per session in bucket
                func.avg(ConversationSession.user_messages).label("conversation_steps"),
            )
            .filter(
                ConversationSession.session_start >= start,
                ConversationSession.session_start <= until,
            )
            .join(Conversation)
            .group_by(bucket_number)
        )

        if sender_ids_to_exclude:
            result = result.filter(Conversation.sender_id.notin_(sender_ids_to_exclude))

        result = result.all()
        result = {r[0]: r for r in result}

        return self._query_result_to_analytics_result(result, n_buckets, start, window)

    def _get_histogram_parameters(
        self,
        start: Optional[float] = None,
        until: Optional[float] = None,
        window: Optional[float] = None,
    ) -> Tuple[int, float, float, float]:

        if start is None:
            start = self._time_of_first_user_utterance([])
        if until is None:
            until = self._time_of_last_user_utterance([])
        if window:
            n_bins = math.ceil((until - start) / window)
        else:
            n_bins = config.default_analytics_bins
            window = math.ceil((until - start) / n_bins)

        return n_bins, start, until, window

    def _time_of_first_user_utterance(self, sender_ids_to_exclude: List[Text]) -> float:
        result = (
            self.query(func.min(MessageLog.time))
            .filter(
                MessageLog.event.has(
                    ConversationEvent.conversation.has(
                        Conversation.sender_id.in_(sender_ids_to_exclude) == false()
                    )
                )
            )
            .first()
        )

        if result and result[0]:
            return result[0]
        else:
            return 0

    def _time_of_last_user_utterance(self, sender_ids_to_exclude: List[Text]) -> float:
        result = (
            self.query(func.max(MessageLog.time))
            .filter(
                MessageLog.event.has(
                    ConversationEvent.conversation.has(
                        Conversation.sender_id.in_(sender_ids_to_exclude) == false()
                    )
                )
            )
            .first()
        )
        if result and result[0]:
            return result[0]
        else:
            return time.time()

    def _query_result_to_analytics_result(
        self, result: Dict[int, KeyedTuple], n_buckets: int, start: float, window: float
    ):
        lower_bucket_edges = [start + n * window for n in range(n_buckets)]

        if lower_bucket_edges and len(lower_bucket_edges) > 1:
            bucket_width = lower_bucket_edges[1] - lower_bucket_edges[0]
        else:
            bucket_width = None

        analytics = self._analytics_result_template()
        analytics["bin_centers"] = [e + window / 2 for e in lower_bucket_edges]
        analytics["bin_width"] = bucket_width

        for i in range(n_buckets):
            bucket_result = result.get(i, EMPTY_BUCKET_RESULT)
            # pytype: disable=attribute-error
            analytics["new_users"].append(bucket_result.new_users)
            analytics["conversations"].append(bucket_result.conversations)

            user_messages = bucket_result.user_messages
            analytics["user_messages"].append(user_messages)

            bot_messages = bucket_result.bot_messages
            analytics["bot_messages"].append(bot_messages)

            analytics["total_messages"].append(user_messages + bot_messages)

            analytics["sessions_per_user"].append(bucket_result.sessions_per_user)
            analytics["conversation_length"].append(bucket_result.conversation_length)
            analytics["conversation_steps"].append(bucket_result.conversation_steps)
            # pytype: enable=attribute-error

        return analytics

    @staticmethod
    def _analytics_result_template() -> Dict[Text, Union[List[float], float]]:
        return {
            "bin_centers": [],
            "bin_width": 0.0,
            "new_users": [],
            "conversations": [],
            "user_messages": [],
            "bot_messages": [],
            "total_messages": [],
            "sessions_per_user": [],
            "conversation_length": [],
            "conversation_steps": [],
        }

    @staticmethod
    def _retrieve_analytic_row_result(
        row_index: int, column_index: int, analytics: Dict[int, List[float]]
    ) -> float:
        result = analytics.get(row_index)
        if result:
            return result[column_index]
        else:
            return 0

    def _persist_analytics(
        self,
        cache_key: Text,
        result: Dict[Text, Any],
        include_platform_users: bool = False,
    ):
        old_analytics = (
            self.query(AnalyticsCache)
            .filter(
                AnalyticsCache.cache_key == cache_key,
                AnalyticsCache.includes_platform_users == include_platform_users,
            )
            .first()
        )

        result = json.dumps(result, cls=rasa_x_utils.DecimalEncoder)

        if old_analytics:
            old_analytics.result = result
        else:
            self.add(
                AnalyticsCache(
                    cache_key=cache_key,
                    includes_platform_users=include_platform_users,
                    result=result,
                )
            )

        self.commit()

    def save_analytics(
        self, body: Union[Text, bytes], sender_id: Optional[Text] = None
    ) -> None:
        logger.debug(f"Saving to AnalyticsService:\n{body}")
        event = json.loads(body)
        if sender_id:
            event["sender_id"] = sender_id

        self.update_session_data(event)

    def update_session_data(self, event: Dict[Text, Any]) -> None:
        event_name = event.get("event")
        event_timestamp = event.get("timestamp") or time.time()
        sender_id = event.get("sender_id") or uuid.uuid4().hex
        policy = event.get("policy")
        latest_session = self._latest_session(sender_id)
        if not latest_session or tracker_utils.is_session_started_event(event_name):
            latest_session = self._create_new_session(
                sender_id, event_timestamp, latest_session
            )

        self._update_session(latest_session, event_name, event_timestamp, policy)

    def _latest_session(self, sender_id: Text) -> Optional[ConversationSession]:
        return (
            self.query(ConversationSession)
            .filter(ConversationSession.conversation_id == sender_id)
            .order_by(ConversationSession.session_id.desc())
            .first()
        )

    def _create_new_session(
        self,
        sender_id: Text,
        event_timestamp: float,
        old_session: Optional[ConversationSession] = None,
    ) -> ConversationSession:
        session_id = old_session.session_id + 1 if old_session else 0
        new_session = ConversationSession(
            conversation_id=sender_id,
            session_id=session_id,
            session_start=event_timestamp,
            # use an `int` to indicate a new user
            # since we then can use `count` to
            # count the new users
            is_new_user=int(session_id == 0),
            in_training_data=True,
            user_messages=0,
            bot_messages=0,
        )
        self.add(new_session)

        return new_session

    def _update_session(
        self,
        conversation_session: ConversationSession,
        event_name: Text,
        event_timestamp: float,
        policy: Optional[Text],
    ):
        conversation_session.session_length = (
            event_timestamp - conversation_session.session_start
        )
        conversation_session.latest_event_time = event_timestamp

        if policy and conversation_session.in_training_data:
            conversation_session.in_training_data = tracker_utils.is_predicted_event_in_training_data(
                policy
            )

        if tracker_utils.is_bot_event(event_name):
            conversation_session.bot_messages += 1
        elif tracker_utils.is_user_event(event_name):
            conversation_session.user_messages += 1
