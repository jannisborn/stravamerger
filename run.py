import os

import typer
from loguru import logger

from app import StravaMerger
from automation import run_automation

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
) -> None:
    merger = StravaMerger(
        credential_path,
        sender_mail=sender,
        dist_theta=distance,
        require_same_gear=require_same_gear,
    )
    merger.refresh_access_token()

    # Fetch activities
    activities = merger.get_activities(n_activities)
    logger.info(f"Fetched {len(activities)} activities.")

    state_path = state_path or os.path.join(output_folder, ".stravamerger-state.json")
    summary = run_automation(
        merger,
        activities=activities,
        output_folder=output_folder,
        recipient=recipient,
        state_path=state_path,
        fix_holes=fix_holes,
        hole_time_threshold=hole_time_threshold,
        hole_distance_threshold=hole_distance_threshold,
    )
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
        ..., "--n_activities", "-n", help="Number of recent activities to retrieve."
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
        True,
        "--fix-holes/--no-fix-holes",
        help="Detect and repair GPS holes with Google Maps Routes.",
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
    state_path: str | None = typer.Option(
        None,
        "--state",
        help="Persistent job-state JSON (defaults inside --ofolder).",
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
        ..., "--n_activities", "-n", help="Number of recent activities to retrieve."
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
        True,
        "--fix-holes/--no-fix-holes",
        help="Detect and repair GPS holes with Google Maps Routes.",
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
    state_path: str | None = typer.Option(
        None,
        "--state",
        help="Persistent job-state JSON (defaults inside --ofolder).",
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
    )


def cli():
    app()


if __name__ == "__main__":
    cli()
