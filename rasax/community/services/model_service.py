import asyncio  # pytype: disable=pyi-error
import datetime
import glob
import logging
import os
import shutil
import tarfile
import tempfile
import ctypes
from typing import Optional, Text, Dict, Any, List, Union

import time
from aiohttp import ClientConnectorError
from packaging import version
from rasax.community.constants import (
    DEFAULT_RASA_ENVIRONMENT,
    RASA_PRODUCTION_ENVIRONMENT,
)
from sanic.request import Request, File
from sqlalchemy import and_
from sqlalchemy.orm import Session

import rasa.cli.utils as rasa_cli_utils
import rasa.model
import rasa.utils.io
from rasa.utils import endpoints
from rasax.community import config
from rasax.community import utils
from rasax.community.database.model import Model, ModelTag
from rasax.community.database.service import DbService
from rasax.community.utils import QueryResult

logger = logging.getLogger(__name__)

# We use this to not serve any models to Rasa Open Source until we successfully
# validated (e.g. minimum compatible version) the models.
were_models_discovered = utils.mp_context().Value(ctypes.c_bool, False)


class ModelService(DbService):
    version_key = "version"

    def __init__(
        self,
        model_directory: Text,
        session: Session,
        environment: Text = DEFAULT_RASA_ENVIRONMENT,
    ):
        self.model_directory = model_directory
        self.environment = environment
        super().__init__(session)

    async def mark_latest_as_production(self, project: Text = config.project_name):
        if self.model_for_tag(project, RASA_PRODUCTION_ENVIRONMENT):
            # don't tag a model as production if there is already another tagged model
            return

        latest = self._latest_model(project)

        if latest:
            try:
                await self.tag_model(
                    latest.project_id, latest.name, RASA_PRODUCTION_ENVIRONMENT
                )
            except ValueError:
                logger.debug(
                    "Latest model could not be tagged since it is not compatible."
                )

    def _latest_model(self, project: Text) -> Optional[Model]:
        """Returns the latest model."""

        return (
            self.query(Model)
            .filter(Model.project_id == project)
            .order_by(Model.trained_at.desc())
            .first()
        )

    async def discover_models(
        self, max_retries: int = 10, sleep_in_seconds: int = 2
    ) -> None:
        """Synchronize model metadata with models stored on disk.

        Args:
            max_retries: In server mode, try repeatedly in case the Rasa server is not
                yet up.
            sleep_in_seconds:  In server mode, wait a specific time between the retries.

        """

        async def _run_loop(_max_retries, _sleep_in_seconds):
            # runs model discovery in loop for `max_retries` attempts
            while _max_retries:
                try:
                    await self._discover_models()
                    return
                except ClientConnectorError:
                    _max_retries -= 1
                    await asyncio.sleep(_sleep_in_seconds)
                    # Increment sleep after each iteration in server mode as the
                    # rasa-production service might take a while to come up.
                    # This is not necessary in local mode due to faster startup
                    # times.
                    if not config.LOCAL_MODE:
                        _sleep_in_seconds += 2

            logger.warning("Could not run model discovery.")

        await _run_loop(max_retries, sleep_in_seconds)
        self._mark_model_discovery_as_finished()

    @staticmethod
    def _mark_model_discovery_as_finished() -> None:
        with were_models_discovered.get_lock():
            were_models_discovered.value = True

    async def minimum_compatible_version(self) -> Optional[Text]:
        from rasax.community.services.settings_service import SettingsService

        if config.LOCAL_MODE:
            # In local mode Rasa X and Rasa are in the same environment
            from rasa.constants import MINIMUM_COMPATIBLE_VERSION

            return MINIMUM_COMPATIBLE_VERSION

        settings_service = SettingsService(self.session)
        stack_service = settings_service.get_stack_service(self.environment)
        info = await stack_service.version()
        if info:
            return info.get("minimum_compatible_version")
        else:
            logger.debug("Couldn't get a minimum compatible model version.")
            return None

    async def _retry_fetching_minimum_compatible_version(
        self,
        attempts: int = 20,
        sleep_between_retries_in_seconds: Union[int, float] = 0.5,
    ) -> Optional[Text]:
        minimum_version = None
        while attempts > 0:
            minimum_version = await self.minimum_compatible_version()
            if minimum_version:
                break
            await asyncio.sleep(sleep_between_retries_in_seconds)
            attempts -= 1

        return minimum_version

    async def _discover_models(self, project_id: Text = config.project_name) -> None:
        minimum_version = await self._retry_fetching_minimum_compatible_version()

        available_model_names = []
        for path in glob.glob(os.path.join(self.model_directory, "*.tar.gz")):
            metadata = self.get_model_metadata(path)
            model_version = self.get_model_version(metadata)
            is_compatible = self.is_model_compatible(
                minimum_version, model_version=model_version
            )
            if not is_compatible:
                rasa_cli_utils.print_warning(
                    "Version of model {} version is not compatible. "
                    "The model was trained with Rasa version {} "
                    "but the current Rasa requires a minimum version of {}. "
                    "Please retrain your model with a more recent Rasa "
                    "version."
                    "".format(os.path.basename(path), model_version, minimum_version)
                )

            filename: Text = os.path.basename(path)
            model_name = filename.split(".tar.gz")[0]
            available_model_names.append(model_name)
            existing_model = self._get_model_by_name(project_id, model_name)
            if not existing_model:
                _ = await self.add_model(project_id, model_name, path)
                logger.debug(f"Imported model '{model_name}'.")
            elif not is_compatible:
                # Delete all tags from existing incompatible model
                existing_model.tags = []
                logger.info(
                    f"Deleting all tags from model '{existing_model.name}' since its "
                    f"current version {existing_model.version} does not suffice the "
                    f"minimum compatible version {minimum_version}."
                )

        self.delete_not_existing_models_from_db(project_id, available_model_names)

    def delete_not_existing_models_from_db(
        self, project_id: Text, available_models: List[Text]
    ):
        """Delete models from the database which are not available on disk."""

        old_models_in_db = (
            self.query(Model)
            .filter(
                and_(
                    Model.project_id == project_id, Model.name.notin_(available_models)
                )
            )
            .all()
        )

        for m in old_models_in_db:
            logger.info(
                "Deleting model '{}' from database since it could not be found "
                "on disk.".format(m.name)
            )
            self.delete(m)

    @staticmethod
    def _model_name_from_path(path: Text) -> Text:
        # noinspection PyTypeChecker
        return os.path.basename(path).split(".tar.gz")[0]

    async def save_trained_model(self, project: Text, content: bytes) -> Optional[Text]:
        """Store a model trained through Rasa X on disk and its metadata in the
        database."""

        model_path = self._store_trained_model_on_disk(content)
        if not model_path:
            return None

        model_name = self._model_name_from_path(model_path)
        await self.add_model(project, model_name=model_name, path=model_path)

        return model_name

    def _store_trained_model_on_disk(self, content: bytes) -> Optional[Text]:
        from rasa.utils.io import create_temporary_file

        # save model at temporary location to extract metadata timestamp
        temp_model_path = create_temporary_file(data=content, mode="w+b")
        metadata = self.get_model_metadata(temp_model_path)
        model_timestamp = self.get_model_training_time_from_file_as_str(
            metadata, temp_model_path
        )

        # move model to permanent location
        return self._store_zipped_model(
            model_name=model_timestamp, file_path=temp_model_path
        )

    async def save_uploaded_model(
        self, project: Text, model_name: Text, path: Text
    ) -> Optional[Dict[Text, Any]]:
        """Store an uploaded model on disk and its metadata in the database.

        IMPORTANT: the `path` needs to be "safe", e.g. if it is
        a user input it needs to be ensured that it doesn't contain
        malicious names (`../../../.ssh/id_rsa`).
        """

        if not os.path.isfile(path):
            raise Exception("Can only handle model files, no dirs.")

        path = os.path.abspath(path)
        if path.startswith(os.path.abspath(self.model_directory)):
            # no need to move model already in model directory
            stored_path = path
        else:
            stored_path = self._store_zipped_model(model_name, path)
            logger.debug(f"Saved zipped model file at '{stored_path}'.")

        return await self.add_model(project, model_name, stored_path)

    @staticmethod
    def _get_model_hash(path: Text) -> Text:
        return utils.get_file_hash(path)

    async def add_model(
        self, project: Text, model_name: Text, path: Text
    ) -> Optional[Dict[Text, Any]]:
        model_hash = self._get_model_hash(path)

        existing = self.get_model_by_name(project, model_name)
        if existing:
            # no need to add, we already got this one
            return existing

        metadata = self.get_model_metadata(path)
        model_version = self.get_model_version(metadata)
        training_time = self.get_training_time_as_unix_timestamp(metadata, path)

        # in server mode, inject domain and training data from first model
        await self.inject_data_from_first_model(project, path)

        return self._save_model_data(
            project, model_name, path, model_hash, model_version, training_time
        )

    @staticmethod
    def get_model_server_url(tag: Text = DEFAULT_RASA_ENVIRONMENT) -> Text:
        """Creates the model server url for a given tag."""

        model_server_host = os.environ.get("RASA_X_HOST", "http://rasa-x:5002")
        model_server_url = endpoints.concat_url(
            model_server_host, "/api/projects/default/models/tags/{}"
        )

        return model_server_url.format(tag)

    async def _inject_domain_from_model(self, project: Text, domain_path: Text) -> None:
        from rasax.community.services.domain_service import DomainService

        domain_service = DomainService(self.session)

        # do not inject if domain_service already contains a domain
        if not domain_service.has_empty_or_no_domain(project):
            return

        from rasax.community.services.nlg_service import NlgService
        from rasa.utils.io import read_yaml_file

        data = read_yaml_file(domain_path)

        # store responses if no responses found in NLG service
        _, number_of_responses = NlgService(self.session).fetch_responses()
        should_store_responses = number_of_responses == 0

        domain_service.store_domain(
            data,
            project,
            path=None,
            store_responses=should_store_responses,
            # responses injected by a model were already included in a training
            have_responses_been_edited=False,
            username=config.SYSTEM_USER,
        )

    async def inject_data_from_first_model(self, project: Text, path: Text) -> None:
        """Inject domain into db from first model if in server mode."""

        # skip if in local mode or there already exists a model in db
        if config.LOCAL_MODE or (await self.get_models(project))[1] > 0:
            return

        unpacked_model_path = rasa.model.unpack_model(path)

        # inject domain
        domain_path = os.path.join(unpacked_model_path, "core", "domain.yml")
        await self._inject_domain_from_model(project, domain_path)

    @staticmethod
    def save_model_to_disk(req: Request) -> Optional[Text]:
        rfiles = req.files

        if "model" not in rfiles or not len(rfiles["model"]):
            raise FileNotFoundError("No `model` file found.")

        _file = rfiles["model"][0]  # type: File
        filename = utils.secure_filename(_file.name)
        if not filename.endswith(".tar.gz"):
            raise TypeError("No `.tar.gz` file found.")

        return utils.write_request_file_to_disk(_file, filename)

    def _store_zipped_model(self, model_name, file_path):
        """Moves a zipped model from a temporary to a permanent location."""

        zpath = self._create_model_save_path(model_name)

        if os.path.exists(zpath):
            raise FileExistsError(zpath)

        shutil.move(file_path, zpath)
        return zpath

    def _create_model_save_path(self, model_name: Text) -> Text:
        """Creates a directory to save the model at.

        Example: /app/core-projects/default/models/my_model.tar.gz

        Args:
            model_name: Name of the model to be saved.

        Returns:
            The path at which the model is to be saved.

        """

        if not os.path.isdir(self.model_directory):
            os.makedirs(self.model_directory)
        return os.path.join(self.model_directory, model_name + ".tar.gz")

    def is_model_compatible(
        self,
        minimum_compatible_version: Text,
        fpath: Optional[Text] = None,
        model_version: Optional[Text] = None,
    ) -> bool:
        """Check if a model on disk is compatible with the connected NLU."""

        if fpath:
            metadata = self.get_model_metadata(fpath)
            model_version = self.get_model_version(metadata)

        return (
            model_version is not None
            and minimum_compatible_version is not None
            and version.parse(model_version)
            >= version.parse(minimum_compatible_version)
        )

    def _save_model_data(
        self,
        project: Text,
        model_name: Text,
        stored_path: Text,
        model_hash: Text,
        _version: Text,
        training_time: float,
    ) -> Dict[Text, Any]:
        model = Model(
            hash=model_hash,
            name=model_name,
            path=stored_path,
            project_id=project,
            version=_version,
            trained_at=training_time,
        )
        self.add(model)

        return model.as_dict()

    async def tag_model(
        self, project: Text, model_name: Text, tag: Text
    ) -> Dict[Text, Any]:
        """Tags a model if it's compatible.

        Args:
            project: Project the model belongs to.
            model_name: Name of the model which should be tagged.
            tag: Tag which this model should get.

        Returns:
            Tagged model.

        """

        model = (
            self.query(Model)
            .filter(and_(Model.project_id == project, Model.name == model_name))
            .first()
        )

        if not model:
            raise ValueError(
                f"Model '{model_name}' was not found for project '{project}'."
            )

        min_compatible_version = await self.minimum_compatible_version()

        if not self.is_model_compatible(
            min_compatible_version, model_version=model.version
        ):
            raise ValueError(
                "Model '{}' cannot be tagged since its version {} is lower than the "
                "minimum required version ({}).".format(
                    model_name, model.version, min_compatible_version
                )
            )

        self._remove_tag_from_all_models(project, tag)

        tag = ModelTag(tag=tag)
        model.tags.append(tag)

        logger.debug(f"Tagged model '{model_name}' as '{tag}'.")

        return model.as_dict()

    def _remove_tag_from_all_models(self, project: Text, tag: Text) -> None:
        model = (
            self.query(Model)
            .join(ModelTag)
            .filter(and_(Model.project_id == project, ModelTag.tag == tag))
            .first()
        )

        if model:
            self.query(ModelTag).filter(
                and_(ModelTag.tag == tag, ModelTag.model_id == model.id)
            ).delete()

    def delete_model(self, project: Text, model_name: Text) -> Dict[Text, Any]:
        """Delete entry and remove model from path."""
        to_delete = (
            self.query(Model)
            .filter(and_(Model.name == model_name, Model.project_id == project))
            .first()
        )

        if to_delete:
            if os.path.exists(to_delete.path):
                os.remove(to_delete.path)

            self.delete(to_delete)

            return to_delete.as_dict()
        else:
            logger.warning(f"Model '{model_name}' was already removed from database.")
            return {}

    def model_for_tag(self, project: Text, tag: Text) -> Optional[Dict[Text, Any]]:
        if not were_models_discovered.value:
            return

        model = (
            self.query(Model)
            .join(ModelTag)
            .filter(and_(ModelTag.tag == tag, Model.project_id == project))
            .first()
        )

        if model:
            return model.as_dict()
        else:
            return None

    def get_model_by_name(
        self, project: Text, model_name: Text
    ) -> Optional[Dict[Text, Any]]:
        if not were_models_discovered.value:
            return

        model = self._get_model_by_name(project, model_name)

        return model.as_dict() if model else None

    def _get_model_by_name(self, project: Text, model_name: Text) -> Optional[Model]:
        """Returns a model belonging to a certain project and having a certain name.

        Args:
            project: Project the model should belong to.
            model_name: The name the model should have.

        Returns:
            A model if one was found or `None` otherwise.
        """

        return (
            self.query(Model)
            .filter(and_(Model.project_id == project, Model.name == model_name))
            .first()
        )

    def delete_tag(self, project: Text, model_name: Text, tag: Text) -> None:
        model = (
            self.query(Model)
            .filter(
                and_(
                    Model.project_id == project,
                    Model.name == model_name,
                    Model.tags.any(ModelTag.tag == tag),
                )
            )
            .first()
        )

        if not model:
            raise ValueError(f"No model '{model_name}' found for project '{project}'.")

        for t in model.tags:
            if t.tag == tag:
                self.delete(t)

    @staticmethod
    def get_model_metadata(path: Text) -> Optional[Dict[Text, Any]]:
        """Retrieve model metadata for file at `path`."""

        temp_dir = tempfile.mkdtemp()

        try:
            tar = tarfile.open(path)
            tar.extractall(temp_dir)
            tar.close()
            metadata_path = os.path.join(temp_dir, rasa.model.FINGERPRINT_FILE_PATH)
            return rasa.utils.io.read_json_file(metadata_path)
        except tarfile.TarError as e:
            logger.error(f"Failed to open model at path '{path}': {e}.")
        except (FileNotFoundError, ValueError):
            logger.error(f"Could not find model at path '{path}'.")
        except Exception as e:
            logger.error(
                "Encountered error while reading metadata for model at path "
                "'{}': {}.".format(path, e)
            )
        finally:
            shutil.rmtree(temp_dir)

    def get_model_version(
        self, metadata: Optional[Dict], default_version: Text = "0.0.0"
    ) -> Text:
        """Get model version for model `metadata`.

        Args:
            metadata: Metadata dictionary.
            default_version: Default version to be returned when no metadata was
                supplied.

        Return:
             The model version.

        """

        if metadata:
            return metadata.get(self.version_key, default_version)

        logger.debug(
            "No metadata found to retrieve model version from. Assigning "
            "default version of '{}'.".format(default_version)
        )
        return default_version

    @staticmethod
    def get_training_time_as_unix_timestamp(
        metadata: Optional[Dict], file_path: Text
    ) -> float:
        """Get the `trained_at` UNIX timestamp for model with `metadata`.

        Retrieve file modification time if no metadata was supplied.

        Args:
            metadata: Model metadata.
            file_path: Path at which the model is saved.

        Returns:
            Model training time as a UNIX timestamp.

        """

        if metadata:
            trained_at = metadata.get("trained_at")
            if not trained_at:
                logger.debug(
                    "Could not find `trained_at` key in metadata: '{}'\n"
                    "Using file modification time instead with "
                    "os.path.getctime().".format(metadata)
                )
                return os.path.getctime(file_path)
            try:
                dt = datetime.datetime.strptime(trained_at, "%Y%m%d-%H%M%S")
                return time.mktime(dt.timetuple())
            except (TypeError, ValueError):
                try:
                    return float(trained_at)
                except (TypeError, ValueError):
                    logger.debug(
                        "Could not convert `trained_at` time to float "
                        "from metadata: '{}'\nUsing file "
                        "modification time instead with "
                        "os.path.getctime().".format(trained_at)
                    )
                    return os.path.getctime(file_path)
        else:
            logger.debug(
                "No metadata supplied for model at path '{}'.\nUsing file "
                "modification time instead with "
                "os.path.getctime().".format(file_path)
            )
            return os.path.getctime(file_path)

    def get_model_training_time_from_file_as_str(
        self,
        metadata: Optional[Dict],
        file_path: Text,
        timestamp_format: Text = "%Y%m%d-%H%M%S",
    ) -> Optional[Text]:
        """Get the `trained_at` time for model at `file_path` as formatted timestamp.

        Retrieve file modification time if no metadata was supplied.

        Args:
            metadata: Model metadata.
            file_path: Path at which the model is saved.
            timestamp_format: Format to return the timestamp in. The default is
                '%Y%m%d-%H%M%S', producing timestamps such as '20190918-132305'.

        Returns:
            Model training timestamp  formatted using `timestamp_format`.

        """

        model_training_time = self.get_training_time_as_unix_timestamp(
            metadata, file_path
        )
        return datetime.datetime.fromtimestamp(model_training_time).strftime(
            timestamp_format
        )

    async def get_models(
        self,
        project_id: Text,
        limit: Optional[int] = None,
        offset: Optional[int] = None,
        tag: Optional[Text] = None,
    ) -> QueryResult:
        """Retrieves models for a specified project.

        Args:
            project_id: The project which the data belongs to.
            limit: A maximum number of models to return.
            offset: Return models after the `offset`th row in the database.
            tag: Return only those models that have the specified `tag`.

        Returns:
            The paginated models matching the given query and the total
            number of results for the given query.
        """

        models = self.query(Model).filter(Model.project_id == project_id)

        if tag:
            models = models.filter(Model.tags.any(ModelTag.tag == tag))

        total_number_models = models.count()

        # Order by name and trained at in case two models were trained at the same time
        models = (
            models.order_by(Model.trained_at.desc(), Model.name.asc())
            .offset(offset)
            .limit(limit)
            .all()
        )

        minimum_compatible_version = await self.minimum_compatible_version()

        out = []
        for m in models:
            model_version = m.version
            model = m.as_dict()
            model["is_compatible"] = self.is_model_compatible(
                minimum_compatible_version, model_version=model_version
            )
            out.append(model)

        return QueryResult(out, total_number_models)

    def get_model_count(self) -> int:
        """Returns the number of models available."""
        return self.query(Model).count()

    @staticmethod
    def extract_domain_from_model(model_path: Text) -> Optional[Dict[Text, Any]]:
        from rasa.utils.io import read_yaml_file
        from rasa.constants import DEFAULT_DOMAIN_PATH

        temp_model_dir = rasa.model.unpack_model(model_path)
        domain_path = os.path.join(temp_model_dir, "core", DEFAULT_DOMAIN_PATH)

        try:
            return read_yaml_file(domain_path)
        except (FileNotFoundError, UnicodeDecodeError) as e:
            logger.error(
                "Could not read domain file for model at "
                "'{}'. Details:\n{}".format(model_path, e)
            )
        finally:
            shutil.rmtree(temp_model_dir, ignore_errors=True)


async def discover_models() -> None:
    """Synchronize model metadata with models stored on disk."""

    from rasax.community.database import utils as db_utils

    if config.LOCAL_MODE:
        from rasax.community.local import LOCAL_MODELS_DIR  # pytype: disable=pyi-error

        model_directory = LOCAL_MODELS_DIR
    else:
        model_directory = config.rasa_model_dir

    with db_utils.session_scope() as session:
        model_service = ModelService(model_directory, session, DEFAULT_RASA_ENVIRONMENT)
        await model_service.discover_models()

        if config.LOCAL_MODE:
            await model_service.mark_latest_as_production()
