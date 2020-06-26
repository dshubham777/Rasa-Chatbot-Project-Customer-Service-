from typing import Any, Text, Dict, Union

import sqlalchemy as sa
from sqlalchemy.orm import relationship

from rasax.community.database.base import Base
from rasax.community.database import utils


class Model(Base):
    """Stores metadata about trained models."""

    __tablename__ = "model"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    hash = sa.Column(sa.String, nullable=False)
    name = sa.Column(sa.String, nullable=False, unique=True)
    path = sa.Column(sa.String, nullable=False, unique=True)
    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))
    project = relationship("Project", back_populates="models")
    version = sa.Column(sa.String)
    # time when the training was performed as unix timestamp
    trained_at = sa.Column(sa.Float)
    tags = relationship(
        "ModelTag", cascade="all, delete-orphan", back_populates="model"
    )
    nlu_evaluations = relationship(
        "NluEvaluation", back_populates="model", cascade="all, delete-orphan"
    )
    # no `cascade="delete"` so that the message logs don't get removed when the models
    # gets removed
    message_logs = relationship("MessageLog", back_populates="model")

    def as_dict(self) -> Dict[Text, Any]:
        return {
            "hash": self.hash,
            "model": self.name,
            "path": self.path,
            "project": self.project_id,
            "trained_at": self.trained_at,
            "version": self.version,
            "tags": [t.tag for t in self.tags],
        }


class ModelTag(Base):
    """Stores tags which have been assigned to certain models."""

    __tablename__ = "model_tag"

    model_id = sa.Column(sa.Integer, sa.ForeignKey("model.id"), primary_key=True)
    tag = sa.Column(sa.String, primary_key=True)
    model = relationship("Model", back_populates="tags")


class NluEvaluation(Base):
    """Stores the results of NLU evaluations."""

    __tablename__ = "nlu_evaluation"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    model_id = sa.Column(sa.String, sa.ForeignKey("model.name"))
    model = relationship("Model", back_populates="nlu_evaluations")
    report = sa.Column(sa.Text)
    precision = sa.Column(sa.Float)
    f1 = sa.Column(sa.Float)
    accuracy = sa.Column(sa.Float)
    timestamp = sa.Column(sa.Float)  # time of the evaluation as unix timestamp
    predictions = relationship(
        "NluEvaluationPrediction", cascade="all", back_populates="evaluation"
    )

    def as_dict(self) -> Dict[Text, Dict[Text, Any]]:
        return {
            "intent_evaluation": {
                "report": self.report,
                "f1_score": self.f1,
                "accuracy": self.accuracy,
                "precision": self.precision,
                "predictions": [p.as_dict() for p in self.predictions],
                "timestamp": self.timestamp,
            },
            "model": self.model_id,
        }


class NluEvaluationPrediction(Base):
    """Stores the predictions which were done as part of the NLU evaluation."""

    __tablename__ = "nlu_evaluation_prediction"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    evaluation_id = sa.Column(sa.Integer, sa.ForeignKey("nlu_evaluation.id"))
    evaluation = relationship("NluEvaluation", back_populates="predictions")
    text = sa.Column(sa.String)
    intent = sa.Column(sa.String)
    predicted = sa.Column(sa.String)
    confidence = sa.Column(sa.Float)

    def as_dict(self) -> Dict[Text, Union[Text, sa.Float]]:
        return {
            "text": self.text,
            "intent": self.intent,
            "predicted": self.predicted,
            "confidence": self.confidence,
        }
