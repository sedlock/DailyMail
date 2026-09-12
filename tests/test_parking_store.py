"""Schema upgrade, bootstrap, upsert, aliases, overrides, fingerprints, backup.

Everything here runs against a real SQLite database in a temp directory and the
snapshotted authoritative sources, so the whole refresh path is exercised without
a network call or a subprocess.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from dailymail import db, maintenance, parking, parking_refresh, parking_sources, parking_store
from dailymail.parking_store import LocationRecord

from conftest import (
    PARKING_FIXTURE_DIR,
    OfflineParkingFetcher,
    stub_description_runner,
)


def make_record(**overrides) -> LocationRecord:
    values = dict(
        canonical_id="glassboro:lot:test-1",
        campus="glassboro",
        canonical_name="Lot Test-1",
        location_type="surface_lot",
        permit_class="Employee",
        latitude=39.7125,
        longitude=-75.1205,
        source_id="glassboro-mymaps",
        source_type="google_my_maps_kml",
        source_url="https://example.rowan.edu/map",
        source_fingerprint="abc",
        confidence="high",
    )
    values.update(overrides)
    return LocationRecord(**values)


# --- schema ------------------------------------------------------------------


def test_schema_version_is_current_and_parking_tables_exist(settings_obj):
    connection = db.connect()
    assert db.initialize(connection) == db.SCHEMA_VERSION
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    assert {
        "parking_sources",
        "parking_locations",
        "parking_aliases",
        "announcement_parking_locations",
        "parking_unresolved",
    } <= tables
    columns = {row["name"] for row in connection.execute("PRAGMA table_info(runs)")}
    assert "parking_stats" in columns
    connection.close()


def test_initialize_is_idempotent(settings_obj):
    connection = db.connect()
    assert db.initialize(connection) == db.SCHEMA_VERSION
    assert db.initialize(connection) == db.SCHEMA_VERSION
    assert db.initialize(connection) == db.SCHEMA_VERSION
    connection.close()


def test_upgrade_from_a_real_v1_database_preserves_history(settings_obj, tmp_path):
    """The production upgrade path: v1 with announcements in it -> v2."""
    path = tmp_path / "v1.sqlite3"
    connection = db.connect(path)
    # Build a v1 database exactly as the previous release would have.
    with db.transaction(connection):
        connection.executescript(db.SCHEMA)
        connection.execute(
            "INSERT INTO schema_meta (key, value) VALUES ('schema_version', '1')"
        )
        connection.execute(
            "INSERT INTO categories (category_id, title, first_seen_at, last_seen_at) "
            "VALUES (3, 'Public Safety', 'x', 'x')"
        )
        connection.execute(
            "INSERT INTO announcements (submission_id, source_audience, category_id, "
            "first_observed_at, last_observed_at, official_url) "
            "VALUES (6622, 'Both', 3, 'x', 'x', 'https://example')"
        )
        connection.execute(
            "INSERT INTO runs (target_date, started_at, status) "
            "VALUES ('2026-08-20', 'x', 'success')"
        )
    assert db.schema_version(connection) == 1

    assert db.initialize(connection) == db.SCHEMA_VERSION
    assert db.schema_version(connection) == db.SCHEMA_VERSION
    # History intact.
    assert connection.execute(
        "SELECT COUNT(*) FROM announcements WHERE submission_id = 6622"
    ).fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 1
    assert connection.execute("SELECT COUNT(*) FROM categories").fetchone()[0] == 1
    # New surface present.
    assert connection.execute("SELECT COUNT(*) FROM parking_locations").fetchone()[0] == 0
    assert "parking_stats" in {
        row["name"] for row in connection.execute("PRAGMA table_info(runs)")
    }
    assert connection.execute(
        "SELECT value FROM schema_meta WHERE key = 'upgraded_at'"
    ).fetchone() is not None
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_a_newer_schema_is_refused(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    connection.execute("UPDATE schema_meta SET value = '99' WHERE key = 'schema_version'")
    connection.commit()
    with pytest.raises(RuntimeError, match="newer than this build"):
        db.initialize(connection)
    connection.close()


# --- bootstrap ---------------------------------------------------------------


def test_bootstrap_populates_all_three_campuses(parking_cache):
    stats = parking_store.statistics(parking_cache)
    assert stats["locations"] >= 50
    assert set(stats["by_campus"]) >= {"glassboro", "stratford", "camden"}
    assert stats["by_campus"]["glassboro"]["locations"] == 38
    assert stats["by_campus"]["stratford"]["locations"] == 11
    assert stats["by_campus"]["camden"]["locations"] == 4
    # Every cached facility has a coordinate; that is the caching bar.
    assert stats["with_coordinates"] == stats["locations"]
    assert stats["aliases"] > stats["locations"] * 3


def test_bootstrap_is_idempotent(settings_obj, parking_fetcher):
    connection = db.connect()
    db.initialize(connection)
    first = parking_refresh.refresh(
        connection, settings_obj, fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
    )
    before = parking_store.statistics(connection)
    second = parking_refresh.refresh(
        connection, settings_obj, fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
    )
    after = parking_store.statistics(connection)

    assert first.locations_touched > 0
    # Nothing changed upstream, so the second pass writes nothing at all.
    assert second.locations_touched == 0
    assert all(entry.status == "unchanged" for entry in second.sources)
    assert second.descriptions_written == 0
    assert after["locations"] == before["locations"]
    assert after["aliases"] == before["aliases"]
    assert after["with_description"] == before["with_description"]
    connection.close()


def test_bootstrap_records_provenance_for_every_location(parking_cache):
    for row in parking_store.all_locations(parking_cache):
        assert row["source_id"]
        assert row["source_type"]
        assert row["source_url"]
        assert row["provenance"]
        assert row["confidence"] in parking.CONFIDENCE_ORDER
        assert row["first_discovered_at"] and row["last_verified_at"]


def test_o1_is_cached_from_official_rowan_geometry(parking_cache):
    row = parking_store.location_by_canonical_id(parking_cache, "glassboro:lot:o-1")
    assert row is not None
    assert row["canonical_name"] == "Lot O-1"
    assert row["campus"] == "glassboro"
    assert row["permit_class"] == "Employee"
    assert row["location_type"] == "surface_lot"
    assert row["confidence"] == "high"
    assert row["latitude"] == pytest.approx(39.712482, abs=1e-6)
    assert row["longitude"] == pytest.approx(-75.120453, abs=1e-6)
    assert row["source_type"] == "google_my_maps_kml"
    assert "1c2Qlz4nAV57oTio6HbOTgmYTwOoqimKW" in row["source_url"]


def test_lot_h_listed_under_two_use_classes_becomes_mixed(parking_cache):
    """Rowan's layer files Lot H under both Employee and Visitor parking."""
    row = parking_store.location_by_canonical_id(parking_cache, "glassboro:lot:h")
    assert row["permit_class"] == "Mixed"


def test_garages_are_typed_as_garages(parking_cache):
    for canonical_id in (
        "glassboro:garage:rowan-boulevard",
        "glassboro:garage:townhouse",
        "camden:garage:medical-school",
    ):
        row = parking_store.location_by_canonical_id(parking_cache, canonical_id)
        assert row is not None, canonical_id
        assert row["location_type"] == "garage"


def test_stratford_patient_lot_is_typed_and_classified(parking_cache):
    row = parking_store.location_by_canonical_id(parking_cache, "stratford:lot:a")
    assert row["location_type"] == "patient_lot"
    assert row["permit_class"] == "Patient"


def test_unnamed_parking_placemarks_are_counted_not_invented(settings_obj, parking_fetcher):
    """Stratford's My Maps parking points have a use class but no lot letter."""
    connection = db.connect()
    db.initialize(connection)
    outcome = parking_refresh.refresh(
        connection, settings_obj, fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
    )
    by_id = {entry.source_id: entry for entry in outcome.sources}
    assert by_id["stratford-mymaps"].unnamed_parking == 13
    assert by_id["stratford-mymaps"].locations_seen == 0
    assert by_id["sewell-mymaps"].unnamed_parking == 2
    # Sewell's unnamed points are not turned into invented facilities.
    assert parking_store.all_locations(connection, campus="sewell") == []
    connection.close()


# --- upsert and aliases ------------------------------------------------------


def test_upsert_inserts_then_reports_unchanged(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    with db.transaction(connection):
        _, action = parking_store.upsert_location(connection, make_record())
    assert action == "inserted"
    with db.transaction(connection):
        _, action = parking_store.upsert_location(connection, make_record())
    assert action == "unchanged"
    with db.transaction(connection):
        _, action = parking_store.upsert_location(
            connection, make_record(permit_class="Commuter")
        )
    assert action == "updated"
    row = parking_store.location_by_canonical_id(connection, "glassboro:lot:test-1")
    assert row["permit_class"] == "Commuter"
    connection.close()


def test_upsert_writes_normalized_aliases(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    with db.transaction(connection):
        location_id, _ = parking_store.upsert_location(connection, make_record())
    aliases = {
        row["normalized_alias"]
        for row in connection.execute(
            "SELECT normalized_alias FROM parking_aliases WHERE location_id = ?",
            (location_id,),
        )
    }
    assert "lot test 1" in aliases
    assert "parking lot test 1" in aliases
    assert "test 1 lot" in aliases
    connection.close()


def test_an_alias_is_unique_within_a_campus(settings_obj):
    """Two Glassboro lots may not both claim `lot a`; the first keeps it."""
    connection = db.connect()
    db.initialize(connection)
    with db.transaction(connection):
        first, _ = parking_store.upsert_location(
            connection,
            make_record(canonical_id="glassboro:lot:a", canonical_name="Lot A"),
        )
        parking_store.upsert_location(
            connection,
            make_record(
                canonical_id="glassboro:lot:a-copy",
                canonical_name="Lot A",
                latitude=39.7130,
            ),
        )
    owners = list(
        connection.execute(
            "SELECT location_id FROM parking_aliases WHERE campus = 'glassboro' "
            "AND normalized_alias = 'lot a'"
        )
    )
    assert len(owners) == 1
    assert owners[0]["location_id"] == first
    connection.close()


def test_the_same_alias_may_exist_on_two_campuses(parking_cache):
    """`Lot A` really does exist at Glassboro and Stratford. Both are kept."""
    index = parking_store.alias_index(parking_cache)
    campuses = {row["campus"] for row in index["lot a"]}
    assert campuses == {"glassboro", "stratford"}


def test_campus_aware_uniqueness_of_canonical_names(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    with db.transaction(connection):
        parking_store.upsert_location(
            connection, make_record(canonical_id="glassboro:lot:a", canonical_name="Lot A")
        )
        parking_store.upsert_location(
            connection,
            make_record(
                canonical_id="stratford:lot:a",
                campus="stratford",
                canonical_name="Lot A",
                latitude=39.8302355,
                longitude=-75.0068015,
            ),
        )
    assert len(parking_store.find_locations(connection, "Lot A")) == 2
    assert len(parking_store.find_locations(connection, "Lot A", campus="stratford")) == 1
    connection.close()


def test_scannable_flag_is_stored_for_the_alias_scan(parking_cache):
    row = parking_cache.execute(
        "SELECT scannable FROM parking_aliases WHERE campus='glassboro' "
        "AND normalized_alias='lot a'"
    ).fetchone()
    assert row["scannable"] == 0
    row = parking_cache.execute(
        "SELECT scannable FROM parking_aliases WHERE campus='glassboro' "
        "AND normalized_alias='lot o 1'"
    ).fetchone()
    assert row["scannable"] == 1


def test_a_record_with_impossible_coordinates_is_refused(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    with pytest.raises(parking.ParkingDataError):
        with db.transaction(connection):
            parking_store.upsert_location(
                connection, make_record(latitude=51.5074, longitude=-0.1278)
            )
    assert parking_store.all_locations(connection) == []
    connection.close()


@pytest.mark.parametrize(
    "overrides",
    [
        {"location_type": "hovercraft_pad"},
        {"permit_class": "Wizard"},
        {"campus": "hogwarts"},
        {"confidence": "absolute"},
    ],
)
def test_a_record_with_an_unknown_enum_is_refused(settings_obj, overrides):
    connection = db.connect()
    db.initialize(connection)
    with pytest.raises(parking.ParkingDataError):
        with db.transaction(connection):
            parking_store.upsert_location(connection, make_record(**overrides))
    connection.close()


# --- manual overrides --------------------------------------------------------


def test_manual_override_survives_a_source_refresh(settings_obj, parking_fetcher):
    connection = db.connect()
    db.initialize(connection)
    parking_refresh.refresh(
        connection, settings_obj, fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
    )
    corrected = "Employee lot immediately west of James Hall, south of Richard Wackar Stadium."
    parking_store.set_manual_override(
        connection,
        "glassboro:lot:o-1",
        {"description": corrected, "latitude": 39.71250, "longitude": -75.12040},
    )

    # The source changes upstream and every lot is re-fetched.
    mutated = (PARKING_FIXTURE_DIR / "glassboro-mymaps.kml").read_bytes().replace(
        b"<name>Main Campus | Glassboro</name>", b"<name>Main Campus Glassboro v2</name>"
    )
    parking_refresh.refresh(
        connection,
        settings_obj,
        fetcher=OfflineParkingFetcher(mutate={"glassboro-mymaps": mutated}),
        description_runner=stub_description_runner(),
    )

    row = parking_store.location_by_canonical_id(connection, "glassboro:lot:o-1")
    assert row["description"] == corrected
    assert row["latitude"] == pytest.approx(39.71250)
    assert row["longitude"] == pytest.approx(-75.12040)
    assert row["manual_override"] == 1
    assert set(json.loads(row["override_fields"])) == {
        "description", "latitude", "longitude"
    }
    # Non-pinned fields still track the source.
    assert row["source_fingerprint"] != "abc"
    connection.close()


def test_refresh_reports_that_it_preserved_an_override(settings_obj, parking_fetcher):
    connection = db.connect()
    db.initialize(connection)
    parking_refresh.refresh(
        connection, settings_obj, fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
    )
    parking_store.set_manual_override(
        connection, "glassboro:lot:o-1", {"permit_class": "Visitor"}
    )
    mutated = (PARKING_FIXTURE_DIR / "glassboro-mymaps.kml").read_bytes() + b"<!-- v2 -->"
    outcome = parking_refresh.refresh(
        connection,
        settings_obj,
        fetcher=OfflineParkingFetcher(mutate={"glassboro-mymaps": mutated}),
        description_runner=stub_description_runner(),
    )
    glassboro = next(e for e in outcome.sources if e.source_id == "glassboro-mymaps")
    assert glassboro.overrides_preserved == 1
    row = parking_store.location_by_canonical_id(connection, "glassboro:lot:o-1")
    assert row["permit_class"] == "Visitor"
    connection.close()


def test_clearing_an_override_lets_refresh_win_again(settings_obj, parking_fetcher):
    connection = db.connect()
    db.initialize(connection)
    parking_refresh.refresh(
        connection, settings_obj, fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
    )
    parking_store.set_manual_override(
        connection, "glassboro:lot:o-1", {"permit_class": "Visitor"}
    )
    parking_store.clear_manual_override(connection, "glassboro:lot:o-1")
    mutated = (PARKING_FIXTURE_DIR / "glassboro-mymaps.kml").read_bytes() + b"<!-- v3 -->"
    parking_refresh.refresh(
        connection,
        settings_obj,
        fetcher=OfflineParkingFetcher(mutate={"glassboro-mymaps": mutated}),
        description_runner=stub_description_runner(),
    )
    row = parking_store.location_by_canonical_id(connection, "glassboro:lot:o-1")
    assert row["permit_class"] == "Employee"
    assert row["manual_override"] == 0
    connection.close()


def test_override_rejects_an_unknown_field_and_a_bad_coordinate(parking_cache):
    with pytest.raises(parking.ParkingDataError, match="cannot override"):
        parking_store.set_manual_override(
            parking_cache, "glassboro:lot:o-1", {"source_url": "https://evil"}
        )
    with pytest.raises(parking.ParkingDataError):
        parking_store.set_manual_override(
            parking_cache, "glassboro:lot:o-1", {"latitude": 0.0, "longitude": 0.0}
        )


def test_a_pinned_description_is_not_replaced_by_the_writer(parking_cache):
    corrected = "Employee lot west of James Hall and south of Richard Wackar Stadium."
    parking_store.set_manual_override(
        parking_cache, "glassboro:lot:o-1", {"description": corrected}
    )
    parking_store.set_description(
        parking_cache, "glassboro:lot:o-1", "Something else entirely near James Hall.",
        method="agent",
    )
    row = parking_store.location_by_canonical_id(parking_cache, "glassboro:lot:o-1")
    assert row["description"] == corrected


def test_manual_alias_can_be_added(parking_cache):
    row = parking_store.location_by_canonical_id(parking_cache, "glassboro:lot:o-1")
    with db.transaction(parking_cache):
        added = parking_store.add_aliases(
            parking_cache, int(row["location_id"]), "glassboro",
            ["The O One Lot"], origin="manual",
        )
    assert added == 1
    index = parking_store.alias_index(parking_cache)
    assert index[parking.normalize_name("The O One Lot")][0]["canonical_id"] == (
        "glassboro:lot:o-1"
    )


# --- sources and fingerprints ------------------------------------------------


def test_every_registered_source_is_recorded(parking_cache):
    recorded = {row["source_id"] for row in parking_store.source_rows(parking_cache)}
    assert recorded == {source.source_id for source in parking_sources.SOURCES}


def test_fingerprint_change_moves_last_changed_but_unchanged_does_not(
    settings_obj, parking_fetcher
):
    connection = db.connect()
    db.initialize(connection)
    parking_refresh.refresh(
        connection, settings_obj, source_ids=["glassboro-mymaps"],
        fetcher=parking_fetcher, describe=False,
    )
    first = dict(parking_store.source_row(connection, "glassboro-mymaps"))

    parking_refresh.refresh(
        connection, settings_obj, source_ids=["glassboro-mymaps"],
        fetcher=OfflineParkingFetcher(), describe=False,
    )
    same = dict(parking_store.source_row(connection, "glassboro-mymaps"))
    assert same["fingerprint"] == first["fingerprint"]
    assert same["last_changed_at"] == first["last_changed_at"]
    assert same["last_status"] == "unchanged"

    mutated = (PARKING_FIXTURE_DIR / "glassboro-mymaps.kml").read_bytes() + b"<!-- x -->"
    parking_refresh.refresh(
        connection, settings_obj, source_ids=["glassboro-mymaps"],
        fetcher=OfflineParkingFetcher(mutate={"glassboro-mymaps": mutated}),
        describe=False,
    )
    changed = dict(parking_store.source_row(connection, "glassboro-mymaps"))
    assert changed["fingerprint"] != first["fingerprint"]
    assert changed["last_status"] == "ok"
    connection.close()


def test_a_hand_derived_source_that_changes_is_flagged_not_regenerated(
    settings_obj, parking_fetcher
):
    connection = db.connect()
    db.initialize(connection)
    parking_refresh.refresh(
        connection, settings_obj, fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
    )
    before = dict(parking_store.location_by_canonical_id(connection, "stratford:lot:d-3"))

    outcome = parking_refresh.refresh(
        connection,
        settings_obj,
        fetcher=OfflineParkingFetcher(
            mutate={"stratford-som-campus-map": b"a new Stratford campus map PDF"}
        ),
        description_runner=stub_description_runner(),
    )
    entry = next(e for e in outcome.sources if e.source_id == "stratford-som-campus-map")
    assert entry.status == "changed_review"
    assert outcome.review_needed == ["stratford-som-campus-map"]
    after = dict(parking_store.location_by_canonical_id(connection, "stratford:lot:d-3"))
    assert after["latitude"] == before["latitude"]
    assert after["description"] == before["description"]
    connection.close()


def test_a_source_outage_does_not_stop_the_others(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    outcome = parking_refresh.refresh(
        connection,
        settings_obj,
        fetcher=OfflineParkingFetcher(fail={"glassboro-mymaps"}),
        description_runner=stub_description_runner(),
    )
    failed = next(e for e in outcome.sources if e.source_id == "glassboro-mymaps")
    assert failed.status == "error"
    assert "simulated outage" in failed.error
    # Stratford and Camden still landed.
    assert parking_store.all_locations(connection, campus="stratford")
    assert parking_store.all_locations(connection, campus="camden")
    row = parking_store.source_row(connection, "glassboro-mymaps")
    assert row["last_status"] == "error"
    connection.close()


def test_malformed_source_bytes_are_reported_not_crashed(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    outcome = parking_refresh.refresh(
        connection,
        settings_obj,
        fetcher=OfflineParkingFetcher(mutate={"glassboro-mymaps": b"<kml><oops"}),
        description_runner=stub_description_runner(),
    )
    entry = next(e for e in outcome.sources if e.source_id == "glassboro-mymaps")
    assert entry.status == "error"
    assert "KML" in entry.error or "malformed" in entry.error
    connection.close()


def test_stale_source_determination(settings_obj, parking_cache):
    assert parking_store.stale_sources(parking_cache, max_age_days=180) == []
    parking_cache.execute(
        "UPDATE parking_sources SET last_verified_at = '2020-01-01T00:00:00+00:00' "
        "WHERE source_id = 'glassboro-mymaps'"
    )
    parking_cache.commit()
    assert parking_store.stale_sources(parking_cache, max_age_days=180) == [
        "glassboro-mymaps"
    ]
    assert parking_store.stale_sources(
        parking_cache, max_age_days=180, campus="camden"
    ) == []


def test_a_never_checked_source_counts_as_stale(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    assert parking_store.stale_sources(connection, max_age_days=180) == []
    with db.transaction(connection):
        parking_store.upsert_source(
            connection, source_id="x", campus="glassboro", source_type="t",
            source_url="https://x",
        )
    connection.execute("UPDATE parking_sources SET last_verified_at = NULL")
    connection.commit()
    assert parking_store.stale_sources(connection, max_age_days=180) == ["x"]
    connection.close()


# --- unresolved bookkeeping --------------------------------------------------


def test_unresolved_candidates_accumulate_and_clear(parking_cache):
    with db.transaction(parking_cache):
        parking_store.record_unresolved(
            parking_cache, normalized_match="lot q 7", matched_text="Lot Q-7",
            campus_hint="glassboro", reason="not found",
        )
        parking_store.record_unresolved(
            parking_cache, normalized_match="lot q 7", matched_text="Lot Q-7",
            campus_hint="glassboro", reason="still not found", resolver_called=True,
        )
    rows = parking_store.unresolved_rows(parking_cache)
    assert len(rows) == 1
    assert rows[0]["attempts"] == 2
    assert rows[0]["resolver_calls"] == 1
    with db.transaction(parking_cache):
        parking_store.clear_unresolved(parking_cache, "lot q 7")
    assert parking_store.unresolved_rows(parking_cache) == []


def test_association_upsert_is_stable_per_date_and_match(parking_cache):
    row = parking_store.location_by_canonical_id(parking_cache, "glassboro:lot:o-1")
    # The association table references a real announcement, so seed one.
    with db.transaction(parking_cache):
        parking_cache.execute(
            "INSERT INTO announcements (submission_id, source_audience, "
            "first_observed_at, last_observed_at, official_url) "
            "VALUES (6622, 'Both', 'x', 'x', 'https://example')"
        )
    for method in ("cache_pattern_lot_prefix", "cache_alias_scan"):
        with db.transaction(parking_cache):
            parking_store.record_association(
                parking_cache,
                target_date="2026-08-20",
                submission_id=6622,
                normalized_match="lot o 1",
                matched_text="Parking Lot O-1",
                match_method=method,
                location_id=int(row["location_id"]),
            )
    associations = parking_store.associations_for_date(parking_cache, "2026-08-20")
    assert len(associations) == 1
    assert associations[0]["match_method"] == "cache_alias_scan"


# --- backup ------------------------------------------------------------------


def test_database_backup_includes_the_parking_schema_and_data(parking_cache, settings_obj):
    backup_path = maintenance.backup_database(parking_cache)
    assert backup_path.exists()
    restored = sqlite3.connect(str(backup_path))
    restored.row_factory = sqlite3.Row
    try:
        assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert int(
            restored.execute("SELECT value FROM schema_meta WHERE key='schema_version'")
            .fetchone()[0]
        ) == db.SCHEMA_VERSION
        assert restored.execute("SELECT COUNT(*) FROM parking_locations").fetchone()[0] > 40
        assert restored.execute("SELECT COUNT(*) FROM parking_aliases").fetchone()[0] > 100
        assert restored.execute("SELECT COUNT(*) FROM parking_sources").fetchone()[0] == len(
            parking_sources.SOURCES
        )
        row = restored.execute(
            "SELECT * FROM parking_locations WHERE canonical_id = 'glassboro:lot:o-1'"
        ).fetchone()
        assert row["latitude"] == pytest.approx(39.712482, abs=1e-6)
    finally:
        restored.close()


def test_statistics_surface_the_numbers_the_cli_prints(parking_cache):
    stats = parking_store.statistics(parking_cache)
    for key in (
        "locations", "aliases", "with_coordinates", "with_description",
        "manual_overrides", "unresolved", "by_campus", "aliases_by_campus",
        "by_type", "by_permit", "oldest_verification", "last_source_refresh",
        "sources",
    ):
        assert key in stats
    assert stats["oldest_verification"]["at"]
    assert stats["last_source_refresh"]


def test_db_statistics_include_parking_counts(parking_cache):
    stats = db.statistics(parking_cache)
    assert stats["parking_locations"] > 40
    assert stats["parking_aliases"] > 100
    assert stats["parking_sources"] == len(parking_sources.SOURCES)
    assert stats["parking_unresolved"] == 0


# --- security sweep over the new tables --------------------------------------


def test_no_sensitive_data_anywhere_in_the_parking_tables(parking_cache):
    """The Phase 1 boundary sweep, applied to the reference cache.

    Parking data comes from public maps, so nothing sensitive should be able to
    reach it -- but the catalog is written by an agent, so the assertion is worth
    making rather than assuming.
    """
    import re

    banner = re.compile(r"\b9\d{8}\b")
    email = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
    forbidden = (
        "External_Id", "SubmittedByExternalId", "ApproverExternalId", "Password",
        "Last_Login", "rolesInfo", "GMAIL_APP_PASSWORD", "GMAIL_SMTP_USER",
        "abcd efgh ijkl mnop",  # the fake credential the test harness uses
    )
    tables = [
        row["name"]
        for row in parking_cache.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'parking%'"
        )
    ] + ["announcement_parking_locations"]
    assert len(tables) == 6

    for table in tables:
        for row in parking_cache.execute(f"SELECT * FROM {table}"):
            for key in row.keys():
                value = row[key]
                if not isinstance(value, str):
                    continue
                for banned in forbidden:
                    assert banned not in value, (table, key, banned)
                assert not banner.search(value), (table, key, "banner-id-shaped value")
                assert not email.search(value), (table, key, "email address")


def test_parking_column_names_contain_no_forbidden_fields(parking_cache):
    for table in (
        "parking_locations", "parking_aliases", "parking_sources",
        "parking_landmarks", "parking_unresolved",
        "announcement_parking_locations",
    ):
        columns = {
            row[1].lower()
            for row in parking_cache.execute(f"PRAGMA table_info({table})")
        }
        for banned in (
            "external_id", "password", "last_login", "username", "rolesinfo",
            "banner_id", "contact_email", "recipient", "credentials",
        ):
            assert banned not in columns, (table, banned)


def test_every_cached_source_url_is_https_and_rowan_or_google_maps(parking_cache):
    """Nothing in the catalog should point anywhere unexpected."""
    from urllib.parse import urlparse

    allowed = {
        "sites.rowan.edu", "www.rowan.edu", "som.rowan.edu", "cmsru.rowan.edu",
        "www.google.com",
    }
    for row in parking_store.all_locations(parking_cache):
        parsed = urlparse(row["source_url"] or "")
        assert parsed.scheme == "https", row["canonical_id"]
        assert parsed.hostname in allowed, (row["canonical_id"], parsed.hostname)
    for row in parking_store.source_rows(parking_cache):
        parsed = urlparse(row["source_url"])
        assert parsed.scheme == "https"
        assert parsed.hostname in allowed


def test_the_catalog_never_stores_an_opaque_map_share_link(parking_cache):
    """Map links are generated from coordinates, so no share URL may be cached."""
    for row in parking_store.all_locations(parking_cache):
        blob = " ".join(
            str(row[key] or "") for key in ("description", "provenance", "source_url")
        )
        for opaque in ("goo.gl", "maps.app.goo.gl", "/maps/place/", "/maps/@"):
            assert opaque not in blob, (row["canonical_id"], opaque)
