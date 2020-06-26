import argparse
import asyncio  # pytype: disable=pyi-error
import logging
import os
import signal
import typing
from typing import Text, Tuple, Dict, Any, List, Union, Optional

import rasax.community.initialise as initialise
from sqlalchemy.orm import Session

import rasax.community.jwt
import rasax.community.utils as rasa_x_utils
from rasa.cli.utils import print_success
from rasa.nlu.training_data import TrainingData
from rasax.community import config, telemetry, sql_migrations, scheduler
from rasax.community.constants import (
    COMMUNITY_PROJECT_NAME,
    COMMUNITY_TEAM_NAME,
    COMMUNITY_USERNAME,
)
from rasax.community.database.utils import session_scope
from rasax.community.api.app import initialize_app
import rasax.community.server as rasa_x_server
from rasax.community.services.domain_service import DomainService
from rasax.community.services.settings_service import (
    SettingsService,
    default_environments_config_local,
)

if typing.TYPE_CHECKING:
    from sanic import Sanic

logger = logging.getLogger(__name__)

LOCAL_DATA_DIR = "data"
LOCAL_DEFAULT_NLU_FILENAME = "nlu.md"
LOCAL_DEFAULT_STORIES_FILENAME = "stories.md"
LOCAL_DOMAIN_PATH = "domain.yml"
LOCAL_MODELS_DIR = "models"
LOCAL_ENDPOINTS_PATH = "endpoints.yml"


def _configure_for_local_server(
    data_path: Text, config_path: Text, token: Optional[Text] = None
) -> None:
    """Create `models` directory and set variables for local mode.

    Sets the API-wide token if provided.
    """

    if not os.path.isdir(LOCAL_MODELS_DIR):
        os.makedirs(LOCAL_MODELS_DIR)

    if token is not None:
        config.rasa_x_token = token

    config.data_dir = data_path
    config.rasa_model_dir = LOCAL_MODELS_DIR
    config.project_name = COMMUNITY_PROJECT_NAME
    config.team_name = COMMUNITY_TEAM_NAME
    config.data_dir = LOCAL_DATA_DIR
    config.default_nlu_filename = LOCAL_DEFAULT_NLU_FILENAME
    config.default_stories_filename = LOCAL_DEFAULT_STORIES_FILENAME
    config.default_username = COMMUNITY_USERNAME
    config.default_domain_path = LOCAL_DOMAIN_PATH
    config.default_config_path = config_path
    config.endpoints_path = LOCAL_ENDPOINTS_PATH


def check_license_and_metrics(args):
    """Ask the user to accept terms and conditions.

    If already accepted, skip it. Also, prompt the user to set the telemetry
    ('metrics') settings.
    """
    config.LOCAL_MODE = True

    if not rasa_x_utils.are_terms_accepted():
        rasa_x_utils.accept_terms_or_quit(args)

    telemetry.initialize_from_file(args.no_prompt)


def _enable_development_mode_and_get_additional_auth_endpoints(
    app: "Sanic",
) -> Union[Tuple[()], Tuple[Text, Any]]:
    """Enable development mode if Rasa Enterprise is installed.

    Configures enterprise endpoints and returns additional authentication
    endpoints if possible.

    Args:
        app: Sanic app to configure.

    Returns:
        Tuple of authentication endpoints if Rasa Enterprise is installed and
        Rasa X is run in development, otherwise an empty tuple.

    """
    if config.development_mode:
        if not rasa_x_utils.is_enterprise_installed():
            raise Exception(
                "Rasa Enterprise is not installed. Using enterprise endpoints in "
                "local development mode requires an installation of "
                "Rasa Enterprise."
            )

        import rasax.enterprise.server as rasa_x_enterprise_server  # pytype: disable=import-error

        rasa_x_enterprise_server.configure_enterprise_endpoints(app)

        return rasa_x_enterprise_server.additional_auth_endpoints()

    return ()


def _event_service(endpoints_path: Text) -> None:
    """Start the event service."""
    # noinspection PyUnresolvedReferences
    from rasax.community.services import event_service

    # set endpoints path variable in this process
    config.endpoints_path = endpoints_path

    def signal_handler(sig, frame):
        print("Stopping event service.")
        os.kill(os.getpid(), signal.SIGTERM)

    signal.signal(signal.SIGINT, signal_handler)

    event_service.main(should_run_liveness_endpoint=False)


def _start_event_service() -> None:
    """Run the event service in a separate process."""

    rasa_x_utils.run_in_process(
        fn=_event_service, args=(config.endpoints_path,), daemon=True
    )


def _initialize_with_local_data(
    project_path: Text,
    data_path: Text,
    session: Session,
    rasa_port: Union[int, Text],
    config_path: Text,
) -> Tuple[Dict[Text, Any], List[Dict[Text, Any]], TrainingData]:

    settings_service = SettingsService(session)
    default_env = default_environments_config_local(rasa_port)
    settings_service.save_environments_config(
        COMMUNITY_PROJECT_NAME, default_env.get("environments")
    )

    loop = asyncio.get_event_loop()
    # inject data
    domain, story_blocks, nlu_data = loop.run_until_complete(
        rasax.community.initialise.inject_files_from_disk(
            project_path, data_path, session, config_path=config_path
        )
    )

    # dump domain once
    domain_service = DomainService(session)
    domain_service.dump_domain()
    return domain, story_blocks, nlu_data


def main(
    args: argparse.Namespace,
    project_path: Text,
    data_path: Text,
    token: Optional[Text] = None,
    config_path: Optional[Text] = config.default_config_path,
) -> None:
    """Start Rasa X in local mode."""
    config.LOCAL_MODE = True
    print_success("Starting Rasa X in local mode... 🚀")

    config.self_port = args.rasa_x_port

    _configure_for_local_server(data_path, config_path, token)

    rasax.community.jwt.initialise_jwt_keys()

    app = rasa_x_server.configure_app()

    with session_scope() as session:
        auth_endpoints = _enable_development_mode_and_get_additional_auth_endpoints(app)
        initialize_app(app, class_views=auth_endpoints)

        sql_migrations.run_migrations(session)

        initialise.create_community_user(session, app)

        _initialize_with_local_data(
            project_path, data_path, session, args.port, config_path
        )

        telemetry.track(telemetry.LOCAL_START_EVENT)
        telemetry.track_project_status(session)

    # this needs to run after initial database structures are created
    # otherwise projects assigned to events won't be present
    _start_event_service()

    scheduler.start_background_scheduler()

    app.run(
        host="0.0.0.0",
        port=config.self_port,
        auto_reload=os.environ.get("SANIC_AUTO_RELOAD"),
        access_log=False,
    )
