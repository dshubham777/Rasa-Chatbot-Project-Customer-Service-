import logging
import warnings

import typing
from typing import Text, Optional, Union, List, Tuple, Any

from rasa.core.constants import RASA_EXPORT_PROCESS_ID_HEADER_NAME
from rasa.utils import endpoints
from rasax.community.constants import DEFAULT_RASA_ENVIRONMENT
from rasax.community.services.event_consumers.event_consumer import EventConsumer

if typing.TYPE_CHECKING:
    from pika import BasicProperties
    from pika.adapters.blocking_connection import BlockingChannel
    from pika.spec import Basic

logger = logging.getLogger(__name__)


class PikaEventConsumer(EventConsumer):
    type_name = "pika"

    def __init__(
        self,
        host: Text,
        username: Text,
        password: Text,
        port: Union[Text, int, None] = 5672,
        queue: Optional[Text] = "rasa_production_events",
        should_run_liveness_endpoint: bool = False,
        **kwargs: Any,
    ):
        """Pika event consumer.

        Args:
            host: RabbitMQ host.
            username: RabbitMQ username.
            password: RabbitMQ password.
            port: RabbitMQ port.
            queue: RabbitMQ queue to be consumed.
            should_run_liveness_endpoint: If `True`, runs a simple Sanic server as a
                background process that can be used to probe liveness of this service.
                The service will be exposed at a port defined by the
                `SELF_PORT` environment variable (5673 by default).
            kwargs: Additional kwargs to be processed. If `queue` is not provided, and
                `queues` is present in `kwargs`, the first queue listed under
                `queues` will be used as the queue to consume.
        """
        from rasa.core.brokers import pika

        self.queue = self._get_queue_from_args(queue, kwargs)
        self.host = host
        self.channel = pika.initialise_pika_channel(
            host, self.queue, username, password, port
        )
        super().__init__(should_run_liveness_endpoint)

    @classmethod
    def from_endpoint_config(
        cls,
        consumer_config: Optional[endpoints.EndpointConfig],
        should_run_liveness_endpoint: bool,
    ) -> Optional["PikaEventConsumer"]:
        if consumer_config is None:
            logger.debug(
                "Could not initialise `PikaEventConsumer` from endpoint config."
            )
            return None

        return cls(
            consumer_config.url,
            **consumer_config.kwargs,
            should_run_liveness_endpoint=should_run_liveness_endpoint,
        )

    @staticmethod
    def _get_queue_from_args(
        queue: Union[List[Text], Text, None], kwargs: Any,
    ) -> Text:
        """Get queue to consume for this event consumer.

        Args:
            queue: Value of the supplied `queue` argument.
            kwargs: Additional kwargs supplied to the `PikaEventConsumer` constructor.
                If `queue` is not supplied, the `queues` kwarg will be used instead.

        Returns:
            Queue this event consumer consumes.

        Raises:
            `ValueError` if no valid `queue` or `queues` argument was found.
        """
        queues: Union[List[Text], Text, None] = kwargs.pop("queues", None)

        if queues and isinstance(queues, list):
            first_queue = queues[0]
            if len(queues) > 1:
                warnings.warn(
                    f"Found multiple queues under the `queues` parameter in the pika "
                    f"event consumer config. Will consume the first queue "
                    f"`{first_queue}`."
                )
            return first_queue

        elif queues:
            # `queues` is a string
            return queues  # pytype: disable=bad-return-type

        if queue and isinstance(queue, list):
            first_queue = queue[0]
            if len(queue) > 1:
                warnings.warn(
                    f"Found multiple queues under the `queue` parameter in the pika "
                    f"event consumer config. Will consume the first queue "
                    f"`{first_queue}`."
                )
            return first_queue

        elif queue:
            # `queue` is a string
            return queue  # pytype: disable=bad-return-type

        raise ValueError(
            "Could not initialise `PikaEventConsumer` due to invalid "
            "`queues` or `queue` argument in constructor."
        )

    @staticmethod
    def _origin_from_message_properties(pika_properties: "BasicProperties") -> Text:
        """Fetch message origin from the `app_id` attribute of the message
        properties.

        Args:
            pika_properties: Pika message properties.

        Returns:
            The message properties' `app_id` property if set, otherwise
            `rasax.community.constants.DEFAULT_RASA_ENVIRONMENT`.

        """
        return pika_properties.app_id or DEFAULT_RASA_ENVIRONMENT

    @staticmethod
    def _export_process_id_from_message_properties(
        pika_properties: "BasicProperties",
    ) -> Optional[Text]:
        """Fetch the export process ID header.

        Args:
            pika_properties: Pika message properties.

        Returns:
            The value of the message properties' `rasa-export-process-id` header if
            present.

        """
        headers = pika_properties.headers

        return headers.get(RASA_EXPORT_PROCESS_ID_HEADER_NAME) if headers else None

    # noinspection PyUnusedLocal
    def _callback(
        self,
        ch: "BlockingChannel",
        method: "Basic.Deliver",
        properties: "BasicProperties",
        body: bytes,
    ):
        self.log_event(
            body,
            origin=self._origin_from_message_properties(properties),
            import_process_id=self._export_process_id_from_message_properties(
                properties
            ),
        )

    def consume(self):
        logger.info(f"Start consuming queue '{self.queue}' on pika host '{self.host}'.")
        self.channel.basic_consume(self.queue, self._callback, auto_ack=True)
        self.channel.start_consuming()
