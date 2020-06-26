import logging

from sanic import Blueprint, response
from sanic.request import Request

from rasax.community import utils
from rasax.community.api.decorators import rasa_x_scoped, validate_schema
from rasax.community.constants import REQUEST_DB_SESSION_KEY
from rasax.community.services.intent_service import (
    IntentService,
    INTENT_NAME_KEY,
    INTENT_USER_GOAL_KEY,
    INTENT_EXAMPLES_KEY,
    INTENT_SUGGESTIONS_KEY,
)
from rasax.community.services.user_goal_service import UserGoalService
from rasax.community.utils import error

logger = logging.getLogger(__name__)


def _intent_service(request: Request) -> IntentService:
    return IntentService(request[REQUEST_DB_SESSION_KEY])


def _user_goal_service(request: Request) -> UserGoalService:
    return UserGoalService(request[REQUEST_DB_SESSION_KEY])


def blueprint() -> Blueprint:
    intent_endpoints = Blueprint("intent_endpoints")

    @intent_endpoints.route(
        "/projects/<project_id>/intents", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("intents.list")
    async def get_intents(request, project_id):
        include_temporary_intents = utils.bool_arg(request, "is_temporary", True)
        included_fields = utils.fields_arg(
            request, {INTENT_SUGGESTIONS_KEY, INTENT_USER_GOAL_KEY, INTENT_EXAMPLES_KEY}
        )

        intents = _intent_service(request).get_intents(
            project_id, include_temporary_intents, included_fields
        )

        return response.json(intents, headers={"X-Total-Count": len(intents)})

    @intent_endpoints.route("/projects/<project_id>/intents", methods=["POST"])
    @rasa_x_scoped("intents.create")
    @validate_schema("intent/new")
    async def create_intent(request, project_id):
        intent = request.json
        intent_service = _intent_service(request)
        existing_intents = intent_service.get_permanent_intents(project_id)
        if intent[INTENT_NAME_KEY] not in existing_intents:
            intent_service.add_temporary_intent(intent, project_id)

        user_goal = intent.get(INTENT_USER_GOAL_KEY)
        if user_goal:
            try:
                _user_goal_service(request).add_intent_to_user_goal(
                    user_goal, intent[INTENT_NAME_KEY], project_id
                )
            except ValueError as e:
                logger.error(e)
                return error(
                    400,
                    "AddIntentToUserGoalFailed",
                    "Failed to add intent to user goal.",
                    details=e,
                )

        return response.text(
            "Intent '{}' created.".format(intent.get(INTENT_NAME_KEY)), 201
        )

    @intent_endpoints.route(
        "/projects/<project_id>/intents/<intent_id>", methods=["PUT", "OPTIONS"]
    )
    @rasa_x_scoped("intents.update")
    @validate_schema("intent")
    async def update_intent(request, intent_id, project_id):
        intent = request.json

        intent_service = _intent_service(request)
        existing_intents = intent_service.get_permanent_intents(project_id)
        if intent[INTENT_NAME_KEY] not in existing_intents:
            intent_service.update_temporary_intent(intent_id, intent, project_id)

        user_goal = intent.get(INTENT_USER_GOAL_KEY)
        user_goal_service = _user_goal_service(request)
        try:
            if user_goal:
                user_goal_service.add_intent_to_user_goal(
                    user_goal, intent[INTENT_NAME_KEY], project_id
                )
            elif INTENT_USER_GOAL_KEY in intent.keys():
                user_goal_service.remove_intent_from_user_goal(
                    user_goal, intent[INTENT_NAME_KEY], project_id
                )
        except ValueError as e:
            logger.error(e)
            return error(
                400, "UpdateIntentFailed", "Failed to update user goal.", details=e
            )
        return response.text(f"Intent '{intent_id}' updated.", 200)

    @intent_endpoints.route(
        "/projects/<project_id>/intents/<intent_id>", methods=["DELETE"]
    )
    @rasa_x_scoped("intents.delete")
    async def delete_intent(request, intent_id, project_id):
        _intent_service(request).delete_temporary_intent(intent_id, project_id)

        return response.text(f"Temporary intent '{intent_id}' deleted.", 200)

    @intent_endpoints.route(
        "/projects/<project_id>/userGoals", methods=["POST", "OPTIONS"]
    )
    @rasa_x_scoped("userGoals.create")
    @validate_schema("user_goal")
    async def create_user_goal(request, project_id):
        user_goal = request.json

        user_goal_name = user_goal["name"]
        _user_goal_service(request).create_user_goal(user_goal_name, project_id)

        return response.text(f"User goal '{user_goal_name}' created.", 201)

    @intent_endpoints.route(
        "/projects/<project_id>/userGoals/<user_goal>", methods=["DELETE", "OPTIONS"]
    )
    @rasa_x_scoped("userGoals.delete")
    async def delete_user_goal(request, user_goal, project_id):
        _user_goal_service(request).delete_user_goal(user_goal, project_id)

        return response.text(f"User goal '{user_goal}' deleted.", 200)

    return intent_endpoints
