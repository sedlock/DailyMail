"""Reference-data refresh: fetch authoritative sources, cache what changed.

Parking lots move on the scale of years, so this is deliberately not a daily job.
It runs on bootstrap, on a real cache miss, when a source has not been verified
for the configured staleness window, and whenever an operator asks.

The fingerprint is what makes a refresh cheap. Every source is hashed on
retrieval; when the bytes have not changed, the source's `last_verified_at` moves
forward and nothing is regenerated -- no re-upsert churn, no description
regeneration, no agent call. Descriptions are only written for facilities that do
not already have one.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from . import db, parking, parking_agent, parking_reference, parking_sources, parking_store
from .parking_sources import Source, SourceFetchError
from .settings import Settings

log = logging.getLogger("dailymail.parking")


@dataclass(frozen=True)
class _CachedLandmark:
    """A landmark read back out of SQLite, shaped like the parser's own."""

    name: str
    campus: str
    category: str
    latitude: float
    longitude: float


@dataclass
class SourceOutcome:
    source_id: str
    campus: str
    status: str                  # ok | unchanged | changed_review | error | skipped
    changed: bool = False
    locations_seen: int = 0
    inserted: int = 0
    updated: int = 0
    unchanged: int = 0
    overrides_preserved: int = 0
    landmarks: int = 0
    unnamed_parking: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)


@dataclass
class RefreshOutcome:
    sources: list[SourceOutcome] = field(default_factory=list)
    landmarks: list = field(default_factory=list)
    descriptions_written: int = 0
    descriptions_rejected: dict[str, str] = field(default_factory=dict)
    description_calls: int = 0
    description_cost_usd: float = 0.0
    description_model: str | None = None
    description_error: str | None = None

    @property
    def locations_touched(self) -> int:
        return sum(o.inserted + o.updated for o in self.sources)

    @property
    def errors(self) -> list[str]:
        return [f"{o.source_id}: {o.error}" for o in self.sources if o.error]

    @property
    def review_needed(self) -> list[str]:
        return [o.source_id for o in self.sources if o.status == "changed_review"]


def refresh(
    connection,
    settings: Settings,
    *,
    campus: str | None = None,
    source_ids: list[str] | None = None,
    describe: bool = True,
    client=None,
    fetcher=None,
    description_runner=None,
) -> RefreshOutcome:
    """Fetch, fingerprint and cache the authoritative parking sources.

    `fetcher(source) -> FetchedSource` is injectable so the test suite can run
    the whole path against snapshots without touching the network.
    """
    outcome = RefreshOutcome()
    fetch = fetcher or (lambda source: parking_sources.fetch(source, client=client))

    selected = [
        source
        for source in parking_sources.sources_for(campus)
        if source_ids is None or source.source_id in source_ids
    ]
    if not selected:
        log.info("parking refresh: no sources selected")
        return outcome

    for source in selected:
        outcome.sources.append(
            _refresh_one(connection, source, fetch=fetch, collected=outcome)
        )

    if describe and settings.parking_descriptions_enabled:
        _write_descriptions(
            connection, settings, outcome, campus=campus, runner=description_runner
        )
    return outcome


def _refresh_one(connection, source: Source, *, fetch, collected: RefreshOutcome) -> SourceOutcome:
    result = SourceOutcome(source_id=source.source_id, campus=source.campus, status="ok")
    try:
        fetched = fetch(source)
    except (SourceFetchError, OSError) as exc:
        result.status = "error"
        result.error = str(exc)[:300]
        log.warning("parking source %s failed: %s", source.source_id, result.error)
        with db.transaction(connection):
            parking_store.upsert_source(
                connection,
                source_id=source.source_id,
                campus=source.campus,
                source_type=source.source_type,
                source_url=source.url,
                source_map_id=source.map_id,
                machine_readable=source.machine_readable,
                source_version=source.source_version,
                status="error",
                error=result.error,
            )
        return result

    previous = parking_store.source_row(connection, source.source_id)
    previous_fingerprint = previous["fingerprint"] if previous else None
    changed = fetched.fingerprint != previous_fingerprint
    result.changed = changed

    try:
        parsed = parking_sources.parse(fetched)
    except SourceFetchError as exc:
        result.status = "error"
        result.error = str(exc)[:300]
        with db.transaction(connection):
            parking_store.upsert_source(
                connection,
                source_id=source.source_id, campus=source.campus,
                source_type=source.source_type, source_url=source.url,
                source_map_id=source.map_id,
                machine_readable=source.machine_readable,
                source_version=source.source_version,
                fingerprint=fetched.fingerprint, status="error", error=result.error,
            )
        return result

    collected.landmarks.extend(parsed.landmarks)
    result.landmarks = len(parsed.landmarks)
    if parsed.landmarks:
        with db.transaction(connection):
            parking_store.upsert_landmarks(connection, parsed.landmarks)
    result.unnamed_parking = parsed.unnamed_parking
    result.warnings = parsed.warnings
    result.locations_seen = len(parsed.locations)

    # A hand-derived source whose bytes changed needs a human: step 1 of the
    # derivation is a text extraction nobody can do at runtime. Records stay as
    # they are and the source is flagged.
    review = changed and previous is not None and not source.machine_readable and bool(
        parking_reference.DERIVED.get(source.campus)
    )

    status = "changed_review" if review else ("ok" if changed else "unchanged")

    with db.transaction(connection):
        # An unchanged machine-readable source still re-upserts on first sight
        # (nothing cached yet); after that the fingerprint short-circuits it.
        should_write = changed or previous is None
        if should_write and not review:
            for record in parsed.locations:
                record.source_fingerprint = fetched.fingerprint
                try:
                    _, action = parking_store.upsert_location(connection, record)
                except parking.ParkingDataError as exc:
                    result.warnings.append(f"{record.canonical_id}: {exc}")
                    continue
                if action == "inserted":
                    result.inserted += 1
                elif action == "updated":
                    result.updated += 1
                elif action == "override_preserved":
                    result.overrides_preserved += 1
                else:
                    result.unchanged += 1
        parking_store.upsert_source(
            connection,
            source_id=source.source_id,
            campus=source.campus,
            source_type=source.source_type,
            source_url=source.url,
            source_map_id=source.map_id,
            machine_readable=source.machine_readable,
            fingerprint=fetched.fingerprint,
            source_version=source.source_version,
            status=status,
            error=None,
            locations_seen=len(parsed.locations),
        )
    result.status = status
    if review:
        log.warning(
            "parking source %s changed but is hand-derived; records left alone "
            "pending human review",
            source.source_id,
        )
    return result


def _write_descriptions(
    connection, settings: Settings, outcome: RefreshOutcome, *, campus: str | None, runner
) -> None:
    """Fill in missing plain-English descriptions from the collected evidence."""
    pending = [
        row
        for row in parking_store.locations_needing_description(connection, campus=campus)
        if row["latitude"] is not None
    ]
    if not pending:
        return
    # Read landmarks back from the cache rather than using only what this refresh
    # happened to fetch, so a single-source refresh still has full evidence.
    landmarks = [
        _CachedLandmark(
            name=row["name"], campus=row["campus"],
            category=row["category"] or "Locations",
            latitude=row["latitude"], longitude=row["longitude"],
        )
        for row in parking_store.landmark_rows(connection)
    ] or outcome.landmarks
    if not landmarks:
        log.info("parking descriptions skipped: no landmark evidence available")
        return

    evidence = [
        parking_agent.build_evidence(dict(row), landmarks) for row in pending
    ]
    evidence = [item for item in evidence if item.nearby]
    if not evidence:
        log.info("parking descriptions skipped: no facility had a nearby landmark")
        return

    result = parking_agent.generate_descriptions(
        evidence, settings, batch_size=settings.parking_description_batch, runner=runner
    )
    outcome.description_calls = result.calls
    outcome.description_cost_usd = result.cost_usd
    outcome.description_model = result.model
    outcome.description_error = result.error
    outcome.descriptions_rejected = result.rejected

    with db.transaction(connection):
        for canonical_id, description in result.accepted.items():
            parking_store.set_description(
                connection,
                canonical_id,
                description,
                method="agent",
                model=result.model,
            )
            outcome.descriptions_written += 1
    if result.rejected:
        log.info(
            "%d parking description(s) rejected and left unresolved: %s",
            len(result.rejected),
            "; ".join(f"{k}: {v[:80]}" for k, v in list(result.rejected.items())[:5]),
        )


def collect_landmarks(
    *, campus: str | None = None, client=None, fetcher=None
) -> list:
    """Fetch just the landmark evidence, without touching the cache.

    Used when a description has to be written for a location the resolver just
    discovered and no refresh is in flight.
    """
    fetch = fetcher or (lambda source: parking_sources.fetch(source, client=client))
    landmarks: list = []
    for source in parking_sources.sources_for(campus):
        if source.source_type != "google_my_maps_kml":
            continue
        try:
            landmarks.extend(parking_sources.parse(fetch(source)).landmarks)
        except (SourceFetchError, OSError) as exc:
            log.info("landmark fetch for %s failed: %s", source.source_id, exc)
    return landmarks


def landmark_campus_index(landmarks: list) -> dict[str, str]:
    """normalized landmark name -> campus, for campus inference from buildings."""
    index: dict[str, str] = {}
    for landmark in landmarks:
        key = parking.normalize_name(landmark.name)
        if not key or len(key) < 6:
            continue
        if index.get(key) not in (None, landmark.campus):
            # A name shared by two campuses proves nothing; drop it.
            index[key] = ""
            continue
        index[key] = landmark.campus
    return {name: campus for name, campus in index.items() if campus}
