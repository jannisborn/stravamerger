import json
import os
import re
import smtplib
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

import gpxpy.gpx
import requests
from gpxpy.gpx import GPXTrack, GPXTrackSegment
from loguru import logger
from tqdm import tqdm

from utils import (
    NAME_DICT,
    Activity,
    CustomGPX,
    haversine,
    parse_date,
    parse_datetime,
)

GPXTPX_NAMESPACE = "http://www.garmin.com/xmlschemas/TrackPointExtension/v1"
BOT_MARKER = "StravaMerger bot"


@dataclass
class UploadResult:
    """Outcome of one Strava file upload."""

    gpx: CustomGPX
    success: bool
    status: str
    error: str | None = None
    activity_id: int | None = None


class StravaMerger:
    AUTH_URL = "https://www.strava.com/oauth/token"
    ACTIVITIES_URL = "https://www.strava.com/api/v3/athlete/activities"
    ACTIVITIES_WEBURL = "https://www.strava.com/activities/"
    STREAM_URL_TEMPLATE = "https://www.strava.com/api/v3/activities/{}/streams"
    SINGLE_ACTIVITY_URL = "https://www.strava.com/api/v3/activities/{}"
    UPLOAD_URL = "https://www.strava.com/api/v3/uploads"

    DELETE_BODY = """<html><head></head><body><p>Here are the Strava activities to be deleted:</p><ul>"""
    CONFIRM_BODY = (
        """<html><head></head><body><p>Here are the new Strava activities:</p><ul>"""
    )

    def __init__(
        self,
        secret_path: str,
        sender_mail: str,
        dist_theta: float = 1000.0,
        hour_theta: int = 6,
        require_same_gear: bool = False,
    ):
        """
        Initializes the StravaMerger with the necessary credentials.

        Args:
            secret_path: Path to the JSON file containing the credentials.
            sender_mail: Email address of the sender.
            dist_theta: Distance threshold for merging activities.
            hour_theta: Maximal pausing between adjacent activities occuring on ADJACENT days.
            require_same_gear: If True, only merge activities with identical non-empty gear_id.
        """

        self.dist_theta = dist_theta
        self.hour_theta = hour_theta
        self.require_same_gear = require_same_gear
        self.sender_mail = sender_mail
        self.secret_path = os.path.abspath(secret_path)

        try:
            with open(self.secret_path, "r") as f:
                secret = json.load(f)
        except FileNotFoundError as e:
            raise FileNotFoundError(
                f"Credentials file not found at {secret_path!r}."
            ) from e
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Credentials file at {secret_path!r} is not valid JSON."
            ) from e

        required_keys = [
            "client_id",
            "client_secret",
            "access_token",
            "refresh_token",
        ]
        if sender_mail:
            required_keys.append("mail")
        try:
            for key in required_keys:
                secret[key]
        except KeyError as e:
            raise KeyError(
                f"Missing required key {e.args[0]!r} in credentials file {secret_path!r}."
            ) from e
        self.client_id = secret["client_id"]
        self.client_secret = secret["client_secret"]
        self.access_token = secret["access_token"]
        self.refresh_token = secret["refresh_token"]
        self.mail_password = secret.get("mail")
        self.google_maps_api_key = secret.get("google_maps_api_key")

    @staticmethod
    def check_rate_limit(response):
        """
        Raises if the rate limit has been exceeded.

        Args:
            response (requests.Response): The response from the Strava API.
        """
        if isinstance(response, requests.Response):
            if response.status_code == 429:
                raise ValueError("Rate Limit Exceeded")
            try:
                response = response.json()
            except ValueError:
                return
        if (
            isinstance(response, dict)
            and response.get("message") == "Rate Limit Exceeded"
        ):
            raise ValueError("Rate Limit Exceeded")

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

        response = requests.post(self.AUTH_URL, data=payload, timeout=30)
        self.check_rate_limit(response)
        response.raise_for_status()
        token_payload = response.json()
        new_token = token_payload.get("access_token")

        if not new_token:
            raise ValueError("Failed to refresh access token.")

        self.access_token = new_token
        self.refresh_token = token_payload.get("refresh_token", self.refresh_token)
        self._persist_tokens(token_payload)
        logger.info("Refreshed the Strava access token.")
        return new_token

    def _persist_tokens(self, token_payload: dict[str, Any]) -> None:
        """Persist Strava's rotated refresh token atomically."""
        with open(self.secret_path, "r") as file:
            secret = json.load(file)
        for key in ("access_token", "refresh_token", "expires_at"):
            if key in token_payload:
                secret[key] = token_payload[key]

        secret_dir = os.path.dirname(self.secret_path) or "."
        descriptor, temp_path = tempfile.mkstemp(
            dir=secret_dir, prefix=".stravamerger-token-", text=True
        )
        try:
            with os.fdopen(descriptor, "w") as file:
                json.dump(secret, file, indent=2)
                file.write("\n")
            os.replace(temp_path, self.secret_path)
        except Exception:
            if os.path.exists(temp_path):
                os.unlink(temp_path)
            raise

    def get_activities(self, num_activities: int) -> list[dict[str, Any]]:
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
                params = {
                    "per_page": min(200, num_activities - len(activities)),
                    "page": page,
                }
                response = requests.get(
                    self.ACTIVITIES_URL, headers=header, params=params, timeout=30
                )
                self.check_rate_limit(response)
                response.raise_for_status()
                page_activities = response.json()
                fetched = len(page_activities)
                pbar.update(min(fetched, num_activities - len(activities)))
                activities.extend(page_activities)
                if fetched < params["per_page"]:
                    break
                page += 1

        for act in tqdm(activities, desc="Getting activity details"):
            time.sleep(0.2)
            detailed_resp = requests.get(
                self.SINGLE_ACTIVITY_URL.format(act["id"]),
                headers=header,
                timeout=30,
            )
            self.check_rate_limit(detailed_resp)
            detailed_resp.raise_for_status()
            detailed_act = detailed_resp.json()
            act.clear()
            act.update(detailed_act)

        return activities[:num_activities]

    @staticmethod
    def get_end_date(start_date: str, duration: int) -> str:
        start_date_value = parse_datetime(start_date)
        end_date = start_date_value + timedelta(seconds=duration)
        return end_date.strftime("%Y-%m-%dT%H:%M:%SZ")

    @classmethod
    def activity_from_api(cls, activity: dict[str, Any]) -> Activity:
        """Convert a Strava activity response into the internal representation."""
        start_date_local = activity["start_date_local"]
        return Activity(
            name=activity["name"],
            id=activity["id"],
            start_date=start_date_local,
            start_date_utc=activity.get("start_date"),
            end_date=cls.get_end_date(start_date_local, activity["elapsed_time"]),
            start_coords=activity.get("start_latlng") or [],
            end_coords=activity.get("end_latlng") or [],
            gear_id=activity.get("gear_id"),
            sport=activity.get("sport_type") or activity.get("type"),
            description=activity.get("description") or "",
            commute=bool(activity.get("commute")),
            trainer=bool(activity.get("trainer")),
            external_id=activity.get("external_id"),
            source_ids=(activity["id"],),
        )

    @staticmethod
    def is_bot_activity(activity: dict[str, Any]) -> bool:
        return BOT_MARKER.lower() in (activity.get("description") or "").lower()

    @staticmethod
    def can_fix_activity(activity: dict[str, Any]) -> bool:
        """Return whether an activity is eligible for automatic hole detection."""
        description = (activity.get("description") or "").lower()
        return bool(activity.get("start_latlng")) and not (
            "nofix" in description
            or "nomerge" in description
            or BOT_MARKER.lower() in description
        )

    def fixed_activity_name(
        self,
        activity: Activity,
        gpx: CustomGPX | None = None,
    ) -> str:
        """Choose the name for a repaired single-activity replacement."""
        generic_name = activity.name.casefold().startswith(("fahrt am ", "lauf am "))
        if not generic_name:
            return activity.name

        def uses_location(location: tuple[float, float]) -> bool:
            if activity.start_coords and (
                haversine(location, activity.start_coords) < self.dist_theta
            ):
                return True
            if gpx is None:
                return False
            return any(
                haversine(location, (point.latitude, point.longitude)) < self.dist_theta
                for track in gpx.tracks
                for segment in track.segments
                for point in segment.points
            )

        for location, route_name in NAME_DICT.items():
            if uses_location(location):
                return route_name
        return activity.name

    def get_activity(self, activity_id: int) -> dict[str, Any] | None:
        """Fetch an owned activity, returning ``None`` after it is deleted."""
        response = requests.get(
            self.SINGLE_ACTIVITY_URL.format(activity_id),
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=30,
        )
        self.check_rate_limit(response)
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    def activity_exists(self, activity_id: int) -> bool:
        """Check whether an owned activity still exists on Strava."""
        return self.get_activity(activity_id) is not None

    def detect_merging_activities(
        self, activities: list[dict[str, Any]]
    ) -> list[list[Activity]]:
        """Find and print activities that start where another ended on the same day."""

        sorted_activities = sorted(
            activities, key=lambda x: parse_date(x["start_date_local"])
        )

        candidate_chains = []  # most will have only one activity
        for activity in sorted_activities:

            description = activity.get("description") or ""

            if "nomerge" in description.lower() or self.is_bot_activity(activity):
                continue

            activity_object = self.activity_from_api(activity)
            # For first activity
            if not candidate_chains:
                candidate_chains.append([activity_object])
                continue

            logger.debug('Considering activity "{}"', activity_object.name)
            match = False
            for candidate_chain in candidate_chains:

                last_activity = candidate_chain[-1]
                end_latlng = last_activity.end_coords
                start_latlng = activity_object.start_coords
                if start_latlng == [] or end_latlng == []:
                    # Activity without GPS footage
                    continue
                dist = haversine(end_latlng, start_latlng)
                same_day = (
                    parse_date(last_activity.start_date).date()
                    == parse_date(activity_object.start_date).date()
                )
                same_type = last_activity.sport == activity_object.sport
                same_gear = (
                    bool(last_activity.gear_id)
                    and bool(activity_object.gear_id)
                    and last_activity.gear_id == activity_object.gear_id
                )
                gear_matches = same_gear or not self.require_same_gear

                if same_day and same_type and gear_matches and dist < self.dist_theta:
                    logger.info(
                        f"Match found: \n\tActivity {activity['name']} on {activity['start_date_local']} with {activity['id']}\n\t"
                        + f"Merge with activity {last_activity.name} on {last_activity.start_date} with {last_activity.id}"
                    )
                    # Append to identified chain
                    match = True
                    candidate_chain.append(activity_object)
                    break
                elif not same_type:
                    # Try next chain
                    continue
                elif self.require_same_gear and not same_gear:
                    continue
                elif not same_day:
                    # If <6h passed between activities we consider them as adjacent
                    stop = parse_datetime(last_activity.end_date)
                    start = parse_datetime(activity_object.start_date)
                    if abs(stop - start) < timedelta(hours=self.hour_theta):
                        match = True
                        candidate_chain.append(activity_object)
                        break
                elif dist >= self.dist_theta:
                    continue
                else:
                    raise ValueError("Impossible case")

            if not match:
                # Create new chain
                candidate_chains.append([activity_object])

        merge_chains = [c for c in candidate_chains if len(c) > 1]
        return merge_chains

    def activity_to_gpx(self, activity: Activity) -> CustomGPX:
        """
        Fetches activity streams (lat-long, time, altitude) for a given activity ID from Strava.

        Args:
            activity (Activity): Activity object containing e.g., Strava ID, name, sport type
                and start time in ISO format.

        Returns:
            CustomGPX: The GPX object representing the activity.
        """

        header = {"Authorization": "Bearer " + self.access_token}
        stream_url = self.get_stream_url(activity.id)
        stream_keys = ("latlng", "altitude", "heartrate", "temp", "time")
        response = requests.get(
            stream_url,
            headers=header,
            params={"keys": ",".join(stream_keys), "key_by_type": "true"},
            timeout=30,
        )
        self.check_rate_limit(response)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            streams = {stream["type"]: stream for stream in payload}
        else:
            streams = payload

        def stream_data(key: str) -> list | None:
            stream = streams.get(key)
            if stream is None:
                logger.warning(f'Could not fetch {key} stream for "{activity.name}"')
                return None
            return stream.get("data") if isinstance(stream, dict) else None

        latlong = stream_data("latlng")
        time_list = stream_data("time")
        if not latlong or not time_list:
            raise ValueError(
                f"Activity {activity.id} has no usable latlng/time streams."
            )
        if len(latlong) != len(time_list):
            raise ValueError(
                f"Activity {activity.id} has misaligned latlng/time streams "
                f"({len(latlong)} != {len(time_list)})."
            )
        altitude = stream_data("altitude")
        heartrate = stream_data("heartrate")
        atemp = stream_data("temp")

        # Creates a GPX object from activity data
        gpx = CustomGPX()
        gpx_track = gpxpy.gpx.GPXTrack()
        gpx.tracks.append(gpx_track)
        gpx_segment = gpxpy.gpx.GPXTrackSegment()
        gpx_track.segments.append(gpx_segment)
        start_time = parse_datetime(activity.start_date_utc or activity.start_date)
        gpx.set_activity(activity)
        if heartrate is not None or atemp is not None:
            gpx.nsmap["gpxtpx"] = GPXTPX_NAMESPACE

        for i, (lat, lon) in enumerate(latlong):
            point = gpxpy.gpx.GPXTrackPoint(
                latitude=lat,
                longitude=lon,
                elevation=self._stream_value(altitude, i),
                time=(start_time + timedelta(seconds=time_list[i])),
            )

            # Add extensions
            heart_rate = self._stream_value(heartrate, i)
            temperature = self._stream_value(atemp, i)
            if heart_rate is not None or temperature is not None:
                extensions = ET.Element(f"{{{GPXTPX_NAMESPACE}}}TrackPointExtension")
                if heart_rate is not None:
                    gpx_hr = ET.SubElement(extensions, f"{{{GPXTPX_NAMESPACE}}}hr")
                    gpx_hr.text = str(heart_rate)
                if temperature is not None:
                    gpx_temp = ET.SubElement(extensions, f"{{{GPXTPX_NAMESPACE}}}atemp")
                    gpx_temp.text = str(temperature)
                point.extensions.append(extensions)
            gpx_segment.points.append(point)

        return gpx

    @staticmethod
    def _stream_value(stream: list | None, index: int):
        if stream is None or index >= len(stream):
            return None
        return stream[index]

    def fetch_gpxs(self, acts_to_merge: list[Activity]) -> list[CustomGPX]:
        """Fetches and returns GPX data for pairs of activities to be merged.

        Args:
            acts_to_merge: List of tuples of activities to be merged.

        Returns:
           List[CustomGPX] : List of GPX objects to be merged.

        """
        gpxs = []
        with tqdm(total=len(acts_to_merge), desc="Fetching GPX Data") as pbar:
            for i, act in enumerate(acts_to_merge):
                pbar.set_postfix_str(
                    f"Processing activity {i+1}/{len(acts_to_merge)}: {act.id}"
                )
                pbar.update(1)
                gpxs.append(self.activity_to_gpx(act))

        return gpxs

    def merge_gpx(self, gpx_list: list[CustomGPX]) -> CustomGPX:
        """
        Merges two GPX objects into one in the order of their starting times.

        Args:
            gpx_list: A list of all CustomGPX objects to be merged.

        Returns:
            CustomGPX: The merged GPX object in chronological order.
        """
        # Merge in chronological order
        merged_gpx = CustomGPX()
        merged_track = GPXTrack()
        merged_segment = GPXTrackSegment()

        sorted_gpx_list = sorted(gpx_list, key=self.get_start_time)

        for gpx in sorted_gpx_list:
            if "gpxtpx" in gpx.nsmap:
                merged_gpx.nsmap["gpxtpx"] = gpx.nsmap["gpxtpx"]
            for track in gpx.tracks:
                for segment in track.segments:
                    merged_segment.points.extend(segment.points)

        # Add the merged segment to the merged track, and the track to the GPX
        merged_track.segments.append(merged_segment)
        merged_gpx.tracks.append(merged_track)

        return merged_gpx

    @staticmethod
    def get_start_time(gpx: CustomGPX):
        return (
            gpx.tracks[0].segments[0].points[0].time
            if gpx.tracks and gpx.tracks[0].segments
            else None
        )

    def __call__(
        self,
        to_merge_gpx: list[list[CustomGPX]],
        new_activities: list[Activity],
    ) -> list[CustomGPX]:
        """Merges pairs of activities into one activity."""
        assert len(to_merge_gpx) == len(
            new_activities
        ), f"{len(to_merge_gpx)} != {len(new_activities)}"

        merged = []
        for i, (act, gpx_chain) in enumerate(zip(new_activities, to_merge_gpx)):
            merged_gpx = self.merge_gpx(gpx_chain)
            merged_gpx.set_activity(act)
            merged.append(merged_gpx)
        logger.info(f"Merged {len(merged)} activities.")
        return merged

    def get_new_activity(self, gpx_list: list[CustomGPX]) -> Activity:
        """Returns a list of new activities to be uploaded to Strava."""
        name = ""
        for gpx in gpx_list:
            act = gpx.activity
            for loc, tname in NAME_DICT.items():
                if haversine(loc, act.start_coords) < self.dist_theta:
                    name = tname + "&"
                    break
            else:
                name = f" {act.name} &"
        name = name[:-1]
        first_activity, last_activity = gpx_list[0].activity, gpx_list[-1].activity
        chain_gear_ids = [gpx.activity.gear_id for gpx in gpx_list]
        if chain_gear_ids and all(
            gid and gid == chain_gear_ids[0] for gid in chain_gear_ids
        ):
            merged_gear_id = chain_gear_ids[0]
        else:
            merged_gear_id = None

        current_time = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        source_ids = tuple(gpx.activity.id for gpx in gpx_list)
        act = Activity(
            name=name,
            description=(
                f"{BOT_MARKER} at {current_time}. Merged source activities: "
                + ", ".join(str(source_id) for source_id in source_ids)
            ),
            id=-1,
            start_date=first_activity.start_date,
            start_date_utc=first_activity.start_date_utc,
            end_date=last_activity.end_date,
            start_coords=first_activity.start_coords,
            end_coords=last_activity.end_coords,
            gear_id=merged_gear_id,
            sport=first_activity.sport,
            commute=all(gpx.activity.commute for gpx in gpx_list),
            trainer=all(gpx.activity.trainer for gpx in gpx_list),
            source_ids=source_ids,
            external_id="stravamerger-merge-" + "-".join(map(str, source_ids)),
        )
        return act

    def update_activity_gear(self, activity_id: int, gear_id: str):
        response = requests.put(
            self.SINGLE_ACTIVITY_URL.format(activity_id),
            headers={"Authorization": f"Bearer {self.access_token}"},
            data={"gear_id": gear_id},
            timeout=30,
        )
        try:
            payload = response.json()
        except ValueError:
            payload = {}
        self.check_rate_limit(payload)
        if response.status_code >= 300:
            logger.warning(
                f"Could not set gear_id {gear_id} for activity {activity_id}: {response.text}"
            )
        else:
            logger.info(f"Set gear_id {gear_id} for activity {activity_id}")

    def save_activities(
        self,
        to_merge_gpx: list[list[CustomGPX]],
        merged_activities: list[CustomGPX],
        folder: str,
    ):
        """
        Saves the original and merged activities as GPX files in a specified root folder.
        Creates the folder if it does not exist.

        Args:
            to_merge_gpx (list): List of lists containing original GPX objects to be merged.
            merged_activities (list): List of merged GPX objects.
            folder (str): Path to the root folder where files will be saved.
        """
        # Create the root folder if it does not exist
        if not os.path.exists(folder):
            os.makedirs(folder)

        for idx, (old_gpxs, new_gpx) in enumerate(zip(to_merge_gpx, merged_activities)):
            # Define file paths
            org_paths = []
            for i in range(len(old_gpxs)):
                o = old_gpxs[i].activity.name.replace(" ", "").strip().replace("/", "")
                org_paths.append(os.path.join(folder, f"{idx}_{i}_{o}.gpx"))
            n = new_gpx.activity.name.replace(" ", "").strip().replace("/", "")
            merged_path = os.path.join(folder, f"{idx}_{n}.gpx")
            new_gpx.activity.filepath = merged_path

            # Save the original activities
            for org_path, org_gpx in zip(org_paths, old_gpxs):
                with open(org_path, "w") as file:
                    file.write(org_gpx.to_xml())

            # Save the merged activity
            with open(merged_path, "w") as file:
                file.write(new_gpx.to_xml())

        logger.info(
            f"Saved {len(to_merge_gpx)*2} original activities & their {len(merged_activities)} merged versions in {folder}"
        )

    def save_replacement(
        self,
        originals: list[CustomGPX],
        replacement: CustomGPX,
        *,
        folder: str,
        prefix: str,
    ) -> str:
        """Save source backups and a replacement GPX, returning its path."""
        os.makedirs(folder, exist_ok=True)
        safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", prefix).strip("._")
        for original in originals:
            original_path = os.path.join(
                folder,
                f"{safe_prefix}_source_{original.activity.id}.gpx",
            )
            with open(original_path, "w") as file:
                file.write(original.to_xml())

        replacement_path = os.path.abspath(
            os.path.join(folder, f"{safe_prefix}_replacement.gpx")
        )
        with open(replacement_path, "w") as file:
            file.write(replacement.to_xml())
        replacement.activity.filepath = replacement_path
        return replacement_path

    def get_delete_mail_body(self, activity_chains: list[list[Activity]]) -> str:
        body = self.DELETE_BODY
        for activity_chain in activity_chains:
            for act in activity_chain:
                link = f"https://www.strava.com/activities/{act.id}"
                body += f"<li><a href='{link}'>{act.name} (Start Date: {act.start_date})</a></li>"
            body += "<br>"
        return body

    def get_confirm_mail_body(self, merged_gpxs: list[CustomGPX]) -> str:
        body = self.CONFIRM_BODY
        for gpx in merged_gpxs:
            body += f"<li><a href='{gpx.activity.url}'>{gpx.activity.name} (Start Date: {gpx.activity.start_date})</a></li>"
        return body

    def send_email(
        self,
        recipient_email: str,
        subject: str,
        body: str,
    ) -> bool:
        """Sends an email with a list of Strava activities to be deleted.

        Args:
            recipient_email (str): The email address of the recipient.
            subject (str): The subject of the email.
            body (str): The body of the email.
        """
        if not self.sender_mail:
            logger.info(f"Email disabled; skipped notification: {subject}")
            return False
        if not recipient_email:
            raise ValueError("Email is enabled but no recipient was configured.")
        if not self.mail_password:
            raise ValueError(
                "Email is enabled but the credentials file has no 'mail' key."
            )

        message = MIMEMultipart()

        message["From"] = self.sender_mail
        message["To"] = recipient_email
        message["Subject"] = subject

        message.attach(MIMEText(body, "html"))
        # SMTP server setup (example with Gmail)
        server = smtplib.SMTP("smtp.gmail.com", 587)
        server.starttls()
        server.login(self.sender_mail, self.mail_password)

        # Sending the email
        server.send_message(message)
        server.quit()

        logger.info(f"Email sent to {recipient_email}")
        return True

    def check_upload_status(
        self, upload_id: int, idx: int, *, max_polls: int = 60
    ) -> requests.Response:
        """
        Checks the status of an upload on Strava.

        Args:
            upload_id (str): The upload ID received from the upload activity response.
            idx (int): The index of the upload in the list of uploads.

        Returns:
            dict: Response from Strava API regarding the upload status.
        """
        for _ in range(max_polls):
            response = requests.get(
                f"{self.UPLOAD_URL}/{upload_id}",
                headers={"Authorization": f"Bearer {self.access_token}"},
                timeout=30,
            )
            self.check_rate_limit(response)
            response.raise_for_status()
            status = response.json()

            logger.info(
                f"Status {idx}/{self.num_files} with {upload_id}: {status['status']}"
            )

            if status["status"] in {
                "Your activity is ready.",
                "There was an error processing your activity.",
            }:
                return response
            if status["status"] == "Your activity is still being processed.":
                pass

            time.sleep(2)
        raise TimeoutError(f"Strava upload {upload_id} did not finish in time.")

    def upload_activities_to_strava(
        self,
        filedata: list[CustomGPX],
        data_type: str = "gpx",
    ) -> list[UploadResult]:
        """
        Uploads an activity file to Strava.

        Args:
            filedata (List[CustomGPX]): List of GPX objects to upload.
            data_type (str): Type of the activity file ('fit', 'tcx', or 'gpx').

        Returns:
            One result for every attempted upload. Failed uploads are not retried forever.
        """
        self.num_files = len(filedata)
        results = []
        for i, gpx in enumerate(filedata):
            filepath = gpx.activity.filepath
            if not filepath or not os.path.exists(filepath):
                results.append(
                    UploadResult(
                        gpx=gpx,
                        success=False,
                        status="missing_file",
                        error=f"Replacement file does not exist: {filepath}",
                    )
                )
                continue

            data = {
                "data_type": data_type,
                "name": gpx.activity.name,
                "description": gpx.activity.description,
                "trainer": int(gpx.activity.trainer),
                "commute": int(gpx.activity.commute),
                "sport_type": gpx.activity.sport,
                "external_id": gpx.activity.external_id,
            }
            try:
                with open(filepath, "rb") as upload_file:
                    response = requests.post(
                        self.UPLOAD_URL,
                        headers={"Authorization": f"Bearer {self.access_token}"},
                        files={"file": upload_file},
                        data={
                            key: value
                            for key, value in data.items()
                            if value is not None
                        },
                        timeout=30,
                    )
                self.check_rate_limit(response)
                if response.status_code >= 300:
                    try:
                        error_payload = response.json()
                    except ValueError:
                        error_payload = {}
                    error = (
                        error_payload.get("error")
                        or error_payload.get("message")
                        or response.text
                        or f"HTTP {response.status_code}"
                    )
                    results.append(
                        UploadResult(
                            gpx=gpx,
                            success=False,
                            status="request_failed",
                            error=error,
                            activity_id=self.duplicate_activity_id(error),
                        )
                    )
                    continue
                response.raise_for_status()
                upload_id = str(response.json()["id"])
                response = self.check_upload_status(upload_id, idx=i + 1)
                payload = response.json()
            except (
                requests.RequestException,
                KeyError,
                TimeoutError,
                ValueError,
            ) as error:
                logger.error(f"Upload {i + 1}/{len(filedata)} failed: {error}")
                results.append(
                    UploadResult(
                        gpx=gpx,
                        success=False,
                        status="request_failed",
                        error=str(error),
                    )
                )
                continue

            status = payload.get("status", "unknown")
            if status == "Your activity is ready.":
                activity_id = int(payload["activity_id"])
                url = f"{self.ACTIVITIES_WEBURL}{activity_id}"
                logger.info(f"Uploaded {i + 1}/{len(filedata)} to {url}")
                gpx.activity.id = activity_id
                gpx.activity.url = url
                if gpx.activity.gear_id:
                    self.update_activity_gear(activity_id, gpx.activity.gear_id)
                results.append(
                    UploadResult(
                        gpx=gpx,
                        success=True,
                        status=status,
                        activity_id=activity_id,
                    )
                )
            else:
                error = payload.get("error") or status
                logger.warning(f"Upload {i + 1}/{len(filedata)} failed: {error}")
                results.append(
                    UploadResult(
                        gpx=gpx,
                        success=False,
                        status=status,
                        error=error,
                        activity_id=self.duplicate_activity_id(error),
                    )
                )
        return results

    @staticmethod
    def duplicate_activity_id(error: str | None) -> int | None:
        if not error:
            return None
        if "duplicate" not in error.lower():
            return None
        match = re.search(
            r"(?:activity\s+|/activities/)(\d+)",
            error,
            re.IGNORECASE,
        )
        return int(match.group(1)) if match else None
