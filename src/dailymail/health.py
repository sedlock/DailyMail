"""Machine-readable, read-only DailyMail health for ControlPanel.

The contract deliberately derives only from DailyMail's durable SQLite history
and the installed user timer.  It never loads credentials, creates a config or
database, contacts Rowan/Gmail/Claude, or changes systemd state.
"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
from datetime import datetime, timezone
from typing import Any

from . import db, systemd_units

SCHEMA_VERSION = "controlpanel.status.v1"
RECENT_RUN_LIMIT = 10
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|token|secret|credential|authorization)\s*([=:])\s*\S+"
)
_EMAIL_ADDRESS = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_text(value: object, *, limit: int = 300) -> str | None:
    """Keep an actionable error summary without disclosing secret material."""
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    text = _SENSITIVE_ASSIGNMENT.sub(r"\1\2 [redacted]", text)
    text = _EMAIL_ADDRESS.sub("[redacted-email]", text)
    return text[:limit]


def _duration_seconds(started_at: str | None, completed_at: str | None) -> float | None:
    if not started_at or not completed_at:
        return None
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        completed = datetime.fromisoformat(completed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, round((completed - started).total_seconds(), 3))


def _age_seconds(stamp: str | None) -> float | None:
    if not stamp:
        return None
    try:
        parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, round((datetime.now(timezone.utc) - parsed).total_seconds(), 3))


def _row_run(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "run_id": row["run_id"],
        "target_date": row["target_date"],
        "started_at": row["started_at"],
        "completed_at": row["completed_at"],
        "duration_seconds": _duration_seconds(row["started_at"], row["completed_at"]),
        "status": row["status"],
        "trigger": row["trigger"],
        "collector_validation": row["collector_validation"],
        "email_status": row["email_status"],
        "announcements": {
            "unique": row["unique_count"],
            "new": row["new_count"],
            "standing": row["standing_count"],
            "changed": row["changed_count"],
            "employee_view": row["employee_count"],
            "student_view": row["student_count"],
        },
        "error": _safe_text(row["error_summary"]),
    }


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
        "mentions_detected", "cache_hits", "cache_misses", "source_refreshes",
        "resolver_calls", "new_resolutions", "unresolved",
    )
    return {
        name: raw[name]
        for name in names
        if isinstance(raw.get(name), int) and not isinstance(raw[name], bool)
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


def _problems(
    runs: list[dict[str, Any]], timer: dict[str, Any], db_error: str | None
) -> list[dict[str, str]]:
    problems: list[dict[str, str]] = []
    if db_error:
        problems.append({
            "component_id": "daily-retrieval",
            "severity": "unknown",
            "summary": "DailyMail history is unavailable",
            "evidence": db_error,
        })
        return problems
    latest = runs[0] if runs else None
    if latest is None:
        problems.append({
            "component_id": "daily-retrieval",
            "severity": "unknown",
            "summary": "No DailyMail runs have been recorded yet",
            "evidence": "The SQLite history contains no production run.",
        })
    elif latest["status"] != "success":
        problems.append({
            "component_id": "daily-retrieval",
            "severity": "failed",
            "summary": "The latest DailyMail run failed",
            "evidence": latest["error"] or "See the DailyMail service journal.",
        })
    elif latest["email_status"] == "failed":
        problems.append({
            "component_id": "daily-digest",
            "severity": "failed",
            "summary": "The latest DailyMail digest was not delivered",
            "evidence": latest["error"] or "The SMTP delivery result was failed.",
        })
    if timer.get("timer_enabled") not in ("enabled", "enabled-runtime"):
        problems.append({
            "component_id": "daily-digest",
            "severity": "degraded",
            "summary": "The DailyMail timer is not enabled",
            "evidence": f"timer enabled state: {timer.get('timer_enabled') or 'unknown'}",
        })
    return problems


def build_status() -> dict[str, Any]:
    """Return the stable ``controlpanel.status.v1`` document.

    The function has no network or write path.  Database and systemd failures
    become explicit Unknown observations so an integration keeps the last
    useful ControlPanel observation rather than losing the project entirely.
    """
    observed_at = _now()
    timer = _systemd_snapshot()
    db_error: str | None = None
    stats: dict[str, int] = {}
    runs: list[dict[str, Any]] = []
    parking: dict[str, Any] = {}
    last_retrieval_success: dict[str, Any] | None = None
    last_delivery_success: dict[str, Any] | None = None
    try:
        connection = db.connect_readonly()
        try:
            schema = connection.execute(
                "SELECT value FROM schema_meta WHERE key = 'schema_version'"
            ).fetchone()
            if schema is None:
                raise RuntimeError("schema metadata is missing")
            stats = db.statistics(connection)
            rows = list(connection.execute(
                "SELECT run_id, target_date, started_at, completed_at, status, "
                "employee_count, student_count, unique_count, new_count, "
                "standing_count, changed_count, collector_validation, "
                "email_status, error_summary, trigger, parking_stats "
                "FROM runs ORDER BY run_id DESC LIMIT ?",
                (RECENT_RUN_LIMIT,),
            ))
            runs = [_row_run(row) for row in rows]
            retrieval_success_row = connection.execute(
                "SELECT run_id, target_date, started_at, completed_at, status, "
                "employee_count, student_count, unique_count, new_count, "
                "standing_count, changed_count, collector_validation, "
                "email_status, error_summary, trigger "
                "FROM runs WHERE status = 'success' ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            if retrieval_success_row is not None:
                last_retrieval_success = _row_run(retrieval_success_row)
            delivery_success_row = connection.execute(
                "SELECT run_id, target_date, started_at, completed_at, status, "
                "employee_count, student_count, unique_count, new_count, "
                "standing_count, changed_count, collector_validation, "
                "email_status, error_summary, trigger "
                "FROM runs WHERE status = 'success' "
                "AND email_status IN ('sent', 'skipped_duplicate') "
                "ORDER BY run_id DESC LIMIT 1"
            ).fetchone()
            if delivery_success_row is not None:
                last_delivery_success = _row_run(delivery_success_row)
            source_summary = connection.execute(
                "SELECT MIN(last_verified_at) AS oldest_verified_at, "
                "MAX(last_verified_at) AS newest_verified_at, "
                "SUM(CASE WHEN last_status != 'ok' THEN 1 ELSE 0 END) AS failed_sources "
                "FROM parking_sources"
            ).fetchone()
            parking = {
                "locations": stats["parking_locations"],
                "sources": stats["parking_sources"],
                "unresolved": stats["parking_unresolved"],
                "oldest_verification_at": source_summary["oldest_verified_at"],
                "newest_verification_at": source_summary["newest_verified_at"],
                "cache_age_seconds": _age_seconds(source_summary["oldest_verified_at"]),
                "failed_sources": source_summary["failed_sources"] or 0,
                "latest_run": _parse_parking_stats(rows[0]["parking_stats"]) if rows else None,
            }
        finally:
            connection.close()
    except (FileNotFoundError, sqlite3.Error, RuntimeError) as exc:
        db_error = _safe_text(exc) or "Unable to read DailyMail history."

    latest = runs[0] if runs else None
    actions = ["run_now", "enable", "disable", "refresh", "view_logs"]
    schedule = {
        "unit": timer.get("timer"),
        "enabled_state": timer.get("timer_enabled") or "unknown",
        "active_state": timer.get("timer_active") or "unknown",
        "next_execution": timer.get("next_elapse") or None,
        "timezone": "America/New_York",
    }
    retrieval = {
        "id": "daily-retrieval",
        "display_name": "Rowan announcement retrieval",
        "kind": "oneshot",
        "unit_name": timer.get("service") or systemd_units.SERVICE_NAME,
        "schedule": schedule,
        "last_attempt": latest["started_at"] if latest else None,
        "last_success": (
            last_retrieval_success["completed_at"] if last_retrieval_success else None
        ),
        "last_result": latest["collector_validation"] if latest else None,
        "safe_actions": actions,
        "unsupported_actions": ["start", "stop", "restart"],
        "status_source": "DailyMail SQLite runs table and systemd user timer",
    }
    digest = {
        "id": "daily-digest",
        "display_name": "Curated digest delivery",
        "kind": "oneshot",
        "unit_name": timer.get("service") or systemd_units.SERVICE_NAME,
        "schedule": schedule,
        "last_attempt": latest["started_at"] if latest else None,
        "last_success": (
            last_delivery_success["completed_at"] if last_delivery_success else None
        ),
        "last_result": latest["email_status"] if latest else None,
        "safe_actions": actions,
        "unsupported_actions": ["start", "stop", "restart"],
        "status_source": "DailyMail SQLite deliveries/runs and systemd user timer",
    }
    problems = _problems(runs, timer, db_error)
    if db_error or not runs:
        health, summary = "unknown", "DailyMail history is unavailable or has not started"
    elif any(problem["severity"] == "failed" for problem in problems):
        health, summary = "failed", problems[0]["summary"]
    elif problems:
        health, summary = "degraded", problems[0]["summary"]
    else:
        health, summary = "healthy", "Latest DailyMail retrieval and digest completed successfully"
    latest_metrics = (
        last_retrieval_success["announcements"] if last_retrieval_success else {}
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "project": "dailymail",
        "observed_at": observed_at,
        "overall": {"health": health, "summary": summary},
        "components": [retrieval, digest],
        "metrics": {
            "announcements": latest_metrics,
            "database": {**stats, "path": str(db.database_path()), "size_bytes": _database_size()},
            "parking_cache": parking,
        },
        "recent_runs": runs,
        "problems": problems,
    }


def _database_size() -> int | None:
    path = db.database_path()
    try:
        return path.stat().st_size
    except OSError:
        return None
