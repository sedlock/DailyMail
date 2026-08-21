"""Persist Phase 1 collection artifacts into SQLite.

Idempotent by construction: re-ingesting the same artifact produces no new
versions, no duplicate observations and no duplicate daily records. That makes
`db-init` safe to rerun and lets a same-day re-run recover without side effects.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from . import config as collector_config
from . import db
from .errors import ValidationError
from .normalize import FORBIDDEN_KEYS
from .settings import Settings

# Source-view names stored in daily_presence, mapped from the collector's
# request-audience names.
VIEW_FROM_QUERY = {"Employees": "Employee", "Students": "Student"}

REQUIRED_ARTIFACT_KEYS = (
    "schema_version",
    "target_date",
    "counts",
    "source_counts",
    "category_registry",
    "announcements",
    "validation",
)

REQUIRED_RECORD_KEYS = (
    "submission_id",
    "title",
    "source_audience",
    "status",
    "full_body",
    "distribution_dates",
    "first_distribution_date",
    "source_query_membership",
)


def validate_artifact(artifact: object, *, origin: str = "artifact") -> dict:
    """Reject anything malformed or unvalidated before it can reach the database."""
    if not isinstance(artifact, dict):
        raise ValidationError(f"{origin}: not a JSON object")

    missing = [key for key in REQUIRED_ARTIFACT_KEYS if key not in artifact]
    if missing:
        raise ValidationError(f"{origin}: missing keys {missing}")

    version = artifact.get("schema_version")
    if version != 1:
        raise ValidationError(
            f"{origin}: unsupported collection schema_version {version!r}"
        )

    target_date = artifact.get("target_date")
    if not isinstance(target_date, str) or len(target_date) != 10:
        raise ValidationError(f"{origin}: bad target_date {target_date!r}")

    if not isinstance(artifact.get("announcements"), list):
        raise ValidationError(f"{origin}: announcements is not a list")
    if not artifact.get("category_registry"):
        raise ValidationError(
            f"{origin}: empty category registry -- the artifact is not a validated "
            "collection (a genuine zero-announcement day still carries the registry)"
        )

    counts = artifact.get("counts") or {}
    announcements = artifact["announcements"]
    if counts.get("unique") != len(announcements):
        raise ValidationError(
            f"{origin}: counts.unique {counts.get('unique')} != "
            f"{len(announcements)} announcements present"
        )

    seen: set[str] = set()
    for record in announcements:
        if not isinstance(record, dict):
            raise ValidationError(f"{origin}: an announcement is not an object")
        absent = [key for key in REQUIRED_RECORD_KEYS if key not in record]
        if absent:
            raise ValidationError(
                f"{origin}: announcement missing {absent} "
                f"(submission_id={record.get('submission_id')!r})"
            )
        submission_id = str(record["submission_id"])
        if not submission_id.isdigit() or int(submission_id) <= 0:
            raise ValidationError(f"{origin}: bad submission_id {submission_id!r}")
        if submission_id in seen:
            raise ValidationError(f"{origin}: duplicate submission_id {submission_id}")
        seen.add(submission_id)
        if record["source_audience"] not in collector_config.SOURCE_AUDIENCES:
            raise ValidationError(
                f"{origin}: bad source_audience {record['source_audience']!r}"
            )
        if record["status"] not in ("New", "Standing"):
            raise ValidationError(f"{origin}: bad status {record['status']!r}")
        if target_date not in (record.get("distribution_dates") or []):
            raise ValidationError(
                f"{origin}: submission {submission_id} does not list {target_date}"
            )
        for view in record.get("source_query_membership") or []:
            if view not in VIEW_FROM_QUERY:
                raise ValidationError(f"{origin}: unknown source view {view!r}")

    _assert_no_forbidden(artifact, origin)
    return artifact


def _assert_no_forbidden(obj: object, origin: str, path: str = "$") -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in FORBIDDEN_KEYS:
                raise ValidationError(
                    f"{origin}: forbidden field {key!r} at {path}; refusing to persist"
                )
            _assert_no_forbidden(value, origin, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            _assert_no_forbidden(value, origin, f"{path}[{index}]")


def ingest_artifact(
    connection: sqlite3.Connection,
    artifact: dict,
    settings: Settings,
    *,
    observed_at: str | None = None,
    origin: str = "artifact",
) -> dict:
    """Persist one validated collection. Returns a summary of what happened."""
    validate_artifact(artifact, origin=origin)
    target_date = artifact["target_date"]
    stamp = observed_at or artifact.get("generated_at_utc") or db.now_utc()
    priority_map = settings.category_priority_map()

    changed_ids: list[int] = []
    new_versions = 0

    with db.transaction(connection):
        for entry in artifact["category_registry"]:
            db.upsert_category(
                connection,
                category_id=int(entry["id"]),
                title=entry.get("title") or f"Category {entry['id']}",
                rowan_rank=entry.get("rank"),
                color=entry.get("color"),
                is_active=entry.get("is_active"),
                manual_priority=priority_map.get(entry.get("title") or ""),
                observed_at=stamp,
            )

        before = connection.execute(
            "SELECT COUNT(*) FROM announcement_versions"
        ).fetchone()[0]

        for record in artifact["announcements"]:
            submission_id = int(record["submission_id"])
            version_id, changed = db.record_announcement(
                connection, record, observed_at=stamp
            )
            if changed:
                changed_ids.append(submission_id)
            db.record_presence(
                connection,
                target_date=target_date,
                submission_id=submission_id,
                source_views=[
                    VIEW_FROM_QUERY[view]
                    for view in record.get("source_query_membership") or []
                ],
                observed_at=stamp,
            )
            db.record_daily(
                connection,
                target_date=target_date,
                submission_id=submission_id,
                version_id=version_id,
                status=record["status"],
                changed=changed,
                observed_at=stamp,
            )

        after = connection.execute(
            "SELECT COUNT(*) FROM announcement_versions"
        ).fetchone()[0]
        new_versions = after - before

    return {
        "target_date": target_date,
        "announcements": len(artifact["announcements"]),
        "categories": len(artifact["category_registry"]),
        "new_versions": new_versions,
        "changed_ids": sorted(changed_ids),
        "changed_count": len(changed_ids),
    }


def import_existing_collections(
    connection: sqlite3.Connection,
    settings: Settings,
    *,
    directory: Path | None = None,
) -> dict:
    """Seed history from Phase 1 artifacts already on disk.

    Chronological so version history builds in the right order. Malformed or
    unvalidated files are skipped with a reason rather than poisoning the
    database, and the whole operation is safe to rerun.
    """
    source = directory or collector_config.collections_dir()
    imported: list[dict] = []
    skipped: list[dict] = []

    if not source.is_dir():
        return {"imported": imported, "skipped": skipped, "source": str(source)}

    for path in sorted(source.glob("*.json")):
        try:
            artifact = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            skipped.append({"file": path.name, "reason": f"unreadable: {exc.__class__.__name__}"})
            continue
        try:
            summary = ingest_artifact(
                connection, artifact, settings, origin=path.name
            )
        except ValidationError as exc:
            skipped.append({"file": path.name, "reason": str(exc)})
            continue
        imported.append(summary)

    return {"imported": imported, "skipped": skipped, "source": str(source)}
