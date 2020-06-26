import logging

import time

from rasax.community.constants import DEFAULT_RASA_ENVIRONMENT
from rasax.community.services.event_consumers.event_consumer import EventConsumer

logger = logging.getLogger(__name__)


class SQLiteEventConsumer(EventConsumer):
    type_name = "sql"

    def __init__(self, should_run_liveness_endpoint: bool = False):
        from rasa.core.brokers.sql import SQLEventBroker

        self.producer = SQLEventBroker()
        super().__init__(should_run_liveness_endpoint)

    def consume(self):
        logger.info("Start consuming SQLite events from database 'events.db'.")
        with self.producer.session_scope() as session:
            while True:
                new_events = (
                    session.query(self.producer.SQLBrokerEvent)
                    .order_by(self.producer.SQLBrokerEvent.id.asc())
                    .all()
                )

                for event in new_events:
                    self.log_event(
                        event.data,
                        sender_id=event.sender_id,
                        event_number=event.id,
                        origin=DEFAULT_RASA_ENVIRONMENT,
                    )
                    session.delete(event)
                    session.commit()

                time.sleep(0.01)
