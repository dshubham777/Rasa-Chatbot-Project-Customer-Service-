import logging
import os
import shutil
import uuid
import warnings
from pathlib import Path
from typing import Text, Dict, Union, Optional, List, Iterable

import rasax.community.initialise  # pytype: disable=import-error
import rasax.community.services.data_service
from git import Repo, Reference, PushInfo, RemoteReference, Actor
from rasax.community import config, utils, telemetry
from rasax.community.config import SYSTEM_USER
from rasax.community.services.integrated_version_control.ssh_key_provider import (
    GitSSHKeyProvider,
)
from sqlalchemy.orm import Session
from rasa.utils import io as io_utils

from rasax.community.constants import DEFAULT_GIT_REPOSITORY_DIRECTORY
from rasax.community.database.admin import GitRepository
from rasax.community.database.service import DbService

logger = logging.getLogger(__name__)

DEFAULT_TARGET_BRANCH = "master"

DEFAULT_REMOTE_NAME: Text = "origin"
GIT_BACKGROUND_JOB_ID = "git-synchronization"
SSH_FILES_DIRECTORY = "ssh_files"
DEFAULT_COMMIT_MESSAGE = "Rasa X annotations"

PUSH_RESULT_BRANCH_KEY = "remote_branch"
PUSH_RESULT_PUSHED_TO_TARGET_BRANCH_KEY = "committed_to_target_branch"
REPOSITORY_SSH_FIELD_KEY = "ssh_key"
REPOSITORY_USE_GENERATED_KEYS_KEY = "use_generated_ssh_keys"

COMMIT_AUTHOR = Actor("Rasa X", "rasa-x@rasa.com")


class GitService(DbService):
    def __init__(
        self,
        session: Session,
        directory_for_git_clones: Union[Text, Path] = DEFAULT_GIT_REPOSITORY_DIRECTORY,
        project_id: Text = config.project_name,
        repository_id: Optional[int] = None,
    ) -> None:
        """Creates a `GitService` instance for a given repository.

        Args:
            session: The database session.
            directory_for_git_clones: Parent directory which should be used to store
                the cloned repositories.
            project_id: The ID of the project.
            repository_id: The ID of the repository which this service should be
                used with.
        """

        super().__init__(session)
        self._directory_for_git_clones = self._create_directory_for_git_clones(
            directory_for_git_clones
        )

        self._project: Text = project_id

        self._repository: Optional[Repo] = None
        self._repository_id: Optional[int] = None
        if repository_id:
            self.set_repository(repository_id)

    @staticmethod
    def _create_directory_for_git_clones(
        directory_for_git_clones: Union[Text, Path]
    ) -> Path:
        try:
            io_utils.create_directory(directory_for_git_clones)
        except OSError as e:
            # This should only happen when running tests locally
            logger.error(e)
            import tempfile

            directory_for_git_clones = tempfile.mkdtemp()

        return Path(directory_for_git_clones)

    def _target_branch(self) -> Text:
        return self._get_repository(self._repository_id).target_branch

    def get_current_branch(self) -> Text:
        """Get the currently active Git branch.

        Returns:
            Name of the currently active branch.
        """

        return self._repository.active_branch.name

    def create_branch(self, branch_name: Text) -> None:
        """Creates a new branch (does not check it out).

        Args:
            branch_name: Name of the branch.
        """

        self._repository.create_head(branch_name)

    def checkout_branch(
        self, branch_name: Text, force: bool = False, inject_changes: bool = True
    ) -> None:
        """Checks out a branch (`git checkout`).

        Args:
            branch_name: Name of the branch which should be checked out.
            force: If `True` discard any local changes before switching branch.
            inject_changes: If `True` the latest data is injected into the database.
        """

        self._fetch()

        try:
            matching_branch: Reference = next(
                branch
                for branch in self._repository.refs
                if branch.name == branch_name
                or branch.name == self._remote_branch_name(branch_name)
            )
        except StopIteration:
            raise ValueError(f"Branch '{branch_name}' does not exist.")

        if isinstance(matching_branch, RemoteReference):
            # Create local branch from remote branch
            matching_branch = self._repository.create_head(branch_name, matching_branch)

        # Reset to branch after remote branch was checkout out as local branch
        if force and self.is_dirty():
            self._discard_any_changes()

        matching_branch.checkout()
        logger.debug(f"Checked out branch '{branch_name}'.")

        if inject_changes:
            self.trigger_immediate_project_synchronization()

    def _fetch(self) -> None:
        self._repository.git.fetch()

    def _discard_any_changes(self) -> None:
        from rasax.community.services import background_dump_service

        logger.debug("Resetting local changes.")

        # Skip dump remaining changes
        background_dump_service.cancel_pending_dumping_job()
        # Remove untracked files
        self._repository.git.clean("-fd")
        # Undo changes to tracked files
        self._reset_to(self._target_branch())

    def _reset_to(self, branch_name: Text) -> None:
        """Reset the current branch to the state of a specific branch.

        Args:
            branch_name: Branch which the repository should be reset to.
        """

        self._repository.git.reset("--hard", branch_name)

    def is_dirty(self) -> bool:
        """Check if there are uncommitted changes.

        Uses the `first_annotator_id` field to check since data changes might not
        have been dumped to disk yet.

        Returns:
            `True` if there are uncommitted changes, otherwise `False`.
        """
        if not self._repository_id:
            return False

        return self._get_repository().first_annotator_id is not None or self._repository.is_dirty(
            untracked_files=True
        )

    def save_repository(
        self, repository_information: Dict[Text, Union[Text, int]]
    ) -> Dict[Text, Union[Text, int]]:
        """Saves the credentials for a repository.

        Args:
            repository_information: The information for the repository as `Dict`.

        Returns:
             The saved repository information including database ID.

        """

        repository_credentials = self._get_repository_from_dict(repository_information)

        if not self.has_required_remote_permission(
            repository_credentials.repository_url, repository_credentials.ssh_key
        ):
            raise ValueError(
                "Given repository credentials don't provide write "
                "permissions to the repository. Please make sure the ssh "
                "key is correct and the administrator of the remote "
                "repository gave you the required permissions."
            )

        self.add(repository_credentials)
        self.flush()  # to assign database id

        self._add_ssh_credentials(
            repository_credentials.ssh_key, repository_credentials.id
        )

        io_utils.create_directory(self.repository_path(repository_credentials.id))
        self.set_repository(repository_credentials.id)

        was_created_via_ui = bool(
            repository_information.get(REPOSITORY_USE_GENERATED_KEYS_KEY)
        )
        telemetry.track_repository_creation(
            repository_credentials.target_branch, was_created_via_ui
        )

        return repository_credentials.as_dict()

    def _get_repository_from_dict(
        self,
        repository_information: Dict[Text, Union[Text, int]],
        existing: Optional[GitRepository] = None,
    ) -> GitRepository:
        if repository_information.get(REPOSITORY_USE_GENERATED_KEYS_KEY):
            self._insert_generated_private_ssh_key(repository_information)

        if not existing:
            existing = GitRepository(
                target_branch=DEFAULT_TARGET_BRANCH,
                is_target_branch_protected=False,
                project_id=self._project,
            )

        for key, value in repository_information.items():
            setattr(existing, key, value)

        return existing

    @staticmethod
    def _insert_generated_private_ssh_key(
        repository_information: Dict[Text, Union[Text, int]]
    ) -> None:
        repository_information[
            REPOSITORY_SSH_FIELD_KEY
        ] = GitSSHKeyProvider.get_private_ssh_key()

    def _add_ssh_credentials(
        self, ssh_key: Text, repository_id: int
    ) -> Dict[Text, Text]:
        base_path = self._directory_for_git_clones / SSH_FILES_DIRECTORY
        io_utils.create_directory(base_path)

        path_to_key = base_path / f"{repository_id}.key"
        path_to_executable = self._ssh_executable_path(repository_id)

        return self._add_ssh_credentials_to_disk(
            ssh_key, path_to_key, path_to_executable
        )

    @staticmethod
    def _add_ssh_credentials_to_disk(
        ssh_key: Optional[Text], path_to_key: Path, path_to_executable: Path
    ) -> Dict[Text, Text]:
        if not ssh_key:
            return {}

        GitService._save_ssh_key(ssh_key, path_to_key)
        GitService._save_ssh_executable(path_to_executable, path_to_key)

        return {"GIT_SSH": str(path_to_executable)}

    @staticmethod
    def _save_ssh_key(ssh_key: Text, path: Path) -> None:
        utils.write_text_to_file(path, f"{ssh_key}\n")

        # We have to be able to read and write this
        path.chmod(0o600)

    @staticmethod
    def _save_ssh_executable(path: Path, path_to_key: Path) -> None:
        command = f"""
#!/bin/sh
ID_RSA={path_to_key}
# Kubernetes tends to reset file permissions on restart. Hence, re-apply the required
# permissions whenever using the key.
chmod 600 $ID_RSA
exec /usr/bin/ssh -o UserKnownHostsFile=/dev/null -o StrictHostKeyChecking=no -i $ID_RSA "$@"
"""

        utils.write_text_to_file(path, command)

        # We have to be able read, write and execute the file
        path.chmod(0o700)

    def _ssh_executable_path(self, repository_id: int) -> Path:
        return (
            self._directory_for_git_clones / SSH_FILES_DIRECTORY / f"{repository_id}.sh"
        )

    def get_repositories(self) -> List[Dict[Text, Union[Text, int]]]:
        """Retrieves the stored repository information from the database.

        Returns:
            Stored git credentials (not including ssh keys and access tokens).
        """

        repositories = (
            self.query(GitRepository)
            .filter(GitRepository.project_id == self._project)
            .order_by(GitRepository.id)
            .all()
        )
        serialized_repositories = [credential.as_dict() for credential in repositories]

        return serialized_repositories

    def _get_most_recent_repository(self) -> Optional[GitRepository]:
        """Returns the credentials for the current working directory.

        Returns:
            `None` if no repository information has been stored, otherwise a `Dict` of the
            provided repository details (not including ssh keys and access tokens)..
        """

        return (
            self.session.query(GitRepository).order_by(GitRepository.id.desc()).first()
        )

    def get_repository(self) -> Dict[Text, Union[Text, int]]:
        """Retrieve the repository information for the current repository."""

        return self._get_repository(self._repository_id).as_dict()

    def _get_repository(self, repository_id: Optional[int] = None) -> GitRepository:
        repository_id = repository_id or self._repository_id
        result = (
            self.query(GitRepository).filter(GitRepository.id == repository_id).first()
        )
        if not result:
            raise ValueError(f"Repository with ID '{repository_id}' does not exist.")
        else:
            return result

    def update_repository(
        self, repository_information: Dict[Text, Union[Text, int]]
    ) -> Dict[Text, Union[Text, int]]:
        """Updates the current repository with new credentials.

        Args:
            repository_information: The values which overwrite the currently
                stored information.

        Returns:
            The repository with the updated fields.
        """

        old = self._get_repository(self._repository_id)
        old_target_branch = old.target_branch
        updated = self._get_repository_from_dict(repository_information, old)

        if old_target_branch != updated.target_branch:
            self._fetch()
            self.checkout_branch(old.target_branch)

        return updated.as_dict()

    def delete_repository(self) -> None:
        """Deletes the current repository from the database and the file system."""

        result = self._get_repository(self._repository_id)
        self.delete(result)

        # Generate new keys for the next repository
        GitSSHKeyProvider.generate_new_ssh_keys()

        # Delete cloned directory
        shutil.rmtree(self.repository_path())

    def clone(self) -> None:
        """Clone the current repository."""

        repository_info = self._get_repository(self._repository_id)
        target_path = self.repository_path(repository_info.id)

        if self._repository or self._is_git_directory(target_path):
            raise ValueError(f"'{target_path}' is already a git directory.")

        repository_url = repository_info.repository_url
        logger.info(f"Cloning git repository from URL '{repository_url}'.")

        # Configure authentication
        # TODO: Add option to use token for cloning
        # (https://help.github.com/en/github/authenticating-to-github/creating-a-personal-access-token-for-the-command-line)
        environment = self._add_ssh_credentials(
            repository_info.ssh_key, repository_info.id
        )

        self._repository_id = repository_info.id
        # We don't have to handle errors here cause we are already doing that when
        # saving the repository.
        # Do a shallow clone of repository which means that we retrieve ony the latest
        # commit of each branch (this is faster than getting the complete history).
        self._repository = Repo.clone_from(
            repository_url, target_path, env=environment, depth=1, no_single_branch=True
        )

        self.checkout_branch(repository_info.target_branch, inject_changes=False)

        logger.debug(f"Finished cloning git repository '{repository_url}'.")

    def repository_path(self, repository_id: Optional[int] = None) -> Path:
        """Returns the path to the current repository on disk.

        Args:
            repository_id: If a repository id is given, it returns the path to this
                repository instead of the current one.

        Returns:
            Path to repository on disk.
        """

        if not repository_id and self._repository_id:
            repository_id = self._repository_id

        if repository_id:
            return self._directory_for_git_clones / str(repository_id)
        else:
            raise ValueError("No path to Git repository found.")

    def set_repository(self, repository_id: int) -> None:
        """Changes the current repository of the service to another one.

        Args:
            repository_id: ID of the repository which should be used.
        """

        directory = self._directory_for_git_clones / str(repository_id)
        self._repository_id = repository_id

        if self._is_git_directory(directory):
            self._repository = Repo(directory)
            repository = self._get_repository(repository_id)

            if repository.ssh_key:
                self._repository.git.update_environment(
                    GIT_SSH=str(self._ssh_executable_path(self._repository_id))
                )
        else:
            self._repository = None

    @staticmethod
    def _is_git_directory(directory: Union[Text, Path]) -> bool:
        return os.path.exists(directory) and ".git" in os.listdir(directory)

    def commit_to(self, branch_name: Text, message: Optional[Text]) -> None:
        """Commit local changes and push them to remote.

        Args:
            branch_name: Branch which the changes should be committed to.
            message: Commit message.

        Returns:
            Information about the commit.
        """

        if branch_name == self._target_branch():
            _ = self._commit_to_target_branch(message)
        else:
            _ = self._commit_to_new_branch(branch_name, message)
            # Change back
            self.checkout_branch(self._target_branch())

    def _commit_to_target_branch(self, message: Optional[Text]) -> Text:
        """Commits changes to the target branch.

        The target branch is e.g. `master` for 'production environment'.

        Args:
            message: The commit message. If `None` a message is generated.

        Returns:
            Name of the target branch.

        Raises:
            GitCommitError: If target branch is protected.
        """

        repository = self._get_repository()
        if repository.is_target_branch_protected:
            raise GitCommitError(
                f"'{repository.target_branch}' is a protected branch. "
                f"You cannot commit and push changes to this branch."
            )

        self._commit_changes(message)
        return repository.target_branch

    def _commit_changes(self, message: Optional[Text]) -> None:
        self._repository.git.add(A=True)
        self._repository.index.commit(message, author=COMMIT_AUTHOR)

    def _commit_to_new_branch(self, branch_name: Text, message: Optional[Text]) -> Text:
        """Commits changes to a new branch.

        Args:
            message: The commit message. If `None` a message is generated.
            branch_name: Branch which should be used.

        Returns:
            Used branch name (the name will be generated).
        """

        self.create_branch(branch_name)
        self.checkout_branch(branch_name, inject_changes=False)
        self._commit_changes(message)

        return branch_name

    def merge_latest_remote_branch(self) -> None:
        """Merges the current branch with its remote branch."""

        self._fetch()

        current_branch = self.get_current_branch()
        if not self.is_dirty() and self.is_remote_branch_ahead(current_branch):
            logger.debug(
                f"Merging remote branch '{current_branch}' into current branch '{current_branch}'."
            )
            self._repository.git.merge(self._remote_branch_name(current_branch))

    @staticmethod
    def _remote_branch_name(branch: Text) -> Text:
        return f"{DEFAULT_REMOTE_NAME}/{branch}"

    def is_remote_branch_ahead(self, other_branch: Optional[Text] = None) -> bool:
        """Checks if `other_branch` has new commits.

        Args:
            other_branch: Remote branch which should be used for comparison. If `None`,
                the remote target branch is used.

        Returns:
            `True` if the remote branch is ahead, `False` if it's not ahead.
        """

        if not other_branch:
            other_branch = self._target_branch()

        # Fetch latest remote status
        self._fetch()

        current_branch = self.get_current_branch()
        remote_branch = self._remote_branch_name(other_branch)
        commits_behind = self._repository.iter_commits(
            f"{current_branch}..{remote_branch}"
        )
        number_of_commits_behind = sum(1 for _ in commits_behind)

        logger.debug(
            f"Branch '{current_branch}' is {number_of_commits_behind} commits behind '{other_branch}'."
        )
        return number_of_commits_behind > 0

    async def commit_and_push_changes_to(
        self, branch_name: Text
    ) -> Dict[Text, Union[Text, bool]]:
        """Commit and push changes to branch.

        Args:
            branch_name: Branch to push changes to.

        Returns:
            Result of the push operation.
        """
        from rasax.community.services import background_dump_service

        await background_dump_service.wait_for_pending_changes_to_be_dumped()

        self.commit_to(branch_name, DEFAULT_COMMIT_MESSAGE)
        is_committing_to_target_branch = branch_name == self._target_branch()
        result_of_push_operation = self._push(
            branch_name, is_committing_to_target_branch
        )

        telemetry.track_git_changes_pushed(
            result_of_push_operation[PUSH_RESULT_BRANCH_KEY]
        )

        return result_of_push_operation

    def _push(
        self, branch_name: Text, pushing_to_target_branch: bool = True
    ) -> Dict[Text, Union[Text, bool]]:
        """Push committed changes.

        Args:
            branch_name: Branch which should be pushed.
            pushing_to_target_branch: If `True` it will be tried to push to a new branch
                in case pushing to the target branch fails.

        """

        push_results = self._repository.remote().push(branch_name)
        push_succeeded = self._was_push_successful(branch_name, push_results)
        if not push_succeeded and not pushing_to_target_branch:
            raise ValueError(f"Pushing to branch '{branch_name}' failed.")
        elif not push_succeeded:
            warnings.warn(
                f"Pushing to branch '{branch_name}' failed. Please "
                f"ensure that you have write permissions for this branch."
            )
            return self._try_to_push_committed_changes_to_other_branch()
        else:
            return {
                PUSH_RESULT_BRANCH_KEY: branch_name,
                PUSH_RESULT_PUSHED_TO_TARGET_BRANCH_KEY: pushing_to_target_branch,
            }

    @staticmethod
    def _was_push_successful(
        pushed_branch: Text, push_results: Iterable[PushInfo]
    ) -> bool:
        push_result_for_branch = next(
            (
                result
                for result in push_results
                if result.local_ref.name == pushed_branch
            ),
            None,
        )

        if not push_result_for_branch:
            return False
        return push_result_for_branch.flags in [
            PushInfo.UP_TO_DATE,
            PushInfo.NEW_HEAD,
            PushInfo.FAST_FORWARD,
        ]

    def _try_to_push_committed_changes_to_other_branch(
        self,
    ) -> Dict[Text, Union[Text, bool]]:
        """Tries to push committed changes to a new branch and revert the current one.

        Returns:
            The result of the attempt.
        """

        new_branch_name = f"Rasa-X-change-{uuid.uuid4()}"
        self.create_branch(new_branch_name)
        self.checkout_branch(new_branch_name, inject_changes=False)

        push_result = self._push(new_branch_name, pushing_to_target_branch=False)

        self._revert_to_remote_target_branch()

        return push_result

    def _revert_to_remote_target_branch(self) -> None:
        self.checkout_branch(self._target_branch(), inject_changes=False)
        remote_target_branch = self._remote_branch_name(self._target_branch())
        self._reset_to(remote_target_branch)
        self.trigger_immediate_project_synchronization()

    async def synchronize_project(self, force_data_injection: bool = False) -> None:
        """Synchronizes the Git repository with the database."""

        repository_info = self._get_most_recent_repository()
        if not repository_info:
            logger.debug("Skip synchronizing with Git since no credentials were given.")
            return

        # Git directory already exists - let's try to fetch the changes
        self.set_repository(repository_info.id)
        repository_path = self.repository_path(repository_info.id)

        # Set project directory to the directory which is synchronized
        utils.set_project_directory(repository_path)

        if not self._is_git_directory(repository_path):
            self.clone()
            await self._inject_data()
            return

        if force_data_injection:
            logger.debug("Data injection was forced.")
            await self._inject_data()
            # The next runs should not run with `force_data_injection=True` anymore
            self.remove_force_data_injection_flag_from_project_synchronization()
            return

        if not self.is_remote_branch_ahead():
            logger.debug(
                "Remote branch does not contain new changes. No new data "
                "needs to be injected."
            )
            return
        elif self.is_dirty():
            logger.debug(
                "Skip synchronizing with Git since the working directory "
                "contains changes. Please commit and push them in order to "
                "pull the latest changes."
            )
            return
        else:
            self.merge_latest_remote_branch()
            await self._inject_data()

    async def _inject_data(self) -> None:
        from rasax.community import local
        from rasa.constants import DEFAULT_DATA_PATH

        data_path = self.repository_path() / DEFAULT_DATA_PATH
        logger.debug("Injecting latest changes from git.")

        await rasax.community.initialise.inject_files_from_disk(
            str(self.repository_path()), str(data_path), self.session, SYSTEM_USER
        )
        self._reset_first_annotation()

    def _reset_first_annotation(self) -> None:
        if not self._repository_id:
            return

        repository = self._get_repository()

        repository.first_annotator_id = None
        repository.first_annotated_at = None

    def get_repository_status(self) -> Dict[Text, Union[Text, bool, float]]:
        """Returns the current repository status."""

        is_remote_ahead = self.is_remote_branch_ahead()
        repository = self._get_repository()

        return {
            "is_committing_to_target_branch_allowed": not is_remote_ahead,
            "is_remote_ahead": is_remote_ahead,
            "are_there_local_changes": self.is_dirty(),
            "current_branch": self.get_current_branch(),
            "time_of_last_pull": self._get_latest_fetch_time(),
            "first_annotator_id": repository.first_annotator_id,
            "first_annotated_at": repository.first_annotated_at,
        }

    def _get_latest_fetch_time(self) -> float:
        fetch_head_path = Path(self._repository.git_dir) / "FETCH_HEAD"
        if fetch_head_path.exists():
            return fetch_head_path.stat().st_mtime
        else:
            logging.debug(
                f"Failed to fetch the latest repository pull time for the repository "
                f"with ID {self._repository_id}. Using the timestamp of the latest "
                f"commit instead."
            )
            return self._repository.commit().committed_datetime.timestamp()

    def has_required_remote_permission(
        self, repository_url: Text, ssh_key: Optional[Text]
    ) -> bool:
        """Test whether authentication credentials give required permissions on remote.

        The test is done by trying to push a branch to the remote repository.

        Args:
            repository_url: URL of the repository to test.
            ssh_key: SSH key which should be used for the authentication.

        Returns:
            `True` if credentials are correct.
        """

        import tempfile

        clone_directory = Path(tempfile.mkdtemp())
        ssh_directory = Path(tempfile.mkdtemp())

        ssh_key_path = ssh_directory / "ssh.key"
        executable_path = ssh_directory / "ssh.sh"
        git_environment = self._add_ssh_credentials_to_disk(
            ssh_key, ssh_key_path, executable_path
        )

        has_permissions = False
        try:
            # Use a shallow clone to speed up the cloning.
            cloned_repository = Repo.clone_from(
                repository_url, clone_directory, env=git_environment, depth=1
            )

            # Do a change
            (clone_directory / "test_file").touch()

            # Use a new `GitService` instance to not mess with this one
            git_service = GitService(self.session, clone_directory)
            git_service._repository = cloned_repository

            import uuid

            # Push changes to new branch
            test_branch = f"Rasa-test-branch-{uuid.uuid4()}"
            git_service._commit_to_new_branch(
                test_branch,
                "This is a test in order to check if the user has write permissions "
                "in this repository.",
            )
            git_service._push(test_branch, pushing_to_target_branch=False)

            # Remove pushed changes
            git_service._delete_remote_branch(test_branch)
            has_permissions = True
        except Exception as e:
            warnings.warn(
                f"An error happened when trying to access '{repository_url}'. It seems "
                f"you don't have to correct permissions for this repository. Please "
                f"check if your credentials are correct and you have write permissions "
                f"in the given repository. The error was: {e}."
            )

        # Remove temporary directories
        shutil.rmtree(clone_directory)
        shutil.rmtree(ssh_directory)

        return has_permissions

    def _delete_remote_branch(self, branch_name: Text) -> None:
        """Deletes a remote branch."""

        self._repository.remote().push(f":{branch_name}")

    @staticmethod
    def run_background_synchronization(force_data_injection: bool = False) -> None:
        from rasax.community.database import utils as db_utils
        import asyncio  # pytype: disable=pyi-error

        logger.debug("Running scheduled Git synchronization.")

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        with db_utils.session_scope() as session:
            git_service = GitService(session, DEFAULT_GIT_REPOSITORY_DIRECTORY)
            loop.run_until_complete(
                git_service.synchronize_project(force_data_injection)
            )
        loop.close()

    def track_training_data_dumping(
        self, annotator: Text, annotation_time: float
    ) -> None:
        repository = self._get_most_recent_repository()
        if not repository:
            return

        if repository.first_annotator_id:
            return

        repository.first_annotator_id = annotator
        repository.first_annotated_at = annotation_time

    @staticmethod
    def trigger_immediate_project_synchronization() -> None:
        """Trigger an immediate synchronization of the Git repository and the database.
        """
        from rasax.community import scheduler

        scheduler.run_job_immediately(GIT_BACKGROUND_JOB_ID, force_data_injection=True)

    @staticmethod
    def remove_force_data_injection_flag_from_project_synchronization() -> None:
        """Set the `force_data_injection flag to `False` for the next synchronizations.
        """

        from rasax.community import scheduler

        scheduler.modify_job(GIT_BACKGROUND_JOB_ID, force_data_injection=False)


class GitCommitError(Exception):
    """Exception in case an error happens when trying to push changes."""
