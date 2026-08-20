import os
from collections.abc import Sequence

import typer
from loguru import logger

from app import StravaMerger, StravaRateLimitError
from automation import (
    MAX_HOLES_PER_ACTIVITY,
    JobStore,
    prepare_oldest_activity_batch,
    run_automation,
)
from utils import DEFAULT_GENERIC_NAME_PATTERNS

app = typer.Typer(no_args_is_help=True)


def run(
    *,
    credential_path: str,
    recipient: str,
    sender: str,
    n_activities: int,
    output_folder: str,
    distance: float,
    require_same_gear: bool,
    fix_holes: bool,
    hole_time_threshold: float,
    hole_distance_threshold: float,
    state_path: str | None,
    max_holes_per_activity: int = MAX_HOLES_PER_ACTIVITY,
    generic_name_patterns: Sequence[str] = DEFAULT_GENERIC_NAME_PATTERNS,
) -> None:
    merger = StravaMerger(
        credential_path,
        sender_mail=sender,
        dist_theta=distance,
        require_same_gear=require_same_gear,
        generic_name_patterns=generic_name_patterns,
    )
    try:
        merger.refresh_access_token()

        state_path = state_path or os.path.join(
            output_folder, "stravamerger-state.json"
        )
        store = JobStore(state_path)
        activities, scan_activity_ids = prepare_oldest_activity_batch(
            merger,
            store,
            n_activities,
            max_holes_per_activity=max_holes_per_activity,
        )
        summary = run_automation(
            merger,
            activities=activities,
            output_folder=output_folder,
            recipient=recipient,
            state_path=state_path,
            fix_holes=fix_holes,
            hole_time_threshold=hole_time_threshold,
            hole_distance_threshold=hole_distance_threshold,
            max_holes_per_activity=max_holes_per_activity,
            scan_activity_ids=scan_activity_ids,
            generic_name_patterns=generic_name_patterns,
        )
        store = JobStore(state_path)
        previously_screened = {
            int(activity_id)
            for activity_id in store.data["scan"]["screened_ids"]
        }
        recorded_ids = summary.screened_activity_ids | store.claimed_source_ids()
        store.record_screened(recorded_ids)
        scan = store.data["scan"]
        screened_this_run = len(
            set(scan["screened_ids"]) - previously_screened
        )
        logger.info(
            "History progress: {} source activities screened total (+{} this run), "
            "{} StravaMerger replacements excluded, {} pending; latest screened "
            "start {}.",
            scan.get("screened_count", 0),
            screened_this_run,
            scan.get("excluded_count", 0),
            scan.get("pending_count", 0),
            scan.get("last_screened_start_date") or "none",
        )
    except StravaRateLimitError as error:
        logger.error("{}", error)
        return
    logger.info(
        "Finished: {} merge replacement(s), {} repaired activity replacement(s), "
        "{} repaired hole(s), {} upload(s), {} deferred job(s).",
        summary.merged_jobs,
        summary.repaired_jobs,
        summary.repaired_holes,
        summary.uploaded_jobs,
        summary.deferred_jobs,
    )


@app.callback(invoke_without_command=True)
def merge(
    ctx: typer.Context,
    credential_path: str = typer.Option(
        "secret.json",
        "--credentials",
        "-c",
        help="Path to the JSON file with credentials.",
    ),
    recipient: str = typer.Option(
        "",
        "--recipient",
        "-r",
        help="Notification recipient; required only when --sender is set.",
    ),
    sender: str = typer.Option(
        "",
        "--sender",
        "-s",
        help="Gmail sender; omit to disable email notifications.",
    ),
    n_activities: int = typer.Option(
        ...,
        "--n_activities",
        "-n",
        help="Maximum number of oldest unscreened activities to process per run.",
    ),
    output_folder: str = typer.Option(
        ..., "--ofolder", "-o", help="Folder path to save output files."
    ),
    distance: float = typer.Option(
        1000.0, "--distance", "-d", help="Distance threshold for merging activities."
    ),
    require_same_gear: bool = typer.Option(
        False,
        "--require-same-gear",
        help="Only merge activities when all matched activities use the same gear_id.",
    ),
    fix_holes: bool = typer.Option(
        False,
        "--fix-holes/--no-fix-holes",
        help="Opt in to detecting and repairing GPS holes with Google Maps Routes.",
    ),
    hole_time_threshold: float = typer.Option(
        30.0,
        "--hole-time-threshold",
        help="Minimum time jump in seconds for a GPS hole.",
    ),
    hole_distance_threshold: float = typer.Option(
        400.0,
        "--hole-distance-threshold",
        help="Minimum straight-line distance in meters for a GPS hole.",
    ),
    max_holes_per_activity: int = typer.Option(
        MAX_HOLES_PER_ACTIVITY,
        "--max-holes-per-activity",
        min=1,
        help="Maximum number of GPS holes repaired in one activity.",
    ),
    state_path: str | None = typer.Option(
        None,
        "--state",
        help=(
            "Persistent history JSON (defaults to stravamerger-state.json "
            "in --ofolder)."
        ),
    ),
    generic_name_patterns: list[str] | None = typer.Option(
        None,
        "--generic-name-pattern",
        "--generic-name",
        help=(
            "Case-insensitive generic-title regex; repeat to replace the defaults. "
            "Each pattern must match the complete title."
        ),
    ),
):
    """Merge split activities, repair GPS holes, and upload replacements."""
    if ctx.invoked_subcommand is not None:
        return
    run(
        credential_path=credential_path,
        recipient=recipient,
        sender=sender,
        n_activities=n_activities,
        output_folder=output_folder,
        distance=distance,
        require_same_gear=require_same_gear,
        fix_holes=fix_holes,
        hole_time_threshold=hole_time_threshold,
        hole_distance_threshold=hole_distance_threshold,
        state_path=state_path,
        max_holes_per_activity=max_holes_per_activity,
        generic_name_patterns=(
            generic_name_patterns or DEFAULT_GENERIC_NAME_PATTERNS
        ),
    )


@app.command(name="run")
def run_cmd(
    credential_path: str = typer.Option(
        "secret.json",
        "--credentials",
        "-c",
        help="Path to the JSON file with credentials.",
    ),
    recipient: str = typer.Option(
        "",
        "--recipient",
        "-r",
        help="Notification recipient; required only when --sender is set.",
    ),
    sender: str = typer.Option(
        "",
        "--sender",
        "-s",
        help="Gmail sender; omit to disable email notifications.",
    ),
    n_activities: int = typer.Option(
        ...,
        "--n_activities",
        "-n",
        help="Maximum number of oldest unscreened activities to process per run.",
    ),
    output_folder: str = typer.Option(
        ..., "--ofolder", "-o", help="Folder path to save output files."
    ),
    distance: float = typer.Option(
        1000.0, "--distance", "-d", help="Distance threshold for merging activities."
    ),
    require_same_gear: bool = typer.Option(
        False,
        "--require-same-gear",
        help="Only merge activities when all matched activities use the same gear_id.",
    ),
    fix_holes: bool = typer.Option(
        False,
        "--fix-holes/--no-fix-holes",
        help="Opt in to detecting and repairing GPS holes with Google Maps Routes.",
    ),
    hole_time_threshold: float = typer.Option(
        5.0,
        "--hole-time-threshold",
        help="Minimum time jump in seconds for a GPS hole.",
    ),
    hole_distance_threshold: float = typer.Option(
        400.0,
        "--hole-distance-threshold",
        help="Minimum straight-line distance in meters for a GPS hole.",
    ),
    max_holes_per_activity: int = typer.Option(
        MAX_HOLES_PER_ACTIVITY,
        "--max-holes-per-activity",
        min=1,
        help="Maximum number of GPS holes repaired in one activity.",
    ),
    state_path: str | None = typer.Option(
        None,
        "--state",
        help=(
            "Persistent history JSON (defaults to stravamerger-state.json "
            "in --ofolder)."
        ),
    ),
    generic_name_patterns: list[str] | None = typer.Option(
        None,
        "--generic-name-pattern",
        "--generic-name",
        help=(
            "Case-insensitive generic-title regex; repeat to replace the defaults. "
            "Each pattern must match the complete title."
        ),
    ),
):
    """Alias for the default command."""
    run(
        credential_path=credential_path,
        recipient=recipient,
        sender=sender,
        n_activities=n_activities,
        output_folder=output_folder,
        distance=distance,
        require_same_gear=require_same_gear,
        fix_holes=fix_holes,
        hole_time_threshold=hole_time_threshold,
        hole_distance_threshold=hole_distance_threshold,
        state_path=state_path,
        max_holes_per_activity=max_holes_per_activity,
        generic_name_patterns=(
            generic_name_patterns or DEFAULT_GENERIC_NAME_PATTERNS
        ),
    )


def cli():
    app()


if __name__ == "__main__":
    cli()
