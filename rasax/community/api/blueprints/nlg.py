import http
import logging
import warnings
from typing import Dict, Text, Any

from sanic.response import HTTPResponse

import rasax.community.utils as rasa_x_utils

from sanic import Blueprint, response
from sanic.request import Request

from rasax.community import utils
from rasax.community.api.decorators import (
    rasa_x_scoped,
    inject_rasa_x_user,
    validate_schema,
)
from rasax.community.constants import REQUEST_DB_SESSION_KEY, RESPONSE_NAME_KEY
from rasax.community.services import background_dump_service
from rasax.community.services.nlg_service import NlgService
from rasax.community.services.domain_service import DomainService

logger = logging.getLogger(__name__)


def _nlg_service(request: Request) -> NlgService:
    return NlgService(request[REQUEST_DB_SESSION_KEY])


def _domain_service(request: Request) -> DomainService:
    return DomainService(request[REQUEST_DB_SESSION_KEY])


def blueprint() -> Blueprint:
    nlg_endpoints = Blueprint("nlg_endpoints")

    @nlg_endpoints.route("/responses", methods=["GET", "HEAD", "OPTIONS"])
    @rasa_x_scoped("responseTemplates.list", allow_api_token=True)
    async def get_responses(request: Request) -> HTTPResponse:
        text_query = utils.default_arg(request, "q", None)
        response_query = utils.default_arg(request, RESPONSE_NAME_KEY, None)
        fields = utils.fields_arg(request, {"text", RESPONSE_NAME_KEY, "id"})

        limit = utils.int_arg(request, "limit")
        offset = utils.int_arg(request, "offset", 0)

        responses, total_number = _nlg_service(request).fetch_responses(
            text_query, response_query, fields, limit, offset
        )

        return response.json(responses, headers={"X-Total-Count": total_number})

    @nlg_endpoints.route("/templates", methods=["GET", "HEAD", "OPTIONS"])
    @rasa_x_scoped("responseTemplates.list", allow_api_token=True)
    async def get_templates(request: Request) -> HTTPResponse:
        warnings.warn(
            f'The endpoint "/templates" is deprecated, please use "/responses" instead',
            category=FutureWarning,
        )
        rasa_x_utils.handle_deprecated_request_parameters(
            request, "template", RESPONSE_NAME_KEY
        )
        return await get_responses(request)

    @nlg_endpoints.route("/responseGroups", methods=["GET", "HEAD", "OPTIONS"])
    @rasa_x_scoped("responseTemplates.list", allow_api_token=True)
    async def get_grouped_responses(request: Request) -> HTTPResponse:
        rasa_x_utils.handle_deprecated_request_parameters(
            request, "template", RESPONSE_NAME_KEY
        )
        text_query = utils.default_arg(request, "q", None)
        response_query = utils.default_arg(request, RESPONSE_NAME_KEY, None)

        responses, total_number = _nlg_service(request).get_grouped_responses(
            text_query, response_query
        )

        return response.json(responses, headers={"X-Total-Count": total_number})

    @nlg_endpoints.route(
        "/responseGroups/<response_name>", methods=["PUT", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("responseTemplates.update")
    @inject_rasa_x_user()
    @validate_schema("nlg/response_name")
    async def rename_response(
        request: Request, response_name: Text, user: Dict[Text, Any]
    ) -> HTTPResponse:
        rjs = request.json
        _nlg_service(request).rename_responses(response_name, rjs, user["username"])
        return response.text("", status=http.HTTPStatus.OK)

    @nlg_endpoints.route("/responses", methods=["POST"])
    @rasa_x_scoped("responseTemplates.create", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    @validate_schema("nlg/response")
    async def add_response(request: Request, user: Dict[Text, Any]) -> HTTPResponse:
        rjs = request.json
        domain_id = _domain_service(request).get_domain_id()

        try:
            saved_response = _nlg_service(request).save_response(
                rjs, user["username"], domain_id=domain_id
            )
            background_dump_service.add_domain_change()

            return response.json(saved_response, http.HTTPStatus.CREATED)

        except (AttributeError, ValueError) as e:
            status_code = (
                http.HTTPStatus.UNPROCESSABLE_ENTITY
                if isinstance(e, AttributeError)
                else http.HTTPStatus.BAD_REQUEST
            )

            return rasa_x_utils.error(
                status_code,
                "WrongResponse",
                "Could not add the specified response.",
                str(e),
            )

    @nlg_endpoints.route("/templates", methods=["POST"])
    @rasa_x_scoped("responseTemplates.create", allow_api_token=True)
    @validate_schema("nlg/response")
    async def add_template(request: Request) -> HTTPResponse:
        warnings.warn(
            f'The endpoint "/templates" is deprecated, please use "/responses" instead.',
            category=FutureWarning,
        )
        return await add_response(request)

    @nlg_endpoints.route("/responses", methods=["PUT"])
    @rasa_x_scoped("bulkResponseTemplates.update", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    @validate_schema("nlg/response_bulk")
    async def update_responses(request: Request, user: Dict[Text, Any]) -> HTTPResponse:
        """Delete old bot responses and replace them with the responses in the
        payload."""

        rjs = request.json
        domain_id = _domain_service(request).get_domain_id()
        try:
            inserted_count = _nlg_service(request).replace_responses(
                rjs, user["username"], domain_id=domain_id
            )
            background_dump_service.add_domain_change()

            return response.text(f"Successfully uploaded {inserted_count} responses.")

        except AttributeError as e:
            status_code = (
                http.HTTPStatus.UNPROCESSABLE_ENTITY
                if isinstance(e, AttributeError)
                else http.HTTPStatus.BAD_REQUEST
            )
            return rasa_x_utils.error(
                status_code,
                "WrongResponse",
                "Could not update the specified response.",
                str(e),
            )

    @nlg_endpoints.route("/templates", methods=["PUT"])
    @rasa_x_scoped("bulkResponseTemplates.update", allow_api_token=True)
    @validate_schema("nlg/response_bulk")
    async def update_templates(request: Request) -> HTTPResponse:
        warnings.warn(
            f'The endpoint "/templates" is deprecated, please use "/responses" instead.',
            category=FutureWarning,
        )
        return await update_responses(request)

    @nlg_endpoints.route("/responses/<response_id:int>", methods=["PUT", "OPTIONS"])
    @rasa_x_scoped("responseTemplates.update", allow_api_token=True)
    @inject_rasa_x_user(allow_api_token=True)
    @validate_schema("nlg/response")
    async def modify_response(
        request: Request, response_id: int, user: Dict[Text, Any]
    ) -> HTTPResponse:
        rjs = request.json
        try:
            updated_response = _nlg_service(request).update_response(
                response_id, rjs, user["username"]
            )
            background_dump_service.add_domain_change()
            return response.json(updated_response.as_dict())

        except (KeyError, AttributeError, ValueError) as e:
            if isinstance(e, KeyError):
                status_code = http.HTTPStatus.NOT_FOUND
            elif isinstance(e, AttributeError):
                status_code = http.HTTPStatus.UNPROCESSABLE_ENTITY
            else:
                status_code = http.HTTPStatus.BAD_REQUEST

            return rasa_x_utils.error(
                status_code,
                "WrongResponse",
                "Could not modify the specified response.",
                str(e),
            )

    @nlg_endpoints.route("/templates/<response_id:int>", methods=["PUT", "OPTIONS"])
    @rasa_x_scoped("responseTemplates.update", allow_api_token=True)
    @validate_schema("nlg/response")
    async def modify_template(request: Request, response_id: int) -> HTTPResponse:
        warnings.warn(
            f'The endpoint "/templates/<responses_id>" is deprecated, please use '
            f'"/responses/<responses_id>" instead.',
            category=FutureWarning,
        )
        return await modify_response(request, response_id)

    @nlg_endpoints.route("/responses/<response_id:int>", methods=["DELETE"])
    @rasa_x_scoped("responseTemplates.delete", allow_api_token=True)
    async def delete_response(request: Request, response_id: int) -> HTTPResponse:
        deleted = _nlg_service(request).delete_response(response_id)
        if deleted:
            background_dump_service.add_domain_change()
            return response.text("", http.HTTPStatus.NO_CONTENT)
        return rasa_x_utils.error(
            http.HTTPStatus.NOT_FOUND,
            "ResponseNotFound",
            "Response could not be found.",
        )

    @nlg_endpoints.route("/templates/<response_id:int>", methods=["DELETE"])
    @rasa_x_scoped("responseTemplates.delete", allow_api_token=True)
    async def delete_template(request: Request, response_id: int) -> HTTPResponse:
        warnings.warn(
            f'The endpoint "/templates/<responses_id>" is deprecated, please use '
            f'"/responses/<responses_id>" instead.',
            category=FutureWarning,
        )
        return await delete_response(request, response_id)

    return nlg_endpoints
