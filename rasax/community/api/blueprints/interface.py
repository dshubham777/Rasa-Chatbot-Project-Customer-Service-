import logging
import os
import subprocess
from multiprocessing import Process  # type: ignore
from typing import Text

import pkg_resources
from sanic import Blueprint, response
from sanic.exceptions import NotFound
from sanic.request import Request
from sanic.response import text

import rasax.community
from rasa.cli.utils import print_success, print_info, print_error
from rasax.community import utils, config, constants

logger = logging.getLogger(__name__)

PACKAGE_INTERFACE_DIRECTORY = pkg_resources.resource_filename(
    rasax.community.__name__, "interface"
)

# project root, I know, lots of .. :D
root_dir = os.path.join(
    os.path.dirname(os.path.realpath(__file__)), "..", "..", "..", "..", "..", ".."
)

BUILD_IN_PROGRESS_INDEX_HTML = (
    '<html><head><meta http-equiv="refresh" content="1"></head>'
    '<body><img src="https://cataas.com/cat/says/Frontend compiling - '
    'and now...we wait" alt="Frontend is compiling..."/><body></html>'
)


def _write_index_html(path, text):
    if not os.path.exists(path):
        os.mkdir(path)
    index = os.path.join(path, "index.html")
    utils.write_bytes_to_file(index, text.encode("utf-8"))
    return index


def build_interface() -> Text:
    """Compile the frontend using the repos make file and move it to a temp dir.

    Returns the temporary directory containing the compiled frontend."""

    frontend_directory = os.path.join(root_dir, "src", "rasa-frontend", "build")

    def run_build(cwd, output):
        if config.development_mode:
            build_cmd = "build-frontend-enterprise"
        else:
            build_cmd = "build-frontend"

        print_info("Building frontend (development mode)...")
        # this will always use the frontend enterprise build, as in this case we
        # have the source anyways (won't happen in packaged build)
        if subprocess.call(["make", "install-frontend"], cwd=cwd):
            print_error(
                "Failed to install frontend dependencies. Check logs for details."
            )
            _write_index_html(
                frontend_directory,
                "Frontend install failed! Check the logs for details.",
            )
        elif subprocess.call(["make", build_cmd], cwd=cwd):
            print_error("Failed to build frontend code. Check logs for details.")
            _write_index_html(
                frontend_directory, "Frontend build failed! Check the logs for details."
            )
        else:
            print_success(
                "Finished building frontend, serving from {}."
                "".format(os.path.abspath(output))
            )

    p = Process(target=run_build, args=(root_dir, frontend_directory))
    p.daemon = True
    p.start()

    return frontend_directory


def locate_interface() -> Text:
    """Check if there is a packaged interface - if not build it from source.

    Returns the path to the interface directory."""

    if utils.is_enterprise_installed():
        from rasax.enterprise.interface import (  # pytype: disable=import-error
            PACKAGE_ENTERPRISE_INTERFACE_DIRECTORY,
        )

        pkg_base = PACKAGE_ENTERPRISE_INTERFACE_DIRECTORY
    else:
        pkg_base = PACKAGE_INTERFACE_DIRECTORY

    pkg_index = os.path.join(pkg_base, "index.html")
    if os.path.exists(pkg_index):
        return pkg_base
    else:
        if not os.environ.get("SKIP_FRONTEND_BUILD", "false").lower() == "true":
            return build_interface()
        else:
            external_frontend = os.getenv(
                "EXTERNAL_FRONTEND",
                os.path.join(root_dir, "src", "rasa-frontend", "build"),
            )
            print_info(
                f"Using external frontend build.\nMake sure there is a frontend build "
                f"available in '{os.path.abspath(external_frontend)}'."
            )
            return external_frontend


def blueprint() -> Blueprint:
    """Serve the Rasa X interface."""

    interface_directory = locate_interface()

    index_html = os.path.join(interface_directory, "index.html")

    interface = Blueprint("interface")

    subfolders = ["static", "icons", "fonts"]

    for s in subfolders:
        interface.static(f"/{s}", os.path.join(interface_directory, s))

    @interface.route("/", methods=["GET", "HEAD", "OPTIONS"])
    async def index(request: Request):
        if os.path.exists(index_html):
            return await response.file(index_html)
        else:
            return response.html(BUILD_IN_PROGRESS_INDEX_HTML)

    @interface.exception(NotFound)
    async def ignore_404s(request: Request, exception):
        if request.path.startswith(constants.API_URL_PREFIX):
            # tries to avoid answering any API calls with
            # the frontend response
            return text("Not found.", status=404)
        logger.debug(f"Answered 404 with index.html. Request url '/': '{request.url}'")
        # we need this, as the frontend does its own routing, but needs
        # the server to always deliver the index.html to not interfere
        return await index(request)

    return interface
