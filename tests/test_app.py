import json
import os
import tempfile
import unittest
from unittest.mock import patch

import gpxpy.gpx

from app import StravaMerger
from utils import Activity, CustomGPX


class FakeResponse:
    def __init__(self, payload, status_code=200, text=""):
        self.payload = payload
        self.status_code = status_code
        self.text = text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 300:
            raise RuntimeError(self.text)


class StravaApiTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.secret_path = os.path.join(self.temporary_directory.name, "secret.json")
        with open(self.secret_path, "w") as file:
            json.dump(
                {
                    "client_id": 1,
                    "client_secret": "client-secret",
                    "access_token": "old-access",
                    "refresh_token": "old-refresh",
                    "mail": "mail-password",
                    "google_maps_api_key": "maps-key",
                },
                file,
            )
        self.merger = StravaMerger(self.secret_path, sender_mail="me@example.com")

    def test_refresh_persists_rotated_token(self):
        response = FakeResponse(
            {
                "access_token": "new-access",
                "refresh_token": "new-refresh",
                "expires_at": 123,
            }
        )
        with patch("app.requests.post", return_value=response):
            self.assertEqual(self.merger.refresh_access_token(), "new-access")

        with open(self.secret_path) as file:
            saved = json.load(file)
        self.assertEqual(saved["refresh_token"], "new-refresh")
        self.assertEqual(saved["expires_at"], 123)
        self.assertEqual(saved["google_maps_api_key"], "maps-key")

    def test_google_maps_key_is_not_read_from_environment(self):
        with open(self.secret_path) as file:
            secret = json.load(file)
        secret.pop("google_maps_api_key")
        with open(self.secret_path, "w") as file:
            json.dump(secret, file)

        with patch.dict(os.environ, {"GOOGLE_MAPS_API_KEY": "environment-key"}):
            merger = StravaMerger(self.secret_path, sender_mail="")

        self.assertIsNone(merger.google_maps_api_key)

    def test_activity_streams_are_fetched_in_one_aligned_request(self):
        response = FakeResponse(
            [
                {"type": "latlng", "data": [[47.0, 8.0], [47.1, 8.1]]},
                {"type": "time", "data": [0, 10]},
                {"type": "altitude", "data": [400, 410]},
                {"type": "heartrate", "data": [120, 125]},
            ]
        )
        activity = Activity(
            name="Ride",
            id=12,
            start_date="2026-08-13T09:00:00Z",
            start_date_utc="2026-08-13T07:00:00Z",
            end_date="2026-08-13T09:00:10Z",
            start_coords=(47.0, 8.0),
            end_coords=(47.1, 8.1),
            sport="Ride",
        )

        with patch("app.requests.get", return_value=response) as request:
            gpx = self.merger.activity_to_gpx(activity)

        self.assertEqual(request.call_count, 1)
        params = request.call_args.kwargs["params"]
        self.assertEqual(params["key_by_type"], "true")
        self.assertIn("latlng", params["keys"])
        points = gpx.tracks[0].segments[0].points
        self.assertEqual(points[0].time.isoformat(), "2026-08-13T07:00:00+00:00")
        self.assertEqual(points[1].elevation, 410)
        self.assertTrue(points[0].extensions)
        serialized = gpx.to_xml()
        self.assertIn("xmlns:gpxtpx=", serialized)
        gpxpy.parse(serialized)

        merged = self.merger.merge_gpx([gpx])
        gpxpy.parse(merged.to_xml())

    def test_synchronous_duplicate_upload_is_returned_without_retry_loop(self):
        filepath = os.path.join(self.temporary_directory.name, "replacement.gpx")
        gpx = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(segment)
        gpx.tracks.append(track)
        segment.points.append(gpxpy.gpx.GPXTrackPoint(47.0, 8.0))
        with open(filepath, "w") as file:
            file.write(gpx.to_xml())
        gpx.set_activity(
            Activity(
                name="Ride",
                id=-1,
                start_date="2026-08-13T07:00:00Z",
                end_date="2026-08-13T08:00:00Z",
                start_coords=(47.0, 8.0),
                end_coords=(47.1, 8.1),
                filepath=filepath,
                sport="Ride",
            )
        )
        response = FakeResponse(
            {"error": "replacement.gpx duplicate of activity 987654"},
            status_code=400,
        )

        with patch("app.requests.post", return_value=response):
            result = self.merger.upload_activities_to_strava([gpx])[0]

        self.assertFalse(result.success)
        self.assertEqual(result.activity_id, 987654)

    def test_duplicate_activity_id_accepts_strava_html_link(self):
        error = (
            "replacement.gpx duplicate of "
            "<a href='/activities/19729897492'>Fahrt am Morgen</a>"
        )

        self.assertEqual(
            StravaMerger.duplicate_activity_id(error),
            19729897492,
        )


if __name__ == "__main__":
    unittest.main()
