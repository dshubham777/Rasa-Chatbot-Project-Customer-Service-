import ctypes
import sys
import json
import multiprocessing
import os
import time
import uuid

from rasax.community.constants import EVENT_CONSUMER_SEPARATION_ENV

LOCAL_MODE = False
OPEN_WEB_BROWSER = os.environ.get("OPEN_WEB_BROWSER", "true").lower() == "true"
PROCESS_START = time.time()

SYSTEM_USER = "system_user"

# Multiprocessing context to use in this Rasa X server
# See: https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods
# `None` indicates platform default
MP_CONTEXT = None

if sys.platform == "darwin" and sys.version_info < (3, 8):
    # On macOS, Python 3.8 has switched the default start method to "spawn". To
    # quote the documentation: "The fork start method should be considered
    # unsafe as it can lead to crashes of the subprocess". Apply this fix when
    # running on macOS on Python <= 3.7.x as well. Without this fix, an
    # OS-level error was triggered when trying to read multiprocessing Queues
    # from the telemetry process.

    # See:
    # https://docs.python.org/3/library/multiprocessing.html#contexts-and-start-methods
    MP_CONTEXT = "spawn"

# Platform-wide variables
rasa_x_token = os.environ.get("RASA_X_TOKEN") or uuid.uuid4().hex
password_salt = os.environ.get("PASSWORD_SALT", "salt")
project_name = os.environ.get("PROJECT_ID", "default")
self_port = int(os.environ.get("SELF_PORT", "5002"))
jwt_public_key_path = os.environ.get("JWT_PUBLIC_KEY_PATH", "/app/public_key")
jwt_private_key_path = os.environ.get("JWT_PRIVATE_KEY_PATH", "/app/private_key")
jwt_public_key = None  # placeholder, will be set on startup
jwt_private_key = None  # placeholder, will be set on startup
jwt_expiration_time = int(
    os.environ.get("JWT_EXPIRATION_TIME") or (60 * 60 * 8)  # 8 hours
)
debug_mode = os.environ.get("DEBUG_MODE", "false") == "true"
log_level = "DEBUG" if debug_mode else os.environ.get("LOG_LEVEL", "INFO")
team_name = os.environ.get("TEAM_ID", "rasa")
saml_path = os.environ.get("SAML_PATH", "/app/auth/saml")
saml_default_role = os.environ.get("SAML_DEFAULT_ROLE", "tester")
rasa_model_dir = os.environ.get("RASA_MODEL_DIR", "/app/models")
data_dir = os.environ.get("DATA_DIR", "data")
default_environments_config_path = os.environ.get(
    "DEFAULT_ENVIRONMENT_CONFIG_PATH", "environments.yml"
)
default_nlu_filename = os.environ.get("DEFAULT_NLU_FILENAME", "nlu.md")
default_stories_filename = os.environ.get("DEFAULT_STORIES_FILENAME", "stories.md")
default_domain_path = os.environ.get("DEFAULT_DOMAIN_PATH", "domain.yml")
default_config_path = os.environ.get("DEFAULT_CONFIG_PATH", "config.yml")
default_responses_filename = os.environ.get(
    "DEFAULT_RESPONSES_FILENAME", "responses.md"
)
default_e2e_tests_dir = os.environ.get("DEFAULT_E2E_TESTS_DIR", "tests")
default_e2e_test_file_path = os.environ.get(
    "DEFAULT_E2E_TEST_FILE_PATH", "conversation_tests.md"
)
default_username = os.environ.get("DEFAULT_USERNAME", "me")
development_mode = os.environ.get("DEVELOPMENT_MODE", "false").lower() == "true"
credentials_path = os.environ.get("CREDENTIALS_PATH", "/app/credentials.yml")
endpoints_path = os.environ.get("ENDPOINTS_PATH", "/app/endpoints.yml")

# Interval at which to send "Status" event, in seconds
telemetry_status_event_interval = int(
    os.environ.get("TELEMETRY_STATUS_INTERVAL") or (60 * 60)  # 1 hour
)

# The Segment write key is set from the environment. By default, it's set to
# `None`, which means that telemetry events won't be sent to Segment.
telemetry_write_key = os.environ.get("TELEMETRY_WRITE_KEY", "").strip() or None

# by default data based on platform users will be excluded from the analytics
# set to "1" if you wish to include them
rasa_x_user_analytics = bool(int(os.environ.get("RASA_X_USER_ANALYTICS", "0")))

# the number of bins in the analytics results if `window` is not specified
# in the query
default_analytics_bins = int(os.environ.get("DEFAULT_ANALYTICS_BINS", "10"))

# APscheduler config defining the update behaviour of the analytics cache. See
# https://apscheduler.readthedocs.io/en/latest/modules/triggers/cron.html
# the default value of {"hour": "*"} will run the caching once per hour
analytics_update_kwargs = json.loads(
    os.environ.get("ANALYTICS_UPDATE_KWARGS", '{"hour": "*"}')
)

# Stack variables
rasa_token = os.environ.get("RASA_TOKEN", "")

# RabbitMQ variables
rabbitmq_username = os.environ.get("RABBITMQ_USERNAME", "user")
rabbitmq_password = os.environ.get("RABBITMQ_PASSWORD", "bitnami")
rabbitmq_host = os.environ.get("RABBITMQ_HOST", "localhost")
rabbitmq_port = os.environ.get("RABBITMQ_PORT", "5672")
rabbitmq_queue = os.environ.get("RABBITMQ_QUEUE", "rasa_production_events")

# whether the Pika event consumer should be run from within rasa X or as a
# separate service
should_run_event_consumer_separately = (
    os.environ.get(EVENT_CONSUMER_SEPARATION_ENV, "false").lower() == "true"
)

# SSL configuration
ssl_certificate = os.environ.get("RASA_X_SSL_CERTIFICATE")
ssl_keyfile = os.environ.get("RASA_X_SSL_KEYFILE")
ssl_ca_file = os.environ.get("RASA_X_SSL_CA_FILE")
ssl_password = os.environ.get("RASA_X_SSL_PASSWORD")

# Feature Flags which only apply to local mode
DEFAULT_LOCAL_FEATURE_FLAGS = []
# Feature Flags which only apply to server mode
DEFAULT_SERVER_FEATURE_FLAGS = []

# Path to the current project directory which contains domain, training data and so on
# Use a `multiprocessing.Array` to ensure that changes are reflected across
# multiple Sanic workers.
PROJECT_DIRECTORY = multiprocessing.get_context(MP_CONTEXT).Array(ctypes.c_char, 300)

SANIC_RESPONSE_TIMEOUT_IN_SECONDS = int(
    os.environ.get("SANIC_RESPONSE_TIMEOUT", "3600")
)
SANIC_ACCESS_CONTROL_MAX_AGE = int(
    os.environ.get("SANIC_ACCESS_CONTROL_MAX_AGE") or (60 * 30)  # 30 minutes
)
SANIC_REQUEST_MAX_SIZE_IN_BYTES = int(
    os.environ.get("SANIC_REQUEST_MAX_SIZE_IN_BYTES") or 800000000  # 800 MB
)

# When a file has to be dumped to disk it will be done with a delay of at most
# `BACKGROUND_DUMP_SERVICE_DELAY_IN_SECONDS`.
MAX_DUMPING_DELAY_IN_SECONDS = int(os.environ.get("MAX_DUMPING_DELAY_IN_SECONDS") or 60)
