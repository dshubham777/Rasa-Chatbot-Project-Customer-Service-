import json
import os
from pathlib import Path
from typing import Dict, Text, Any, Optional

import sqlalchemy as sa
from rasax.community import config
from sqlalchemy import Column
from sqlalchemy.orm import relationship, deferred

from rasax.community.constants import RESPONSE_NAME_KEY
from rasax.community.database import utils
from rasax.community.database.base import Base

# keys that are used for response annotations
# these keys are removed from the response dictionary when dumped as part of a
# domain file
RESPONSE_ANNOTATION_KEYS = {
    RESPONSE_NAME_KEY,
    "project_id",
    "annotator_id",
    "id",
    "annotated_at",
}


class Response(Base):
    """Stores the responses."""

    __tablename__ = "response"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    response_name = sa.Column(sa.String, nullable=False)
    text = sa.Column(sa.Text)
    content = sa.Column(sa.Text)
    annotator_id = sa.Column(sa.String, sa.ForeignKey("rasa_x_user.username"))
    annotated_at = sa.Column(sa.Float)  # annotation time as unix timestamp
    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))
    edited_since_last_training = sa.Column(sa.Boolean, default=True)

    domain = relationship("Domain", back_populates="responses")
    domain_id = sa.Column(sa.Integer, sa.ForeignKey("domain.id"))

    def as_dict(self) -> Dict[Text, Any]:
        result = json.loads(self.content)
        result[RESPONSE_NAME_KEY] = self.response_name
        result["id"] = self.id
        result["annotator_id"] = self.annotator_id
        result["annotated_at"] = self.annotated_at
        result["project_id"] = self.project_id
        result["edited_since_last_training"] = self.edited_since_last_training

        return result

    @staticmethod
    def get_stripped_value(response: Dict[Text, Any], key: Text) -> Optional[Text]:
        """Get value by `key` from a `response` dictionary.

        Args:
            response: Response to strip.
            key: A key from dictionary to get.

        Returns:
            The stripped value, if it is a string. Otherwise None.
        """
        v = response.get(key)
        return v.strip() if isinstance(v, str) else None


class Story(Base):
    """Stores Rasa Core training stories."""

    __tablename__ = "story"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    name = sa.Column(sa.String)
    story = sa.Column(sa.Text)
    user = sa.Column(sa.String, sa.ForeignKey("rasa_x_user.username"))
    annotated_at = sa.Column(sa.Float)  # annotation time as unix timestamp
    filename = sa.Column(sa.String)

    def as_dict(self) -> Dict[Text, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "story": self.story,
            "annotation": {"user": self.user, "time": self.annotated_at},
            "filename": self.filename,
        }


class TrainingData(Base):
    """Stores the annotated NLU training data."""

    __tablename__ = "nlu_training_data"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    hash = sa.Column(sa.String, index=True)
    text = sa.Column(sa.Text)
    intent = sa.Column(sa.String)
    annotator_id = sa.Column(sa.String, sa.ForeignKey("rasa_x_user.username"))
    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))
    annotated_at = sa.Column(sa.Float)  # annotation time as unix timestamp
    filename = sa.Column(sa.String)
    entities = relationship(
        "TrainingDataEntity",
        lazy="joined",
        cascade="all, delete-orphan",
        back_populates="example",
    )

    def as_dict(self) -> Dict[Text, Any]:
        return {
            "id": self.id,
            "text": self.text,
            "intent": self.intent,
            "entities": [e.as_dict() for e in self.entities],
            "hash": self.hash,
            "annotation": {"user": self.annotator_id, "time": self.annotated_at},
        }


class TrainingDataEntity(Base):
    """Stores annotated entities of the NLU training data."""

    __tablename__ = "nlu_training_data_entity"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    example_id = sa.Column(sa.Integer, sa.ForeignKey("nlu_training_data.id"))
    example = relationship("TrainingData", back_populates="entities")
    entity = sa.Column(sa.String)
    value = sa.Column(sa.String)
    original_value = sa.Column(sa.String, index=True)
    start = sa.Column(sa.Integer)
    end = sa.Column(sa.Integer)
    extractor = sa.Column(sa.String)
    entity_synonym_id = sa.Column(
        sa.Integer, sa.ForeignKey("entity_synonym.id", ondelete="SET NULL")
    )

    def as_dict(self) -> Dict[Text, Text]:
        entity_dict = {
            "start": self.start,
            "end": self.end,
            "entity": self.entity,
            "value": self.value,
            "entity_synonym_id": self.entity_synonym_id,
        }

        if self.extractor:
            entity_dict["extractor"] = self.extractor

        return entity_dict


class RegexFeature(Base):
    """Stores annotated regex features of the NLU training data."""

    __tablename__ = "regex_feature"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))
    name = sa.Column(sa.String)
    pattern = sa.Column(sa.String)
    filename = sa.Column(sa.String)

    def as_dict(self) -> Dict[Text, Text]:
        return {"id": self.id, "name": self.name, "pattern": self.pattern}


class LookupTable(Base):
    """Stores annotated lookup tables of the NLU training data."""

    __tablename__ = "lookup_table"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))
    name = sa.Column(sa.String)
    number_of_elements = Column(sa.Integer)
    # Load content only if it's actually accessed
    elements = deferred(Column(sa.Text))
    referencing_nlu_file = sa.Column("filename", sa.String)

    def as_dict(self, should_include_filename: bool = False) -> Dict[Text, Text]:
        """Returns a JSON-like representation of this LookupTable object.

        Args:
            should_include_filename: If `True`, also include a `filename` property with
                the lookup table's filename in the result.

        Returns:
            Dict containing the LookupTable's attributes.
        """

        value = {
            "id": self.id,
            "name": os.path.basename(self.name),
            # If users download the training data, we don't export the actual elements
            # of the lookup table, but merely include a link to the file with the
            # elements
            "elements": self.relative_file_path,
            "number_of_elements": self.number_of_elements,
        }

        if should_include_filename:
            value["filename"] = self.relative_file_path

        return value

    @property
    def relative_file_path(self) -> Text:
        # If we just have a file name (and not a path), we assume it's in the default
        # data directory
        if os.path.basename(self.name) == self.name:
            return str(Path(config.data_dir) / self.name)
        else:
            return self.name

    @property
    def absolute_file_path(self) -> Text:
        from rasax.community import utils as rasax_utils

        return str(rasax_utils.get_project_directory() / self.relative_file_path)


class EntitySynonym(Base):
    """Stores annotated entity synonyms of the NLU training data."""

    __tablename__ = "entity_synonym"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    name = sa.Column(sa.String)
    synonym_values = relationship(
        "EntitySynonymValue",
        cascade="all, delete-orphan",
        order_by=lambda: EntitySynonymValue.id.asc(),
        back_populates="entity_synonym",
    )
    project_id = sa.Column(sa.String, sa.ForeignKey("project.project_id"))
    filename = sa.Column(sa.String)

    def as_dict(
        self, value_use_counts: Optional[Dict[Text, int]] = None
    ) -> Dict[Text, Any]:
        """Returns a JSON-like representation of this EntitySynonym object.

        Args:
            value_use_counts: Dictionary containing the number of times each value
                mapped to this entity synonym is used in the NLU training data.

        Returns:
            Dict containing the EntitySynonym's attributes.
        """

        serialized = {"id": self.id, "synonym_reference": self.name}
        if value_use_counts:
            serialized["mapped_values"] = [
                value.as_dict(value_use_counts[value.id])
                for value in self.synonym_values
            ]
        return serialized


class EntitySynonymValue(Base):
    """Stores values mapped to entity synonyms. This mapping (relationship) is
    what effectively creates a synonym (i.e. one term can be replaced for another)."""

    __tablename__ = "entity_synonym_value"

    id = sa.Column(sa.Integer, utils.create_sequence(__tablename__), primary_key=True)
    entity_synonym_id = sa.Column(sa.Integer, sa.ForeignKey("entity_synonym.id"))
    entity_synonym = relationship("EntitySynonym", back_populates="synonym_values")
    name = sa.Column(sa.String, index=True)

    def as_dict(self, use_count: int) -> Dict[Text, Any]:
        """Returns a JSON-like representation of this EntitySynonymValue object.

        Args:
            use_count: Number of times this particular value appears in the NLU
                training data.

        Returns:
            Dict containing the EntitySynonymValue's attributes.
        """

        return {"value": self.name, "id": self.id, "nlu_examples_count": use_count}
