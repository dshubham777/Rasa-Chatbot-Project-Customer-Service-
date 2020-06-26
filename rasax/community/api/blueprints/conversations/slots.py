import logging

from rasax.community.api.decorators import rasa_x_scoped
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
    slots_endpoints = Blueprint("conversation_slots_endpoints")

    @slots_endpoints.route(
        "/conversations/slotNames", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("conversationSlotNames.list", allow_api_token=True)
    async def unique_slot_names(request: Request) -> HTTPResponse:
        """Return a list of unique slot names found in existing conversations.

        Args:
            request: HTTP request being processed.

        Returns:
            HTTP response.
        """
        slot_names = _event_service(request).get_unique_slot_names()
        return response.json(slot_names, headers={"X-Total-Count": len(slot_names)})

    @slots_endpoints.route(
        "/conversations/slotValues", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("conversationSlotValues.list", allow_api_token=True)
    async def unique_slot_values(request: Request) -> HTTPResponse:
        """Return a list of unique slot values found in existing conversations,
        according to certain filters.

        Args:
            request: HTTP request being processed.

        Returns:
            HTTP response.
        """
        query = rasa_x_utils.default_arg(request, "q")
        slot = rasa_x_utils.default_arg(request, "slot")
        slot_values = _event_service(request).get_unique_slot_values(slot, query)
        return response.json(slot_values, headers={"X-Total-Count": len(slot_values)})

    return slots_endpoints
