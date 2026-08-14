"""Orchestrate merge and hole-repair replacement jobs."""

from __future__ import annotations

import copy
import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Any

import gpxpy
from loguru import logger

from app import BOT_MARKER, GPXTPX_NAMESPACE, StravaMerger
from gpxfixer import (
    MAX_HOLE_DISTANCE,
    GeocodingError,
    GoogleGeocodingClient,
    GoogleRoutesClient,
    RouteError,
    TrackHole,
    detect_holes,
    repair_holes,
    travel_mode_for_sport,
    validate_route,
)
from utils import Activity, CustomGPX

MAX_HOLES_PER_ACTIVITY = 5
READ_REQUEST_RESERVE = 10


@dataclass
class AutomationSummary:
    merged_jobs: int = 0
    repaired_jobs: int = 0
    repaired_holes: int = 0
    uploaded_jobs: int = 0
    deferred_jobs: int = 0
    review_messages: list[str] = field(default_factory=list)


class JobStore:
    """A small durable queue for replacements that may span scheduled runs."""

    def __init__(self, path: str) -> None:
        self.path = os.path.abspath(path)
        self.data = {"version": 1, "jobs": {}}
        if os.path.exists(self.path):
            with open(self.path, "r") as file:
                loaded = json.load(file)
            if loaded.get("version") != 1 or not isinstance(loaded.get("jobs"), dict):
                raise ValueError(f"Unsupported StravaMerger state file: {self.path}")
            self.data = loaded
        self.data.setdefault("checks", {})
        self.data.setdefault("reviews", {})

    @property
    def jobs(self) -> dict[str, dict[str, Any]]:
        return self.data["jobs"]

    def claimed_source_ids(self) -> set[int]:
        claimed = set()
        for job in self.jobs.values():
            if job["status"] not in {"cancelled", "complete"}:
                claimed.update(job["source_ids"])
        return claimed

    def was_checked_clean(self, activity_id: int) -> bool:
        return str(activity_id) in self.data["checks"]

    def mark_checked_clean(self, activity_id: int) -> None:
        self.data["checks"][str(activity_id)] = {"checked_at": _now()}
        self.save()

    def review_reason(self, activity_id: int) -> str | None:
        review = self.data["reviews"].get(str(activity_id))
        return review["reason"] if review else None

    def mark_for_review(self, activity_id: int, reason: str) -> None:
        self.data["reviews"][str(activity_id)] = {
            "reason": reason,
            "reviewed_at": _now(),
        }
        self.save()

    def add(
        self,
        *,
        job_id: str,
        kind: str,
        source_activities: list[Activity],
        replacement: CustomGPX,
        hole_distances_meters: dict[int, list[float]] | None = None,
        hole_details: dict[int, list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        replacement_data = asdict(replacement.activity)
        replacement_data["filepath"] = os.path.abspath(replacement.activity.filepath)
        hole_distances_meters = hole_distances_meters or {}
        hole_details = hole_details or {}
        job = {
            "id": job_id,
            "kind": kind,
            "source_ids": [source.id for source in source_activities],
            "sources": [asdict(source) for source in source_activities],
            "hole_distances_meters": {
                str(source.id): [
                    float(distance)
                    for distance in hole_distances_meters.get(source.id, [])
                ]
                for source in source_activities
            },
            "hole_details": {
                str(source.id): hole_details.get(source.id, [])
                for source in source_activities
            },
            "replacement": replacement_data,
            "status": "ready",
            "delete_notified": False,
            "delete_notification_recipient": None,
            "confirmation_notification_recipient": None,
            "created_at": _now(),
            "updated_at": _now(),
            "last_error": None,
            "uploaded_activity_id": None,
        }
        self.jobs[job_id] = job
        self.save()
        return job

    def save(self) -> None:
        state_dir = os.path.dirname(self.path) or "."
        os.makedirs(state_dir, exist_ok=True)
        descriptor, temporary_path = tempfile.mkstemp(
            dir=state_dir, prefix=".stravamerger-state-", text=True
        )
        try:
            with os.fdopen(descriptor, "w") as file:
                json.dump(self.data, file, indent=2)
                file.write("\n")
            os.replace(temporary_path, self.path)
        except Exception:
            if os.path.exists(temporary_path):
                os.unlink(temporary_path)
            raise


def run_automation(
    merger: StravaMerger,
    *,
    activities: list[dict[str, Any]],
    output_folder: str,
    recipient: str,
    state_path: str,
    fix_holes: bool = False,
    hole_time_threshold: float = 5.0,
    hole_distance_threshold: float = 400.0,
) -> AutomationSummary:
    """Resume queued jobs, discover new replacements, and upload what is ready."""
    if hole_time_threshold < 0 or hole_distance_threshold < 0:
        raise ValueError("Hole detection thresholds cannot be negative.")
    store = JobStore(state_path)
    summary = AutomationSummary()
    activities_by_id = {activity["id"]: activity for activity in activities}
    source_exists_cache = {activity_id: True for activity_id in activities_by_id}
    _refresh_pending_source_activities(
        merger,
        store,
        activities_by_id,
        source_exists_cache,
    )
    _resume_jobs(
        merger,
        store,
        recipient=recipient,
        summary=summary,
        activities_by_id=activities_by_id,
        source_exists_cache=source_exists_cache,
    )
    # Pending-action email is intentionally sent before the potentially expensive
    # historical scan, so a later process failure cannot suppress the daily reminder.
    _notify_outstanding_deletions(
        merger,
        store,
        recipient,
        activities_by_id=activities_by_id,
        source_exists_cache=source_exists_cache,
    )

    claimed = store.claimed_source_ids()
    available = [activity for activity in activities if activity["id"] not in claimed]
    merge_chains = merger.detect_merging_activities(available)
    merged_source_ids = {activity.id for chain in merge_chains for activity in chain}
    google_client = None
    geocoding_client = None
    address_cache: dict[tuple[float, float], str] = {}
    scan_budget_exhausted = False

    def defer_remaining_scans() -> None:
        nonlocal scan_budget_exhausted
        if not scan_budget_exhausted:
            logger.warning(
                "Keeping {} Strava read requests in reserve; remaining new activities "
                "will be checked on a later run.",
                READ_REQUEST_RESERVE,
            )
        scan_budget_exhausted = True

    def fetch(activity: Activity) -> CustomGPX:
        # GPX streams can be very large. Keeping every checked track cached caused
        # long initial scans to be killed by the OS for excessive memory use.
        return merger.activity_to_gpx(activity)

    def repair(gpx: CustomGPX) -> tuple[CustomGPX | None, list[dict[str, Any]]]:
        nonlocal geocoding_client, google_client
        activity = gpx.activity
        if not fix_holes or any(
            marker in activity.description.lower() for marker in ("nofix", "nomerge")
        ):
            return gpx, []
        if store.review_reason(activity.id):
            return None, []
        holes = detect_holes(
            gpx,
            time_threshold=hole_time_threshold,
            distance_threshold=hole_distance_threshold,
        )
        if not holes:
            store.mark_checked_clean(activity.id)
            return gpx, []

        if merger.google_maps_api_key:
            geocoding_client = geocoding_client or GoogleGeocodingClient(
                merger.google_maps_api_key
            )
        hole_details = [
            _describe_hole(hole, geocoding_client, address_cache) for hole in holes
        ]
        for detail in hole_details:
            logger.info(
                'Detected GPS hole in "{}": {} between {} and {}.',
                activity.name,
                _format_distance(detail["distance_meters"]),
                detail["origin_address"],
                detail["destination_address"],
            )
        if len(holes) > MAX_HOLES_PER_ACTIVITY:
            message = (
                f"Activity {activity.id} ({activity.name}) has {len(holes)} holes, "
                f"exceeding the automatic limit of {MAX_HOLES_PER_ACTIVITY}."
            )
            summary.review_messages.append(message)
            store.mark_for_review(activity.id, message)
            return None, []

        mode = travel_mode_for_sport(activity.sport)
        if mode is None:
            message = (
                f"Activity {activity.id} ({activity.name}) has {len(holes)} hole(s), "
                f"but sport type {activity.sport!r} has no safe automatic Google route mode."
            )
            summary.review_messages.append(message)
            store.mark_for_review(activity.id, message)
            return None, []
        if not merger.google_maps_api_key:
            summary.review_messages.append(
                f"Activity {activity.id} ({activity.name}) has {len(holes)} hole(s), "
                "but no Google Maps API key is configured."
            )
            return None, []
        google_client = google_client or GoogleRoutesClient(merger.google_maps_api_key)

        repairs = []
        try:
            for hole in holes:
                if hole.distance_meters > MAX_HOLE_DISTANCE:
                    raise RouteError(
                        f"{hole.distance_meters:.0f} m straight-line gap exceeds the "
                        f"{MAX_HOLE_DISTANCE:.0f} m safety limit."
                    )
            for hole in holes:
                route = google_client.route(hole.origin, hole.destination, mode)
                validate_route(hole, route)
                repairs.append((hole, route))
        except RouteError as error:
            message = (
                f"Activity {activity.id} ({activity.name}) was not repaired: {error}"
            )
            summary.review_messages.append(message)
            store.mark_for_review(activity.id, message)
            return None, []
        return repair_holes(gpx, repairs), hole_details

    new_job_ids = []
    for chain in merge_chains:
        requests_needed = len(chain) + sum(
            _activity_detail_request_needed(
                merger,
                activity.id,
                activities_by_id,
            )
            for activity in chain
        )
        if not _has_read_capacity(merger, requests_needed):
            defer_remaining_scans()
            break
        detailed_activities = [
            _get_detailed_activity(
                merger,
                activity.id,
                activities_by_id,
                source_exists_cache,
            )
            for activity in chain
        ]
        if any(activity is None for activity in detailed_activities):
            continue
        if any(
            "nomerge" in (activity.get("description") or "").lower()
            or StravaMerger.is_bot_activity(activity)
            for activity in detailed_activities
        ):
            continue
        chain = [
            merger.activity_from_api(activity) for activity in detailed_activities
        ]
        if any(
            store.review_reason(activity.id)
            and "nofix" not in activity.description.lower()
            for activity in chain
        ):
            continue
        original_gpxs = [fetch(activity) for activity in chain]
        replacement_inputs = []
        repaired_count = 0
        hole_distances_meters = {}
        hole_details = {}
        for original in original_gpxs:
            repaired, details = repair(original)
            if repaired is None:
                replacement_inputs = []
                break
            replacement_inputs.append(repaired)
            hole_details[original.activity.id] = details
            hole_distances_meters[original.activity.id] = [
                detail["distance_meters"] for detail in details
            ]
            repaired_count += len(details)
        if not replacement_inputs:
            continue

        replacement_activity = merger.get_new_activity(replacement_inputs)
        replacement = merger([replacement_inputs], [replacement_activity])[0]
        source_ids = [activity.id for activity in chain]
        job_id = "merge-" + "-".join(map(str, source_ids))
        merger.save_replacement(
            original_gpxs,
            replacement,
            folder=output_folder,
            prefix=job_id,
        )
        store.add(
            job_id=job_id,
            kind="merge",
            source_activities=chain,
            replacement=replacement,
            hole_distances_meters=hole_distances_meters,
            hole_details=hole_details,
        )
        new_job_ids.append(job_id)
        summary.merged_jobs += 1
        summary.repaired_holes += repaired_count

    for api_activity in available if fix_holes and not scan_budget_exhausted else []:
        if (
            api_activity["id"] in merged_source_ids
            or not api_activity.get("start_latlng")
            or StravaMerger.is_bot_activity(api_activity)
            or (
                "description" in api_activity
                and not merger.can_fix_activity(api_activity)
            )
            or store.was_checked_clean(api_activity["id"])
            or store.review_reason(api_activity["id"])
        ):
            continue
        requests_needed = 1 + _activity_detail_request_needed(
            merger,
            api_activity["id"],
            activities_by_id,
        )
        if not _has_read_capacity(merger, requests_needed):
            defer_remaining_scans()
            break
        api_activity = _get_detailed_activity(
            merger,
            api_activity["id"],
            activities_by_id,
            source_exists_cache,
        )
        if api_activity is None or not merger.can_fix_activity(api_activity):
            continue
        source = merger.activity_from_api(api_activity)
        original = fetch(source)
        repaired, hole_details = repair(original)
        if repaired is None or not hole_details:
            continue

        replacement = repaired
        replacement.activity = _fixed_activity(
            source,
            len(hole_details),
            name=merger.fixed_activity_name(source, replacement),
        )
        job_id = f"fix-{source.id}"
        merger.save_replacement(
            [original],
            replacement,
            folder=output_folder,
            prefix=job_id,
        )
        store.add(
            job_id=job_id,
            kind="fix",
            source_activities=[source],
            replacement=replacement,
            hole_distances_meters={
                source.id: [detail["distance_meters"] for detail in hole_details]
            },
            hole_details={source.id: hole_details},
        )
        new_job_ids.append(job_id)
        summary.repaired_jobs += 1
        summary.repaired_holes += len(hole_details)

    if new_job_ids:
        _upload_jobs(merger, store, new_job_ids, recipient, summary)
        _notify_outstanding_deletions(
            merger,
            store,
            recipient,
            activities_by_id=activities_by_id,
            source_exists_cache=source_exists_cache,
        )

    if summary.review_messages:
        merger.send_email(
            recipient,
            subject="StravaMerger - Tracks requiring review",
            body=_review_mail_body(summary.review_messages),
        )
    return summary


def _resume_jobs(
    merger: StravaMerger,
    store: JobStore,
    *,
    recipient: str,
    summary: AutomationSummary,
    activities_by_id: dict[int, dict[str, Any]],
    source_exists_cache: dict[int, bool],
) -> None:
    ready = []
    confirmation_pending = []
    for job_id, job in store.jobs.items():
        for source in job.get("sources", []):
            current = activities_by_id.get(source["id"])
            if current is not None:
                source["name"] = current.get("name", source["name"])
                source["description"] = current.get(
                    "description", source.get("description", "")
                ) or ""
        opt_out = _job_opt_out(job, activities_by_id)
        if opt_out and job["status"] in {"awaiting_deletion", "ready"}:
            activity_name, marker = opt_out
            job["status"] = "cancelled"
            job["last_error"] = (
                f'Cancelled because "{activity_name}" contains {marker}.'
            )
            job["updated_at"] = _now()
            logger.info(
                'Cancelled pending replacement for "{}": description contains {}.',
                activity_name,
                marker,
            )
            continue
        if job["status"] == "cancelled":
            continue
        if job["status"] in {"uploaded", "complete"}:
            if job.get("confirmation_notification_recipient") != recipient:
                confirmation_pending.append(job_id)
            if job["status"] == "uploaded" and not any(
                _source_exists(merger, source_id, source_exists_cache)
                for source_id in job["source_ids"]
            ):
                job["status"] = "complete"
                job["updated_at"] = _now()
            continue
        if job["status"] == "ready":
            duplicate_id = StravaMerger.duplicate_activity_id(job.get("last_error"))
            if duplicate_id in job["source_ids"]:
                job["status"] = "awaiting_deletion"
                job["updated_at"] = _now()
        if job["status"] == "awaiting_deletion":
            if any(
                _source_exists(merger, source_id, source_exists_cache)
                for source_id in job["source_ids"]
            ):
                summary.deferred_jobs += 1
                continue
            job["status"] = "ready"
            job["updated_at"] = _now()
        if job["status"] == "ready":
            ready.append(job_id)
    store.save()

    if confirmation_pending:
        _notify_confirmations(merger, store, recipient, confirmation_pending)
    if ready:
        _upload_jobs(merger, store, ready, recipient, summary)


def _job_opt_out(
    job: dict[str, Any],
    activities_by_id: dict[int, dict[str, Any]],
) -> tuple[str, str] | None:
    """Return the source name and marker that cancels a queued replacement."""
    for source in job["sources"]:
        activity = activities_by_id.get(source["id"])
        if activity is None:
            continue
        description = (activity.get("description") or "").lower()
        if "nomerge" in description:
            return activity["name"], "nomerge"
        if "nofix" in description and (
            job["kind"] == "fix"
            or job.get("hole_details", {}).get(str(source["id"]))
        ):
            return activity["name"], "nofix"
    return None


def _source_exists(
    merger: StravaMerger,
    source_id: int,
    cache: dict[int, bool],
) -> bool:
    if source_id not in cache:
        if not _has_read_capacity(merger, 1):
            return True
        cache[source_id] = merger.activity_exists(source_id)
    return cache[source_id]


def _has_read_capacity(merger: StravaMerger, requests_needed: int) -> bool:
    has_capacity = getattr(merger, "has_read_capacity", None)
    if not callable(has_capacity):
        return True
    return has_capacity(requests_needed, reserve=READ_REQUEST_RESERVE)


def _activity_detail_request_needed(
    merger: StravaMerger,
    activity_id: int,
    activities_by_id: dict[int, dict[str, Any]],
) -> int:
    activity = activities_by_id.get(activity_id)
    if activity is not None and "description" in activity:
        return 0
    return int(callable(getattr(merger, "get_activity", None)))


def _get_detailed_activity(
    merger: StravaMerger,
    activity_id: int,
    activities_by_id: dict[int, dict[str, Any]],
    source_exists_cache: dict[int, bool],
) -> dict[str, Any] | None:
    activity = activities_by_id.get(activity_id)
    if activity is not None and "description" in activity:
        return activity
    get_activity = getattr(merger, "get_activity", None)
    if not callable(get_activity):
        return activity
    activity = get_activity(activity_id)
    source_exists_cache[activity_id] = activity is not None
    if activity is None:
        activities_by_id.pop(activity_id, None)
    else:
        activities_by_id[activity_id] = activity
    return activity


def _refresh_pending_source_activities(
    merger: StravaMerger,
    store: JobStore,
    activities_by_id: dict[int, dict[str, Any]],
    source_exists_cache: dict[int, bool],
) -> None:
    """Refresh pending sources that have fallen outside the recent-activity window."""
    get_activity = getattr(merger, "get_activity", None)
    if not callable(get_activity):
        return
    for job in store.jobs.values():
        if job["status"] not in {"awaiting_deletion", "ready", "uploaded"}:
            continue
        for source_id in job["source_ids"]:
            activity = activities_by_id.get(source_id)
            if activity is not None and "description" in activity:
                continue
            if not _has_read_capacity(merger, 1):
                logger.warning(
                    "Could not refresh all pending Strava activities without using the "
                    "reserved read quota."
                )
                return
            _get_detailed_activity(
                merger,
                source_id,
                activities_by_id,
                source_exists_cache,
            )


def _notify_outstanding_deletions(
    merger: StravaMerger,
    store: JobStore,
    recipient: str,
    *,
    activities_by_id: dict[int, dict[str, Any]],
    source_exists_cache: dict[int, bool],
) -> None:
    """Send the current outstanding-source list on every scheduled run."""
    job_ids = []
    existing_source_ids = set()
    for job_id, job in store.jobs.items():
        if job["status"] not in {"awaiting_deletion", "ready", "uploaded"}:
            continue
        if _job_opt_out(job, activities_by_id):
            continue
        job_source_ids = {
            source_id
            for source_id in job["source_ids"]
            if _source_exists(merger, source_id, source_exists_cache)
        }
        if job_source_ids:
            job_ids.append(job_id)
            existing_source_ids.update(job_source_ids)
    if job_ids:
        _notify_deletions(
            merger,
            store,
            recipient,
            job_ids,
            existing_source_ids=existing_source_ids,
        )


def _notify_deletions(
    merger: StravaMerger,
    store: JobStore,
    recipient: str,
    job_ids: list[str],
    *,
    existing_source_ids: set[int] | None = None,
) -> None:
    jobs = [store.jobs[job_id] for job_id in job_ids]
    geocoding_client = (
        GoogleGeocodingClient(merger.google_maps_api_key)
        if merger.google_maps_api_key
        else None
    )
    address_cache: dict[tuple[float, float], str] = {}
    backfilled = [
        _backfill_hole_details(job, geocoding_client, address_cache) for job in jobs
    ]
    if any(backfilled):
        store.save()
    delivered = merger.send_email(
        recipient,
        subject="StravaMerger - Delete source activities",
        body=_delete_mail_body(jobs, existing_source_ids=existing_source_ids),
    )
    if not delivered:
        return
    for job in jobs:
        job["delete_notified"] = True
        job["delete_notification_recipient"] = recipient
        job["last_delete_notification_at"] = _now()
        job["updated_at"] = _now()
    store.save()


def _upload_jobs(
    merger: StravaMerger,
    store: JobStore,
    job_ids: list[str],
    recipient: str,
    summary: AutomationSummary,
) -> None:
    gpxs = [_load_replacement(store.jobs[job_id]) for job_id in job_ids]
    for job_id, gpx in zip(job_ids, gpxs):
        job = store.jobs[job_id]
        if job["kind"] == "fix":
            source = Activity(**job["sources"][0])
            name = merger.fixed_activity_name(source, gpx)
            gpx.activity.name = name
            job["replacement"]["name"] = name
    results = merger.upload_activities_to_strava(gpxs)
    successful_job_ids = []
    for job_id, result in zip(job_ids, results):
        job = store.jobs[job_id]
        job["updated_at"] = _now()
        job["last_error"] = result.error
        if result.success:
            job["status"] = "uploaded"
            job["uploaded_activity_id"] = result.activity_id
            job["replacement"]["url"] = result.gpx.activity.url
            job["replacement"]["id"] = result.activity_id
            successful_job_ids.append(job_id)
            summary.uploaded_jobs += 1
        elif result.activity_id in job["source_ids"]:
            job["status"] = "awaiting_deletion"
            summary.deferred_jobs += 1
        elif result.activity_id is not None:
            job["status"] = "manual_review"
            summary.deferred_jobs += 1
            summary.review_messages.append(
                f"Job {job_id} is a duplicate of unexpected activity "
                f"{result.activity_id}; it will not be retried automatically."
            )
        else:
            job["status"] = "ready"
            summary.deferred_jobs += 1
    store.save()
    if successful_job_ids:
        _notify_confirmations(
            merger,
            store,
            recipient,
            successful_job_ids,
        )


def _notify_confirmations(
    merger: StravaMerger,
    store: JobStore,
    recipient: str,
    job_ids: list[str],
) -> None:
    jobs = [store.jobs[job_id] for job_id in job_ids]
    delivered = merger.send_email(
        recipient,
        subject="StravaMerger - New activities",
        body=_confirmation_mail_body(jobs),
    )
    if not delivered:
        return
    for job in jobs:
        job["confirmation_notification_recipient"] = recipient
        job["updated_at"] = _now()
    store.save()


def _fixed_activity(
    source: Activity,
    repaired_holes: int,
    *,
    name: str | None = None,
) -> Activity:
    activity = copy.deepcopy(source)
    activity.name = name or source.name
    activity.id = -1
    activity.url = None
    activity.filepath = None
    activity.source_ids = (source.id,)
    activity.external_id = f"stravamerger-fix-{source.id}-v1"
    original_description = source.description.strip()
    repair_note = (
        f"{BOT_MARKER} repaired {repaired_holes} GPS hole(s) in source activity "
        f"{source.id} at {_now()}."
    )
    activity.description = (
        f"{original_description}\n\n{repair_note}"
        if original_description
        else repair_note
    )
    return activity


def _load_replacement(job: dict[str, Any]) -> CustomGPX:
    filepath = job["replacement"]["filepath"]
    xml = _repair_legacy_extension_file(filepath)
    for source_id in job["source_ids"]:
        source_path = _source_backup_path(job, source_id)
        if source_path and os.path.isfile(source_path):
            _repair_legacy_extension_file(source_path)
    parsed = gpxpy.parse(xml)
    gpx = CustomGPX()
    gpx.creator = parsed.creator
    gpx.name = parsed.name
    gpx.description = parsed.description
    gpx.tracks = parsed.tracks
    gpx.routes = parsed.routes
    gpx.waypoints = parsed.waypoints
    gpx.set_activity(Activity(**job["replacement"]))
    return gpx


def _backfill_hole_details(
    job: dict[str, Any],
    client: GoogleGeocodingClient | None,
    cache: dict[tuple[float, float], str],
) -> bool:
    """Populate structured hole details for jobs created before they were persisted."""
    if "hole_details" in job:
        return False
    details_by_source = {}
    distances_by_source = {}
    for source in job["sources"]:
        source_path = _source_backup_path(job, source["id"])
        if not source_path or not os.path.isfile(source_path):
            details_by_source[str(source["id"])] = []
            distances_by_source[str(source["id"])] = []
            continue
        xml = _repair_legacy_extension_file(source_path)
        source_gpx = gpxpy.parse(xml)
        details = [
            _describe_hole(hole, client, cache) for hole in detect_holes(source_gpx)
        ]
        details_by_source[str(source["id"])] = details
        distances_by_source[str(source["id"])] = [
            detail["distance_meters"] for detail in details
        ]
    job["hole_details"] = details_by_source
    job.setdefault("hole_distances_meters", distances_by_source)
    return True


def _source_backup_path(job: dict[str, Any], source_id: int) -> str | None:
    replacement_path = job["replacement"]["filepath"]
    replacement_name = os.path.basename(replacement_path)
    if not replacement_name.endswith("_replacement.gpx"):
        return None
    prefix = replacement_name.removesuffix("_replacement.gpx")
    return os.path.join(
        os.path.dirname(replacement_path),
        f"{prefix}_source_{source_id}.gpx",
    )


def _repair_legacy_extension_file(filepath: str) -> str:
    """Repair GPX files written with the legacy gpxtpx namespace bug."""
    with open(filepath, "r", encoding="utf-8") as file:
        xml = file.read()
    legacy_prefix = f"{GPXTPX_NAMESPACE}:"
    if f"<{legacy_prefix}" not in xml and f"</{legacy_prefix}" not in xml:
        return xml

    repaired = xml.replace(f"<{legacy_prefix}", "<gpxtpx:")
    repaired = repaired.replace(f"</{legacy_prefix}", "</gpxtpx:")
    if "xmlns:gpxtpx=" not in repaired:
        repaired = repaired.replace(
            "<gpx ",
            f'<gpx xmlns:gpxtpx="{GPXTPX_NAMESPACE}" ',
            1,
        )
    gpxpy.parse(repaired)

    directory = os.path.dirname(os.path.abspath(filepath))
    descriptor, temporary_path = tempfile.mkstemp(
        dir=directory,
        prefix=".stravamerger-gpx-",
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as file:
            file.write(repaired)
        os.replace(temporary_path, filepath)
    except Exception:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)
        raise
    logger.warning("Repaired legacy GPX extension namespaces in {}", filepath)
    return repaired


def _delete_mail_body(
    jobs: list[dict[str, Any]],
    *,
    existing_source_ids: set[int] | None = None,
) -> str:
    body = [
        (
            "<html><body><p>These source activities are still awaiting action. Delete "
            "each activity in Strava to accept its prepared replacement, or add "
            "<code>nomerge</code> to its description to cancel the replacement. This "
            "current list is sent on every scheduled run while action remains.</p><ul>"
        )
    ]
    seen = set()
    for job in jobs:
        for source in job["sources"]:
            if (
                source["id"] in seen
                or (
                    existing_source_ids is not None
                    and source["id"] not in existing_source_ids
                )
            ):
                continue
            seen.add(source["id"])
            url = f"https://www.strava.com/activities/{source['id']}"
            details_by_source = job.get("hole_details", {})
            hole_details = details_by_source.get(str(source["id"]), [])
            distances_by_source = job.get("hole_distances_meters", {})
            distances = distances_by_source.get(str(source["id"]), [])
            if hole_details:
                label = "GPS hole" if len(hole_details) == 1 else "GPS holes"
                detail_text = "; ".join(
                    (
                        f"{_format_distance(detail['distance_meters'])} between "
                        f"{detail['origin_address']} and "
                        f"{detail['destination_address']}"
                    )
                    for detail in hole_details
                )
                details = f"{label}: {detail_text}"
            elif distances:
                label = "GPS hole" if len(distances) == 1 else "GPS holes"
                distance_text = ", ".join(
                    _format_distance(distance) for distance in distances
                )
                details = f"{label}: {distance_text} straight-line"
            elif job["kind"] == "fix":
                details = "GPS hole repair; distance unavailable"
            else:
                details = "no GPS hole; merge replacement"
            body.append(
                f"<li><a href='{url}'>{escape(source['name'])}</a> "
                f"({escape(details)})</li>"
            )
    body.append("</ul></body></html>")
    return "".join(body)


def _confirmation_mail_body(jobs: list[dict[str, Any]]) -> str:
    body = ["<html><body><p>These replacement activities are now on Strava:</p><ul>"]
    for job in jobs:
        activity = job["replacement"]
        body.append(
            f"<li><a href='{escape(activity['url'])}'>{escape(activity['name'])}</a></li>"
        )
    body.append("</ul></body></html>")
    return "".join(body)


def _review_mail_body(messages: list[str]) -> str:
    items = "".join(f"<li>{escape(message)}</li>" for message in messages)
    return f"<html><body><p>These tracks were left unchanged:</p><ul>{items}</ul></body></html>"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_distance(distance_meters: float) -> str:
    if distance_meters >= 1_000:
        return f"{distance_meters / 1_000:.2f} km"
    return f"{distance_meters:.0f} m"


def _describe_hole(
    hole: TrackHole,
    client: GoogleGeocodingClient | None,
    cache: dict[tuple[float, float], str],
) -> dict[str, Any]:
    return {
        "distance_meters": float(hole.distance_meters),
        "origin": list(hole.origin),
        "destination": list(hole.destination),
        "origin_address": _address_for(hole.origin, client, cache),
        "destination_address": _address_for(hole.destination, client, cache),
    }


def _address_for(
    location: tuple[float, float],
    client: GoogleGeocodingClient | None,
    cache: dict[tuple[float, float], str],
) -> str:
    coordinate = f"{location[0]:.6f}, {location[1]:.6f}"
    if client is None:
        return coordinate
    if location in cache:
        return cache[location]
    try:
        address = client.reverse_geocode(location)
    except GeocodingError as error:
        logger.warning("Could not reverse geocode {}: {}", coordinate, error)
        address = None
    cache[location] = address or coordinate
    return cache[location]
