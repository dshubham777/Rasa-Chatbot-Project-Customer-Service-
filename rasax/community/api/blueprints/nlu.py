import json
import logging
from http import HTTPStatus
from typing import Text, Dict, Any, Optional

from aiohttp import ClientError
from sanic import Blueprint, response
from sanic.request import Request

from rasa.nlu.training_data.loading import RASA, MARKDOWN
from rasax.community import utils, telemetry
from rasax.community.api.blueprints.models import _model_service
from rasax.community.api.decorators import (
    rasa_x_scoped,
    inject_rasa_x_user,
    validate_schema,
)
from rasax.community.constants import REQUEST_DB_SESSION_KEY
from rasax.community.services.data_service import DataService
from rasax.community.services.evaluation_service import EvaluationService
from rasax.community.services.intent_service import INTENT_MAPPED_TO_KEY
from rasax.community.services.intent_service import IntentService
from rasax.community.services.logs_service import LogsService
from rasax.community.services.settings_service import SettingsService
from rasax.community.services.stack_service import StackService
from rasax.community.utils import error, get_text_hash
from sanic.response import HTTPResponse

logger = logging.getLogger(__name__)


def _stack_service(
    request: Request, project_id: Text, default_environment: Text
) -> StackService:
    settings_service = SettingsService(request[REQUEST_DB_SESSION_KEY])

    environment = utils.deployment_environment_from_request(
        request, default_environment
    )
    service = settings_service.get_stack_service(environment, project_id)
    if not service:
        error(
            HTTPStatus.NOT_FOUND,
            "ServiceNotFound",
            f"Service for requested environment '{environment}' not found.",
        )

    return service


def _log_service(request: Request) -> LogsService:
    return LogsService(request[REQUEST_DB_SESSION_KEY])


def _data_service(request: Request) -> DataService:
    return DataService(request[REQUEST_DB_SESSION_KEY])


def _evaluation_service(
    request: Request, project_id: Text, default_environment: Text
) -> EvaluationService:
    service = _stack_service(request, project_id, default_environment)
    return EvaluationService(service, request[REQUEST_DB_SESSION_KEY])


async def _create_message_log_from_query(
    request: Request, project_id: Text, query: Text
) -> Dict[Text, Any]:
    stack_service = _stack_service(request, project_id, "worker")
    stack_service_has_model = await stack_service.has_active_model()

    if stack_service_has_model:
        parse_data = await stack_service.parse(query)
    else:
        # We don't have an active model to create an initial guess of the intent and
        # entities. We still want to be able to create a log, and will use parse data
        # that only contains the query under the `text` key.
        parse_data = {"text": query}

    logs_service = _log_service(request)
    log_hash = get_text_hash(query)
    existing_log = logs_service.get_log_by_hash(log_hash)

    if existing_log:
        existing_log_dict = existing_log.as_dict()
        return logs_service.replace_log(
            existing_log_dict, parse_data, created_from_model=stack_service_has_model
        )
    else:
        return logs_service.create_log_from_parse_data(
            parse_data, created_from_model=stack_service_has_model
        )


def blueprint():
    nlu_endpoints = Blueprint("nlu_endpoints")

    @nlu_endpoints.route(
        "/projects/<project_id>/logs", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("logs.list")
    async def suggestions(request: Request, project_id: Text) -> HTTPResponse:
        limit = utils.int_arg(request, "limit")
        offset = utils.int_arg(request, "offset", 0)
        text_query = utils.default_arg(request, "q", None)
        intent_query = utils.default_arg(request, "intent", None)
        fields = utils.fields_arg(
            request, {"user_input.intent.name", "user_input.text", "id"}
        )
        distinct = utils.bool_arg(request, "distinct", True)
        suggested, total_suggestions = _log_service(request).get_suggestions(
            text_query, intent_query, fields, limit, offset, distinct
        )

        return response.json(suggested, headers={"X-Total-Count": total_suggestions})

    # noinspection PyUnusedLocal
    @nlu_endpoints.route(
        "/projects/<project_id>/logs/<log_id>", methods=["DELETE", "OPTIONS"]
    )
    @rasa_x_scoped("logs.delete")
    @inject_rasa_x_user()
    async def archive_one(
        request: Request, project_id: Text, log_id, user: Optional[Dict] = None
    ) -> HTTPResponse:
        success = _log_service(request).archive(log_id)
        if not success:
            return error(
                HTTPStatus.BAD_REQUEST,
                "ArchiveLogFailed",
                f"Failed to archive log with log_id {log_id}",
            )
        return response.text("", HTTPStatus.NO_CONTENT)

    @nlu_endpoints.route(
        "/projects/<project_id>/logs/<_hash>", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("logs.get")
    async def get_log_by_hash(
        request: Request, project_id: Text, _hash: Text
    ) -> HTTPResponse:
        log = _log_service(request).get_log_by_hash(_hash)
        if not log:
            return error(
                HTTPStatus.BAD_REQUEST,
                "NluLogError",
                f"Log with hash {_hash} could not be found",
            )

        return response.json(log.as_dict())

    @nlu_endpoints.route("/projects/<project_id>/logs", methods=["POST"])
    @rasa_x_scoped("logs.create")
    async def add_log(request: Request, project_id: Text) -> HTTPResponse:
        query = utils.default_arg(request, "q", "")
        # if no query text found, check for json content
        if not query:
            query = request.json
            if not utils.check_schema("log", query):
                return error(
                    HTTPStatus.BAD_REQUEST,
                    "WrongSchema",
                    "Please check the schema of your NLU query.",
                )

        try:
            created_log = await _create_message_log_from_query(
                request, project_id, query
            )
            return response.json(created_log)
        except ClientError as e:
            logger.warning(f"Parsing the message '{query}' failed. Error: {e}")
            return error(
                HTTPStatus.NOT_FOUND,
                "NluParseFailed",
                f"Failed to parse NLU query ('{query}').",
                details=e,
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/data", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("examples.list")
    async def training_data(request: Request, project_id: Text) -> HTTPResponse:
        """Retrieve the training data for the project."""

        limit = utils.int_arg(request, "limit")
        offset = utils.int_arg(request, "offset", 0)
        text_query = utils.default_arg(request, "q", None)
        intent_query = utils.default_arg(request, "intent", None)
        entity_query = utils.bool_arg(request, "entities", False)
        sort_by_descending_id = utils.bool_arg(request, "sorted", True)
        fields = utils.fields_arg(
            request, {"intent", "text", "id", "entities", "entities.entity"}
        )
        distinct = utils.bool_arg(request, "distinct", True)
        data_query = _data_service(request).get_training_data(
            project_id=project_id,
            sort_by_descending_id=sort_by_descending_id,
            text_query=text_query,
            intent_query=intent_query,
            entity_query=entity_query,
            fields_query=fields,
            limit=limit,
            offset=offset,
            distinct=distinct,
        )

        return response.json(
            data_query.result, headers={"X-Total-Count": data_query.count}
        )

    # noinspection PyUnusedLocal
    @nlu_endpoints.route(
        "/projects/<project_id>/data/<_hash>", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("examples.get")
    async def training_example_by_hash(
        request: Request, project_id: Text, _hash: Text
    ) -> HTTPResponse:
        """Retrieve a training example by its hash."""
        example = _data_service(request).get_example_by_hash(project_id, _hash)
        if not example:
            return error(
                HTTPStatus.BAD_REQUEST,
                "TrainingExampleError",
                "Example could not be found.",
            )

        return response.json(example)

    # noinspection PyUnusedLocal
    @nlu_endpoints.route(
        "/projects/<project_id>/dataWarnings", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("warnings.get")
    async def training_data_warnings(
        request: Request, project_id: Text
    ) -> HTTPResponse:
        """Retrieve the training data for the project."""

        warnings = _data_service(request).get_training_data_warnings(
            project_id=project_id
        )

        return response.json(warnings)

    @nlu_endpoints.route(
        "/projects/<project_id>/data.json", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("bulkData.get", allow_api_token=True)
    async def training_data_as_json(request: Request, project_id: Text) -> HTTPResponse:
        """Download the training data for the project in Rasa NLU json format."""

        data_service = _data_service(request)
        content = data_service.create_formatted_training_data(project_id=project_id)

        return response.text(
            json.dumps(content, indent=4, ensure_ascii=False),
            content_type="application/json",
            headers={"Content-Disposition": "attachment;filename=nlu.train.json"},
        )

    @nlu_endpoints.route(
        "/projects/<project_id>/data.md", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("bulkData.get", allow_api_token=True)
    async def training_data_as_md(request: Request, project_id: Text) -> HTTPResponse:
        """Download the training data for the project in Rasa NLU markdown format."""

        data_service = _data_service(request)
        content = data_service.get_nlu_training_data_object(project_id=project_id)

        return response.text(
            content.nlu_as_markdown(),
            content_type="text/markdown",
            headers={"Content-Disposition": "attachment;filename=nlu.train.md"},
        )

    @nlu_endpoints.route("/projects/<project_id>/data", methods=["POST"])
    @rasa_x_scoped("examples.create")
    @inject_rasa_x_user()
    @validate_schema("data")
    async def add_training_example(
        request: Request, project_id: Text, user: Optional[Dict] = None
    ) -> HTTPResponse:
        """Add a new training example to the project."""

        rjs = request.json
        data_service = _data_service(request)
        example_hash = get_text_hash(rjs.get("text"))
        existing_example = data_service.get_example_by_hash(project_id, example_hash)
        if existing_example:

            example_id = existing_example["id"]
            example = data_service.replace_example(user, project_id, rjs, example_id)

        else:
            example = data_service.save_example(user["username"], project_id, rjs)

        if not example:
            return error(
                HTTPStatus.BAD_REQUEST, "SaveExampleError", "Example could not be saved"
            )

        telemetry.track_message_annotated_from_referrer(request.headers.get("Referer"))
        return response.json(example)

    @nlu_endpoints.route(
        "/projects/<project_id>/data/<example_id:int>", methods=["PUT", "OPTIONS"]
    )
    @rasa_x_scoped("examples.update")
    @inject_rasa_x_user()
    @validate_schema("data")
    async def update_example(
        request: Request, project_id: Text, example_id, user: Optional[Dict] = None
    ) -> HTTPResponse:
        """Update an existing training example."""

        rjs = request.json
        data_service = _data_service(request)
        example_hash = get_text_hash(rjs.get("text"))
        existing_example = data_service.get_example_by_hash(project_id, example_hash)
        if existing_example and example_id != existing_example["id"]:
            return error(
                HTTPStatus.BAD_REQUEST,
                "ExampleUpdateFailed",
                "Example with this text already exists",
            )

        mapped_to = rjs.get("intent_mapped_to")
        if mapped_to:
            intent_service = IntentService(request[REQUEST_DB_SESSION_KEY])
            intent = {INTENT_MAPPED_TO_KEY: mapped_to}
            intent_service.update_temporary_intent(rjs["intent"], intent, project_id)
            intent_service.add_example_to_temporary_intent(
                rjs[intent], example_hash, project_id
            )
        updated_example = data_service.replace_example(
            user, project_id, rjs, example_id
        )
        if not updated_example:
            return error(
                HTTPStatus.BAD_REQUEST,
                "ExampleUpdateFailed",
                "Example could not be updated",
            )

        return response.json(updated_example)

    # noinspection PyUnusedLocal
    @nlu_endpoints.route(
        "/projects/<project_id>/data/<example_id:int>", methods=["DELETE"]
    )
    @rasa_x_scoped("examples.delete")
    @inject_rasa_x_user()
    async def training_data_delete(
        request: Request, project_id: Text, example_id: int, user: Optional[Dict]
    ) -> HTTPResponse:
        """Remove a training example from the project by id."""

        success = _data_service(request).delete_example(example_id)
        if success:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return error(
                HTTPStatus.NOT_FOUND,
                "DeleteTrainingExampleFailed",
                "Training example with example_id '{}' could not be "
                "deleted".format(example_id),
            )

    @nlu_endpoints.route("/projects/<project_id>/data", methods=["PUT"])
    @rasa_x_scoped("bulkData.update")
    @inject_rasa_x_user()
    async def training_data_bulk(
        request: Request, project_id: Text, user: Optional[Dict] = None
    ) -> HTTPResponse:
        """Replace existing training samples with the posted data."""

        content_type = request.headers.get("Content-Type")
        try:
            data_service = _data_service(request)
            if content_type == "application/json":
                rjs = request.json
                data_service.replace_data(project_id, rjs, RASA, user["username"])
            elif content_type == "text/markdown":
                data = request.body
                data_service.replace_data(project_id, data, MARKDOWN, user["username"])

            return response.text("Bulk upload of training data successful.")
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.BAD_REQUEST,
                "AddTrainingDataFailed",
                "Failed to add training data",
                details=e,
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/regexes", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("regexes.list")
    async def get_regex_features(request: Request, project_id: Text) -> HTTPResponse:
        """Get the regular expressions for a project."""

        limit = utils.int_arg(request, "limit")
        offset = utils.int_arg(request, "offset", 0)

        data_service = _data_service(request)
        regexes = data_service.get_regex_features(
            project_id, offset=offset, limit=limit
        )

        return response.json(regexes.result, headers={"X-Total-Count": regexes.count})

    @nlu_endpoints.route("/projects/<project_id>/regexes", methods=["POST"])
    @rasa_x_scoped("regexes.create")
    @validate_schema("regex")
    async def create_regular_expression(
        request: Request, project_id: Text
    ) -> HTTPResponse:
        """Get the regular expressions for a project."""

        data_service = _data_service(request)
        try:
            created = data_service.create_regex_feature(request.json, project_id)
            return response.json(created, status=HTTPStatus.CREATED)
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.BAD_REQUEST,
                "CreatingRegexFailed",
                "Failed to create regex.",
                details=e,
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/regexes/<regex_id:int>",
        methods=["GET", "HEAD", "OPTIONS"],
    )
    @rasa_x_scoped("regexes.get")
    async def get_regex_feature_by_id(
        request: Request, project_id: Text, regex_id: int
    ) -> HTTPResponse:
        """Get regular expression by its id."""

        data_service = _data_service(request)
        try:
            regex = data_service.get_regex_feature_by_id(regex_id)
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.NOT_FOUND,
                "GetRegexFailed",
                "Failed to get regex by id.",
                details=e,
            )

        return response.json(regex)

    @nlu_endpoints.route(
        "/projects/<project_id>/regexes/<regex_id:int>", methods=["PUT"]
    )
    @rasa_x_scoped("regexes.update")
    @validate_schema("regex")
    async def update_regex_feature(
        request: Request, project_id: Text, regex_id: int
    ) -> HTTPResponse:
        """Update an existing regular expressions."""

        data_service = _data_service(request)
        try:
            updated = data_service.update_regex_feature(regex_id, request.json)
            return response.json(updated)
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.NOT_FOUND,
                "UpdatingRegexFailed",
                "Failed to update regex.",
                details=e,
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/regexes/<regex_id:int>", methods=["DELETE"]
    )
    @rasa_x_scoped("regexes.delete")
    async def delete_regex_feature(
        request: Request, project_id: Text, regex_id: int
    ) -> HTTPResponse:
        """Delete regular expression by its id."""

        data_service = _data_service(request)
        try:
            data_service.delete_regex_feature(regex_id)
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.NOT_FOUND,
                "DeletingRegexFailed",
                "Failed to delete regex by id.",
                details=e,
            )

        return response.text("", status=HTTPStatus.NO_CONTENT)

    @nlu_endpoints.route(
        "/projects/<project_id>/lookupTables", methods=["GET", "OPTIONS"]
    )
    @rasa_x_scoped("lookup_tables.list")
    async def get_lookup_tables(request: Request, project_id: Text) -> HTTPResponse:
        """Returns all lookup tables of a certain project.

        The returned objects do not contain the elements of the lookup tables, but
        the reference to the lookup table file.
        """

        data_service = _data_service(request)
        lookup_tables = data_service.get_lookup_tables(project_id)

        return response.json(
            lookup_tables, headers={"X-Total-Count": len(lookup_tables)}
        )

    @nlu_endpoints.route(
        "/projects/<project_id>/lookupTables/<lookup_table_id:int>",
        methods=["GET", "OPTIONS"],
    )
    @rasa_x_scoped("lookup_tables.get")
    async def get_lookup_table_content(
        request: Request, project_id: Text, lookup_table_id: int
    ) -> HTTPResponse:
        """Returns the content of a lookup table."""

        data_service = _data_service(request)
        try:
            content = data_service.get_lookup_table(lookup_table_id)
            return response.text(content)
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.NOT_FOUND,
                "GettingLookupTableFailed",
                f"Lookup table with id '{lookup_table_id}' does not exist.",
                details=e,
            )

    @nlu_endpoints.route("/projects/<project_id>/lookupTables", methods=["POST"])
    @rasa_x_scoped("lookup_tables.create")
    @validate_schema("lookup_table")
    async def create_lookup_table(request: Request, project_id: Text) -> HTTPResponse:
        """Upload a lookup table."""

        lookup_table = request.json
        content = lookup_table["content"]
        content = utils.decode_base64(content)

        data_service = _data_service(request)
        created = data_service.save_lookup_table(
            lookup_table["filename"], content, project_id
        )

        return response.json(created, status=HTTPStatus.CREATED)

    @nlu_endpoints.route(
        "/projects/<project_id>/lookupTables/<lookup_table_id:int>", methods=["DELETE"]
    )
    @rasa_x_scoped("lookup_tables.delete")
    async def delete_lookup_table(
        request: Request, project_id: Text, lookup_table_id: int
    ) -> HTTPResponse:
        """Deletes a lookup table."""

        data_service = _data_service(request)
        try:
            data_service.delete_lookup_table(lookup_table_id)
            return response.text("", HTTPStatus.NO_CONTENT)
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.NOT_FOUND,
                "LookupTableDeletionFailed",
                f"Lookup table with id '{lookup_table_id}' does not exist.",
                details=e,
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/synonyms", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("entity_synonyms.list", allow_api_token=True)
    async def get_entity_synonyms(request: Request, project_id: Text) -> HTTPResponse:
        """Get all entity synonyms and their mapped values."""
        data_service = _data_service(request)

        mapped_value_query = utils.default_arg(request, "mapped_value")
        if mapped_value_query:
            matching_synonym = data_service.get_synonym_by_mapped_value(
                mapped_value_query, project_id
            )
            if matching_synonym:
                entity_synonyms = [matching_synonym]
            else:
                entity_synonyms = []
        else:
            entity_synonyms = data_service.get_entity_synonyms(
                project_id, nlu_format=False
            )

        return response.json(
            entity_synonyms, headers={"X-Total-Count": len(entity_synonyms)}
        )

    @nlu_endpoints.route("/projects/<project_id>/synonyms", methods=["POST"])
    @rasa_x_scoped("entity_synonyms.create", allow_api_token=True)
    @validate_schema("entity_synonym")
    async def create_entity_synonym(request: Request, project_id: Text) -> HTTPResponse:
        """Create a new entity synonym with mapped values."""

        data_service = _data_service(request)

        synonym_name = request.json["synonym_reference"]
        mapped_values = [item["value"] for item in request.json["mapped_values"]]

        if len(set(mapped_values)) != len(request.json["mapped_values"]):
            return error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymCreationFailed",
                "One or more mapped values were repeated.",
            )

        try:
            created = data_service.create_entity_synonym(
                project_id, synonym_name, mapped_values
            )
        except ValueError as e:
            logger.error(e)
            return error(HTTPStatus.BAD_REQUEST, "EntitySynonymCreationFailed", str(e))

        if created:
            return response.json(
                data_service.get_entity_synonym(project_id, created.id),
                status=HTTPStatus.CREATED,
            )
        else:
            return error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymCreationFailed",
                "An entity synonym with that value already exists.",
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>",
        methods=["GET", "HEAD", "OPTIONS"],
    )
    @rasa_x_scoped("entity_synonyms.get", allow_api_token=True)
    async def get_entity_synonym(
        request: Request, project_id: Text, synonym_id: int
    ) -> HTTPResponse:
        """Get a specific entity synonym and its mapped values."""

        data_service = _data_service(request)
        entity_synonym = data_service.get_entity_synonym(project_id, synonym_id)
        if entity_synonym:
            return response.json(entity_synonym)
        else:
            return error(
                HTTPStatus.NOT_FOUND,
                "GettingEntitySynonymFailed",
                f"Could not find entity synonym for ID '{synonym_id}'.",
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>", methods=["POST"]
    )
    @rasa_x_scoped("entity_synonym_values.create", allow_api_token=True)
    @validate_schema("entity_synonym_values")
    async def create_entity_synonym_mapped_values(
        request: Request, project_id: Text, synonym_id: int
    ) -> HTTPResponse:
        """Map new values to an existing entity synonym."""

        mapped_values = [item["value"] for item in request.json["mapped_values"]]

        try:
            created = _data_service(request).add_entity_synonym_mapped_values(
                project_id, synonym_id, mapped_values
            )
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.BAD_REQUEST, "EntitySynonymValuesCreationFailed", str(e)
            )

        if created is not None:
            return response.json(created, status=HTTPStatus.CREATED)
        else:
            return error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymValuesCreationFailed",
                "One or more mapped values already existed.",
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>", methods=["PUT"]
    )
    @rasa_x_scoped("entity_synonyms.update", allow_api_token=True)
    @validate_schema("entity_synonym_name")
    async def update_entity_synonym(
        request: Request, project_id: Text, synonym_id: int
    ) -> HTTPResponse:
        """Modify the text value (name) of an entity synonym."""

        data_service = _data_service(request)

        try:
            updated = data_service.update_entity_synonym(
                project_id, synonym_id, request.json["synonym_reference"]
            )
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.NOT_FOUND,
                "EntitySynonymUpdateFailed",
                "Could not find entity synonym.",
            )

        if updated:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymUpdateFailed",
                "An EntitySynonym with that value already exists.",
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>", methods=["DELETE"]
    )
    @rasa_x_scoped("entity_synonyms.delete", allow_api_token=True)
    async def delete_entity_synonym(
        request: Request, project_id: Text, synonym_id: int
    ) -> HTTPResponse:
        """Delete an entity synonym."""

        deleted = _data_service(request).delete_entity_synonym(project_id, synonym_id)

        if deleted:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return error(
                HTTPStatus.NOT_FOUND,
                "EntitySynonymDeletionFailed",
                "Could not find entity synonym.",
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>/<mapping_id:int>",
        methods=["DELETE", "OPTIONS"],
    )
    @rasa_x_scoped("entity_synonym_values.delete", allow_api_token=True)
    async def delete_entity_synonym_mapped_value(
        request: Request, project_id: Text, synonym_id: int, mapping_id: int
    ) -> HTTPResponse:
        """Delete an entity synonym mapped value."""

        data_service = _data_service(request)
        try:
            deleted = data_service.delete_entity_synonym_mapped_value(
                project_id, synonym_id, mapping_id
            )
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.NOT_FOUND,
                "EntitySynonymValueDeletionFailed",
                "Could not find entity synonym.",
            )

        if deleted:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return error(
                HTTPStatus.NOT_FOUND,
                "EntitySynonymValueDeletionFailed",
                "Could not find entity synonym mapped value.",
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/synonyms/<synonym_id:int>/<mapping_id:int>",
        methods=["PUT"],
    )
    @rasa_x_scoped("entity_synonym_values.update", allow_api_token=True)
    @validate_schema("entity_synonym_value")
    async def update_entity_synonym_mapped_value(
        request: Request, project_id: Text, synonym_id: int, mapping_id: int
    ) -> HTTPResponse:
        """Modify the text value of an existing value mapped to an entity synonym."""

        data_service = _data_service(request)

        try:
            updated = data_service.update_entity_synonym_mapped_value(
                project_id, synonym_id, mapping_id, request.json["value"]
            )
        except ValueError as e:
            logger.error(e)
            return error(
                HTTPStatus.BAD_REQUEST, "EntitySynonymValueUpdateFailed", str(e)
            )

        if updated:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return error(
                HTTPStatus.BAD_REQUEST,
                "EntitySynonymValueUpdateFailed",
                "Another mapped value with that text value already exists.",
            )

    # noinspection PyUnusedLocal
    @nlu_endpoints.route(
        "/projects/<project_id>/entities", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("entities.list")
    @inject_rasa_x_user()
    async def get_entities(
        request: Request, project_id: Text, user: Optional[Dict] = None
    ) -> HTTPResponse:
        """Fetches a list of unique entities present in training data."""

        entities = _data_service(request).get_entities(project_id)
        return response.json(entities, headers={"X-Total-Count": len(entities)})

    @nlu_endpoints.route(
        "/projects/<project_id>/evaluations", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("models.evaluations.list", allow_api_token=True)
    async def evaluations(request: Request, project_id: Text) -> HTTPResponse:
        """Fetches a list of Rasa NLU evaluations."""

        evaluation_service = _evaluation_service(request, project_id, "worker")
        results = evaluation_service.formatted_evaluations(project_id)
        return response.json(results, headers={"X-Total-Count": len(results)})

    @nlu_endpoints.route("/projects/<project_id>/evaluations/<model>", methods=["PUT"])
    @rasa_x_scoped("models.evaluations.update", allow_api_token=True)
    async def put_evaluation(
        request: Request, project_id: Text, model: Text
    ) -> HTTPResponse:
        data = _data_service(request).get_training_data(project_id).result
        model_service = _model_service(request)
        model_object = model_service.get_model_by_name(project_id, model)
        if not model_object:
            return error(
                HTTPStatus.BAD_REQUEST,
                "NluEvaluationFailed",
                f"Could not find requested model '{model}'.",
            )

        evaluation_service = _evaluation_service(request, project_id, "worker")

        try:
            content = await evaluation_service.evaluate(data, model)
            evaluation = evaluation_service.persist_evaluation(
                project_id, model, content
            )
            return response.json(evaluation.as_dict())
        except ClientError as e:
            return error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "NluEvaluationFailed",
                f"Failed to create evaluation for model '{model}'.",
                details=e,
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/evaluations/<model>", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("models.evaluations.get", allow_api_token=True)
    async def get_evaluation(
        request: Request, project_id: Text, model: Text
    ) -> HTTPResponse:
        evaluation_service = _evaluation_service(request, project_id, "worker")
        evaluation = evaluation_service.evaluation_for_model(project_id, model)
        if evaluation:
            return response.json(evaluation)
        else:
            return error(
                HTTPStatus.NOT_FOUND,
                "NluEvaluationNotFound",
                "Could not find evaluation. "
                "A PUT to this endpoint will create an evaluation.",
            )

    @nlu_endpoints.route(
        "/projects/<project_id>/evaluations/<model>", methods=["DELETE"]
    )
    @rasa_x_scoped("models.evaluations.delete", allow_api_token=True)
    async def delete_evaluation(
        request: Request, project_id: Text, model: Text
    ) -> HTTPResponse:
        evaluation_service = _evaluation_service(request, project_id, "worker")
        delete = evaluation_service.delete_evaluation(project_id, model)
        if delete:
            return response.text("", HTTPStatus.NO_CONTENT)
        else:
            return error(
                HTTPStatus.NOT_FOUND,
                "NluEvaluationNotFound",
                "Could not find evaluation. "
                "A PUT to this endpoint will create an evaluation.",
            )

    return nlu_endpoints
