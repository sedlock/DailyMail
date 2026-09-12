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

import errno
import json
import logging
import os
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config, redact
from .atomic import UnsafePathError, atomic_write_json

log = logging.getLogger("dailymail.status")

SCHEMA_VERSION = "dailymail.status-snapshot.v1"

# Matches `health.RECENT_RUN_LIMIT`. Kept as its own constant so the snapshot's
# bound is a property of the snapshot rather than of whoever reads it.
RECENT_RUN_LIMIT = 10
# Any single free-text field. `health._safe_text` bounds at 300 on the way out;
# bounding on the way in means the file cannot grow without limit either.
MAX_TEXT = 300
# What the *reader* will tolerate in any one field. The writer clips to
# MAX_TEXT; this is the bound enforced on a file that may not have come from
# this application, with headroom for the compacted stats columns.
MAX_FIELD_TEXT = 2048

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


@contextmanager
def _consistent_read(connection: sqlite3.Connection):
    """A single deferred read transaction, or nothing at all.

    `BEGIN` (deferred) takes a read snapshot on the first SELECT and holds it,
    so every query in the projection sees the same instant. It is opened
    defensively: a caller that already has a transaction open, or a connection
    that refuses one, still gets a document -- just one assembled the old way.
    """
    started = False
    try:
        if not connection.in_transaction:
            connection.execute("BEGIN")
            started = True
    except sqlite3.Error:
        started = False
    try:
        yield connection
    finally:
        if started:
            try:
                connection.execute("COMMIT")
            except sqlite3.Error:  # pragma: no cover - read-only, cannot conflict
                pass


def _clip(value: Any) -> Any:
    """Bound and scrub a free-text column on its way into the file.

    Scrubbed, not merely shortened. `error_summary` carries `str(exc)` from the
    mailer, the collector and the renderer, and `mailer.send` raises
    `recipients refused: [...]` with the address in it. `health` has always
    scrubbed that on the way out -- but the snapshot is a file, and
    `status-snapshot show` prints it, so anything not scrubbed here would reach
    a terminal and an operator's scrollback unscrubbed.
    """
    if not isinstance(value, str):
        return value
    return redact.safe_text(value, limit=MAX_TEXT)


def _compact_stats(value: Any) -> Any:
    """Re-serialize a stats column keeping only its counters.

    `runs.parking_stats` is a JSON *document* in a text column, not prose, and
    `health._parse_parking_stats` reads numbers out of it. Clipping it as free
    text truncated it mid-object, so the snapshot-backed document silently lost
    every parking metric while the database-backed one kept them -- caught by the
    test that requires the two to be identical after a real pipeline run.

    So it is bounded by *dropping what the status document does not read* --
    notably the unbounded `errors` list -- rather than by cutting the string.
    The result is still valid JSON, still the same type, and strictly smaller.
    """
    if not value:
        return value
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return _clip(value)
    if not isinstance(parsed, dict):
        return _clip(value)
    counters = {
        name: entry
        for name, entry in parsed.items()
        if isinstance(entry, (int, float)) and not isinstance(entry, bool)
    }
    return json.dumps(counters, sort_keys=True, separators=(",", ":"))


# Columns holding a JSON document rather than prose.
_STATS_COLUMNS = frozenset({"parking_stats", "calendar_stats"})


def _row(row: sqlite3.Row | None, columns) -> dict | None:
    if row is None:
        return None
    return {
        name: (
            _compact_stats(row[name])
            if name in _STATS_COLUMNS
            else _clip(row[name])
        )
        for name in columns
    }


# --- building ----------------------------------------------------------------


def build(connection: sqlite3.Connection) -> dict:
    """Project the current committed state into a snapshot document.

    Takes an open connection so it runs inside the caller's own session,
    immediately after that caller's transaction has committed. It reads only;
    it never opens the database itself and never creates one.
    """
    from . import __version__, db  # local: db imports this module's writer

    # One read transaction for the whole projection. Without it the ~30 SELECTs
    # below each run in their own implicit transaction, so a commit from another
    # connection can interleave and the document mixes two instants -- measured:
    # `statistics.runs` counting a run that `recent_runs` does not contain.
    # Every value was committed either way, so this is about internal
    # consistency rather than about reading dirty data. In WAL a read
    # transaction does not block writers.
    with _consistent_read(connection):
        return _project(connection, db, __version__)


def _project(connection: sqlite3.Connection, db, version: str) -> dict:
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

    # Informational only. Taken during a transition the main database file has
    # not been checkpointed yet, so this can read far smaller than the real
    # size (measured: 4,096 against a live 253,952). `health` therefore prefers
    # its own `stat()`, which works perfectly well inside the collector sandbox
    # -- a file size needs no WAL index.
    try:
        size_bytes = db.database_path().stat().st_size
    except OSError:
        size_bytes = None

    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": db.now_utc(),
        "application_version": version,
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

    The except clause is deliberately bare. A narrower one (OSError, sqlite3
    errors, ValueError, TypeError) still let `MemoryError` and `RecursionError`
    escape *after* a delivery row had been committed, which would mark a
    successful run failed because writing an observability file went wrong.
    Nothing this function can raise is worth that.
    """
    try:
        return write(connection)
    except Exception as exc:  # noqa: BLE001 - see the docstring
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

    # One descriptor, opened with O_NOFOLLOW, then checked and read through that
    # same descriptor. Resolving the path several times -- symlink check, stat,
    # open -- would let anyone who can write the directory swap the file between
    # the checks, defeating both the symlink refusal and the size bound. The
    # bound is enforced on the bytes actually read rather than on what `stat`
    # reported, for the same reason.
    limit = config.STATUS_SNAPSHOT_MAX_BYTES
    # O_NONBLOCK matters as much as O_NOFOLLOW: without it, opening a FIFO for
    # reading blocks until a writer appears, so anyone able to put a FIFO at
    # this path could hang the status probe forever -- a denial of service on the
    # one command that is supposed to always answer. On a regular file the flag
    # does nothing. Found by a test that created a FIFO and never returned.
    try:
        descriptor = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError as exc:
        raise SnapshotError("no status snapshot has been written yet") from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise SnapshotError(
                "status snapshot is a symlink; refusing to read it"
            ) from exc
        raise SnapshotError(f"status snapshot is unreadable: {exc}") from exc

    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise SnapshotError("status snapshot is not a regular file")
        if info.st_size > limit:
            raise SnapshotError(
                f"status snapshot is {info.st_size} bytes, above the "
                f"{limit}-byte bound"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = -1
            payload = stream.read(limit + 1)
    except SnapshotError:
        if descriptor >= 0:
            os.close(descriptor)
        raise
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise SnapshotError(f"status snapshot is unreadable: {exc}") from exc

    if len(payload) > limit:
        raise SnapshotError(
            f"status snapshot exceeds the {limit}-byte bound while being read"
        )
    try:
        raw = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SnapshotError(f"status snapshot is not valid UTF-8: {exc}") from exc

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
    generated_at = document.get("generated_at")
    if not generated_at or not isinstance(generated_at, str):
        raise SnapshotError("status snapshot has no generated_at timestamp")
    try:
        datetime.fromisoformat(generated_at)
    except ValueError as exc:
        raise SnapshotError(
            f"status snapshot generated_at is not a timestamp: {generated_at[:64]!r}"
        ) from exc
    for key in ("statistics", "recent_runs"):
        if key not in document:
            raise SnapshotError(f"status snapshot is missing {key!r}")
    if not isinstance(document["recent_runs"], list):
        raise SnapshotError("status snapshot recent_runs is not a list")
    if not isinstance(document["statistics"], dict):
        raise SnapshotError("status snapshot statistics is not an object")
    if len(document["recent_runs"]) > RECENT_RUN_LIMIT:
        raise SnapshotError(
            f"status snapshot carries {len(document['recent_runs'])} runs, above "
            f"the bound of {RECENT_RUN_LIMIT}"
        )

    # Every row is checked field by field before anything downstream touches it.
    # `health` reads these with `row["column"]` and hands the values straight
    # into the published contract, so a row that is merely *present* is not good
    # enough: a missing key would raise out of a status probe, and a non-scalar
    # value would be copied verbatim into a document another service consumes.
    # A snapshot we cannot fully trust is reported as unusable, never partially
    # believed.
    for entry in document["recent_runs"]:
        _validate_row(entry, RUN_COLUMNS, "run")
    for key in ("last_retrieval_success", "last_delivery_success"):
        _validate_optional_row(document.get(key), RUN_COLUMNS, key)
    for key in ("latest_delivery", "last_confirmed_delivery"):
        _validate_optional_row(document.get(key), DELIVERY_COLUMNS, key)

    sources = document.get("parking_sources")
    if sources is not None and not isinstance(sources, dict):
        raise SnapshotError("status snapshot parking_sources is not an object")

    for name, value in document["statistics"].items():
        if not isinstance(name, str) or not _is_scalar(value):
            raise SnapshotError(
                f"status snapshot statistic {name!r} is not a scalar"
            )
        if len(name) > MAX_FIELD_TEXT:
            raise SnapshotError("status snapshot carries an oversized statistic name")

    return document


def _is_scalar(value: Any) -> bool:
    """What may appear in a published status document: a number, text or null."""
    return value is None or isinstance(value, (int, float, str, bool))


def _validate_row(entry: Any, columns, label: str) -> None:
    if not isinstance(entry, dict):
        raise SnapshotError(f"status snapshot contains a malformed {label} entry")
    missing = [name for name in columns if name not in entry]
    if missing:
        raise SnapshotError(
            f"status snapshot {label} entry is missing {', '.join(sorted(missing))}"
        )
    for name in columns:
        value = entry[name]
        if not _is_scalar(value):
            raise SnapshotError(
                f"status snapshot {label} entry field {name!r} is not a scalar"
            )
        # The writer clips to MAX_TEXT; the reader must not take that on trust,
        # because the file could have come from anywhere. A little headroom is
        # allowed for the compacted stats columns, which are short JSON rather
        # than prose.
        if isinstance(value, str) and len(value) > MAX_FIELD_TEXT:
            raise SnapshotError(
                f"status snapshot {label} entry field {name!r} is "
                f"{len(value)} characters, above the {MAX_FIELD_TEXT} bound"
            )


def _validate_optional_row(entry: Any, columns, label: str) -> None:
    if entry is None:
        return
    _validate_row(entry, columns, label)


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
