"""The daily path: detect parking references, resolve them, attach callouts.

This is the code that runs every morning, and almost all of it is a dictionary
lookup. One query loads the alias index, detection is regex work over text
already in memory, and a resolved lot is a hit in that index. On a normal day
there is no network call, no subprocess and no model.

The expensive branches exist and are ordered so that the cheap answer is always
tried first:

    detect -> alias cache -> refresh the campus source -> retry -> resolver agent

Every step past the cache is guarded and best effort. Parking enrichment is
useful, not critical: if a source is down, if the resolver times out, if the
catalog is empty, the digest still goes out complete -- the announcement simply
carries the compact fallback, or nothing at all.
"""

from __future__ import annotations

import logging
import time

from . import (
    db,
    parking,
    parking_agent,
    parking_refresh,
    parking_sources,
    parking_store,
)
from .parking import ParkingCallout, ParkingMetrics
from .parking_store import LocationRecord
from .settings import Settings

log = logging.getLogger("dailymail.parking")

# How much announcement text the resolver may see, for campus disambiguation.
CONTEXT_CHARS = 600


def enrich_digest(
    connection,
    rows,
    *,
    target_date: str,
    settings: Settings,
    allow_refresh: bool = True,
    allow_resolver: bool = True,
    fetcher=None,
    resolver_runner=None,
    description_runner=None,
) -> tuple[dict[str, list[ParkingCallout]], ParkingMetrics]:
    """Resolve parking mentions for one day. Returns `{submission_id: [callout]}`.

    Never raises. A failure anywhere becomes a metric and, at worst, a fallback
    callout.
    """
    started = time.monotonic()
    metrics = ParkingMetrics()
    callouts: dict[str, list[ParkingCallout]] = {}
    if not settings.parking_enabled or not rows:
        metrics.duration_seconds = time.monotonic() - started
        return callouts, metrics

    try:
        state = _CacheState(connection)
    except Exception as exc:  # noqa: BLE001 - a broken cache must not stop the digest
        metrics.errors.append(f"cache unavailable: {type(exc).__name__}: {exc}")
        metrics.duration_seconds = time.monotonic() - started
        return callouts, metrics

    if allow_refresh:
        _refresh_if_stale(connection, settings, state, metrics, fetcher, description_runner)

    for row in rows:
        submission_id = str(row["submission_id"])
        try:
            resolved = _enrich_one(
                connection,
                row,
                target_date=target_date,
                settings=settings,
                state=state,
                metrics=metrics,
                allow_refresh=allow_refresh,
                allow_resolver=allow_resolver,
                fetcher=fetcher,
                resolver_runner=resolver_runner,
                description_runner=description_runner,
            )
        except Exception as exc:  # noqa: BLE001 - never lose an announcement
            metrics.errors.append(
                f"{submission_id}: {type(exc).__name__}: {str(exc)[:160]}"
            )
            log.warning("parking enrichment failed for %s: %s", submission_id, exc)
            continue
        if resolved:
            callouts[submission_id] = resolved
            metrics.announcements_enriched += 1

    metrics.duration_seconds = time.monotonic() - started
    return callouts, metrics


class _CacheState:
    """One snapshot of the parking cache, loaded once per run."""

    def __init__(self, connection) -> None:
        self.connection = connection
        self.reload()

    def reload(self) -> None:
        self.alias_index = parking_store.alias_index(self.connection)
        self.scannable = parking_store.scannable_aliases(self.alias_index)
        self.landmark_index = _landmark_campus_index(self.connection)
        self.refreshed_campuses: set[str] = getattr(self, "refreshed_campuses", set())
        self.resolver_attempted: set[str] = getattr(self, "resolver_attempted", set())

    def lookup(self, keys) -> list:
        """Locations claiming any of these keys, most specific key first."""
        for key in keys:
            hits = self.alias_index.get(key)
            if hits:
                return list(hits)
        return []

    def campuses_with_garages(self, campus: str) -> list:
        return [
            rows[0]
            for key, rows in self.alias_index.items()
            if rows and rows[0]["campus"] == campus and rows[0]["location_type"] == "garage"
        ]


def _landmark_campus_index(connection) -> dict[str, str]:
    """Named things whose campus we know, for disambiguating `Lot A`.

    Two sources, both already cached: the campus buildings from Rowan's own map
    layers, and the parking facilities themselves -- a mention of
    `Rowan Boulevard Garage` implies Glassboro even when nothing else does. A name
    that occurs on two campuses is dropped rather than allowed to mislead.
    """
    index: dict[str, str] = dict(parking_store.landmark_campus_index(connection))
    for row in connection.execute(
        "SELECT canonical_name, campus FROM parking_locations WHERE is_active = 1"
    ):
        key = parking.normalize_name(row["canonical_name"])
        if len(key) < 8:
            continue
        if index.get(key) not in (None, row["campus"]):
            index[key] = ""
            continue
        index[key] = row["campus"]
    return {name: campus for name, campus in index.items() if campus}


def _refresh_if_stale(
    connection, settings: Settings, state: _CacheState, metrics: ParkingMetrics,
    fetcher, description_runner,
) -> None:
    """Verify authoritative sources at most once per staleness window."""
    try:
        stale = parking_store.stale_sources(
            connection, max_age_days=settings.parking_refresh_days
        )
    except Exception as exc:  # noqa: BLE001
        metrics.errors.append(f"staleness check failed: {exc}")
        return
    if not stale:
        return
    log.info("parking reference data is stale (%d source(s)); refreshing", len(stale))
    try:
        outcome = parking_refresh.refresh(
            connection, settings, source_ids=stale,
            fetcher=fetcher, description_runner=description_runner,
        )
        metrics.source_refreshes += len(outcome.sources)
        metrics.errors.extend(outcome.errors)
        state.reload()
    except Exception as exc:  # noqa: BLE001
        metrics.errors.append(f"stale refresh failed: {type(exc).__name__}: {exc}")


def _enrich_one(
    connection,
    row,
    *,
    target_date: str,
    settings: Settings,
    state: _CacheState,
    metrics: ParkingMetrics,
    allow_refresh: bool,
    allow_resolver: bool,
    fetcher,
    resolver_runner,
    description_runner,
) -> list[ParkingCallout]:
    submission_id = int(row["submission_id"])
    title = row["title"] or ""
    body_text = row["body_text"] or ""
    event_location = row["event_location"] if row["is_event"] else None

    mentions = parking.detect_mentions(
        title=title,
        body_text=body_text,
        event_location=event_location,
        named_aliases=state.scannable,
    )
    if not mentions:
        return []
    metrics.mentions_detected += len(mentions)

    campus, campus_evidence = parking.infer_campus(
        category_title=row["category_title"],
        title=title,
        body_text=body_text,
        event_location=event_location,
        landmarks=state.landmark_index,
    )

    callouts: list[ParkingCallout] = []
    seen_locations: set[int] = set()

    for mention in mentions:
        callout = _resolve_mention(
            connection,
            mention,
            campus=campus,
            campus_evidence=campus_evidence,
            row=row,
            target_date=target_date,
            settings=settings,
            state=state,
            metrics=metrics,
            allow_refresh=allow_refresh,
            allow_resolver=allow_resolver,
            fetcher=fetcher,
            resolver_runner=resolver_runner,
            description_runner=description_runner,
            submission_id=submission_id,
        )
        if callout is None:
            continue
        # One block per lot: a lot named three times still renders once.
        key = callout.canonical_name or callout.matched_text
        if callout.resolved:
            location_key = f"{callout.campus}:{key}"
            if location_key in seen_locations:
                continue
            seen_locations.add(location_key)
        else:
            if any(not existing.resolved for existing in callouts):
                continue
        callouts.append(callout)

    return callouts[: settings.parking_max_callouts]


def _resolve_mention(
    connection,
    mention,
    *,
    campus,
    campus_evidence,
    row,
    target_date,
    settings,
    state,
    metrics,
    allow_refresh,
    allow_resolver,
    fetcher,
    resolver_runner,
    description_runner,
    submission_id,
) -> ParkingCallout | None:
    """Cache first; escalate only when the cheap answer does not exist."""
    key = mention.primary_key

    hits = state.lookup(mention.lookup_keys)
    if mention.method == "pattern_bare_garage":
        return _bare_garage(
            connection, mention, campus, state, metrics, target_date, submission_id, row
        )

    if hits:
        metrics.cache_hits += 1
        return _from_hits(
            connection, mention, hits, campus, campus_evidence, settings, metrics,
            target_date, submission_id, row,
        )

    metrics.cache_misses += 1

    # A bare single letter that is not in the catalog is far more likely to be
    # ordinary prose than a lot Rowan forgot to publish. Record it, spend nothing.
    if mention.weak:
        _record_miss(
            connection, mention, campus, metrics, target_date, submission_id, row,
            reason="single-letter code not in the catalog; not researched",
        )
        return None

    # Step 1: refresh the authoritative source for the inferred campus.
    if allow_refresh and settings.parking_refresh_on_miss:
        refresh_campus = campus
        if refresh_campus not in state.refreshed_campuses:
            state.refreshed_campuses.add(refresh_campus or "*")
            try:
                outcome = parking_refresh.refresh(
                    connection, settings, campus=refresh_campus,
                    fetcher=fetcher, description_runner=description_runner,
                )
                metrics.source_refreshes += len(outcome.sources)
                metrics.errors.extend(outcome.errors)
                state.reload()
            except Exception as exc:  # noqa: BLE001
                metrics.errors.append(
                    f"miss refresh failed: {type(exc).__name__}: {str(exc)[:160]}"
                )

        # Step 2: retry deterministic resolution.
        hits = state.lookup(mention.lookup_keys)
        if hits:
            metrics.new_resolutions += 1
            return _from_hits(
                connection, mention, hits, campus, campus_evidence, settings, metrics,
                target_date, submission_id, row, method_prefix="refresh",
            )

    # Step 3: the targeted resolver, at most once per candidate per run.
    if allow_resolver and settings.parking_resolver_enabled and key not in state.resolver_attempted:
        state.resolver_attempted.add(key)
        callout = _run_resolver(
            connection, mention, campus, campus_evidence, row, settings, state,
            metrics, target_date, submission_id, resolver_runner,
        )
        if callout is not None:
            return callout

    # Step 4: unresolved. Fall back, never fabricate.
    _record_miss(
        connection, mention, campus, metrics, target_date, submission_id, row,
        reason="not found in the parking catalog or by targeted resolution",
    )
    return _fallback_callout(mention, campus)


def _from_hits(
    connection, mention, hits, campus, campus_evidence, settings, metrics,
    target_date, submission_id, row, *, method_prefix: str = "cache",
) -> ParkingCallout | None:
    """Pick the one location the evidence supports, or refuse to pick at all."""
    candidates = hits
    if campus:
        narrowed = [hit for hit in hits if hit["campus"] == campus]
        if narrowed:
            candidates = narrowed

    if len(candidates) > 1:
        metrics.ambiguous += 1
        campuses = sorted({hit["campus"] for hit in candidates})
        reason = (
            f"{mention.matched_text!r} exists on {', '.join(campuses)}; "
            f"campus evidence: {campus_evidence}"
        )
        with db.transaction(connection):
            parking_store.record_association(
                connection,
                target_date=target_date,
                submission_id=submission_id,
                normalized_match=mention.primary_key,
                matched_text=mention.matched_text,
                match_method="ambiguous_campus",
                version_id=row["version_id"] if "version_id" in row.keys() else None,
                campus_hint=campus,
            )
            parking_store.record_unresolved(
                connection,
                normalized_match=mention.primary_key,
                matched_text=mention.matched_text,
                campus_hint=campus,
                reason=reason,
            )
        log.info("parking mention left unresolved: %s", reason)
        return _fallback_callout(mention, campus)

    location = candidates[0]
    if not parking.confidence_at_least(
        location["confidence"], settings.parking_min_confidence
    ):
        metrics.unresolved += 1
        with db.transaction(connection):
            parking_store.record_unresolved(
                connection,
                normalized_match=mention.primary_key,
                matched_text=mention.matched_text,
                campus_hint=location["campus"],
                reason=f"cached at confidence {location['confidence']!r}",
            )
        return _fallback_callout(mention, location["campus"])

    if location["latitude"] is None or not location["description"]:
        metrics.unresolved += 1
        missing = "coordinates" if location["latitude"] is None else "a description"
        with db.transaction(connection):
            parking_store.record_unresolved(
                connection,
                normalized_match=mention.primary_key,
                matched_text=mention.matched_text,
                campus_hint=location["campus"],
                reason=f"cached location is missing {missing}",
            )
        return _fallback_callout(mention, location["campus"])

    with db.transaction(connection):
        parking_store.record_association(
            connection,
            target_date=target_date,
            submission_id=submission_id,
            normalized_match=mention.primary_key,
            matched_text=mention.matched_text,
            match_method=f"{method_prefix}_{mention.method}",
            version_id=row["version_id"] if "version_id" in row.keys() else None,
            location_id=int(location["location_id"]),
            confidence=location["confidence"],
            campus_hint=location["campus"],
        )
        parking_store.clear_unresolved(connection, mention.primary_key)
    return _callout_for(location, mention)


def _bare_garage(
    connection, mention, campus, state, metrics, target_date, submission_id, row
) -> ParkingCallout | None:
    """`Parking Garage` with no name: only answerable if the campus has one."""
    if not campus:
        metrics.ambiguous += 1
        return None
    garages = state.campuses_with_garages(campus)
    unique = {row_["canonical_id"]: row_ for row_ in garages}
    if len(unique) != 1:
        metrics.ambiguous += 1
        return None
    location = next(iter(unique.values()))
    if not location["description"] or location["latitude"] is None:
        metrics.unresolved += 1
        return _fallback_callout(mention, campus)
    metrics.cache_hits += 1
    with db.transaction(connection):
        parking_store.record_association(
            connection,
            target_date=target_date,
            submission_id=submission_id,
            normalized_match=mention.primary_key,
            matched_text=mention.matched_text,
            match_method="cache_sole_campus_garage",
            version_id=row["version_id"] if "version_id" in row.keys() else None,
            location_id=int(location["location_id"]),
            confidence=location["confidence"],
            campus_hint=campus,
        )
    return _callout_for(location, mention)


def _run_resolver(
    connection, mention, campus, campus_evidence, row, settings, state, metrics,
    target_date, submission_id, resolver_runner,
) -> ParkingCallout | None:
    """Research one unknown candidate, then cache it only on real evidence."""
    campus_candidates = [campus] if campus else list(parking.DEFAULT_CAMPUS_ORDER)
    known = {
        name: sorted(
            {
                hit["canonical_name"]
                for hits in state.alias_index.values()
                for hit in hits
                if hit["campus"] == name
            }
        )[:60]
        for name in campus_candidates
    }
    context = " ".join(
        part for part in ((row["title"] or ""), (row["body_text"] or "")) if part
    )[:CONTEXT_CHARS]

    payload = parking_agent.build_resolver_payload(
        matched_text=mention.matched_text,
        lookup_keys=list(mention.lookup_keys),
        campus_candidates=campus_candidates,
        campus_evidence=campus_evidence,
        context_excerpt=context,
        known_by_campus=known,
    )
    metrics.resolver_calls += 1
    log.info("invoking targeted parking resolver for %r", mention.matched_text)
    result = parking_agent.resolve_location(payload, settings, runner=resolver_runner)

    if not result.resolved or not result.record:
        _record_miss(
            connection, mention, campus, metrics, target_date, submission_id, row,
            reason=f"resolver: {result.reason}", resolver_called=True,
        )
        return _fallback_callout(mention, campus)

    record = result.record
    location_type = record["location_type"]
    canonical = parking.canonical_id(
        record["campus"], location_type, record["canonical_name"]
    )
    try:
        with db.transaction(connection):
            location_id, _ = parking_store.upsert_location(
                connection,
                LocationRecord(
                    canonical_id=canonical,
                    campus=record["campus"],
                    canonical_name=record["canonical_name"],
                    location_type=location_type,
                    permit_class=record["permit_class"],
                    description=record["description"],
                    latitude=record["latitude"],
                    longitude=record["longitude"],
                    source_id="targeted-resolver",
                    source_type="targeted_resolver",
                    source_url=(record["evidence"][0]["source_url"]
                                if record["evidence"] else None),
                    provenance=(
                        "Resolved by the targeted parking resolver from: "
                        + "; ".join(
                            f"{entry['source_url']} ({entry['what_it_shows']})"
                            for entry in record["evidence"][:3]
                        )
                    ),
                    confidence=record["confidence"],
                    aliases=record["aliases"] + [mention.matched_text],
                ),
            )
            parking_store.add_aliases(
                connection, location_id, record["campus"],
                [mention.matched_text] + record["aliases"], origin="resolver",
            )
            parking_store.record_association(
                connection,
                target_date=target_date,
                submission_id=submission_id,
                normalized_match=mention.primary_key,
                matched_text=mention.matched_text,
                match_method="resolver_agent",
                version_id=row["version_id"] if "version_id" in row.keys() else None,
                location_id=location_id,
                confidence=record["confidence"],
                campus_hint=record["campus"],
            )
            parking_store.clear_unresolved(connection, mention.primary_key)
    except parking.ParkingDataError as exc:
        _record_miss(
            connection, mention, campus, metrics, target_date, submission_id, row,
            reason=f"resolver result rejected: {exc}", resolver_called=True,
        )
        return _fallback_callout(mention, campus)

    metrics.new_resolutions += 1
    state.reload()
    location = parking_store.location_by_canonical_id(connection, canonical)
    if location is None or not location["description"]:
        return _fallback_callout(mention, record["campus"])
    return _callout_for(location, mention)


def _record_miss(
    connection, mention, campus, metrics, target_date, submission_id, row,
    *, reason: str, resolver_called: bool = False,
) -> None:
    metrics.unresolved += 1
    with db.transaction(connection):
        parking_store.record_unresolved(
            connection,
            normalized_match=mention.primary_key,
            matched_text=mention.matched_text,
            campus_hint=campus,
            reason=reason,
            resolver_called=resolver_called,
        )
        parking_store.record_association(
            connection,
            target_date=target_date,
            submission_id=submission_id,
            normalized_match=mention.primary_key,
            matched_text=mention.matched_text,
            match_method="unresolved",
            version_id=row["version_id"] if "version_id" in row.keys() else None,
            campus_hint=campus,
        )
    log.info("parking mention %r unresolved: %s", mention.matched_text, reason)


def _callout_for(location, mention) -> ParkingCallout:
    campus = location["campus"]
    return ParkingCallout(
        matched_text=mention.matched_text,
        resolved=True,
        canonical_name=location["canonical_name"],
        campus=campus,
        campus_display=parking.CAMPUSES[campus]["display"],
        permit_class=location["permit_class"],
        location_type=location["location_type"],
        description=location["description"],
        latitude=location["latitude"],
        longitude=location["longitude"],
        map_url=parking.maps_url(location["latitude"], location["longitude"]),
        confidence=location["confidence"],
    )


def _fallback_callout(mention, campus) -> ParkingCallout:
    """Compact, honest fallback: no coordinate, no sentence, just the official map."""
    url, label = parking_sources.fallback_map_url(campus)
    return ParkingCallout(
        matched_text=mention.matched_text,
        resolved=False,
        campus=campus,
        campus_display=parking.CAMPUSES[campus]["display"] if campus else None,
        fallback_map_url=url,
        fallback_map_label=label,
    )
