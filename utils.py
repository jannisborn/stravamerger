from dataclasses import dataclass
from datetime import datetime
from math import atan2, cos, radians, sin, sqrt
from typing import Optional, Tuple

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
    return datetime.fromisoformat(date_str).date()


@dataclass
class Activity:
    name: str
    id: int
    start_date: str
    start_coords: Tuple[float, float]
    filepath: Optional[str] = None
    sport: Optional[str] = None
    description: str = ""
    url: Optional[str] = None


class CustomGPX(gpxpy.gpx.GPX):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.activity = None

    def set_activity(self, activity: Activity):
        self.activity = activity


NAME_DICT = {
    (47.310019, 8.544049): "IBM",
}
