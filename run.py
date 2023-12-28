import typer
from app import StravaMerger
from loguru import logger

app = typer.Typer()


@app.command()
def run(
    credential_path: str = typer.Option(
        ..., "--credentials", "-c", help="Path to the JSON file with credentials."
    ),
    recipient: str = typer.Option(
        ...,
        "--recipient",
        "-r",
        help="Email address to send to-be-deleted and merged activities to.",
    ),
    sender: str = typer.Option(
        "jannis.born@gmail.com",
        "--sender",
        "-s",
        help="Email address that sends the emails.",
    ),
    activities: int = typer.Option(
        ..., "--activities", "-a", help="Number of recent activities to retrieve."
    ),
    output_folder: str = typer.Option(
        ..., "--ofolder", "-o", help="Folder path to save output files."
    ),
    distance: float = typer.Option(
        1000.0, "--distance", "-d", help="Distance threshold for merging activities."
    ),
):
    merger = StravaMerger(credential_path, sender_mail=sender, dist_theta=distance)
    merger.refresh_access_token()

    # Fetch activities
    old_activities = merger.get_activities(activities)
    logger.info(f"Fetched {len(old_activities)} activities.")

    acts_to_merge = merger.detect_merging_activities(old_activities)

    if len(acts_to_merge) == 0:
        logger.info("No activities to merge.")
        return
    to_merge_gpx = merger.fetch_gpxs(acts_to_merge)
    new_activities = merger.get_new_activities(acts_to_merge)
    merged = merger(to_merge_gpx, new_activities=new_activities)
    merger.save_activities(to_merge_gpx, merged, folder=output_folder)

    delete_body = merger.get_delete_mail_body(acts_to_merge)
    merger.send_email(
        recipient, subject="StravaMerger - Delete activities", body=delete_body
    )
    merged = merger.upload_activities_to_strava(merged)
    body = merger.get_confirm_mail_body(merged)
    merger.send_email(recipient, subject="StravaMerger - New Activities", body=body)

    logger.info(
        f"Processed {len(new_activities)} activities, Saved {len(to_merge_gpx) + len(merged)} to {output_folder}"
        f" as backup. \nMerged {len(merged)} and uploaded them to Strava "
    )


if __name__ == "__main__":
    app()
