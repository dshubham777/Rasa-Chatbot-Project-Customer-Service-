import json
from typing import Any, Text, Dict, Union

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from rasax.community.database import utils
from rasax.community.database.base import Base

# Table for many-to-many relationship between users and roles.
user_role_mapping = sa.Table(
    "user_role_mapping",
    Base.metadata,
    sa.Column("username", sa.String, sa.ForeignKey("rasa_x_user.username")),
    sa.Column("role", sa.String, sa.ForeignKey("user_role.role")),
)


class User(Base):
    """Stores the Rasa X users and their credentials."""

    __tablename__ = "rasa_x_user"

    username = sa.Column(sa.String, primary_key=True)
    name_id = sa.Column(sa.String, nullable=True)
    password_hash = sa.Column(sa.Text, nullable=True)
    team = sa.Column(sa.String, nullable=False)
    api_token = sa.Column(sa.String, nullable=False)
    project = sa.Column(sa.String, sa.ForeignKey("project.project_id"))
    data = sa.Column(sa.Text)

    # Store whether the username has been deliberately assigned.
    # This property if False for SAML users who have not yet assigned
    # their own username.
    # as_dict() will return {"username": None} if username_is_assigned == False
    username_is_assigned = sa.Column(sa.Boolean, default=True)

    # one-to-one relationship between single-use token and user
    single_use_token = relationship(
        "SingleUseToken", uselist=False, back_populates="user"
    )

    roles = relationship("Role", secondary=user_role_mapping, backref="users")

    authentication_mechanism = sa.Column(sa.String)

    def __repr__(self):
        roles = [role.role for role in self.roles]

        return f"User {self.username} (roles={roles}, team={self.team})"

    def as_dict(self, return_api_token=False) -> Dict[Text, Any]:
        from rasax.community.services.role_service import RoleService

        role_service = RoleService()

        roles = [role.role for role in self.roles]
        user_dict = {
            "username": self.username if self.username_is_assigned else None,
            "roles": roles,
            "projects": [{"name": self.project, "roles": roles}],
            "team": self.team,
            "authentication_mechanism": self.authentication_mechanism,
            "data": json.loads(self.data) if self.data else None,
        }

        if self.name_id:
            user_dict["name_id"] = self.name_id

        if self.single_use_token:
            user_dict["single_use_token"] = self.single_use_token.token
            user_dict["single_use_token_expires"] = self.single_use_token.expires

        if return_api_token:
            user_dict["api_token"] = self.api_token

        user_dict["permissions"] = sorted(
            role_service.get_stripped_role_permissions(self.roles)
        )

        return user_dict


class Project(Base):
    """Stores different bot projects."""

    __tablename__ = "project"

    project_id = sa.Column(sa.String, primary_key=True)
    team = sa.Column(sa.String, nullable=False)
    active_model = sa.Column(sa.String)
    config = sa.Column(sa.Text)
    handoff_url = sa.Column(sa.String)

    models = relationship(
        "Model", cascade="all, delete-orphan", back_populates="project"
    )

    def as_dict(self) -> Dict[Text, Union[Text, Dict]]:
        return {
            "project_id": self.project_id,
            "name": self.project_id,
            "config": json.loads(self.config) if self.config else {},
            "team": self.team,
        }

    def get_model_config(self) -> Dict[Text, Union[Text, Dict]]:
        """Return model config.

        Removes `path` key from model config, as it is unnecessary for
        for the yaml dump.
        """

        _config = json.loads(self.config)
        if "path" in _config:
            del _config["path"]

        return _config


class Environment(Base):
    """Stores the different bot environments which are available.

    E.g. a production environment and a development environment to test new
    version of the bot.
    """

    __tablename__ = "environment"

    name = sa.Column(sa.String, primary_key=True)
    project = sa.Column(
        sa.String, sa.ForeignKey("project.project_id"), primary_key=True
    )
    url = sa.Column(sa.String, nullable=False)
    token = sa.Column(sa.String)

    def as_dict(self) -> Dict[Text, Any]:
        environment_dict = {self.name: {"url": self.url, "token": self.token}}

        return environment_dict


class PlatformFeature(Base):
    """Stores whether certain feature flags are activated."""

    __tablename__ = "platform_feature"

    feature_name = sa.Column(sa.String, primary_key=True)
    enabled = sa.Column(sa.Boolean, default=False)

    def as_dict(self) -> Dict[Text, Union[Text, bool]]:
        return {"name": self.feature_name, "enabled": self.enabled}


class Role(Base):
    """Stores the different roles which Platform users can have."""

    __tablename__ = "user_role"

    role = sa.Column(sa.String, primary_key=True)
    permissions = relationship(
        "Permission", cascade="all, delete-orphan", back_populates="role"
    )
    description = sa.Column(sa.String, nullable=True)
    is_default = sa.Column(sa.Boolean, default=False)

    def as_dict(self):
        return {
            "role": self.role,
            "description": self.description,
            "is_default": self.is_default,
        }


class Permission(Base):
    """Stores the permissions for each role."""

    __tablename__ = "permission"

    project = sa.Column(
        sa.String, sa.ForeignKey("project.project_id"), primary_key=True
    )
    role_id = sa.Column(sa.String, sa.ForeignKey("user_role.role"), primary_key=True)
    permission = sa.Column(sa.String, primary_key=True)

    role = relationship("Role", back_populates="permissions")


class ChatToken(Base):
    """Stores chat_token used to share bots."""

    __tablename__ = "chat_tokens"

    token = sa.Column(sa.String, primary_key=True)
    bot_name = sa.Column(sa.String)
    description = sa.Column(sa.String)
    expires = sa.Column(sa.Integer)

    def as_dict(self):
        return {
            "chat_token": self.token,
            "bot_name": self.bot_name,
            "description": self.description,
            "expires": self.expires,
        }


class SingleUseToken(Base):
    """Stores single-use token used for enterprise single sign-on."""

    __tablename__ = "single_use_token"

    token = sa.Column(sa.String, primary_key=True)
    expires = sa.Column(sa.Float)
    username = sa.Column(sa.String, sa.ForeignKey("rasa_x_user.username"))
    user = relationship("User", back_populates="single_use_token")


class LocalPassword(Base):
    """Stores the Rasa X password for use in local mode."""

    __tablename__ = "password"

    password = sa.Column(sa.String, primary_key=True)


class GitRepository(Base):
    """Stores credentials for connected git repositories."""

    __tablename__ = "git_repository"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    name = sa.Column(sa.String)
    repository_url = sa.Column(sa.Text)
    ssh_key = sa.Column(sa.Text)
    git_service = sa.Column(sa.String)
    git_service_access_token = sa.Column(sa.Text)
    target_branch = sa.Column(sa.String)
    is_target_branch_protected = sa.Column(sa.Boolean)
    first_annotator_id = sa.Column(sa.String, sa.ForeignKey("rasa_x_user.username"))
    first_annotated_at = sa.Column(sa.Float)  # annotation time as unix timestamp

    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))

    def as_dict(self) -> Dict[Text, Union[Text, int]]:
        return {
            "id": self.id,
            "name": self.name,
            "repository_url": self.repository_url,
            "git_service": self.git_service,
            "target_branch": self.target_branch,
            "is_target_branch_protected": self.is_target_branch_protected,
        }


class ConfigValue(Base):
    """Stores configuration values for the Rasa X server."""

    __tablename__ = "configuration"

    key = sa.Column(sa.String, primary_key=True, index=True)
    value = sa.Column(sa.Text, nullable=False)
