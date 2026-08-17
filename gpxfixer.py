"""Headless GPX hole detection and repair using Google Maps routes."""

from __future__ import annotations

import copy
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import timedelta
from itertools import pairwise
from math import ceil

import gpxpy.gpx
import requests

from utils import CustomGPX, haversine

GOOGLE_ROUTES_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"
GOOGLE_GEOCODING_URL = "https://maps.googleapis.com/maps/api/geocode/json"

BICYCLE_SPORTS = {
    "EBikeRide",
    "EMountainBikeRide",
    "GravelRide",
    "Handcycle",
    "MountainBikeRide",
    "Ride",
    "Velomobile",
}
WALK_SPORTS = {
    "Hike",
    "Run",
    "TrailRun",
    "Walk",
    "Wheelchair",
}
SUPPORTED_TRAVEL_MODES = {"BICYCLE", "DRIVE", "TWO_WHEELER", "WALK"}
MAX_HOLE_DISTANCE = 20_000.0
MAX_ROUTE_FACTOR = 4.0
MAX_STRAIGHT_LINE_POINT_INTERVAL_SECONDS = 3.0


class RouteError(RuntimeError):
    """Raised when a missing track section cannot be routed safely."""


class NoRouteError(RouteError):
    """Raised when Google returns no usable route geometry."""


class RouteTooIndirectError(RouteError):
    """Raised when a Google route is implausibly longer than the direct gap."""


class GeocodingError(RuntimeError):
    """Raised when a coordinate cannot be reverse geocoded."""


@dataclass(frozen=True)
class TrackHole:
    """A discontinuity between two adjacent GPX points."""

    track_index: int
    segment_index: int
    point_index: int
    elapsed_seconds: float
    distance_meters: float
    origin: tuple[float, float]
    destination: tuple[float, float]


@dataclass(frozen=True)
class Route:
    """Geometry and summary returned by a routing provider."""

    points: tuple[tuple[float, float], ...]
    distance_meters: float
    duration_seconds: float


def detect_holes(
    gpx: gpxpy.gpx.GPX,
    *,
    time_threshold: float = 5.0,
    distance_threshold: float = 400.0,
) -> list[TrackHole]:
    """Return adjacent point pairs that exceed both supplied thresholds."""
    holes: list[TrackHole] = []
    for track_index, track in enumerate(gpx.tracks):
        for segment_index, segment in enumerate(track.segments):
            for point_index in range(1, len(segment.points)):
                previous = segment.points[point_index - 1]
                current = segment.points[point_index]
                if previous.time is None or current.time is None:
                    continue
                elapsed_seconds = (current.time - previous.time).total_seconds()
                if elapsed_seconds <= time_threshold:
                    continue
                distance_meters = haversine(
                    [previous.latitude, previous.longitude],
                    [current.latitude, current.longitude],
                )
                if distance_meters <= distance_threshold:
                    continue
                holes.append(
                    TrackHole(
                        track_index=track_index,
                        segment_index=segment_index,
                        point_index=point_index,
                        elapsed_seconds=elapsed_seconds,
                        distance_meters=distance_meters,
                        origin=(previous.latitude, previous.longitude),
                        destination=(current.latitude, current.longitude),
                    )
                )
    return holes


def travel_mode_for_sport(sport: str | None) -> str | None:
    """Map Strava sport types to conservative Google Routes travel modes."""
    if sport in BICYCLE_SPORTS:
        return "BICYCLE"
    if sport in WALK_SPORTS:
        return "WALK"
    return None


def decode_google_polyline(encoded: str) -> tuple[tuple[float, float], ...]:
    """Decode a Google encoded polyline without adding another dependency."""
    coordinates: list[tuple[float, float]] = []
    latitude = 0
    longitude = 0
    index = 0

    while index < len(encoded):
        deltas = []
        for _ in range(2):
            result = 0
            shift = 0
            while True:
                if index >= len(encoded):
                    raise RouteError("Google returned a malformed encoded polyline.")
                value = ord(encoded[index]) - 63
                index += 1
                result |= (value & 0x1F) << shift
                shift += 5
                if value < 0x20:
                    break
            deltas.append(~(result >> 1) if result & 1 else result >> 1)
        latitude += deltas[0]
        longitude += deltas[1]
        coordinates.append((latitude / 1e5, longitude / 1e5))

    return tuple(coordinates)


class GoogleRoutesClient:
    """Small client for the Google Maps Routes API Compute Routes method."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("A Google Maps API key is required for hole repair.")
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()

    def route(
        self,
        origin: Sequence[float],
        destination: Sequence[float],
        travel_mode: str,
    ) -> Route:
        mode = travel_mode.upper()
        if mode not in SUPPORTED_TRAVEL_MODES:
            supported = ", ".join(sorted(SUPPORTED_TRAVEL_MODES))
            raise ValueError(
                f"Unsupported Google travel mode {mode!r}; use {supported}."
            )

        payload = {
            "origin": {"location": {"latLng": _lat_lng(origin)}},
            "destination": {"location": {"latLng": _lat_lng(destination)}},
            "travelMode": mode,
            "computeAlternativeRoutes": False,
            "polylineQuality": "HIGH_QUALITY",
            "polylineEncoding": "ENCODED_POLYLINE",
            "units": "METRIC",
        }
        try:
            response = self.session.post(
                GOOGLE_ROUTES_URL,
                headers={
                    "Content-Type": "application/json",
                    "X-Goog-Api-Key": self.api_key,
                    "X-Goog-FieldMask": (
                        "routes.duration,routes.distanceMeters,"
                        "routes.polyline.encodedPolyline"
                    ),
                },
                json=payload,
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            if not hasattr(error, "response") or error.response is None:
                raise RouteError(f"Google Routes request failed: {error}") from error
            response = error.response
            detail = _google_error_message(response)
            raise RouteError(f"Google Routes request failed: {detail}") from error

        try:
            body = response.json()
        except ValueError as error:
            raise RouteError("Google Routes returned invalid JSON.") from error
        if not body.get("routes"):
            raise NoRouteError("Google Routes returned no route.")
        route = body["routes"][0]
        encoded = route.get("polyline", {}).get("encodedPolyline")
        if not encoded:
            raise NoRouteError("Google Routes returned no route geometry.")

        decoded = decode_google_polyline(encoded)
        points = _with_exact_endpoints(tuple(origin), tuple(destination), decoded)
        geometry_distance = _route_length(points)
        return Route(
            points=points,
            distance_meters=max(
                float(route.get("distanceMeters", 0)), geometry_distance
            ),
            duration_seconds=_parse_duration(route.get("duration", "0s")),
        )


class GoogleGeocodingClient:
    """Small client for Google Maps Geocoding API reverse lookups."""

    def __init__(
        self,
        api_key: str,
        *,
        timeout: float = 30.0,
        session: requests.Session | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("A Google Maps API key is required for address lookup.")
        self.api_key = api_key
        self.timeout = timeout
        self.session = session or requests.Session()

    def reverse_geocode(self, location: Sequence[float]) -> str | None:
        """Return Google's closest formatted address, or ``None`` if none exists."""
        latitude, longitude = map(float, location)
        try:
            response = self.session.get(
                GOOGLE_GEOCODING_URL,
                params={
                    "latlng": f"{latitude:.7f},{longitude:.7f}",
                    "key": self.api_key,
                },
                timeout=self.timeout,
            )
            response.raise_for_status()
        except requests.RequestException as error:
            raise GeocodingError(
                f"Google reverse-geocoding request failed: {error}"
            ) from error

        try:
            body = response.json()
        except ValueError as error:
            raise GeocodingError("Google Geocoding returned invalid JSON.") from error
        status = body.get("status")
        if status == "ZERO_RESULTS":
            return None
        if status != "OK":
            detail = body.get("error_message") or status or "unknown error"
            raise GeocodingError(f"Google reverse geocoding failed: {detail}")
        results = body.get("results") or []
        address = results[0].get("formatted_address") if results else None
        return address or None


def validate_route(
    hole: TrackHole,
    route: Route,
) -> None:
    """Reject routes that are too large or implausibly indirect."""
    if hole.distance_meters > MAX_HOLE_DISTANCE:
        raise RouteError(
            f"{hole.distance_meters:.0f} m straight-line gap exceeds the "
            f"{MAX_HOLE_DISTANCE:.0f} m safety limit."
        )
    if route.distance_meters > hole.distance_meters * MAX_ROUTE_FACTOR:
        raise RouteTooIndirectError(
            f"{route.distance_meters:.0f} m route is more than {MAX_ROUTE_FACTOR:g}x "
            f"the {hole.distance_meters:.0f} m straight-line gap."
        )
    if len(route.points) < 3:
        raise NoRouteError("The route contains no points between the gap endpoints.")


def straight_line_route(hole: TrackHole) -> Route:
    """Create direct-line coordinates at intervals of at most three seconds."""
    steps = max(
        2, ceil(hole.elapsed_seconds / MAX_STRAIGHT_LINE_POINT_INTERVAL_SECONDS)
    )
    latitude_delta = hole.destination[0] - hole.origin[0]
    longitude_delta = hole.destination[1] - hole.origin[1]
    points = tuple(
        (
            hole.origin[0] + latitude_delta * step / steps,
            hole.origin[1] + longitude_delta * step / steps,
        )
        for step in range(steps + 1)
    )
    return Route(
        points=points,
        distance_meters=hole.distance_meters,
        duration_seconds=hole.elapsed_seconds,
    )


def repair_holes(
    gpx: CustomGPX,
    repairs: Iterable[tuple[TrackHole, Route]],
) -> CustomGPX:
    """Return a copy of ``gpx`` with routed points inserted into each hole."""
    repaired = copy.deepcopy(gpx)
    ordered_repairs = sorted(
        repairs,
        key=lambda item: (
            item[0].track_index,
            item[0].segment_index,
            item[0].point_index,
        ),
        reverse=True,
    )

    for hole, route in ordered_repairs:
        segment = repaired.tracks[hole.track_index].segments[hole.segment_index]
        previous = segment.points[hole.point_index - 1]
        current = segment.points[hole.point_index]
        new_points = _interpolate_route_points(previous, current, route.points)
        segment.points[hole.point_index : hole.point_index] = new_points

    return repaired


def _interpolate_route_points(
    start: gpxpy.gpx.GPXTrackPoint,
    end: gpxpy.gpx.GPXTrackPoint,
    route_points: Sequence[tuple[float, float]],
) -> list[gpxpy.gpx.GPXTrackPoint]:
    if start.time is None or end.time is None:
        raise RouteError("Cannot repair a hole whose endpoints have no timestamps.")

    points = _with_exact_endpoints(
        (start.latitude, start.longitude),
        (end.latitude, end.longitude),
        tuple(route_points),
    )
    cumulative = [0.0]
    for left, right in pairwise(points):
        cumulative.append(cumulative[-1] + haversine(list(left), list(right)))
    total_distance = cumulative[-1]
    if total_distance <= 0:
        raise RouteError("Cannot interpolate a zero-length route.")

    elapsed = end.time - start.time
    result = []
    for coordinate, distance in zip(points[1:-1], cumulative[1:-1]):
        fraction = distance / total_distance
        elevation = _interpolate_optional(start.elevation, end.elevation, fraction)
        result.append(
            gpxpy.gpx.GPXTrackPoint(
                latitude=coordinate[0],
                longitude=coordinate[1],
                elevation=elevation,
                time=start.time + timedelta(seconds=elapsed.total_seconds() * fraction),
            )
        )
    return result


def _with_exact_endpoints(
    origin: tuple[float, float],
    destination: tuple[float, float],
    points: Sequence[tuple[float, float]],
) -> tuple[tuple[float, float], ...]:
    result = list(points)
    if not result or haversine(list(origin), list(result[0])) > 1.0:
        result.insert(0, origin)
    else:
        result[0] = origin
    if haversine(list(destination), list(result[-1])) > 1.0:
        result.append(destination)
    else:
        result[-1] = destination

    deduplicated = [result[0]]
    for point in result[1:]:
        if haversine(list(deduplicated[-1]), list(point)) > 0.5:
            deduplicated.append(point)
    return tuple(deduplicated)


def _route_length(points: Sequence[tuple[float, float]]) -> float:
    return sum(haversine(list(left), list(right)) for left, right in pairwise(points))


def _lat_lng(coordinate: Sequence[float]) -> dict[str, float]:
    return {"latitude": float(coordinate[0]), "longitude": float(coordinate[1])}


def _parse_duration(value: str) -> float:
    if not value.endswith("s"):
        raise RouteError(f"Unexpected Google route duration {value!r}.")
    return float(value[:-1])


def _interpolate_optional(
    start: float | None, end: float | None, fraction: float
) -> float | None:
    if start is not None and end is not None:
        return start + ((end - start) * fraction)
    return start if start is not None else end


def _google_error_message(response: requests.Response) -> str:
    try:
        payload = response.json()
    except ValueError:
        return response.text or f"HTTP {response.status_code}"
    return payload.get("error", {}).get("message") or response.text
