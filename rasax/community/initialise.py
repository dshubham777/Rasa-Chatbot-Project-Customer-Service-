import logging
import os
from pathlib import Path

import sanic
import sqlalchemy.orm as orm
import typing
import rasax.community.constants as constants
import rasa.cli.utils as rasa_cli_utils

from typing import Text, Tuple, Generator, Dict, List, Set, Any, Optional, Union

from rasa.core.domain import InvalidDomain
from rasa.nlu.training_data import TrainingData
from rasa.utils.io import read_yaml_file, read_file
from rasax.community import utils
from rasa.utils import common as rasa_utils
from rasax.community import config
from rasax.community.services.user_service import UserService
from rasax.community.utils import random_password, run_operation_in_single_sanic_worker
from rasax.community.services.domain_service import DomainService
from rasax.community.services.story_service import StoryService

if typing.TYPE_CHECKING:
    from rasax.community.services.settings_service import SettingsService
    from rasa.nlu.training_data import TrainingData as NluTrainingData
    from rasax.community.services.role_service import RoleService
    from rasax.community.services.data_service import DataService

logger = logging.getLogger(__name__)


def inject_config(
    config_path: Text, settings_service: "SettingsService"
) -> Optional[Dict[Text, Any]]:
    """Load a configuration file from `path` and save it to the database.

    Quits the application if config cannot be loaded.
    """

    if not os.path.exists(config_path):
        rasa_cli_utils.print_error_and_exit(
            f"Failed to inject Rasa configuration. The file "
            f"'{os.path.abspath(config_path)}' does not exist."
        )

    _config = read_yaml_file(config_path)
    if not _config:
        rasa_cli_utils.print_error_and_exit(
            f"Failed to inject Rasa configuration:\n"
            f"Reading of yaml '{os.path.abspath(config_path)}' file failed. Most "
            f"likely the file was not found or uses the wrong syntax."
        )

    settings_service.save_config(
        config.team_name, "default", _config, config_path, should_dump=False
    )

    logger.debug(
        "Loaded local configuration from '{}' into database".format(
            os.path.abspath(config_path)
        )
    )
    return _config


def _read_data(paths: List[Text]) -> Generator[Tuple[Text, Text], None, None]:
    for filename in paths:
        try:
            yield read_file(filename), filename
        except ValueError:
            rasa_cli_utils.print_warning(f"Cannot read file {filename}")


def inject_nlu_data(
    nlu_files: Set[Text], project_id: Text, username: Text, data_service: "DataService"
) -> TrainingData:
    """Load Rasa NLU training data from `path` and save it to the database."""

    # delete existing data in db if files are provided
    if nlu_files:
        data_service.delete_data()

    training_data = data_service.save_bulk_data_from_files(
        nlu_files, project_id, username
    )

    logger.debug(
        f"Injected {len(training_data.training_examples)} NLU training data examples."
    )

    return training_data


async def inject_stories(
    story_files: Set[Text],
    story_service: "StoryService",
    username: Text,
    team: Text = config.team_name,
) -> List[Dict[Text, Any]]:
    """Load Core stories from `data_directory` and save to the database."""

    story_blocks = []

    if story_files:
        # delete existing data in db if files are provided
        await story_service.delete_stories(team)
        # store provided stories in db
        story_blocks = await story_service.save_stories_from_files(
            story_files, team, username
        )

    logger.debug(f"Injected {len(story_blocks)} Core stories.")
    return story_blocks


def inject_domain(
    domain_path: Text,
    domain_service: "DomainService",
    project_id: Text = config.project_name,
    username: Text = config.default_username,
) -> Dict[Text, Any]:
    """Load Rasa Core domain at `path` and save it to database.

    Quits the application if domain cannot be loaded.
    """

    if not os.path.exists(domain_path):
        rasa_cli_utils.print_error_and_exit(
            f"domain.yml could not be found at '{os.path.abspath(domain_path)}'. "
            f"Rasa X requires a domain in the project root directory."
        )

    try:
        domain_service.validate_and_store_domain_yaml(
            domain_yaml=read_file(domain_path),
            project_id=project_id,
            path=domain_path,
            store_responses=True,
            username=username,
            should_dump_domain=False,
        )

    except InvalidDomain as e:
        rasa_cli_utils.print_error_and_exit(f"Could not inject domain. Details:\n{e}")

    return domain_service.get_or_create_domain(project_id)


def create_project_and_settings(
    _settings_service: "SettingsService", _role_service: "RoleService", team: Text
) -> None:
    """Create project and settings."""

    project_id = config.project_name

    existing = _settings_service.get(team, project_id)

    if existing is None:
        _settings_service.init_project(team, project_id)

    _role_service.init_roles(project_id=project_id)


def create_community_user(
    session: orm.Session, app: Optional[sanic.Sanic] = None
) -> None:
    from rasax.community.services.role_service import RoleService
    from rasax.community.services.settings_service import SettingsService

    role_service = RoleService(session)
    role_service.init_roles(project_id=constants.COMMUNITY_PROJECT_NAME)

    settings_service = SettingsService(session)
    password = settings_service.get_community_user_password()
    is_password_generated = False

    # only re-assign password in local mode or if it doesn't exist
    if config.LOCAL_MODE or not password:
        password = os.environ.get(constants.ENV_RASA_X_PASSWORD)
        if not password:
            password = random_password()
            is_password_generated = True
        settings_service.save_community_user_password(password)

    user_service = UserService(session)
    user_service.insert_or_update_user(
        constants.COMMUNITY_USERNAME, password, constants.COMMUNITY_TEAM_NAME
    )

    if app:
        run_operation_in_single_sanic_worker(
            app, AppStartedCallable(password, is_password_generated)
        )


class AppStartedCallable:
    """A class that represents a callable that is called after the start of the
    application."""

    def __init__(self, password: Text, is_password_generated: bool) -> None:
        """The constructor for the AppStartedCallable

        Args:
            password: The password for the initial user.
            is_password_generated: Specifies if this password was randomly generated.
        """
        self.password = password
        self.is_password_generated = is_password_generated

    @staticmethod
    def open_web_browser(login_url: Text) -> None:
        """Opens a new tab on the user's preferred web browser and points it to `login_url`.
        Depending on the telemetry configuration, a separate tab may be opened as well,
        showing the user a welcome page.

        Args:
            login_url: URL which the tab should be pointed at.
        """

        import webbrowser

        telemetry_config = rasa_utils.read_global_config_value(
            constants.CONFIG_FILE_TELEMETRY_KEY
        )

        if telemetry_config and telemetry_config[constants.CONFIG_TELEMETRY_ENABLED]:
            # If the telemetry config does not contain CONFIG_TELEMETRY_WELCOME_SHOWN,
            # then the user has upgraded from a previous version of Rasa X (before
            # this config value was introduced). In these cases, assume that the
            # user has already seen the welcome page.
            if not telemetry_config.get(constants.CONFIG_TELEMETRY_WELCOME_SHOWN, True):
                webbrowser.open_new_tab(constants.WELCOME_PAGE_URL)

            telemetry_config[constants.CONFIG_TELEMETRY_WELCOME_SHOWN] = True
            rasa_utils.write_global_config_value(
                constants.CONFIG_FILE_TELEMETRY_KEY, telemetry_config
            )

        webbrowser.open_new_tab(login_url)

    def __call__(self) -> None:
        """Execute a set of actions that should be executed after the successful application start.
        In local mode, this callable prints a login url to console and opens a browser window.
        Otherwise, it checks if the password was generated, and then just prints this password if it was.
        """
        username = constants.COMMUNITY_USERNAME

        if not config.LOCAL_MODE:
            if self.is_password_generated:
                rasa_cli_utils.print_success(
                    f"Your login password is '{self.password}'."
                )
        else:
            server_url = f"http://localhost:{config.self_port}"
            login_url = (
                f"{server_url}/login?username={username}&password={self.password}"
            )

            rasa_cli_utils.print_success(f"\nThe server is running at {login_url}\n")

            if config.OPEN_WEB_BROWSER:
                AppStartedCallable.open_web_browser(login_url)


async def inject_files_from_disk(
    project_path: Union[Path, Text],
    data_path: Text,
    session: orm.Session,
    username: Optional[Text] = constants.COMMUNITY_USERNAME,
    config_path: Optional[Text] = config.default_config_path,
) -> Tuple[Dict[Text, Any], List[Dict[Text, Any]], "NluTrainingData"]:
    """Injects local files into database.

    Args:
        project_path: Path to the project of which the data should be injected.
        data_path: Path to the data within this project.
        session: Database session.
        username: The username which is used to inject the data.
        config_path: Path to the config file within the project

    Returns:
        Tuple of domain, stories, and NLU training data.
    """
    import rasa.data
    from rasax.community.local import LOCAL_DOMAIN_PATH
    from rasax.community.services.data_service import DataService
    from rasax.community.services.settings_service import SettingsService

    utils.set_project_directory(project_path)

    domain_service = DomainService(session)
    domain = inject_domain(
        os.path.join(project_path, LOCAL_DOMAIN_PATH),
        domain_service,
        constants.COMMUNITY_PROJECT_NAME,
        username,
    )

    settings_service = SettingsService(session)
    inject_config(os.path.join(project_path, config_path), settings_service)

    story_files, nlu_files = rasa.data.get_core_nlu_files([data_path])

    story_service = StoryService(session)
    story_blocks = await inject_stories(
        story_files, story_service, username, constants.COMMUNITY_TEAM_NAME
    )

    data_service = DataService(session)
    nlu_data = inject_nlu_data(
        nlu_files, constants.COMMUNITY_PROJECT_NAME, username, data_service
    )

    return domain, story_blocks, nlu_data
