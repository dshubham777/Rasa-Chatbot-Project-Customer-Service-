from pathlib import Path
from typing import Text

from rasax.community import utils

from rasa.utils import io as io_utils

GIT_GENERATED_PRIVATE_KEY_PATH = "/tmp/private-ssh-key-for-git"
GIT_GENERATED_PUBLIC_KEY_PATH = "/tmp/public-ssh-key-for-git"


class GitSSHKeyProvider:
    """Provides generated SSH keys for the setup of the Integrated Version Control."""

    @staticmethod
    def generate_new_ssh_keys() -> None:
        """Generate a new SSH key and overwrite already existing ones."""

        from rasax.community import cryptography

        (
            private_key,
            public_key,
        ) = cryptography.generate_rsa_key_pair_in_open_ssh_format()
        GitSSHKeyProvider._store_keys_in_default_location(private_key, public_key)

    @staticmethod
    def get_public_ssh_key() -> Text:
        """Get a public SSH key which can be used to setup Integrated Version Control.

        Returns: A public SSH key which has to be provided to the Git server so that
            Rasa X can authenticate with its private key.
        """

        if not Path(GIT_GENERATED_PRIVATE_KEY_PATH).exists():
            GitSSHKeyProvider.generate_new_ssh_keys()

        return io_utils.read_file(GIT_GENERATED_PUBLIC_KEY_PATH)

    @staticmethod
    def _store_keys_in_default_location(private_key: bytes, public_key: bytes) -> None:
        utils.write_bytes_to_file(GIT_GENERATED_PRIVATE_KEY_PATH, private_key)
        utils.write_bytes_to_file(GIT_GENERATED_PUBLIC_KEY_PATH, public_key)

    @staticmethod
    def get_private_ssh_key() -> Text:
        """Get the private key which Rasa X can use to authenticate with a Git server.

        Returns: A private SSH key which can be used together with the public key which
            the user has to store at their Git Server.
        """

        if not Path(GIT_GENERATED_PRIVATE_KEY_PATH).exists():
            raise ValueError("Please request the public key first.")

        return io_utils.read_file(GIT_GENERATED_PRIVATE_KEY_PATH)
