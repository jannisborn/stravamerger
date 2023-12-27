import json
import os
import smtplib
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any, Dict, List, Optional, Tuple

import gpxpy.gpx
import requests
from gpxpy.gpx import GPXTrack, GPXTrackSegment
from loguru import logger
from tqdm import tqdm

from utils import NAME_DICT, CustomGPX, haversine, parse_date

TODAY = datetime.today().strftime("%Y-%m-%d")


@dataclass
class Activity:
    name: str
    id: int
    start_date: str
    start_coords: Tuple[float, float]
    filepath: Optional[str] = None
    sport: Optional[str] = None


class StravaMerger:
    AUTH_URL = "https://www.strava.com/oauth/token"
    ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
    ACTIVITIES_WEBURL = "https://www.strava.com/activities/"
    STREAM_URL_TEMPLATE = "https://www.strava.com/api/v3/activities/{}/streams"
    UPLOAD_URL = "https://www.strava.com/api/v3/uploads"
    SENDER_MAIL = "jannis.born@gmail.com"
    DELETE_BODY = """<html><head></head><body><p>Here are the Strava activities to be deleted:</p><ul>"""
    CONFIRM_BODY = (
        """<html><head></head><body><p>Here are the new Strava activities:</p><ul>"""
    )

    def __init__(self, secret_path: str, dist_theta: float = 1000.0):
        """
        Initializes the StravaMerger with the necessary credentials.

        Args:
            secret_path: Path to the JSON file containing the credentials.
            dist_theta: Distance threshold for merging activities.
        """

        self.dist_theta = dist_theta

        with open("secret.json", "r") as f:
            secret = json.load(f)

        self.client_id = secret["client_id"]
        self.client_secret = secret["client_secret"]
        self.access_token = secret["access_token"]
        self.refresh_token = secret["refresh_token"]
        self.authorization_code = secret["authorization_code"]
        self.mail_password = secret["mail"]

    def get_stream_url(self, activity_id: int) -> str:
        """
        Generates the URL for activity streams based on a given activity ID.

        Args:
            activity_id (int): The ID of the activity for which to retrieve the stream.

        Returns:
            str: The URL for fetching the activity stream data.
        """
        return self.STREAM_URL_TEMPLATE.format(activity_id)

    def refresh_access_token(self) -> str:
        """
        Refreshes the Strava access token using the provided refresh token.
        """

        payload = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "refresh_token": self.refresh_token,
            "grant_type": "refresh_token",
            "f": "json",
        }

        response = requests.post(self.AUTH_URL, data=payload, verify=False)
        new_token = response.json().get("access_token")
        logger.info("Access Token = {}\n".format(new_token))

        if not new_token:
            raise ValueError("Failed to refresh access token.")

        self.access_token = new_token

    def get_activities(self, num_activities: int) -> List[Dict[str, Any]]:
        """
        Retrieves a list of recent activities from Strava.

        Args:
            num_activities (int): The number of recent activities to retrieve.

        Returns:
            List[Dict[str, Any]]: A list of activities, each represented as a dictionary.
        """

        activities = []
        header = {"Authorization": f"Bearer {self.access_token}"}
        page = 1
        with tqdm(total=num_activities, desc="Fetching Activities") as pbar:
            while len(activities) < num_activities:
                params = {"per_page": min(200, num_activities), "page": page}
                response = requests.get(
                    self.ACTIVITIES_URL, headers=header, params=params
                ).json()
                if (
                    isinstance(response, dict)
                    and response["message"] == "Rate Limit Exceeded"
                ):
                    raise ValueError("Rate Limit Exceeded")
                fetched = len(response)
                pbar.update(min(fetched, num_activities - len(activities)))
                activities.extend(response)
                if len(response) < min(200, num_activities):
                    break
                page += 1
        return activities

    def detect_merging_activities(
        self, activities: List[Dict[str, Any]]
    ) -> List[Tuple[Activity, Activity]]:
        """Find and print activities that start where another ended on the same day."""

        sorted_activities = sorted(
            activities, key=lambda x: parse_date(x["start_date_local"])
        )
        matches = []
        for i, activity in enumerate(sorted_activities):
            for j, other_activity in enumerate(sorted_activities):
                if i <= j or activity.get("type") != other_activity.get("type"):
                    continue
                if parse_date(activity["start_date_local"]) != parse_date(
                    other_activity["start_date_local"]
                ):
                    continue
                end_latlng = activity.get("end_latlng")
                start_latlng = other_activity.get("start_latlng")
                dist = haversine(end_latlng, start_latlng)
                if dist < self.dist_theta:
                    logger.info(
                        f"Match found: \n\tActivity 1: {activity['name']} on {activity['start_date_local']} with {activity['id']}\n\t"
                        + f"Activity 2: {other_activity['name']} on {other_activity['start_date_local']} with {other_activity['id']}"
                    )
                    first = Activity(
                        name=activity["name"],
                        id=activity["id"],
                        start_date=activity["start_date_local"],
                        start_coords=activity["start_latlng"],
                        sport=activity["type"],
                    )
                    second = Activity(
                        name=other_activity["name"],
                        id=other_activity["id"],
                        start_date=other_activity["start_date_local"],
                        start_coords=other_activity["start_latlng"],
                        sport=other_activity["type"],
                    )
                    matches.append((first, second))
                else:
                    logger.info(
                        f"No match found between {activity['id']} ({activity['name']}) and {other_activity['id']}"
                        + f"({other_activity['name']}), distance was {dist}. Links: "
                        + f"{os.path.join(self.ACTIVITIES_URL, str(activity['id']))} and "
                        + f"{os.path.join(self.ACTIVITIES_URL, str(other_activity['id']))}"
                    )

        return matches

    def activity_to_gpx(
        self, activity_id: int, sport: str, start_time: str = None
    ) -> CustomGPX:
        """
        Fetches activity streams (lat-long, time, altitude) for a given activity ID from Strava.

        Args:
            activity_id (int): The ID of the activity for which to retrieve the stream.
            sport (str): The sport type of the activity.
            start_time (str): The start time of the activity in ISO format.

        Returns:
            CustomGPX: The GPX object representing the activity.
        """

        header = {"Authorization": "Bearer " + self.access_token}
        stream_url = self.get_stream_url(activity_id)

        # Fetch each data stream separately and handle the potential absence of any data stream
        def get_stream_data(key):
            response = requests.get(stream_url, headers=header, params={"keys": [key]})
            idx = 0 if key in ["latlng", "temp"] else 1

            if response.status_code == 200 and key in [
                e["type"] for e in response.json()
            ]:
                stream = response.json()[idx]["data"]
                dtype = response.json()[idx]["type"]
                if key != dtype:
                    for i in response.json():
                        for k, v in i.items():
                            print(k, v)
                    raise ValueError(
                        f"Series type does not match key: {key} and {dtype}"
                    )
            else:
                logger.warning(
                    f"Could not fetch {key} stream for activity {activity_id}"
                )
                stream = [None] * len(latlong)

            return stream

        latlong = get_stream_data("latlng")
        altitude = get_stream_data("altitude")
        heartrate = get_stream_data("heartrate")
        atemp = get_stream_data("temp")
        time_list = get_stream_data("time")

        # Creates a GPX object from activity data
        gpx = CustomGPX()
        gpx_track = gpxpy.gpx.GPXTrack()
        gpx.tracks.append(gpx_track)
        gpx_segment = gpxpy.gpx.GPXTrackSegment()
        gpx_track.segments.append(gpx_segment)
        start_time = datetime.strptime(start_time, "%Y-%m-%dT%H:%M:%SZ")
        gpx.set_sport(sport)

        for i, (lat, lon) in enumerate(latlong):
            point = gpxpy.gpx.GPXTrackPoint(
                latitude=lat,
                longitude=lon,
                elevation=altitude[i],
                time=(start_time + timedelta(seconds=time_list[i])),
            )

            # Add extensions
            extensions = ET.Element("gpxtpx:TrackPointExtension")
            if heartrate[i] is not None:
                gpx_hr = ET.SubElement(extensions, "gpxtpx:hr")
                gpx_hr.text = str(heartrate[i])
            if atemp[i] is not None:
                gpx_temp = ET.SubElement(extensions, "gpxtpx:atemp")
                gpx_temp.text = str(atemp[i])

            point.extensions.append(extensions)
            gpx_segment.points.append(point)

        return gpx

    def fetch_gpxs(
        self, acts_to_merge: List[Tuple[Activity, Activity]]
    ) -> List[Tuple[CustomGPX, CustomGPX]]:
        """Fetches and returns GPX data for pairs of activities to be merged.

        Args:
            acts_to_merge: List of tuples of activities to be merged.

        Returns:
           List[Tuple[CustomGPX, CustomGPX]] : List of tuples of GPX objects to be merged.

        """
        gpxs = []
        with tqdm(total=len(acts_to_merge), desc="Fetching GPX Data") as pbar:
            for i, (a1, a2) in enumerate(acts_to_merge):
                pbar.set_postfix_str(
                    f"Processing activity pair {i+1}/{len(acts_to_merge)}: {a1.id} and {a2.id}"
                )
                pbar.update(1)
                gpx1 = self.activity_to_gpx(
                    a1.id, start_time=a1.start_date, sport=a1.sport
                )
                gpx2 = self.activity_to_gpx(
                    a2.id, start_time=a2.start_date, sport=a2.sport
                )

                gpxs.append((gpx1, gpx2))

        return gpxs

    def merge_gpx(self, gpx1: gpxpy.gpx.GPX, gpx2: gpxpy.gpx.GPX) -> gpxpy.gpx.GPX:
        """
        Merges two GPX objects into one in the order of their starting times.

        Args:
            gpx1 (gpxpy.gpx.GPX): The first GPX object.
            gpx2 (gpxpy.gpx.GPX): The second GPX object.

        Returns:
            gpxpy.gpx.GPX: The merged GPX object in chronological order.
        """
        # Find the starting times of each GPX track
        start_time1 = (
            gpx1.tracks[0].segments[0].points[0].time
            if gpx1.tracks and gpx1.tracks[0].segments
            else None
        )
        start_time2 = (
            gpx2.tracks[0].segments[0].points[0].time
            if gpx2.tracks and gpx2.tracks[0].segments
            else None
        )

        # Determine the order based on start times
        first_gpx, second_gpx = (
            (gpx1, gpx2) if start_time1 < start_time2 else (gpx2, gpx1)
        )

        # Merge in chronological order
        merged_gpx = CustomGPX()
        merged_track = GPXTrack()
        merged_segment = GPXTrackSegment()
        merged_gpx.set_sport(first_gpx.sport)

        # Function to add points from a track to the merged segment
        def add_points_from_track(gpx):
            for track in gpx.tracks:
                for segment in track.segments:
                    merged_segment.points.extend(segment.points)

        # Add points from both GPX objects
        add_points_from_track(gpx1)
        add_points_from_track(gpx2)

        # Add the merged segment to the merged track, and the track to the GPX
        merged_track.segments.append(merged_segment)
        merged_gpx.tracks.append(merged_track)

        return merged_gpx

    def __call__(self, to_merge_gpx: List) -> List[gpxpy.gpx.GPX]:
        """Merges pairs of activities into one activity."""
        merged = []
        for gpx1, gpx2 in to_merge_gpx:
            merged_gpx = self.merge_gpx(gpx1, gpx2)
            merged.append(merged_gpx)
        logger.info(f"Merged {len(merged)} activities.")
        return merged

    def get_new_activities(
        self, acts: List[Tuple[Activity, Activity]]
    ) -> List[Activity]:
        """Returns a list of new activities to be uploaded to Strava."""
        new_activities = []

        for act1, act2 in acts:
            for loc, tname in NAME_DICT.items():
                if haversine(loc, act1.start_coords) < self.dist_theta:
                    name = tname
                    break
            else:
                name = f"StravaMerger joint {act1.sport}"

            act = Activity(
                name=name,
                id=-1,
                start_date=act1.start_date,
                start_coords=act1.start_coords,
                sport=act1.sport,
            )
            new_activities.append(act)
        return new_activities

    def save_activities(self, to_merge_gpx: list, merged_activities: list, folder: str):
        """
        Saves the original and merged activities as GPX files in a specified root folder.
        Creates the folder if it does not exist.

        Args:
            to_merge_gpx (list): List of tuples containing original GPX objects to be merged.
            merged_activities (list): List of merged GPX objects.
            folder (str): Path to the root folder where files will be saved.
        """
        # Create the root folder if it does not exist
        if not os.path.exists(folder):
            os.makedirs(folder)

        for idx, ((gpx1, gpx2), merged_gpx) in enumerate(
            zip(to_merge_gpx, merged_activities)
        ):
            # Define file paths
            original_file_1 = os.path.join(folder, f"original_activity_{idx}_first.gpx")
            original_file_2 = os.path.join(
                folder, f"original_activity_{idx}_second.gpx"
            )
            merged_file = os.path.join(folder, f"merged_activity_{idx}.gpx")
            merged_gpx.set_filepath(merged_file)

            # Save the original activities
            with open(original_file_1, "w") as file:
                file.write(gpx1.to_xml())
            with open(original_file_2, "w") as file:
                file.write(gpx2.to_xml())

            # Save the merged activity
            with open(merged_file, "w") as file:
                file.write(merged_gpx.to_xml())

        logger.info(
            f"Saved {len(to_merge_gpx)*2} original activities & their {len(merged_activities)} merged versions in {folder}"
        )

    def get_delete_mail_body(self, activities: List[Tuple[Activity, Activity]]) -> str:
        body = self.DELETE_BODY
        for act1, act2 in activities:
            first_link = f"https://www.strava.com/activities/{act1.id}"
            second_link = f"https://www.strava.com/activities/{act2.id}"
            body += f"<li><a href='{first_link}'>{act1.name} (Start Date: {act1.start_date})</a></li>"
            body += f"<li><a href='{second_link}'>{act2.name} (Start Date: {act2.start_date})</a></li><br>"
        return body

    def get_confirm_mail_body(self, urls: List[str], activities: List[Activity]) -> str:
        body = self.CONFIRM_BODY
        for act, url in zip(activities, urls):
            body += f"<li><a href='{url}'>{act.name} (Start Date: {act.start_date})</a></li>"
        return body

    def send_email(
        self,
        recipient_email: str,
        subject: str,
        body: str,
    ):
        """Sends an email with a list of Strava activities to be deleted.

        Args:
            recipient_email (str): The email address of the recipient.
            subject (str): The subject of the email.
            body (str): The body of the email.
        """
        message = MIMEMultipart()

        message["From"] = self.SENDER_MAIL
        message["To"] = recipient_email
        message["Subject"] = subject

        message.attach(MIMEText(body, "html"))
        # SMTP server setup (example with Gmail)
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(self.SENDER_MAIL, self.mail_password)

        # Sending the email
        server.send_message(message)
        server.quit()

        logger.info("Email sent to {}".format(recipient_email))

    def check_upload_status(self, upload_id: int) -> dict:
        """
        Checks the status of an upload on Strava.

        Args:
            upload_id (str): The upload ID received from the upload activity response.

        Returns:
            dict: Response from Strava API regarding the upload status.
        """
        while True:
            response = requests.get(
                os.path.join(self.UPLOAD_URL, upload_id),
                headers={"Authorization": f"Bearer {self.access_token}"},
            )
            status = response.json()
            if status["status"] == "Your activity is ready.":
                return response
            elif status["status"] == "There was an error processing your activity.":
                return response
            if status["status"] == "Your activity is still being processed.":
                pass

            time.sleep(3)  # Sleep for a short interval before checking again

    def upload_activity_to_strava(
        self,
        filedata: List[CustomGPX],
        activities: List[Activity],
        data_type: str = "gpx",
        description: str = "",
    ) -> List[str]:
        """
        Uploads an activity file to Strava.

        Args:
            filedata (List[CustomGPX]): List of GPX objects to upload.
            activities (List[Activity]): List of activities with metadata.
            data_type (str): Type of the activity file ('fit', 'tcx', or 'gpx').
            description (str): Description of the activity.

        Returns:
            List[str]: List of URLs to the uploaded activities.
        """
        assert len(filedata) == len(activities), f"{len(filedata)} != {len(activities)}"
        success = [False] * len(filedata)

        tries = 0
        urls = []
        while any(not s for s in success):
            tries += 1
            for i, (file, act) in enumerate(zip(filedata, activities)):
                if success[i]:
                    continue
                assert os.path.exists(file.filepath)
                files = {"file": open(file.filepath, "rb")}

                data = {
                    "data_type": data_type,
                    "name": act.name,
                    "description": description,
                    "trainer": 0,
                    "commute": 0,
                    "sport_type": file.sport,
                }

                response = requests.post(
                    self.UPLOAD_URL,
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    files=files,
                    data=data,
                )
                upload_id = str(response.json()["id"])
                response = self.check_upload_status(upload_id)
                status = response.json()["status"]
                if status == "Your activity is ready.":
                    url = os.path.join(
                        self.ACTIVITIES_WEBURL, str(response.json()["activity_id"])
                    )
                    logger.info(f"Uploaded {i+1}/{len(filedata)} to {url}")
                    urls.append(url)
                    success[i] = True
                elif status == "There was an error processing your activity.":
                    pass
                elif status == "Your activity is still being processed.":
                    logger.warning(
                        f"Seems there was a glitch, {i} is still being processed."
                    )
                else:
                    logger.error(f"Unknown status: {status}")

            if all(success):
                break

            # Wait for 10 minutes before checking again
            time.sleep(600)
            if tries % 10 == 0:
                logger.info(
                    f"Tried {tries} times, {sum(success)/len(success)} succeeded so far."
                )

        return urls
