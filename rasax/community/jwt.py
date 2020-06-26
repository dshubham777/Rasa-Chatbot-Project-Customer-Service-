import os
from typing import Dict, Union, Text, Optional, Tuple

import jwt
from jwt import InvalidSignatureError

import rasa.cli.utils as rasa_cli_utils
import rasa.core.constants as rasa_core_constants
import rasa.core.utils as rasa_core_utils
from rasax.community import constants, config
from rasax.community.utils import logger, file_as_bytes
from rasax.community import cryptography


def encode_jwt(payload: Dict, private_key: Union[Text, bytes]) -> Text:
    """Encodes a payload into a signed JWT."""

    return jwt.encode(payload, private_key, algorithm=constants.JWT_METHOD).decode(
        "utf-8"
    )


def bearer_token(
    payload: Dict, private_key: Union[Text, bytes] = config.jwt_private_key
) -> Text:
    """Creates a signed bearer token."""

    return rasa_core_constants.BEARER_TOKEN_PREFIX + encode_jwt(payload, private_key)


def verify_bearer_token(
    authorization_header_value: Optional[Text],
    public_key: Union[Text, bytes] = config.jwt_public_key,
) -> Dict:
    """Verifies whether a bearer token contains a valid JWT."""

    if authorization_header_value is None:
        raise TypeError("Authorization header is `None`.")
    elif rasa_core_constants.BEARER_TOKEN_PREFIX not in authorization_header_value:
        raise ValueError(
            "Authorization header is not prefixed with '{}'. Found header value "
            "'{}'.".format(
                rasa_core_constants.BEARER_TOKEN_PREFIX, authorization_header_value
            )
        )

    authorization_header_value = authorization_header_value.replace(
        rasa_core_constants.BEARER_TOKEN_PREFIX, ""
    )
    try:
        return jwt.decode(
            authorization_header_value, public_key, algorithms=constants.JWT_METHOD
        )
    except Exception:
        raise ValueError("Invalid bearer token.")


def initialise_jwt_keys() -> None:
    """Read JWT keys from file and set them. Generate keys if files are not present."""

    if os.path.isfile(config.jwt_private_key_path) and os.path.isfile(
        config.jwt_public_key_path
    ):
        logger.debug(
            "Attempting to set JWT keys from files '{}' (private key) "
            "and '{}' (public key)."
            "".format(config.jwt_private_key, config.jwt_public_key)
        )
        private_key, public_key = _fetch_and_verify_jwt_keys_from_file()
    else:

        logger.debug("Generating JWT RSA key pair.")
        private_key, public_key = cryptography.generate_rsa_key_pair()
        _ = _save_rsa_private_key_to_temporary_file(private_key)

    _set_keys(private_key, public_key)


def _save_rsa_private_key_to_temporary_file(private_key: bytes) -> Text:
    """Save RSA `private_key` to temporary file and return path."""
    from rasa.utils.io import create_temporary_file

    private_key_temp_path = create_temporary_file(private_key, mode="w+b")
    logger.debug(
        "Saved RSA private key to temporary file '{}'." "".format(private_key_temp_path)
    )
    return private_key_temp_path


def _verify_keys(private_key: bytes, public_key: bytes) -> None:
    """Sign message with private key and decode with public key."""

    encoded = jwt.encode({}, private_key, algorithm=constants.JWT_METHOD)
    _ = jwt.decode(encoded, public_key, algorithms=constants.JWT_METHOD)


def _set_keys(private_key: Union[Text, bytes], public_key: Union[Text, bytes]) -> None:
    """Update `private_key` and `public_key` in `rasax.community.config`."""

    config.jwt_private_key = private_key
    config.jwt_public_key = public_key


def _fetch_and_verify_jwt_keys_from_file(
    private_key_path: Text = config.jwt_private_key_path,
    public_key_path: Text = config.jwt_public_key_path,
) -> Tuple[bytes, bytes]:
    """Load the public and private JWT key files and verify them."""

    try:
        private_key = file_as_bytes(private_key_path)
        public_key = file_as_bytes(public_key_path)
        _verify_keys(private_key, public_key)
        return private_key, public_key
    except FileNotFoundError as e:
        error_message = f"Could not find key file. Error: '{e}'"
    except ValueError as e:
        error_message = (
            "Failed to load key data. Make sure the key "
            "files are enclosed with the "
            "'-----BEGIN PRIVATE KEY-----' etc. tags. Error: '{}'".format(e)
        )
    except InvalidSignatureError as e:
        error_message = f"Failed to verify key signature. Error: '{e}'"
    except Exception as e:
        error_message = f"Encountered error trying to verify JWT keys: '{e}'"

    rasa_cli_utils.print_error_and_exit(error_message)


def add_jwt_key_to_result(results: Dict) -> Dict:
    """Add JWT public key to a dictionary of 'version' results `results`.

    Follows basic jwks format: https://auth0.com/docs/jwks
    """

    results["keys"] = [
        {
            "alg": constants.JWT_METHOD,
            "key": rasa_core_utils.convert_bytes_to_string(config.jwt_public_key),
        }
    ]
    return results
