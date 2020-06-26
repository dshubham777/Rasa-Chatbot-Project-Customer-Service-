from rasax.community.api.decorators import rasa_x_scoped
from rasax.community.constants import REQUEST_DB_SESSION_KEY
from rasax.community.services.event_service import EventService
from sanic import Blueprint, response
from sanic.request import Request
from sanic.response import HTTPResponse


def _event_service(request: Request) -> EventService:
    return EventService(request[REQUEST_DB_SESSION_KEY])


def blueprint() -> Blueprint:
    channel_endpoints = Blueprint("conversation_channels_endpoints")

    @channel_endpoints.route(
        "/conversations/inputChannels", methods=["GET", "HEAD", "OPTIONS"]
    )
    @rasa_x_scoped("conversationInputChannels.list")
    async def list_input_channels(request: Request) -> HTTPResponse:
        """Return a list of all input channels used in stored conversations.

        Args:
            request: Incoming HTTP request.

        Returns:
            JSON response with list of conversation input channels.
        """
        channels = _event_service(request).get_unique_input_channels()
        return response.json(channels, headers={"X-Total-Count": len(channels)})

    return channel_endpoints
