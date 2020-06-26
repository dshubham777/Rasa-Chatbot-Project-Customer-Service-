from typing import Dict, Text, Any

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from rasax.community.database.base import Base
from rasax.community.database import utils


class Intent(Base):
    """Stores the intents (currently only temporary ones)."""

    __tablename__ = "intent"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    name = sa.Column(sa.String)
    mapped_to = sa.Column(sa.String)
    is_temporary = sa.Column(sa.Boolean, default=True)
    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))
    temporary_examples = relationship(
        "TemporaryIntentExample",
        cascade="all, delete-orphan",
        back_populates="temporary_intent",
    )

    def as_dict(self, include_example_hashes: bool = True) -> Dict[Text, Any]:
        intent = {
            "intent": self.name,
            "is_temporary": self.is_temporary,
            "mapped_to": self.mapped_to,
        }

        if include_example_hashes:
            intent["example_hashes"] = [t.example_hash for t in self.temporary_examples]
        return intent


class TemporaryIntentExample(Base):
    """Stores which training examples belong to mapped temporary intents."""

    __tablename__ = "temporary_intent_example"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    intent_id = sa.Column(sa.Integer, sa.ForeignKey("intent.id"))
    temporary_intent = relationship("Intent", back_populates="temporary_examples")
    example_hash = sa.Column(sa.String)


class UserGoal(Base):
    """Stores the user goals which intents can belong to."""

    __tablename__ = "user_goal"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    name = sa.Column(sa.String)
    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))

    intents = relationship(
        "UserGoalIntent", cascade="all, delete-orphan", back_populates="user_goal"
    )

    def as_dict(self) -> Dict[Text, Any]:
        return {"name": self.name, "intents": [i.intent_name for i in self.intents]}


class UserGoalIntent(Base):
    """Stores which intents belong to which user goal."""

    __tablename__ = "user_goal_intent"

    user_goal_id = sa.Column(
        sa.Integer, sa.ForeignKey("user_goal.id"), primary_key=True
    )
    intent_name = sa.Column(sa.String, primary_key=True)

    user_goal = relationship("UserGoal", back_populates="intents")
