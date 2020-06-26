import logging
import os
from typing import Text

import pkg_resources
from alembic import command
from alembic.config import Config
from sqlalchemy.orm import Session

import rasax.community.config as rasa_x_config
from rasax.community.database.admin import Project
from rasax.community.services.domain_service import DomainService
from rasax.community.services.role_service import RoleService, DEFAULT_ROLES
from rasax.community.services.settings_service import SettingsService
from rasax.community.services.user_service import UserService, ADMIN

logger = logging.getLogger(__name__)

ALEMBIC_PACKAGE = pkg_resources.resource_filename(
    __name__, "database/schema_migrations"
)


def run_migrations(session: Session) -> None:
    _run_schema_migrations(session)

    _create_initial_project(session)
    _create_default_roles(session)
    _create_default_permissions(session)
    _create_system_user(session)
    _generate_chat_token(session)


def _run_schema_migrations(session: Session) -> None:
    logger.debug("Start running schema migrations.")

    # Configure alembic paths and database connection
    alembic_config = _get_alembic_config(session)

    # Run migrations
    _run_alembic_migration(alembic_config)

    logger.debug("Schema migrations finished.")


def _get_alembic_config(session: Session) -> Config:
    alembic_config_file = os.path.join(ALEMBIC_PACKAGE, "alembic.ini")
    alembic_config = Config(alembic_config_file)
    alembic_config.set_main_option(
        "script_location", os.path.join(ALEMBIC_PACKAGE, "alembic")
    )

    connection_url = str(session.bind.url)
    # To avoid interpolation of `%` we have to escape them by duplicating them
    connection_url = connection_url.replace("%", "%%")
    alembic_config.set_main_option("sqlalchemy.url", connection_url)
    alembic_config.engine = session.bind

    return alembic_config


def _run_alembic_migration(
    alembic_config: Config, target_revision: Text = "head"
) -> None:
    command.upgrade(alembic_config, target_revision)


def _create_initial_project(session) -> None:
    if not session.query(Project).first():
        settings_service = SettingsService(session)
        settings_service.init_project(
            rasa_x_config.team_name, rasa_x_config.project_name
        )

        session.commit()
        logger.debug(
            f"No projects present. Created initial default project '{rasa_x_config.project_name}'."
        )


def _create_default_roles(session) -> None:
    role_service = RoleService(session)
    existing_roles = role_service.roles

    for role in [*DEFAULT_ROLES]:
        if role not in existing_roles:
            role_service.save_role(role)
            logger.debug(f"Created role '{role}'.")
    session.commit()


def _create_default_permissions(session) -> None:
    role_service = RoleService(session)

    default_roles = role_service.default_roles.items()
    for role, permissions in default_roles:
        if not role_service.get_role_permissions(role):
            role_service.save_permissions_for_role(role, permissions)
            logger.debug(f"Created default permissions for '{role}' role.")
    session.commit()


def _generate_chat_token(session) -> None:
    domain_service = DomainService(session)
    existing_token = domain_service.get_token()
    if not existing_token:
        generated_token = domain_service.generate_and_save_token()
        logger.debug(
            "Generated chat token '{}' with expiry date {}"
            "".format(generated_token.token, generated_token.expires)
        )


def _create_system_user(session: Session) -> None:
    user_service = UserService(session)
    if user_service.fetch_user(rasa_x_config.SYSTEM_USER):
        logger.debug(
            f"Found existing system system user '{rasa_x_config.SYSTEM_USER}'."
        )
        return

    user_service.create_user(
        rasa_x_config.SYSTEM_USER, None, rasa_x_config.team_name, ADMIN
    )
    logger.debug(f"Created new system user '{rasa_x_config.SYSTEM_USER}'.")
