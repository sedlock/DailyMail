"""Persistence for the parking reference cache.

The cache is the whole point of the feature: once DailyMail knows where Lot O-1
is, the daily run answers from SQLite and costs nothing. Everything expensive --
fetching an authoritative source, asking a model to phrase a description,
researching an unknown lot -- happens here only on an explicit refresh or a real
cache miss.

Two rules are enforced at this layer rather than left to callers:

* **A manual override is never overwritten.** `override_fields` records exactly
  which columns a human set; an automated refresh skips those columns and leaves
  everything else current.
* **An alias is unique per campus.** Two lots on one campus may not claim the
  same alias, so a within-campus lookup is always unambiguous. Collisions
  *across* campuses are allowed and are what campus disambiguation exists for.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from . import parking
from .db import now_utc, transaction

# Columns an automated refresh is allowed to change, and which a manual override
# can pin. `canonical_id`, `campus` and timestamps are identity/bookkeeping and
# are never listed.
OVERRIDABLE_FIELDS = (
    "canonical_name",
    "location_type",
    "permit_class",
    "description",
    "latitude",
    "longitude",
    "confidence",
    "is_active",
)


@dataclass
class LocationRecord:
    """One parking facility as an authoritative source describes it."""

    canonical_id: str
    campus: str
    canonical_name: str
    location_type: str
    permit_class: str = "Unknown"
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    source_id: str | None = None
    source_type: str | None = None
    source_url: str | None = None
    source_map_id: str | None = None
    source_fingerprint: str | None = None
    provenance: str | None = None
    confidence: str = "low"
    is_active: bool = True
    aliases: list[str] = field(default_factory=list)

    def validated(self) -> "LocationRecord":
        if self.location_type not in parking.LOCATION_TYPES:
            raise parking.ParkingDataError(
                f"unknown location_type {self.location_type!r}"
            )
        if self.permit_class not in parking.PERMIT_CLASSES:
            raise parking.ParkingDataError(
                f"unknown permit_class {self.permit_class!r}"
            )
        if self.campus not in parking.CAMPUSES:
            raise parking.ParkingDataError(f"unknown campus {self.campus!r}")
        if self.confidence not in parking.CONFIDENCE_ORDER:
            raise parking.ParkingDataError(f"unknown confidence {self.confidence!r}")
        if self.latitude is not None or self.longitude is not None:
            parking.validate_coordinates(self.latitude, self.longitude)
        return self


# --- sources -----------------------------------------------------------------


def upsert_source(
    connection: sqlite3.Connection,
    *,
    source_id: str,
    campus: str,
    source_type: str,
    source_url: str,
    source_map_id: str | None = None,
    machine_readable: bool = False,
    fingerprint: str | None = None,
    source_version: str | None = None,
    status: str = "ok",
    error: str | None = None,
    locations_seen: int | None = None,
    stamp: str | None = None,
) -> bool:
    """Record a source check. Returns True when the fingerprint actually changed.

    `last_verified_at` moves on every successful check; `last_changed_at` moves
    only when the bytes differ. That distinction is what lets a refresh skip
    regenerating unchanged reference records.
    """
    when = stamp or now_utc()
    existing = connection.execute(
        "SELECT fingerprint FROM parking_sources WHERE source_id = ?", (source_id,)
    ).fetchone()
    previous = existing["fingerprint"] if existing else None
    changed = fingerprint is not None and fingerprint != previous

    connection.execute(
        """
        INSERT INTO parking_sources (source_id, campus, source_type, source_url,
            source_map_id, machine_readable, fingerprint, source_version,
            last_retrieved_at, last_verified_at, last_changed_at, last_status,
            last_error, locations_seen)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(source_id) DO UPDATE SET
            campus            = excluded.campus,
            source_type       = excluded.source_type,
            source_url        = excluded.source_url,
            source_map_id     = excluded.source_map_id,
            machine_readable  = excluded.machine_readable,
            fingerprint       = COALESCE(excluded.fingerprint,
                                         parking_sources.fingerprint),
            source_version    = COALESCE(excluded.source_version,
                                         parking_sources.source_version),
            last_retrieved_at = excluded.last_retrieved_at,
            last_verified_at  = excluded.last_verified_at,
            last_changed_at   = CASE WHEN ? THEN excluded.last_changed_at
                                     ELSE parking_sources.last_changed_at END,
            last_status       = excluded.last_status,
            last_error        = excluded.last_error,
            locations_seen    = COALESCE(excluded.locations_seen,
                                         parking_sources.locations_seen)
        """,
        (
            source_id, campus, source_type, source_url, source_map_id,
            int(bool(machine_readable)), fingerprint, source_version,
            when, when, when, status, error, locations_seen,
            1 if changed else 0,
        ),
    )
    return changed


def source_row(connection: sqlite3.Connection, source_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM parking_sources WHERE source_id = ?", (source_id,)
    ).fetchone()


def source_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute("SELECT * FROM parking_sources ORDER BY campus, source_id")
    )


def stale_sources(
    connection: sqlite3.Connection, *, max_age_days: int, campus: str | None = None
) -> list[str]:
    """Source ids never verified, or last verified longer ago than the policy."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, max_age_days))
    ).isoformat(timespec="seconds")
    sql = "SELECT source_id, last_verified_at, campus FROM parking_sources"
    rows = list(connection.execute(sql))
    stale = []
    for row in rows:
        if campus and row["campus"] != campus:
            continue
        if not row["last_verified_at"] or row["last_verified_at"] < cutoff:
            stale.append(row["source_id"])
    return stale


# --- locations ---------------------------------------------------------------

_LOCATION_SELECT = "SELECT * FROM parking_locations"


def upsert_location(
    connection: sqlite3.Connection, record: LocationRecord, *, stamp: str | None = None
) -> tuple[int, str]:
    """Insert or refresh one location. Returns `(location_id, outcome)`.

    `outcome` is `inserted`, `updated`, `unchanged` or `override_preserved`, so a
    refresh can report exactly what it did without diffing the table afterwards.
    """
    record.validated()
    when = stamp or now_utc()
    normalized = parking.normalize_name(record.canonical_name)

    existing = connection.execute(
        _LOCATION_SELECT + " WHERE canonical_id = ?", (record.canonical_id,)
    ).fetchone()
    if existing is None:
        existing = connection.execute(
            _LOCATION_SELECT + " WHERE campus = ? AND normalized_name = ?",
            (record.campus, normalized),
        ).fetchone()

    if existing is None:
        cursor = connection.execute(
            """
            INSERT INTO parking_locations (canonical_id, campus, canonical_name,
                normalized_name, location_type, permit_class, description,
                latitude, longitude, source_id, source_type, source_url,
                source_map_id, source_fingerprint, provenance, confidence,
                is_active, manual_override, override_fields,
                first_discovered_at, last_verified_at, last_changed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL,
                    ?, ?, ?)
            """,
            (
                record.canonical_id, record.campus, record.canonical_name,
                normalized, record.location_type, record.permit_class,
                record.description, record.latitude, record.longitude,
                record.source_id, record.source_type, record.source_url,
                record.source_map_id, record.source_fingerprint,
                record.provenance, record.confidence, int(bool(record.is_active)),
                when, when, when,
            ),
        )
        location_id = int(cursor.lastrowid)
        _write_aliases(connection, location_id, record, when)
        return location_id, "inserted"

    location_id = int(existing["location_id"])
    protected = set(json.loads(existing["override_fields"] or "[]"))
    proposed = {
        "canonical_name": record.canonical_name,
        "location_type": record.location_type,
        "permit_class": record.permit_class,
        "latitude": record.latitude,
        "longitude": record.longitude,
        "confidence": record.confidence,
        "is_active": int(bool(record.is_active)),
    }
    # A description already in the cache is never replaced by a source refresh:
    # generating one is an agent call, and a good sentence should survive.
    if record.description and not existing["description"]:
        proposed["description"] = record.description

    updates = {
        column: value
        for column, value in proposed.items()
        if column not in protected and existing[column] != value
    }
    skipped = sorted(
        column for column in proposed if column in protected
        and existing[column] != proposed[column]
    )

    # Provenance always tracks the source we last saw, even when values are
    # pinned, so an operator can see where the automated answer came from.
    bookkeeping = {
        "normalized_name": normalized,
        "source_id": record.source_id,
        "source_type": record.source_type,
        "source_url": record.source_url,
        "source_map_id": record.source_map_id,
        "source_fingerprint": record.source_fingerprint,
        "provenance": record.provenance,
        "last_verified_at": when,
    }
    if updates:
        bookkeeping["last_changed_at"] = when

    assignments = {**bookkeeping, **updates}
    connection.execute(
        "UPDATE parking_locations SET "
        + ", ".join(f"{column} = ?" for column in assignments)
        + " WHERE location_id = ?",
        (*assignments.values(), location_id),
    )
    _write_aliases(connection, location_id, record, when)
    if skipped:
        return location_id, "override_preserved"
    return location_id, "updated" if updates else "unchanged"


def _write_aliases(
    connection: sqlite3.Connection,
    location_id: int,
    record: LocationRecord,
    when: str,
    *,
    origin: str = "generated",
) -> None:
    """Add generated and source-supplied aliases, never removing a manual one."""
    candidates = list(parking.generate_aliases(record.canonical_name))
    for extra in record.aliases:
        candidates.extend(parking.generate_aliases(extra))
    add_aliases(connection, location_id, record.campus, candidates, when, origin=origin)


def add_aliases(
    connection: sqlite3.Connection,
    location_id: int,
    campus: str,
    aliases: list[str],
    when: str | None = None,
    *,
    origin: str = "generated",
) -> int:
    """Insert aliases, ignoring any that another lot on this campus already owns."""
    stamp = when or now_utc()
    added = 0
    for alias in aliases:
        normalized = parking.normalize_name(alias)
        if not normalized:
            continue
        cursor = connection.execute(
            """
            INSERT INTO parking_aliases (location_id, campus, alias,
                normalized_alias, scannable, origin, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campus, normalized_alias) DO NOTHING
            """,
            (
                location_id, campus, " ".join(alias.split()), normalized,
                int(parking.scannable_alias(alias)), origin, stamp,
            ),
        )
        added += cursor.rowcount if cursor.rowcount > 0 else 0
    return added


def set_manual_override(
    connection: sqlite3.Connection,
    canonical_id: str,
    fields: dict,
    *,
    stamp: str | None = None,
) -> sqlite3.Row:
    """Pin operator-supplied values so no refresh can undo them."""
    when = stamp or now_utc()
    row = connection.execute(
        _LOCATION_SELECT + " WHERE canonical_id = ?", (canonical_id,)
    ).fetchone()
    if row is None:
        raise parking.ParkingDataError(f"no parking location {canonical_id!r}")

    unknown = sorted(set(fields) - set(OVERRIDABLE_FIELDS))
    if unknown:
        raise parking.ParkingDataError(
            f"cannot override {unknown}; overridable fields are "
            f"{list(OVERRIDABLE_FIELDS)}"
        )
    if "latitude" in fields or "longitude" in fields:
        latitude = fields.get("latitude", row["latitude"])
        longitude = fields.get("longitude", row["longitude"])
        parking.validate_coordinates(latitude, longitude)

    protected = set(json.loads(row["override_fields"] or "[]")) | set(fields)
    values = dict(fields)
    if "is_active" in values:
        values["is_active"] = int(bool(values["is_active"]))
    values["override_fields"] = json.dumps(sorted(protected))
    values["manual_override"] = 1
    values["last_changed_at"] = when
    values["last_verified_at"] = when

    with transaction(connection):
        connection.execute(
            "UPDATE parking_locations SET "
            + ", ".join(f"{column} = ?" for column in values)
            + " WHERE canonical_id = ?",
            (*values.values(), canonical_id),
        )
    return connection.execute(
        _LOCATION_SELECT + " WHERE canonical_id = ?", (canonical_id,)
    ).fetchone()


def clear_manual_override(
    connection: sqlite3.Connection, canonical_id: str
) -> sqlite3.Row:
    with transaction(connection):
        connection.execute(
            "UPDATE parking_locations SET manual_override = 0, override_fields = NULL, "
            "last_changed_at = ? WHERE canonical_id = ?",
            (now_utc(), canonical_id),
        )
    return connection.execute(
        _LOCATION_SELECT + " WHERE canonical_id = ?", (canonical_id,)
    ).fetchone()


# --- lookups (the daily hot path) --------------------------------------------


def alias_index(connection: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    """normalized alias -> every location claiming it, across all campuses.

    One query per run. A list, not a row: a shared alias across campuses is the
    ambiguity the resolver must not guess through.
    """
    index: dict[str, list[sqlite3.Row]] = {}
    for row in connection.execute(
        """
        SELECT a.normalized_alias, a.alias, a.scannable, l.*
        FROM parking_aliases a
        JOIN parking_locations l ON l.location_id = a.location_id
        WHERE l.is_active = 1
        ORDER BY a.normalized_alias, l.campus, l.canonical_name
        """
    ):
        index.setdefault(row["normalized_alias"], []).append(row)
    return index


def scannable_aliases(index: dict[str, list[sqlite3.Row]]) -> dict[str, str]:
    """The subset of aliases distinctive enough to sweep across free text."""
    out: dict[str, str] = {}
    for normalized, rows in index.items():
        if rows and rows[0]["scannable"]:
            out[normalized] = rows[0]["alias"]
    return out


def location_by_canonical_id(
    connection: sqlite3.Connection, canonical_id: str
) -> sqlite3.Row | None:
    return connection.execute(
        _LOCATION_SELECT + " WHERE canonical_id = ?", (canonical_id,)
    ).fetchone()


def find_locations(
    connection: sqlite3.Connection, text: str, *, campus: str | None = None
) -> list[sqlite3.Row]:
    """Manual lookup helper: match on alias, canonical id or canonical name."""
    normalized = parking.normalize_name(text)
    params: list = [normalized, normalized, text.strip().lower()]
    sql = """
        SELECT DISTINCT l.* FROM parking_locations l
        LEFT JOIN parking_aliases a ON a.location_id = l.location_id
        WHERE (a.normalized_alias = ? OR l.normalized_name = ?
               OR LOWER(l.canonical_id) = ?)
    """
    if campus:
        sql += " AND l.campus = ?"
        params.append(campus)
    sql += " ORDER BY l.campus, l.canonical_name"
    return list(connection.execute(sql, params))


def all_locations(
    connection: sqlite3.Connection, *, campus: str | None = None
) -> list[sqlite3.Row]:
    sql = _LOCATION_SELECT
    params: tuple = ()
    if campus:
        sql += " WHERE campus = ?"
        params = (campus,)
    sql += " ORDER BY campus, canonical_name"
    return list(connection.execute(sql, params))


def locations_needing_description(
    connection: sqlite3.Connection, *, campus: str | None = None
) -> list[sqlite3.Row]:
    sql = (
        _LOCATION_SELECT
        + " WHERE is_active = 1 AND (description IS NULL OR TRIM(description) = '')"
    )
    params: tuple = ()
    if campus:
        sql += " AND campus = ?"
        params = (campus,)
    sql += " ORDER BY campus, canonical_name"
    return list(connection.execute(sql, params))


def set_description(
    connection: sqlite3.Connection,
    canonical_id: str,
    description: str,
    *,
    method: str,
    model: str | None = None,
) -> None:
    """Cache an accepted description. Never touches a manually pinned one."""
    when = now_utc()
    connection.execute(
        """
        UPDATE parking_locations
           SET description = ?, description_method = ?, description_model = ?,
               last_changed_at = ?, last_verified_at = ?
         WHERE canonical_id = ?
           AND (override_fields IS NULL OR override_fields NOT LIKE '%"description"%')
        """,
        (description, method, model, when, when, canonical_id),
    )


# --- landmarks ---------------------------------------------------------------


def upsert_landmarks(
    connection: sqlite3.Connection, landmarks, *, stamp: str | None = None
) -> int:
    """Cache named campus features from the authoritative layers.

    They serve two purposes, both offline: evidence for a description, and campus
    disambiguation when an announcement names a building instead of a campus.
    """
    when = stamp or now_utc()
    written = 0
    for landmark in landmarks:
        normalized = parking.normalize_name(landmark.name)
        if not normalized:
            continue
        connection.execute(
            """
            INSERT INTO parking_landmarks (campus, name, normalized_name, category,
                latitude, longitude, source_id, last_verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(campus, normalized_name) DO UPDATE SET
                name             = excluded.name,
                category         = excluded.category,
                latitude         = excluded.latitude,
                longitude        = excluded.longitude,
                source_id        = excluded.source_id,
                last_verified_at = excluded.last_verified_at
            """,
            (
                landmark.campus, landmark.name, normalized, landmark.category,
                landmark.latitude, landmark.longitude,
                getattr(landmark, "source_id", None), when,
            ),
        )
        written += 1
    return written


def landmark_rows(
    connection: sqlite3.Connection, *, campus: str | None = None
) -> list[sqlite3.Row]:
    sql = "SELECT * FROM parking_landmarks"
    params: tuple = ()
    if campus:
        sql += " WHERE campus = ?"
        params = (campus,)
    return list(connection.execute(sql + " ORDER BY campus, name", params))


def landmark_campus_index(connection: sqlite3.Connection) -> dict[str, str]:
    """normalized landmark name -> campus, dropping names shared by two campuses.

    A name that is not unique proves nothing about campus, so it is excluded
    rather than allowed to cast a misleading vote.
    """
    index: dict[str, str] = {}
    for row in connection.execute(
        "SELECT normalized_name, campus FROM parking_landmarks"
    ):
        name = row["normalized_name"]
        if len(name) < 6:
            continue
        if index.get(name) not in (None, row["campus"]):
            index[name] = ""
            continue
        index[name] = row["campus"]
    return {name: campus for name, campus in index.items() if campus}


# --- audit trail and misses --------------------------------------------------


def record_association(
    connection: sqlite3.Connection,
    *,
    target_date: str,
    submission_id: int,
    normalized_match: str,
    matched_text: str,
    match_method: str,
    version_id: int | None = None,
    location_id: int | None = None,
    confidence: str | None = None,
    campus_hint: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO announcement_parking_locations (target_date, submission_id,
            normalized_match, version_id, location_id, matched_text, match_method,
            confidence, campus_hint, resolved_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(target_date, submission_id, normalized_match) DO UPDATE SET
            version_id   = excluded.version_id,
            location_id  = excluded.location_id,
            matched_text = excluded.matched_text,
            match_method = excluded.match_method,
            confidence   = excluded.confidence,
            campus_hint  = excluded.campus_hint,
            resolved_at  = excluded.resolved_at
        """,
        (
            target_date, int(submission_id), normalized_match, version_id,
            location_id, matched_text, match_method, confidence,
            campus_hint or "", now_utc(),
        ),
    )


def associations_for_date(
    connection: sqlite3.Connection, target_date: str
) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM announcement_parking_locations WHERE target_date = ? "
            "ORDER BY submission_id, normalized_match",
            (target_date,),
        )
    )


def record_unresolved(
    connection: sqlite3.Connection,
    *,
    normalized_match: str,
    matched_text: str,
    campus_hint: str | None,
    reason: str,
    resolver_called: bool = False,
) -> None:
    """Remember a miss so repeated misses are visible and countable."""
    when = now_utc()
    connection.execute(
        """
        INSERT INTO parking_unresolved (normalized_match, campus_hint, matched_text,
            attempts, resolver_calls, last_reason, first_seen_at, last_seen_at)
        VALUES (?, ?, ?, 1, ?, ?, ?, ?)
        ON CONFLICT(normalized_match, campus_hint) DO UPDATE SET
            attempts       = parking_unresolved.attempts + 1,
            resolver_calls = parking_unresolved.resolver_calls + ?,
            matched_text   = excluded.matched_text,
            last_reason    = excluded.last_reason,
            last_seen_at   = excluded.last_seen_at
        """,
        (
            normalized_match, campus_hint or "", matched_text,
            1 if resolver_called else 0, reason[:300], when, when,
            1 if resolver_called else 0,
        ),
    )


def clear_unresolved(connection: sqlite3.Connection, normalized_match: str) -> None:
    connection.execute(
        "DELETE FROM parking_unresolved WHERE normalized_match = ?",
        (normalized_match,),
    )


def unresolved_rows(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            "SELECT * FROM parking_unresolved ORDER BY last_seen_at DESC"
        )
    )


# --- status ------------------------------------------------------------------


def statistics(connection: sqlite3.Connection) -> dict:
    def scalar(sql: str, params: tuple = ()) -> int:
        return int(connection.execute(sql, params).fetchone()[0] or 0)

    by_campus = {
        row["campus"]: {
            "locations": row["n"],
            "with_coordinates": row["geo"],
            "with_description": row["described"],
            "manual_overrides": row["overrides"],
        }
        for row in connection.execute(
            """
            SELECT campus, COUNT(*) AS n,
                   SUM(CASE WHEN latitude IS NOT NULL THEN 1 ELSE 0 END) AS geo,
                   SUM(CASE WHEN description IS NOT NULL AND TRIM(description) <> ''
                            THEN 1 ELSE 0 END) AS described,
                   SUM(manual_override) AS overrides
              FROM parking_locations WHERE is_active = 1
             GROUP BY campus ORDER BY campus
            """
        )
    }
    aliases_by_campus = {
        row["campus"]: row["n"]
        for row in connection.execute(
            "SELECT campus, COUNT(*) AS n FROM parking_aliases GROUP BY campus"
        )
    }
    types = {
        row["location_type"]: row["n"]
        for row in connection.execute(
            "SELECT location_type, COUNT(*) AS n FROM parking_locations "
            "WHERE is_active = 1 GROUP BY location_type"
        )
    }
    permits = {
        row["permit_class"]: row["n"]
        for row in connection.execute(
            "SELECT permit_class, COUNT(*) AS n FROM parking_locations "
            "WHERE is_active = 1 GROUP BY permit_class ORDER BY permit_class"
        )
    }
    landmarks = {
        row["campus"]: row["n"]
        for row in connection.execute(
            "SELECT campus, COUNT(*) AS n FROM parking_landmarks GROUP BY campus"
        )
    }
    oldest = connection.execute(
        "SELECT canonical_id, last_verified_at FROM parking_locations "
        "WHERE is_active = 1 ORDER BY last_verified_at LIMIT 1"
    ).fetchone()
    last_refresh = connection.execute(
        "SELECT MAX(last_verified_at) FROM parking_sources"
    ).fetchone()[0]

    return {
        "locations": scalar("SELECT COUNT(*) FROM parking_locations WHERE is_active = 1"),
        "inactive": scalar("SELECT COUNT(*) FROM parking_locations WHERE is_active = 0"),
        "aliases": scalar("SELECT COUNT(*) FROM parking_aliases"),
        "with_coordinates": scalar(
            "SELECT COUNT(*) FROM parking_locations "
            "WHERE is_active = 1 AND latitude IS NOT NULL"
        ),
        "with_description": scalar(
            "SELECT COUNT(*) FROM parking_locations WHERE is_active = 1 "
            "AND description IS NOT NULL AND TRIM(description) <> ''"
        ),
        "manual_overrides": scalar(
            "SELECT COUNT(*) FROM parking_locations WHERE manual_override = 1"
        ),
        "unresolved": scalar("SELECT COUNT(*) FROM parking_unresolved"),
        "associations": scalar("SELECT COUNT(*) FROM announcement_parking_locations"),
        "by_campus": by_campus,
        "aliases_by_campus": aliases_by_campus,
        "landmarks_by_campus": landmarks,
        "landmarks": sum(landmarks.values()),
        "by_type": types,
        "by_permit": permits,
        "oldest_verification": (
            {"canonical_id": oldest["canonical_id"], "at": oldest["last_verified_at"]}
            if oldest
            else None
        ),
        "last_source_refresh": last_refresh,
        "sources": [dict(row) for row in source_rows(connection)],
    }
