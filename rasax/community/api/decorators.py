from functools import wraps
from inspect import isawaitable
from typing import Text, Callable, Union, Tuple, Optional, Dict, Any

import sanic_jwt.utils as sanic_jwt_utils
from jsonschema import validate, ValidationError
from sanic import Blueprint
from sanic.request import Request
from sanic.response import HTTPResponse
from sanic_jwt import exceptions
from sanic_jwt.decorators import instant_config
from sanic_jwt.validators import validate_scopes

from rasax.community import config
from rasax.community.api import json_schema
from rasax.community.constants import REQUEST_DB_SESSION_KEY
from rasax.community.services.role_service import normalise_permissions
from rasax.community.services.user_service import UserService
from rasax.community.utils import default_arg, error


def inject_rasa_x_user(
    initialized_on=None,
    allow_api_token: bool = False,
    extract_user_from_jwt: bool = False,
    **kw,
):
    """Adapted from sanic_jwt.decorators.inject_user"""

    def decorator(f):
        @wraps(f)
        async def decorated_function(request, *args, **kwargs):
            if initialized_on and isinstance(initialized_on, Blueprint):
                instance = initialized_on
            else:
                instance = request.app

            with instant_config(instance, request=request, **kw):
                user = await _get_user_from_request(
                    instance, request, allow_api_token, extract_user_from_jwt
                )
                return await f(request, user=user, *args, **kwargs)

        return decorated_function

    return decorator


async def _get_user_from_request(
    instance,
    request: Request,
    allow_api_token: bool = False,
    extract_user_from_jwt: bool = False,
) -> Optional[Dict[Text, Any]]:
    if allow_api_token:
        # noinspection PyBroadException
        try:
            payload = instance.auth.extract_payload(request, verify=False)
        except Exception:
            payload = None
    else:
        payload = instance.auth.extract_payload(request, verify=False)

    return await instance.auth.retrieve_user(
        request, payload, allow_api_token, extract_user_from_jwt
    )


async def user_from_request(request: Request) -> Optional[Dict[Text, Any]]:
    """Extract a Rasa X user from a request.

    Args:
        request: The HTTP request.

    Returns:
        The user. Might be `None` in case the endpoint does not require authentication.
    """
    with instant_config(request.app, request=request):
        return await _get_user_from_request(request.app, request)


def rasa_x_scoped(
    scopes: Union[Tuple[Text], Text, None] = None,
    require_all: bool = False,
    require_all_actions: bool = True,
    initialized_on: Blueprint = None,
    allow_api_token: bool = False,
    allow_rasa_x_token: bool = False,
    **kw,
) -> Callable:
    """Adapted from sanic_jwt.decorators.scoped"""

    if isinstance(scopes, str):
        scopes = [scopes]

    scopes = normalise_permissions(scopes)

    async def authorise_user(
        request_args, request_kwargs, instance, reasons, request, status, user_scopes
    ):
        # Retrieve the scopes from the payload if not provided
        if not user_scopes:
            user_scopes = instance.auth.extract_scopes(request)

        if user_scopes is None:
            # If there are no defined scopes in the payload,
            # deny access
            is_authorized = False
            status = 403
            reasons = "Invalid scope"
        else:
            override = instance.auth.override_scope_validator
            destructure = instance.auth.destructure_scopes
            is_authorized = await validate_scopes(
                request,
                scopes,
                user_scopes,
                require_all=require_all,
                require_all_actions=require_all_actions,
                override=override,
                destructure=destructure,
                request_args=request_args,
                request_kwargs=request_kwargs,
            )
            if not is_authorized:
                status = 403
                reasons = "Invalid scope"

        return is_authorized, reasons, status

    def decorator(f):
        async def await_and_return_response(args, kwargs, request):
            response = f(request, *args, **kwargs)
            if isawaitable(response):
                response = await response
            return response

        @wraps(f)
        async def decorated_function(request, *args, **kwargs):
            user_service = UserService(request[REQUEST_DB_SESSION_KEY])

            if initialized_on and isinstance(initialized_on, Blueprint):
                instance = initialized_on
            else:
                instance = request.app

            with instant_config(instance, request=request, **kw):
                if request.method == "OPTIONS":
                    return await sanic_jwt_utils.call(f, request, *args, **kwargs)

                is_authenticated = False
                user_scopes = None
                reasons = None
                status = None

                if allow_rasa_x_token:
                    rasa_x_token = default_arg(request, "token", None)
                    if rasa_x_token == config.rasa_x_token:
                        return await await_and_return_response(args, kwargs, request)

                if allow_api_token:
                    # if decorator allows api_tokens for authentication
                    # skip the usual JWT authentication
                    api_token = default_arg(request, "api_token")
                    if api_token:
                        user = user_service.api_token_auth(api_token)
                        is_authenticated = True
                        status = 200
                        permissions = user["permissions"]
                        user_scopes = normalise_permissions(permissions)

                if not is_authenticated:
                    try:
                        (
                            is_authenticated,
                            status,
                            reasons,
                        ) = instance.auth._check_authentication(
                            request, request_args=args, request_kwargs=kwargs
                        )
                    except AttributeError:
                        raise exceptions.SanicJWTException(
                            "Authentication instance not found. Perhaps you "
                            "used @scoped without passing in a blueprint? "
                            "Try @scoped(..., initialized_on=blueprint)",
                            status_code=500,
                        )
                    except exceptions.SanicJWTException as e:
                        status = e.status_code
                        reasons = e.args[0]

                if is_authenticated:
                    is_authorized, reasons, status = await authorise_user(
                        args, kwargs, instance, reasons, request, status, user_scopes
                    )
                else:
                    is_authorized = False

                if is_authorized:
                    # the user is authorized.
                    # run the handler method and return the response
                    # NOTE: it's possible to use return await.utils(f, ...) in
                    # here, but inside the @protected decorator it wont work,
                    # so this is left as is for now
                    return await await_and_return_response(args, kwargs, request)

                else:
                    raise exceptions.Unauthorized(reasons, status_code=status)

        return decorated_function

    return decorator


def validate_schema(schema: Text, optional: bool = False) -> Callable:
    """Decorator which checks whether a json request body is compliant with a schema.

    Args:
        schema: The schema which should be used to validate the payload.
        optional: `True` if an empty request body is valid.

    Returns:
        The function which decorates the annotated function.
    """

    def decorator(f):
        @wraps(f)
        async def decorated_function(
            request: Request, *args: Any, **kwargs: Any
        ) -> HTTPResponse:
            json_to_validate = request.json
            skip_validation = json_to_validate is None and optional

            try:
                if not skip_validation:
                    validate(json_to_validate, json_schema[schema])

                return await f(request, *args, **kwargs)
            except ValidationError as e:
                return error(
                    400,
                    "WrongSchema",
                    "The payload schema is invalid."
                    "Please check the API specification at "
                    "https://rasa.com/docs/rasa-x/api/rasa-x-http-api/ for the correct schema.",
                    details=e,
                )

        return decorated_function

    return decorator
