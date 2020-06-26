import itertools
import json
import logging
import time
from typing import Text, List, Dict, Any, Optional, Tuple, Set

from sqlalchemy import or_, and_, true, false

from rasa.core.constants import UTTER_PREFIX
from rasa.utils.common import raise_warning
from rasax.community import config
from rasax.community.constants import RESPONSE_NAME_KEY
from rasax.community.database.data import Response
from rasax.community.database.service import DbService
from rasax.community.utils import (
    get_columns_from_fields,
    get_query_selectors,
    query_result_to_dict,
    QueryResult,
)

logger = logging.getLogger(__name__)


def _fix_response_name_key(response: Dict[Text, Any]) -> None:
    if "template" in response:
        response[RESPONSE_NAME_KEY] = response.pop("template")
        raise_warning(
            f"The response you provided includes the key 'template'. This key has been "
            f"deprecated and renamed to '{RESPONSE_NAME_KEY}'. The 'template' key will "
            f"no longer work in future versions of Rasa X. Please use "
            f"'{RESPONSE_NAME_KEY}' instead.",
            FutureWarning,
        )


class NlgService(DbService):
    def save_response(
        self,
        response: Dict[Text, Any],
        username: Optional[Text] = None,
        domain_id: Optional[int] = None,
        project_id: Text = config.project_name,
    ) -> Dict[Text, Any]:
        """Save response.

        Args:
            response: Response object to save.
            username: Username performing the save operation.
            domain_id: Domain associated with the response.
            project_id: Project ID associated with the response.

        Raises:
            `ValueError` if any of:
            1. no domain ID is specified
            2. the response name does not start with `rasa.core.constants.UTTER_PREFIX`
            3. a response with the same text already exists

        Returns:
            The saved response object.
        """
        response = response.copy()

        if domain_id is None:
            raise ValueError("Response could not be saved since domain ID is `None`.")

        _fix_response_name_key(response)

        response_name = Response.get_stripped_value(response, RESPONSE_NAME_KEY)

        if not response_name or not response_name.startswith(UTTER_PREFIX):
            raise AttributeError(
                f"Failed to save response. Response '{response_name}' does "
                f"not begin with '{UTTER_PREFIX}' prefix."
            )

        response[RESPONSE_NAME_KEY] = response_name
        response["text"] = Response.get_stripped_value(response, "text")

        if self.get_response(project_id, response):
            raise ValueError(
                f"Another response with same pair of ({RESPONSE_NAME_KEY}, text) "
                f"(({response_name},{response['text']})) already exists for this "
                f"project and domain."
            )

        if not username:
            username = config.default_username

        new_response = Response(
            response_name=response[RESPONSE_NAME_KEY],
            text=response["text"],
            content=json.dumps(response),
            annotated_at=time.time(),
            annotator_id=username,
            project_id=project_id,
        )

        if domain_id:
            new_response.domain_id = domain_id

        self.add(new_response)

        # flush so ID becomes available
        self.flush()

        return new_response.as_dict()

    def fetch_responses(
        self,
        text_query: Optional[Text] = None,
        response_query: Optional[Text] = None,
        fields_query: List[Tuple[Text, bool]] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        sort_by_response_name: bool = False,
        intersect_filters: bool = False,
    ) -> QueryResult:
        """Returns a list of responses. Each response includes its response name
        as a property under RESPONSE_NAME.

        An example of a response item could be:
        ```
        {
            "text": "Hey! How are you?",
            RESPONSE_NAME: "utter_greet",
            "id": 6,
            "annotator_id": "me",
            "annotated_at": 1567780833.8198001,
            "project_id": "default",
            "edited_since_last_training": false
        }
        ```

        Args:
            text_query: Filter results by response text (case insensitive).
            response_query: Filter results by response name.
            fields_query: Fields to include per item returned.
            limit: Maximum number of responses to be retrieved.
            offset: Excludes the first ``offset`` responses from the result.
            sort_by_response_name: If ``True``, sort responses by their
                RESPONSE_NAME property.
            intersect_filters: If ``True``, join ``text_query`` and
                ``response_query`` conditions with an AND instead of an OR.

        Returns:
            List of responses with total number of responses found.
        """

        responses_to_query = response_query.split(",") if response_query else []
        columns = get_columns_from_fields(fields_query)

        if not responses_to_query and not text_query:
            # Case 1: No conditions were specified - base query is TRUE (all results)
            query = true()
        else:
            if intersect_filters:
                # Case 2: One or both conditions were specified, and caller wants
                #         to join them with AND.
                query = true()
            else:
                # Case 3: One or both conditions were specified, and caller wants
                #         to join them with OR.
                query = false()

        query_joiner = and_ if intersect_filters else or_

        if text_query:
            query = query_joiner(query, Response.text.ilike(f"%{text_query}%"))

        if response_query:
            query = query_joiner(query, Response.response_name.in_(responses_to_query))

        responses = self.query(*get_query_selectors(Response, columns)).filter(query)

        if sort_by_response_name:
            responses = responses.order_by(Response.response_name.asc())

        total_number_of_results = responses.count()

        responses = responses.offset(offset).limit(limit).all()

        if columns:
            results = [query_result_to_dict(r, fields_query) for r in responses]
        else:
            results = [t.as_dict() for t in responses]

        return QueryResult(results, total_number_of_results)

    def get_grouped_responses(
        self, text_query: Optional[Text] = None, response_query: Optional[Text] = None
    ) -> QueryResult:
        """Return responses grouped by their response name.

        Args:
            text_query: Filter responses by response text (case insensitive).
            response_query: Filter response groups by response name.

        Returns:
            `QueryResult` containing grouped responses and total number of responses
            across all groups.
        """
        # Sort since `groupby` only groups consecutive entries
        responses = self.fetch_responses(
            text_query=text_query,
            response_query=response_query,
            sort_by_response_name=True,
            intersect_filters=True,
        ).result

        grouped_responses = itertools.groupby(
            responses, key=lambda each: each[RESPONSE_NAME_KEY]
        )

        result = [
            {RESPONSE_NAME_KEY: k, "responses": list(g)} for k, g in grouped_responses
        ]
        count = sum(len(item["responses"]) for item in result)

        return QueryResult(result, count)

    def fetch_all_response_names(self) -> Set[Text]:
        """Fetch a list of all response names in db."""

        return {t[RESPONSE_NAME_KEY] for t in self.fetch_responses()[0]}

    def delete_response(self, _id: int) -> bool:
        delete_result = self.query(Response).filter(Response.id == _id).delete()

        return delete_result

    def delete_all_responses(self) -> None:
        self.query(Response).delete()

    def update_response(
        self, _id: int, response: Dict[Text, Any], username: Text
    ) -> Optional[Response]:
        response = response.copy()

        old_response = self.query(Response).filter(Response.id == _id).first()
        if not old_response:
            raise KeyError(f"Could not find existing response with ID '{_id}'.")

        _fix_response_name_key(response)

        response_name = Response.get_stripped_value(response, RESPONSE_NAME_KEY)

        if not response_name or not response_name.startswith(UTTER_PREFIX):
            raise AttributeError(
                f"Failed to update response. Response '{response_name}' does "
                f"not begin with '{UTTER_PREFIX}' prefix."
            )

        response[RESPONSE_NAME_KEY] = response_name
        response["text"] = Response.get_stripped_value(response, "text")

        if self.get_response(old_response.project_id, response):
            raise ValueError(
                f"Response could not be saved since another one with same pair of "
                f"({RESPONSE_NAME_KEY}, text) exists."
            )

        old_response.response_name = response[RESPONSE_NAME_KEY]
        old_response.text = response["text"]
        old_response.annotated_at = time.time()
        old_response.content = json.dumps(response)
        old_response.annotator_id = username
        old_response.edited_since_last_training = True

        # because it's the same as the updated response
        return old_response

    def replace_responses(
        self,
        new_responses: List[Dict[Text, Any]],
        username: Optional[Text] = None,
        domain_id: Optional[int] = None,
    ) -> int:
        """Deletes all responses and adds new responses.

        Returns the number of inserted responses.
        """
        self.delete_all_responses()
        insertions = 0
        for response in new_responses:
            try:
                inserted = self.save_response(response, username, domain_id=domain_id)
                if inserted:
                    insertions += 1
            except ValueError:
                pass

        return insertions

    def get_response(
        self, project_id: Text, response: Dict[Text, Any]
    ) -> Optional[Response]:
        """Return a response that has the specified `project_id` and `response`.

        Args:
            project_id: Project ID.
            response: Response.
        """
        _fix_response_name_key(response)

        response_content = json.dumps(response)

        return (
            self.query(Response)
            .filter(
                and_(
                    Response.project_id == project_id,
                    Response.content.like(f"%{response_content}%"),
                )
            )
            .first()
        )

    def mark_responses_as_used(
        self, training_start_time: float, project_id: Text = config.project_name
    ) -> None:
        """Unset the `edited_since_last_training_flag` for responses which were included
        in the last training.

        Args:
            training_start_time: annotation time until responses should be marked as
                used in training.
            project_id: Project which was trained.

        """
        responses_which_were_used_in_training = (
            self.query(Response)
            .filter(
                and_(
                    Response.annotated_at <= training_start_time,
                    Response.edited_since_last_training,
                    Response.project_id == project_id,
                )
            )
            .all()
        )
        for response in responses_which_were_used_in_training:
            response.edited_since_last_training = False

    def rename_responses(
        self, old_response_name: Text, response: Dict[Text, Text], annotator: Text
    ) -> None:
        """
        Bulk-rename all responses with this response name.

        Args:
            old_response_name: The name of the response which should be renamed.
            response: The object containing the new response values.
            annotator: The name of the user who is doing the rename.
        """
        responses = (
            self.query(Response)
            .filter(Response.response_name == old_response_name)
            .all()
        )
        new_response_name = response["name"]
        for response in responses:
            response.response_name = new_response_name
            response.annotated_at = time.time()
            response.annotator_id = annotator
