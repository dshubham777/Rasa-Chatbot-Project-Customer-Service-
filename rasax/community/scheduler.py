import logging
from typing import Text, Union, Dict, Optional, TYPE_CHECKING, Set, Any, List
from datetime import datetime, timedelta

from apscheduler.job import Job
from apscheduler.schedulers.background import BackgroundScheduler
from pytz import UnknownTimeZoneError, utc

from rasax.community import utils, config
from rasax.community.services import background_dump_service
from rasax.community.services.background_dump_service import DumpService

if TYPE_CHECKING:
    from multiprocessing import Process  # type: ignore

logger = logging.getLogger(__name__)

JOB_ID_KEY = "job_id"
CANCEL_JOB_KEY = "cancel"

_job_queue = utils.mp_context().Queue()


def _schedule_background_jobs(scheduler: BackgroundScheduler) -> None:
    # Schedule Git synchronization
    if utils.is_git_available() and not config.LOCAL_MODE:
        from rasax.community.services.integrated_version_control.git_service import (
            GitService,
            GIT_BACKGROUND_JOB_ID,
        )

        scheduler.add_job(
            GitService.run_background_synchronization,
            "cron",
            id=GIT_BACKGROUND_JOB_ID,
            next_run_time=datetime.now(),
            replace_existing=True,
            minute="*",
        )

    # Schedule periodic telemetry Status event
    from rasax.community import telemetry

    if telemetry.is_telemetry_enabled():
        scheduler.add_job(
            telemetry.track_project_status,
            "interval",
            seconds=config.telemetry_status_event_interval,
        )

    # Schedule analytics caching
    if utils.is_enterprise_installed():
        from rasax.community.services.analytics_service import AnalyticsService

        scheduler.add_job(
            AnalyticsService.run_analytics_caching,
            "cron",
            replace_existing=True,
            **config.analytics_update_kwargs,
        )


def _run_scheduler() -> None:
    try:
        logging.getLogger("apscheduler.scheduler").setLevel(logging.WARNING)
        scheduler = BackgroundScheduler()
        scheduler.start()
    except UnknownTimeZoneError:
        logger.warning(
            "apscheduler could not find a timezone and is "
            "defaulting to utc. This is probably because "
            "your system timezone is not set. "
            'Set it with e.g. echo "Europe/Berlin" > '
            "/etc/timezone"
        )
        scheduler = BackgroundScheduler(timezone=utc)
        scheduler.start()

    _schedule_background_jobs(scheduler)

    # Check regularly if a job should be executed right away
    try:
        while True:
            job_information = _job_queue.get()
            _handle_next_queue_item(scheduler, job_information)
    except KeyboardInterrupt:
        # Handle Ctrl-C in local mode
        pass


def _handle_next_queue_item(
    scheduler: BackgroundScheduler, job_information: Dict[Text, Any]
) -> None:
    job_id = job_information.pop(JOB_ID_KEY)
    existing_job: Optional[Job] = scheduler.get_job(job_id)

    should_cancel_job = job_information.pop(CANCEL_JOB_KEY, False)
    if should_cancel_job and existing_job:
        scheduler.remove_job(job_id)
        return

    if existing_job:
        _modify_job(existing_job, job_information)
    elif job_id == background_dump_service.BACKGROUND_DUMPING_JOB_ID:
        _add_job_to_dump_files(scheduler, job_information)
    else:
        logger.warning(f"Did not find a scheduled job with id '{job_id}'.")


def _add_job_to_dump_files(
    scheduler: BackgroundScheduler, job_information: Dict[Text, Union[bool, Dict, Set]]
) -> None:
    dumping_delay = datetime.now() + timedelta(
        seconds=config.MAX_DUMPING_DELAY_IN_SECONDS
    )

    scheduler.add_job(
        DumpService.dump_changes,
        "date",
        run_date=dumping_delay,
        id=background_dump_service.BACKGROUND_DUMPING_JOB_ID,
        kwargs=job_information,
    )
    logger.debug("Created job to dump files in the background.")


def _modify_job(
    background_job: Job, job_modification: Dict[Text, Union[bool, Dict, Set]]
) -> None:
    changes = {}
    job_id = background_job.id
    run_immediately = job_modification.pop("run_immediately", False)

    if run_immediately:
        changes["next_run_time"] = datetime.now()
        logger.debug(f"Running job with id '{job_id}' immediately.")

    # Set keyword arguments to call scheduled job function with
    changes["kwargs"] = _get_merged_job_kwargs(background_job, job_modification)

    background_job.modify(**changes)
    logger.debug(f"Modifying job with id '{background_job.id}'.")


def _get_merged_job_kwargs(
    existing_job: Job, new_job_kwargs: Dict[Text, Union[bool, Dict, Set]]
) -> Dict:
    """Merge `kwargs` for the existing background job with new values.

    `kwargs` are the arguments which are passed as argument to the scheduled function
    which the `BackgroundScheduler` executes. Re-scheduling an existing job should not
    overwrite these values, but rather extend the values.

    Args:
        existing_job: The currently scheduled job.
        new_job_kwargs: New `kwargs` for the scheduled job which should extend the
            given `kwargs`.

    Returns:
        The merged job `kwargs`.
    """
    merged_job_modification = existing_job.kwargs or {}
    for key, updated in new_job_kwargs.items():
        current = merged_job_modification.get(key)

        if current and updated and type(current) != type(updated):
            logger.warning(
                f"Tried to update job kwargs '{key}' with a value of type "
                f"'{type(updated)}' while the current type is "
                f"'{type(current)}'."
            )
            continue

        merged_job_modification[key] = _merge_single_job_kwarg(current, updated)

    return merged_job_modification


def _merge_single_job_kwarg(
    current: Union[bool, Dict, Set, None], updated: Union[bool, Dict, Set, List, None]
) -> Union[bool, Dict, Set, List, None]:
    """Merge the value of a single `kwarg` with an updated value.

    `kwargs` are the arguments which are passed as argument to the scheduled function
    which the `BackgroundScheduler` executes. Re-scheduling an existing job should not
    overwrite these values, but rather extend the values.

    Args:
        current: The current value of a `kwarg`.
        updated: An updated value for a `kwarg`.

    Returns:
        Merged value for the `kwarg` as far as merging is implemented for type. Return
        `None` in case merging is not implemented for this type of value.
    """
    if current is None:
        return updated

    if updated is None:
        return current

    if isinstance(current, Set):
        return current | updated
    elif isinstance(current, list):
        return current + updated
    elif isinstance(current, dict):
        return {**current, **updated}
    elif isinstance(current, bool):
        return updated


def run_job_immediately(job_id: Text, **kwargs: Union[bool, Text]) -> None:
    """Trigger a scheduled background job to run immediately.

    Args:
        job_id: ID of the job which should be triggered.
        kwargs: Keyword arguments to call scheduled job function with

    """

    modify_job(job_id, run_immediately=True, **kwargs)


def modify_job(job_id: Text, **kwargs: Union[bool, Text, Dict, Set]) -> None:
    """Modify a scheduled background job.

    Args:
        job_id: ID of the job which should be modified.
        kwargs: Keyword arguments to call scheduled job function with
    """
    job_information = kwargs
    job_information[JOB_ID_KEY] = job_id
    _job_queue.put(job_information)


def start_background_scheduler() -> "Process":
    """Start a background scheduler which runs periodic tasks."""

    # Start scheduler in a separate process so that we can create a process and
    # process-safe interface by using a `Queue` to communicate with it.

    return utils.run_in_process(fn=_run_scheduler)


def cancel_job(job_id: Text) -> None:
    """Cancel any scheduled jobs with the given ID.

    Args:
        job_id: ID of the job which should be canceled.
    """
    return _job_queue.put({JOB_ID_KEY: job_id, CANCEL_JOB_KEY: True})
