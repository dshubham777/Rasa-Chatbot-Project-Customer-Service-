import json
import logging
from typing import Dict, Text, Any, Optional, List, Tuple, Union

import time
from sqlalchemy import or_, false, and_
from sqlalchemy.orm import Session

from rasa.core.events import UserUttered, Event
from rasax.community import config
from rasax.community.database.conversation import MessageLog
from rasax.community.database.data import TrainingData
from rasax.community.database.model import Model
from rasax.community.database.service import DbService
from rasax.community.utils import (
    get_text_hash,
    get_query_selectors,
    get_columns_from_fields,
    query_result_to_dict,
    QueryResult,
)

logger = logging.getLogger(__name__)


class LogsService(DbService):
    rasa_team_id = config.team_name

    def __init__(self, session: Session):
        self._buffer = []
        super().__init__(session)

    def fetch_logs(
        self,
        text_query: Optional[Text] = None,
        intent_query: Optional[Text] = None,
        fields_query: Optional[List[Tuple[Text, bool]]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        exclude_training_data: bool = False,
        distinct: bool = True,
    ) -> QueryResult:
        # returns logs, sorts it in reverse-chronological order
        intents = intent_query.split(",") if intent_query else []
        query = True
        if text_query or intent_query:
            query = or_(
                MessageLog.text.like(f"%{text_query}%"), MessageLog.intent.in_(intents)
            )

        columns = get_columns_from_fields(fields_query)
        # map `name` field to `intent`
        columns = [c if c != "name" else "intent" for c in columns]

        exclude_training_data_query = True
        if exclude_training_data:
            from rasa.core.constants import INTENT_MESSAGE_PREFIX

            # exclude if in training data or if the intent was hardcoded
            # we can't use `exist()` here cause that does not work with Oracle
            exclude_training_data_query = and_(
                MessageLog.hash.notin_(self.query(TrainingData.hash).subquery()),
                MessageLog.text.notlike(f"{INTENT_MESSAGE_PREFIX}%"),
            )
        query_selectors = get_query_selectors(MessageLog, columns)
        logs = (
            self.query(*query_selectors)
            .distinct()
            .filter(query)
            .filter(MessageLog.archived == false())
            .filter(exclude_training_data_query)
        )

        # Save the current query state so we can exclude the  `order_by` for the count
        result_count_query = logs

        # Only order by `id` if `id` was selected. Ordering by something which is
        # not in the `select` can lead to problems with the `distinct` query.
        is_id_included = not columns or "id" in columns
        if is_id_included:
            logs = logs.order_by(MessageLog.id.desc())
        elif distinct:
            logs = logs.distinct()
            result_count_query = logs

        total_number_logs = result_count_query.count()

        logs = logs.offset(offset).limit(limit).all()

        if columns:
            results = [query_result_to_dict(r, fields_query) for r in logs]
        else:
            results = [t.as_dict() for t in logs]

        return QueryResult(results, total_number_logs)

    def get_suggestions(
        self,
        text_query: Optional[Text] = None,
        intent_query: Optional[Text] = None,
        fields_query: Optional[List[Tuple[Text, bool]]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        distinct: bool = True,
    ) -> QueryResult:

        return self.fetch_logs(
            text_query,
            intent_query,
            fields_query,
            exclude_training_data=True,
            limit=limit,
            offset=offset,
            distinct=distinct,
        )

    def archive(self, log_id: Text) -> bool:
        log = self.query(MessageLog).filter(MessageLog.id == log_id).first()

        if log:
            log.archived = True
            self.commit()

        return log is not None

    def get_log_by_hash(self, _hash: Text) -> Optional[MessageLog]:
        return self.query(MessageLog).filter(MessageLog.hash == _hash).first()

    def replace_log(
        self,
        existing_log: Dict[Text, Any],
        parse_data: Dict[Text, Any],
        created_from_model: bool = True,
    ) -> Dict[Text, Any]:
        """Replace `existing_log` with log created from `parse_data`.

        `created_from_model` indicates whether `parse_data` has been created by a
        Rasa model.
        """

        new_log = self._create_log(parse_data, created_from_model=created_from_model)
        new_log.id = existing_log["id"]

        self.merge(new_log)
        self.commit()

        return new_log.as_dict()

    def create_log_from_parse_data(
        self,
        parse_data: Dict[Text, Any],
        created_from_model: bool = True,
        event_id: Optional[int] = None,
    ) -> Dict[Text, Any]:
        """Create from `parse_data`.

        `created_from_model` indicates whether `parse_data` has been created by a Rasa model.
        """

        log = self._create_log(parse_data, event_id, created_from_model)
        stored_log = self.get_log_by_hash(log.hash)
        if stored_log:
            # update the old log with the new one,
            # `merge` will use the old id to replace it with the new object
            log.id = stored_log.id
            self.merge(log)
        else:
            self.add(log)
            # flush so id gets assigned
            self.flush()

        inserted = (
            self.query(MessageLog).filter(MessageLog.id == log.id).first().as_dict()
        )

        logger.debug(f"Saving to NLU logs:\n{inserted}")

        return inserted

    def _create_log(
        self,
        parse_data: Dict[Text, Any],
        event_id: Optional[int] = None,
        created_from_model: bool = True,
    ) -> MessageLog:
        if created_from_model:
            project = parse_data.get("project") or config.project_name
            model_result = (
                self.query(Model.id).filter(Model.project_id == project).first()
            )
            if not model_result:
                raise ValueError(f"No model found for project '{project}'.")
            (model_id,) = model_result
        else:
            model_id = None

        text = parse_data.get("text")
        intent = parse_data.get("intent", {})

        return MessageLog(
            model_id=model_id,
            text=text,
            hash=get_text_hash(text),
            intent=intent.get("name"),
            confidence=intent.get("confidence", 0),
            entities=json.dumps(parse_data.get("entities", [])),
            intent_ranking=json.dumps(parse_data.get("intent_ranking", [])),
            time=time.time(),
            event_id=event_id,
        )

    def save_nlu_logs_from_event(
        self, event_data: Union[Text, bytes], event_id: Optional[int] = None
    ) -> Optional[int]:
        try:
            event = Event.from_parameters(json.loads(event_data))
            if isinstance(event, UserUttered):
                log = self.create_log_from_parse_data(
                    event.parse_data, event_id=event_id
                )
                return log["id"]

        except ValueError as e:
            logger.exception(f"Could not persist event '{e}' to NLU logs:\n {e}")
