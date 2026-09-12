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

SCHEMA_VERSION = 4
BUSY_TIMEOUT_MS = 10_000
# MEMORY, not the default 0 (= a file in TMPDIR). See `_apply_temp_store`.
TEMP_STORE = "MEMORY"
# Negative means KiB rather than pages. 16 MiB keeps the whole delivered
# corpus comparison in the page cache on a normal day.
CACHE_SIZE_KIB = -16000

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

# --- schema v2: durable parking reference data -------------------------------
#
# Separate from the announcement tables on purpose. Parking geography is
# *reference* data: it changes on the scale of years, it is shared across every
# announcement, and it is never derived from a single day's collection. Keeping
# it in its own tables means a parking refresh can never touch announcement
# history, and announcement retention can never drop a lot.
PARKING_SCHEMA = """
CREATE TABLE IF NOT EXISTS parking_sources (
    source_id         TEXT PRIMARY KEY,
    campus            TEXT NOT NULL,
    source_type       TEXT NOT NULL,
    source_url        TEXT NOT NULL,
    source_map_id     TEXT,
    machine_readable  INTEGER NOT NULL DEFAULT 0,
    fingerprint       TEXT,
    source_version    TEXT,
    last_retrieved_at TEXT,
    last_verified_at  TEXT,
    last_changed_at   TEXT,
    last_status       TEXT,
    last_error        TEXT,
    locations_seen    INTEGER
);

CREATE TABLE IF NOT EXISTS parking_locations (
    location_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_id        TEXT NOT NULL UNIQUE,
    campus              TEXT NOT NULL,
    canonical_name      TEXT NOT NULL,
    normalized_name     TEXT NOT NULL,
    location_type       TEXT NOT NULL,
    permit_class        TEXT NOT NULL DEFAULT 'Unknown',
    description         TEXT,
    latitude            REAL,
    longitude           REAL,
    source_id           TEXT,
    source_type         TEXT,
    source_url          TEXT,
    source_map_id       TEXT,
    source_fingerprint  TEXT,
    provenance          TEXT,
    confidence          TEXT NOT NULL DEFAULT 'low',
    is_active           INTEGER NOT NULL DEFAULT 1,
    manual_override     INTEGER NOT NULL DEFAULT 0,
    override_fields     TEXT,
    description_method  TEXT,
    description_model   TEXT,
    first_discovered_at TEXT NOT NULL,
    last_verified_at    TEXT NOT NULL,
    last_changed_at     TEXT NOT NULL,
    UNIQUE (campus, normalized_name)
);

CREATE TABLE IF NOT EXISTS parking_aliases (
    alias_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    location_id      INTEGER NOT NULL
                       REFERENCES parking_locations(location_id) ON DELETE CASCADE,
    campus           TEXT NOT NULL,
    alias            TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    scannable        INTEGER NOT NULL DEFAULT 0,
    origin           TEXT NOT NULL DEFAULT 'generated',
    created_at       TEXT NOT NULL,
    UNIQUE (campus, normalized_alias)
);

CREATE TABLE IF NOT EXISTS announcement_parking_locations (
    target_date      TEXT NOT NULL,
    submission_id    INTEGER NOT NULL
                       REFERENCES announcements(submission_id) ON DELETE CASCADE,
    normalized_match TEXT NOT NULL,
    version_id       INTEGER,
    location_id      INTEGER
                       REFERENCES parking_locations(location_id) ON DELETE SET NULL,
    matched_text     TEXT NOT NULL,
    match_method     TEXT NOT NULL,
    confidence       TEXT,
    campus_hint      TEXT NOT NULL DEFAULT '',
    resolved_at      TEXT NOT NULL,
    PRIMARY KEY (target_date, submission_id, normalized_match)
);

CREATE TABLE IF NOT EXISTS parking_unresolved (
    normalized_match TEXT NOT NULL,
    campus_hint      TEXT NOT NULL DEFAULT '',
    matched_text     TEXT NOT NULL,
    attempts         INTEGER NOT NULL DEFAULT 0,
    resolver_calls   INTEGER NOT NULL DEFAULT 0,
    last_reason      TEXT,
    first_seen_at    TEXT NOT NULL,
    last_seen_at     TEXT NOT NULL,
    PRIMARY KEY (normalized_match, campus_hint)
);

-- Named campus features from the same authoritative layers as the lots. Cached
-- so campus disambiguation ("Lot A beside Rowan Medicine") works every morning
-- without refetching anything.
CREATE TABLE IF NOT EXISTS parking_landmarks (
    landmark_id     INTEGER PRIMARY KEY AUTOINCREMENT,
    campus          TEXT NOT NULL,
    name            TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    category        TEXT,
    latitude        REAL NOT NULL,
    longitude       REAL NOT NULL,
    source_id       TEXT,
    last_verified_at TEXT NOT NULL,
    UNIQUE (campus, normalized_name)
);

CREATE INDEX IF NOT EXISTS idx_parking_alias_lookup
    ON parking_aliases (normalized_alias);
CREATE INDEX IF NOT EXISTS idx_parking_landmarks_name
    ON parking_landmarks (normalized_name);
CREATE INDEX IF NOT EXISTS idx_parking_alias_scan
    ON parking_aliases (scannable);
CREATE INDEX IF NOT EXISTS idx_parking_locations_campus
    ON parking_locations (campus, is_active);
CREATE INDEX IF NOT EXISTS idx_announcement_parking_date
    ON announcement_parking_locations (target_date);
"""


# --- schema v3: calendar actions, event venues, logical repeats ---------------
#
# Three additive concerns, kept in their own tables for the same reason parking
# is: they are derived, auditable and rebuildable, and none of them may ever be
# able to touch announcement history.
#
#   * `event_venues`      reference data -- a venue's coordinates and the travel
#                         time from the configured base. Venues repeat weekly,
#                         so this is what stops us geocoding the same ballroom
#                         every morning.
#   * `calendar_recommendations`  one row per (date, announcement): what we
#                         decided and why, so behaviour can be tuned without
#                         regenerating anything.
#   * `repeat_matches`    which prior announcement a new SubmissionId was found
#                         to be a logical repeat of, with the evidence.
CALENDAR_SCHEMA = """
CREATE TABLE IF NOT EXISTS event_venues (
    venue_id            INTEGER PRIMARY KEY AUTOINCREMENT,
    normalized_venue    TEXT NOT NULL UNIQUE,
    display_name        TEXT NOT NULL,
    campus              TEXT,
    latitude            REAL,
    longitude           REAL,
    source              TEXT NOT NULL,
    source_detail       TEXT,
    travel_mode         TEXT,
    outbound_minutes    INTEGER,
    return_minutes      INTEGER,
    distance_metres     REAL,
    routing_source      TEXT,
    routing_fingerprint TEXT,
    routing_estimated   INTEGER NOT NULL DEFAULT 0,
    first_seen_at       TEXT NOT NULL,
    last_verified_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS calendar_recommendations (
    target_date         TEXT NOT NULL,
    submission_id       INTEGER NOT NULL
                          REFERENCES announcements(submission_id) ON DELETE CASCADE,
    version_id          INTEGER,
    is_event_candidate  INTEGER NOT NULL DEFAULT 0,
    candidate_evidence  TEXT,
    offer_calendar      INTEGER NOT NULL DEFAULT 0,
    relevance_score     REAL,
    relevance_reason    TEXT,
    relevance_method    TEXT,
    relevance_model     TEXT,
    attendance_mode     TEXT,
    calendar_title      TEXT,
    event_date          TEXT,
    start_datetime      TEXT,
    end_datetime        TEXT,
    timezone            TEXT,
    location            TEXT,
    venue_id            INTEGER REFERENCES event_venues(venue_id) ON DELETE SET NULL,
    travel_required     INTEGER NOT NULL DEFAULT 0,
    travel_mode         TEXT,
    travel_minutes_before INTEGER,
    travel_minutes_after  INTEGER,
    travel_estimated    INTEGER NOT NULL DEFAULT 0,
    mechanism           TEXT,
    action_url          TEXT,
    ics_filename        TEXT,
    ics_bytes           INTEGER,
    validation_status   TEXT NOT NULL DEFAULT 'ok',
    withheld_reason     TEXT,
    -- How many selectable sittings the announcement offered, and one JSON line
    -- per sitting: its times and which .ics carries it.
    session_count       INTEGER,
    sessions            TEXT,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (target_date, submission_id)
);

-- A newly created Rowan SubmissionId that carries the same informational
-- announcement as one already delivered. Rowan submitters routinely repost
-- rather than extend a distribution list, so this is observed behaviour, not a
-- hypothetical. Evidence is stored so a match can be audited or reversed.
CREATE TABLE IF NOT EXISTS repeat_matches (
    submission_id       INTEGER PRIMARY KEY
                          REFERENCES announcements(submission_id) ON DELETE CASCADE,
    matched_submission_id INTEGER NOT NULL
                          REFERENCES announcements(submission_id) ON DELETE CASCADE,
    family_key          TEXT NOT NULL,
    method              TEXT NOT NULL,
    confidence          REAL NOT NULL,
    body_similarity     REAL,
    materially_changed  INTEGER NOT NULL DEFAULT 0,
    evidence            TEXT,
    first_detected_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_calendar_recs_date
    ON calendar_recommendations (target_date);
CREATE INDEX IF NOT EXISTS idx_repeat_family ON repeat_matches (family_key);
"""


# --- v4: durable logical-announcement families -------------------------------
#
# `repeat_matches` records one *pairwise* finding: 6768 repeats 6665. That was
# enough while every comparison ran against a recent delivered corpus, but it
# carries no forward memory. Two consequences bit production on 1 September 2026:
#
#   * a repost is compared only against announcements still inside the delivered
#     corpus, so the original eventually ages out of reach, and
#   * a day on which detection fails loses the finding entirely -- the next
#     repost starts again from nothing.
#
# A family makes the relationship durable. Once DailyMail has established that
# two SubmissionIds are the same logical announcement, both become members of a
# family keyed on the normalized title, category and audience; a later repost is
# compared against the family's canonical and most recent members regardless of
# how long ago they were delivered.
#
# The family only ever *widens the candidate pool*. Every veto in `repeats.compare`
# still applies to each comparison, so this cannot make a false positive more
# likely -- it can only stop a true positive being missed for want of a candidate.
FAMILY_SCHEMA = """
CREATE TABLE IF NOT EXISTS logical_announcement_families (
    family_id               INTEGER PRIMARY KEY AUTOINCREMENT,
    family_key              TEXT NOT NULL UNIQUE,
    canonical_submission_id INTEGER NOT NULL
                              REFERENCES announcements(submission_id) ON DELETE CASCADE,
    normalized_title        TEXT NOT NULL,
    category_id             INTEGER,
    source_audience         TEXT,
    member_count            INTEGER NOT NULL DEFAULT 0,
    confidence              REAL,
    match_method            TEXT,
    created_at              TEXT NOT NULL,
    last_seen_at            TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS logical_announcement_members (
    family_id             INTEGER NOT NULL
                            REFERENCES logical_announcement_families(family_id)
                            ON DELETE CASCADE,
    submission_id         INTEGER NOT NULL
                            REFERENCES announcements(submission_id) ON DELETE CASCADE,
    matched_submission_id INTEGER,
    content_hash          TEXT,
    match_confidence      REAL,
    match_method          TEXT,
    is_canonical          INTEGER NOT NULL DEFAULT 0,
    first_seen            TEXT NOT NULL,
    last_seen             TEXT NOT NULL,
    PRIMARY KEY (family_id, submission_id)
);

-- Why a persisted display_status was changed after the fact. Rowan's own
-- `daily_records.status` is never rewritten and neither is run history; this is
-- the audit trail for a correction to what the digest *shows*.
CREATE TABLE IF NOT EXISTS display_status_corrections (
    correction_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    target_date    TEXT NOT NULL,
    submission_id  INTEGER NOT NULL,
    previous_display_status TEXT,
    new_display_status      TEXT,
    reason         TEXT NOT NULL,
    corrected_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_family_members_submission
    ON logical_announcement_members (submission_id);
CREATE INDEX IF NOT EXISTS idx_family_key
    ON logical_announcement_families (family_key);
CREATE INDEX IF NOT EXISTS idx_corrections_date
    ON display_status_corrections (target_date);
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


def _apply_temp_store(connection: sqlite3.Connection) -> None:
    """Keep every sort and GROUP BY spill in memory, never in a temp file.

    This is not a performance tweak. SQLite's default `temp_store` writes a
    materialized temp b-tree to `$SQLITE_TMPDIR`/`$TMPDIR`/`/var/tmp`/`/tmp`, and
    on 1 September 2026 the filesystem holding this host's `TMPDIR` was mounted
    read-only for five hours across the 06:30 run. The spill failed, SQLite
    raised `SQLITE_CANTOPEN` ("unable to open database file"), and the entire
    logical-repeat stage was skipped for that day's digest -- so a Cayuse repost
    the reader had already been sent three times arrived labelled NEW.

    The digest's own data lives on a different filesystem to `TMPDIR`, so a
    working database plus an unwritable, unrelated temp directory must not be
    able to change what the reader is told. Pinning temp storage to memory
    removes that coupling outright. The spill is a few megabytes; the queries
    that need it are also narrowed (see `delivered_history`).

    `PRAGMA temp_store` costs nothing and cannot fail meaningfully, but a build
    compiled with `SQLITE_TEMP_STORE=0` rejects it, so a refusal is tolerated.
    """
    try:
        connection.execute(f"PRAGMA temp_store = {TEMP_STORE}")
        connection.execute(f"PRAGMA cache_size = {CACHE_SIZE_KIB}")
    except sqlite3.DatabaseError:  # pragma: no cover - build-dependent
        pass


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
    _apply_temp_store(connection)
    return connection


def connect_readonly(path: Path | None = None) -> sqlite3.Connection:
    """Open the existing database without creating or changing anything.

    This is intentionally separate from :func:`connect`: an operator status
    command must be safe to run while a production job owns the writer lock and
    must never bootstrap an empty database as a side effect of being observed.
    """
    target = path or database_path()
    if not target.is_file():
        raise FileNotFoundError(f"database does not exist: {target}")
    connection = sqlite3.connect(
        f"file:{target}?mode=ro", uri=True, timeout=BUSY_TIMEOUT_MS / 1000
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
    _apply_temp_store(connection)
    connection.execute("PRAGMA query_only = ON")
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


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        row["name"] for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _upgrade_to_v2(connection: sqlite3.Connection) -> None:
    """Add the parking reference tables and the per-run parking metrics column.

    Idempotent and additive: every statement is `IF NOT EXISTS` or guarded, no
    existing table is rewritten, and no announcement history is touched. Runs
    inside the caller's transaction so a failure leaves v1 intact.
    """
    connection.executescript(PARKING_SCHEMA)
    if "parking_stats" not in _column_names(connection, "runs"):
        connection.execute("ALTER TABLE runs ADD COLUMN parking_stats TEXT")


def _upgrade_to_v3(connection: sqlite3.Connection) -> None:
    """Add calendar/venue/repeat tables and the two derived announcement columns.

    Additive and idempotent, exactly like v2: `CREATE TABLE IF NOT EXISTS` plus
    guarded `ALTER TABLE ... ADD COLUMN`. No existing row is rewritten and no
    announcement content is read, so a failure leaves v2 intact.

    `daily_records.display_status` is deliberately a *separate* column from
    `status`. `status` stays exactly what Rowan's own distribution dates say --
    the source semantics are never lost -- while `display_status` is what the
    digest shows once logical-repeat detection has had its say.
    """
    connection.executescript(CALENDAR_SCHEMA)
    if "display_status" not in _column_names(connection, "daily_records"):
        connection.execute(
            "ALTER TABLE daily_records ADD COLUMN display_status TEXT"
        )
    if "calendar_stats" not in _column_names(connection, "runs"):
        connection.execute("ALTER TABLE runs ADD COLUMN calendar_stats TEXT")


def _upgrade_to_v4(connection: sqlite3.Connection) -> None:
    """Add the durable logical-family tables and the correction audit trail.

    Additive and idempotent like v2 and v3. It also *backfills* families from the
    pairwise findings already in `repeat_matches`, so the knowledge earned in
    production before this schema existed is not thrown away: the Cayuse family
    (6665 -> 6668 -> 6768) becomes resolvable immediately rather than after the
    next repost happens to fall inside a comparison window.
    """
    connection.executescript(FAMILY_SCHEMA)
    if "family_id" not in _column_names(connection, "repeat_matches"):
        connection.execute("ALTER TABLE repeat_matches ADD COLUMN family_id INTEGER")
    # One announcement can now offer several selectable sittings, so the audit
    # row records how many and what each one carries.
    calendar_columns = _column_names(connection, "calendar_recommendations")
    if "session_count" not in calendar_columns:
        connection.execute(
            "ALTER TABLE calendar_recommendations ADD COLUMN session_count INTEGER"
        )
    if "sessions" not in calendar_columns:
        connection.execute(
            "ALTER TABLE calendar_recommendations ADD COLUMN sessions TEXT"
        )
    backfill_families_from_repeat_matches(connection)


# Applied in ascending order for any version below SCHEMA_VERSION. Each entry
# must be safe to run against an already-upgraded database.
MIGRATIONS = {2: _upgrade_to_v2, 3: _upgrade_to_v3, 4: _upgrade_to_v4}


def initialize(connection: sqlite3.Connection) -> int:
    """Create or upgrade the schema and return the schema version."""
    with transaction(connection):
        connection.executescript(SCHEMA)
        row = connection.execute(
            "SELECT value FROM schema_meta WHERE key = 'schema_version'"
        ).fetchone()
        if row is None:
            for target in sorted(MIGRATIONS):
                MIGRATIONS[target](connection)
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
            for target in sorted(MIGRATIONS):
                if target > version:
                    MIGRATIONS[target](connection)
            if version < SCHEMA_VERSION:
                connection.execute(
                    "UPDATE schema_meta SET value = ? WHERE key = 'schema_version'",
                    (str(SCHEMA_VERSION),),
                )
                connection.execute(
                    "INSERT OR REPLACE INTO schema_meta (key, value) VALUES "
                    "('upgraded_at', ?)",
                    (now_utc(),),
                )
                version = SCHEMA_VERSION
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
    """Upsert the per-day row. `changed` is sticky so re-running a date keeps it.

    `status` is Rowan's own source semantics and is never overwritten by
    anything downstream. `display_status` is left alone here: logical-repeat
    detection owns it, and a re-ingest must not silently discard a decision it
    already made for this date.
    """
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


def set_display_status(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    submission_id: int,
    display_status: str | None,
    mark_changed: bool = False,
) -> None:
    """Record what the digest should show, leaving Rowan's own `status` intact.

    `mark_changed` exists for one case: a repost that *is* a logical repeat but
    was substantively revised. Rowan gave it a fresh SubmissionId, so the normal
    version-diff sees a first sighting and no UPDATED badge -- yet relative to
    what the reader was actually sent, it did change. Like the flag it sets,
    this is sticky and never cleared here.
    """
    connection.execute(
        "UPDATE daily_records SET display_status = ?, "
        "changed = MAX(changed, ?) "
        "WHERE target_date = ? AND submission_id = ?",
        (display_status, int(bool(mark_changed)), target_date, submission_id),
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
                -- What the digest shows. Rowan's own classification stays
                -- available as `source_status`; only logical-repeat detection
                -- can make these differ.
                COALESCE(d.display_status, d.status) AS status,
                d.status AS source_status,
                d.display_status,
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
                 ) AS previous_deliveries,
                rm.matched_submission_id AS repeat_of_submission_id,
                rm.family_key            AS repeat_family_key,
                rm.confidence            AS repeat_confidence,
                rm.method                AS repeat_method,
                rm.materially_changed    AS repeat_materially_changed
            FROM daily_records d
            JOIN announcement_versions v ON v.version_id = d.version_id
            JOIN announcements a ON a.submission_id = d.submission_id
            LEFT JOIN categories c ON c.category_id = v.category_id
            LEFT JOIN repeat_matches rm ON rm.submission_id = d.submission_id
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
          SUM(CASE WHEN COALESCE(display_status, status) = 'New' THEN 1 ELSE 0 END)
            AS new_count,
          SUM(CASE WHEN COALESCE(display_status, status) = 'Standing' THEN 1 ELSE 0 END)
            AS standing_count,
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


def _publish_status_snapshot(connection: sqlite3.Connection) -> None:
    """Publish the operational status snapshot after a committed transition.

    Imported lazily because `status_snapshot` reads back through this module.
    Never raises: see `status_snapshot.write_after_commit` for why an
    observability failure is not allowed to move business state.
    """
    from . import status_snapshot

    status_snapshot.write_after_commit(connection)


def start_run(
    connection: sqlite3.Connection, target_date: str, trigger: str = "manual"
) -> int:
    with transaction(connection):
        cursor = connection.execute(
            "INSERT INTO runs (target_date, started_at, status, trigger) "
            "VALUES (?, ?, 'running', ?)",
            (target_date, now_utc(), trigger),
        )
        run_id = int(cursor.lastrowid)
    # Outside the transaction, so the file can only ever describe a row the
    # database has already committed. `status_snapshot.py` explains why an
    # observer reads this instead of taking a lock on a WAL database it cannot
    # create a `-shm` for.
    _publish_status_snapshot(connection)
    return run_id


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
        "parking_stats",
        "calendar_stats",
    }
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates["completed_at"] = now_utc()
    assignments = ", ".join(f"{k} = ?" for k in updates)
    with transaction(connection):
        connection.execute(
            f"UPDATE runs SET {assignments} WHERE run_id = ?",
            (*updates.values(), run_id),
        )
    _publish_status_snapshot(connection)


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
        delivery_id = int(cursor.lastrowid)
    # Delivery state is the half of DailyMail's status that `runs` alone cannot
    # answer -- sent, held, skipped as a duplicate, failed at SMTP -- so it gets
    # its own publish, again only after the row is committed.
    _publish_status_snapshot(connection)
    return delivery_id


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
        "parking_locations": scalar("SELECT COUNT(*) FROM parking_locations"),
        "parking_aliases": scalar("SELECT COUNT(*) FROM parking_aliases"),
        "parking_sources": scalar("SELECT COUNT(*) FROM parking_sources"),
        "parking_unresolved": scalar("SELECT COUNT(*) FROM parking_unresolved"),
        "parking_landmarks": scalar("SELECT COUNT(*) FROM parking_landmarks"),
        "event_venues": scalar("SELECT COUNT(*) FROM event_venues"),
        "calendar_recommendations": scalar(
            "SELECT COUNT(*) FROM calendar_recommendations"
        ),
        "calendar_actions_offered": scalar(
            "SELECT COUNT(*) FROM calendar_recommendations WHERE offer_calendar = 1"
        ),
        "repeat_matches": scalar("SELECT COUNT(*) FROM repeat_matches"),
        "logical_families": scalar(
            "SELECT COUNT(*) FROM logical_announcement_families"
        ),
        "logical_family_members": scalar(
            "SELECT COUNT(*) FROM logical_announcement_members"
        ),
        "display_status_corrections": scalar(
            "SELECT COUNT(*) FROM display_status_corrections"
        ),
        # Rowan's own `ExtraEdition` flag, counted so the open question from
        # Phase 0 U3 is answerable from the status document instead of from a
        # hand-written query. It has been zero since collection began; see
        # `docs/extra-editions.md` for what that does and does not mean.
        "extra_editions": scalar(
            "SELECT COUNT(*) FROM announcement_versions WHERE extra_edition = 1"
        ),
    }


# --- calendar, venues and logical repeats ------------------------------------
#
# All three are derived data. Nothing here can modify an announcement, a
# version, a distribution date or a delivery, and every write is idempotent so
# re-running a date produces the same rows rather than a growing pile.

_CALENDAR_COLUMNS = (
    "target_date",
    "submission_id",
    "version_id",
    "is_event_candidate",
    "candidate_evidence",
    "offer_calendar",
    "relevance_score",
    "relevance_reason",
    "relevance_method",
    "relevance_model",
    "attendance_mode",
    "calendar_title",
    "event_date",
    "start_datetime",
    "end_datetime",
    "timezone",
    "location",
    "venue_id",
    "travel_required",
    "travel_mode",
    "travel_minutes_before",
    "travel_minutes_after",
    "travel_estimated",
    "mechanism",
    "action_url",
    "ics_filename",
    "ics_bytes",
    "validation_status",
    "withheld_reason",
    "session_count",
    "sessions",
    "created_at",
)


def save_calendar_recommendations(
    connection: sqlite3.Connection, target_date: str, records: list[dict]
) -> None:
    """Replace the day's calendar decisions in one transaction.

    Idempotent by construction: the same announcement content on the same date
    produces the same rows, so a re-run is a no-op rather than a duplicate.
    """
    stamp = now_utc()
    with transaction(connection):
        connection.execute(
            "DELETE FROM calendar_recommendations WHERE target_date = ?",
            (target_date,),
        )
        if not records:
            return
        placeholders = ", ".join("?" for _ in _CALENDAR_COLUMNS)
        connection.executemany(
            f"INSERT INTO calendar_recommendations "
            f"({', '.join(_CALENDAR_COLUMNS)}) VALUES ({placeholders})",
            [
                tuple(
                    stamp
                    if column == "created_at"
                    else (target_date if column == "target_date" else record.get(column))
                    for column in _CALENDAR_COLUMNS
                )
                for record in records
            ],
        )


def calendar_recommendations(
    connection: sqlite3.Connection, target_date: str
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM calendar_recommendations WHERE target_date = ? "
            "ORDER BY submission_id",
            (target_date,),
        )
    )


def venue_by_normalized(
    connection: sqlite3.Connection, normalized_venue: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM event_venues WHERE normalized_venue = ?",
        (normalized_venue,),
    ).fetchone()


def upsert_venue(connection: sqlite3.Connection, record: dict) -> int:
    """Insert or refresh one venue. Returns its `venue_id`.

    Travel numbers are only overwritten when the caller actually computed new
    ones, so a routing outage can never downgrade a good cached estimate.
    """
    stamp = now_utc()
    normalized = record["normalized_venue"]
    existing = venue_by_normalized(connection, normalized)
    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO event_venues (normalized_venue, display_name, campus,
                latitude, longitude, source, source_detail, travel_mode,
                outbound_minutes, return_minutes, distance_metres, routing_source,
                routing_fingerprint, routing_estimated, first_seen_at,
                last_verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized,
                record["display_name"],
                record.get("campus"),
                record.get("latitude"),
                record.get("longitude"),
                record["source"],
                record.get("source_detail"),
                record.get("travel_mode"),
                record.get("outbound_minutes"),
                record.get("return_minutes"),
                record.get("distance_metres"),
                record.get("routing_source"),
                record.get("routing_fingerprint"),
                int(bool(record.get("routing_estimated"))),
                stamp,
                stamp,
            ),
        )
        return int(cursor.lastrowid)

    has_travel = record.get("outbound_minutes") is not None
    connection.execute(
        """
        UPDATE event_venues SET
            display_name        = COALESCE(?, display_name),
            campus              = COALESCE(?, campus),
            latitude            = COALESCE(?, latitude),
            longitude           = COALESCE(?, longitude),
            source              = COALESCE(?, source),
            source_detail       = COALESCE(?, source_detail),
            travel_mode         = CASE WHEN ? THEN ? ELSE travel_mode END,
            outbound_minutes    = CASE WHEN ? THEN ? ELSE outbound_minutes END,
            return_minutes      = CASE WHEN ? THEN ? ELSE return_minutes END,
            distance_metres     = COALESCE(?, distance_metres),
            routing_source      = CASE WHEN ? THEN ? ELSE routing_source END,
            routing_fingerprint = CASE WHEN ? THEN ? ELSE routing_fingerprint END,
            routing_estimated   = CASE WHEN ? THEN ? ELSE routing_estimated END,
            last_verified_at    = ?
        WHERE normalized_venue = ?
        """,
        (
            record.get("display_name"),
            record.get("campus"),
            record.get("latitude"),
            record.get("longitude"),
            record.get("source"),
            record.get("source_detail"),
            has_travel, record.get("travel_mode"),
            has_travel, record.get("outbound_minutes"),
            has_travel, record.get("return_minutes"),
            record.get("distance_metres"),
            has_travel, record.get("routing_source"),
            has_travel, record.get("routing_fingerprint"),
            has_travel, int(bool(record.get("routing_estimated"))),
            stamp,
            normalized,
        ),
    )
    return int(existing["venue_id"])


def record_repeat_match(connection: sqlite3.Connection, record: dict) -> None:
    """Persist one logical-repeat determination, keeping its first-detected time."""
    connection.execute(
        """
        INSERT INTO repeat_matches (submission_id, matched_submission_id,
            family_key, method, confidence, body_similarity, materially_changed,
            evidence, first_detected_at, family_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(submission_id) DO UPDATE SET
            matched_submission_id = excluded.matched_submission_id,
            family_key            = excluded.family_key,
            method                = excluded.method,
            confidence            = excluded.confidence,
            body_similarity       = excluded.body_similarity,
            materially_changed    = excluded.materially_changed,
            evidence              = excluded.evidence,
            family_id             = COALESCE(excluded.family_id,
                                             repeat_matches.family_id)
        """,
        (
            int(record["submission_id"]),
            int(record["matched_submission_id"]),
            record["family_key"],
            record["method"],
            float(record["confidence"]),
            record.get("body_similarity"),
            int(bool(record.get("materially_changed"))),
            record.get("evidence"),
            now_utc(),
            record.get("family_id"),
        ),
    )


def repeat_match(
    connection: sqlite3.Connection, submission_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM repeat_matches WHERE submission_id = ?", (int(submission_id),)
    ).fetchone()


def delivered_history(
    connection: sqlite3.Connection, before_date: str
) -> list[sqlite3.Row]:
    """Every announcement actually delivered to the reader before `before_date`.

    Deliberately keyed on a *successful delivery*, not merely on having been
    collected: "the reader has already seen this" is the only claim that
    justifies demoting something to Standing.

    **Bodies are deliberately not selected here.** This is the index pass. The
    earlier version returned `body_text` and `full_body` for the whole delivered
    corpus and sorted the result, which made SQLite materialize every delivered
    announcement -- 5.9 MB after twelve days, and growing without bound -- into a
    temp b-tree on every run. On 1 September 2026 that spill landed on a
    filesystem that had been remounted read-only, `SQLITE_CANTOPEN` propagated as
    "unable to open database file", and the whole logical-repeat stage was
    skipped. `repeats.find_repeats` now shortlists by title first and asks for
    the two or three bodies it actually needs via `announcement_bodies`.
    """
    return list(
        connection.execute(
            """
            SELECT
                a.submission_id,
                seen.first_delivered_date,
                seen.last_delivered_date,
                seen.delivered_days,
                v.title,
                v.category_id,
                v.source_audience,
                v.is_event,
                v.event_date,
                v.event_start_time,
                v.event_location,
                v.contact_email,
                v.submitted_by_email,
                v.content_hash
            FROM (
                SELECT d.submission_id,
                       MIN(d.target_date) AS first_delivered_date,
                       MAX(d.target_date) AS last_delivered_date,
                       COUNT(*)           AS delivered_days
                  FROM daily_records d
                 WHERE d.target_date < ?
                   AND EXISTS (SELECT 1 FROM deliveries del
                                WHERE del.target_date = d.target_date
                                  AND del.state = 'sent')
                 GROUP BY d.submission_id
            ) AS seen
            JOIN announcements a ON a.submission_id = seen.submission_id
            JOIN announcement_versions v ON v.version_id = a.current_version_id
            """,
            (before_date,),
        )
    )


def announcement_bodies(
    connection: sqlite3.Connection, submission_ids
) -> dict[int, sqlite3.Row]:
    """The comparison payload for a shortlist of announcements, keyed by id.

    Split out from `delivered_history` so the expensive columns are read for the
    handful of candidates that share a normalized title with something in today's
    digest, rather than for the entire delivered corpus.
    """
    ids = sorted({int(value) for value in submission_ids})
    if not ids:
        return {}
    out: dict[int, sqlite3.Row] = {}
    # Chunked so a large family can never approach SQLITE_MAX_VARIABLE_NUMBER.
    for offset in range(0, len(ids), 400):
        chunk = ids[offset : offset + 400]
        placeholders = ",".join("?" * len(chunk))
        for row in connection.execute(
            f"""
            SELECT a.submission_id, v.title, v.body_text, v.full_body,
                   v.category_id, v.source_audience, v.is_event, v.event_date,
                   v.event_start_time, v.event_location, v.contact_email,
                   v.submitted_by_email, v.content_hash
              FROM announcements a
              JOIN announcement_versions v ON v.version_id = a.current_version_id
             WHERE a.submission_id IN ({placeholders})
            """,
            chunk,
        ):
            out[int(row["submission_id"])] = row
    return out


# --- durable logical families ------------------------------------------------


def family_by_key(
    connection: sqlite3.Connection, family_key: str
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM logical_announcement_families WHERE family_key = ?",
        (family_key,),
    ).fetchone()


def family_members(connection: sqlite3.Connection, family_id: int) -> list[sqlite3.Row]:
    """Members oldest first, so the canonical original leads the list."""
    return list(
        connection.execute(
            """
            SELECT m.*, a.first_distribution_date, v.title
              FROM logical_announcement_members m
              JOIN announcements a ON a.submission_id = m.submission_id
              LEFT JOIN announcement_versions v ON v.version_id = a.current_version_id
             WHERE m.family_id = ?
             ORDER BY m.submission_id
            """,
            (int(family_id),),
        )
    )


def family_member_index(
    connection: sqlite3.Connection, family_key: str
) -> list[sqlite3.Row]:
    """Index rows for every member of one family, in the shape of `delivered_history`.

    This is what removes the moving-window limitation: a candidate reaches the
    comparison because it is a known member of the same logical family, not
    because it happens to have been delivered recently. Whether it may then
    *justify* a demotion is still decided by `repeats.compare`, and a member the
    reader was never sent still carries no delivery dates.
    """
    return list(
        connection.execute(
            """
            SELECT
                a.submission_id,
                (SELECT MIN(d.target_date) FROM daily_records d
                  WHERE d.submission_id = a.submission_id
                    AND EXISTS (SELECT 1 FROM deliveries del
                                 WHERE del.target_date = d.target_date
                                   AND del.state = 'sent')) AS first_delivered_date,
                (SELECT MAX(d.target_date) FROM daily_records d
                  WHERE d.submission_id = a.submission_id
                    AND EXISTS (SELECT 1 FROM deliveries del
                                 WHERE del.target_date = d.target_date
                                   AND del.state = 'sent')) AS last_delivered_date,
                v.title,
                v.category_id,
                v.source_audience,
                v.is_event,
                v.event_date,
                v.event_start_time,
                v.event_location,
                v.contact_email,
                v.submitted_by_email,
                v.content_hash,
                f.family_id,
                m.is_canonical
              FROM logical_announcement_families f
              JOIN logical_announcement_members m ON m.family_id = f.family_id
              JOIN announcements a ON a.submission_id = m.submission_id
              JOIN announcement_versions v ON v.version_id = a.current_version_id
             WHERE f.family_key = ?
             ORDER BY a.submission_id
            """,
            (family_key,),
        )
    )


def family_for_submission(
    connection: sqlite3.Connection, submission_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        """
        SELECT f.* FROM logical_announcement_families f
          JOIN logical_announcement_members m ON m.family_id = f.family_id
         WHERE m.submission_id = ?
        """,
        (int(submission_id),),
    ).fetchone()


def upsert_family(
    connection: sqlite3.Connection,
    *,
    family_key: str,
    canonical_submission_id: int,
    normalized_title: str,
    category_id=None,
    source_audience: str | None = None,
    confidence: float | None = None,
    match_method: str | None = None,
) -> int:
    """Create or refresh one family, returning its id. Idempotent.

    The canonical member is the *lowest* SubmissionId ever seen in the family --
    Rowan allocates them monotonically, so that is the original posting. It only
    ever moves earlier, never later, so re-running a day cannot rewrite history.
    """
    stamp = now_utc()
    existing = family_by_key(connection, family_key)
    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO logical_announcement_families
                (family_key, canonical_submission_id, normalized_title,
                 category_id, source_audience, member_count, confidence,
                 match_method, created_at, last_seen_at)
            VALUES (?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                family_key, int(canonical_submission_id), normalized_title,
                category_id, source_audience, confidence, match_method,
                stamp, stamp,
            ),
        )
        return int(cursor.lastrowid)

    connection.execute(
        """
        UPDATE logical_announcement_families SET
            canonical_submission_id = MIN(canonical_submission_id, ?),
            normalized_title        = COALESCE(?, normalized_title),
            category_id             = COALESCE(?, category_id),
            source_audience         = COALESCE(?, source_audience),
            confidence              = MAX(COALESCE(confidence, 0), COALESCE(?, 0)),
            match_method            = COALESCE(?, match_method),
            last_seen_at            = ?
        WHERE family_id = ?
        """,
        (
            int(canonical_submission_id), normalized_title, category_id,
            source_audience, confidence, match_method, stamp,
            int(existing["family_id"]),
        ),
    )
    return int(existing["family_id"])


def add_family_member(
    connection: sqlite3.Connection,
    *,
    family_id: int,
    submission_id: int,
    matched_submission_id: int | None = None,
    content_hash: str | None = None,
    match_confidence: float | None = None,
    match_method: str | None = None,
) -> None:
    """Record membership, keeping the original `first_seen`. Idempotent."""
    stamp = now_utc()
    connection.execute(
        """
        INSERT INTO logical_announcement_members
            (family_id, submission_id, matched_submission_id, content_hash,
             match_confidence, match_method, is_canonical, first_seen, last_seen)
        VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)
        ON CONFLICT(family_id, submission_id) DO UPDATE SET
            matched_submission_id = COALESCE(excluded.matched_submission_id,
                                             logical_announcement_members
                                               .matched_submission_id),
            content_hash     = COALESCE(excluded.content_hash,
                                        logical_announcement_members.content_hash),
            match_confidence = MAX(
                COALESCE(logical_announcement_members.match_confidence, 0),
                COALESCE(excluded.match_confidence, 0)),
            match_method     = COALESCE(excluded.match_method,
                                        logical_announcement_members.match_method),
            last_seen        = excluded.last_seen
        """,
        (
            int(family_id), int(submission_id),
            None if matched_submission_id is None else int(matched_submission_id),
            content_hash, match_confidence, match_method, stamp, stamp,
        ),
    )
    _resync_family(connection, int(family_id))


def _resync_family(connection: sqlite3.Connection, family_id: int) -> None:
    """Keep `member_count` and the canonical flag true to the member rows."""
    connection.execute(
        """
        UPDATE logical_announcement_families
           SET member_count = (SELECT COUNT(*) FROM logical_announcement_members
                                WHERE family_id = ?),
               canonical_submission_id = COALESCE(
                   (SELECT MIN(submission_id) FROM logical_announcement_members
                     WHERE family_id = ?),
                   canonical_submission_id)
         WHERE family_id = ?
        """,
        (family_id, family_id, family_id),
    )
    connection.execute(
        """
        UPDATE logical_announcement_members
           SET is_canonical = (
               submission_id = (SELECT canonical_submission_id
                                  FROM logical_announcement_families
                                 WHERE family_id = ?))
         WHERE family_id = ?
        """,
        (family_id, family_id),
    )


def backfill_families_from_repeat_matches(connection: sqlite3.Connection) -> int:
    """Turn the pairwise findings already recorded into durable families.

    Runs inside the v4 migration and is safe to run again at any time: every
    write is an idempotent upsert. Returns the number of families touched.
    """
    rows = list(
        connection.execute(
            """
            SELECT r.submission_id, r.matched_submission_id, r.family_key,
                   r.method, r.confidence,
                   cur.content_hash AS current_hash,
                   prior.content_hash AS prior_hash,
                   curv.category_id, curv.source_audience
              FROM repeat_matches r
              LEFT JOIN announcements a   ON a.submission_id = r.submission_id
              LEFT JOIN announcement_versions cur
                     ON cur.version_id = a.current_version_id
              LEFT JOIN announcement_versions curv
                     ON curv.version_id = a.current_version_id
              LEFT JOIN announcements pa  ON pa.submission_id = r.matched_submission_id
              LEFT JOIN announcement_versions prior
                     ON prior.version_id = pa.current_version_id
            """
        )
    )
    touched: set[int] = set()
    for row in rows:
        # `family_key` is `normalized title|category_id|audience`; the title part
        # is everything before the last two separators.
        parts = str(row["family_key"]).rsplit("|", 2)
        normalized_title = parts[0] if parts else str(row["family_key"])
        family_id = upsert_family(
            connection,
            family_key=row["family_key"],
            canonical_submission_id=min(
                int(row["submission_id"]), int(row["matched_submission_id"])
            ),
            normalized_title=normalized_title,
            category_id=row["category_id"],
            source_audience=row["source_audience"],
            confidence=row["confidence"],
            match_method=row["method"],
        )
        add_family_member(
            connection, family_id=family_id,
            submission_id=int(row["matched_submission_id"]),
            content_hash=row["prior_hash"],
            match_confidence=row["confidence"], match_method=row["method"],
        )
        add_family_member(
            connection, family_id=family_id,
            submission_id=int(row["submission_id"]),
            matched_submission_id=int(row["matched_submission_id"]),
            content_hash=row["current_hash"],
            match_confidence=row["confidence"], match_method=row["method"],
        )
        connection.execute(
            "UPDATE repeat_matches SET family_id = ? WHERE submission_id = ?",
            (family_id, int(row["submission_id"])),
        )
        touched.add(family_id)
    return len(touched)


# --- display-status corrections ----------------------------------------------


def record_display_status_correction(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    submission_id: int,
    previous_display_status: str | None,
    new_display_status: str | None,
    reason: str,
) -> None:
    """Append-only audit of a retrospective change to what the digest shows."""
    connection.execute(
        """
        INSERT INTO display_status_corrections
            (target_date, submission_id, previous_display_status,
             new_display_status, reason, corrected_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            target_date, int(submission_id), previous_display_status,
            new_display_status, reason, now_utc(),
        ),
    )


def display_status_corrections(
    connection: sqlite3.Connection, target_date: str | None = None
) -> list[sqlite3.Row]:
    if target_date is None:
        return list(
            connection.execute(
                "SELECT * FROM display_status_corrections ORDER BY correction_id"
            )
        )
    return list(
        connection.execute(
            "SELECT * FROM display_status_corrections WHERE target_date = ? "
            "ORDER BY correction_id",
            (target_date,),
        )
    )


def daily_record(
    connection: sqlite3.Connection, target_date: str, submission_id: int
) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM daily_records WHERE target_date = ? AND submission_id = ?",
        (target_date, int(submission_id)),
    ).fetchone()


def dates_with_record(
    connection: sqlite3.Connection, submission_id: int
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT target_date, status, display_status, changed FROM daily_records "
            "WHERE submission_id = ? ORDER BY target_date",
            (int(submission_id),),
        )
    )
