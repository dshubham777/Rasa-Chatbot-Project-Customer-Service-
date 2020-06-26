import logging
from http import HTTPStatus
from typing import Tuple, Dict, Text, Any, List, Optional, Union

import jwt
from sanic import Sanic
from sanic.request import Request
from sanic.response import HTTPResponse
from sanic.handlers import ErrorHandler
from sanic.exceptions import SanicException
from sanic_cors import CORS
from sanic_jwt import Initialize, Responses
from sanic_jwt import exceptions

import rasax.community.utils as rasa_x_utils
from rasax.community import config, constants
from rasax.community.api.blueprints import (
    stack,
    nlg,
    nlu,
    models,
    intents,
    project,
    interface,
    telemetry,
)
from rasax.community.api.blueprints.conversations import tags, slots, channels

from rasax.community.constants import API_URL_PREFIX, REQUEST_DB_SESSION_KEY
from rasax.community.database.utils import setup_db
from rasax.community.services import model_service
from rasax.community.services.role_service import normalise_permissions
from rasax.community.services.user_service import UserService, has_role, GUEST

logger = logging.getLogger(__name__)


class ExtendedResponses(Responses):
    @staticmethod
    def extend_verify(request, user=None, payload=None):
        return {"username": jwt.decode(request.token, verify=False)["username"]}


class RasaXErrorHandler(ErrorHandler):
    """Sanic error handler for the Rasa X API.
    """

    def default(
        self, request: Request, exception: Union[Exception, SanicException]
    ) -> HTTPResponse:
        """Handle unexpected errors when processing an HTTP request.

        Args:
            request: The current HTTP request being processed.
            exception: The unhandled exception that was raised during processing.

        Returns:
            HTTP response with status code 500.
        """
        # Call `ErrorHandler.default()` to log the exception.
        super().default(request, exception)

        # Return a custom JSON that looks familiar to API users.
        return rasa_x_utils.error(
            HTTPStatus.INTERNAL_SERVER_ERROR,
            "The server encountered an internal error and cannot complete the request.",
            message="See the server logs for more information.",
        )


async def authenticate(request, *args, **kwargs):
    """Set up JWT auth."""

    user_service = UserService(request[REQUEST_DB_SESSION_KEY])
    rjs = request.json

    # enterprise SSO single-use-token login
    if rjs and rjs.get("single_use_token") is not None:
        user = user_service.single_use_token_login(
            rjs["single_use_token"], return_api_token=True
        )
        if user:
            return user
        else:
            raise exceptions.AuthenticationFailed("Wrong authentication token.")

    if not rjs:
        raise exceptions.AuthenticationFailed("Missing username or password.")

    # standard auth with username and password in request
    username = rjs.get("username", None)
    password = rjs.get("password", None)

    if username and password:
        return user_service.login(username, password, return_api_token=True)

    raise exceptions.AuthenticationFailed("Missing username or password.")


def remove_unused_payload_keys(user_dict: Dict[Text, Any]):
    """Removes unused keys from `user` dictionary in JWT payload.

    Removes keys `permissions`, `authentication_mechanism`, `projects` and  `team`."""

    for key in ["permissions", "authentication_mechanism", "projects", "team"]:
        del user_dict[key]


async def scope_extender(user: Dict[Text, Any], *args, **kwargs) -> List[Text]:
    permissions = user["permissions"]
    remove_unused_payload_keys(user)
    return normalise_permissions(permissions)


async def payload_extender(payload, user):
    payload.update({"user": user})
    return payload


async def retrieve_user(
    request: Request,
    payload: Dict,
    allow_api_token: bool,
    extract_user_from_jwt: bool,
    *args: Any,
    **kwargs: Any,
) -> Optional[Dict]:
    if extract_user_from_jwt and payload and has_role(payload.get("user"), GUEST):
        return payload["user"]

    user_service = UserService(request[REQUEST_DB_SESSION_KEY])

    if allow_api_token:
        api_token = rasa_x_utils.default_arg(request, "api_token")
        if api_token:
            return user_service.api_token_auth(api_token)

    if payload:
        username = payload.get("username", None)
        if username is not None:
            return user_service.fetch_user(username)
        else:
            # user is first-time enterprise user and has username None
            # in this case we'll fetch the profile using name_id
            name_id = payload.get("user", {}).get("name_id", None)
            return user_service.fetch_user(name_id)

    return None


def initialize_app(app: Sanic, class_views: Tuple = ()) -> None:
    Initialize(
        app,
        authenticate=authenticate,
        add_scopes_to_payload=scope_extender,
        extend_payload=payload_extender,
        class_views=class_views,
        responses_class=ExtendedResponses,
        retrieve_user=retrieve_user,
        algorithm=constants.JWT_METHOD,
        private_key=config.jwt_private_key,
        public_key=config.jwt_public_key,
        url_prefix="/api/auth",
        user_id="username",
    )


def configure_app(local_mode: bool = config.LOCAL_MODE) -> Sanic:
    """Create the Sanic app with the endpoint blueprints.

    Args:
        local_mode: `True` if Rasa X is running in local mode, `False` for server mode.

    Returns:
        Sanic app including the available blueprints.
    """

    # sanic-cors shows a DEBUG message for every request which we want to
    # suppress
    logging.getLogger("sanic_cors").setLevel(logging.INFO)
    logging.getLogger("spf.framework").setLevel(logging.INFO)

    app = Sanic(__name__, configure_logging=config.debug_mode)

    # allow CORS and OPTIONS on every endpoint
    CORS(
        app,
        expose_headers=["X-Total-Count"],
        automatic_options=True,
        max_age=config.SANIC_ACCESS_CONTROL_MAX_AGE,
    )

    # Configure Sanic response timeout
    app.config.RESPONSE_TIMEOUT = config.SANIC_RESPONSE_TIMEOUT_IN_SECONDS

    # set max request size (for large model uploads)
    app.config.REQUEST_MAX_SIZE = config.SANIC_REQUEST_MAX_SIZE_IN_BYTES

    # set JWT expiration time
    app.config.SANIC_JWT_EXPIRATION_DELTA = config.jwt_expiration_time

    # Install a custom error handler
    app.error_handler = RasaXErrorHandler()

    # Set up Blueprints
    app.blueprint(interface.blueprint())
    app.blueprint(project.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(stack.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(tags.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(models.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(nlg.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(nlu.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(intents.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(telemetry.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(slots.blueprint(), url_prefix=API_URL_PREFIX)
    app.blueprint(channels.blueprint(), url_prefix=API_URL_PREFIX)

    if not local_mode and rasa_x_utils.is_git_available():
        from rasax.community.api.blueprints import git

        app.blueprint(git.blueprint(), url_prefix=API_URL_PREFIX)

    app.register_listener(setup_db, "before_server_start")
    rasa_x_utils.run_operation_in_single_sanic_worker(
        app, model_service.discover_models
    )

    return app
