import logging
import typing
from typing import Text, List, Optional, Union, Dict

import time

import rasax.community.config as rasa_x_config
from rasa.utils import endpoints
from rasax.community.services.event_consumers.event_consumer import EventConsumer

if typing.TYPE_CHECKING:
    from kafka.structs import TopicPartition
    from kafka.consumer.fetcher import ConsumerRecord

logger = logging.getLogger(__name__)


class KafkaEventConsumer(EventConsumer):
    type_name = "kafka"

    def __init__(
        self,
        host: List[Text],
        topic: Text,
        security_protocol: Text = "PLAINTEXT",
        sasl_username: Union[Text, int, None] = None,
        sasl_password: Optional[Text] = None,
        ssl_cafile: Optional[Text] = None,
        ssl_certfile: Optional[Text] = None,
        ssl_keyfile: Optional[Text] = None,
        ssl_check_hostname: bool = False,
        should_run_liveness_endpoint: bool = False,
    ):
        """Kafka event consumer.

        Args:
            host: 'host[:port]' string (or list of 'host[:port]'
                strings) that the consumer should contact to bootstrap initial
                cluster metadata. This does not have to be the full node list.
                It just needs to have at least one broker that will respond to a
                Metadata API Request. The default port is 9092. If no servers are
                specified, it will default to `localhost:9092`.
            topic: Topics to subscribe to. If not set, call subscribe() or assign()
                before consuming records
            sasl_username: Username for sasl PLAIN authentication.
                Required if `sasl_mechanism` is `PLAIN`.
            sasl_password: Password for sasl PLAIN authentication.
                Required if `sasl_mechanism` is PLAIN.
            ssl_cafile: Optional filename of ca file to use in certificate
                verification. Default: None.
            ssl_certfile: Optional filename of file in pem format containing
                the client certificate, as well as any ca certificates needed to
                establish the certificate's authenticity. Default: None.
            ssl_keyfile: Optional filename containing the client private key.
                Default: None.
            ssl_check_hostname: Flag to configure whether ssl handshake
                should verify that the certificate matches the brokers hostname.
                Default: False.
            security_protocol: Protocol used to communicate with brokers.
                Valid values are: PLAINTEXT, SSL, SASL_PLAINTEXT, SASL_SSL.
                Default: PLAINTEXT.
            should_run_liveness_endpoint: If `True`, runs a simple Sanic server as a
                background process that can be used to probe liveness of this service.
                The service will be exposed at a port defined by the
                `SELF_PORT` environment variable (5673 by default).

        """
        self.host = host
        self.topic = topic
        self.security_protocol = security_protocol
        self.sasl_username = sasl_username
        self.sasl_password = sasl_password
        self.ssl_cafile = ssl_cafile
        self.ssl_certfile = ssl_certfile
        self.ssl_keyfile = ssl_keyfile
        self.ssl_check_hostname = ssl_check_hostname
        self.consumer: Optional["KafkaConsumer"] = None
        super().__init__(should_run_liveness_endpoint)

    @classmethod
    def from_endpoint_config(
        cls,
        consumer_config: Optional[endpoints.EndpointConfig],
        should_run_liveness_endpoint: bool = not rasa_x_config.LOCAL_MODE,
    ) -> Optional["KafkaEventConsumer"]:
        if consumer_config is None:
            logger.debug(
                "Could not initialise `KafkaEventConsumer` from endpoint config."
            )
            return None

        return cls(
            consumer_config.url,
            **consumer_config.kwargs,
            should_run_liveness_endpoint=should_run_liveness_endpoint,
        )

    def _create_consumer(self) -> None:
        # noinspection PyPackageRequirements
        import kafka

        if "plaintext" in self.security_protocol.lower():
            self.consumer = kafka.KafkaConsumer(
                self.topic,
                bootstrap_servers=self.host,
                security_protocol="PLAINTEXT",
                sasl_mechanism="PLAIN",
                sasl_plain_username=self.sasl_username,
                sasl_plain_password=self.sasl_password,
                ssl_check_hostname=False,
            )
        elif "ssl" in self.security_protocol.lower():
            self.consumer = kafka.KafkaConsumer(
                self.topic,
                bootstrap_servers=self.host,
                security_protocol="SSL",
                ssl_cafile=self.ssl_cafile,
                ssl_certfile=self.ssl_certfile,
                ssl_keyfile=self.ssl_keyfile,
                ssl_check_hostname=self.ssl_check_hostname,
            )
        else:
            raise ValueError(
                f"Cannot initialise `kafka.KafkaConsumer` "
                f"with security protocol '{self.security_protocol}'."
            )

    def consume(self):
        self._create_consumer()
        logger.info(
            f"Start consuming topic '{self.topic}' on Kafka host '{self.host}'."
        )
        while True:
            records: Dict[
                "TopicPartition", List["ConsumerRecord"]
            ] = self.consumer.poll()

            # records contain only one topic, so we can just get all values
            for messages in records.values():
                for message in messages:
                    self.log_event(message.value)

            time.sleep(0.01)
