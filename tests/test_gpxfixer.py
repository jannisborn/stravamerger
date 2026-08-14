import unittest
from datetime import datetime, timedelta, timezone

import gpxpy.gpx

from gpxfixer import (
    GOOGLE_GEOCODING_URL,
    GOOGLE_ROUTES_URL,
    GoogleGeocodingClient,
    GoogleRoutesClient,
    Route,
    RouteError,
    decode_google_polyline,
    detect_holes,
    repair_holes,
    travel_mode_for_sport,
    validate_route,
)
from utils import Activity, CustomGPX


def make_gpx():
    gpx = CustomGPX()
    track = gpxpy.gpx.GPXTrack()
    segment = gpxpy.gpx.GPXTrackSegment()
    track.segments.append(segment)
    gpx.tracks.append(track)
    start = datetime(2026, 8, 13, 7, tzinfo=timezone.utc)
    segment.points.extend(
        [
            gpxpy.gpx.GPXTrackPoint(47.0, 8.0, elevation=400, time=start),
            gpxpy.gpx.GPXTrackPoint(
                47.01,
                8.01,
                elevation=500,
                time=start + timedelta(seconds=120),
            ),
        ]
    )
    gpx.set_activity(
        Activity(
            name="Morning Ride",
            id=42,
            start_date="2026-08-13T09:00:00Z",
            end_date="2026-08-13T09:02:00Z",
            start_coords=(47.0, 8.0),
            end_coords=(47.01, 8.01),
            sport="Ride",
        )
    )
    return gpx


class GpxFixerTests(unittest.TestCase):
    def test_decode_known_google_polyline(self):
        self.assertEqual(
            decode_google_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@"),
            ((38.5, -120.2), (40.7, -120.95), (43.252, -126.453)),
        )

    def test_detect_hole_requires_both_thresholds(self):
        gpx = make_gpx()
        holes = detect_holes(gpx, time_threshold=5, distance_threshold=400)
        self.assertEqual(len(holes), 1)
        self.assertEqual(holes[0].point_index, 1)
        self.assertEqual(holes[0].elapsed_seconds, 120)

        self.assertEqual(
            detect_holes(gpx, time_threshold=200, distance_threshold=400), []
        )
        self.assertEqual(
            detect_holes(gpx, time_threshold=5, distance_threshold=2_000), []
        )

    def test_repair_interpolates_time_and_elevation_without_mutating_source(self):
        gpx = make_gpx()
        hole = detect_holes(gpx)[0]
        route = Route(
            points=((47.0, 8.0), (47.005, 8.005), (47.01, 8.01)),
            distance_meters=1_345,
            duration_seconds=100,
        )
        repaired = repair_holes(gpx, [(hole, route)])

        self.assertEqual(len(gpx.tracks[0].segments[0].points), 2)
        points = repaired.tracks[0].segments[0].points
        self.assertEqual(len(points), 3)
        self.assertAlmostEqual(points[1].elevation, 450, places=1)
        self.assertAlmostEqual(
            (points[1].time - points[0].time).total_seconds(), 60, delta=0.2
        )
        self.assertIsNot(repaired.activity, gpx.activity)
        self.assertEqual(repaired.activity.id, 42)

    def test_route_safety_limits(self):
        hole = detect_holes(make_gpx())[0]
        validate_route(
            hole,
            Route(
                points=(hole.origin, (47.005, 8.005), hole.destination),
                distance_meters=2_000,
                duration_seconds=100,
            ),
        )
        with self.assertRaises(RouteError):
            validate_route(
                hole,
                Route(
                    points=(hole.origin, (47.005, 8.005), hole.destination),
                    distance_meters=10_000,
                    duration_seconds=100,
                ),
            )

    def test_conservative_sport_mode_mapping(self):
        self.assertEqual(travel_mode_for_sport("Ride"), "BICYCLE")
        self.assertEqual(travel_mode_for_sport("TrailRun"), "WALK")
        self.assertIsNone(travel_mode_for_sport("Swim"))

    def test_google_routes_client_uses_routes_api_and_high_quality_polyline(self):
        class Response:
            status_code = 200
            text = ""

            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "routes": [
                        {
                            "distanceMeters": 1000,
                            "duration": "120s",
                            "polyline": {
                                "encodedPolyline": "_p~iF~ps|U_ulLnnqC_mqNvxq`@"
                            },
                        }
                    ]
                }

        class Session:
            def post(self, url, **kwargs):
                self.url = url
                self.kwargs = kwargs
                return Response()

        session = Session()
        client = GoogleRoutesClient("not-a-real-key", session=session)
        route = client.route((38.5, -120.2), (43.252, -126.453), "BICYCLE")

        self.assertEqual(session.url, GOOGLE_ROUTES_URL)
        self.assertEqual(session.kwargs["json"]["travelMode"], "BICYCLE")
        self.assertEqual(session.kwargs["json"]["polylineQuality"], "HIGH_QUALITY")
        self.assertEqual(session.kwargs["headers"]["X-Goog-Api-Key"], "not-a-real-key")
        self.assertGreaterEqual(route.distance_meters, 1000)

    def test_google_reverse_geocoding_returns_formatted_address(self):
        class Response:
            @staticmethod
            def raise_for_status():
                return None

            @staticmethod
            def json():
                return {
                    "status": "OK",
                    "results": [{"formatted_address": "Example Street 1, 8000 Zürich"}],
                }

        class Session:
            def get(self, url, **kwargs):
                self.url = url
                self.kwargs = kwargs
                return Response()

        session = Session()
        client = GoogleGeocodingClient("not-a-real-key", session=session)

        address = client.reverse_geocode((47.3769, 8.5417))

        self.assertEqual(address, "Example Street 1, 8000 Zürich")
        self.assertEqual(session.url, GOOGLE_GEOCODING_URL)
        self.assertEqual(session.kwargs["params"]["latlng"], "47.3769000,8.5417000")
        self.assertEqual(session.kwargs["params"]["key"], "not-a-real-key")


if __name__ == "__main__":
    unittest.main()
