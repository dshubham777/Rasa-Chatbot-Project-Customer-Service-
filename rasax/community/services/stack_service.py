import asyncio  # pytype: disable=pyi-error

import logging
import typing
from typing import Any, Dict, List, Optional, Text, Union, Tuple

import aiohttp
from aiohttp import ClientError, ClientSession
from concurrent.futures import TimeoutError  # pytype: disable=pyi-error

import rasax.community.jwt
from rasa.core.events import Restarted
from rasa.core.trackers import EventVerbosity
from rasa.core.utils import dump_obj_as_yaml_to_string
from rasa.nlu.training_data.formats import MarkdownWriter
import rasa.constants
from rasa.nlu.constants import RESPONSE_KEY_ATTRIBUTE
from rasa.utils.endpoints import EndpointConfig
import rasa.cli.utils as rasa_cli_utils
from rasax.community import config
from rasax.community.constants import (
    SHARE_YOUR_BOT_CHANNEL_NAME,
    DEFAULT_CHANNEL_NAME,
    INVALID_RASA_VERSION,
)
from rasax.community.services.user_service import GUEST

if typing.TYPE_CHECKING:
    from rasax.community.services.data_service import DataService
    from rasax.community.services.story_service import StoryService
    from rasax.community.services.domain_service import DomainService
    from rasax.community.services.settings_service import (  # pytype: disable=pyi-error
        SettingsService,
    )

logger = logging.getLogger(__name__)

RASA_VERSION_KEY = "version"
INCLUDE_EVENTS_QUERY_PARAM = "include_events"
TOKEN_QUERY_PARAM = "token"


class RasaCredentials(typing.NamedTuple):
    """Credentials to connect and authenticate to a Rasa Open Source environment."""

    url: Text
    token: Text


class StackService:
    """Connects to a running Rasa server.

    Used to retrieve information about models and conversations."""

    def __init__(
        self,
        credentials: RasaCredentials,
        data_service: "DataService",
        story_service: "StoryService",
        domain_service: "DomainService",
        settings_service: "SettingsService",
    ) -> None:
        """Create a `StackService` instance.

        Args:
            credentials: Credentials for connecting to the Rasa instance.
            data_service: Service to obtain the current NLU training data.
            story_service: Service to obtain the Rasa Core stories training data.
            domain_service: Service to obtain the Rasa Core domain for the training.
            settings_service: Service to obtain the current model config.
        """

        self.rasa_credentials = credentials
        self.data_service = data_service
        self.story_services = story_service
        self.domain_service = domain_service
        self.settings_service = settings_service

    async def version(self, timeout_in_seconds: Optional[float] = None) -> Any:
        """Call the `/version` endpoint of the Rasa Open Source API.

        Response Example:
        {
            "version": "1.9.5",
            "minimum_compatible_version": "1.9.0"
        }

        Args:
            timeout_in_seconds: Request timeout in seconds which is used for the call.

        Returns:
            Request response.
        """

        async with self._session() as session:
            response = await session.get(
                self._request_url("/version"),
                params=self._query_parameters(),
                timeout=timeout_in_seconds,
            )
            return await response.json()

    @staticmethod
    def _session() -> ClientSession:
        """Create session for requests to a Rasa Open Source instance.

        Returns:
            Session with default configuration.
        """
        return aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=rasa.constants.DEFAULT_REQUEST_TIMEOUT),
            raise_for_status=True,
        )

    def _request_url(self, sub_path: Text) -> Text:
        """Create the full URL for requests to the Rasa Open Source instance.

        Args:
            sub_path: Path of the resource which should be requested. This is the part
                after the host or port in the URL.

        Returns:
            Full URL.
        """
        import urllib.parse as urllib

        return urllib.urljoin(self.rasa_credentials.url, sub_path)

    def _query_parameters(
        self, params: Optional[Dict[Text, Any]] = None
    ) -> Dict[Text, Any]:
        """Create the query parameters for a request to the Rasa Open Source instance.

        Args:
            params: Optional query parameters for the requests.

        Returns:
            Passed query parameters including the default query parameters.
        """
        params = params or {}
        if self.rasa_credentials.token:
            params[TOKEN_QUERY_PARAM] = self.rasa_credentials.token

        return params

    async def rasa_version(self, timeout_in_seconds: Optional[float] = None) -> Text:
        """Retrieve the Rasa Open Source version.

        Calls the `/version` endpoint of the Rasa Open Source API and extracts
        the Rasa Open Source version from the response.

        Args:
            timeout_in_seconds: Request timeout in seconds for getting the version.

        Returns:
            The version or `0.0.0` in case there was an error.
        """
        try:
            status_response = await self.version(timeout_in_seconds)
            return status_response.get(RASA_VERSION_KEY, INVALID_RASA_VERSION)
        except (ClientError, TimeoutError) as e:
            # Use `debug` logging level to avoid printing logs when Rasa Open Source
            # is not yet up
            logger.debug(f"Error when retrieving version from Rasa Open Source. {e}")
            return INVALID_RASA_VERSION

    async def has_active_model(self) -> bool:
        """Returns whether service has an active model."""

        try:
            async with self._session() as session:
                response = await session.get(
                    self._request_url("/status"), params=self._query_parameters()
                )
                response_body = await response.json()

                return response_body.get("fingerprint") != {}
        except ClientError:
            return False

    async def tracker_json(
        self,
        conversation_id: Text,
        event_verbosity: EventVerbosity = EventVerbosity.ALL,
        until: Optional[int] = None,
    ) -> Any:
        """Retrieve a tracker's json representation from remote instance."""

        url = f"/conversations/{conversation_id}/tracker"
        params = {INCLUDE_EVENTS_QUERY_PARAM: event_verbosity.name}
        if until:
            params["until"] = until

        async with self._session() as session:
            response = await session.get(
                self._request_url(url), params=self._query_parameters(params)
            )
            return await response.json()

    async def update_events(
        self, conversation_id: Text, events: List[Dict[Text, Any]]
    ) -> Any:
        """Update events in the tracker of a conversation."""

        # don't overwrite existing events but rather restart the conversation
        # and append the updated events.
        events = [Restarted().as_dict()] + events

        return await self.append_events_to_tracker(conversation_id, events)

    async def append_events_to_tracker(
        self,
        conversation_id: Text,
        events: Union[Dict[Text, Any], List[Dict[Text, Any]]],
    ) -> Any:
        """Add some more events to the tracker of a conversation."""

        url = f"/conversations/{conversation_id}/tracker/events"

        async with self._session() as session:
            response = await session.post(
                self._request_url(url), params=self._query_parameters(), json=events
            )
            return await response.json()

    async def execute_action(
        self, conversation_id: Text, action: Dict, event_verbosity: EventVerbosity
    ) -> Any:
        """Run an action in a conversation."""

        url = f"/conversations/{conversation_id}/execute"
        params = {INCLUDE_EVENTS_QUERY_PARAM: event_verbosity.name}

        async with self._session() as session:
            response = await session.post(
                self._request_url(url),
                params=self._query_parameters(params),
                json=action,
            )
            return await response.json()

    async def evaluate_story(self, story: Text) -> Any:
        """Evaluate a story at Core's /evaluate endpoint."""
        url = "/model/test/stories"

        async with self._session() as session:
            response = await session.post(
                self._request_url(url),
                params=self._query_parameters(),
                data=story,
                timeout=300,
            )
            return await response.json()

    async def send_message(self, message: Dict[Text, Text], token: Text) -> Any:
        """Sends user messages to the stack Rasa webhook.

        Returns:
            If the request was successful the result as a list, otherwise
            `None`.
        """

        url = "/webhooks/rasa/webhook"
        message["input_channel"] = self._get_user_properties_from_bearer(token)[0]

        async with self._session() as session:
            response = await session.post(
                self._request_url(url), headers={"Authorization": token}, json=message
            )
            return await response.json()

    @staticmethod
    def _get_user_properties_from_bearer(
        token: Text, public_key: Union[Text, bytes, None] = None
    ) -> Tuple[Text, Optional[Text]]:
        """Given a value of a HTTP Authorization header, verifies its validity
        as a JWT token and returns two values inside its payload: the user's
        name and their role.

        Returns:
            A tuple containing the input channel and the username.
        """

        if not public_key:
            public_key = config.jwt_public_key

        jwt_payload = rasax.community.jwt.verify_bearer_token(
            token, public_key=public_key
        )
        user = jwt_payload.get("user", {})
        username = user.get("username")
        user_roles = user.get("roles", [])

        if GUEST in user_roles:
            input_channel = SHARE_YOUR_BOT_CHANNEL_NAME
        else:
            input_channel = DEFAULT_CHANNEL_NAME

        return input_channel, username

    @staticmethod
    def _get_responses_file_name() -> Text:
        """Load the name of the file with responses."""
        from rasax.community import utils

        return str(
            utils.get_project_directory()
            / config.data_dir
            / config.default_responses_filename
        )

    async def parse(self, text: Text) -> Optional[Dict]:
        url = "/model/parse"
        async with self._session() as session:
            response = await session.post(
                self._request_url(url),
                params=self._query_parameters(),
                headers={"Accept": "application/json"},
                json={"text": text},
            )
            return await response.json()

    async def start_training_process(
        self, team: Text = config.team_name, project_id: Text = config.project_name
    ) -> Any:
        url = "/model/train"

        nlu_training_data = self.data_service.get_nlu_training_data_object(
            should_include_lookup_table_entries=True,
        )

        responses: Optional[Dict] = None

        if self.data_service.training_data_contains_retrieval_intents(
            nlu_training_data
        ):
            try:
                from rasa.utils import io as io_utils

                responses = io_utils.read_yaml(self._get_responses_file_name())
            except ValueError as e:
                rasa_cli_utils.print_error(
                    "Could not complete training request as your training data contains "
                    "retrieval intents of the form 'intent/response_key' but there is no "
                    "responses file found."
                )
                raise ValueError(
                    f"Unable to train on data containing retrieval intents. "
                    f"Details:\n{e}"
                )

        nlu_training_data = nlu_training_data.filter_training_examples(
            lambda ex: ex.get(RESPONSE_KEY_ATTRIBUTE) is None
        )
        md_formatted_data = nlu_training_data.nlu_as_markdown().strip()

        stories = self.story_services.fetch_stories(None)
        combined_stories = self.story_services.get_stories_markdown(stories)

        domain = self.domain_service.get_or_create_domain()
        domain_yaml = dump_obj_as_yaml_to_string(domain)

        _config = self.settings_service.get_config(team, project_id)
        config_yaml = dump_obj_as_yaml_to_string(_config)

        payload = dict(
            domain=domain_yaml,
            config=config_yaml,
            nlu=md_formatted_data,
            stories=combined_stories,
            responses=dump_obj_as_yaml_to_string(responses),
            force=False,
            save_to_default_model_directory=False,
        )

        async with self._session() as session:
            response = await session.post(
                self._request_url(url),
                params=self._query_parameters(),
                json=payload,
                timeout=24 * 60 * 60,  # 24 hours
            )
            return await response.read()

    async def evaluate_intents(
        self, training_data: List[Dict[Text, Any]], model_path: Text
    ) -> Any:
        from rasax.community.services import data_service

        url = "/model/test/intents"
        params = {"model": model_path}
        async with self._session() as session:
            response = await session.post(
                self._request_url(url),
                params=self._query_parameters(params),
                json=data_service.nlu_format(training_data),
            )
            return await response.json()

    async def predict_next_action(
        self, conversation_id: Text, included_events: Text = "ALL"
    ) -> Any:

        url = f"/conversations/{conversation_id}/predict"
        params = {INCLUDE_EVENTS_QUERY_PARAM: included_events}

        async with self._session() as session:
            response = await session.post(
                self._request_url(url), params=self._query_parameters(params)
            )
            return await response.json()


async def collect_version_calls(
    environments: Dict[Text, StackService], timeout_in_seconds: Optional[float] = None
) -> Dict[Text, Union[Dict[Text, Text], Exception]]:
    """Collect the responses of the `/version` endpoint request each every environment.

    Args:
        environments: Mapping of environment names and their `StackService` instances.
        timeout_in_seconds: Timeout to use for the requests.

    Returns:
        Mapping of environment names and the responses of the `/version` request.
        Potential errors will be returned as if they would be regular values.
    """
    version_calls = [
        rasaService.version(timeout_in_seconds=timeout_in_seconds)
        for rasaService in environments.values()
    ]

    responses = await asyncio.gather(*version_calls, return_exceptions=True)
    return dict(zip(environments.keys(), responses))
