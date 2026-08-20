import json
import os
import tempfile
import unittest
from unittest.mock import patch

import gpxpy.gpx
import requests

from app import StravaMerger, StravaRateLimitError
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

    def test_activity_list_does_not_fetch_every_activity_individually(self):
        activities = [
            {"id": 1, "name": "One"},
            {"id": 2, "name": "Two"},
        ]
        with patch("app.requests.get", return_value=FakeResponse(activities)) as request:
            result = self.merger.get_activities(2)

        self.assertEqual(result, activities)
        self.assertEqual(request.call_count, 1)
        self.assertEqual(request.call_args.args[0], self.merger.ACTIVITIES_URL)

    def test_complete_activity_catalog_uses_200_item_pages(self):
        first_page = [{"id": activity_id} for activity_id in range(200)]
        second_page = [{"id": 200}]
        with patch(
            "app.requests.get",
            side_effect=[FakeResponse(first_page), FakeResponse(second_page)],
        ) as request:
            result = self.merger.get_all_activities()

        self.assertEqual(len(result), 201)
        self.assertEqual(request.call_count, 2)
        self.assertEqual(request.call_args_list[0].kwargs["params"]["page"], 1)
        self.assertEqual(request.call_args_list[1].kwargs["params"]["page"], 2)
        self.assertEqual(
            request.call_args_list[0].kwargs["params"]["per_page"], 200
        )

    def test_reported_read_quota_is_reserved_for_essential_requests(self):
        response = requests.Response()
        response.status_code = 200
        response._content = b"{}"
        response.headers["X-ReadRateLimit-Limit"] = "100,1000"
        response.headers["X-ReadRateLimit-Usage"] = "89,500"

        self.merger.check_rate_limit(response)

        self.assertTrue(self.merger.has_read_capacity(1, reserve=10))
        self.assertFalse(self.merger.has_read_capacity(2, reserve=10))

    def test_synchronous_duplicate_upload_is_returned_without_retry_loop(self):
        filepath = os.path.join(self.temporary_directory.name, "deleted.gpx")
        gpx = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(segment)
        gpx.tracks.append(track)
        segment.points.append(gpxpy.gpx.GPXTrackPoint(47.0, 8.0))
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

        with patch("app.requests.post", return_value=response) as request:
            result = self.merger.upload_activities_to_strava([gpx])[0]

        self.assertFalse(result.success)
        self.assertEqual(result.activity_id, 987654)
        uploaded_file = request.call_args.kwargs["files"]["file"]
        self.assertEqual(uploaded_file[0], "deleted.gpx")
        self.assertIsInstance(uploaded_file[1], bytes)

    def test_successful_upload_survives_gear_update_rate_limit(self):
        gpx = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        segment.points.append(gpxpy.gpx.GPXTrackPoint(47.0, 8.0))
        track.segments.append(segment)
        gpx.tracks.append(track)
        gpx.set_activity(
            Activity(
                name="Ride",
                id=-1,
                start_date="2026-08-13T07:00:00Z",
                end_date="2026-08-13T08:00:00Z",
                start_coords=(47.0, 8.0),
                end_coords=(47.1, 8.1),
                gear_id="bike-1",
                sport="Ride",
            )
        )
        upload_response = FakeResponse({"id": 77})
        status_response = FakeResponse(
            {"status": "Your activity is ready.", "activity_id": 987}
        )

        with (
            patch("app.requests.post", return_value=upload_response),
            patch.object(
                self.merger,
                "check_upload_status",
                return_value=status_response,
            ),
            patch.object(
                self.merger,
                "update_activity_gear",
                side_effect=StravaRateLimitError("rate limit reached"),
            ),
        ):
            result = self.merger.upload_activities_to_strava([gpx])[0]

        self.assertTrue(result.success)
        self.assertEqual(result.activity_id, 987)
        self.assertFalse(result.gear_applied)
        self.assertEqual(result.gear_error, "rate limit reached")
        self.assertEqual(gpx.activity.url, "https://www.strava.com/activities/987")

    def test_merge_preserves_one_unambiguous_gear_and_minute_timestamp(self):
        activities = []
        for activity_id, gear_id in ((1, "bike-1"), (2, None)):
            gpx = CustomGPX()
            gpx.set_activity(
                Activity(
                    name=f"Ride {activity_id}",
                    id=activity_id,
                    start_date="2026-08-13T07:00:00Z",
                    end_date="2026-08-13T08:00:00Z",
                    start_coords=(47.0, 8.0),
                    end_coords=(47.1, 8.1),
                    gear_id=gear_id,
                    sport="Ride",
                )
            )
            activities.append(gpx)

        merged = self.merger.get_new_activity(activities)

        self.assertEqual(merged.gear_id, "bike-1")
        self.assertRegex(
            merged.description,
            r"StravaMerger bot · merged activities 1 \+ 2 · "
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\.$",
        )
        self.assertNotRegex(merged.description, r"\d{2}:\d{2}:\d{2}")

        activities[1].activity.gear_id = "bike-2"
        self.assertIsNone(self.merger.get_new_activity(activities).gear_id)

    def test_update_activity_gear_uses_replacement_and_source_gear(self):
        with patch(
            "app.requests.put", return_value=FakeResponse({}, status_code=200)
        ) as request:
            applied, error = self.merger.update_activity_gear(987, "bike-1")

        self.assertTrue(applied)
        self.assertIsNone(error)
        self.assertEqual(
            request.call_args.kwargs["data"], {"gear_id": "bike-1"}
        )
        self.assertTrue(request.call_args.args[0].endswith("/activities/987"))

    def test_update_activity_name_uses_strava_update_endpoint(self):
        with patch(
            "app.requests.put", return_value=FakeResponse({}, status_code=200)
        ) as request:
            applied, error = self.merger.update_activity_name(987, "IBM")

        self.assertTrue(applied)
        self.assertIsNone(error)
        self.assertEqual(request.call_args.kwargs["data"], {"name": "IBM"})
        self.assertTrue(request.call_args.args[0].endswith("/activities/987"))

    def test_duplicate_activity_id_accepts_strava_html_link(self):
        error = (
            "replacement.gpx duplicate of "
            "<a href='/activities/19729897492'>Fahrt am Morgen</a>"
        )

        self.assertEqual(
            StravaMerger.duplicate_activity_id(error),
            19729897492,
        )

    def test_fixed_activity_name_uses_route_only_for_generic_names(self):
        activity = Activity(
            name="Fahrt am Morgen",
            id=12,
            start_date="2026-08-13T07:00:00Z",
            end_date="2026-08-13T08:00:00Z",
            start_coords=(47.310019, 8.544049),
            end_coords=(47.4, 8.5),
            sport="Ride",
        )
        self.assertEqual(self.merger.fixed_activity_name(activity), "IBM")

        activity.name = "Custom training name"
        self.assertEqual(
            self.merger.fixed_activity_name(activity),
            "Custom training name",
        )

        activity.name = "Lauf am Abend"
        activity.start_coords = (47.4, 8.5)
        gpx = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        segment.points.append(gpxpy.gpx.GPXTrackPoint(47.310019, 8.544049))
        track.segments.append(segment)
        gpx.tracks.append(track)
        self.assertEqual(self.merger.fixed_activity_name(activity, gpx), "IBM")

    def test_generic_name_rule_matches_any_point_but_not_custom_titles(self):
        location = (47.45, 8.48)
        self.merger.add_activity_name_location(location, "Zurich Pendeln")
        activity = Activity(
            name="Radfahrt am Morgen",
            id=12,
            start_date="2026-08-13T07:00:00Z",
            end_date="2026-08-13T08:00:00Z",
            start_coords=(47.4, 8.4),
            end_coords=(47.5, 8.5),
            sport="Ride",
        )
        gpx = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        segment.points.append(gpxpy.gpx.GPXTrackPoint(*location))
        track.segments.append(segment)
        gpx.tracks.append(track)

        self.assertEqual(
            self.merger.activity_name_for_track(activity, gpx),
            "Zurich Pendeln",
        )
        activity.name = "Sunday ride with friends"
        self.assertIsNone(self.merger.activity_name_for_track(activity, gpx))

    def test_nomerge_also_disables_hole_repair(self):
        activity = {
            "start_latlng": [47.0, 8.0],
            "description": "Keep this activity NOMERGE please",
        }

        self.assertFalse(StravaMerger.can_fix_activity(activity))

    def test_bot_activity_is_recognized_from_summary_external_id(self):
        activity = {
            "description": None,
            "external_id": "stravamerger-fix-123-v1",
        }

        self.assertTrue(StravaMerger.is_bot_activity(activity))


if __name__ == "__main__":
    unittest.main()
