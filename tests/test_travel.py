"""Venue resolution and travel reservation.

The invariant these tests protect is that a cached venue costs nothing: no
network call, no model call, no geocoding. `conftest` wires the OSRM lookup to
fail loudly, so a test that accidentally reaches for routing fails instead of
quietly making a real request -- the same discipline the parking cache uses.
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest

from dailymail import db, parking, travel

BASE = (39.70791, -75.11288)  # 201 Mullica Hill Rd, Glassboro NJ

# Real coordinates from Rowan's own published campus map, already cached in
# `parking_landmarks` by Phase 3.
CHAMBERLAIN = (39.708828, -75.117798)
BUNCE = (39.707233, -75.120613)
CMSRU_CAMDEN = (39.9445, -75.1196)


@pytest.fixture
def landmarks_db(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    with db.transaction(connection):
        for campus, name, lat, lon in (
            ("glassboro", "Chamberlain Student Center", *CHAMBERLAIN),
            ("glassboro", "Bunce Hall", *BUNCE),
            ("camden", "Medical Education Building", *CMSRU_CAMDEN),
            ("glassboro", "Rowan Hall", 39.712257, -75.122246),
            ("stratford", "Academic Center", 39.8319, -75.0181),
        ):
            connection.execute(
                "INSERT INTO parking_landmarks (campus, name, normalized_name, "
                "latitude, longitude, last_verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                (campus, name, parking.normalize_name(name), lat, lon, db.now_utc()),
            )
    yield connection
    connection.close()


# --- venue normalization -----------------------------------------------------


def test_spelling_variants_share_one_cache_key():
    keys = {
        travel.normalize_venue("Chamberlain Student Center, Eynon Ballroom"),
        travel.normalize_venue("chamberlain  student center,  eynon ballroom"),
        travel.normalize_venue("Chamberlain Student Center - Eynon Ballroom"),
    }
    assert len(keys) == 1


def test_a_room_falls_back_to_its_building():
    fragments = travel.venue_fragments("Chamberlain Student Center, Eynon Ballroom")
    assert "Chamberlain Student Center" in fragments


def test_a_hyphen_separator_is_split_but_a_hyphenated_name_is_not():
    assert "Chamberlain Student Center" in travel.venue_fragments(
        "Chamberlain Student Center- Enyon Ballroom"
    )
    assert travel.venue_fragments("Rowan-Virtua Campus")[0] == "Rowan-Virtua Campus"


def test_a_floor_qualifier_is_stripped():
    """`Oak Hall North 3rd Floor` reduces to the building the campus map knows."""
    fragments = travel.venue_fragments("Oak Hall North 3rd Floor")
    assert fragments[0] == "Oak Hall North 3rd Floor"  # tried verbatim first
    assert "Oak Hall" in fragments


# --- venue resolution --------------------------------------------------------


def test_the_eynon_ballroom_resolves_through_its_cached_building(landmarks_db):
    """Reuses Phase 3's campus map rather than building a second one."""
    resolution = travel.resolve_venue(
        landmarks_db, "Chamberlain Student Center, Eynon Ballroom",
        campus_hint="glassboro",
    )
    assert resolution.located
    assert resolution.source == travel.SOURCE_LANDMARK
    assert resolution.source_detail == "Chamberlain Student Center"
    assert (resolution.latitude, resolution.longitude) == CHAMBERLAIN


def test_an_unknown_venue_is_reported_not_guessed(landmarks_db):
    resolution = travel.resolve_venue(landmarks_db, "The Moon")
    assert not resolution.located
    assert resolution.source == travel.SOURCE_UNKNOWN


def test_a_parking_facility_can_supply_the_coordinate(landmarks_db, settings_obj):
    with db.transaction(landmarks_db):
        cursor = landmarks_db.execute(
            "INSERT INTO parking_locations (canonical_id, campus, canonical_name, "
            "normalized_name, location_type, latitude, longitude, "
            "first_discovered_at, last_verified_at, last_changed_at) "
            "VALUES ('glassboro:lot:w', 'glassboro', 'Lot W', 'lot w', "
            "'surface_lot', 39.710365, -75.117538, ?, ?, ?)",
            (db.now_utc(), db.now_utc(), db.now_utc()),
        )
        landmarks_db.execute(
            "INSERT INTO parking_aliases (location_id, campus, alias, "
            "normalized_alias, created_at) VALUES (?, 'glassboro', 'Lot W', "
            "'lot w', ?)",
            (cursor.lastrowid, db.now_utc()),
        )
    resolution = travel.resolve_venue(landmarks_db, "Lot W", campus_hint="glassboro")
    assert resolution.located
    assert resolution.source == travel.SOURCE_PARKING


def test_a_name_on_two_campuses_without_evidence_stays_unresolved(landmarks_db):
    with db.transaction(landmarks_db):
        for campus in ("glassboro", "camden"):
            landmarks_db.execute(
                "INSERT INTO parking_landmarks (campus, name, normalized_name, "
                "latitude, longitude, last_verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                (campus, "Science Building", "science building", 39.7, -75.1,
                 db.now_utc()),
            )
    resolution = travel.resolve_venue(landmarks_db, "Science Building")
    assert not resolution.located


def test_a_campus_hint_disambiguates(landmarks_db):
    with db.transaction(landmarks_db):
        for campus, lat in (("glassboro", 39.71), ("camden", 39.94)):
            landmarks_db.execute(
                "INSERT INTO parking_landmarks (campus, name, normalized_name, "
                "latitude, longitude, last_verified_at) VALUES (?, ?, ?, ?, ?, ?)",
                (campus, "Science Building", "science building", lat, -75.1,
                 db.now_utc()),
            )
    resolution = travel.resolve_venue(
        landmarks_db, "Science Building", campus_hint="camden"
    )
    assert resolution.located
    assert resolution.campus == "camden"


# --- travel planning ---------------------------------------------------------


def _resolution(lat, lon, name="Somewhere"):
    return travel.VenueResolution(
        normalized_venue=travel.normalize_venue(name), display_name=name,
        latitude=lat, longitude=lon, campus="glassboro",
        source=travel.SOURCE_LANDMARK,
    )


def test_a_virtual_event_reserves_no_travel(settings_obj):
    plan = travel.plan_travel(
        _resolution(*CHAMBERLAIN), attendance_mode="virtual", settings=settings_obj
    )
    assert not plan.required
    assert plan.reason == "virtual_attendance"
    assert plan.minutes_before == plan.minutes_after == 0


def test_an_unresolved_venue_reserves_no_travel(settings_obj):
    plan = travel.plan_travel(
        travel.VenueResolution(normalized_venue="x", display_name="x"),
        attendance_mode="in_person", settings=settings_obj,
    )
    assert not plan.required
    assert plan.reason == "venue_unresolved"


def test_the_base_location_itself_reserves_no_travel(settings_obj):
    plan = travel.plan_travel(
        _resolution(*BASE), attendance_mode="in_person", settings=settings_obj
    )
    assert not plan.required
    assert plan.reason == "at_base_location"


def test_a_nearby_campus_building_gets_a_short_walking_buffer(settings_obj):
    """The Eynon Ballroom is ~430 m from base: a ten-minute walk, not a drive."""
    plan = travel.plan_travel(
        _resolution(*CHAMBERLAIN), attendance_mode="in_person", settings=settings_obj
    )
    assert plan.required
    assert plan.mode == travel.MODE_WALK
    assert plan.minutes_before == plan.minutes_after == 10
    assert not plan.estimated
    assert 400 < plan.distance_metres < 500


def test_a_walk_across_campus_is_longer_but_still_a_walk(settings_obj):
    plan = travel.plan_travel(
        _resolution(*BUNCE), attendance_mode="in_person", settings=settings_obj
    )
    assert plan.mode == travel.MODE_WALK
    assert 10 <= plan.minutes_before <= 20


def test_an_off_campus_destination_uses_the_route_time(settings_obj):
    calls = []

    def router(origin, destination, *, settings):
        calls.append((origin, destination))
        return 1638.5  # the real OSRM answer for base -> Camden

    plan = travel.plan_travel(
        _resolution(*CMSRU_CAMDEN, name="Medical Education Building"),
        attendance_mode="in_person", settings=settings_obj, router=router,
    )
    assert len(calls) == 1
    assert plan.mode == travel.MODE_DRIVE
    assert not plan.estimated
    assert plan.routing_source == "osrm"
    # ~27 min drive plus arrival/parking margin, rounded to 5-minute blocks.
    assert plan.minutes_before == 40
    assert plan.minutes_after == 35


def test_a_route_failure_falls_back_to_a_conservative_estimate(settings_obj):
    def broken(origin, destination, *, settings):
        raise OSError("router unavailable")

    plan = travel.plan_travel(
        _resolution(*CMSRU_CAMDEN), attendance_mode="in_person",
        settings=settings_obj, router=broken,
    )
    assert plan.required, "a routing outage must never suppress the travel block"
    assert plan.estimated
    assert plan.routing_source == "distance_estimate"
    assert plan.minutes_before > 0


def test_travel_blocks_are_rounded_and_capped(settings_obj):
    plan = travel.plan_travel(
        _resolution(40.7128, -74.0060, name="Manhattan"),
        attendance_mode="in_person", settings=settings_obj,
        router=lambda *a, **k: 60 * 60 * 6,
    )
    assert plan.minutes_before == settings_obj.calendar_travel_max_minutes
    assert plan.minutes_before % settings_obj.calendar_travel_rounding_minutes == 0


def test_travel_can_be_switched_off(settings_obj):
    import dataclasses

    disabled = dataclasses.replace(settings_obj, calendar_travel_enabled=False)
    plan = travel.plan_travel(
        _resolution(*CHAMBERLAIN), attendance_mode="in_person", settings=disabled
    )
    assert not plan.required
    assert plan.reason == "travel_disabled"


# --- the venue cache ---------------------------------------------------------


def test_a_cached_venue_is_reused_without_recomputing(landmarks_db, settings_obj):
    resolution = _resolution(*CMSRU_CAMDEN, name="Medical Education Building")
    fingerprint = travel.routing_fingerprint(settings_obj, resolution)
    db.upsert_venue(
        landmarks_db,
        {
            "normalized_venue": resolution.normalized_venue,
            "display_name": resolution.display_name,
            "campus": "camden", "latitude": CMSRU_CAMDEN[0],
            "longitude": CMSRU_CAMDEN[1], "source": travel.SOURCE_LANDMARK,
            "travel_mode": travel.MODE_DRIVE, "outbound_minutes": 40,
            "return_minutes": 35, "distance_metres": 26400.0,
            "routing_source": "osrm", "routing_fingerprint": fingerprint,
            "routing_estimated": False,
        },
    )
    landmarks_db.commit()

    row = db.venue_by_normalized(landmarks_db, resolution.normalized_venue)
    assert travel.cache_is_fresh(row, settings_obj, fingerprint, today=date.today())
    plan = travel.plan_from_cache(row)
    assert plan.minutes_before == 40
    assert plan.minutes_after == 35
    assert plan.reason == "venue_cache_hit"


def test_a_stale_cache_entry_is_not_reused(landmarks_db, settings_obj):
    resolution = _resolution(*CHAMBERLAIN, name="Chamberlain Student Center")
    fingerprint = travel.routing_fingerprint(settings_obj, resolution)
    db.upsert_venue(
        landmarks_db,
        {
            "normalized_venue": resolution.normalized_venue,
            "display_name": resolution.display_name, "campus": "glassboro",
            "latitude": CHAMBERLAIN[0], "longitude": CHAMBERLAIN[1],
            "source": travel.SOURCE_LANDMARK, "travel_mode": travel.MODE_WALK,
            "outbound_minutes": 10, "return_minutes": 10,
            "routing_fingerprint": fingerprint,
        },
    )
    landmarks_db.execute(
        "UPDATE event_venues SET last_verified_at = ?",
        ((date.today() - timedelta(days=400)).isoformat(),),
    )
    landmarks_db.commit()
    row = db.venue_by_normalized(landmarks_db, resolution.normalized_venue)
    assert not travel.cache_is_fresh(row, settings_obj, fingerprint, today=date.today())


def test_changing_the_base_location_invalidates_the_cached_travel_time(settings_obj):
    import dataclasses

    resolution = _resolution(*CHAMBERLAIN)
    original = travel.routing_fingerprint(settings_obj, resolution)
    moved = dataclasses.replace(settings_obj, calendar_base_latitude=39.8)
    assert travel.routing_fingerprint(moved, resolution) != original


def test_upsert_never_downgrades_a_good_cached_travel_time(landmarks_db, settings_obj):
    record = {
        "normalized_venue": "bunce hall", "display_name": "Bunce Hall",
        "campus": "glassboro", "latitude": BUNCE[0], "longitude": BUNCE[1],
        "source": travel.SOURCE_LANDMARK, "travel_mode": travel.MODE_WALK,
        "outbound_minutes": 15, "return_minutes": 15,
        "routing_fingerprint": "abc",
    }
    db.upsert_venue(landmarks_db, record)
    # A later resolution that computed no travel must leave the numbers alone.
    db.upsert_venue(
        landmarks_db,
        {k: v for k, v in record.items() if k not in ("outbound_minutes",
                                                      "return_minutes")},
    )
    landmarks_db.commit()
    row = db.venue_by_normalized(landmarks_db, "bunce hall")
    assert row["outbound_minutes"] == 15
    assert row["return_minutes"] == 15
