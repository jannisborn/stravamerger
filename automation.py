"""Orchestrate merge and hole-repair replacement jobs."""

from __future__ import annotations

import copy
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html import escape
from typing import Any

import gpxpy
from loguru import logger

from app import BOT_MARKER, StravaMerger
from gpxfixer import (
    MAX_HOLE_DISTANCE,
    GeocodingError,
    GoogleGeocodingClient,
    GoogleRoutesClient,
    NoRouteError,
    Route,
    RouteError,
    RouteTooIndirectError,
    TrackHole,
    detect_holes,
    repair_holes,
    straight_line_route,
    travel_mode_for_sport,
    validate_route,
)
from utils import Activity, CustomGPX

MAX_HOLES_PER_ACTIVITY = 5
READ_REQUEST_RESERVE = 10
ACTIVITY_ID_PATTERN = re.compile(r"\b([Aa]ctivity) (\d+)\b")


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

    def clear_review(self, activity_id: int) -> None:
        if self.data["reviews"].pop(str(activity_id), None) is not None:
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
        replacement_data["filepath"] = (
            os.path.abspath(replacement.activity.filepath)
            if replacement.activity.filepath
            else None
        )
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
            "replacement_gpx": replacement.to_xml(),
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

    rebuild_source_ids = {
        source_id
        for job in store.jobs.values()
        if job.get("rebuild_required")
        for source_id in job["source_ids"]
    }
    claimed = store.claimed_source_ids()
    available = [activity for activity in activities if activity["id"] not in claimed]
    available_ids = {activity["id"] for activity in available}
    available.extend(
        activities_by_id[source_id]
        for source_id in rebuild_source_ids
        if source_id not in available_ids and source_id in activities_by_id
    )
    available.sort(key=lambda activity: activity["id"] not in rebuild_source_ids)
    merge_chains = merger.detect_merging_activities(available)
    merge_chains.sort(
        key=lambda chain: not any(
            activity.id in rebuild_source_ids for activity in chain
        )
    )
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
        previous_review = store.review_reason(activity.id)
        if previous_review:
            if not _review_can_use_straight_line(previous_review):
                return None, []
            logger.info(
                'Retrying "{}" because straight-line fallback is now available.',
                activity.name,
            )
            store.clear_review(activity.id)
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
            for hole, detail in zip(holes, hole_details):
                route, fallback_reason = _route_with_fallback(
                    google_client, hole, mode
                )
                if fallback_reason:
                    detail["repair_method"] = "straight_line"
                    detail["fallback_reason"] = fallback_reason
                    logger.warning(
                        'Using straight-line GPX coordinates in "{}" for {} between '
                        "{} and {} because {}",
                        activity.name,
                        _format_distance(detail["distance_meters"]),
                        detail["origin_address"],
                        detail["destination_address"],
                        fallback_reason,
                    )
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
            _review_blocks_retry(store, activity.id)
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
            or _review_blocks_retry(store, api_activity["id"])
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
        if opt_out and (
            job["status"] in {"awaiting_deletion", "ready"}
            or job.get("rebuild_required")
        ):
            activity_name, marker = opt_out
            job["status"] = "cancelled"
            job.pop("rebuild_required", None)
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
        if (
            job["status"] in {"awaiting_deletion", "ready"}
            and not job.get("replacement_gpx")
        ):
            source_exists = any(
                _source_exists(merger, source_id, source_exists_cache)
                for source_id in job["source_ids"]
            )
            source_names = " & ".join(
                source.get("name", "Unnamed activity") for source in job["sources"]
            )
            job["updated_at"] = _now()
            if source_exists:
                job["status"] = "cancelled"
                job["rebuild_required"] = True
                job["last_error"] = (
                    "Queued replacement had no embedded data and will be rebuilt."
                )
                logger.warning(
                    'Rebuilding queued replacement for "{}" because its embedded '
                    "data is missing.",
                    source_names,
                )
            else:
                job["status"] = "manual_review"
                job["last_error"] = (
                    "Replacement data is missing and no source activity remains."
                )
                summary.review_messages.append(
                    f"{source_names}: replacement data is missing and the source "
                    "activity no longer exists."
                )
            continue
        if job["status"] == "cancelled":
            continue
        if job["status"] == "manual_review":
            if job.get("upload_recovery_checked"):
                continue
            duplicate_id = StravaMerger.duplicate_activity_id(job.get("last_error"))
            if (
                duplicate_id is None
                or duplicate_id in job["source_ids"]
                or not _is_uploaded_replacement(merger, job, duplicate_id)
            ):
                job["upload_recovery_checked"] = True
                continue
            _mark_job_uploaded(job, duplicate_id)
            logger.info(
                'Recovered an already-uploaded replacement for "{}".',
                job["replacement"]["name"],
            )
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
        if (
            job["status"] not in {"awaiting_deletion", "ready", "uploaded"}
            and not job.get("rebuild_required")
        ):
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
    successful_job_ids = []
    try:
        for job_id, gpx in zip(job_ids, gpxs):
            results = merger.upload_activities_to_strava([gpx])
            if not results:
                raise RuntimeError(
                    f"Strava returned no upload result for queued job {job_id}."
                )
            result = results[0]
            job = store.jobs[job_id]
            job["updated_at"] = _now()
            job["last_error"] = result.error
            if result.success:
                _mark_job_uploaded(
                    job,
                    result.activity_id,
                    url=result.gpx.activity.url,
                )
                successful_job_ids.append(job_id)
                summary.uploaded_jobs += 1
            elif result.activity_id in job["source_ids"]:
                job["status"] = "awaiting_deletion"
                summary.deferred_jobs += 1
            elif result.activity_id is not None:
                if _is_uploaded_replacement(merger, job, result.activity_id):
                    _mark_job_uploaded(job, result.activity_id)
                    successful_job_ids.append(job_id)
                    summary.uploaded_jobs += 1
                    logger.info(
                        'Recovered an already-uploaded replacement for "{}".',
                        job["replacement"]["name"],
                    )
                else:
                    job["status"] = "manual_review"
                    job["upload_recovery_checked"] = True
                    summary.deferred_jobs += 1
                    summary.review_messages.append(
                        f"Job {job_id} is a duplicate of unexpected activity "
                        f"{result.activity_id}; it will not be retried automatically."
                    )
            else:
                job["status"] = "ready"
                summary.deferred_jobs += 1
            # Persist every outcome before starting another upload. A later rate-limit
            # failure must not lose successful Strava activity IDs.
            store.save()
    finally:
        if successful_job_ids:
            _notify_confirmations(
                merger,
                store,
                recipient,
                successful_job_ids,
            )


def _mark_job_uploaded(
    job: dict[str, Any],
    activity_id: int,
    *,
    url: str | None = None,
) -> None:
    job["status"] = "uploaded"
    job["uploaded_activity_id"] = activity_id
    job["replacement"]["id"] = activity_id
    job["replacement"]["url"] = url or (
        f"https://www.strava.com/activities/{activity_id}"
    )
    job["last_error"] = None
    job["updated_at"] = _now()


def _is_uploaded_replacement(
    merger: StravaMerger,
    job: dict[str, Any],
    activity_id: int,
) -> bool:
    get_activity = getattr(merger, "get_activity", None)
    if not callable(get_activity):
        return False
    activity = get_activity(activity_id)
    if not activity:
        return False
    expected_external_id = job["replacement"].get("external_id")
    if expected_external_id and activity.get("external_id") == expected_external_id:
        return True
    expected_description = job["replacement"].get("description")
    return bool(
        expected_description
        and BOT_MARKER in expected_description
        and activity.get("description") == expected_description
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
    xml = job.get("replacement_gpx")
    if not xml:
        raise ValueError("Queued job has no embedded replacement GPX data.")
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


def _route_with_fallback(
    client: GoogleRoutesClient,
    hole: TrackHole,
    travel_mode: str,
) -> tuple[Route, str | None]:
    try:
        route = client.route(hole.origin, hole.destination, travel_mode)
        validate_route(hole, route)
    except (NoRouteError, RouteTooIndirectError) as error:
        return straight_line_route(hole), str(error)
    return route, None


def _review_can_use_straight_line(reason: str) -> bool:
    return any(
        marker in reason
        for marker in (
            "Google Routes returned no route",
            "route is more than 4x",
            "route contains no points between the gap endpoints",
        )
    )


def _review_blocks_retry(store: JobStore, activity_id: int) -> bool:
    reason = store.review_reason(activity_id)
    return bool(reason and not _review_can_use_straight_line(reason))


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
                    _format_hole_detail(detail) for detail in hole_details
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
                f"<li>{escape(source['name'])} &mdash; "
                f"<a href='{url}'>Strava activity {source['id']}</a> "
                f"({escape(details)})</li>"
            )
    body.append("</ul></body></html>")
    return "".join(body)


def _confirmation_mail_body(jobs: list[dict[str, Any]]) -> str:
    body = ["<html><body><p>These replacement activities are now on Strava:</p><ul>"]
    for job in jobs:
        activity = job["replacement"]
        body.append(
            f"<li>{escape(activity['name'])} &mdash; "
            f"<a href='{escape(activity['url'])}'>Strava activity "
            f"{activity['id']}</a></li>"
        )
    body.append("</ul></body></html>")
    return "".join(body)


def _review_mail_body(messages: list[str]) -> str:
    items = "".join(f"<li>{_link_activity_ids(message)}</li>" for message in messages)
    return f"<html><body><p>These tracks were left unchanged:</p><ul>{items}</ul></body></html>"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _format_distance(distance_meters: float) -> str:
    if distance_meters >= 1_000:
        return f"{distance_meters / 1_000:.2f} km"
    return f"{distance_meters:.0f} m"


def _format_hole_detail(detail: dict[str, Any]) -> str:
    text = (
        f"{_format_distance(detail['distance_meters'])} between "
        f"{detail['origin_address']} and {detail['destination_address']}"
    )
    if detail.get("repair_method") == "straight_line":
        text += ". Straight-line GPX coordinates were used"
        if detail.get("fallback_reason"):
            text += f" because {detail['fallback_reason']}"
    return text


def _link_activity_ids(message: str) -> str:
    escaped_message = escape(message)

    def replace(match: re.Match[str]) -> str:
        label, activity_id = match.groups()
        url = f"https://www.strava.com/activities/{activity_id}"
        return f"{label} <a href='{url}'>{activity_id}</a>"

    return ACTIVITY_ID_PATTERN.sub(replace, escaped_message)


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
