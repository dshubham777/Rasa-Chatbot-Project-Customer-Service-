import logging
from typing import Text, Dict, List, Union, Optional

from sqlalchemy import and_

from rasax.community.database.intent import UserGoal, UserGoalIntent
from rasax.community.database.service import DbService

logger = logging.getLogger(__name__)

GOAL_NAME_KEY = "name"
GOAL_INTENTS_KEY = "intents"


class UserGoalService(DbService):
    def get_user_goals(
        self, project_id: Text
    ) -> List[Dict[str, Union[str, List[str]]]]:
        """Returns all user goals.

        Args:
            project_id: id of the project the user goal belongs to.

        Returns:
            List of user goal objects including the related intents.
        """

        goals = self.query(UserGoal).filter(UserGoal.project_id == project_id).all()

        return [g.as_dict() for g in goals]

    def create_user_goal(self, user_goal: Text, project_id: Text):
        """Creates a new user goal.

        Args:
            user_goal: Name of the user goal.
            project_id: id of the project the user goal belongs to.
        """

        goal = UserGoal(name=user_goal, project_id=project_id)
        self.add(goal)

    def add_intent_to_user_goal(self, user_goal: Text, intent: Text, project_id: Text):
        """Adds an intent to a user goal.

        Args:
            user_goal: Name of the user goal.
            intent: Name of the intent which belongs to the user goal.
            project_id: id of the project the user goal belongs to.
        """

        user_goal = self._get_user_goal(user_goal, project_id)

        if not user_goal:
            raise ValueError(
                f"User goal '{user_goal}' not found. Cannot add intent '{intent}' to this goal."
            )

        goal = UserGoalIntent(intent_name=intent, user_goal_id=user_goal.id)
        self.add(goal)

    def _get_user_goal(self, user_goal: Text, project_id: Text) -> Optional[UserGoal]:
        return (
            self.query(UserGoal)
            .filter(and_(UserGoal.name == user_goal, UserGoal.project_id == project_id))
            .first()
        )

    def remove_intent_from_user_goal(
        self, user_goal: Text, intent: Text, project_id: Text
    ):
        """Removes an intent from a user goal.

        Args:
            user_goal: Name of the user goal.
            intent: Name of the intent which should be removed from the goal.
            project_id: id of the project the user goal belongs to.
        """

        user_goal = self._get_user_goal(user_goal, project_id)
        if not user_goal:
            raise ValueError(
                f"User goal '{user_goal}' not found. Cannot remove intent '{intent}' from this goal."
            )

        self.query(UserGoalIntent).filter(
            and_(
                UserGoalIntent.user_goal_id == user_goal.id,
                UserGoalIntent.intent_name == intent,
            )
        ).delete()

    def delete_user_goal(self, user_goal: Text, project_id: Text):
        """Deletes a user goal.
        Args:
            user_goal: Name of the user goal which should be deleted.
            project_id: id of the project which the user goal belongs to.
        """

        to_delete = (
            self.query(UserGoal)
            .filter(and_(UserGoal.name == user_goal, UserGoal.project_id == project_id))
            .all()
        )
        self.delete_all(to_delete)
