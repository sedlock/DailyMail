"""SQLite persistence.

Plain `sqlite3`, no ORM and no migration framework -- one integer schema version
and a small idempotent `initialize()`.

Durability choices: foreign keys on, WAL journal, a real busy timeout, and every
mutating operation wrapped in a transaction so a run either lands or does not.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .settings import data_dir

SCHEMA_VERSION = 1
BUSY_TIMEOUT_MS = 10_000

OFFICIAL_URL_TEMPLATE = (
    "https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId={submission_id}"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    category_id       INTEGER PRIMARY KEY,
    title             TEXT NOT NULL,
    rowan_rank        INTEGER,
    color             TEXT,
    is_active         INTEGER,
    manual_priority   INTEGER,
    inferred_priority INTEGER,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcements (
    submission_id           INTEGER PRIMARY KEY,
    source_audience         TEXT NOT NULL,
    category_id             INTEGER REFERENCES categories(category_id),
    first_distribution_date TEXT,
    first_observed_at       TEXT NOT NULL,
    last_observed_at        TEXT NOT NULL,
    current_version_id      INTEGER,
    official_url            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS announcement_versions (
    version_id             INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id          INTEGER NOT NULL
                             REFERENCES announcements(submission_id) ON DELETE CASCADE,
    content_hash           TEXT NOT NULL,
    title                  TEXT NOT NULL,
    full_body              TEXT NOT NULL,
    body_text              TEXT,
    short_body             TEXT,
    category_id            INTEGER,
    source_audience        TEXT NOT NULL,
    distribution_dates     TEXT NOT NULL,
    submitted_status       TEXT,
    is_event               INTEGER,
    event_name             TEXT,
    event_date             TEXT,
    event_start_time       TEXT,
    event_end_time         TEXT,
    event_location         TEXT,
    event_no_end           INTEGER,
    contact_name           TEXT,
    contact_department     TEXT,
    contact_job_title      TEXT,
    contact_email          TEXT,
    contact_phone          TEXT,
    submitted_by_name      TEXT,
    submitted_by_department TEXT,
    submitted_by_job_title TEXT,
    submitted_by_email     TEXT,
    submitted_by_phone     TEXT,
    approved_by_name       TEXT,
    approved_by_department TEXT,
    approved_by_job_title  TEXT,
    approved_by_email      TEXT,
    approved_by_phone      TEXT,
    submitted_date         TEXT,
    approved_date          TEXT,
    source_updated_date    TEXT,
    source_updated_by_name TEXT,
    extra_edition          INTEGER,
    body_diagnostics       TEXT,
    first_observed_at      TEXT NOT NULL,
    UNIQUE (submission_id, content_hash)
);

CREATE TABLE IF NOT EXISTS distribution_dates (
    submission_id     INTEGER NOT NULL
                        REFERENCES announcements(submission_id) ON DELETE CASCADE,
    distribution_date TEXT NOT NULL,
    PRIMARY KEY (submission_id, distribution_date)
);

CREATE TABLE IF NOT EXISTS daily_presence (
    target_date   TEXT NOT NULL,
    submission_id INTEGER NOT NULL
                    REFERENCES announcements(submission_id) ON DELETE CASCADE,
    source_view   TEXT NOT NULL CHECK (source_view IN ('Employee', 'Student')),
    observed_at   TEXT NOT NULL,
    PRIMARY KEY (target_date, submission_id, source_view)
);

CREATE TABLE IF NOT EXISTS daily_records (
    target_date   TEXT NOT NULL,
    submission_id INTEGER NOT NULL
                    REFERENCES announcements(submission_id) ON DELETE CASCADE,
    version_id    INTEGER NOT NULL REFERENCES announcement_versions(version_id),
    status        TEXT NOT NULL CHECK (status IN ('New', 'Standing')),
    changed       INTEGER NOT NULL DEFAULT 0,
    observed_at   TEXT NOT NULL,
    PRIMARY KEY (target_date, submission_id)
);

CREATE TABLE IF NOT EXISTS runs (
    run_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date          TEXT NOT NULL,
    started_at           TEXT NOT NULL,
    completed_at         TEXT,
    status               TEXT NOT NULL,
    employee_count       INTEGER,
    student_count        INTEGER,
    unique_count         INTEGER,
    new_count            INTEGER,
    standing_count       INTEGER,
    changed_count        INTEGER,
    collector_validation TEXT,
    curation_method      TEXT,
    email_status         TEXT,
    error_summary        TEXT,
    trigger              TEXT
);

CREATE TABLE IF NOT EXISTS curation_results (
    target_date     TEXT NOT NULL,
    submission_id   INTEGER NOT NULL
                      REFERENCES announcements(submission_id) ON DELETE CASCADE,
    section         TEXT NOT NULL,
    final_rank      INTEGER NOT NULL,
    relevance_score REAL,
    urgency_score   REAL,
    rationale       TEXT,
    method          TEXT NOT NULL,
    model           TEXT,
    created_at      TEXT NOT NULL,
    PRIMARY KEY (target_date, submission_id)
);

CREATE TABLE IF NOT EXISTS deliveries (
    delivery_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date    TEXT NOT NULL,
    recipient      TEXT NOT NULL,
    prepared_at    TEXT NOT NULL,
    sent_at        TEXT,
    message_id     TEXT,
    content_hash   TEXT NOT NULL,
    smtp_status    TEXT,
    state          TEXT NOT NULL,
    message_bytes  INTEGER,
    image_count    INTEGER,
    forced         INTEGER NOT NULL DEFAULT 0,
    error_summary  TEXT
);

CREATE INDEX IF NOT EXISTS idx_versions_submission
    ON announcement_versions (submission_id);
CREATE INDEX IF NOT EXISTS idx_presence_date ON daily_presence (target_date);
CREATE INDEX IF NOT EXISTS idx_records_date ON daily_records (target_date);
CREATE INDEX IF NOT EXISTS idx_deliveries_lookup
    ON deliveries (target_date, recipient, state);
CREATE INDEX IF NOT EXISTS idx_runs_date ON runs (target_date);
CREATE INDEX IF NOT EXISTS idx_distdates_date
    ON distribution_dates (distribution_date);
"""


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def database_path() -> Path:
    return data_dir() / "dailymail.sqlite3"


def official_url(submission_id: int | str) -> str:
    return OFFICIAL_URL_TEMPLATE.format(submission_id=submission_id)


# --- canonical content hashing ----------------------------------------------

# Exactly the user-visible published content. Deliberately excludes every
# timestamp and framework field, so an unrelated `UpdatedDate` touch or a
# re-approval does not manufacture a spurious "UPDATED" badge.
CONTENT_HASH_FIELDS = (
    "title",
    "full_body",
    "category_id",
    "source_audience",
    "submitted_status",
    "is_event",
    "event_name",
    "event_date",
    "event_start_time",
    "event_end_time",
    "event_location",
    "event_no_end",
    "contact_name",
    "contact_department",
    "contact_job_title",
    "contact_email",
    "contact_phone",
    "submitted_by_name",
    "submitted_by_department",
    "submitted_by_job_title",
    "submitted_by_email",
    "submitted_by_phone",
    "approved_by_name",
    "approved_by_department",
    "approved_by_job_title",
    "approved_by_email",
    "approved_by_phone",
)


def content_hash(record: dict) -> str:
    """Stable hash of the substantive, user-visible content of one announcement."""
    payload = {}
    for field in CONTENT_HASH_FIELDS:
        value = record.get(field)
        if isinstance(value, str):
            # Collapse whitespace so cosmetic re-indentation is not "substantive".
            value = " ".join(value.split())
        payload[field] = value
    payload["distribution_dates"] = sorted(record.get("distribution_dates") or [])
    encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


# --- connection --------------------------------------------------------------


def connect(path: Path | None = None, *, create_dirs: bool = True) -> sqlite3.Connection:
    target = path or database_path()
    if create_dirs:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(str(target), timeout=BUSY_TIMEOUT_MS / 1000)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA synchronous = FULL")
    return connection


@contextmanager
def transaction(connection: sqlite3.Connection):
    """Explicit all-or-nothing unit of work."""
    try:
        connection.execute("BEGIN IMMEDIATE")
        yield connection
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()


def initialize(connection: sqlite3.Connection) -> int:
    """Create the schema if needed and return the schema version."""
    with transaction(connection):
        connection.executescript(SCHEMA)
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO schema_meta (key, value) VALUES ('schema_version', ?)",
                (str(SCHEMA_VERSION),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
                "('created_at', ?)",
                (now_utc(),),
            )
            version = SCHEMA_VERSION
        else:
            version = int(row["value"])
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema version {version} is newer than this build "
                    f"({SCHEMA_VERSION}); refusing to operate on it"
                )
    try:
        database_path().parent.chmod(0o700)
    except OSError:  # pragma: no cover - best effort
        pass
    return version


def schema_version(connection: sqlite3.Connection) -> int:
    row = connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'schema_version'"
    ).fetchone()
    return int(row["value"]) if row else 0


# --- categories --------------------------------------------------------------


def upsert_category(
    connection: sqlite3.Connection,
    *,
    category_id: int,
    title: str,
    rowan_rank: int | None,
    color: str | None,
    is_active: bool | None,
    manual_priority: int | None,
    observed_at: str | None = None,
) -> None:
    """Insert or refresh a category. Rowan's list is dynamic, never an enum."""
    stamp = observed_at or now_utc()
    connection.execute(
        """
        INSERT INTO categories (category_id, title, rowan_rank, color, is_active,
                                manual_priority, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(category_id) DO UPDATE SET
            title           = excluded.title,
            rowan_rank      = excluded.rowan_rank,
            color           = excluded.color,
            is_active       = excluded.is_active,
            -- manual_priority comes from config; refresh it, but keep an existing
            -- value if config no longer lists the category.
            manual_priority = COALESCE(excluded.manual_priority,
                                       categories.manual_priority),
            last_seen_at    = excluded.last_seen_at,
            first_seen_at   = MIN(categories.first_seen_at, excluded.first_seen_at)
        """,
        (
            category_id,
            title,
            rowan_rank,
            color,
            None if is_active is None else int(is_active),
            manual_priority,
            stamp,
            stamp,
        ),
    )


def set_inferred_priority(
    connection: sqlite3.Connection, category_id: int, priority: int
) -> None:
    """Record a curation-estimated position for a category absent from config.

    Never touches `manual_priority`, so the manually locked order is safe.
    """
    connection.execute(
        "UPDATE categories SET inferred_priority = ? "
        "WHERE category_id = ? AND manual_priority IS NULL",
        (priority, category_id),
    )


def categories_by_id(connection: sqlite3.Connection) -> dict[int, sqlite3.Row]:
    return {
        row["category_id"]: row
        for row in connection.execute("SELECT * FROM categories")
    }


def effective_priority(row: sqlite3.Row, unknown_default: int) -> int:
    """manual beats inferred beats a stable fallback."""
    if row["manual_priority"] is not None:
        return int(row["manual_priority"])
    if row["inferred_priority"] is not None:
        return int(row["inferred_priority"])
    return unknown_default


# --- announcements and versions ---------------------------------------------

_VERSION_COLUMNS = (
    "submission_id",
    "content_hash",
    "title",
    "full_body",
    "body_text",
    "short_body",
    "category_id",
    "source_audience",
    "distribution_dates",
    "submitted_status",
    "is_event",
    "event_name",
    "event_date",
    "event_start_time",
    "event_end_time",
    "event_location",
    "event_no_end",
    "contact_name",
    "contact_department",
    "contact_job_title",
    "contact_email",
    "contact_phone",
    "submitted_by_name",
    "submitted_by_department",
    "submitted_by_job_title",
    "submitted_by_email",
    "submitted_by_phone",
    "approved_by_name",
    "approved_by_department",
    "approved_by_job_title",
    "approved_by_email",
    "approved_by_phone",
    "submitted_date",
    "approved_date",
    "source_updated_date",
    "source_updated_by_name",
    "extra_edition",
    "body_diagnostics",
    "first_observed_at",
)


def record_announcement(
    connection: sqlite3.Connection, record: dict, *, observed_at: str
) -> tuple[int, bool]:
    """Persist one announcement, versioning only on substantive change.

    Returns `(version_id, changed)` where `changed` is True only when a *new*
    version was created for an announcement we had already seen -- that is, the
    signal behind the UPDATED badge. A first sighting is not "changed".
    """
    submission_id = int(record["submission_id"])
    digest = content_hash(record)

    existing = connection.execute(
        "SELECT submission_id, first_observed_at, current_version_id "
        "FROM announcements WHERE submission_id = ?",
        (submission_id,),
    ).fetchone()

    if existing is None:
        connection.execute(
            """
            INSERT INTO announcements (submission_id, source_audience, category_id,
                                       first_distribution_date, first_observed_at,
                                       last_observed_at, official_url)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                submission_id,
                record["source_audience"],
                record.get("category_id"),
                record.get("first_distribution_date"),
                observed_at,
                observed_at,
                official_url(submission_id),
            ),
        )
        previously_known = False
        previous_version_id = None
    else:
        previously_known = True
        previous_version_id = existing["current_version_id"]
        connection.execute(
            """
            UPDATE announcements SET
                source_audience         = ?,
                category_id             = ?,
                first_distribution_date = ?,
                last_observed_at        = MAX(last_observed_at, ?),
                first_observed_at       = MIN(first_observed_at, ?)
            WHERE submission_id = ?
            """,
            (
                record["source_audience"],
                record.get("category_id"),
                record.get("first_distribution_date"),
                observed_at,
                observed_at,
                submission_id,
            ),
        )

    version_row = connection.execute(
        "SELECT version_id FROM announcement_versions "
        "WHERE submission_id = ? AND content_hash = ?",
        (submission_id, digest),
    ).fetchone()

    created_new_version = version_row is None
    if version_row is not None:
        version_id = int(version_row["version_id"])
    else:
        values = {
            "submission_id": submission_id,
            "content_hash": digest,
            "title": record["title"],
            "full_body": record["full_body"],
            "body_text": record.get("body_text"),
            "short_body": record.get("short_body"),
            "category_id": record.get("category_id"),
            "source_audience": record["source_audience"],
            "distribution_dates": json.dumps(
                sorted(record.get("distribution_dates") or [])
            ),
            "submitted_status": record.get("submitted_status"),
            "is_event": int(bool(record.get("is_event"))),
            "event_name": record.get("event_name"),
            "event_date": record.get("event_date"),
            "event_start_time": record.get("event_start_time"),
            "event_end_time": record.get("event_end_time"),
            "event_location": record.get("event_location"),
            "event_no_end": int(bool(record.get("event_no_end"))),
            "contact_name": record.get("contact_name"),
            "contact_department": record.get("contact_department"),
            "contact_job_title": record.get("contact_job_title"),
            "contact_email": record.get("contact_email"),
            "contact_phone": record.get("contact_phone"),
            "submitted_by_name": record.get("submitted_by_name"),
            "submitted_by_department": record.get("submitted_by_department"),
            "submitted_by_job_title": record.get("submitted_by_job_title"),
            "submitted_by_email": record.get("submitted_by_email"),
            "submitted_by_phone": record.get("submitted_by_phone"),
            "approved_by_name": record.get("approved_by_name"),
            "approved_by_department": record.get("approved_by_department"),
            "approved_by_job_title": record.get("approved_by_job_title"),
            "approved_by_email": record.get("approved_by_email"),
            "approved_by_phone": record.get("approved_by_phone"),
            "submitted_date": record.get("submitted_date"),
            "approved_date": record.get("approved_date"),
            "source_updated_date": record.get("updated_date"),
            "source_updated_by_name": record.get("updated_by_name"),
            "extra_edition": int(bool(record.get("extra_edition"))),
            "body_diagnostics": json.dumps(record.get("body_diagnostics") or {}),
            "first_observed_at": observed_at,
        }
        placeholders = ", ".join("?" for _ in _VERSION_COLUMNS)
        cursor = connection.execute(
            f"INSERT INTO announcement_versions ({', '.join(_VERSION_COLUMNS)}) "
            f"VALUES ({placeholders})",
            tuple(values[column] for column in _VERSION_COLUMNS),
        )
        version_id = int(cursor.lastrowid)

    connection.execute(
        "UPDATE announcements SET current_version_id = ? WHERE submission_id = ?",
        (version_id, submission_id),
    )

    for date_value in sorted(set(record.get("distribution_dates") or [])):
        connection.execute(
            "INSERT OR IGNORE INTO distribution_dates (submission_id, "
            "distribution_date) VALUES (?, ?)",
            (submission_id, date_value),
        )

    changed = bool(
        created_new_version
        and previously_known
        and previous_version_id is not None
        and previous_version_id != version_id
    )
    return version_id, changed


def record_presence(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    submission_id: int,
    source_views: list[str],
    observed_at: str,
) -> None:
    """Independent audit of which source query returned this announcement."""
    for view in source_views:
        connection.execute(
            "INSERT OR IGNORE INTO daily_presence (target_date, submission_id, "
            "source_view, observed_at) VALUES (?, ?, ?, ?)",
            (target_date, submission_id, view, observed_at),
        )


def record_daily(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    submission_id: int,
    version_id: int,
    status: str,
    changed: bool,
    observed_at: str,
) -> None:
    """Upsert the per-day row. `changed` is sticky so re-running a date keeps it."""
    connection.execute(
        """
        INSERT INTO daily_records (target_date, submission_id, version_id, status,
                                   changed, observed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_date, submission_id) DO UPDATE SET
            version_id = excluded.version_id,
            status     = excluded.status,
            changed    = MAX(daily_records.changed, excluded.changed)
        """,
        (target_date, submission_id, version_id, status, int(changed), observed_at),
    )


# --- reads used by curation and rendering -----------------------------------


def digest_rows(connection: sqlite3.Connection, target_date: str) -> list[sqlite3.Row]:
    """Everything needed to curate and render one day, from stored state."""
    return list(
        connection.execute(
            """
            SELECT
                d.target_date,
                d.submission_id,
                d.status,
                d.changed,
                v.*,
                a.official_url,
                a.first_distribution_date AS announcement_first_distribution_date,
                a.first_observed_at       AS announcement_first_observed_at,
                c.title    AS category_title,
                c.color    AS category_color,
                c.manual_priority,
                c.inferred_priority,
                (SELECT GROUP_CONCAT(p.source_view)
                   FROM daily_presence p
                  WHERE p.target_date = d.target_date
                    AND p.submission_id = d.submission_id) AS source_views,
                (SELECT COUNT(*)
                   FROM daily_records prior
                  WHERE prior.submission_id = d.submission_id
                    AND prior.target_date < d.target_date) AS prior_appearances,
                (SELECT COUNT(*)
                   FROM deliveries del
                  WHERE del.state = 'sent'
                    AND del.target_date < d.target_date
                    AND EXISTS (SELECT 1 FROM daily_records dr
                                 WHERE dr.target_date = del.target_date
                                   AND dr.submission_id = d.submission_id)
                 ) AS previous_deliveries
            FROM daily_records d
            JOIN announcement_versions v ON v.version_id = d.version_id
            JOIN announcements a ON a.submission_id = d.submission_id
            LEFT JOIN categories c ON c.category_id = v.category_id
            WHERE d.target_date = ?
            ORDER BY d.submission_id
            """,
            (target_date,),
        )
    )


def counts_for_date(connection: sqlite3.Connection, target_date: str) -> dict:
    row = connection.execute(
        """
        SELECT
          COUNT(*) AS unique_count,
          SUM(CASE WHEN status = 'New' THEN 1 ELSE 0 END) AS new_count,
          SUM(CASE WHEN status = 'Standing' THEN 1 ELSE 0 END) AS standing_count,
          SUM(changed) AS changed_count
        FROM daily_records WHERE target_date = ?
        """,
        (target_date,),
    ).fetchone()
    audience = connection.execute(
        """
        SELECT v.source_audience AS audience, COUNT(*) AS n
        FROM daily_records d
        JOIN announcement_versions v ON v.version_id = d.version_id
        WHERE d.target_date = ?
        GROUP BY v.source_audience
        """,
        (target_date,),
    ).fetchall()
    views = connection.execute(
        "SELECT source_view, COUNT(*) AS n FROM daily_presence "
        "WHERE target_date = ? GROUP BY source_view",
        (target_date,),
    ).fetchall()
    by_audience = {r["audience"]: r["n"] for r in audience}
    by_view = {r["source_view"]: r["n"] for r in views}
    return {
        "unique": row["unique_count"] or 0,
        "new": row["new_count"] or 0,
        "standing": row["standing_count"] or 0,
        "changed": row["changed_count"] or 0,
        "everyone": by_audience.get("Both", 0),
        "employee_only": by_audience.get("Employees", 0),
        "student_only": by_audience.get("Students", 0),
        "employee_view": by_view.get("Employee", 0),
        "student_view": by_view.get("Student", 0),
    }


# --- runs --------------------------------------------------------------------


def start_run(
    connection: sqlite3.Connection, target_date: str, trigger: str = "manual"
) -> int:
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO runs (target_date, started_at, status, trigger) "
            "VALUES (?, ?, 'running', ?)",
            (target_date, now_utc(), trigger),
        )
        return int(cursor.lastrowid)


def finish_run(connection: sqlite3.Connection, run_id: int, **fields) -> None:
    allowed = {
        "status",
        "employee_count",
        "student_count",
        "unique_count",
        "new_count",
        "standing_count",
        "changed_count",
        "collector_validation",
        "curation_method",
        "email_status",
        "error_summary",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates["completed_at"] = now_utc()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with transaction(connection):
        connection.execute(
            f"UPDATE runs SET {assignments} WHERE run_id = ?",
            (*updates.values(), run_id),
        )


# --- curation ----------------------------------------------------------------


def save_curation(
    connection: sqlite3.Connection,
    target_date: str,
    entries: list[dict],
    *,
    method: str,
    model: str | None,
) -> None:
    stamp = now_utc()
    with transaction(connection):
        connection.execute(
            "DELETE FROM curation_results WHERE target_date = ?", (target_date,)
        )
        connection.executemany(
            """
            INSERT INTO curation_results (target_date, submission_id, section,
                final_rank, relevance_score, urgency_score, rationale, method,
                model, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    target_date,
                    int(entry["submission_id"]),
                    entry["section"],
                    int(entry["final_rank"]),
                    entry.get("relevance_score"),
                    entry.get("urgency_score"),
                    entry.get("rationale"),
                    method,
                    model,
                    stamp,
                )
                for entry in entries
            ],
        )


# --- deliveries --------------------------------------------------------------


def successful_delivery(
    connection: sqlite3.Connection, target_date: str, recipient: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM deliveries WHERE target_date = ? AND recipient = ? "
        "AND state = 'sent' ORDER BY delivery_id DESC LIMIT 1",
        (target_date, recipient),
    ).fetchone()


def record_delivery(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    recipient: str,
    content_hash_value: str,
    state: str,
    message_id: str | None = None,
    smtp_status: str | None = None,
    message_bytes: int | None = None,
    image_count: int | None = None,
    forced: bool = False,
    error_summary: str | None = None,
    sent_at: str | None = None,
) -> int:
    with transaction(connection):
        cursor = connection.execute(
            """
            INSERT INTO deliveries (target_date, recipient, prepared_at, sent_at,
                message_id, content_hash, smtp_status, state, message_bytes,
                image_count, forced, error_summary)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                target_date,
                recipient,
                now_utc(),
                sent_at,
                message_id,
                content_hash_value,
                smtp_status,
                state,
                message_bytes,
                image_count,
                int(forced),
                error_summary,
            ),
        )
        return int(cursor.lastrowid)


def statistics(connection: sqlite3.Connection) -> dict:
    def scalar(sql: str) -> int:
        return int(connection.execute(sql).fetchone()[0])

    return {
        "schema_version": schema_version(connection),
        "announcements": scalar("SELECT COUNT(*) FROM announcements"),
        "versions": scalar("SELECT COUNT(*) FROM announcement_versions"),
        "observations": scalar("SELECT COUNT(*) FROM daily_presence"),
        "daily_records": scalar("SELECT COUNT(*) FROM daily_records"),
        "categories": scalar("SELECT COUNT(*) FROM categories"),
        "distribution_dates": scalar("SELECT COUNT(*) FROM distribution_dates"),
        "runs": scalar("SELECT COUNT(*) FROM runs"),
        "deliveries": scalar("SELECT COUNT(*) FROM deliveries"),
        "dates_covered": scalar(
            "SELECT COUNT(DISTINCT target_date) FROM daily_records"
        ),
    }
