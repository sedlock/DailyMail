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
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from . import db, systemd_units

SCHEMA_VERSION = "controlpanel.status.v1"
RECENT_RUN_LIMIT = 10
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


def _duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at)
        completed = datetime.fromisoformat(completed_at)
    except ValueError:
        return None
    return max(0.0, round((completed - started).total_seconds(), 3))


def _age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
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
        "started_at": row["started_at"],
        "finished_at": row["completed_at"],
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
    latest: sqlite3.Row | None,
    last_success: sqlite3.Row | None,
    db_error: str | None,
    delivery: bool,
) -> dict[str, Any]:
    source = "DailyMail SQLite history and systemd user manager"
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
        "last_attempt": latest["started_at"] if latest else None,
        "last_success": last_success["completed_at"] if last_success else None,
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


def build_status() -> dict[str, Any]:
    """Return the stable ``controlpanel.status.v1`` document without writes."""
    observed_at = _now()
    timer = _systemd_snapshot()
    db_error: str | None = None
    stats: dict[str, int] = {}
    rows: list[sqlite3.Row] = []
    last_retrieval_success: sqlite3.Row | None = None
    last_delivery_success: sqlite3.Row | None = None
    parking: dict[str, Any] = {}
    try:
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
    except (FileNotFoundError, sqlite3.Error, RuntimeError) as exc:
        db_error = _safe_text(exc) or "Unable to read DailyMail history."

    latest = rows[0] if rows else None
    components = [
        _component(
            component_id="daily-retrieval",
            name="Rowan announcement retrieval",
            observed_at=observed_at,
            timer=timer,
            latest=latest,
            last_success=last_retrieval_success,
            db_error=db_error,
            delivery=False,
        ),
        _component(
            component_id="daily-digest",
            name="Curated digest delivery",
            observed_at=observed_at,
            timer=timer,
            latest=latest,
            last_success=last_delivery_success,
            db_error=db_error,
            delivery=True,
        ),
    ]
    health, summary = _overall(components)
    metrics: dict[str, int | float | str | None] = {
        f"database_{name}": value for name, value in stats.items()
    }
    metrics["database_size_bytes"] = _database_size()
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
        "adapter_errors": [db_error] if db_error else [],
    }


def _database_size() -> int | None:
    try:
        return db.database_path().stat().st_size
    except OSError:
        return None
