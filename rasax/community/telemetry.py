import os
import datetime
import hashlib
import logging
import uuid
import asyncio  # pytype: disable=pyi-error
import urllib.parse
import requests
from functools import wraps
from typing import Any, Dict, Optional, Text, TYPE_CHECKING, Union, List

from sqlalchemy.orm import Session
import aiohttp

from rasa.cli import utils as rasa_cli_utils
from rasa.constants import GLOBAL_USER_CONFIG_PATH
from rasa.utils import common as rasa_utils

from rasax.community.database import utils as db_utils
from rasax.community import config, utils, constants
from rasax.community.services.config_service import (
    ConfigService,
    ConfigKey,
    MissingConfigValue,
    InvalidConfigValue,
)
from rasax.community.constants import (
    CONFIG_TELEMETRY_ENABLED,
    CONFIG_TELEMETRY_ID,
    CONFIG_FILE_TELEMETRY_KEY,
    CONFIG_TELEMETRY_DATE,
    CONFIG_TELEMETRY_WELCOME_SHOWN,
    RASA_PRODUCTION_ENVIRONMENT,
)

from rasax.community.version import __version__

if TYPE_CHECKING:
    from multiprocessing import Queue, Process  # type: ignore

logger = logging.getLogger(__name__)

SEGMENT_ENDPOINT = "https://api.segment.io/v1/track"
USER_GROUPS_ENDPOINT = os.environ.get(
    "USER_GROUPS_ENDPOINT", "https://rasa.com/.netlify/functions/rasa-x-user-groups"
)
TELEMETRY_HTTP_TIMEOUT = 2  # Seconds
ENVIRONMENT_LIVE_TIMEOUT = 2  # Seconds
USER_GROUPS_HTTP_TIMEOUT = 5  # Seconds
TELEMETRY_ID = "metrics_id"
TELEMETRY_ID_LENGTH = 64

# If updating or creating a new event, remember to update
# docs/telemetry/events.json as well!
LOCAL_START_EVENT = "Local X Start"
SERVER_START_EVENT = "Server X Start"
MODEL_TRAINED_EVENT = "Model Trained"
MODEL_UPLOADED_EVENT = "Model Uploaded"
MODEL_PROMOTED_EVENT = "Model Promoted"
MESSAGE_RECEIVED_EVENT = "Message Received"
MESSAGE_ANNOTATED_EVENT = "Message Annotated"
MESSAGE_FLAGGED_EVENT = "Message Flagged"
STORY_CREATED_EVENT = "Story Created"
E2E_TEST_CREATED_EVENT = "End-to-End Test Created"
STATUS_EVENT = "Status"
REPOSITORY_CREATED_EVENT = "Repository Created"
GIT_CHANGES_PUSHED_EVENT = "Git Changes Pushed"
FEATURE_FLAG_UPDATED_EVENT = "Feature Flag Updated"
CONVERSATION_TAGGED = "Conversation Tagged"
CONVERSATIONS_IMPORTED_EVENT = "Conversations Imported"

MESSAGE_ANNOTATED_CONVERSATIONS = "conversations"
MESSAGE_ANNOTATED_NEW_DATA = "annotate_new_data"
MESSAGE_ANNOTATED_INTERACTIVE_LEARNING = "interactive_learning"

FROM_INTERACTIVE = "interactive"
FROM_STORIES = "stories"

# Telemetry events queue, shared between Rasa X processes
_telemetry_queue = None


def ensure_telemetry_enabled(f):
    """Function decorator for telemetry functions that only runs the decorated
    function if telemetry is enabled - that is, if the telemetry events queue
    has been initialized."""

    @wraps(f)
    def decorated(*args, **kwargs):
        try:
            if is_telemetry_enabled():
                return f(*args, **kwargs)
        except Exception as e:
            logger.debug(f"Skipping telemetry collection: {e}")

    return decorated


def is_telemetry_enabled() -> bool:
    """Indicate whether telemetry is enabled or not.

    Returns:
        `True` if telemetry is enabled, `False` otherwise.
    """

    return get_events_queue() is not None


def get_events_queue() -> Optional["Queue"]:
    """Return the global telemetry events queue.

    Returns:
        The `multiprocessing.Queue` for telemetry events.
    """
    return _telemetry_queue


def set_events_queue(queue: Optional["Queue"]) -> None:
    """Establish the global telemetry events queue for the current process.
    To create new processes that inherit this events queue, use the
    `run_in_process` function.

    Args:
        queue: Object to set as the events queue.
    """
    global _telemetry_queue
    _telemetry_queue = queue


def segment_request_header(write_key: Text) -> Dict[Text, Any]:
    """Use a segment write key to create authentication headers for the segment API."""

    return {
        "Authorization": "Basic {}".format(utils.encode_base64(write_key + ":")),
        "Content-Type": "application/json",
    }


def segment_request_payload(
    distinct_id: Text,
    event_name: Text,
    properties: Optional[Dict[Text, Any]] = None,
    context: Optional[Dict[Text, Any]] = None,
) -> Dict[Text, Any]:
    """Compose a valid payload for the segment API."""

    return {
        "userId": distinct_id,
        "event": event_name,
        "properties": properties or {},
        "context": context or {},
    }


def _context_allows_telemetry() -> bool:
    """Check if this Rasa X build is enabled to make requests to external
    services, like Segment or https://rasa.com.

    Returns:
        `True` if external HTTP requests are allowed for this build.
    """
    if not config.telemetry_write_key:
        # If TELEMETRY_WRITE_KEY is empty or `None`, telemetry has not been
        # enabled for this Rasa X build. Telemetry is only enabled for builds
        # that are tagged with a version (e.g. 0.28.0).
        logger.info("Skipping request to external service: telemetry key not set.")
        return False

    if utils.in_continuous_integration():
        # Don't make requests to external services during builds.
        logger.info("Skipping request to external service: running in a CI context.")
        return False

    return True


def _send_event(
    distinct_id: Text,
    event_name: Text,
    properties: Optional[Dict[Text, Any]] = None,
    context: Optional[Dict[Text, Any]] = None,
) -> None:
    """Sends the contents of an event to the /track Segment endpoint.
    Documentation: https://segment.com/docs/sources/server/http/

    Do not call this function from outside telemetry.py! This function does not
    check if telemetry is enabled or not.

    Args:
        distinct_id: Unique telemetry ID.
        event_name: Name of the event.
        properties: Values to send along the event.
        context: Context information about the event.
    """
    if not _context_allows_telemetry():
        return

    headers = segment_request_header(config.telemetry_write_key)
    payload = segment_request_payload(distinct_id, event_name, properties, context)

    resp = requests.post(
        SEGMENT_ENDPOINT, headers=headers, json=payload, timeout=TELEMETRY_HTTP_TIMEOUT
    )
    resp.raise_for_status()

    data = resp.json()
    if not data.get("success"):
        raise Exception(f"Got an unsuccessful response from Segment: {data}")


def _track_internal(
    event_name: Text,
    properties: Optional[Dict[Text, Any]] = None,
    context: Optional[Dict[Text, Any]] = None,
) -> None:
    """Add a telemetry event to the telemetry events queue.

    Do not call this function from outside telemetry.py! This function does not
    check if telemetry is enabled or not.

    Args:
        event_name: Name of the event.
        properties: Values to send along the event.
        context: Context information about the event.
    """

    queue = get_events_queue()

    if not queue:
        logger.warning("Telemetry queue has not been initialized: can't add event")
        return

    # This event will eventually be picked up by the telemetry queue-consuming
    # process. Note that adding this event to the queue is not blocking, since
    # the queue does not have a maximum capacity defined.
    queue.put({"name": event_name, "properties": properties, "context": context})


@ensure_telemetry_enabled
def track(
    event_name: Text,
    properties: Optional[Dict[Text, Any]] = None,
    context: Optional[Dict[Text, Any]] = None,
) -> None:
    """Tracks a telemetry event.

    It is OK to use this function from outside telemetry.py, but note that it
    is recommended to create a new track_xyz() function for complex telemetry
    events, or events that are generated from many parts of the Rasa X code.

    Args:
        event_name: Name of the event.
        properties: Dictionary containing the event's properties.
        context: Dictionary containing some context for this event.
    """

    _track_internal(event_name, properties, context)


@ensure_telemetry_enabled
def track_project_status(session: Optional[Session] = None) -> None:
    """Tracks an event which describes the current state of the project.

    Args:
        session: Optional database session to use. If not provided, create a
            new one with `session_scope`.
    """

    loop = asyncio.new_event_loop()

    try:
        if not session:
            with db_utils.session_scope() as db_session:
                status_event = loop.run_until_complete(
                    _get_project_status_event(db_session)
                )
        else:
            status_event = loop.run_until_complete(_get_project_status_event(session))

        track(STATUS_EVENT, status_event)
    finally:
        loop.close()


async def _get_project_status_event(
    session: Session, project_id: Text = config.project_name
) -> Dict[Text, Any]:
    """Collect data used in `status` event.

    Args:
        session: Database session.
        project_id: The project ID.

    Returns:
        A dictionary containing statistics describing the current project's status.
    """

    from rasax.community.services.event_service import EventService
    from rasax.community.services.domain_service import DomainService
    from rasax.community.services.model_service import ModelService
    from rasax.community.services.data_service import DataService
    from rasax.community.services.story_service import StoryService
    from rasax.community.services.settings_service import SettingsService
    import rasax.community.services.test_service as test_service
    from rasax.community.services import stack_service

    event_service = EventService(session)
    domain_service = DomainService(session)
    model_service = ModelService(config.rasa_model_dir, session)
    data_service = DataService(session)
    story_service = StoryService(session)
    settings_service = SettingsService(session)

    domain = domain_service.get_domain(project_id) or {}
    nlu_data = data_service.get_nlu_training_data_object(project_id=project_id)
    stories = story_service.fetch_stories()

    num_conversations = event_service.get_conversation_metadata_for_all_clients().count
    num_events = event_service.get_events_count()
    num_models = model_service.get_model_count()
    lookup_tables = data_service.get_lookup_tables(project_id, include_filenames=True)
    num_lookup_table_files = len({table["filename"] for table in lookup_tables})
    num_lookup_table_entries = sum(
        table.get("number_of_elements", 0) for table in lookup_tables
    )
    synonyms = data_service.get_entity_synonyms(project_id)
    num_synonyms = sum(len(entry["synonyms"]) for entry in synonyms)
    num_regexes = data_service.get_regex_features(project_id).count

    rasa_services = settings_service.stack_services(project_id)
    version_responses = await stack_service.collect_version_calls(
        rasa_services, timeout_in_seconds=ENVIRONMENT_LIVE_TIMEOUT
    )

    environment_names = _environment_names(rasa_services)

    tags = event_service.get_all_conversation_tags()
    conversations_with_tags = set()
    for tag in tags:
        conversations_with_tags.update(tag["conversations"])

    e2e_tests = test_service.get_tests_from_file()

    return {
        # Use the SHA256 of the project ID in case its value contains
        # information about the user's use of Rasa X. On the analytics side,
        # having the original value or the hash makes no difference. This
        # reasoning is also applied on other values sent in this module.
        "project": hashlib.sha256(project_id.encode("utf-8")).hexdigest(),
        "local_mode": config.LOCAL_MODE,
        "rasa_x": __version__,
        "rasa_open_source": _rasa_version(version_responses),
        "num_intent_examples": len(nlu_data.intent_examples),
        "num_entity_examples": len(nlu_data.entity_examples),
        "num_actions": len(domain.get("actions", [])),
        "num_templates": len(
            domain.get("responses", [])
        ),  # Old nomenclature from when 'responses' were still called 'templates' in the domain
        "num_slots": len(domain.get("slots", [])),
        "num_forms": len(domain.get("forms", [])),
        "num_intents": len(domain.get("intents", [])),
        "num_entities": len(domain.get("entities", [])),
        "num_stories": len(stories),
        "num_conversations": num_conversations,
        "num_events": num_events,
        "num_models": num_models,
        "num_lookup_table_files": num_lookup_table_files,
        "num_lookup_table_entries": num_lookup_table_entries,
        "num_synonyms": num_synonyms,
        "num_regexes": num_regexes,
        "num_environments": len(environment_names),
        "environment_names": environment_names,
        "num_live_environments": _number_of_live_rasa_environments(version_responses),
        "uptime_seconds": utils.get_uptime(),
        "num_tags": len(tags),
        "num_conversations_with_tags": len(conversations_with_tags),
        "num_e2e_tests": len(e2e_tests),
    }


def _rasa_version(
    version_responses: Dict[Text, Union[Dict[Text, Text], Exception]]
) -> Text:
    """Get the version of the Rasa production environment.

    Args:
        version_responses: `/version` responses of every environment.

    Returns:
        The version of the Rasa production environment or `0.0.0` if there was an error.
    """
    from rasax.community.services.stack_service import RASA_VERSION_KEY

    production_response = version_responses.get(RASA_PRODUCTION_ENVIRONMENT)
    if not isinstance(production_response, dict):
        return constants.INVALID_RASA_VERSION

    return production_response.get(RASA_VERSION_KEY) or constants.INVALID_RASA_VERSION


def _number_of_live_rasa_environments(
    version_responses: Dict[Text, Union[Dict[Text, Text], Exception]]
) -> int:
    """Get the number of Rasa environments which are actually running.

    Args:
        version_responses: `/version` responses of every environment.

    Returns:
        Number of live environments.
    """
    return len([r for r in version_responses.values() if not isinstance(r, Exception)])


def _environment_names(rasa_services: Dict[Text, Any]) -> List[Text]:
    """Names of the configured Rasa Open Source environments.

    Args:
        rasa_services: Mapping of Rasa environment names and their `StackService`
            instances.

    Returns:
        Names of configured Rasa environments.
    """
    return [
        (
            name
            if name in [RASA_PRODUCTION_ENVIRONMENT, "worker", "development"]
            else hashlib.sha256(name.encode("utf-8")).hexdigest()
        )
        for name in rasa_services.keys()
    ]


@ensure_telemetry_enabled
def track_story_created(referrer: Optional[Text]) -> None:
    """Tracks an event when a new story is created."""

    if not referrer:
        return

    origin = None
    path = urllib.parse.urlparse(referrer).path

    if path.startswith("/interactive"):
        origin = FROM_INTERACTIVE
    elif path.startswith("/stories"):
        origin = FROM_STORIES

    if origin:
        track(STORY_CREATED_EVENT, {"story_created_from": origin})


@ensure_telemetry_enabled
def track_e2e_test_created(referrer: Optional[Text]) -> None:
    """Tracks an event when a new e2e test is created."""

    if not referrer:
        return

    origin = None
    path = urllib.parse.urlparse(referrer).path

    if path.startswith("/interactive"):
        origin = FROM_INTERACTIVE
    elif path.startswith("/stories"):
        origin = FROM_STORIES

    if origin:
        track(E2E_TEST_CREATED_EVENT, {"e2e_test_created_from": origin})


@ensure_telemetry_enabled
def track_message_annotated(origin: Text) -> None:
    """Tracks an event when a message is annotated."""

    track(MESSAGE_ANNOTATED_EVENT, {"message_annotated_from": origin})


@ensure_telemetry_enabled
def track_message_annotated_from_referrer(referrer: Optional[Text] = None) -> None:
    """Tracks an event when a message is annotated, using a 'Referer' HTTP
    header value to determine the origin of the event."""

    if not referrer:
        return

    path = urllib.parse.urlparse(referrer).path

    if path.startswith("/conversations"):
        origin = MESSAGE_ANNOTATED_CONVERSATIONS
    elif path.startswith("/data"):
        origin = MESSAGE_ANNOTATED_NEW_DATA

    track_message_annotated(origin)


@ensure_telemetry_enabled
def track_message_received(username: Text, channel: Optional[Text]) -> None:
    """Tracks an event when a message is received."""

    track(
        MESSAGE_RECEIVED_EVENT,
        {
            "username": hashlib.sha256(username.encode("utf-8")).hexdigest(),
            "channel": channel or constants.DEFAULT_CHANNEL_NAME,
        },
    )


async def get_user_groups() -> List[Text]:
    """Return the list of all telemetry user groups the current Rasa X
    telemetry ID belongs to. The list is fetched from an external resource using the
    current telemetry ID.

    Returns:
        List containing strings, each one representing a telemetry group the user
        belongs to (for example, "power users").
    """
    telemetry_id = get_telemetry_id()

    # Check first if we're allowed to make the HTTP request to an external service.
    if not all([_context_allows_telemetry(), is_telemetry_enabled(), telemetry_id]):
        # If not, assume there's no groups.
        return []

    user_groups = []
    params = {"user_id": telemetry_id}

    try:
        async with aiohttp.ClientSession(
            read_timeout=USER_GROUPS_HTTP_TIMEOUT
        ) as session:
            async with session.get(USER_GROUPS_ENDPOINT, params=params) as resp:
                resp.raise_for_status()
                user_groups = await resp.json()

                if not isinstance(user_groups, list):
                    raise ValueError(
                        f"User groups data must be a list, got: {user_groups}"
                    )

                if not all([isinstance(group, str) for group in user_groups]):
                    raise ValueError(
                        f"Each user group must be a string, got: {user_groups}"
                    )
    except Exception as e:
        logger.debug(f"Unable to fetch user groups: {e}")

    return user_groups


def get_telemetry_id() -> Optional[Text]:
    """Return the unique telemetry identifier for this Rasa X install.
    The identifier can be any string, but it should be a UUID.

    Returns:
        The identifier, if it is configured correctly.
    """
    telemetry_id = None

    try:
        if config.LOCAL_MODE:
            stored_config = rasa_utils.read_global_config_value(
                CONFIG_FILE_TELEMETRY_KEY
            )

            if isinstance(stored_config, dict):
                telemetry_id = stored_config.get(CONFIG_TELEMETRY_ID)
        else:
            with db_utils.session_scope() as session:
                config_service = ConfigService(session)
                telemetry_id = config_service.get_value(
                    ConfigKey.TELEMETRY_UUID, expected_type=str
                )
    except Exception as e:
        logger.warning(f"Unable to retrieve telemetry ID: {e}")

    return telemetry_id


def _consume_telemetry_events() -> None:
    """Consume events from the telemetry events queue in a loop.
    When an event is received, send it to Segment.
    """
    queue = get_events_queue()
    if not queue:
        logger.warning(
            "Can't consume telemetry events as queue has not been initialized."
        )
        return

    telemetry_id = get_telemetry_id()

    if not telemetry_id:
        logger.warning("Will not send telemetry events as no ID was found.")
        return

    logger.debug("Started consuming telemetry events.")

    while True:
        try:
            event = queue.get()
        except KeyboardInterrupt:
            # Handle Ctrl-C in local mode
            break

        try:
            properties = event["properties"]
            if properties:
                properties[TELEMETRY_ID] = telemetry_id

            _send_event(telemetry_id, event["name"], properties, event["context"])
        except Exception as e:
            logger.warning(
                f"An error occured when trying to send the telemetry event: {e}"
            )


def _initialize_telemetry_process() -> "Process":
    """Initialize the global telemetry event queue, and create the process that consumes
    events from it.

    Returns:
        Created process (already running).
    """
    # Initialize the telemetry events queue. This marks telemetry as being
    # enabled for this process and all processes that inherit this queue after
    # a fork.
    set_events_queue(utils.mp_context().Queue())
    return utils.run_in_process(_consume_telemetry_events)


def _read_telemetry_consent(no_prompt: bool) -> bool:
    """Check if the user wants to enable telemetry or not.

    Args:
        no_prompt: If `True`, do not prompt the user for input (but inform
            about any decision taken).

    Returns:
        Boolean indicating if the user wants to enable telemetry.
    """
    import questionary

    allow_telemetry = (
        questionary.confirm(
            "Rasa will track a minimal amount of anonymized usage information "
            "(like how often the 'train' button is used) to help us improve Rasa X. "
            "None of your training data or conversations will ever be sent to Rasa. "
            "Are you OK with Rasa collecting anonymized usage data?"
        )
        .skip_if(no_prompt, default=True)
        .ask()
    )

    if not no_prompt:
        rasa_cli_utils.print_success(
            f"Your decision has been stored into '{GLOBAL_USER_CONFIG_PATH}'."
        )
    else:
        rasa_cli_utils.print_info(
            "By adding the '--no_prompt' parameter you agreed to allow Rasa to track "
            "and send anonymized usage information."
        )

    return allow_telemetry


def _read_segment_write_key() -> Optional[Text]:
    """Read the Segment write key from the segment key text file.
    The segment key text file should by present only in wheel/sdist packaged
    versions of Rasa X. Therefore, this function should only be used when
    `LOCAL_MODE` is `True`.

    Returns:
        Segment write key, if the key file was present.
    """
    import pkg_resources
    from rasax.community import __name__ as name

    write_key_path = pkg_resources.resource_filename(name, "key")

    try:
        with open(write_key_path) as f:
            return f.read().strip()
    except Exception:
        return None


def initialize_from_file(no_prompt: bool) -> Optional["Process"]:
    """Read telemetry configuration from the user's Rasa config file in
    $HOME. After that is done, start consuming the telemetry events queue
    from a different process, if telemetry is enabled.

    Args:
        no_prompt: If `True`, do not prompt the user for input.
    """
    if not config.LOCAL_MODE:
        logger.error(
            "Attempted to read telemetry configuration file in $HOME while in server "
            "mode. Telemetry will not be enabled."
        )
        return

    if not config.telemetry_write_key:
        # If the Segment write key has not been set via environment variable,
        # try to read it from the key file:
        config.telemetry_write_key = _read_segment_write_key()

    telemetry_enabled = None

    try:
        stored_config = rasa_utils.read_global_config_value(
            CONFIG_FILE_TELEMETRY_KEY, unavailable_ok=False
        )

        telemetry_enabled = stored_config[CONFIG_TELEMETRY_ENABLED]
    except ValueError as e:
        logger.debug(f"Could not read telemetry settings from configuration file: {e}")

    if telemetry_enabled is None:
        telemetry_enabled = _read_telemetry_consent(no_prompt)

        new_config = {
            CONFIG_TELEMETRY_ENABLED: telemetry_enabled,
            CONFIG_TELEMETRY_ID: uuid.uuid4().hex,
            CONFIG_TELEMETRY_DATE: datetime.datetime.now(),
            CONFIG_TELEMETRY_WELCOME_SHOWN: False,
        }

        rasa_utils.write_global_config_value(CONFIG_FILE_TELEMETRY_KEY, new_config)

    return _initialize_telemetry_process() if telemetry_enabled else None


def initialize_from_db(
    session: Session, overwrite_configuration: bool = True
) -> Optional["Process"]:
    """Read telemetry configuration from the database. After that is done,
    start consuming the telemetry events queue from a different process, if
    telemetry is enabled.

    Args:
        session: Database session to use.
        overwrite_configuration: `True` if any modified telemetry config should
            be persisted in the database. E.g. the `EventService` should not override
            configuration made by Rasa X.
    """

    if config.LOCAL_MODE:
        logger.error(
            "Attempted to read telemetry configuration from the database while in "
            "local mode. Telemetry will not be enabled."
        )
        return

    config_service = ConfigService(session)

    telemetry_enabled = _default_enabled_value()

    try:
        telemetry_enabled = config_service.get_value(
            ConfigKey.TELEMETRY_ENABLED, expected_type=bool
        )
    except (MissingConfigValue, InvalidConfigValue):
        logger.debug(
            f"Telemetry was not configured yet. It is now configured with "
            f"the defaults."
        )

    if overwrite_configuration:
        # Always save the value back to the database to make sure we remember
        # potentially set environment variables
        config_service.set_value(ConfigKey.TELEMETRY_ENABLED, telemetry_enabled)

    return _initialize_telemetry_process() if telemetry_enabled else None


def _default_enabled_value() -> bool:
    return not utils.is_enterprise_installed()


@ensure_telemetry_enabled
def track_repository_creation(target_branch: Text, created_via_ui: bool) -> None:
    """Tracks when a new Git repository was connected to Rasa X server.

    Args:
        target_branch: The target branch of this repository.
        created_via_ui: `True` if the repository was created using the Rasa X UI.
    """

    track(
        REPOSITORY_CREATED_EVENT,
        {"branch": target_branch, "created_with_ui": created_via_ui},
    )


@ensure_telemetry_enabled
def track_git_changes_pushed(branch: Text) -> None:
    """Track when changes were pushed to the remote Git repository.

    Args:
        branch: Name of the branch the changes were pushed to.
    """

    track(GIT_CHANGES_PUSHED_EVENT, {"branch": branch})


@ensure_telemetry_enabled
def track_feature_flag(feature_name: Text, enabled: bool) -> None:
    """Track when a feature flag was enabled / disabled.

    Args:
        feature_name: Name of the feature.
        enabled: `True` if the feature was enabled, otherwise `False`.
    """

    track(FEATURE_FLAG_UPDATED_EVENT, {"name": feature_name, "enabled": enabled})


@ensure_telemetry_enabled
def track_conversation_tagged(count: int) -> None:
    """Track an event when a conversation is tagged with one or more tags.

    Args:
        count: Number of tags assigned to the conversation.
    """

    track(CONVERSATION_TAGGED, {"count": count})


@ensure_telemetry_enabled
def track_conversations_imported(process_id: Text) -> None:
    """Track when conversations were imported through a `rasa export` call.

    Args:
        process_id: Import process ID.

    """
    track(CONVERSATIONS_IMPORTED_EVENT, {"process_id": process_id})


@ensure_telemetry_enabled
def track_server_start() -> None:
    """Send the initial telemetry events when the server starts."""
    track(
        SERVER_START_EVENT,
        {"quick_install": _was_deployed_using_quick_install_script()},
    )

    track_project_status()


def _was_deployed_using_quick_install_script() -> bool:
    return os.environ.get("QUICK_INSTALL", "true").lower() == "true"
