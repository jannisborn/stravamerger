from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from fnmatch import fnmatchcase
from math import atan2, cos, radians, sin, sqrt

import gpxpy.gpx


def haversine(loc1: list, loc2: list) -> float:
    """
    Calculate the great circle distance in meters between two points
    on the earth (specified in decimal degrees)

    Args:
    loc1 (list): [latitude, longitude] of point 1.
    loc2 (list): [latitude, longitude] of point 2.

    Returns:
    float: Distance between loc1 and loc2 in meters.
    """
    R = 6371e3  # Radius of the Earth in meters
    lat1, lon1 = map(radians, loc1)
    lat2, lon2 = map(radians, loc2)

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = sin(dlat / 2) ** 2 + cos(lat1) * cos(lat2) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))

    distance = R * c
    return distance


def parse_date(date_str: str) -> datetime.date:
    """Parse a datetime string to a date object."""
    date_str = date_str.rstrip("Z")
    return datetime.fromisoformat(date_str)


def parse_datetime(date_str: str) -> datetime:
    """Parse an ISO 8601 timestamp, including Strava's trailing ``Z`` form."""
    return datetime.fromisoformat(date_str.replace("Z", "+00:00"))


@dataclass
class Activity:
    name: str
    id: int
    start_date: str
    end_date: str
    start_coords: tuple[float, float]
    end_coords: tuple[float, float]
    gear_id: str | None = None
    filepath: str | None = None
    sport: str | None = None
    description: str = ""
    url: str | None = None
    start_date_utc: str | None = None
    commute: bool = False
    trainer: bool = False
    external_id: str | None = None
    source_ids: tuple[int, ...] = field(default_factory=tuple)


class CustomGPX(gpxpy.gpx.GPX):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.activity = None

    def set_activity(self, activity: Activity):
        self.activity = activity


NAME_DICT = {
    (47.310019, 8.544049): "IBM",
}

DEFAULT_GENERIC_NAMES = (
    "Fahrt am *",
    "Lauf am *",
    "Morning Run",
    "Afternoon Run",
    "Evening Run",
    "Morning Ride",
    "Afternoon Ride",
    "Evening Ride",
)


def is_generic_activity_name(
    name: str,
    patterns: Sequence[str] = DEFAULT_GENERIC_NAMES,
) -> bool:
    """Return whether an activity title matches a configured case-insensitive glob."""
    normalized = name.strip().casefold()
    for pattern in patterns:
        normalized_pattern = pattern.strip().casefold()
        if fnmatchcase(normalized, normalized_pattern):
            return True
        if (
            normalized_pattern.endswith(" *")
            and normalized == normalized_pattern[:-2]
        ):
            return True
    return False
