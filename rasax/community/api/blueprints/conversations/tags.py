import logging
from http import HTTPStatus
from typing import Text

from rasax.community.api.decorators import (
    rasa_x_scoped,
    validate_schema,
)
from rasax.community import telemetry
from rasax.community.constants import REQUEST_DB_SESSION_KEY
from rasax.community.services.event_service import EventService
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse

import rasax.community.utils as rasa_x_utils

logger = logging.getLogger(__name__)


def _event_service(request: Request) -> EventService:
    return EventService(request[REQUEST_DB_SESSION_KEY])


def blueprint() -> Blueprint:
    tags_endpoints = Blueprint("conversation_tags_endpoints")

    @tags_endpoints.route(
        "/conversations/<conversation_id:string>/tags", methods=["POST", "OPTIONS"]
    )
    @validate_schema("conversation_tags")
    async def add_conversation_tags(
        request: Request, conversation_id: Text
    ) -> HTTPResponse:
        """Assign tags to <conversation_id>. If tags don't exist, they will be created.

        Args:
            request: Incoming HTTP request.
            conversation_id: Id of conversation to assign tags to.

        Returns:
            List of assigned tags in format of JSON schema `conversation_tags`.
        """

        try:
            tags = _event_service(request).add_conversation_tags(
                conversation_id, request.json
            )

            telemetry.track_conversation_tagged(len(tags))

            return response.json(tags, headers={"X-Total-Count": len(tags)})
        except ValueError as e:
            return rasa_x_utils.error(
                HTTPStatus.BAD_REQUEST, "Failed to add tags to conversation", str(e)
            )

    @tags_endpoints.route(
        "/conversations/<conversation_id:string>/tags/<tag_id:int>",
        methods=["DELETE", "OPTIONS"],
    )
    @rasa_x_scoped("conversationTags.delete")
    async def delete_conversation_tag_from_conversation(
        request: Request, conversation_id: Text, tag_id: int
    ) -> HTTPResponse:
        """Remove tag with <tag_id> from <conversation_id>.

        Args:
            request: Incoming HTTP request.
            conversation_id: Id of conversation to remove tag from.
            tag_id: Id of a tag that will be removed.
        """

        try:
            _event_service(request).remove_conversation_tag(conversation_id, tag_id)

            return response.json("", status=HTTPStatus.NO_CONTENT)
        except ValueError as e:
            return rasa_x_utils.error(
                HTTPStatus.NOT_FOUND, "Failed to delete tag from conversation", str(e)
            )

    @tags_endpoints.route("/conversations/tags", methods=["GET", "OPTIONS"])
    @rasa_x_scoped("conversationTags.list")
    async def get_all_conversation_tags(request: Request) -> HTTPResponse:
        """Returns all existing conversations tags with detailed information and
        conversations IDs they're assigned to.

        Args:
            request: Incoming HTTP request.

        Returns:
            List of existing tags in format of JSON schema `conversation_tags` with
            additional field "conversations" which is a list with all conversation IDs
            the given tag is assigned to.
        """
        tags = _event_service(request).get_all_conversation_tags()
        return response.json(tags, status=HTTPStatus.OK)

    @tags_endpoints.route(
        "/conversations/<conversation_id:string>/tags", methods=["GET"]
    )
    @rasa_x_scoped("conversationTags.list")
    async def get_tags_for_conversation_id(
        request: Request, conversation_id: Text
    ) -> HTTPResponse:
        """Returns all conversations tags assigned to given conversation ID
        with detailed information and all conversations IDs they're assigned to.

        Args:
            request: Incoming HTTP request.
            conversation_id: ID of conversation to receive the tags from.

        Returns:
            List of tags attached to the given conversation ID in format of
            JSON schema `conversation_tags` with additional field "conversations"
            which is a list with all conversation IDs the given tag is assigned to.
        """
        try:
            tags = _event_service(request).get_tags_for_conversation_id(conversation_id)
            return response.json(tags, status=HTTPStatus.OK)
        except ValueError as e:
            return rasa_x_utils.error(
                HTTPStatus.NOT_FOUND,
                f"Failed to get tags. Conversation id '{conversation_id}' was not found",
                str(e),
            )

    @tags_endpoints.route(
        "/conversations/tags/<tag_id:int>", methods=["DELETE", "OPTIONS"]
    )
    @rasa_x_scoped("conversationTags.delete")
    async def delete_conversation_tag_by_id(
        request: Request, tag_id: int
    ) -> HTTPResponse:
        """Delete conversation tag with the given <tag_id>.

        Args:
            request: Incoming HTTP request.
            tag_id: ID of the tag to be deleted.
        """
        try:
            _event_service(request).delete_conversation_tag_by_id(tag_id)

            return response.json("", status=HTTPStatus.NO_CONTENT)
        except ValueError as e:
            return rasa_x_utils.error(
                HTTPStatus.NOT_FOUND,
                f"Failed to delete tag: tag with id '{tag_id}' was not found",
                str(e),
            )

    return tags_endpoints
