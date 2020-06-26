import hashlib
import json
import logging
import random

import time
from sanic_jwt import exceptions
from sqlalchemy import and_
from typing import Optional, Text, Dict, List, Any, Union

from rasax.community import config
from rasax.community.constants import COMMUNITY_USERNAME
from rasax.community.database.admin import User, Role, SingleUseToken
from rasax.community.database.service import DbService

logger = logging.getLogger(__name__)

ADMIN = "admin"
ANNOTATOR = "annotator"
TESTER = "tester"
GUEST = "guest"


class AuthMechanisms:
    username_password = "username/password"
    saml = "saml"


class MismatchedPasswordsException(Exception):
    """Exception raised for errors related to mismatched passwords."""

    def __init__(self):
        self.message = "Passwords do not match!"

    def __str__(self):
        return self.message


class UserException(Exception):
    """Exception raised for errors related to operations involving `User` objects."""

    def __init__(self, username):
        self.message = username

    def __str__(self):
        return self.message


class RoleException(Exception):
    """Exception raised for errors related to operations involving `Role` objects."""

    def __init__(self, username):
        self.message = username

    def __str__(self):
        return self.message


class UserService(DbService):
    def fetch_user(
        self, username: Text, return_api_token: bool = False
    ) -> Optional[Dict]:
        user = self._fetch_user(username)
        if user:
            return user.as_dict(return_api_token=return_api_token)
        else:
            return None

    def _fetch_user(self, username: Text) -> User:
        return self.query(User).filter(User.username == username).first()

    def fetch_all_users(
        self,
        team: Text,
        username_query: Optional[Text] = None,
        role_query: Optional[Text] = None,
        exclude_system_user: bool = True,
    ) -> List[Dict]:
        """Fetch all users.

        Args:
            team: Users' team
            username_query: Username query
            role_query: comma-separated role query
            exclude_system_user: whether to exclude the system user

        Returns:
            List of users as dict.
        """

        if role_query:
            roles = role_query.split(",")
            query = User.roles.any(Role.role.in_(roles))
        else:
            query = True

        if exclude_system_user:
            query = and_(query, User.username != config.SYSTEM_USER)

        if username_query:
            query = and_(query, User.username.ilike(f"%{username_query}%"))

        users = self.query(User).filter(User.team == team).filter(query).all()

        return [u.as_dict() for u in users]

    def get_number_of_users(self) -> int:
        """Return the number of users."""
        return self.query(User).filter(User.username != config.SYSTEM_USER).count()

    def create_user(
        self,
        username: Text,
        raw_password: Optional[Text],
        team: Text,
        roles: Union[List[Text], Text, None],
        auth_mechanism: Text = AuthMechanisms.username_password,
    ):
        from rasax.community.constants import COMMUNITY_USERNAME
        import rasax.community.utils as rasa_x_utils

        # do not allow user creation if CE is installed,
        # unless it's the community or system user
        if not rasa_x_utils.is_enterprise_installed() and username not in (
            COMMUNITY_USERNAME,
            config.SYSTEM_USER,
        ):
            logger.error(
                "Rasa X does not support multiple users. "
                "If you'd like to create more users, please contact us at hi@rasa.com "
                "for a Rasa Enterprise license."
            )
            return

        existing_user = self._fetch_user(username)
        if existing_user:
            raise UserException(username)

        api_token = self.generate_api_token()

        if raw_password is not None:
            password_hash = self.hash_pw(raw_password)
        else:
            password_hash = None

        new_user = User(
            username=username,
            project="default",
            team=team,
            password_hash=password_hash,
            api_token=api_token,
            authentication_mechanism=auth_mechanism,
        )

        self.add(new_user)

        if isinstance(roles, str):
            roles = [roles]

        for role in roles:
            self.add_role_to_user(username, role)

    def insert_or_update_user(
        self, username: Text, password: Text, team: Text, role: Text = ADMIN
    ):
        """Inserts a user or updates their password if the user already exists."""

        if self.fetch_user(username):
            logger.debug(f"Found user: '{username}'.")
            self.admin_change_password(username, password)
            logger.debug(f"Updated password for user '{username}' to '{password}'.")
        else:
            self.create_user(username, password, team, role)
            logger.debug(
                "Created local user named {} "
                "with password {}".format(username, password)
            )

    def delete_user(self, username: Text, requesting_user: Optional[Text] = None):
        if requesting_user and username == requesting_user:
            raise UserException(f"Requesting user '{username}' cannot delete itself.")

        existing_user = self._fetch_user(username)
        if not existing_user:
            raise UserException(username)

        if username == config.SYSTEM_USER:
            raise UserException(f"Cannot delete user '{username}'.")

        deleted_user = existing_user.as_dict()
        self.delete(existing_user)

        return deleted_user

    def _add_role_to_user(self, existing_user: User, role: Text) -> None:
        """Gives `existing_user` an additional role."""

        _role = self.query(Role).filter(Role.role == role).first()

        if not _role:
            raise RoleException(f"Role '{role}' does not exist.")

        if not any([r.role == _role.role for r in existing_user.roles]):
            existing_user.roles.append(_role)
        else:
            logger.debug(
                f"User '{existing_user.username or existing_user.name_id}' already "
                f"had role '{_role.role}'."
            )

    def add_role_to_user(self, username: Text, role: Text) -> None:
        """Gives the user an additional role."""

        existing_user = self._fetch_user(username)

        if not existing_user:
            raise UserException(f"User '{username}' does not exist.")

        self._add_role_to_user(existing_user, role)

    def add_role_to_saml_user(self, name_id: Text, role: Text) -> None:
        """Gives the user an additional role."""

        existing_user = self._fetch_saml_user(name_id)

        if not existing_user:
            raise UserException(f"SAML user with name ID '{name_id}' does not exist.")

        self._add_role_to_user(existing_user, role)

    @staticmethod
    def _inspect_admin_role_update(
        existing_user: User, requesting_user: Optional[Text], username: Text
    ) -> None:
        """Inspect role update operation on `existing_user` by `requesting_user`.

        Raises `RoleException` if `requesting_user` currently holds `admin` role
        and tries to remove that role.
        """

        if (
            requesting_user
            and username == requesting_user
            and has_role(existing_user.as_dict(), ADMIN)
        ):
            raise RoleException(
                "User '{}' currently holds 'admin' role and "
                "cannot remove that role.".format(username)
            )

    def replace_user_roles(
        self,
        username: Text,
        roles: Union[List[Text], Text, None],
        requesting_user: Optional[Text] = None,
    ) -> None:
        """Removes all roles from a user and replaces them with new ones."""

        existing_user = self._fetch_user(username)

        if not isinstance(roles, list):
            roles = [roles]

        if ADMIN not in roles:
            self._inspect_admin_role_update(existing_user, requesting_user, username)

        for _role in existing_user.roles:
            existing_user.roles.remove(_role)

        if roles and roles[0]:
            for role in roles:
                self.add_role_to_user(username, role)

    def delete_user_role(
        self, username: Text, role: Text, requesting_user: Optional[Text] = None
    ) -> None:
        """Deletes a role from a user."""

        existing_user = self._fetch_user(username)
        if not existing_user:
            raise UserException(username)

        if role == ADMIN:
            self._inspect_admin_role_update(existing_user, requesting_user, username)

        roles_to_delete = [r for r in existing_user.roles if r.role == role]

        if not roles_to_delete:
            raise RoleException(role)

        for role in roles_to_delete:
            existing_user.roles.remove(role)

    def create_saml_user(
        self,
        name_id: Text,
        team: Text,
        role: Text,
        username: Optional[Text] = None,
        api_token: Optional[Text] = None,
    ) -> None:
        api_token = api_token or self.generate_api_token()

        existing_name_id = self._fetch_user(name_id)
        if existing_name_id:
            raise UserException(f"Username for SAML ID '{name_id}' already exists.")

        new_user = User(
            username=username or name_id,
            name_id=name_id,
            project="default",
            team=team,
            api_token=api_token,
            authentication_mechanism=AuthMechanisms.saml,
            username_is_assigned=username is not None,
        )
        self.add(new_user)

        if username:
            self.add_role_to_user(username=username, role=role)
        else:
            self.add_role_to_saml_user(name_id=name_id, role=role)

    def update_saml_username(
        self, name_id: Text, username: Text
    ) -> Optional[Dict[str, Any]]:
        """Creates a new user for `username`.

        Deletes the old user associated with `name_id`.
        """

        existing_user = self._fetch_saml_user(name_id)

        if not existing_user:
            raise UserException(f"User with SAML ID '{name_id}' not found")

        if self._fetch_user(username):
            raise UserException(f"Username '{username}' already exists")

        # delete old user
        _ = self.delete_user(existing_user.username)

        self.create_saml_user(
            name_id=name_id,
            team=existing_user.team,
            role=existing_user.roles[0].role,
            username=username,
            api_token=existing_user.api_token,
        )

        return self.fetch_saml_user(name_id)

    def _fetch_saml_user(self, name_id: Text) -> User:
        return self.query(User).filter(User.name_id == name_id).first()

    def fetch_saml_user(self, name_id: Text) -> Optional[Dict[str, Any]]:
        user = self._fetch_saml_user(name_id)
        if user:
            return user.as_dict()
        else:
            return None

    def _delete_single_use_token(self, token: Text) -> None:
        existing_token = (
            self.query(SingleUseToken).filter(SingleUseToken.token == token).first()
        )
        if not existing_token:
            return None

        self.delete(existing_token)

    def update_single_use_token(
        self,
        name_id: Text,
        single_use_token: Text,
        lifetime: float = 60.0
        # 1 minute
    ) -> None:
        existing_name_id_user = self.query(User).filter(User.name_id == name_id).first()

        if not existing_name_id_user:
            raise UserException(f"name_id '{name_id}' not found")

        expires = time.time() + lifetime

        existing_token = existing_name_id_user.single_use_token

        if existing_token:
            self._delete_single_use_token(existing_token.token)

        new_token = SingleUseToken(
            token=single_use_token, expires=expires, username=name_id
        )
        existing_name_id_user.single_use_token = new_token

    def single_use_token_login(
        self, single_use_token: Text, return_api_token: bool = False
    ) -> Optional[Dict]:
        user = (
            self.query(User)
            .filter(User.single_use_token.has(SingleUseToken.token == single_use_token))
            .first()
        )
        if not user:
            logger.debug(f"No user found for single-use token '{single_use_token}'.")
            return None

        # check if token has expired
        token_expires = user.single_use_token.expires
        if time.time() > token_expires:
            logger.debug(
                "single-use token '{}' expired at '{}'"
                "".format(single_use_token, token_expires)
            )
            return None

        # delete single-use token
        self._delete_single_use_token(single_use_token)

        return user.as_dict(return_api_token=return_api_token)

    @staticmethod
    def hash_pw(pw):
        salted_pw = (config.password_salt + pw).encode()
        return hashlib.sha256(salted_pw).hexdigest()

    @staticmethod
    def generate_api_token():
        body = "{}".format(random.random()).encode()
        return hashlib.sha1(body).hexdigest()

    @staticmethod
    def is_username_password_user(user: User) -> bool:
        return user.authentication_mechanism == AuthMechanisms.username_password

    def login(
        self, username: Text, password: Text, return_api_token: bool = False
    ) -> Dict:
        pw_hash = self.hash_pw(password)
        user = self._fetch_user(username)

        if user is None:
            raise exceptions.AuthenticationFailed("Incorrect user or password.")

        if username == config.SYSTEM_USER:
            raise exceptions.AuthenticationFailed(f"Cannot log in user '{username}'.")

        if not self.is_username_password_user(user):
            logger.info(
                "Cannot log in user '{}' with username/password. User "
                "has auth mechanism '{}'."
                "".format(user.username, user.authentication_mechanism)
            )
            raise exceptions.AuthenticationFailed("Incorrect user or password.")

        if user.password_hash != pw_hash:
            raise exceptions.AuthenticationFailed("Incorrect user or password.")

        return user.as_dict(return_api_token=return_api_token)

    def change_password(self, fields: Dict[Text, Text]) -> Optional[Dict]:
        username = fields["username"]
        user = self.login(username, fields["old_password"])

        if user is None:
            return None
        elif fields["new_password"] != fields["new_password_confirm"]:
            raise MismatchedPasswordsException

        user = self._fetch_user(username)
        user.password_hash = self.hash_pw(fields["new_password"])

        # update LocalPassword for community user
        self._update_community_user_password(fields["username"], fields["new_password"])
        return user.as_dict()

    def update_user(self, username: Text, values: Dict[Text, Any]) -> None:
        """Update the properties of a `User`.

        Args:
            username: The user's username.
            values: Values to update the user with.
        """

        user = self._fetch_user(username)
        if not user:
            raise UserException(username)

        user.data = json.dumps(values["data"])

    def admin_change_password(self, username: Text, password: Text) -> Dict:
        existing_user = self._fetch_user(username)
        if not existing_user:
            raise UserException(username)

        existing_user.password_hash = self.hash_pw(password)

        # update LocalPassword for community user
        self._update_community_user_password(username, password)

        return existing_user.as_dict()

    def _update_community_user_password(self, username: Text, password: Text) -> None:
        if username == COMMUNITY_USERNAME:
            from rasax.community.services.settings_service import SettingsService

            SettingsService(self.session).save_community_user_password(password)

    def api_token_auth(self, api_token: Text, return_api_token: bool = False) -> Dict:
        user = self.query(User).filter(User.api_token == api_token).first()
        if user is None:
            raise exceptions.AuthenticationFailed("Incorrect api_token.")
        return user.as_dict(return_api_token=return_api_token)

    def assign_project_to_user(self, user: Dict, project_id: Text) -> None:
        """Update user's project_id."""

        owner = self._fetch_user(user.get("username"))
        if not user:
            return
        owner.project = project_id
        owner.role_name = ADMIN


def has_role(user: Dict[Text, Any], role: Text) -> bool:
    """Checks whether the user possesses a role."""

    return role in user.get("roles")
