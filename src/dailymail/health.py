"""Machine-readable, read-only DailyMail health for ControlPanel.

The contract derives only from DailyMail's durable SQLite history and installed
user timer. It never loads credentials, creates a config/database, contacts a
network service, or changes systemd state.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from . import db, systemd_units

SCHEMA_VERSION = "controlpanel.status.v1"
RECENT_RUN_LIMIT = 10

# Where the history in this document came from.
SOURCE_AUTO = "auto"
SOURCE_DB = "database"
SOURCE_SNAPSHOT = "snapshot"
SOURCE_UNAVAILABLE = "unavailable"


class _SkipDatabase(Exception):
    """Internal: `--source snapshot` must not open SQLite at all."""
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:access[_-]?token|api[_-]?key|authorization|credential|"
    r"password|secret|token)\b\s*([=:])\s*(?!bearer\b)[^\s,;]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\b(?:(authorization)\s*[:=]\s*)?(bearer)\s+[^\s,;]+")
_URI_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
_EMAIL_ADDRESS = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")
_ENABLED_TIMER_STATES = frozenset({"enabled", "enabled-runtime"})
_PAUSED_TIMER_STATES = frozenset({"disabled", "disabled-runtime", "indirect"})


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _safe_text(value: object, *, limit: int = 300) -> str | None:
    """Return an actionable, bounded error summary without secret material."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    text = _URI_USERINFO.sub(r"\1[redacted]@", text)

    def redact_bearer(match: re.Match[str]) -> str:
        prefix = "Authorization: " if match.group(1) else ""
        return f"{prefix}Bearer [redacted]"

    text = _BEARER_TOKEN.sub(redact_bearer, text)
    text = _SENSITIVE_ASSIGNMENT.sub("[redacted]", text)
    text = _EMAIL_ADDRESS.sub("[redacted-email]", text)
    return text[:limit]


def _parse_persisted_timestamp(value: object) -> datetime | None:
    """Parse a historical timestamp, treating legacy naive values as UTC.

    DailyMail's production writes are UTC-aware ISO timestamps. Older or
    manually recovered SQLite rows can be naive, though, so the read-only
    health contract treats them as UTC rather than allowing a mixed-aware
    subtraction to fail. Malformed values remain Unknown (`None`).
    """
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalized_timestamp(value: object) -> str | None:
    parsed = _parse_persisted_timestamp(value)
    return parsed.isoformat() if parsed else None


def _duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    started = _parse_persisted_timestamp(started_at)
    completed = _parse_persisted_timestamp(completed_at)
    if not started or not completed:
        return None
    return max(0.0, round((completed - started).total_seconds(), 3))


def _age_seconds(stamp: str | None) -> float | None:
    parsed = _parse_persisted_timestamp(stamp)
    if not parsed:
        return None
    return max(0.0, round((datetime.now(UTC) - parsed).total_seconds(), 3))


def _freshness_seconds(stamp: str | None) -> int | None:
    age = _age_seconds(stamp)
    return int(age) if age is not None else None


def _next_expected(value: object) -> str | None:
    """Convert systemd's local display timestamp to the contract's UTC ISO form."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        local = datetime.strptime(value.strip(), "%a %Y-%m-%d %H:%M:%S %Z").replace(
            tzinfo=ZoneInfo("America/New_York")
        )
    except ValueError:
        return None
    return local.astimezone(UTC).isoformat()


def _parse_parking_stats(value: object) -> dict[str, int] | None:
    if not value:
        return None
    try:
        raw = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    names = (
        "mentions_detected",
        "cache_hits",
        "cache_misses",
        "source_refreshes",
        "resolver_calls",
        "new_resolutions",
        "unresolved",
    )
    return {
        name: raw[name]
        for name in names
        if isinstance(raw.get(name), int) and not isinstance(raw[name], bool)
    }


def _run_metrics(row: sqlite3.Row) -> dict[str, int | float | str | None]:
    metrics: dict[str, int | float | str | None] = {
        "run_id": row["run_id"],
        "target_date": row["target_date"],
        "duration_seconds": _duration_seconds(row["started_at"], row["completed_at"]),
        "collector_validation": row["collector_validation"],
        "email_status": row["email_status"],
        "trigger": row["trigger"],
        "announcements_unique": row["unique_count"],
        "announcements_new": row["new_count"],
        "announcements_standing": row["standing_count"],
        "announcements_changed": row["changed_count"],
        "announcements_employee_view": row["employee_count"],
        "announcements_student_view": row["student_count"],
    }
    parking = _parse_parking_stats(row["parking_stats"])
    if parking:
        metrics.update({f"parking_{name}": value for name, value in parking.items()})
    return metrics


def _run_summary(row: sqlite3.Row) -> dict[str, Any]:
    status = row["status"]
    email_status = row["email_status"]
    error = _safe_text(row["error_summary"])
    if status == "running":
        success, summary = None, "DailyMail run is in progress"
    elif status == "success" and email_status in ("sent", "skipped_duplicate"):
        success = True
        summary = (
            "Digest sent"
            if email_status == "sent"
            else "Existing digest delivery confirmed"
        )
    elif status == "success" and email_status == "dry_run":
        success, summary = None, "Dry run completed; no digest was sent"
    else:
        success = False
        summary = "DailyMail run failed"
        if error:
            summary = f"{summary}: {error}"
    return {
        "started_at": _normalized_timestamp(row["started_at"]),
        "finished_at": _normalized_timestamp(row["completed_at"]),
        "success": success,
        "summary": summary,
        "metrics": _run_metrics(row),
    }


def _systemd_snapshot() -> dict[str, Any]:
    """Treat an unavailable user manager as unknown status, never a CLI crash."""
    try:
        return systemd_units.status()
    except (OSError, subprocess.SubprocessError):  # pragma: no cover - platform edge
        return {
            "service": systemd_units.SERVICE_NAME,
            "timer": systemd_units.TIMER_NAME,
            "service_load_state": "unknown",
            "timer_enabled": "unknown",
            "timer_active": "unknown",
            "next_elapse": None,
            "last_result": "unknown",
            "list_timers": "",
            "linger": None,
        }


def _timer_health(timer_enabled: object) -> str | None:
    state = str(timer_enabled or "unknown")
    if state in _PAUSED_TIMER_STATES:
        return "paused"
    if state not in _ENABLED_TIMER_STATES:
        return "unknown"
    return None


def _component(
    *,
    component_id: str,
    name: str,
    observed_at: str,
    timer: dict[str, Any],
    latest: Any,
    last_success: Any,
    db_error: str | None,
    delivery: bool,
    source: str = "DailyMail SQLite history and systemd user manager",
) -> dict[str, Any]:
    timer_health = _timer_health(timer.get("timer_enabled"))
    latest_status = latest["status"] if latest is not None else None
    email_status = latest["email_status"] if latest is not None else None
    if db_error:
        health, summary = "unknown", "DailyMail history is unavailable"
    elif latest is None:
        health, summary = "unknown", "No DailyMail run has been recorded"
    elif latest_status == "running":
        health, summary = "running", "DailyMail run is in progress"
    elif latest_status != "success":
        health, summary = "failed", "Latest DailyMail run failed"
    elif delivery and email_status == "dry_run":
        health, summary = "paused", "Latest run was a dry run; no digest was sent"
    elif delivery and email_status not in ("sent", "skipped_duplicate"):
        health, summary = "failed", "Latest run did not deliver a digest"
    elif timer_health:
        health = timer_health
        summary = (
            "Daily schedule is intentionally disabled"
            if health == "paused"
            else "Daily schedule state is unavailable"
        )
    else:
        health = "healthy"
        summary = (
            "Latest digest delivery succeeded"
            if delivery
            else "Latest retrieval succeeded"
        )
    latest_error = _safe_text(latest["error_summary"]) if latest is not None else None
    evidence = [
        {
            "label": "Unit",
            "value": str(timer.get("service") or systemd_units.SERVICE_NAME),
        },
        {
            "label": "Timer enabled",
            "value": str(timer.get("timer_enabled") or "unknown"),
        },
        {"label": "Timer active", "value": str(timer.get("timer_active") or "unknown")},
    ]
    if timer.get("next_elapse"):
        evidence.append(
            {"label": "Next scheduled", "value": str(timer["next_elapse"])[:120]}
        )
    if latest_error:
        evidence.append({"label": "Last error", "value": latest_error})
    return {
        "id": component_id,
        "name": name,
        "health": health,
        "summary": summary,
        "observed_at": observed_at,
        "source": source,
        "freshness_seconds": _freshness_seconds(latest["completed_at"])
        if latest
        else None,
        "last_attempt": _normalized_timestamp(latest["started_at"]) if latest else None,
        "last_success": (
            _normalized_timestamp(last_success["completed_at"])
            if last_success
            else None
        ),
        "next_expected": _next_expected(timer.get("next_elapse")),
        "actions": ["run", "enable", "disable", "refresh"],
        "evidence": evidence,
    }


def _problems(components: list[dict[str, Any]]) -> list[dict[str, str]]:
    return [
        {
            "component_id": component["id"],
            "severity": component["health"],
            "summary": component["summary"],
            "evidence": "; ".join(
                f"{entry['label']}: {entry['value']}" for entry in component["evidence"]
            )[:500],
        }
        for component in components
        if component["health"] in {"failed", "degraded", "unknown"}
    ]


def _overall(components: list[dict[str, Any]]) -> tuple[str, str]:
    ranking = {
        "failed": 6,
        "degraded": 5,
        "unknown": 4,
        "running": 3,
        "paused": 2,
        "healthy": 1,
    }
    chosen = max(components, key=lambda item: ranking[item["health"]])
    return chosen["health"], chosen["summary"]


def build_status(source: str = SOURCE_AUTO) -> dict[str, Any]:
    """Return the stable ``controlpanel.status.v1`` document without writes.

    ``source`` selects where the history comes from:

    ``auto``
        Read the live database; fall back to the published status snapshot when
        that read fails. This is what production uses.
    ``db``
        Database only, never the snapshot. Diagnosis, and the test that proves
        the failure being routed around is real.
    ``snapshot``
        Snapshot only, never opening SQLite at all. This is what proves the
        ControlPanel collector boundary works with no ``-shm`` present.

    Falling back is never silent. ``adapter_errors`` keeps the live database
    failure, ``status_data_source`` says ``snapshot``, and the snapshot's own
    timestamp and age travel with it, so a reader can tell "DailyMail's last
    committed state" from "the database was successfully probed".
    """
    if source not in (SOURCE_AUTO, SOURCE_DB, SOURCE_SNAPSHOT):
        raise ValueError(f"unknown status source {source!r}")

    observed_at = _now()
    timer = _systemd_snapshot()
    db_error: str | None = None
    snapshot_error: str | None = None
    staleness_notes: list[str] = []
    snapshot_meta: dict[str, Any] = {}
    data_source = SOURCE_DB
    stats: dict[str, int] = {}
    rows: list[Any] = []
    last_retrieval_success: Any = None
    last_delivery_success: Any = None
    parking: dict[str, Any] = {}
    database_size: int | None = None
    try:
        if source == SOURCE_SNAPSHOT:
            raise _SkipDatabase()
        connection = db.connect_readonly()
        try:
            schema = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if schema is None:
                raise RuntimeError("schema metadata is missing")
            stats = db.statistics(connection)
            query = (
                "SELECT run_id, target_date, started_at, completed_at, status, "
                "employee_count, student_count, unique_count, new_count, standing_count, "
                "changed_count, collector_validation, email_status, error_summary, trigger, "
                "parking_stats FROM runs "
            )
            rows = list(
                connection.execute(
                    query + "ORDER BY run_id DESC LIMIT ?", (RECENT_RUN_LIMIT,)
                )
            )
            last_retrieval_success = connection.execute(
                query + "WHERE status = 'success' ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            last_delivery_success = connection.execute(
                query
                + "WHERE status = 'success' AND email_status IN ('sent', 'skipped_duplicate') "
                "ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            source_summary = connection.execute(
                "SELECT MIN(last_verified_at) AS oldest_verified_at, "
                "MAX(last_verified_at) AS newest_verified_at, "
                "SUM(CASE WHEN last_status != 'ok' THEN 1 ELSE 0 END) AS failed_sources "
                "FROM parking_sources"
            ).fetchone()
            parking = {
                "oldest_verified_at": source_summary["oldest_verified_at"],
                "newest_verified_at": source_summary["newest_verified_at"],
                "cache_age_seconds": _age_seconds(source_summary["oldest_verified_at"]),
                "failed_sources": source_summary["failed_sources"] or 0,
                "latest_run": _parse_parking_stats(rows[0]["parking_stats"])
                if rows
                else None,
            }
        finally:
            connection.close()
    except _SkipDatabase:
        db_error = None
    except (FileNotFoundError, sqlite3.Error, RuntimeError, OSError) as exc:
        db_error = _safe_text(exc) or "Unable to read DailyMail history."

    if source != SOURCE_DB and (db_error is not None or source == SOURCE_SNAPSHOT):
        # The live database could not be read -- or was deliberately not tried.
        # Fall back to what DailyMail itself published at its last committed
        # transition. `db_error` is kept exactly as it was: the point is to
        # report the outage *and* still say what is true, not to hide one with
        # the other.
        (
            stats,
            rows,
            last_retrieval_success,
            last_delivery_success,
            parking,
            database_size,
            snapshot_meta,
            snapshot_error,
        ) = _load_from_snapshot(observed_at, timer)
        if snapshot_error is None:
            data_source = SOURCE_SNAPSHOT
        else:
            data_source = SOURCE_UNAVAILABLE
    elif db_error is not None:
        data_source = SOURCE_UNAVAILABLE

    latest = rows[0] if rows else None

    # The distinction this whole change rests on. `db_error` means "the live
    # database could not be probed" -- which was always true under the collector
    # boundary and is why the probe is being fixed. `history_error` means "and
    # therefore nothing is known", which is only true when the fallback did not
    # produce usable, current history.
    #
    # Reporting `unknown` while holding a snapshot that says the 06:30 run
    # delivered would be under-reporting exactly as badly as reporting healthy
    # from a stale one would be over-reporting. So: fresh snapshot, judge the
    # committed state; stale snapshot, say stale; no snapshot, say unknown.
    history_error = db_error
    component_source = "DailyMail SQLite history and systemd user manager"
    if data_source == SOURCE_SNAPSHOT:
        component_source = (
            "DailyMail status snapshot (published by DailyMail) and systemd "
            "user manager"
        )
        if snapshot_meta.get("status_snapshot_stale"):
            history_error = (
                "DailyMail status snapshot is stale: no run has published one "
                f"since {snapshot_meta.get('status_snapshot_expected_since')}"
            )
            stale_note = history_error
            if stale_note not in staleness_notes:
                staleness_notes.append(stale_note)
        else:
            history_error = None

    components = [
        _component(
            component_id="daily-retrieval",
            name="Rowan announcement retrieval",
            observed_at=observed_at,
            timer=timer,
            latest=latest,
            last_success=last_retrieval_success,
            db_error=history_error,
            delivery=False,
            source=component_source,
        ),
        _component(
            component_id="daily-digest",
            name="Curated digest delivery",
            observed_at=observed_at,
            timer=timer,
            latest=latest,
            last_success=last_delivery_success,
            db_error=history_error,
            delivery=True,
            source=component_source,
        ),
    ]
    health, summary = _overall(components)
    metrics: dict[str, int | float | str | None] = {
        f"database_{name}": value for name, value in stats.items()
    }
    metrics["database_size_bytes"] = (
        database_size if database_size is not None else _database_size()
    )
    metrics.update(
        {
            "parking_cache_age_seconds": parking.get("cache_age_seconds"),
            "parking_failed_sources": parking.get("failed_sources"),
            "parking_oldest_verification_at": parking.get("oldest_verified_at"),
            "parking_newest_verification_at": parking.get("newest_verified_at"),
        }
    )
    if last_retrieval_success is not None:
        metrics.update(
            {
                f"latest_success_{name}": value
                for name, value in _run_metrics(last_retrieval_success).items()
            }
        )
    latest_parking = parking.get("latest_run")
    if latest_parking:
        metrics.update(
            {
                f"latest_run_parking_{name}": value
                for name, value in latest_parking.items()
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "project": "dailymail",
        "display_name": "DailyMail",
        "observed_at": observed_at,
        "health": health,
        "summary": summary,
        "overall": {"health": health, "summary": summary},
        "components": components,
        "metrics": metrics,
        "recent_runs": [_run_summary(row) for row in rows],
        "problems": _problems(components),
        # Both failures are reported, separately and in full. A snapshot that
        # rescued the document does not erase the database error that made it
        # necessary, and a snapshot that failed too does not erase either.
        "adapter_errors": [
            message
            for message in (db_error, snapshot_error, *staleness_notes)
            if message
        ],
        # Provenance. Never omitted, so "which of these did I get?" is always
        # answerable without inference.
        "status_data_source": data_source,
        **snapshot_meta,
    }


def _expected_run_window_start(now: datetime) -> datetime | None:
    """The most recent moment DailyMail was scheduled to run, in Eastern time.

    Staleness is a schedule question, not an age question. A snapshot written at
    the end of the 06:30 run is the current truth all day; the same snapshot is
    stale the moment a 06:30 comes and goes without a newer one.
    """
    try:
        from . import settings as settings_module

        configured = settings_module.load()
        zone = ZoneInfo(configured.timezone)
        hour, _, minute = configured.daily_send_time.partition(":")
        send_hour, send_minute = int(hour), int(minute or 0)
    except Exception:  # noqa: BLE001 - staleness must never crash the probe
        return None

    local = now.astimezone(zone)
    today = local.replace(
        hour=send_hour, minute=send_minute, second=0, microsecond=0
    )
    return today if local >= today else today - timedelta(days=1)


def _snapshot_staleness(generated_at: str | None, observed_at: str) -> dict[str, Any]:
    """Age, and whether a scheduled run has since come and gone without one."""
    out: dict[str, Any] = {
        "status_snapshot_generated_at": _normalized_timestamp(generated_at),
        "status_snapshot_age_seconds": _age_seconds(generated_at),
        "status_snapshot_stale": None,
    }
    generated = _parse_persisted_timestamp(generated_at)
    if generated is None:
        return out
    now = _parse_persisted_timestamp(observed_at) or datetime.now(timezone.utc)
    window = _expected_run_window_start(now)
    if window is None:
        return out
    # Stale exactly when the last scheduled run started after this snapshot was
    # written -- i.e. a run should have produced a newer one and did not.
    out["status_snapshot_stale"] = generated < window
    out["status_snapshot_expected_since"] = _normalized_timestamp(window.isoformat())
    return out


def _load_from_snapshot(observed_at: str, timer: dict[str, Any]):
    """Rebuild the database-derived half of the document from the snapshot.

    Returns exactly the values `build_status` would have read from SQLite, so
    every downstream helper -- `_component`, `_run_summary`, `_run_metrics`,
    `_problems` -- runs unchanged over them. That is deliberate: it is what makes
    the snapshot-backed and database-backed documents agree field for field
    instead of approximately.
    """
    from . import status_snapshot

    empty = ({}, [], None, None, {}, None, {}, None)
    try:
        document = status_snapshot.read()
    except status_snapshot.SnapshotError as exc:
        # Distinct from the database error, and never turned into zeroes: a
        # document reporting no runs is indistinguishable from a DailyMail that
        # has never run, which is the one thing this must not say.
        return (*empty[:7], _safe_text(exc) or "Status snapshot is unavailable.")

    stats = {
        name: value
        for name, value in (document.get("statistics") or {}).items()
        if isinstance(value, (int, float)) or value is None
    }
    rows = [entry for entry in document.get("recent_runs") or [] if isinstance(entry, dict)]
    sources = document.get("parking_sources") or {}
    parking = {
        "oldest_verified_at": sources.get("oldest_verified_at"),
        "newest_verified_at": sources.get("newest_verified_at"),
        "cache_age_seconds": _age_seconds(sources.get("oldest_verified_at")),
        "failed_sources": sources.get("failed_sources") or 0,
        "latest_run": _parse_parking_stats(rows[0].get("parking_stats"))
        if rows
        else None,
    }
    meta: dict[str, Any] = {
        "status_snapshot_schema": document.get("schema_version"),
        "status_snapshot_path": status_snapshot.describe_path(),
    }
    meta.update(_snapshot_staleness(document.get("generated_at"), observed_at))
    delivery = document.get("latest_delivery") or {}
    if delivery:
        meta["status_latest_delivery_state"] = delivery.get("state")
        meta["status_latest_delivery_at"] = _normalized_timestamp(
            delivery.get("sent_at") or delivery.get("prepared_at")
        )
    confirmed = document.get("last_confirmed_delivery") or {}
    if confirmed:
        meta["status_last_confirmed_delivery_at"] = _normalized_timestamp(
            confirmed.get("sent_at") or confirmed.get("prepared_at")
        )
    return (
        stats,
        rows,
        document.get("last_retrieval_success"),
        document.get("last_delivery_success"),
        parking,
        document.get("database_size_bytes"),
        meta,
        None,
    )


def _database_size() -> int | None:
    try:
        return db.database_path().stat().st_size
    except OSError:
        return None
