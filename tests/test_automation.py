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
    _delete_mail_body,
    _fixed_activity,
    _load_replacement,
    run_automation,
)
from gpxfixer import Route
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

    def test_completed_jobs_no_longer_claim_sources(self):
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        with open(state_path, "w") as file:
            json.dump(
                {
                    "version": 1,
                    "jobs": {
                        "fix-1": {"status": "complete", "source_ids": [1]},
                        "fix-2": {"status": "ready", "source_ids": [2]},
                    },
                },
                file,
            )
        self.assertEqual(JobStore(state_path).claimed_source_ids(), {2})

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

            def __init__(self):
                self.source_exists = True
                self.upload_attempts = 0
                self.emails = []

            def detect_merging_activities(self, activities):
                return []

            can_fix_activity = staticmethod(StravaMerger.can_fix_activity)

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
                self.emails.append((recipient, subject, body))

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

        merger = Merger()
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        with patch("automation.GoogleRoutesClient", Routes):
            first = run_automation(
                merger,
                activities=[api_activity],
                output_folder=self.temporary_directory.name,
                recipient="me@example.com",
                state_path=state_path,
            )

        self.assertEqual(first.repaired_jobs, 1)
        self.assertEqual(first.deferred_jobs, 1)
        self.assertEqual(
            JobStore(state_path).jobs["fix-10"]["status"], "awaiting_deletion"
        )

        merger.source_exists = False
        second = run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )
        self.assertEqual(second.uploaded_jobs, 1)
        self.assertEqual(JobStore(state_path).jobs["fix-10"]["status"], "uploaded")
        self.assertEqual(merger.upload_attempts, 2)


if __name__ == "__main__":
    unittest.main()
