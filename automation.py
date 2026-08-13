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

from app import BOT_MARKER, GPXTPX_NAMESPACE, StravaMerger, UploadResult
from gpxfixer import (
    MAX_HOLE_DISTANCE,
    GoogleRoutesClient,
    RouteError,
    detect_holes,
    repair_holes,
    travel_mode_for_sport,
    validate_route,
)
from utils import Activity, CustomGPX

MAX_HOLES_PER_ACTIVITY = 5


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
            if job["status"] != "complete":
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
    ) -> dict[str, Any]:
        replacement_data = asdict(replacement.activity)
        replacement_data["filepath"] = os.path.abspath(replacement.activity.filepath)
        job = {
            "id": job_id,
            "kind": kind,
            "source_ids": [source.id for source in source_activities],
            "sources": [asdict(source) for source in source_activities],
            "replacement": replacement_data,
            "status": "ready",
            "delete_notified": False,
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
    fix_holes: bool = True,
    hole_time_threshold: float = 5.0,
    hole_distance_threshold: float = 400.0,
) -> AutomationSummary:
    """Resume queued jobs, discover new replacements, and upload what is ready."""
    if hole_time_threshold < 0 or hole_distance_threshold < 0:
        raise ValueError("Hole detection thresholds cannot be negative.")
    store = JobStore(state_path)
    summary = AutomationSummary()
    _resume_jobs(merger, store, recipient=recipient, summary=summary)

    claimed = store.claimed_source_ids()
    available = [activity for activity in activities if activity["id"] not in claimed]
    merge_chains = merger.detect_merging_activities(available)
    merged_source_ids = {activity.id for chain in merge_chains for activity in chain}
    google_client = None
    gpx_cache: dict[int, CustomGPX] = {}

    def fetch(activity: Activity) -> CustomGPX:
        if activity.id not in gpx_cache:
            gpx_cache[activity.id] = merger.activity_to_gpx(activity)
        return gpx_cache[activity.id]

    def repair(gpx: CustomGPX) -> tuple[CustomGPX | None, int]:
        nonlocal google_client
        activity = gpx.activity
        if not fix_holes or "nofix" in activity.description.lower():
            return gpx, 0
        if store.review_reason(activity.id):
            return None, 0
        holes = detect_holes(
            gpx,
            time_threshold=hole_time_threshold,
            distance_threshold=hole_distance_threshold,
        )
        if not holes:
            store.mark_checked_clean(activity.id)
            return gpx, 0
        if len(holes) > MAX_HOLES_PER_ACTIVITY:
            message = (
                f"Activity {activity.id} ({activity.name}) has {len(holes)} holes, "
                f"exceeding the automatic limit of {MAX_HOLES_PER_ACTIVITY}."
            )
            summary.review_messages.append(message)
            store.mark_for_review(activity.id, message)
            return None, 0

        mode = travel_mode_for_sport(activity.sport)
        if mode is None:
            message = (
                f"Activity {activity.id} ({activity.name}) has {len(holes)} hole(s), "
                f"but sport type {activity.sport!r} has no safe automatic Google route mode."
            )
            summary.review_messages.append(message)
            store.mark_for_review(activity.id, message)
            return None, 0
        if not merger.google_maps_api_key:
            summary.review_messages.append(
                f"Activity {activity.id} ({activity.name}) has {len(holes)} hole(s), "
                "but no Google Maps API key is configured."
            )
            return None, 0
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
            return None, 0
        return repair_holes(gpx, repairs), len(repairs)

    new_job_ids = []
    for chain in merge_chains:
        if any(
            store.review_reason(activity.id)
            and "nofix" not in activity.description.lower()
            for activity in chain
        ):
            continue
        original_gpxs = [fetch(activity) for activity in chain]
        replacement_inputs = []
        repaired_count = 0
        for original in original_gpxs:
            repaired, count = repair(original)
            if repaired is None:
                replacement_inputs = []
                break
            replacement_inputs.append(repaired)
            repaired_count += count
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
        )
        new_job_ids.append(job_id)
        summary.merged_jobs += 1
        summary.repaired_holes += repaired_count

    for api_activity in available:
        if (
            api_activity["id"] in merged_source_ids
            or not merger.can_fix_activity(api_activity)
            or store.was_checked_clean(api_activity["id"])
            or store.review_reason(api_activity["id"])
        ):
            continue
        source = merger.activity_from_api(api_activity)
        original = fetch(source)
        repaired, repaired_count = repair(original)
        if repaired is None or repaired_count == 0:
            continue

        replacement = repaired
        replacement.activity = _fixed_activity(source, repaired_count)
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
        )
        new_job_ids.append(job_id)
        summary.repaired_jobs += 1
        summary.repaired_holes += repaired_count

    if new_job_ids:
        _notify_deletions(merger, store, recipient, new_job_ids)
        _upload_jobs(merger, store, new_job_ids, recipient, summary)

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
) -> None:
    ready = []
    for job_id, job in store.jobs.items():
        if job["status"] == "uploaded":
            if not any(
                merger.activity_exists(source_id) for source_id in job["source_ids"]
            ):
                job["status"] = "complete"
                job["updated_at"] = _now()
            continue
        if job["status"] == "awaiting_deletion":
            if any(
                merger.activity_exists(source_id) for source_id in job["source_ids"]
            ):
                summary.deferred_jobs += 1
                continue
            job["status"] = "ready"
            job["updated_at"] = _now()
        if job["status"] == "ready":
            ready.append(job_id)
    store.save()

    pending_notifications = [
        job_id for job_id in ready if not store.jobs[job_id]["delete_notified"]
    ]
    if pending_notifications:
        _notify_deletions(merger, store, recipient, pending_notifications)
    if ready:
        _upload_jobs(merger, store, ready, recipient, summary)


def _notify_deletions(
    merger: StravaMerger,
    store: JobStore,
    recipient: str,
    job_ids: list[str],
) -> None:
    jobs = [store.jobs[job_id] for job_id in job_ids]
    merger.send_email(
        recipient,
        subject="StravaMerger - Delete source activities",
        body=_delete_mail_body(jobs),
    )
    for job in jobs:
        job["delete_notified"] = True
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
    results = merger.upload_activities_to_strava(gpxs)
    successful = []
    for job_id, result in zip(job_ids, results):
        job = store.jobs[job_id]
        job["updated_at"] = _now()
        job["last_error"] = result.error
        if result.success:
            job["status"] = "uploaded"
            job["uploaded_activity_id"] = result.activity_id
            job["replacement"]["url"] = result.gpx.activity.url
            job["replacement"]["id"] = result.activity_id
            successful.append(result)
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
    if successful:
        merger.send_email(
            recipient,
            subject="StravaMerger - New activities",
            body=_confirmation_mail_body(successful),
        )


def _fixed_activity(source: Activity, repaired_holes: int) -> Activity:
    activity = copy.deepcopy(source)
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
    replacement_name = os.path.basename(filepath)
    if replacement_name.endswith("_replacement.gpx"):
        prefix = replacement_name.removesuffix("_replacement.gpx")
        for source_id in job["source_ids"]:
            source_path = os.path.join(
                os.path.dirname(filepath),
                f"{prefix}_source_{source_id}.gpx",
            )
            if os.path.isfile(source_path):
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


def _delete_mail_body(jobs: list[dict[str, Any]]) -> str:
    body = [
        (
            "<html><body><p>Replacement tracks have been prepared. Delete these source "
            "activities in Strava. If an upload is rejected as a duplicate, the stored "
            "replacement will be retried by the next scheduled run after deletion.</p><ul>"
        )
    ]
    seen = set()
    for job in jobs:
        for source in job["sources"]:
            if source["id"] in seen:
                continue
            seen.add(source["id"])
            url = f"https://www.strava.com/activities/{source['id']}"
            body.append(
                f"<li><a href='{url}'>{escape(source['name'])}</a> "
                f"({escape(job['kind'])})</li>"
            )
    body.append("</ul></body></html>")
    return "".join(body)


def _confirmation_mail_body(results: list[UploadResult]) -> str:
    body = ["<html><body><p>These replacement activities are now on Strava:</p><ul>"]
    for result in results:
        activity = result.gpx.activity
        body.append(
            f"<li><a href='{escape(activity.url)}'>{escape(activity.name)}</a></li>"
        )
    body.append("</ul></body></html>")
    return "".join(body)


def _review_mail_body(messages: list[str]) -> str:
    items = "".join(f"<li>{escape(message)}</li>" for message in messages)
    return f"<html><body><p>These tracks were left unchanged:</p><ul>{items}</ul></body></html>"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
