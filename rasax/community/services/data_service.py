import json
import logging
import os
import warnings
from collections import defaultdict
from typing import Text, Dict, List, Union, Any, Optional, Tuple, Set, Iterable

import time
from sqlalchemy import and_, func

import rasa.nlu.training_data.training_data as nlu_data
from rasa.nlu.training_data import TrainingData as NluTrainingData
from rasa.nlu.training_data.formats import MarkdownReader
from rasa.nlu.training_data.formats.rasa import RasaReader
from rasa.nlu.training_data.loading import RASA, MARKDOWN
from rasa.nlu.training_data import loading
from rasa.nlu.constants import RESPONSE_KEY_ATTRIBUTE, INTENT
import rasa.utils.io as io_utils
from rasax.community import config, utils
from rasax.community.database.data import (
    TrainingData,
    TrainingDataEntity,
    RegexFeature,
    LookupTable,
    EntitySynonym,
    EntitySynonymValue,
)
from rasax.community.database.service import DbService
from rasax.community.initialise import _read_data  # pytype: disable=pyi-error
from rasax.community.services import background_dump_service
from rasax.community.utils import get_text_hash, filter_fields_from_dict, QueryResult

logger = logging.getLogger(__name__)


def unique_dictionaries(dicts: List[Dict[Text, Any]]) -> List[Dict[Text, Any]]:
    """Given a list of dictionaries, return a list of unique dictionaries."""

    out = []
    unique_json = set()

    for d in dicts:
        dict_json = json.dumps(d)
        if dict_json not in unique_json:
            out.append(d)
            unique_json.add(dict_json)

    return out


class DataService(DbService):
    def get_training_data(
        self,
        project_id: Text,
        sort_by_descending_id: bool = False,
        text_query: Optional[Text] = None,
        intent_query: Optional[Text] = None,
        entity_query: bool = False,
        fields_query: List[Tuple[Text, bool]] = None,
        filename: Optional[Text] = None,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        distinct: bool = True,
    ) -> QueryResult:
        """Retrieves the NLU training data for a certain project.

        Args:
            project_id: The project which the data belongs to.
            sort_by_descending_id: If `True` the training data is sorted by descending
                id, if `False` it is sorted by ascending id.
            text_query: Only return training examples which contain this text.
            intent_query: Only return training examples which contain one of the
                specified intents (specify multiple intents by separating them
                with `,`).
            entity_query: Return a list of all entities in the training data.
            fields_query: Only return certain fields of the training examples.
            filename: Only return training examples from a specific file.
            limit: Return a maximum number of `limit` training examples.
            offset:  Return training examples after the `offset`th row in the database.
            distinct: If `True` exclude duplicate entries.

        Returns:
            The paginated training examples matching the given query and the total
            number of results for the given query.
        """

        if entity_query:
            entities = sorted(self.get_entities(project_id))
            return QueryResult(entities, len(entities))

        if intent_query:
            query = TrainingData.intent.in_(intent_query.split(","))
        else:
            query = True

        if text_query:
            query = and_(query, TrainingData.text.ilike(f"%{text_query}%"))

        if filename:
            query = and_(query, TrainingData.filename == filename)

        examples = (
            self.query(TrainingData)
            .filter(TrainingData.project_id == project_id)
            .filter(query)
        )

        total_number_examples = examples.count()

        if sort_by_descending_id:
            examples = examples.order_by(TrainingData.id.desc())
        else:
            examples = examples.order_by(TrainingData.id.asc())

        examples = examples.offset(offset).limit(limit)
        if distinct:
            examples = examples.distinct()
        examples = [t.as_dict() for t in examples]

        if fields_query:
            # Field queries can specify relationship items hence we rather get
            # the full objects and then filter out the fields we want.
            examples = [filter_fields_from_dict(e, fields_query) for e in examples]
            if distinct:
                examples = unique_dictionaries(examples)

        return QueryResult(examples, total_number_examples)

    def create_formatted_training_data(
        self,
        file_name: Optional[Text] = None,
        project_id: Text = config.project_name,
        should_include_lookup_table_entries: bool = False,
    ) -> Dict[Text, Dict[Text, Any]]:
        """Return training data in NLU format.

        Args:
            file_name: Only get data of a certain file.
            project_id: Project id of the new training example.
            should_include_lookup_table_entries: If `True` the training data includes
                the entries of `LookupTable`s. If `False` it just includes the
                abbreviated form with a file reference as element.

        Returns:
             Combined contents of `training_data`, `regex_features`,
            `entity_synonyms` and `lookup_tables`.
        """

        training_examples, _ = self.get_training_data(project_id, filename=file_name)
        regex_features, _ = self.get_regex_features(project_id, filename=file_name)
        if should_include_lookup_table_entries:
            lookup_tables = self.get_lookup_tables_with_elements(project_id)
        else:
            lookup_tables = self.get_lookup_tables(project_id, filename=file_name)
        entity_synonyms = self.get_entity_synonyms(project_id, filename=file_name)
        return nlu_format(
            training_examples, regex_features, lookup_tables, entity_synonyms
        )

    def get_all_filenames(self, project_id: Text) -> List[Text]:
        """Return a list of all values of `filename`."""

        filenames = (
            self.query(TrainingData.filename)
            .filter(TrainingData.project_id == project_id)
            .distinct()
            .all()
        )

        return [f for (f,) in filenames]

    def get_training_data_for_filename(
        self, project_id: Text, filename: Text
    ) -> List[Dict[Text, Any]]:
        return self.get_training_data(project_id, filename=filename)[0]

    def assign_filename_based_on_intent(self, intent: Text, project_id: Text) -> Text:
        """Assign a filename based on the intent of an example.

        Args:
            intent: Intent the example has.
            project_id: Project the example belongs to.

        Returns:
            File which already stores examples of this intent or the oldest NLU file.
        """

        result = (
            self.query(TrainingData.filename)
            .filter(
                TrainingData.project_id == project_id, TrainingData.intent == intent
            )
            .first()
        )
        if result and result.filename:
            return result.filename
        else:
            return self.assign_filename(project_id)

    def assign_filename(self, project_id: Text) -> Text:
        """Finds the filename of the oldest `TrainingData` entry.

        Args:
            project_id: Project name which should be searched for intents.

        Returns:
            Path to oldest training data entry, or `config.default_nlu_filename`
            if no filename was found.
        """

        result = (
            self.query(TrainingData.filename)
            .filter(TrainingData.project_id == project_id)
            .order_by(TrainingData.id.asc())
            .first()
        )

        if result:
            file_path = result[0]
        else:
            project_directory = utils.get_project_directory()
            file_path = (
                project_directory / config.data_dir / config.default_nlu_filename
            )

        return str(file_path)

    def get_training_data_warnings(self, project_id: Text) -> List:
        min_examples = 4
        min_intents = 2
        min_examples_per_intent = NluTrainingData.MIN_EXAMPLES_PER_INTENT

        warnings = []

        # actual training data
        default_query = self.query(TrainingData).filter(
            TrainingData.project_id == project_id
        )

        n_examples = default_query.count()

        if n_examples < min_examples:
            warnings.append({"type": "data", "min": min_examples, "count": n_examples})

        ex_per_intent = (
            self.query(func.count(TrainingData.id), TrainingData.intent)
            .filter(TrainingData.project_id == project_id)
            .group_by(TrainingData.intent)
            .all()
        )

        if len(ex_per_intent) < min_intents:
            warnings.append(
                {
                    "type": "intent",
                    "min": min_intents,
                    "count": len(ex_per_intent),
                    "meta": None,
                }
            )

        for examples_for_intent, intent_name in ex_per_intent:
            if examples_for_intent < min_examples_per_intent:
                warnings.append(
                    {
                        "type": "dataPerIntent",
                        "min": min_examples_per_intent,
                        "count": examples_for_intent,
                        "meta": intent_name,
                    }
                )
            # blank intent
            if intent_name == "":
                warnings.append(
                    {"type": "blankIntent", "max": 0, "count": 1, "meta": None}
                )

        return warnings

    def save_example(
        self,
        username: Text,
        project_id: Text,
        example: Dict[Text, Any],
        filename: Optional[Text] = None,
        dump_data: bool = True,
        add_example_items_to_domain: bool = True,
    ) -> Dict[Text, Any]:
        """Saves a training example or updates it if the example already exists."""

        existing_id = example.get("id")

        if not filename:
            filename = self.assign_filename_based_on_intent(
                example.get("intent"), project_id
            )

        if existing_id is not None:
            existing_example = self._get_training_example_by_id(existing_id)
        else:
            text_hash = get_text_hash(example.get("text", ""))
            existing_example = self._get_example_by_hash(project_id, text_hash)

        existing_id = existing_example.id if existing_example else None

        # sqlalchemy automatically updates the attributes of a mapped object
        # so we have to record the filename now for later use
        existing_filename = existing_example.filename if existing_example else None

        if existing_id is not None:
            # if this example already exists we update the existing one
            new_example = self.replace_example(
                {"username": username}, project_id, example, existing_id, filename
            )
        else:
            new_example_object = self._training_data_object_from_dict(
                example, project_id, filename, username
            )
            self.add(new_example_object)
            self.flush()  # flush to get the example id
            new_example = new_example_object.as_dict()

        if dump_data:
            # dump the desired or assigned filename, as well as that of the old
            # example that's been replaced (if present)
            nlu_files_to_dump = {filename}
            if existing_filename:
                nlu_files_to_dump.add(existing_filename)

            background_dump_service.add_nlu_changes(nlu_files_to_dump)

        if add_example_items_to_domain:
            self._add_intents_and_entities_to_domain([example], project_id)

        return new_example

    def _get_training_example_by_id(self, _id: int) -> Optional[TrainingData]:
        """Get `TrainingData` object for `_id`."""
        return self.query(TrainingData).filter(TrainingData.id == _id).first()

    def save_user_event_as_example(
        self, user: Dict[Text, Text], project_id: Text, event: Dict[Text, Any]
    ) -> Dict[Text, Any]:
        """Adds a training example based on a tracker event.

        Args:
            user: User of the training data.
            project_id: Project id of the new training example.
            event: The tracker event of the user message.

        Returns:
            If successful the stored example, otherwise `None`.
        """

        parse_data = event["parse_data"]
        example = {
            "intent": parse_data["intent"]["name"],
            "entities": parse_data.get("entities", []),
            "text": event.get("text"),
        }
        return self.save_example(user["username"], project_id, example)

    def replace_example(
        self,
        user: Dict[Text, Text],
        project_id: Text,
        example: Dict[Text, Any],
        example_id: int,
        filename: Optional[Text] = None,
    ) -> Optional[Dict[Text, Any]]:
        existing = self._get_training_example_by_id(example_id)

        if not existing:
            return None

        filename = filename or existing.filename

        # sqlalchemy automatically updates the attributes of a mapped object
        # so we have to record the filename now for later use
        existing_filename = existing.filename

        updated = self._training_data_object_from_dict(
            example, project_id, filename, user["username"]
        )
        updated.id = existing.id

        self.merge(updated)
        background_dump_service.add_nlu_changes({filename, existing_filename})

        self._add_intents_and_entities_to_domain([example], project_id)

        return updated.as_dict()

    def _add_intents_and_entities_to_domain(
        self, examples: List[Dict[Text, Any]], project_id: Text
    ) -> None:

        intents = [ex.get("intent") for ex in examples]
        entities = [e.get("entity") for ex in examples for e in ex.get("entities", [])]
        self._add_items_to_domain(entities, intents, project_id)

    def add_all_intents_and_entities_to_domain(self, project_id: Text) -> None:
        """Adds intents and entities to domain from all training data."""

        intents = [i["intent"] for i in self.get_intents(project_id)]
        entities = [
            e["entity"]
            for e in self.get_entities(
                project_id, should_exclude_extractor_entities=True
            )
        ]
        self._add_items_to_domain(entities, intents, project_id)

    def _add_items_to_domain(
        self, entities: List[Text], intents: List[Text], project_id: Text
    ):
        from rasax.community.services.domain_service import DomainService

        DomainService(self.session).add_items_to_domain(
            intents=intents,
            entities=entities,
            project_id=project_id,
            dump_data=False,
            origin="NLU training data",
        )

    def dump_nlu_data(
        self,
        project: Text = config.project_name,
        files: Optional[Iterable[Text]] = None,
    ) -> None:
        """Dump Rasa NLU data in database to file at `path`."""

        if not files:
            files = self.get_all_filenames(project)

        for file_name in files:
            logger.debug(f"Dumping NLU data to file '{file_name}'.")
            training_data = self.get_nlu_training_data_object(file_name, project)
            directory = os.path.dirname(file_name) or "."
            training_data.persist(directory, os.path.basename(file_name))

    def _get_example_by_hash(
        self, project_id: Text, _hash: Text
    ) -> Optional[TrainingData]:
        return (
            self.query(TrainingData)
            .filter(
                and_(TrainingData.project_id == project_id, TrainingData.hash == _hash)
            )
            .first()
        )

    def get_example_by_hash(
        self, project_id: Text, _hash: Text
    ) -> Optional[Dict[Text, Any]]:
        """Fetch a training example by its `_hash`.

        Args:
            project_id: Project ID associated with the example.
            _hash: MD5 hash of the example's `text` field.

        Returns:
            JSON dump of training example if it exists, else `None`.
        """
        result = self._get_example_by_hash(project_id, _hash)
        if result:
            return result.as_dict()
        else:
            return None

    def delete_example(self, _id: int) -> bool:
        """Delete a training example by its ID.

        Args:
            _id: ID of the example to be deleted.

        Returns:
            `True` if the deletion was successful, else `False`.
        """
        result = self._get_training_example_by_id(_id)

        if result:
            self.delete(result)
            background_dump_service.add_nlu_changes([result.filename])

        return result is not None

    def delete_example_by_hash(self, project_id: Text, _hash: Text) -> bool:
        """Deletes an example based on its text hash.

        Args:
            project_id: Project id of the training example.
            _hash: Text hash of the training example.

        Returns:
            `True` if an example was deleted, otherwise `False`.
        """

        result = (
            self.query(TrainingData)
            .filter(
                and_(TrainingData.project_id == project_id, TrainingData.hash == _hash)
            )
            .all()
        )

        self.delete_all(result)

        return result

    def _bulk_save_entity_synonyms(
        self,
        project_id: Text,
        synonyms: Dict[Text, Text],
        filename: Optional[Text] = None,
    ) -> None:
        """Persists all entity synonyms and their mapped values. Argument `synonyms`
        must follow the format used by NLU training data synonyms:

        ```
        {
            "LA": "los angeles",
            "nyc": "new york",
            "ny": "new york",
            "manhattan": "new york"
        }
        ```

        Args:
            project_id: Current project ID.
            synonyms: Dictionary containing mapped values as dict keys, and entity
                synonyms as dict values (the order is reversed to allow many values
                to map to the same entity synonym).
            filename: File from where the synonyms were read.
        """

        entity_synonyms = defaultdict(list)
        for mapping_name, synonym_name in synonyms.items():
            entity_synonyms[synonym_name].append(mapping_name)

        for synonym_name, mapped_values in entity_synonyms.items():
            try:
                self.create_entity_synonym(
                    project_id, synonym_name, mapped_values, filename
                )
            except ValueError as e:
                # Bulk inserts should not fail in case one single item is invalid.
                # Instead it should just skip this item.
                warnings.warn(str(e))

    def replace_additional_training_features(
        self,
        project_id: Text,
        training_data: nlu_data.TrainingData,
        filename: Optional[Text] = None,
    ) -> None:
        """Persists regex features, lookup tables and entity synonyms.

        Overwrites existing entries."""

        # delete old entries
        self.query(RegexFeature).filter(RegexFeature.project_id == project_id).delete()

        self.query(LookupTable).filter(LookupTable.project_id == project_id).delete()

        self.query(EntitySynonym).filter(
            EntitySynonym.project_id == project_id
        ).delete()

        # Create new ones
        regex_features = [
            RegexFeature(
                name=r.get("name"),
                pattern=r.get("pattern"),
                project_id=project_id,
                filename=filename,
            )
            for r in training_data.regex_features
        ]

        lookup_tables = [
            LookupTable(
                name=l.get("name"),
                number_of_elements=len(l.get("elements", [])),
                elements=json.dumps(l.get("elements", [])),
                project_id=project_id,
                referencing_nlu_file=filename,
            )
            for l in training_data.lookup_tables
            # Ignore lookup tables that contain a filename as "elements"
            if isinstance(l.get("elements", []), list)
        ]

        # insert new entries
        self.bulk_save_objects(regex_features)
        self.bulk_save_objects(lookup_tables)
        self._bulk_save_entity_synonyms(
            project_id, training_data.entity_synonyms, filename
        )

        self.commit()

    def get_regex_features(
        self,
        project_id: Text,
        filename: Optional[Text] = None,
        offset: Optional[int] = None,
        limit: Optional[int] = None,
    ) -> QueryResult:
        regex_features = self.query(RegexFeature).filter(
            RegexFeature.project_id == project_id
        )

        number_of_total_regexes = regex_features.count()
        if filename:
            regex_features = regex_features.filter(RegexFeature.filename == filename)

        regex_features = regex_features.order_by(RegexFeature.id.asc())
        regex_features = regex_features.offset(offset).limit(limit)
        regex_features = [r.as_dict() for r in regex_features.all()]
        return QueryResult(regex_features, number_of_total_regexes)

    def _get_regex_feature_by_id(self, regex_id: int) -> RegexFeature:
        regex = self.query(RegexFeature).filter(RegexFeature.id == regex_id).first()

        if not regex:
            raise ValueError(f"Regex with id '{regex_id}' was not found")
        return regex

    def get_regex_feature_by_id(self, regex_id: int) -> Dict[Text, Union[int, Text]]:
        """Get regex feature by its unique id."""

        return self._get_regex_feature_by_id(regex_id).as_dict()

    def _get_regex_by_pattern(
        self, pattern: Text, project_id: Text
    ) -> Optional[Dict[Text, Union[int, Text]]]:
        regex = (
            self.query(RegexFeature)
            .filter(
                and_(
                    RegexFeature.pattern == pattern,
                    RegexFeature.project_id == project_id,
                )
            )
            .first()
        )
        if regex:
            return regex.as_dict()
        return None

    def create_regex_feature(
        self,
        regex_feature: Dict[Text, Text],
        project_id: Text = config.project_name,
        filename: Optional[Text] = None,
    ) -> Dict[Text, Union[int, Text]]:
        """Creates a new regex feature."""

        regex_with_same_pattern = self._get_regex_by_pattern(
            regex_feature["pattern"], project_id
        )
        if regex_with_same_pattern:
            raise ValueError(
                "Regex '{}' already has the same pattern '{}'.".format(
                    regex_with_same_pattern["name"], regex_feature["pattern"]
                )
            )
        filename = filename or self.assign_filename(project_id)
        new = RegexFeature(
            name=regex_feature["name"],
            pattern=regex_feature["pattern"],
            project_id=project_id,
            filename=filename,
        )
        self.add(new)
        self.flush()  # flush to get the example id

        background_dump_service.add_nlu_changes([filename])

        return new.as_dict()

    def update_regex_feature(
        self, regex_id: int, updated_regex: Dict[Text, Union[int, Text]]
    ) -> Dict[Text, Union[int, Text]]:
        """Update an existing regex."""

        existing = self._get_regex_feature_by_id(regex_id)
        existing.name = updated_regex["name"]
        existing.pattern = updated_regex["pattern"]

        background_dump_service.add_nlu_changes([existing.filename])

        return existing.as_dict()

    def delete_regex_feature(self, regex_id: int) -> None:
        """Delete regex feature by its id.

        Raises `ValueError` in case the the id does not exist.
        """
        to_delete = self._get_regex_feature_by_id(regex_id)
        self.delete(to_delete)
        background_dump_service.add_nlu_changes([to_delete.filename])

    @staticmethod
    def _rasa_reader_format_entity_synonyms(
        entity_synonyms: List[EntitySynonym],
    ) -> List[Dict[Text, Any]]:
        """Format entity synonyms into shape expected by `RasaReader`.

        `RasaReader` expects a list of dictionaries of the form

        {
            "synonyms": [
              "Chines",
              "chines",
              "Chinese"
            ],
            "value": "chinese"
        }
        """
        return [
            {
                "synonyms": [value.name for value in entity_synonym.synonym_values],
                "value": entity_synonym.name,
            }
            for entity_synonym in entity_synonyms
        ]

    def _entity_synonym_value_use_count(
        self, entity_synonym_value: EntitySynonymValue
    ) -> int:
        """Given a single synonym mapped value, calculate how many times
        its text value is used in the NLU training data.

        Args:
            entity_synonym_value: The mapped value on which to operate on.

        Returns:
            Number representing the number of times the text value of the
                mapped value is used in the NLU training data.
        """

        return (
            self.query(TrainingDataEntity)
            .filter(TrainingDataEntity.original_value == entity_synonym_value.name)
            .count()
        )

    def _entity_synonym_values_use_counts(
        self, entity_synonym: EntitySynonym
    ) -> Dict[Text, int]:
        """Given an entity synonym, calculate how many times each of its mapped values
        are used in the NLU training data.

        Args:
            entity_synonym: The entity synonym on which to operate on.

        Returns:
            A dictionary containing the use count of each mapped value, using
                the mapped value's ID as key.
        """

        example_counts = {}

        for entity_synonym_value in entity_synonym.synonym_values:
            example_counts[
                entity_synonym_value.id
            ] = self._entity_synonym_value_use_count(entity_synonym_value)

        return example_counts

    def _api_consumers_format_entity_synonyms(
        self, entity_synonyms: List[EntitySynonym]
    ) -> List[Dict[Text, Any]]:
        """Format entity synonyms into shape expected by consumers of the Rasa X API.

        Args:
            entity_synonyms: List of entity synonyms to format.

        Returns:
            List of entity synonyms in a JSON-like format.
        """

        return [
            entity_synonym.as_dict(
                self._entity_synonym_values_use_counts(entity_synonym)
            )
            for entity_synonym in entity_synonyms
        ]

    def get_entity_synonyms(
        self, project_id: Text, filename: Optional[Text] = None, nlu_format: bool = True
    ) -> List[Dict[Text, Any]]:
        """Get a JSON-like representation of all entity synonyms, and their respective
        mapped values.

        Args:
            project_id: Filter by project ID.
            filename: Filter by filename on which the entity synonyms were defined.
            nlu_format: If `True`, return results in a format compatible with
                `RasaReader`. If `False`, use the format defined by the Rasa X API.

        Returns:
            Entity synonyms and their properties, including their mapped values (and
            their properties).
        """

        entity_synonyms = self.query(EntitySynonym).filter(
            EntitySynonym.project_id == project_id
        )

        if filename:
            entity_synonyms = entity_synonyms.filter(EntitySynonym.filename == filename)

        entity_synonyms = entity_synonyms.order_by(EntitySynonym.id.asc()).all()
        if nlu_format:
            return self._rasa_reader_format_entity_synonyms(entity_synonyms)

        return self._api_consumers_format_entity_synonyms(entity_synonyms)

    def _get_entity_synonym(
        self, project_id: Text, entity_synonym_id: int
    ) -> Optional[EntitySynonym]:
        """Get an entity synonym.

        Args:
            project_id: Filter by project ID.
            entity_synonym_id: The ID of the entity synonym.

        Returns:
            An `EntitySynonym` if it was found, or `None` otherwise.
        """

        return (
            self.query(EntitySynonym)
            .filter(
                EntitySynonym.project_id == project_id,
                EntitySynonym.id == entity_synonym_id,
            )
            .one_or_none()
        )

    def get_entity_synonym(
        self, project_id: Text, entity_synonym_id: int
    ) -> Optional[Dict[Text, Any]]:
        """Get a JSON-like representation of an entity synonym, with its mapped values.

        Args:
            project_id: Filter by project ID.
            entity_synonym_id: The ID of the entity synonym.

        Returns:
            A dictionary representing the entity synonym if it was found, or `None`
            otherwise.
        """

        entity_synonym = self._get_entity_synonym(project_id, entity_synonym_id)

        if entity_synonym:
            return entity_synonym.as_dict(
                self._entity_synonym_values_use_counts(entity_synonym)
            )

        return None

    def create_entity_synonym(
        self,
        project_id: Text,
        synonym_name: Text,
        mapped_values: List[Text],
        filename: Optional[Text] = None,
    ) -> Optional[EntitySynonym]:
        """Create a new entity synonym (`EntitySynonym`), with one or more
        mapped values (`EntitySynonymValue`).

        Args:
            project_id: Project ID.
            synonym_name: Name of the entity synonym.
            mapped_values: Set of mapped values to create.
            filename: Name of the training data file which stored this synonym.

        Returns:
            `None` if an `EntitySynonym` with that name already exists,
            otherwise an `EntitySynonym` object.

        Raises:
            ValueError: If the mapped values set is empty or if the entity synonym's
            name is contained in the mapped values set.
        """

        existing = (
            self.query(EntitySynonym)
            .filter(
                EntitySynonym.project_id == project_id,
                EntitySynonym.name == synonym_name,
            )
            .first()
        )
        if existing:
            return None

        entity_synonym = EntitySynonym(
            name=synonym_name, project_id=project_id, filename=filename
        )
        self.add(entity_synonym)
        self.flush()

        self.add_entity_synonym_mapped_values(
            project_id, entity_synonym.id, mapped_values
        )

        return entity_synonym

    def update_entity_synonym(
        self, project_id: Text, entity_synonym_id: int, new_value: Text
    ) -> Optional[EntitySynonym]:
        """Update the value ("name") of an entity synonym.

        Args:
            project_id: Filter by project ID.
            entity_synonym_id: The ID of the entity synonym.
            new_value: New value to set.

        Returns:
            An `EntitySynonym` object if the entity synonym was updated successfully and
            `None` if another entity synonym with name == `new_value` already exists.

        Raises:
            ValueError: If the entity synonym with ID `entity_synonym_id` could not be
            found.
        """

        entity_synonym = self._get_entity_synonym(project_id, entity_synonym_id)
        if not entity_synonym:
            raise ValueError(
                f"EntitySynonym with ID '{entity_synonym_id}' does not exist."
            )

        existing = (
            self.query(EntitySynonym)
            .filter(
                EntitySynonym.project_id == project_id, EntitySynonym.name == new_value
            )
            .first()
        )

        if existing:
            return None

        entity_synonym.name = new_value
        return entity_synonym

    def delete_entity_synonym(self, project_id: Text, entity_synonym_id: int) -> bool:
        """Delete an entity synonym.

        Args:
            project_id: Filter by project ID.
            entity_synonym_id: The ID of the entity synonym.

        Returns:
            `True` if the entity synonym was found and deleted, and `False` otherwise.
        """

        entity_synonym = self._get_entity_synonym(project_id, entity_synonym_id)
        if not entity_synonym:
            return False

        self.delete(entity_synonym)
        return True

    def delete_entity_synonym_mapped_value(
        self, project_id: Text, entity_synonym_id: int, mapping_id: int
    ) -> bool:
        """Delete a value mapped to an entity synonym.

        If the entity synonym no longer has any mapped values after deleting the
        specified mapped value, it will be deleted as well.
        This also deletes the synonym reference from `TrainingDataEntity`s using this
        mapped value.

        Args:
            project_id: Filter by project ID.
            entity_synonym_id: The ID of the entity synonym.
            mapping_id: ID of the mapped value to delete.

        Returns:
            `True` if the mapped value was deleted successfully and `False` if no such
            mapped value was found.

        Raises:
            ValueError: If the entity synonym with ID `entity_synonym_id` could not be
            found.
        """

        entity_synonym = self._get_entity_synonym(project_id, entity_synonym_id)
        if not entity_synonym:
            raise ValueError(
                f"EntitySynonym with ID '{entity_synonym_id}' does not exist."
            )

        to_delete = (
            self.query(EntitySynonymValue)
            .filter(
                EntitySynonymValue.entity_synonym_id == entity_synonym.id,
                EntitySynonymValue.id == mapping_id,
            )
            .first()
        )

        if not to_delete:
            return False
        self.delete(to_delete)

        if len(entity_synonym.synonym_values) == 1:
            # The entity synonym no longer has any mapped values. Delete it.
            self.delete(entity_synonym)
        else:
            self._remove_synonym_reference_to_entities(
                entity_synonym_id, to_delete.name
            )

        return True

    def _get_entity_synonym_value(
        self, project_id: Text, entity_synonym_id: int, mapping_id: int
    ) -> Optional[EntitySynonymValue]:
        """Get a mapped value for an entity synonym.

        Args:
            project_id: Filter by project ID.
            entity_synonym_id: The ID of the entity synonym.
            mapping_id: The ID of the mapped value.

        Returns:
            An `EntitySynonymValue` object if it was found, or `None` otherwise.

        Raises:
            ValueError: If the entity synonym with ID `entity_synonym_id` could not be
            found.
        """

        entity_synonym = self.get_entity_synonym(project_id, entity_synonym_id)
        if not entity_synonym:
            raise ValueError(
                f"EntitySynonym with ID '{entity_synonym_id}' does not exist."
            )

        return (
            self.query(EntitySynonymValue)
            .filter(
                EntitySynonymValue.entity_synonym_id == entity_synonym["id"],
                EntitySynonymValue.id == mapping_id,
            )
            .one_or_none()
        )

    def _remove_synonym_reference_to_entities(
        self, synonym_id: int, mapped_value: Text
    ) -> None:
        referencing_entities = (
            self.query(TrainingDataEntity)
            .filter(
                TrainingDataEntity.entity_synonym_id == synonym_id,
                TrainingDataEntity.original_value == mapped_value,
            )
            .all()
        )
        for entity in referencing_entities:
            entity.entity_synonym_id = None

    def _get_entity_synonym_value_by_name(
        self, project_id: Text, entity_synonym_id: int, mapped_value: Text
    ) -> Optional[EntitySynonymValue]:
        """Get a mapped value for an entity synonym, searching by its name.

        Args:
            project_id: Filter by project ID.
            entity_synonym_id: The ID of the entity synonym.
            mapped_value: Filter by `.name` property of the `EntitySynonymValue`.

        Returns:
            An `EntitySynonymValue` object if it was found, or `None` otherwise.

        Raises:
            ValueError: If the entity synonym with ID `entity_synonym_id` could not be
            found.
        """

        entity_synonym = self.get_entity_synonym(project_id, entity_synonym_id)
        if not entity_synonym:
            raise ValueError(
                f"EntitySynonym with ID '{entity_synonym_id}' does not exist."
            )

        return (
            self.query(EntitySynonymValue)
            .filter(
                EntitySynonymValue.entity_synonym_id == entity_synonym["id"],
                EntitySynonymValue.name == mapped_value,
            )
            .one_or_none()
        )

    def get_synonym_by_mapped_value(
        self, mapped_value: Text, project_id: Text
    ) -> Optional[Dict[Text, Any]]:
        """Get synonyms by a mapped value.

        Args:
            mapped_value: Value which should be used to query the synonyms.
            project_id: Project the synonym should belong to.

        Returns:
            Matching entity synonyms (should always be of length 1).
        """
        synonym = (
            self.query(EntitySynonymValue)
            .filter(
                EntitySynonymValue.entity_synonym.has(
                    EntitySynonym.project_id == project_id
                ),
                EntitySynonymValue.name == mapped_value,
            )
            .first()
        )

        return synonym.entity_synonym.as_dict() if synonym else None

    def add_entity_synonym_mapped_values(
        self, project_id: Text, entity_synonym_id: int, mapped_values: List[Text]
    ) -> Optional[List[Dict[Text, Any]]]:
        """Map new values to an existing entity synonym.

        Args:
            project_id: Project ID.
            entity_synonym_id: ID of the entity synonym.
            mapped_values: Values to map to the entity synonym.

        Returns:
            List of all the newly mapped values, or `None` if any of the values was
            already mapped.

        Raises:
            ValueError: If the entity synonym with ID `entity_synonym_id` could not be
            found, or if mapped values set is empty, or if any of the mapped values'
            text is equal to the text value of the entity synonym.
        """

        entity_synonym = self._get_entity_synonym(project_id, entity_synonym_id)
        if not entity_synonym:
            raise ValueError(
                f"EntitySynonym with ID '{entity_synonym_id}' does not exist."
            )

        mapped_values_without_synonyms_value = [
            value for value in mapped_values if value != entity_synonym.name
        ]
        if len(mapped_values_without_synonyms_value) < len(mapped_values):
            warnings.warn(
                f"Value to map '{entity_synonym.name}' is equal to the entity "
                f"synonym's value."
            )

        unique_mapped_values = utils.deduplicate_preserving_order(
            mapped_values_without_synonyms_value
        )
        existing_mappings = (
            self.query(EntitySynonymValue)
            .filter(
                EntitySynonymValue.entity_synonym_id == entity_synonym_id,
                EntitySynonymValue.name.in_(unique_mapped_values),
            )
            .all()
        )

        for existing in existing_mappings:
            unique_mapped_values.remove(existing.name)

        number_of_already_mapped_values = len(
            mapped_values_without_synonyms_value
        ) - len(unique_mapped_values)
        if number_of_already_mapped_values > 0:
            warnings.warn(
                f"Skipped adding {number_of_already_mapped_values} because you are "
                f"trying to map values which have been previously mapped or are "
                f"repeated."
            )

        if not unique_mapped_values:
            raise ValueError("At least one value to map must be specified.")

        entity_synonym_values = self._add_entity_synonym_values(
            entity_synonym, unique_mapped_values
        )
        self.flush()

        return [
            value.as_dict(self._entity_synonym_value_use_count(value))
            for value in entity_synonym_values
        ]

    def _add_entity_synonym_values(
        self, entity_synonym: EntitySynonym, mapped_values: List[Text]
    ) -> List[EntitySynonymValue]:
        """Adds `EntitySynonymValue` to an existing `EntitySynonym` in the database.

        Args:
            entity_synonym: `EntitySynonym` which has some mapped values.
            mapped_values: Mapped values of a synonym which should be added to
                the database.

        Returns:
            Created `EntitySynonymValue`s.
        """

        entity_synonym_values = []
        for mapped_value in mapped_values:
            entity_synonym_value = EntitySynonymValue(
                entity_synonym_id=entity_synonym.id, name=mapped_value
            )

            self.add(entity_synonym_value)
            entity_synonym_values.append(entity_synonym_value)

            self._add_synonym_reference_to_entities(entity_synonym.id, mapped_value)

        return entity_synonym_values

    def _add_synonym_reference_to_entities(
        self, entity_synonym_id: int, mapped_value: Text
    ) -> None:
        """Add the synonym id to entities which use a mapped value of
           this synonym.

        Args:
            entity_synonym_id: Id of the synonym.
            mapped_value: Value which is used to search the entities in order to see
                whether they are using this synonym.
        """

        entities_with_this_text = (
            self.query(TrainingDataEntity)
            .filter(TrainingDataEntity.original_value == mapped_value)
            .all()
        )
        for entity in entities_with_this_text:  # type: TrainingDataEntity
            entity.entity_synonym_id = entity_synonym_id

    def update_entity_synonym_mapped_value(
        self, project_id: Text, entity_synonym_id: int, mapping_id: int, new_value: Text
    ) -> Optional[EntitySynonymValue]:
        """Update the text value of a value mapped to an entity synonym.

        Args:
            project_id: Filter by project ID.
            entity_synonym_id: The ID of the entity synonym.
            mapping_id: ID of the mapped value to update.
            new_value: The new value to set.

        Returns:
            An `EntitySynonymValue` object if the mapped value was updated successfully
            and `None` if another mapped value with the same value already existed.

        Raises:
            ValueError: If the entity synonym with ID `entity_synonym_id` could not be
            found, or if the entity synonym mapped value with value `mapped_value` could
            not be found, or if the new value to map is equal to the entity synonym's
            value.
        """

        entity_synonym_value = self._get_entity_synonym_value(
            project_id, entity_synonym_id, mapping_id
        )

        if not entity_synonym_value:
            raise ValueError(
                f"EntitySynonymValue with ID '{mapping_id}' does not exist."
            )

        if new_value == entity_synonym_value.entity_synonym.name:
            raise ValueError(
                "Value to map '{}' is equal to the entity synonym's value.".format(
                    new_value
                )
            )

        repeated = self._get_entity_synonym_value_by_name(
            project_id, entity_synonym_id, new_value
        )

        if repeated:
            return None

        self._delete_mapped_synonym_value_from_entities(entity_synonym_id)

        # Add new mapped values to entities
        self._add_synonym_reference_to_entities(entity_synonym_id, new_value)

        entity_synonym_value.name = new_value

        return entity_synonym_value

    def _delete_mapped_synonym_value_from_entities(
        self, entity_synonym_id: int
    ) -> None:
        entities_with_mapped_value = (
            self.query(TrainingDataEntity)
            .filter(TrainingDataEntity.entity_synonym_id == entity_synonym_id)
            .all()
        )
        for entity in entities_with_mapped_value:
            entity.entity_synonym_id = None

    def get_lookup_tables(
        self,
        project_id: Text,
        filename: Optional[Text] = None,
        include_filenames: bool = False,
    ) -> List[Dict[Text, Any]]:
        """Get all lookup tables for a certain project / filename.

        Args:
            project_id: id of the project of which the lookup tables should be returned
            filename: returns only lookup tables with a certain filename
            include_filenames: `True` if the filename should be included in the result

        Returns:
            All lookup tables matching the given conditions.
        """

        lookup_tables = self.query(LookupTable).filter(
            LookupTable.project_id == project_id
        )

        if filename:
            lookup_tables = lookup_tables.filter(
                LookupTable.referencing_nlu_file == filename
            )
        lookup_tables = lookup_tables.order_by(LookupTable.id.asc())

        return [r.as_dict(include_filenames) for r in lookup_tables.all()]

    def get_lookup_tables_with_elements(
        self, project_id: Text
    ) -> List[Dict[Text, Union[Text, List[Text]]]]:
        """Get lookup tables including all their elements.

        While `get_lookup_table` does not return the lookup table elements, this method
        returns the `LookupTables` includes all of their elements.

        Args:
            project_id: the id of the project this data belongs to.

        Returns:
            the lookup tables including all their elements.
        """

        lookup_tables = (
            self.query(LookupTable)
            .filter(LookupTable.project_id == project_id)
            .order_by(LookupTable.id.asc())
            .all()
        )

        return [
            {"name": l.name, "elements": json.loads(l.elements)} for l in lookup_tables
        ]

    def save_lookup_table(
        self,
        filename: Text,
        content: Text,
        project_id: Text = config.project_name,
        dump_data: bool = True,
    ) -> Dict[Text, Union[Text, int]]:
        """Store a lookup table which was uploaded as file.

        Args:
            filename: name of the file
            content: content of the lookup tables separated by newlines
            project_id: the id of the project this data belongs to
            dump_data: `True` if lookup table should be saved as file

        Returns:
            created lookup table.
        """

        elements = content.splitlines()
        new = LookupTable(
            project_id=project_id,
            name=filename,
            # that's the NLU file which references this lookup table file
            referencing_nlu_file=self.assign_filename(project_id),
            elements=json.dumps(elements),
            number_of_elements=len(elements),
        )
        self.add(new)
        # Flush to assign id
        self.flush()

        if dump_data:
            background_dump_service.add_lookup_table_change(
                new.id, referencing_nlu_file=new.referencing_nlu_file
            )

        return new.as_dict()

    def get_lookup_table(self, lookup_table_id: int) -> Text:
        """Get a lookup table by its id.

        Args:
            lookup_table_id: the id of the lookup table which should be retrieved

        Returns:
            The content of the lookup table split by newlines.

        Raises:
            ValueError: raised if no lookup table with this id exists
        """

        item = self._get_lookup_table_by_id(lookup_table_id)
        return self._format_lookup_table_elements_as_text(item)

    @staticmethod
    def _format_lookup_table_elements_as_text(lookup_table: LookupTable) -> Text:
        return "\n".join(json.loads(lookup_table.elements))

    def dump_lookup_tables(self, lookup_table_ids: Set[int]) -> None:
        """Dump lookup tables with the given IDs to disk.

        Args:
            lookup_table_ids: The IDs of the lookup tables which should be dumped.
        """
        for _id in lookup_table_ids:
            self._dump_lookup_table_content(_id)

    def _dump_lookup_table_content(self, lookup_table_id: int) -> None:
        lookup_table = self._get_lookup_table_by_id(lookup_table_id)
        formatted = self._format_lookup_table_elements_as_text(lookup_table)
        utils.write_text_to_file(lookup_table.absolute_file_path, formatted)

    def _get_lookup_table_by_id(self, lookup_table_id: int) -> LookupTable:
        item = self.query(LookupTable).filter(LookupTable.id == lookup_table_id).first()

        if not item:
            raise ValueError(
                f"Lookup table with id '{lookup_table_id}' does not exist."
            )
        return item

    def delete_lookup_table(self, lookup_table_id: int) -> None:
        """Delete a lookup table by its id.

        Args:
            lookup_table_id: the id of the lookup table to delete.

        Raises:
            ValueError: raised if no lookup table with this id exists.
        """

        to_delete = self._get_lookup_table_by_id(lookup_table_id)
        self.delete(to_delete)

        self._delete_lookup_table_on_file_system(to_delete.absolute_file_path)

    @staticmethod
    def _delete_lookup_table_on_file_system(lookup_table_path: Text) -> None:
        if (
            utils.should_dump()
            and os.path.exists(lookup_table_path)
            and os.path.isfile(lookup_table_path)
        ):
            logger.debug(f"Deleting lookup table file '{lookup_table_path}'.")
            os.remove(lookup_table_path)

    def get_entities(
        self, project_id: Text, should_exclude_extractor_entities: bool = False
    ) -> List[Dict[Text, Text]]:
        """Fetch entities in training data.

        Exclude entities that have an `extractor` attribute if
        `should_exclude_extractor_entities` is True.
        """

        query = TrainingData.project_id == project_id
        if should_exclude_extractor_entities:
            query = and_(query, TrainingDataEntity.extractor.is_(None))

        entities = (
            self.query(TrainingDataEntity.entity)
            .join(TrainingData)
            .filter(query)
            .distinct()
            .all()
        )

        return [{"entity": e} for e, in entities]

    def get_intents(
        self, project_id: Text, include_example_hashes: bool = True
    ) -> List[Dict[Text, Union[Text, List[Text]]]]:
        """Returns list of intents which appear in the training data.

        Args:
            project_id: Project id of the training data.
            include_example_hashes: If `True` include the hashes of the examples for
                                    each intent.

        Returns:
            List of intent objects which appear in the training data including
            the hashes of the related examples.
        """

        from rasax.community.services.intent_service import INTENT_EXAMPLES_KEY

        intents = (
            self.query(TrainingData.intent)
            .filter(TrainingData.project_id == project_id)
            .distinct()
            .all()
        )

        results = []
        for (i,) in intents:
            intent = {"intent": i}
            if include_example_hashes:
                intent[INTENT_EXAMPLES_KEY] = self._get_example_hashes_for_intent(i)

            results.append(intent)

        return results

    def _get_example_hashes_for_intent(self, intent: Text) -> List[Text]:
        example_hashes = (
            self.query(TrainingData.hash)
            .filter(TrainingData.intent == intent)
            .distinct()
            .all()
        )

        return [e for (e,) in example_hashes]

    def delete_data(self):
        """Deletes all examples."""

        old_training_data = self.query(TrainingData).all()
        self.delete_all(old_training_data)

    def replace_data(
        self,
        project_id: Text,
        data: Union[Text, Dict[Text, Any]],
        data_format: Text,
        username: Optional[Text] = None,
        filename: Optional[Text] = None,
        dump_data: bool = True,
    ):
        """Deletes all examples and adds examples from `data` in json or
        markdown format instead."""

        self.delete_data()
        self.save_bulk_data(
            project_id, data, data_format, username, filename, dump_data
        )

    def _save_bulk_data(
        self,
        examples: List[Dict[Text, Any]],
        username: Optional[Text],
        filename: Optional[Text],
        project_id: Text = config.project_name,
    ) -> None:
        objects = [
            self._training_data_object_from_dict(e, project_id, filename, username)
            for e in examples
        ]
        self.add_all(objects)

    def save_bulk_data_from_files(
        self,
        nlu_files: Union[List[Text], Set[Text]],
        project_id: Text = config.project_name,
        username: Text = config.default_username,
    ) -> NluTrainingData:
        """Saves NLU data from `nlu_files` to database."""

        training_data = NluTrainingData()

        for data, path in _read_data(list(nlu_files)):
            logger.debug(f"Injecting NLU data from file '{path}' to database.")

            data_format = loading.guess_format(path)
            if data_format not in {RASA, MARKDOWN}:
                logger.debug(
                    "Invalid data format for file '{}'. "
                    "Accepting '{}' and '{}' but found "
                    "'{}'.".format(path, RASA, MARKDOWN, data_format)
                )
                continue

            additional_data = self.save_bulk_data(
                project_id,
                data,
                data_format,
                username=username,
                filename=path,
                dump_data=False,
                add_data_items_to_domain=False,
            )

            if additional_data:
                training_data = training_data.merge(additional_data)

        # add intents and entities to domain
        self.add_all_intents_and_entities_to_domain(project_id)

        self._save_referenced_lookup_tables(training_data.lookup_tables, project_id)

        return training_data

    def _save_referenced_lookup_tables(
        self, lookup_tables: List[Dict], project_id: Text
    ) -> None:
        """Tries to load and save lookup tables which were referenced in a NLU file."""

        for lookup_table in lookup_tables:
            elements = lookup_table.get("elements")

            if not isinstance(elements, str):
                continue  # it's an inline lookup table which was already saved

            lookup_table_path = utils.get_project_directory() / elements
            if lookup_table_path.is_file():
                logger.debug(f"Injecting referenced lookup table '{elements}'.")
                content = io_utils.read_file(str(lookup_table_path))
                self.save_lookup_table(elements, content, project_id, dump_data=False)

    @staticmethod
    def _combine_response_keys_and_intents(training_data: NluTrainingData) -> None:
        """Combines response keys and intents in `training_data`.

        NLU training examples with `response_key` attributes are modified such that
        the example intent is merged with the response key in the format
        'intent/response_key'.

        Example:

            {
                'intent': 'faq',
                'response_key': 'ask_howdoing',
                'text': 'how are you?'
            }

            becomes

            {
                'intent': 'faq/ask_howdoing',
                'response_key': 'ask_howdoing',
                'text': 'how are you?'
            }

        Args:
            training_data: `NluTrainingData` to modify.

        """
        for message in training_data.training_examples:
            response_key = message.get(RESPONSE_KEY_ATTRIBUTE)
            intent = message.get(INTENT)
            if response_key is not None and intent is not None:
                message.set(INTENT, f"{intent}/{response_key}")

    @staticmethod
    def training_data_contains_retrieval_intents(
        training_data: NluTrainingData,
    ) -> bool:
        """Whether any of the examples in `training_data` contain a retrieval intent."""

        return any(
            e.get(RESPONSE_KEY_ATTRIBUTE) is not None
            for e in training_data.intent_examples
        )

    def save_bulk_data(
        self,
        project_id: Text,
        data: Union[Text, Dict[Text, Any]],
        data_format: Text,
        username: Optional[Text] = None,
        filename: Optional[Text] = None,
        dump_data: bool = True,
        add_data_items_to_domain: bool = True,
    ) -> Optional[TrainingData]:
        """Adds examples from `data` in json or markdown format."""

        if not filename:
            filename = self.assign_filename(project_id)

        from rasa.nlu.training_data.loading import RASA, MARKDOWN

        if data_format == RASA:
            if isinstance(data, str):
                data = json.loads(data)
            training_data = RasaReader().read_from_json(data)
        elif data_format == MARKDOWN:
            if isinstance(data, (bytes, bytearray)):
                data = data.decode("utf-8")
            training_data = MarkdownReader().reads(data)
        else:
            return None

        # support retrieval intents by merging intents with response keys
        self._combine_response_keys_and_intents(training_data)

        # save regex features, entity synonyms and lookup tables if present
        self.replace_additional_training_features(project_id, training_data, filename)

        examples = [m.as_dict() for m in training_data.training_examples]
        self._save_bulk_data(examples, username, filename, project_id)

        if dump_data:
            background_dump_service.add_nlu_changes([filename])

        if add_data_items_to_domain:
            self._add_intents_and_entities_to_domain(examples, project_id)

        return training_data

    def update_intent(
        self, new_intent: Text, example_hashes: List[Text], project_id: Text
    ) -> None:
        """Updates the intent of training examples.

        Args:
            new_intent: Name of the new intent.
            example_hashes: Hashes of the training examples which should be
                            updated.
            project_id: Project id of the training data.
        """

        examples = (
            self.query(TrainingData)
            .filter(
                and_(
                    TrainingData.project_id == project_id,
                    TrainingData.hash.in_(example_hashes),
                )
            )
            .all()
        )

        for e in examples:
            e.intent = new_intent

    def get_nlu_training_data_object(
        self,
        filename: Optional[Text] = None,
        project_id: Text = config.project_name,
        should_include_lookup_table_entries: bool = False,
    ) -> NluTrainingData:
        """Get an NLU `TrainingData` object from training data stored in the database.

        Args:
            filename: file name associated with the training data.
            project_id: Project id of the training data.
            should_include_lookup_table_entries: `True` includes the elements of the lookup
                tables in the data, `False` only adds the file links of the lookup
                tables.
        """

        combined_nlu_data = self.create_formatted_training_data(
            filename, project_id, should_include_lookup_table_entries
        )
        return RasaReader().read_from_json(combined_nlu_data)

    def _training_data_object_from_dict(
        self, data: Dict[Text, Any], project_id: Text, filename: Text, username: Text
    ) -> TrainingData:
        _hash = get_text_hash(data.get("text"))
        entities = []

        for e in data.get("entities", []):
            start = e.get("start")
            end = e.get("end")
            original_value = training_data_dict_get_entity(data, start, end)

            # TODO: Should we also create new synonym / synonym values here?

            # Check if one of the stored synonyms was used
            matching_synonym_value = (
                self.query(EntitySynonymValue)
                .filter(EntitySynonymValue.name == original_value)
                .first()
            )

            entity = TrainingDataEntity(
                start=start,
                end=end,
                entity=e.get("entity"),
                value=e.get("value"),
                original_value=original_value,
                extractor=e.get("extractor"),
                entity_synonym_id=matching_synonym_value.entity_synonym_id
                if matching_synonym_value
                else None,
            )

            entities.append(entity)

        return TrainingData(
            hash=_hash,
            text=data.get("text"),
            intent=data.get("intent"),
            annotator_id=username,
            annotated_at=time.time(),
            project_id=project_id,
            filename=filename,
            entities=entities,
        )


def training_data_dict_get_entity(
    training_data: Dict[Text, Any], start: Optional[int], end: Optional[int]
) -> Optional[Text]:
    """Extract the value of an entity inside a training data example.

    Args:
        training_data: Training data example as a dictionary.
        start: Start position of the entity value in the example's text.
        end: end position of the entity value in the example's text.

    Returns:
        Text value of the entity if `start` and `end` were provided, and `None`
            otherwise.
    """

    if start is not None and end is not None:
        return training_data["text"][start:end]

    return None


def format_entities(example: Dict[Text, Any]) -> List[Dict[Text, Any]]:
    ents = []
    for ent in example.get("entities", []):
        value = ent.get("value")
        if isinstance(value, dict):
            value = json.dumps(value)
        elif value is not None:
            value = str(value)
        built_entity = {
            "start": int(ent["start"]),
            "end": int(ent["end"]),
            "entity": ent["entity"],
            "value": value,
        }
        if ent.get("extractor"):
            built_entity["extractor"] = ent["extractor"]
        ents.append(built_entity)

    return ents


def nlu_format(
    data: List[Dict[Text, Any]],
    regex_features: Optional[List[Dict[Text, Any]]] = None,
    lookup_tables: Optional[List[Dict[Text, Any]]] = None,
    entity_synonyms: Optional[List[Dict[Text, Any]]] = None,
) -> Dict[Text, Dict[Text, Any]]:
    formatted_data = []
    for e in data:
        formatted_entities = format_entities(e)
        formatted_data.append(
            {"text": e["text"], "intent": e["intent"], "entities": formatted_entities}
        )

    rasa_nlu_data = {"common_examples": formatted_data}
    if regex_features:
        rasa_nlu_data["regex_features"] = regex_features
    if lookup_tables:
        rasa_nlu_data["lookup_tables"] = lookup_tables
    if entity_synonyms:
        rasa_nlu_data["entity_synonyms"] = entity_synonyms

    return {"rasa_nlu_data": rasa_nlu_data}
