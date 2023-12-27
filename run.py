import typer
from loguru import logger

from app import StravaMerger

app = typer.Typer()


@app.command()
def run(
    secret_path: str = typer.Option(
        ..., "--secret-path", "-s", help="Path to the JSON file with credentials."
    ),
    recipient: str = typer.Option(
        ..., "--recipient", "-r", help="Email address to send the merged activity to."
    ),
    activities_count: int = typer.Option(
        ..., "--activities-count", "-a", help="Number of recent activities to retrieve."
    ),
    output_folder: str = typer.Option(
        ..., "--output-folder", "-o", help="Folder path to save output files."
    ),
    dist_theta: float = typer.Option(
        1000.0, "--dist-theta", "-d", help="Distance threshold for merging activities."
    ),
):
    merger = StravaMerger(secret_path, dist_theta=dist_theta)
    merger.refresh_access_token()

    # Fetch activities
    old_activities = merger.get_activities(activities_count)
    logger.info(f"Fetched {len(old_activities)} activities.")

    acts_to_merge = merger.detect_merging_activities(old_activities)

    if len(acts_to_merge) == 0:
        logger.info("No activities to merge.")
        return
    to_merge_gpx = merger.fetch_gpxs(acts_to_merge)
    new_activities = merger.get_new_activities(acts_to_merge)
    merged = merger(to_merge_gpx)
    merger.save_activities(to_merge_gpx, merged, folder=output_folder)

    body = merger.get_delete_mail_body(acts_to_merge)
    merger.send_email(recipient, subject="Strava Activities to Delete", body=body)

    urls = merger.upload_activity_to_strava(merged, activities=new_activities)
    body = merger.get_confirm_mail_body(urls, new_activities)
    merger.send_email(recipient, subject="New uploaded Strava Activities", body=body)

    logger.info(
        f"Processed {len(new_activities)} activities, Saved {len(to_merge_gpx) + len(merged)} to {output_folder}"
        f" as backup. \nMerged {len(merged)} and uploaded them to Strava "
    )


if __name__ == "__main__":
    app()
