import gc
import json
import os
import tempfile
import unittest
import weakref
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import gpxpy.gpx

from app import StravaMerger, StravaRateLimitError, UploadResult
from automation import (
    AutomationSummary,
    JobStore,
    _address_for,
    _daily_mail_body,
    _delete_mail_body,
    _fixed_activity,
    _load_replacement,
    _needs_name_change,
    _review_mail_body,
    _route_with_fallback,
    _upload_jobs,
    prepare_oldest_activity_batch,
    run_automation,
)
from gpxfixer import GeocodingError, NoRouteError, Route, RouteError, TrackHole
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
            hole_details={
                123: [
                    {
                        "distance_meters": 1_500.25,
                        "origin_address": "Start Street",
                        "destination_address": "End Street",
                        "repair_method": "straight_line",
                        "fallback_reason": "Google Routes returned no route.",
                    }
                ]
            },
        )

        loaded_store = JobStore(state_path)
        os.unlink(gpx_path)
        self.assertEqual(loaded_store.claimed_source_ids(), {123})
        self.assertIn(
            "replacement_gpx_gzip", loaded_store.jobs["fix-123"]
        )
        self.assertNotIn("replacement_gpx", loaded_store.jobs["fix-123"])
        loaded_gpx = _load_replacement(loaded_store.jobs["fix-123"])
        self.assertEqual(loaded_gpx.activity.source_ids, [123])
        self.assertEqual(loaded_gpx.activity.external_id, "stravamerger-fix-123-v1")
        with open(state_path) as file:
            self.assertEqual(json.load(file)["version"], 1)
        email_body = _delete_mail_body([loaded_store.jobs["fix-123"]])
        self.assertIn("Ride", email_body)
        self.assertIn("1.50 km", email_body)
        self.assertIn("Straight-line GPX coordinates were used", email_body)
        self.assertIn("Google Routes returned no route", email_body)
        self.assertIn(
            "href='https://www.strava.com/activities/123'>Strava activity 123</a>",
            email_body,
        )

    def test_oldest_catalog_advances_and_includes_backdated_new_uploads(self):
        def summary(activity_id, date):
            return {
                "id": activity_id,
                "name": f"Ride {activity_id}",
                "start_date": date,
                "start_date_local": date,
                "elapsed_time": 60,
                "start_latlng": [47.0, 8.0],
                "end_latlng": [47.1, 8.1],
                "sport_type": "Ride",
            }

        class Merger:
            def __init__(self):
                self.full_calls = 0

            def get_all_activities(self):
                self.full_calls += 1
                activities = [
                    summary(3, "2022-01-01T08:00:00Z"),
                    summary(2, "2021-01-01T08:00:00Z"),
                    summary(1, "2020-01-01T08:00:00Z"),
                ]
                if self.full_calls > 1:
                    activities.append(summary(4, "2019-01-01T08:00:00Z"))
                return activities

        state_path = os.path.join(self.temporary_directory.name, "history.json")
        merger = Merger()
        store = JobStore(state_path)
        context, batch = prepare_oldest_activity_batch(merger, store, 2)

        self.assertEqual(batch, {1, 2})
        self.assertEqual([item["id"] for item in context], [1, 2, 3])
        store.record_screened(batch)

        store = JobStore(state_path)
        context, batch = prepare_oldest_activity_batch(merger, store, 2)

        self.assertEqual(batch, {3, 4})
        self.assertEqual([item["id"] for item in context], [4, 3])
        self.assertEqual(merger.full_calls, 2)
        self.assertLess(os.path.getsize(state_path), 2_000)

    def test_compact_catalog_stays_small_for_2300_activities(self):
        activities = [
            {
                "id": activity_id,
                "name": f"Activity {activity_id}",
                "start_date": f"2020-01-{activity_id % 28 + 1:02d}T08:00:00Z",
                "start_date_local": (
                    f"2020-01-{activity_id % 28 + 1:02d}T09:00:00Z"
                ),
                "elapsed_time": 3_600,
                "start_latlng": [47.0, 8.0],
                "end_latlng": [47.1, 8.1],
                "gear_id": "bike-1",
                "sport_type": "Ride",
                "commute": False,
                "trainer": False,
            }
            for activity_id in range(1, 2_301)
        ]
        state_path = os.path.join(self.temporary_directory.name, "history.json")
        JobStore(state_path).sync_catalog(activities, initialize=True)

        self.assertLess(os.path.getsize(state_path), 1_000_000)

    def test_pending_catalog_metadata_is_refreshed(self):
        state_path = os.path.join(self.temporary_directory.name, "history.json")
        store = JobStore(state_path)
        activity = {
            "id": 1,
            "name": "Morning Ride",
            "start_date": "2020-01-01T08:00:00Z",
            "start_date_local": "2020-01-01T09:00:00Z",
            "elapsed_time": 60,
            "start_latlng": [47.0, 8.0],
            "end_latlng": [47.1, 8.1],
            "gear_id": "old-bike",
            "sport_type": "Ride",
        }
        store.sync_catalog([activity], initialize=True)

        activity["name"] = "Lake loop"
        activity["gear_id"] = "new-bike"
        added = store.sync_catalog([activity])

        pending = store.data["scan"]["pending"]["1"]
        self.assertEqual(added, 0)
        self.assertEqual(pending["name"], "Lake loop")
        self.assertEqual(pending["gear_id"], "new-bike")

    def test_custom_generic_reminder_is_consumed_after_rename(self):
        state_path = os.path.join(self.temporary_directory.name, "history.json")

        class Merger:
            google_maps_api_key = None

            def __init__(self):
                self.name = "Lunch Run"
                self.emails = []

            @staticmethod
            def detect_merging_activities(activities):
                return []

            def get_activity(self, activity_id):
                return {
                    "id": activity_id,
                    "name": self.name,
                    "description": "",
                }

            def send_email(self, recipient, subject, body):
                self.emails.append((recipient, subject, body))
                return True

        merger = Merger()
        first = run_automation(
            merger,
            activities=[
                {
                    "id": 10,
                    "name": "Lunch Run",
                    "start_latlng": [],
                }
            ],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
            generic_names=("Lunch *",),
        )

        self.assertEqual(first.screened_activity_ids, {10})
        self.assertIn("10", JobStore(state_path).data["name_reminders"])
        self.assertIn("Rename generic activities", merger.emails[0][2])

        merger.name = "Canal recovery"
        run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
            generic_names=("Lunch *",),
        )

        self.assertNotIn("10", JobStore(state_path).data["name_reminders"])
        self.assertEqual(len(merger.emails), 1)

    def test_routing_fallback_is_limited_to_no_route_or_indirect_route(self):
        hole = TrackHole(
            track_index=0,
            segment_index=0,
            point_index=1,
            elapsed_seconds=120,
            distance_meters=1_000,
            origin=(47.0, 8.0),
            destination=(47.01, 8.01),
        )

        class NoRoutes:
            @staticmethod
            def route(*args):
                raise NoRouteError("Google Routes returned no route.")

        route, reason = _route_with_fallback(NoRoutes(), hole, "BICYCLE")
        self.assertEqual(route.points[0], hole.origin)
        self.assertEqual(route.points[-1], hole.destination)
        self.assertIn("no route", reason)

        class IndirectRoute:
            @staticmethod
            def route(*args):
                return Route(
                    points=(hole.origin, (48.0, 9.0), hole.destination),
                    distance_meters=4_001,
                    duration_seconds=120,
                )

        route, reason = _route_with_fallback(IndirectRoute(), hole, "BICYCLE")
        self.assertEqual(route.distance_meters, hole.distance_meters)
        self.assertIn("more than 4x", reason)

        class ApiFailure:
            @staticmethod
            def route(*args):
                raise RouteError("Google Routes request failed: quota exceeded")

        with self.assertRaisesRegex(RouteError, "quota exceeded"):
            _route_with_fallback(ApiFailure(), hole, "BICYCLE")

    def test_fixed_activity_preserves_gear_and_has_concise_hole_description(self):
        source = Activity(
            name="Broken Ride",
            id=10,
            start_date="2026-08-13T07:00:00Z",
            end_date="2026-08-13T08:00:00Z",
            start_coords=(47.0, 8.0),
            end_coords=(47.1, 8.1),
            gear_id="bike-1",
            sport="Ride",
            description="Original note",
        )
        replacement = _fixed_activity(
            source,
            1,
            hole_details=[
                {
                    "distance_meters": 1_500,
                    "origin_address": "Start Street 1, Zürich, Switzerland",
                    "destination_address": "End Street 2, Zürich, Switzerland",
                }
            ],
        )

        self.assertEqual(replacement.gear_id, "bike-1")
        self.assertIn(
            "fixed 1 GPS gap: 1.50 km Start Street 1 → End Street 2",
            replacement.description,
        )
        self.assertRegex(
            replacement.description,
            r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC\.$",
        )
        self.assertNotRegex(replacement.description, r"\d{2}:\d{2}:\d{2}")

    def test_review_mail_links_activity_id(self):
        body = _review_mail_body(["Activity 456 was not repaired"])

        self.assertIn(
            "Activity <a href='https://www.strava.com/activities/456'>456</a>",
            body,
        )

    def test_partial_uploads_are_saved_before_rate_limit(self):
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        store = JobStore(state_path)
        for source_id in (1, 2):
            source = Activity(
                name=f"Ride {source_id}",
                id=source_id,
                start_date="2026-08-13T07:00:00Z",
                end_date="2026-08-13T08:00:00Z",
                start_coords=(47.0, 8.0),
                end_coords=(47.1, 8.1),
                sport="Ride",
            )
            gpx = CustomGPX()
            track = gpxpy.gpx.GPXTrack()
            segment = gpxpy.gpx.GPXTrackSegment()
            segment.points.append(gpxpy.gpx.GPXTrackPoint(47.0, 8.0))
            track.segments.append(segment)
            gpx.tracks.append(track)
            gpx.set_activity(_fixed_activity(source, 1))
            store.add(
                job_id=f"fix-{source_id}",
                kind="fix",
                source_activities=[source],
                replacement=gpx,
            )

        class Merger:
            def __init__(self):
                self.upload_calls = 0
                self.emails = []

            @staticmethod
            def fixed_activity_name(source, gpx):
                return source.name

            def upload_activities_to_strava(self, gpxs):
                self.upload_calls += 1
                if self.upload_calls == 2:
                    raise StravaRateLimitError("rate limit reached")
                gpxs[0].activity.id = 901
                gpxs[0].activity.url = "https://www.strava.com/activities/901"
                return [
                    UploadResult(
                        gpx=gpxs[0],
                        success=True,
                        status="Your activity is ready.",
                        activity_id=901,
                    )
                ]

            def send_email(self, recipient, subject, body):
                self.emails.append((recipient, subject, body))
                return True

        merger = Merger()
        summary = AutomationSummary()
        with self.assertRaisesRegex(StravaRateLimitError, "rate limit reached"):
            _upload_jobs(
                merger,
                store,
                ["fix-1", "fix-2"],
                summary,
            )

        reloaded = JobStore(state_path)
        self.assertEqual(reloaded.jobs["fix-1"]["status"], "uploaded")
        self.assertEqual(
            reloaded.jobs["fix-2"]["status"], "awaiting_deletion"
        )
        self.assertIsNone(
            reloaded.jobs["fix-1"]["confirmation_notification_recipient"]
        )
        self.assertEqual(summary.uploaded_jobs, 1)
        self.assertEqual(len(merger.emails), 0)

    def test_orphaned_upload_is_recovered_and_confirmed(self):
        source = Activity(
            name="Broken Ride",
            id=10,
            start_date="2026-08-13T07:00:00Z",
            end_date="2026-08-13T08:00:00Z",
            start_coords=(47.0, 8.0),
            end_coords=(47.1, 8.1),
            sport="Ride",
        )
        replacement = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        segment.points.append(gpxpy.gpx.GPXTrackPoint(47.0, 8.0))
        track.segments.append(segment)
        replacement.tracks.append(track)
        replacement.set_activity(_fixed_activity(source, 1))
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        store = JobStore(state_path)
        job = store.add(
            job_id="fix-10",
            kind="fix",
            source_activities=[source],
            replacement=replacement,
        )
        job["status"] = "manual_review"
        job["last_error"] = "duplicate of activity 901"
        store.save()

        class Merger:
            google_maps_api_key = None

            def __init__(self):
                self.emails = []

            @staticmethod
            def get_activity(activity_id):
                if activity_id == 901:
                    return {
                        "id": 901,
                        "external_id": "stravamerger-fix-10-v1",
                        "description": "replacement",
                    }
                return None

            @staticmethod
            def activity_exists(activity_id):
                return False

            @staticmethod
            def detect_merging_activities(activities):
                return []

            def send_email(self, recipient, subject, body):
                self.emails.append((recipient, subject, body))
                return True

        merger = Merger()
        run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )

        self.assertNotIn("fix-10", JobStore(state_path).jobs)
        self.assertEqual(len(merger.emails), 1)
        self.assertEqual(merger.emails[0][1], "StravaMerger - Daily report")
        self.assertIn("Strava activity 901", merger.emails[0][2])

    def test_generic_activity_names_and_combined_report(self):
        self.assertTrue(_needs_name_change("Fahrt am Morgen"))
        self.assertTrue(_needs_name_change("Fahrt am"))
        self.assertTrue(_needs_name_change("Lauf am Nachmittag"))
        self.assertTrue(_needs_name_change("Morning Ride"))
        self.assertTrue(_needs_name_change("Evening Run"))
        self.assertFalse(_needs_name_change("Morning gravel with friends"))
        self.assertFalse(_needs_name_change("Lunch Ride"))
        self.assertTrue(_needs_name_change("Lunch Ride", ("Lunch *",)))

        body = _daily_mail_body(
            deletion_jobs=[],
            existing_source_ids=set(),
            confirmation_jobs=[
                {
                    "replacement": {
                        "id": 901,
                        "name": "Uploaded Ride",
                        "url": "https://www.strava.com/activities/901",
                    }
                }
            ],
            review_messages=["Activity 456 needs review"],
            name_reminders=[{"id": 123, "name": "Morning Ride"}],
            info_messages=["Restored gear on replacement."],
        )

        self.assertIn("Uploaded replacements", body)
        self.assertIn("Rename generic activities", body)
        self.assertIn("Needs review", body)
        self.assertIn("Completed metadata updates", body)
        self.assertIn("Strava activity 901", body)
        self.assertIn("Strava activity 123", body)

    def test_pending_gear_is_retried_before_job_completes(self):
        source = Activity(
            name="Broken Ride",
            id=10,
            start_date="2026-08-13T07:00:00Z",
            end_date="2026-08-13T08:00:00Z",
            start_coords=(47.0, 8.0),
            end_coords=(47.1, 8.1),
            gear_id="bike-1",
            sport="Ride",
        )
        replacement = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        segment.points.append(gpxpy.gpx.GPXTrackPoint(47.0, 8.0))
        track.segments.append(segment)
        replacement.tracks.append(track)
        replacement.set_activity(_fixed_activity(source, 1))
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        store = JobStore(state_path)
        job = store.add(
            job_id="fix-10",
            kind="fix",
            source_activities=[source],
            replacement=replacement,
        )
        job["status"] = "uploaded"
        job["uploaded_activity_id"] = 901
        job["replacement"]["id"] = 901
        job["replacement"]["url"] = "https://www.strava.com/activities/901"
        job["gear_update_pending"] = True
        job["gear_update_error"] = "rate limit reached"
        store.save()

        class Merger:
            google_maps_api_key = None

            def __init__(self):
                self.gear_updates = []
                self.emails = []

            def update_activity_gear(self, activity_id, gear_id):
                self.gear_updates.append((activity_id, gear_id))
                return True, None

            @staticmethod
            def activity_exists(activity_id):
                return False

            @staticmethod
            def detect_merging_activities(activities):
                return []

            def send_email(self, recipient, subject, body):
                self.emails.append((recipient, subject, body))
                return True

        merger = Merger()
        run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )

        self.assertNotIn("fix-10", JobStore(state_path).jobs)
        self.assertEqual(merger.gear_updates, [(901, "bike-1")])
        self.assertEqual(len(merger.emails), 1)
        self.assertIn("Restored gear", merger.emails[0][2])

    def test_awaiting_job_is_emailed_then_uploaded_on_next_run(self):
        source = Activity(
            name="Commute",
            id=10,
            start_date="2026-08-13T07:00:00Z",
            end_date="2026-08-13T08:00:00Z",
            start_coords=(47.0, 8.0),
            end_coords=(47.1, 8.1),
            gear_id="bike-1",
            sport="Ride",
        )
        replacement = CustomGPX()
        track = gpxpy.gpx.GPXTrack()
        segment = gpxpy.gpx.GPXTrackSegment()
        segment.points.append(gpxpy.gpx.GPXTrackPoint(47.0, 8.0))
        track.segments.append(segment)
        replacement.tracks.append(track)
        replacement.set_activity(_fixed_activity(source, 1))
        source_path = os.path.join(
            self.temporary_directory.name, "fix-10_source_10.gpx"
        )
        replacement_path = os.path.join(
            self.temporary_directory.name, "fix-10_replacement.gpx"
        )
        for path in (source_path, replacement_path):
            with open(path, "w") as file:
                file.write(replacement.to_xml())
        source.filepath = source_path
        replacement.activity.filepath = replacement_path
        state_path = os.path.join(self.temporary_directory.name, "state.json")
        JobStore(state_path).add(
            job_id="fix-10",
            kind="fix",
            source_activities=[source],
            replacement=replacement,
        )

        class Merger:
            google_maps_api_key = None

            def __init__(self):
                self.source_exists = True
                self.uploads = []
                self.emails = []

            @staticmethod
            def detect_merging_activities(activities):
                return []

            @staticmethod
            def fixed_activity_name(source, gpx):
                return source.name

            def activity_exists(self, activity_id):
                return self.source_exists

            def upload_activities_to_strava(self, gpxs):
                self.uploads.extend(gpxs)
                self.assert_source_gear(gpxs[0].activity.gear_id)
                gpxs[0].activity.id = 901
                gpxs[0].activity.url = "https://www.strava.com/activities/901"
                return [
                    UploadResult(
                        gpx=gpxs[0],
                        success=True,
                        status="Your activity is ready.",
                        activity_id=901,
                        gear_applied=True,
                    )
                ]

            @staticmethod
            def assert_source_gear(gear_id):
                if gear_id != "bike-1":
                    raise AssertionError(f"unexpected gear {gear_id}")

            def send_email(self, recipient, subject, body):
                self.emails.append((recipient, subject, body))
                return True

        merger = Merger()
        run_automation(
            merger,
            activities=[{"id": 10, "name": "Commute", "description": ""}],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )

        self.assertEqual(len(merger.uploads), 0)
        self.assertEqual(len(merger.emails), 1)
        self.assertIn("Action required", merger.emails[0][2])
        self.assertTrue(os.path.exists(source_path))
        self.assertTrue(os.path.exists(replacement_path))

        merger.source_exists = False
        run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )

        self.assertNotIn("fix-10", JobStore(state_path).jobs)
        self.assertEqual(len(merger.uploads), 1)
        self.assertEqual(len(merger.emails), 2)
        self.assertIn("Uploaded replacements", merger.emails[1][2])
        self.assertFalse(os.path.exists(source_path))
        self.assertFalse(os.path.exists(replacement_path))

    def test_address_lookup_failure_falls_back_to_coordinates(self):
        class Geocoder:
            @staticmethod
            def reverse_geocode(location):
                raise GeocodingError("Geocoding API is unavailable")

        with patch("automation.logger.warning") as warning:
            address = _address_for((47.0, 8.0), Geocoder(), {})

        self.assertEqual(address, "47.000000, 8.000000")
        warning.assert_called_once()

    def test_clean_track_streams_are_released_during_large_scan(self):
        live_gpxs = weakref.WeakSet()
        max_live_gpxs = 0

        class Merger:
            google_maps_api_key = None

            @staticmethod
            def detect_merging_activities(activities):
                return []

            can_fix_activity = staticmethod(StravaMerger.can_fix_activity)

            @staticmethod
            def activity_from_api(activity):
                return StravaMerger.activity_from_api(activity)

            def activity_to_gpx(self, activity):
                nonlocal max_live_gpxs
                gc.collect()
                gpx = CustomGPX()
                track = gpxpy.gpx.GPXTrack()
                segment = gpxpy.gpx.GPXTrackSegment()
                segment.points.append(
                    gpxpy.gpx.GPXTrackPoint(
                        47.0,
                        8.0,
                        time=datetime(2026, 8, 13, tzinfo=timezone.utc),
                    )
                )
                track.segments.append(segment)
                gpx.tracks.append(track)
                gpx.set_activity(activity)
                live_gpxs.add(gpx)
                max_live_gpxs = max(max_live_gpxs, len(live_gpxs))
                return gpx

            @staticmethod
            def send_email(*args, **kwargs):
                raise AssertionError("Clean tracks do not require an email")

        activities = [
            {
                "id": activity_id,
                "name": f"Ride {activity_id}",
                "start_date": "2026-08-13T07:00:00Z",
                "start_date_local": "2026-08-13T09:00:00Z",
                "elapsed_time": 60,
                "start_latlng": [47.0, 8.0],
                "end_latlng": [47.0, 8.0],
                "gear_id": None,
                "sport_type": "Ride",
                "description": "",
                "commute": False,
                "trainer": False,
            }
            for activity_id in range(10)
        ]

        run_automation(
            Merger(),
            activities=activities,
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=os.path.join(self.temporary_directory.name, "state.json"),
            fix_holes=True,
        )

        self.assertLessEqual(max_live_gpxs, 2)

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
        artifact_path = os.path.join(
            self.temporary_directory.name, "fix-10_replacement.gpx"
        )
        with open(artifact_path, "w") as file:
            file.write("queued replacement")
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
                            "replacement": {"filepath": artifact_path},
                            "replacement_gpx": "queued replacement",
                            "artifact_paths": [artifact_path],
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

        self.assertNotIn("fix-10", JobStore(state_path).jobs)
        cancellation_logs = [
            call
            for call in info.call_args_list
            if call.args and call.args[0].startswith("Cancelled pending replacement")
        ]
        self.assertEqual(cancellation_logs[0].args[1], "Broken Ride")
        self.assertFalse(os.path.exists(artifact_path))

    def test_file_only_job_is_marked_for_rebuild_without_opening_gpx(self):
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
                            "sources": [
                                {
                                    "id": 10,
                                    "name": "Broken Ride",
                                    "description": "",
                                }
                            ],
                            "replacement": {
                                "filepath": "/path/that/does/not/exist.gpx"
                            },
                            "status": "ready",
                            "last_error": None,
                            "hole_details": {"10": []},
                            "hole_distances_meters": {"10": [500]},
                        }
                    },
                },
                file,
            )

        class Merger:
            google_maps_api_key = None

            @staticmethod
            def detect_merging_activities(activities):
                return []

        run_automation(
            Merger(),
            activities=[{"id": 10, "name": "Broken Ride", "description": ""}],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )

        job = JobStore(state_path).jobs["fix-10"]
        self.assertEqual(job["status"], "cancelled")
        self.assertTrue(job["rebuild_required"])

    def test_pending_reminder_is_sent_before_new_activity_scan(self):
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
                            "sources": [
                                {
                                    "id": 10,
                                    "name": "Broken Ride",
                                    "description": "",
                                }
                            ],
                            "status": "awaiting_deletion",
                            "last_error": "duplicate of activity 10",
                            "hole_details": {"10": []},
                            "hole_distances_meters": {"10": [500]},
                            "replacement_gpx": (
                                '<gpx xmlns="http://www.topografix.com/GPX/1/1" '
                                'version="1.1" creator="test" />'
                            ),
                        }
                    },
                },
                file,
            )

        class Merger:
            google_maps_api_key = None

            def __init__(self):
                self.emails = []

            @staticmethod
            def detect_merging_activities(activities):
                raise RuntimeError("historical scan failed")

            def send_email(self, recipient, subject, body):
                self.emails.append((recipient, subject, body))
                return True

        merger = Merger()
        activity = {"id": 10, "name": "Broken Ride", "description": ""}

        with self.assertRaisesRegex(RuntimeError, "historical scan failed"):
            run_automation(
                merger,
                activities=[activity],
                output_folder=self.temporary_directory.name,
                recipient="me@example.com",
                state_path=state_path,
            )

        self.assertEqual(len(merger.emails), 1)
        self.assertEqual(
            merger.emails[0][1], "StravaMerger - Daily report"
        )

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
                if self.source_exists:
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
                        gear_applied=True,
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
                    distance_meters=6_000,
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
        JobStore(state_path).mark_for_review(
            10,
            "Activity 10 (Broken Ride) was not repaired: 6000 m route is more "
            "than 4x the 1345 m straight-line gap.",
        )

        with (
            patch("automation.GoogleRoutesClient", Routes),
            patch("automation.GoogleGeocodingClient", Geocoder),
            patch("automation.logger.info") as info,
            patch("automation.logger.warning") as warning,
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
        self.assertEqual(merger.upload_attempts, 0)
        job = JobStore(state_path).jobs["fix-10"]
        self.assertEqual(job["status"], "awaiting_deletion")
        self.assertIsNone(job["delete_notification_recipient"])
        self.assertIsNone(JobStore(state_path).review_reason(10))
        hole_detail = job["hole_details"]["10"][0]
        self.assertEqual(hole_detail["repair_method"], "straight_line")
        self.assertEqual(hole_detail["travel_mode"], "BICYCLE")
        self.assertIn("more than 4x", hole_detail["fallback_reason"])
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
        fallback_logs = [
            call
            for call in warning.call_args_list
            if call.args and call.args[0].startswith("Using straight-line")
        ]
        self.assertEqual(len(fallback_logs), 1)

        merger.email_enabled = True
        notified = run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )
        self.assertEqual(notified.deferred_jobs, 1)
        self.assertEqual(merger.upload_attempts, 0)
        self.assertEqual(len(merger.emails), 1)
        self.assertEqual(merger.emails[0][1], "StravaMerger - Daily report")
        self.assertIn("1.35 km", merger.emails[0][2])
        self.assertIn("Startstrasse 1, Zürich", merger.emails[0][2])
        self.assertIn("Zielweg 2, Zürich", merger.emails[0][2])
        self.assertIn("Straight-line GPX coordinates were used", merger.emails[0][2])
        self.assertIn("more than 4x", merger.emails[0][2])
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
        self.assertEqual(merger.upload_attempts, 0)
        self.assertEqual(len(merger.emails), 2)
        self.assertEqual(
            merger.emails[1][1], "StravaMerger - Daily report"
        )

        for filename in os.listdir(self.temporary_directory.name):
            if filename.endswith(".gpx"):
                os.unlink(os.path.join(self.temporary_directory.name, filename))
        merger.source_exists = False
        completed = run_automation(
            merger,
            activities=[],
            output_folder=self.temporary_directory.name,
            recipient="me@example.com",
            state_path=state_path,
        )
        self.assertEqual(completed.uploaded_jobs, 1)
        self.assertNotIn("fix-10", JobStore(state_path).jobs)
        self.assertEqual(merger.upload_attempts, 1)
        self.assertEqual(len(merger.emails), 3)
        self.assertEqual(merger.emails[2][1], "StravaMerger - Daily report")
        self.assertIn("Broken Ride", merger.emails[2][2])


if __name__ == "__main__":
    unittest.main()
