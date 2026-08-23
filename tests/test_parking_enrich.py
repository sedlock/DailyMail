"""The daily path: cache hit, cache miss, ambiguity, resolver, fallback, render.

The important assertions here are about what *does not* happen. A cache hit must
not launch a subprocess. An ambiguous lot name must not pick a campus. A resolver
failure must not cost the digest.
"""

from __future__ import annotations

import json

import pytest

from dailymail import (
    curate,
    db,
    ingest,
    parking,
    parking_agent,
    parking_enrich,
    parking_refresh,
    parking_store,
    render,
)
from dailymail.parking_store import LocationRecord

from conftest import (
    TARGET_DATE,
    OfflineParkingFetcher,
    artifact_from_fixtures,
    stub_description_runner,
)

O1_BODY = (
    "Parking Lot O-1 will be closed on Wednesday, August 19, 2026 at 10 pm. "
    "The lot will remain closed until Monday, August 24, 2026."
)


def explode(*args, **kwargs):
    """Any parking agent call in a cache-hit test is a bug."""
    raise AssertionError("no parking agent call is allowed on this path")


# --- a small synthetic row, shaped exactly like db.digest_rows() --------------


class Row(dict):
    """Mimics sqlite3.Row closely enough for the enrichment code path."""

    def __getitem__(self, key):
        return super().__getitem__(key)

    def keys(self):  # noqa: D102
        return super().keys()


def make_row(**overrides) -> Row:
    values = {
        "submission_id": 6622,
        "version_id": 11,
        "title": "Parking Lot O-1  Closure",
        "body_text": O1_BODY,
        "is_event": 0,
        "event_location": None,
        "category_title": "Public Safety",
        "status": "New",
    }
    values.update(overrides)
    return Row(values)


@pytest.fixture
def seeded(parking_cache):
    """A parking cache plus the announcements the association table points at."""
    with db.transaction(parking_cache):
        for submission_id in (6622, 7001, 7002, 7003):
            parking_cache.execute(
                "INSERT OR IGNORE INTO announcements (submission_id, source_audience, "
                "first_observed_at, last_observed_at, official_url) "
                "VALUES (?, 'Both', 'x', 'x', 'https://example')",
                (submission_id,),
            )
    return parking_cache


# --- cache hit ---------------------------------------------------------------


def test_cache_hit_resolves_o1_and_calls_no_agent(seeded, settings_obj):
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [make_row()], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode, description_runner=explode,
    )
    assert metrics.mentions_detected == 1
    assert metrics.cache_hits == 1
    assert metrics.cache_misses == 0
    assert metrics.resolver_calls == 0
    assert metrics.source_refreshes == 0
    assert metrics.unresolved == 0

    spot = callouts["6622"][0]
    assert spot.resolved
    assert spot.canonical_name == "Lot O-1"
    assert spot.campus == "glassboro"
    assert spot.permit_class == "Employee"
    assert spot.latitude == pytest.approx(39.712482, abs=1e-6)
    assert spot.map_url == (
        "https://www.google.com/maps/search/?api=1&query=39.712482,-75.120453"
    )
    assert "James Hall" in spot.description


def test_a_cache_hit_is_fast(seeded, settings_obj):
    """No network, no subprocess: the whole hot path is a dictionary lookup."""
    _, metrics = parking_enrich.enrich_digest(
        seeded, [make_row()], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    assert metrics.duration_seconds < 1.0


def test_the_audit_trail_records_the_association(seeded, settings_obj):
    parking_enrich.enrich_digest(
        seeded, [make_row()], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    rows = parking_store.associations_for_date(seeded, TARGET_DATE)
    assert len(rows) == 1
    assert rows[0]["submission_id"] == 6622
    assert rows[0]["normalized_match"] == "lot o 1"
    assert rows[0]["matched_text"].startswith("Parking Lot O-1")
    assert rows[0]["match_method"].startswith("cache_")
    assert rows[0]["confidence"] == "high"
    assert rows[0]["campus_hint"] == "glassboro"
    assert rows[0]["location_id"] is not None


def test_an_announcement_with_no_parking_reference_gets_nothing(seeded, settings_obj):
    row = make_row(
        submission_id=7001,
        title="Academic Integrity: Resources and Reminders for Faculty",
        body_text="There is a lot of interest in the workshop series this fall.",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    assert callouts == {}
    assert metrics.mentions_detected == 0


# --- multiple lots and de-duplication ----------------------------------------


def test_multiple_lots_each_render_once(seeded, settings_obj):
    row = make_row(
        submission_id=7001,
        title="Paving in Lot O-1 and Lot O-2",
        body_text="Lot O-1 and Lot O-2 close Friday. Lot O-1 reopens Monday.",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    names = [spot.canonical_name for spot in callouts["7001"]]
    assert names == ["Lot O-1", "Lot O-2"]
    assert metrics.cache_hits == 2


def test_repeated_mentions_of_one_lot_produce_one_callout(seeded, settings_obj):
    row = make_row(
        body_text=O1_BODY + " Lot O-1 will reopen. The O-1 Lot signage is posted. "
        "Parking Lot O-1 questions to Public Safety."
    )
    callouts, _ = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    assert len(callouts["6622"]) == 1


def test_callouts_are_capped(seeded, settings_obj):
    from dataclasses import replace

    settings = replace(settings_obj, parking_max_callouts=2)
    row = make_row(
        submission_id=7001,
        body_text="Lots O-1, O-2, D-1, D-2 and C-1 will be swept this week.",
    )
    callouts, _ = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings,
        allow_refresh=False, resolver_runner=explode,
    )
    assert len(callouts["7001"]) == 2


# --- campus ambiguity --------------------------------------------------------


def test_lot_a_without_campus_evidence_stays_unresolved(seeded, settings_obj):
    """Glassboro and Stratford both have a Lot A. Guessing is not allowed."""
    row = make_row(
        submission_id=7002,
        title="Lot A Closure",
        body_text="Lot A will be closed on Friday for resurfacing.",
        category_title="Public Safety",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    assert metrics.ambiguous == 1
    spot = callouts["7002"][0]
    assert not spot.resolved
    assert spot.canonical_name is None
    assert spot.fallback_map_url
    association = parking_store.associations_for_date(seeded, TARGET_DATE)[0]
    assert association["match_method"] == "ambiguous_campus"
    assert association["location_id"] is None
    reason = parking_store.unresolved_rows(seeded)[0]["last_reason"]
    assert "glassboro" in reason and "stratford" in reason


def test_lot_a_with_a_stratford_category_resolves_to_stratford(seeded, settings_obj):
    row = make_row(
        submission_id=7002,
        title="Lot A Closure",
        body_text="Lot A will be closed on Friday.",
        category_title="Stratford Campus",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    spot = callouts["7002"][0]
    assert spot.resolved
    assert spot.campus == "stratford"
    assert spot.permit_class == "Patient"
    assert metrics.ambiguous == 0


def test_lot_a_with_glassboro_evidence_resolves_to_glassboro(seeded, settings_obj):
    row = make_row(
        submission_id=7002,
        title="Lot A Closure",
        body_text="Lot A on the Glassboro campus will be closed on Friday.",
        category_title="Public Safety",
    )
    callouts, _ = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    spot = callouts["7002"][0]
    assert spot.resolved
    assert spot.campus == "glassboro"
    assert spot.permit_class == "Commuter"


def test_lot_a_disambiguated_by_a_named_building(seeded, settings_obj):
    row = make_row(
        submission_id=7002,
        title="Lot A Closure",
        body_text="Lot A beside the Rowan Medicine building will be closed.",
        category_title="Public Safety",
    )
    callouts, _ = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    assert callouts["7002"][0].campus == "stratford"


def test_o1_needs_no_campus_evidence_because_it_is_unique(seeded, settings_obj):
    """Only Glassboro has an O-1, so resolving it is not a guess."""
    index = parking_store.alias_index(seeded)
    assert len(index["lot o 1"]) == 1
    callouts, _ = parking_enrich.enrich_digest(
        seeded,
        [make_row(title="Lot O-1 Closure", body_text="Lot O-1 is closed.",
                  category_title="Public Safety")],
        target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    assert callouts["6622"][0].campus == "glassboro"


# --- cache miss --------------------------------------------------------------


def test_a_weak_single_letter_miss_spends_nothing(seeded, settings_obj):
    row = make_row(
        submission_id=7003,
        title="Building update",
        body_text="Lot I will be closed while the survey is completed.",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=True, resolver_runner=explode, fetcher=explode,
    )
    assert callouts == {}
    assert metrics.cache_misses == 1
    assert metrics.resolver_calls == 0
    assert metrics.source_refreshes == 0
    assert metrics.unresolved == 1
    assert "single-letter" in parking_store.unresolved_rows(seeded)[0]["last_reason"]


def test_a_miss_refreshes_the_source_then_resolves(settings_obj, parking_fetcher):
    """Step 1 and 2 of the miss path: refresh, then retry deterministically."""
    connection = db.connect()
    db.initialize(connection)
    with db.transaction(connection):
        connection.execute(
            "INSERT INTO announcements (submission_id, source_audience, "
            "first_observed_at, last_observed_at, official_url) "
            "VALUES (6622, 'Both', 'x', 'x', 'https://example')"
        )
    # The cache starts empty, so O-1 is a genuine miss.
    assert parking_store.all_locations(connection) == []

    callouts, metrics = parking_enrich.enrich_digest(
        connection, [make_row()], target_date=TARGET_DATE, settings=settings_obj,
        fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
        resolver_runner=explode,
    )
    assert metrics.cache_misses == 1
    assert metrics.source_refreshes > 0
    assert metrics.new_resolutions == 1
    assert metrics.resolver_calls == 0
    spot = callouts["6622"][0]
    assert spot.resolved and spot.canonical_name == "Lot O-1"
    connection.close()


def test_an_unresolvable_miss_falls_back_gracefully(seeded, settings_obj):
    from dataclasses import replace

    settings = replace(settings_obj, parking_resolver_enabled=False)
    row = make_row(
        submission_id=7003,
        title="Lot Q-7 Closure",
        body_text="Lot Q-7 on the Glassboro campus will be closed for paving.",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings,
        fetcher=OfflineParkingFetcher(), description_runner=stub_description_runner(),
        resolver_runner=explode,
    )
    assert metrics.unresolved == 1
    assert metrics.resolver_calls == 0
    spot = callouts["7003"][0]
    assert not spot.resolved
    assert spot.description is None
    assert spot.latitude is None
    assert spot.fallback_map_url.startswith("https://")
    assert "parking" in spot.fallback_map_label.lower()


def test_the_fallback_link_matches_the_inferred_campus(seeded, settings_obj):
    from dataclasses import replace

    settings = replace(settings_obj, parking_resolver_enabled=False)
    row = make_row(
        submission_id=7003,
        title="Lot Q-7 Closure",
        body_text="Lot Q-7 at the Stratford campus is closed.",
    )
    callouts, _ = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings,
        fetcher=OfflineParkingFetcher(), description_runner=stub_description_runner(),
    )
    spot = callouts["7003"][0]
    assert spot.campus == "stratford"
    assert "som.rowan.edu" in spot.fallback_map_url


# --- targeted resolver -------------------------------------------------------


GOOD_RESOLVER_RESPONSE = {
    "resolved": True,
    "campus": "glassboro",
    "canonical_name": "Lot Q-7",
    "location_type": "surface_lot",
    "permit_class": "Employee",
    "description": "Employee lot immediately north of James Hall on the Glassboro campus.",
    "latitude": 39.7135,
    "longitude": -75.1195,
    "aliases": ["Q-7 Lot"],
    "confidence": "high",
    "evidence": [
        {
            "source_url": "https://sites.rowan.edu/publicsafety/parking/",
            "what_it_shows": "Lot Q-7 listed as an employee lot near James Hall",
        }
    ],
}


def resolver_returning(response, *, calls=None):
    def runner(payload, settings):
        if calls is not None:
            calls.append(payload)
        return response, 0.02, "stub-model"

    return runner


def test_the_resolver_caches_a_new_lot_on_good_evidence(seeded, settings_obj):
    calls: list[dict] = []
    row = make_row(
        submission_id=7003,
        title="Lot Q-7 Closure",
        body_text="Lot Q-7 on the Glassboro campus will be closed for paving.",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False,
        resolver_runner=resolver_returning(GOOD_RESOLVER_RESPONSE, calls=calls),
    )
    assert metrics.resolver_calls == 1
    assert metrics.new_resolutions == 1
    spot = callouts["7003"][0]
    assert spot.resolved and spot.canonical_name == "Lot Q-7"
    assert spot.map_url.endswith("39.7135,-75.1195")

    stored = parking_store.location_by_canonical_id(seeded, "glassboro:lot:q-7")
    assert stored is not None
    assert stored["source_type"] == "targeted_resolver"
    assert "sites.rowan.edu" in stored["provenance"]
    # The candidate is now a cache hit, so tomorrow costs nothing.
    index = parking_store.alias_index(seeded)
    assert "lot q 7" in index

    payload = calls[0]
    assert payload["candidate"]["matched_text"] == "Lot Q-7"
    assert payload["campus_candidates"] == ["glassboro"]
    assert payload["authoritative_sources"]


def test_the_resolver_is_asked_at_most_once_per_candidate_per_run(seeded, settings_obj):
    calls: list[dict] = []
    rows = [
        make_row(submission_id=7002, title="Lot Q-7 paving",
                 body_text="Lot Q-7 at Glassboro closes Monday."),
        make_row(submission_id=7003, title="Lot Q-7 reminder",
                 body_text="Lot Q-7 at Glassboro is still closed."),
    ]
    _, metrics = parking_enrich.enrich_digest(
        seeded, rows, target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False,
        resolver_runner=resolver_returning({"resolved": False, "confidence": "low",
                                            "reason": "not found"}, calls=calls),
    )
    assert metrics.resolver_calls == 1
    assert len(calls) == 1


def test_a_resolver_failure_does_not_fail_the_digest(seeded, settings_obj):
    def boom(payload, settings):
        raise TimeoutError("claude timed out after 300s")

    row = make_row(
        submission_id=7003, title="Lot Q-7 Closure",
        body_text="Lot Q-7 at Glassboro will be closed.",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=boom,
    )
    assert metrics.resolver_calls == 1
    assert metrics.unresolved == 1
    spot = callouts["7003"][0]
    assert not spot.resolved and spot.fallback_map_url
    assert "TimeoutError" in parking_store.unresolved_rows(seeded)[0]["last_reason"]


def test_a_broken_parking_cache_does_not_fail_the_digest(settings_obj, tmp_path):
    """A missing parking schema must degrade to no enrichment, not an exception."""
    connection = db.connect(tmp_path / "bare.sqlite3")
    connection.executescript(db.SCHEMA)
    callouts, metrics = parking_enrich.enrich_digest(
        connection, [make_row()], target_date=TARGET_DATE, settings=settings_obj,
    )
    assert callouts == {}
    assert metrics.errors
    connection.close()


def test_enrichment_disabled_does_nothing(seeded, settings_obj):
    from dataclasses import replace

    settings = replace(settings_obj, parking_enabled=False)
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [make_row()], target_date=TARGET_DATE, settings=settings,
    )
    assert callouts == {}
    assert metrics.mentions_detected == 0


def test_a_low_confidence_record_shows_the_fallback_instead(seeded, settings_obj):
    """`min_confidence` keeps a shaky record out of the reader's email."""
    row = make_row(
        submission_id=7003,
        title="Stratford Lot G Closure",
        body_text="Lot G at the Stratford campus is closed.",
    )
    stored = parking_store.location_by_canonical_id(seeded, "stratford:lot:g")
    assert stored["confidence"] == "low"
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    spot = callouts["7003"][0]
    assert not spot.resolved
    assert metrics.unresolved == 1


def test_a_named_garage_resolves_from_the_alias_scan(seeded, settings_obj):
    row = make_row(
        submission_id=7001,
        title="Elevator maintenance",
        body_text="The Rowan Boulevard Garage elevator will be out of service Friday.",
    )
    callouts, metrics = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    spot = callouts["7001"][0]
    assert spot.resolved
    assert spot.canonical_name == "Rowan Boulevard Garage"
    assert spot.location_type == "garage"


def test_a_non_glassboro_callout_names_its_campus(seeded, settings_obj):
    row = make_row(
        submission_id=7002, title="Lot D-3 closure",
        body_text="Lot D-3 is closed.", category_title="Stratford Campus",
    )
    callouts, _ = parking_enrich.enrich_digest(
        seeded, [row], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    spot = callouts["7002"][0]
    assert spot.heading == "Lot D-3 · Stratford"


def test_a_glassboro_callout_does_not_repeat_the_campus(seeded, settings_obj):
    callouts, _ = parking_enrich.enrich_digest(
        seeded, [make_row()], target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    assert callouts["6622"][0].heading == "Lot O-1"


# --- stale refresh -----------------------------------------------------------


def test_stale_sources_are_refreshed_during_a_run(seeded, settings_obj):
    seeded.execute(
        "UPDATE parking_sources SET last_verified_at = '2020-01-01T00:00:00+00:00'"
    )
    seeded.commit()
    fetcher = OfflineParkingFetcher()
    _, metrics = parking_enrich.enrich_digest(
        seeded, [make_row()], target_date=TARGET_DATE, settings=settings_obj,
        fetcher=fetcher, description_runner=stub_description_runner(),
        resolver_runner=explode,
    )
    assert metrics.source_refreshes > 0
    assert fetcher.calls
    assert parking_store.stale_sources(seeded, max_age_days=180) == []


def test_a_fresh_catalog_triggers_no_refresh(seeded, settings_obj):
    fetcher = OfflineParkingFetcher()
    _, metrics = parking_enrich.enrich_digest(
        seeded, [make_row()], target_date=TARGET_DATE, settings=settings_obj,
        fetcher=fetcher, resolver_runner=explode,
    )
    assert metrics.source_refreshes == 0
    assert fetcher.calls == []


# --- rendering ---------------------------------------------------------------


@pytest.fixture
def rendered_o1(parking_cache, settings_obj, employee_fixture, student_fixture):
    """The real 2026-08-20 digest, rendered with parking enrichment."""
    artifact = artifact_from_fixtures(employee_fixture, student_fixture, TARGET_DATE)
    ingest.ingest_artifact(parking_cache, artifact, settings_obj, origin="test")
    rows = db.digest_rows(parking_cache, TARGET_DATE)
    callouts, metrics = parking_enrich.enrich_digest(
        parking_cache, rows, target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False, resolver_runner=explode,
    )
    counts = db.counts_for_date(parking_cache, TARGET_DATE)
    ordering = {
        entry["submission_id"]: entry
        for entry in curate.fallback_rank(rows, TARGET_DATE, settings_obj)
    }
    digest = render.render_digest(
        rows, target_date=TARGET_DATE, counts=counts, ordering=ordering,
        curation_method="fallback", settings=settings_obj, parking=callouts,
    )
    return digest, callouts, metrics, rows


def test_the_historical_o1_digest_renders_the_parking_callout(rendered_o1):
    digest, callouts, metrics, _ = rendered_o1
    assert metrics.cache_hits == 1
    assert digest.parking_callouts == 1
    assert digest.parking_unresolved == 0
    assert "PARKING LOCATION" in digest.html
    assert "Lot O-1" in digest.html
    assert "Open in Google Maps" in digest.html
    assert (
        "https://www.google.com/maps/search/?api=1&amp;query=39.712482,-75.120453"
        in digest.html
    )


def test_the_original_o1_body_is_unchanged_in_the_rendering(rendered_o1):
    digest, _, _, _ = rendered_o1
    assert (
        "Parking Lot O-1 will be closed on Wednesday, August 19, 2026 at 10 pm."
        in digest.html
    )
    assert "The lot will remain closed until Monday, August 24, 2026." in digest.html
    assert "Parking Lot O-1 will be closed" in digest.text
    assert "The lot will remain closed until Monday, August 24, 2026." in digest.text


def test_the_callout_sits_between_the_title_and_the_body(rendered_o1):
    digest, _, _, _ = rendered_o1
    title_at = digest.html.index("Parking Lot O-1 Closure</a>")
    callout_at = digest.html.index("PARKING LOCATION")
    body_at = digest.html.index("Parking Lot O-1 will be closed on Wednesday")
    assert title_at < callout_at < body_at


def test_the_plain_text_alternative_includes_the_parking_information(rendered_o1):
    digest, _, _, _ = rendered_o1
    assert "PARKING LOCATION" in digest.text
    assert "Lot O-1 -- " in digest.text
    assert (
        "Map: https://www.google.com/maps/search/?api=1&query=39.712482,-75.120453"
        in digest.text
    )


def test_the_callout_markup_is_outlook_safe(rendered_o1):
    digest, _, _, _ = rendered_o1
    start = digest.html.index("PARKING LOCATION")
    block = digest.html[start - 600 : start + 1500]
    assert 'role="presentation"' in block
    assert "<table" in block and "cellpadding=\"0\"" in block
    for forbidden in ("display:flex", "position:absolute", "<script", "background-image"):
        assert forbidden not in block
    # Rowan's palette, used for the label and the link only.
    assert "#57150B" in block and "#FFCC00" in block


def test_the_map_link_is_mobile_friendly(rendered_o1):
    digest, _, _, _ = rendered_o1
    start = digest.html.index("Open in Google Maps")
    anchor = digest.html[start - 500 : start + 40]
    assert 'target="_blank"' in anchor
    assert 'rel="noopener noreferrer"' in anchor
    # A real tap target rather than a bare inline word.
    assert "display:inline-block" in anchor
    assert "padding:6px" in anchor
    assert "font-size:13px" in anchor


def test_rendering_without_parking_is_unchanged(
    parking_cache, settings_obj, employee_fixture, student_fixture
):
    """The enrichment is additive: omitting it reproduces the old output."""
    artifact = artifact_from_fixtures(employee_fixture, student_fixture, TARGET_DATE)
    ingest.ingest_artifact(parking_cache, artifact, settings_obj, origin="test")
    rows = db.digest_rows(parking_cache, TARGET_DATE)
    counts = db.counts_for_date(parking_cache, TARGET_DATE)
    ordering = {
        entry["submission_id"]: entry
        for entry in curate.fallback_rank(rows, TARGET_DATE, settings_obj)
    }
    digest = render.render_digest(
        rows, target_date=TARGET_DATE, counts=counts, ordering=ordering,
        curation_method="fallback", settings=settings_obj,
    )
    assert "PARKING LOCATION" not in digest.html
    assert "PARKING LOCATION" not in digest.text
    assert digest.parking_callouts == 0


def test_parking_does_not_change_the_digest_content_hash(
    parking_cache, settings_obj, employee_fixture, student_fixture
):
    """Enrichment is reference data about the announcement, not its content.

    Keeping it out of the hash means a parking-catalog refresh cannot look like a
    content change and cannot disturb delivery idempotency.
    """
    artifact = artifact_from_fixtures(employee_fixture, student_fixture, TARGET_DATE)
    ingest.ingest_artifact(parking_cache, artifact, settings_obj, origin="test")
    rows = db.digest_rows(parking_cache, TARGET_DATE)
    counts = db.counts_for_date(parking_cache, TARGET_DATE)
    ordering = {
        entry["submission_id"]: entry
        for entry in curate.fallback_rank(rows, TARGET_DATE, settings_obj)
    }
    kwargs = dict(
        target_date=TARGET_DATE, counts=counts, ordering=ordering,
        curation_method="fallback", settings=settings_obj,
    )
    plain = render.render_digest(rows, **kwargs)
    callouts, _ = parking_enrich.enrich_digest(
        parking_cache, rows, target_date=TARGET_DATE, settings=settings_obj,
        allow_refresh=False,
    )
    enriched = render.render_digest(rows, parking=callouts, **kwargs)
    assert enriched.content_hash == plain.content_hash
    assert enriched.submission_ids == plain.submission_ids


def test_an_unresolved_fallback_renders_compactly(parking_cache, settings_obj):
    from dailymail.parking import ParkingCallout

    rows = []
    fallback = ParkingCallout(
        matched_text="Lot A",
        resolved=False,
        campus="glassboro",
        campus_display="Glassboro",
        fallback_map_url="https://sites.rowan.edu/publicsafety/parking/",
        fallback_map_label="Glassboro campus parking map",
    )
    environment = render._environment()
    html = environment.get_template("parking.html.j2").render(
        item={"parking": [fallback]}
    )
    assert "could not be resolved automatically" in html
    assert "https://sites.rowan.edu/publicsafety/parking/" in html
    assert "Glassboro campus parking map" in html
    # Compact: one table, no repeated heading, no coordinate.
    assert html.count("<table") == 1
    assert "39." not in html
