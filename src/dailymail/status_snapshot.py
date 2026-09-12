"""A bounded, redacted projection of committed DailyMail state.

Why this exists
---------------

`dailymail health --json` reads the live SQLite database read-only. The database
is in WAL mode, and a WAL database cannot be read -- not even read-only -- without
the `-shm` shared-memory index, which SQLite must **create** in the database's own
directory when it is absent. SQLite unlinks `-wal` and `-shm` when the last
connection closes cleanly, so `-shm` is absent except during the roughly two
minutes `dailymail.service` actually runs.

ControlPanel's collector runs with `ProtectHome=read-only`. It can read
`~/.local/share/dailymail` and cannot write it, so the creation fails with
`SQLITE_CANTOPEN` and the probe returns a structurally valid but blank document.
Measured over 2026-09-06..2026-09-11: **6,786 of 6,801 collections (99.78%)**
returned that blank document, and every daily run was first observed roughly
**24 hours late** -- at the instant the *next* day's run happened to open the
database and leave a `-shm` behind.

So DailyMail publishes what it knows, instead of asking an observer to take a
lock on its database to find out.

What this is not
----------------

It is not a cache, and reading it is not equivalent to probing the database. The
database remains authoritative for every question. This file answers exactly one:
"what was true at DailyMail's last committed state transition?" -- and it always
answers it with a timestamp attached, so `health` can say *snapshot* rather than
implying it looked.

Ruled out, with evidence, before choosing this (see the ControlPanel work order):

* `immutable=1` opens cleanly with no `-shm` and **silently ignores uncheckpointed
  WAL content** -- measured returning 51 runs where `mode=ro` read 52. A probe
  that quietly reports yesterday's digest as current is worse than one that says
  "unavailable".
* `nolock=1` still returns `SQLITE_CANTOPEN`.
* Reverting to DELETE journal mode surrenders WAL concurrency for the whole
  application to satisfy a status probe.
* Granting ControlPanel write access makes the "read-only" probe a writer to
  another application's private state.

Boundedness and redaction
-------------------------

Everything here is already in the `controlpanel.status.v1` document DailyMail
publishes, or is a count. No announcement body, no source HTML, no recipient, no
credential, no header, no environment, no prompt, no unbounded error, no
unbounded history. Errors are truncated; the run list is capped; the whole
document is refused on read above `config.STATUS_SNAPSHOT_MAX_BYTES`.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path
from typing import Any

from . import config
from .atomic import UnsafePathError, atomic_write_json

log = logging.getLogger("dailymail.status")

SCHEMA_VERSION = "dailymail.status-snapshot.v1"

# Matches `health.RECENT_RUN_LIMIT`. Kept as its own constant so the snapshot's
# bound is a property of the snapshot rather than of whoever reads it.
RECENT_RUN_LIMIT = 10
# Any single free-text field. `health._safe_text` bounds at 300 on the way out;
# bounding on the way in means the file cannot grow without limit either.
MAX_TEXT = 300

# Exactly the columns `health.build_status` selects from `runs`. Keeping the
# lists identical is what makes a snapshot-backed document and a database-backed
# document agree field for field rather than approximately.
RUN_COLUMNS = (
    "run_id",
    "target_date",
    "started_at",
    "completed_at",
    "status",
    "employee_count",
    "student_count",
    "unique_count",
    "new_count",
    "standing_count",
    "changed_count",
    "collector_validation",
    "email_status",
    "error_summary",
    "trigger",
    "parking_stats",
)

DELIVERY_COLUMNS = (
    "delivery_id",
    "target_date",
    "prepared_at",
    "sent_at",
    "state",
    "smtp_status",
    "message_bytes",
    "image_count",
    "forced",
    "error_summary",
)

_RUN_QUERY = f"SELECT {', '.join(RUN_COLUMNS)} FROM runs "


class SnapshotError(RuntimeError):
    """The snapshot could not be read, parsed or trusted."""


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[: MAX_TEXT - 1] + "…"
    return value


def _row(row: sqlite3.Row | None, columns) -> dict | None:
    if row is None:
        return None
    return {name: _clip(row[name]) for name in columns}


# --- building ----------------------------------------------------------------


def build(connection: sqlite3.Connection) -> dict:
    """Project the current committed state into a snapshot document.

    Takes an open connection so it runs inside the caller's own session,
    immediately after that caller's transaction has committed. It reads only;
    it never opens the database itself and never creates one.
    """
    from . import __version__, db  # local: db imports this module's writer

    schema_row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()

    recent = [
        _row(row, RUN_COLUMNS)
        for row in connection.execute(
            _RUN_QUERY + "ORDER BY run_id DESC LIMIT ?", (RECENT_RUN_LIMIT,)
        )
    ]
    last_retrieval_success = _row(
        connection.execute(
            _RUN_QUERY + "WHERE status = 'success' ORDER BY run_id DESC LIMIT 1"
        ).fetchone(),
        RUN_COLUMNS,
    )
    last_delivery_success = _row(
        connection.execute(
            _RUN_QUERY
            + "WHERE status = 'success' AND email_status IN ('sent', "
            "'skipped_duplicate') ORDER BY run_id DESC LIMIT 1"
        ).fetchone(),
        RUN_COLUMNS,
    )

    # Delivery state in its own right, not inferred from `runs.email_status`.
    # The recipient is deliberately not carried: it is an address, and nothing
    # in the status contract needs it.
    latest_delivery = _row(
        connection.execute(
            f"SELECT {', '.join(DELIVERY_COLUMNS)} FROM deliveries "
            "ORDER BY delivery_id DESC LIMIT 1"
        ).fetchone(),
        DELIVERY_COLUMNS,
    )
    last_confirmed_delivery = _row(
        connection.execute(
            f"SELECT {', '.join(DELIVERY_COLUMNS)} FROM deliveries "
            "WHERE state = 'sent' ORDER BY delivery_id DESC LIMIT 1"
        ).fetchone(),
        DELIVERY_COLUMNS,
    )

    source_summary = connection.execute(
        "SELECT MIN(last_verified_at) AS oldest_verified_at, "
        "MAX(last_verified_at) AS newest_verified_at, "
        "SUM(CASE WHEN last_status != 'ok' THEN 1 ELSE 0 END) AS failed_sources "
        "FROM parking_sources"
    ).fetchone()

    try:
        size_bytes = db.database_path().stat().st_size
    except OSError:
        size_bytes = None

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": db.now_utc(),
        "application_version": __version__,
        "database_schema_version": (
            int(schema_row["value"]) if schema_row is not None else None
        ),
        "database_size_bytes": size_bytes,
        "statistics": db.statistics(connection),
        "recent_runs": recent,
        "last_retrieval_success": last_retrieval_success,
        "last_delivery_success": last_delivery_success,
        "latest_delivery": latest_delivery,
        "last_confirmed_delivery": last_confirmed_delivery,
        "parking_sources": {
            "oldest_verified_at": source_summary["oldest_verified_at"],
            "newest_verified_at": source_summary["newest_verified_at"],
            "failed_sources": source_summary["failed_sources"] or 0,
        },
        "source": "dailymail",
    }


# --- writing -----------------------------------------------------------------


def write(connection: sqlite3.Connection, *, path: Path | None = None) -> Path:
    """Build and atomically publish the snapshot. Raises on failure."""
    document = build(connection)
    return atomic_write_json(
        path or config.status_snapshot_path(),
        document,
        mode=config.STATUS_SNAPSHOT_MODE,
        # A status file that survives a crash half-written would defeat the
        # point, and it is a few kilobytes once or twice a day.
        fsync=True,
    )


def write_after_commit(connection: sqlite3.Connection) -> Path | None:
    """Publish the snapshot, and never let failing to do so break the run.

    Called from `db.start_run`, `db.finish_run` and `db.record_delivery`, each
    time *after* that function's own transaction has committed -- so the file
    can only ever describe state the database has already accepted, and a
    rolled-back transaction never reaches here at all.

    Observability is not permitted to change business behaviour. If this cannot
    write, the digest still goes out, the delivery is still recorded, nothing is
    retried and nothing is rolled back; the failure is logged, and the next
    `health` call reports the snapshot as missing or stale rather than
    pretending. That is strictly better than a status file that can abort a
    delivery.
    """
    try:
        return write(connection)
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        log.warning(
            "could not publish the status snapshot (%s: %s); committed state is "
            "unaffected and health will report the snapshot as unavailable",
            type(exc).__name__,
            exc,
        )
        return None


# --- reading -----------------------------------------------------------------


def read(path: Path | None = None) -> dict:
    """Load and validate the snapshot. Raises `SnapshotError` on any doubt.

    Every failure mode is distinct and none of them is "zero". A snapshot that
    is missing, truncated, oversized, of an unknown schema or structurally wrong
    must be reported as such, because a status document showing zero runs is
    indistinguishable from a DailyMail that has never run.
    """
    target = Path(path or config.status_snapshot_path())

    if target.is_symlink():
        raise SnapshotError("status snapshot is a symlink; refusing to read it")
    try:
        info = target.stat()
    except FileNotFoundError as exc:
        raise SnapshotError("no status snapshot has been written yet") from exc
    except OSError as exc:
        raise SnapshotError(f"status snapshot is unreadable: {exc}") from exc

    if not target.is_file():
        raise SnapshotError("status snapshot is not a regular file")
    if info.st_size > config.STATUS_SNAPSHOT_MAX_BYTES:
        raise SnapshotError(
            f"status snapshot is {info.st_size} bytes, above the "
            f"{config.STATUS_SNAPSHOT_MAX_BYTES}-byte bound"
        )

    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise SnapshotError(f"status snapshot is unreadable: {exc}") from exc

    try:
        document = json.loads(raw)
    except ValueError as exc:
        raise SnapshotError(f"status snapshot is malformed JSON: {exc}") from exc

    if not isinstance(document, dict):
        raise SnapshotError("status snapshot is not a JSON object")

    schema = document.get("schema_version")
    if schema != SCHEMA_VERSION:
        raise SnapshotError(
            f"status snapshot schema {schema!r} is not {SCHEMA_VERSION!r}"
        )
    if not document.get("generated_at"):
        raise SnapshotError("status snapshot has no generated_at timestamp")
    for key in ("statistics", "recent_runs"):
        if key not in document:
            raise SnapshotError(f"status snapshot is missing {key!r}")
    if not isinstance(document["recent_runs"], list):
        raise SnapshotError("status snapshot recent_runs is not a list")
    if not isinstance(document["statistics"], dict):
        raise SnapshotError("status snapshot statistics is not an object")
    for entry in document["recent_runs"]:
        if not isinstance(entry, dict) or "run_id" not in entry:
            raise SnapshotError("status snapshot contains a malformed run entry")

    return document


def describe_path() -> str:
    """The snapshot location, for diagnostics. Never the contents."""
    return str(config.status_snapshot_path())


__all__ = [
    "SCHEMA_VERSION",
    "SnapshotError",
    "UnsafePathError",
    "build",
    "describe_path",
    "read",
    "write",
    "write_after_commit",
]
