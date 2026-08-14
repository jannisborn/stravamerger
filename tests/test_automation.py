import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import gpxpy.gpx

from app import GPXTPX_NAMESPACE, StravaMerger, UploadResult
from automation import (
    JobStore,
    _address_for,
    _delete_mail_body,
    _fixed_activity,
    _load_replacement,
    run_automation,
)
from gpxfixer import GeocodingError, Route
from utils import Activity, CustomGPX


class AutomationStateTests(unittest.TestCase):
    def setUp(self):
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)

    def test_job_store_round_trip_claims_sources(self):
        source = Activity(
            name="Ride",
            id=123,
            start_date="2026-08-13T07:00:00Z",
            end_date="2026-08-13T08:00:00Z",
            start_coords=(47.0, 8.0),
            end_coords=(47.1, 8.1),
            sport="Ride",
        )
        replacement_activity = _fixed_activity(source, 1)
        gpx = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        track.segments.append(segment)
        gpx.tracks.append(track)
        segment.points.append(gpxpy.gpx.GPXTrackPoint(47.0, 8.0))
        gpx.set_activity(replacement_activity)
        gpx_path = os.path.join(self.temporary_directory.name, "replacement.gpx")
        with open(gpx_path, "w") as file:
            file.write(gpx.to_xml())
        gpx.activity.filepath = gpx_path

        state_path = os.path.join(self.temporary_directory.name, "state.json")
        store = JobStore(state_path)
        store.add(
            job_id="fix-123",
            kind="fix",
            source_activities=[source],
            replacement=gpx,
            hole_distances_meters={123: [1_500.25]},
        )

        loaded_store = JobStore(state_path)
        self.assertEqual(loaded_store.claimed_source_ids(), {123})
        loaded_gpx = _load_replacement(loaded_store.jobs["fix-123"])
        self.assertEqual(loaded_gpx.activity.source_ids, [123])
        self.assertEqual(loaded_gpx.activity.external_id, "stravamerger-fix-123-v1")
        with open(state_path) as file:
            self.assertEqual(json.load(file)["version"], 1)
        email_body = _delete_mail_body([loaded_store.jobs["fix-123"]])
        self.assertIn("Ride", email_body)
        self.assertIn("1.50 km", email_body)

    def test_address_lookup_failure_falls_back_to_coordinates(self):
        class Geocoder:
            @staticmethod
            def reverse_geocode(location):
                raise GeocodingError("Geocoding API is unavailable")

        with patch("automation.logger.warning") as warning:
            address = _address_for((47.0, 8.0), Geocoder(), {})

        self.assertEqual(address, "47.000000, 8.000000")
        warning.assert_called_once()

    def test_completed_jobs_no_longer_claim_sources(self):
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        with open(state_path, "w") as file:
            json.dump(
                {
                    "version": 1,
                    "jobs": {
                        "fix-1": {"status": "complete", "source_ids": [1]},
                        "fix-2": {"status": "ready", "source_ids": [2]},
                        "fix-3": {"status": "cancelled", "source_ids": [3]},
                    },
                },
                file,
            )
        self.assertEqual(JobStore(state_path).claimed_source_ids(), {2})

    def test_current_nomerge_marker_cancels_a_queued_fix(self):
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        with open(state_path, "w") as file:
            json.dump(
                {
                    "version": 1,
                    "jobs": {
                        "fix-10": {
                            "id": "fix-10",
                            "kind": "fix",
                            "source_ids": [10],
                            "sources": [{"id": 10, "name": "Old name"}],
                            "status": "awaiting_deletion",
                            "last_error": "duplicate of activity 10",
                        }
                    },
                },
                file,
            )

        activity = {
            "id": 10,
            "name": "Broken Ride",
            "description": "Please NOMERGE this one",
        }

        class Merger:
            google_maps_api_key = None

            @staticmethod
            def detect_merging_activities(activities):
                return []

            can_fix_activity = staticmethod(StravaMerger.can_fix_activity)

            @staticmethod
            def send_email(*args, **kwargs):
                raise AssertionError("A cancelled job must not send a reminder")

            @staticmethod
            def get_activity(activity_id):
                return activity

        with patch("automation.logger.info") as info:
            run_automation(
                Merger(),
                activities=[],
                output_folder=self.temporary_directory.name,
                recipient="me@example.com",
                state_path=state_path,
                fix_holes=True,
            )

        job = JobStore(state_path).jobs["fix-10"]
        self.assertEqual(job["status"], "cancelled")
        self.assertEqual(info.call_args.args[1], "Broken Ride")

    def test_load_replacement_repairs_legacy_extension_namespace(self):
        source = Activity(
            name="Ride",
            id=123,
            start_date="2026-08-13T07:00:00Z",
            end_date="2026-08-13T08:00:00Z",
            start_coords=(47.0, 8.0),
            end_coords=(47.1, 8.1),
            sport="Ride",
        )
        replacement = _fixed_activity(source, 1)
        filepath = os.path.join(
            self.temporary_directory.name,
            "fix-123_replacement.gpx",
        )
        malformed = f"""<?xml version="1.0" encoding="UTF-8"?>
<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1" creator="test">
  <trk><trkseg><trkpt lat="47.0" lon="8.0"><extensions>
    <{GPXTPX_NAMESPACE}:TrackPointExtension>
      <{GPXTPX_NAMESPACE}:hr>120</{GPXTPX_NAMESPACE}:hr>
    </{GPXTPX_NAMESPACE}:TrackPointExtension>
  </extensions></trkpt></trkseg></trk>
</gpx>"""
        with open(filepath, "w") as file:
            file.write(malformed)
        source_filepath = os.path.join(
            self.temporary_directory.name,
            "fix-123_source_123.gpx",
        )
        with open(source_filepath, "w") as file:
            file.write(malformed)
        replacement.filepath = filepath
        job = {
            "source_ids": [123],
            "replacement": replacement.__dict__,
        }

        loaded = _load_replacement(job)

        self.assertEqual(loaded.activity.source_ids, (123,))
        with open(filepath) as file:
            repaired = file.read()
        self.assertIn("xmlns:gpxtpx=", repaired)
        self.assertIn("<gpxtpx:hr>120</gpxtpx:hr>", repaired)
        gpxpy.parse(repaired)
        with open(source_filepath) as file:
            repaired_source = file.read()
        self.assertIn("xmlns:gpxtpx=", repaired_source)
        gpxpy.parse(repaired_source)

    def test_duplicate_repair_resumes_after_source_deletion(self):
        api_activity = {
            "id": 10,
            "name": "Broken Ride",
            "start_date": "2026-08-13T07:00:00Z",
            "start_date_local": "2026-08-13T09:00:00Z",
            "elapsed_time": 120,
            "start_latlng": [47.0, 8.0],
            "end_latlng": [47.01, 8.01],
            "gear_id": "b1",
            "sport_type": "Ride",
            "description": "",
            "commute": True,
            "trainer": False,
        }

        class Merger:
            google_maps_api_key = "maps-key"
            dist_theta = 1_000.0

            def __init__(self):
                self.source_exists = True
                self.upload_attempts = 0
                self.emails = []
                self.email_enabled = False

            def detect_merging_activities(self, activities):
                return []

            can_fix_activity = staticmethod(StravaMerger.can_fix_activity)
            fixed_activity_name = StravaMerger.fixed_activity_name

            def activity_from_api(self, activity):
                return StravaMerger.activity_from_api(activity)

            def activity_to_gpx(self, activity):
                gpx = CustomGPX()
                track = gpxpy.gpx.GPXTrack()
                segment = gpxpy.gpx.GPXTrackSegment()
                track.segments.append(segment)
                gpx.tracks.append(track)
                start = datetime(2026, 8, 13, 7, tzinfo=timezone.utc)
                segment.points.extend(
                    [
                        gpxpy.gpx.GPXTrackPoint(47.0, 8.0, time=start),
                        gpxpy.gpx.GPXTrackPoint(
                            47.01,
                            8.01,
                            time=start + timedelta(seconds=120),
                        ),
                    ]
                )
                gpx.set_activity(activity)
                return gpx

            def save_replacement(self, *args, **kwargs):
                return StravaMerger.save_replacement(self, *args, **kwargs)

            def send_email(self, recipient, subject, body):
                if not self.email_enabled:
                    return False
                self.emails.append((recipient, subject, body))
                return True

            def upload_activities_to_strava(self, gpxs):
                self.upload_attempts += 1
                if self.upload_attempts == 1:
                    return [
                        UploadResult(
                            gpx=gpxs[0],
                            success=False,
                            status="duplicate",
                            error="duplicate of activity 10",
                            activity_id=10,
                        )
                    ]
                gpxs[0].activity.id = 99
                gpxs[0].activity.url = "https://www.strava.com/activities/99"
                return [
                    UploadResult(
                        gpx=gpxs[0],
                        success=True,
                        status="Your activity is ready.",
                        activity_id=99,
                    )
                ]

            def activity_exists(self, activity_id):
                return self.source_exists

        class Routes:
            def __init__(self, api_key):
                self.api_key = api_key

            def route(self, origin, destination, travel_mode):
                return Route(
                    points=(origin, (47.005, 8.005), destination),
                    distance_meters=1_500,
                    duration_seconds=100,
                )

        class Geocoder:
            def __init__(self, api_key):
                self.api_key = api_key

            @staticmethod
            def reverse_geocode(location):
                if location == (47.0, 8.0):
                    return "Startstrasse 1, Zürich"
                return "Zielweg 2, Zürich"

        merger = Merger()
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        disabled = run_automation(
            merger,
            activities=[api_activity],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )
        self.assertEqual(disabled.repaired_jobs, 0)
        self.assertEqual(merger.upload_attempts, 0)

        with (
            patch("automation.GoogleRoutesClient", Routes),
            patch("automation.GoogleGeocodingClient", Geocoder),
            patch("automation.logger.info") as info,
        ):
            first = run_automation(
                merger,
                activities=[api_activity],
                output_folder=self.temporary_directory.name,
                recipient="me@example.com",
                state_path=state_path,
                fix_holes=True,
            )

        self.assertEqual(first.repaired_jobs, 1)
        self.assertEqual(first.deferred_jobs, 1)
        job = JobStore(state_path).jobs["fix-10"]
        self.assertEqual(job["status"], "awaiting_deletion")
        self.assertIsNone(job["delete_notification_recipient"])
        hole_logs = [
            call
            for call in info.call_args_list
            if call.args and call.args[0].startswith("Detected GPS hole")
        ]
        self.assertEqual(
            hole_logs[0].args,
            (
                'Detected GPS hole in "{}": {} between {} and {}.',
                "Broken Ride",
                "1.35 km",
                "Startstrasse 1, Zürich",
                "Zielweg 2, Zürich",
            ),
        )

        legacy_store = JobStore(state_path)
        legacy_job = legacy_store.jobs["fix-10"]
        legacy_job["status"] = "ready"
        legacy_job["last_error"] = (
            "replacement.gpx duplicate of " "<a href='/activities/10'>Broken Ride</a>"
        )
        legacy_job["delete_notified"] = True
        legacy_job.pop("delete_notification_recipient")
        legacy_job.pop("hole_distances_meters")
        legacy_store.save()

        merger.email_enabled = True
        notified = run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )
        self.assertEqual(notified.deferred_jobs, 1)
        self.assertEqual(merger.upload_attempts, 1)
        self.assertEqual(len(merger.emails), 1)
        self.assertEqual(merger.emails[0][1], "StravaMerger - Delete source activities")
        self.assertIn("1.35 km", merger.emails[0][2])
        self.assertIn("Startstrasse 1, Zürich", merger.emails[0][2])
        self.assertIn("Zielweg 2, Zürich", merger.emails[0][2])
        self.assertEqual(
            JobStore(state_path).jobs["fix-10"]["delete_notification_recipient"],
            "me@example.com",
        )

        reminded_again = run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )
        self.assertEqual(reminded_again.deferred_jobs, 1)
        self.assertEqual(merger.upload_attempts, 1)
        self.assertEqual(len(merger.emails), 2)
        self.assertEqual(
            merger.emails[1][1], "StravaMerger - Delete source activities"
        )

        merger.source_exists = False
        completed = run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )
        self.assertEqual(completed.uploaded_jobs, 1)
        completed_job = JobStore(state_path).jobs["fix-10"]
        self.assertEqual(completed_job["status"], "uploaded")
        self.assertEqual(
            completed_job["confirmation_notification_recipient"],
            "me@example.com",
        )
        self.assertEqual(merger.upload_attempts, 2)
        self.assertEqual(len(merger.emails), 3)
        self.assertEqual(merger.emails[2][1], "StravaMerger - New activities")
        self.assertIn("Broken Ride", merger.emails[2][2])


if __name__ == "__main__":
    unittest.main()
