import logging
import os

from rasax.community.services.integrated_version_control.git_service import GitService
from sqlalchemy.orm import Session

import rasa.cli.utils
from rasax.community import initialise  # pytype: disable=import-error
import rasax.community.jwt
import rasax.community.sql_migrations as sql_migrations
import rasax.community.utils as rasa_x_utils
from rasax.community import config, telemetry, scheduler
from rasax.community.api.app import configure_app, initialize_app
from rasax.community.database.utils import session_scope
from rasax.community.services.settings_service import SettingsService
from rasax.community.services.config_service import ConfigService

logger = logging.getLogger(__name__)


def main():
    rasa_x_utils.update_log_level()
    logger.debug("Starting API service.")

    app = configure_app(local_mode=False)
    rasax.community.jwt.initialise_jwt_keys()
    initialize_app(app)
    with session_scope() as session:
        sql_migrations.run_migrations(session)
        initialise.create_community_user(session, app)

        initialise_server_mode(session)

    launch_event_service()

    telemetry.track_server_start()

    rasa.cli.utils.print_success("Starting Rasa X server... 🚀")
    app.run(
        host="0.0.0.0",
        port=config.self_port,
        auto_reload=os.environ.get("SANIC_AUTO_RELOAD"),
        workers=4,
    )


def initialise_server_mode(session: Session) -> None:
    """Initialise common configuration for the server mode.

    Args:
        session: An established database session.
    """
    # Initialize environments before they are used in the model discovery process
    settings_service = SettingsService(session)
    settings_service.inject_environments_config_from_file(
        config.project_name, config.default_environments_config_path
    )

    # Initialize database with default configuration values so that they
    # can be read later.
    config_service = ConfigService(session)
    config_service.initialize_configuration()

    # Initialize telemetry
    telemetry.initialize_from_db(session)

    # Start background scheduler in separate process
    scheduler.start_background_scheduler()


def _event_service() -> None:
    from rasax.community.services.event_service import main as event_service_main

    event_service_main(should_run_liveness_endpoint=False)


def launch_event_service() -> None:
    """Start the event service in a multiprocessing.Process if
    `EVENT_CONSUMER_SEPARATION_ENV` is `True`, otherwise do nothing."""

    from rasax.community.constants import EVENT_CONSUMER_SEPARATION_ENV

    if config.should_run_event_consumer_separately:
        logger.debug(
            f"Environment variable '{EVENT_CONSUMER_SEPARATION_ENV}' "
            f"set to 'True', meaning Rasa X expects the event consumer "
            f"to run as a separate service."
        )
    else:
        logger.debug("Starting event service from Rasa X.")

        rasa_x_utils.run_in_process(fn=_event_service, daemon=True)


if __name__ == "__main__":
    main()
