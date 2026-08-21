"""SQLite schema, idempotent import, versioning and change detection."""

from __future__ import annotations

import json
import sqlite3

import pytest

from dailymail import db, ingest
from dailymail.errors import ValidationError
from dailymail.normalize import FORBIDDEN_KEYS

from conftest import TARGET_DATE, artifact_from_fixtures


# --- initialization ----------------------------------------------------------


def test_initialize_creates_schema_and_version(settings_obj):
    connection = db.connect()
    assert db.initialize(connection) == db.SCHEMA_VERSION
    tables = {
        row["name"]
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    for expected in (
        "schema_meta", "categories", "announcements", "announcement_versions",
        "distribution_dates", "daily_presence", "daily_records", "runs",
        "curation_results", "deliveries",
    ):
        assert expected in tables, expected
    connection.close()


def test_initialize_is_idempotent(settings_obj):
    connection = db.connect()
    assert db.initialize(connection) == 1
    assert db.initialize(connection) == 1
    rows = connection.execute(
        "SELECT COUNT(*) FROM schema_meta WHERE key='schema_version'"
    ).fetchone()[0]
    assert rows == 1
    connection.close()


def test_pragmas_are_set(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] >= 1000
    connection.close()


def test_newer_schema_version_is_refused(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    connection.execute(
        "UPDATE schema_meta SET value='99' WHERE key='schema_version'"
    )
    connection.commit()
    with pytest.raises(RuntimeError, match="newer than this build"):
        db.initialize(connection)
    connection.close()


def test_foreign_keys_enforced(populated_db):
    with pytest.raises(sqlite3.IntegrityError):
        with db.transaction(populated_db):
            populated_db.execute(
                "INSERT INTO daily_presence (target_date, submission_id, source_view, "
                "observed_at) VALUES ('2026-08-20', 99999999, 'Employee', 'now')"
            )


# --- import ------------------------------------------------------------------


def test_import_from_fixtures(populated_db):
    stats = db.statistics(populated_db)
    assert stats["announcements"] == 13
    assert stats["versions"] == 13
    assert stats["categories"] == 33
    # 13 employee-view + 3 student-view observations
    assert stats["observations"] == 16


def test_import_is_idempotent(settings_obj, employee_fixture, student_fixture):
    connection = db.connect()
    db.initialize(connection)
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)

    first = ingest.ingest_artifact(connection, artifact, settings_obj, origin="a")
    before = db.statistics(connection)
    second = ingest.ingest_artifact(connection, artifact, settings_obj, origin="b")
    after = db.statistics(connection)

    assert first["new_versions"] == 13
    assert second["new_versions"] == 0, "re-import must not create versions"
    assert second["changed_count"] == 0
    assert before == after, "re-import must not change any counts"
    connection.close()


def test_import_rejects_malformed_artifacts(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    for bad, match in [
        ({}, "missing keys"),
        ({"schema_version": 2, "target_date": "2026-08-20", "counts": {},
          "source_counts": {}, "category_registry": [{"id": 1}], "announcements": [],
          "validation": {}}, "unsupported collection schema_version"),
    ]:
        with pytest.raises(ValidationError, match=match):
            ingest.ingest_artifact(connection, bad, settings_obj)
    connection.close()


def test_import_rejects_empty_category_registry(settings_obj, employee_fixture, student_fixture):
    """A valid empty day still carries the registry; an empty one is not valid."""
    connection = db.connect()
    db.initialize(connection)
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    artifact["category_registry"] = []
    with pytest.raises(ValidationError, match="empty category registry"):
        ingest.ingest_artifact(connection, artifact, settings_obj)
    connection.close()


def test_import_rejects_forbidden_fields(settings_obj, employee_fixture, student_fixture):
    connection = db.connect()
    db.initialize(connection)
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    artifact["announcements"][0]["User"] = {"Username": "x", "External_Id": "910000000"}
    with pytest.raises(ValidationError, match="forbidden field"):
        ingest.ingest_artifact(connection, artifact, settings_obj)
    connection.close()


def test_import_rejects_record_missing_target_date(settings_obj, employee_fixture, student_fixture):
    connection = db.connect()
    db.initialize(connection)
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    artifact["announcements"][0]["distribution_dates"] = ["2026-09-01"]
    with pytest.raises(ValidationError, match="does not list"):
        ingest.ingest_artifact(connection, artifact, settings_obj)
    connection.close()


def test_import_rejects_duplicate_ids(settings_obj, employee_fixture, student_fixture):
    connection = db.connect()
    db.initialize(connection)
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    artifact["announcements"].append(dict(artifact["announcements"][0]))
    artifact["counts"]["unique"] = len(artifact["announcements"])
    with pytest.raises(ValidationError, match="duplicate submission_id"):
        ingest.ingest_artifact(connection, artifact, settings_obj)
    connection.close()


def test_import_existing_collections_skips_bad_files(settings_obj, tmp_path):
    from dailymail import config as collector_config

    directory = collector_config.collections_dir()
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "2026-01-01.json").write_text("{not json", encoding="utf-8")
    (directory / "2026-01-02.json").write_text(json.dumps({"nope": 1}), encoding="utf-8")

    connection = db.connect()
    db.initialize(connection)
    result = ingest.import_existing_collections(connection, settings_obj)
    assert result["imported"] == []
    assert len(result["skipped"]) == 2
    assert db.statistics(connection)["announcements"] == 0
    connection.close()


# --- categories --------------------------------------------------------------


def test_category_upsert_records_manual_priority(populated_db):
    row = populated_db.execute(
        "SELECT * FROM categories WHERE title = 'Official'"
    ).fetchone()
    assert row["manual_priority"] == 1
    row = populated_db.execute(
        "SELECT * FROM categories WHERE title = 'Public Safety'"
    ).fetchone()
    assert row["manual_priority"] == 5


def test_category_upsert_refreshes_without_duplicating(populated_db):
    before = populated_db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    with db.transaction(populated_db):
        db.upsert_category(
            populated_db, category_id=3, title="Public Safety Renamed",
            rowan_rank=9, color="red", is_active=True, manual_priority=5,
        )
    after = populated_db.execute("SELECT COUNT(*) FROM categories").fetchone()[0]
    assert after == before
    row = populated_db.execute(
        "SELECT * FROM categories WHERE category_id = 3"
    ).fetchone()
    assert row["title"] == "Public Safety Renamed"
    assert row["rowan_rank"] == 9


def test_unknown_category_is_ingested_and_operates_normally(
    settings_obj, employee_fixture, student_fixture
):
    """A category Rowan invents must work on its first day, never be dropped."""
    connection = db.connect()
    db.initialize(connection)
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    artifact["category_registry"].append(
        {"id": 4242, "title": "Emergency Operations", "rank": 0, "color": "red-dark"}
    )
    record = dict(artifact["announcements"][0])
    record["category_id"] = 4242
    record["category"] = {"id": 4242, "title": "Emergency Operations",
                          "rank": 0, "color": "red-dark"}
    artifact["announcements"][0] = record

    ingest.ingest_artifact(connection, artifact, settings_obj)
    row = connection.execute(
        "SELECT * FROM categories WHERE category_id = 4242"
    ).fetchone()
    assert row["title"] == "Emergency Operations"
    assert row["manual_priority"] is None       # not in the configured list
    assert row["inferred_priority"] is None     # not yet estimated

    rows = db.digest_rows(connection, TARGET_DATE)
    assert any(r["category_id"] == 4242 for r in rows), "announcement was dropped"
    connection.close()


def test_inferred_priority_never_overwrites_manual(populated_db):
    with db.transaction(populated_db):
        db.set_inferred_priority(populated_db, 2, 30)  # 'Official', manual 1
    row = populated_db.execute(
        "SELECT * FROM categories WHERE category_id = 2"
    ).fetchone()
    assert row["manual_priority"] == 1
    assert row["inferred_priority"] is None, "locked category must be untouched"


def test_inferred_priority_applies_to_unplaced_category(populated_db):
    with db.transaction(populated_db):
        db.upsert_category(
            populated_db, category_id=555, title="Brand New", rowan_rank=0,
            color="x", is_active=True, manual_priority=None,
        )
        db.set_inferred_priority(populated_db, 555, 14)
    row = populated_db.execute(
        "SELECT * FROM categories WHERE category_id = 555"
    ).fetchone()
    assert row["inferred_priority"] == 14


# --- versioning and change detection ----------------------------------------


def _record(**overrides) -> dict:
    base = {
        "submission_id": "9001",
        "title": "Original title",
        "source_audience": "Both",
        "category_id": 3,
        "submitted_status": "Approved",
        "full_body": "<p>Original body</p>",
        "body_text": "Original body",
        "distribution_dates": ["2026-08-20", "2026-08-21"],
        "first_distribution_date": "2026-08-20",
        "status": "New",
        "submitted_date": "2026-08-18T10:00:00Z",
        "approved_date": "2026-08-18T11:00:00Z",
        "contact_email": "x@rowan.edu",
        "is_event": False,
    }
    base.update(overrides)
    return base


def test_first_sighting_is_not_marked_changed(populated_db):
    with db.transaction(populated_db):
        version_id, changed = db.record_announcement(
            populated_db, _record(), observed_at="2026-08-20T00:00:00+00:00"
        )
    assert version_id > 0
    assert changed is False, "a first sighting is new, not 'updated'"


def test_identical_content_deduplicates_to_one_version(populated_db):
    with db.transaction(populated_db):
        first, _ = db.record_announcement(populated_db, _record(), observed_at="t1")
    with db.transaction(populated_db):
        second, changed = db.record_announcement(populated_db, _record(), observed_at="t2")
    assert first == second
    assert changed is False
    count = populated_db.execute(
        "SELECT COUNT(*) FROM announcement_versions WHERE submission_id = 9001"
    ).fetchone()[0]
    assert count == 1


def test_substantive_body_change_creates_version_and_marks_changed(populated_db):
    with db.transaction(populated_db):
        first, _ = db.record_announcement(populated_db, _record(), observed_at="t1")
    with db.transaction(populated_db):
        second, changed = db.record_announcement(
            populated_db,
            _record(full_body="<p>Body was revised with new details</p>"),
            observed_at="t2",
        )
    assert second != first
    assert changed is True
    count = populated_db.execute(
        "SELECT COUNT(*) FROM announcement_versions WHERE submission_id = 9001"
    ).fetchone()[0]
    assert count == 2
    current = populated_db.execute(
        "SELECT current_version_id FROM announcements WHERE submission_id = 9001"
    ).fetchone()[0]
    assert current == second


@pytest.mark.parametrize(
    "field,value",
    [
        ("title", "A different title"),
        ("category_id", 20),
        ("source_audience", "Employees"),
        ("contact_email", "different@rowan.edu"),
        ("distribution_dates", ["2026-08-20", "2026-08-21", "2026-08-30"]),
        ("is_event", True),
        ("event_location", "Bunce Hall"),
    ],
)
def test_user_visible_changes_are_substantive(populated_db, field, value):
    with db.transaction(populated_db):
        db.record_announcement(populated_db, _record(), observed_at="t1")
    with db.transaction(populated_db):
        _, changed = db.record_announcement(
            populated_db, _record(**{field: value}), observed_at="t2"
        )
    assert changed is True, f"{field} should count as substantive"


@pytest.mark.parametrize(
    "field,value",
    [
        ("submitted_date", "2026-08-19T10:00:00Z"),
        ("approved_date", "2026-08-19T11:00:00Z"),
        ("updated_date", "2026-08-19T12:00:00Z"),
        ("updated_by_name", "Someone Else"),
        ("body_text", "a differently derived plain text"),
        ("short_body", "a different teaser"),
        ("body_diagnostics", {"body_bytes": 4}),
        ("extra_edition", True),
    ],
)
def test_framework_and_timestamp_changes_are_not_substantive(populated_db, field, value):
    """An UpdatedDate touch alone must not manufacture an UPDATED badge."""
    with db.transaction(populated_db):
        db.record_announcement(populated_db, _record(), observed_at="t1")
    with db.transaction(populated_db):
        _, changed = db.record_announcement(
            populated_db, _record(**{field: value}), observed_at="t2"
        )
    assert changed is False, f"{field} must not create a content version"


def test_whitespace_only_change_is_not_substantive(populated_db):
    with db.transaction(populated_db):
        db.record_announcement(populated_db, _record(), observed_at="t1")
    with db.transaction(populated_db):
        _, changed = db.record_announcement(
            populated_db,
            _record(full_body="<p>Original    body</p>\n", title=" Original title "),
            observed_at="t2",
        )
    assert changed is False


def test_changed_flag_is_sticky_across_reruns(populated_db):
    with db.transaction(populated_db):
        v1, _ = db.record_announcement(populated_db, _record(), observed_at="t1")
        db.record_daily(
            populated_db, target_date="2026-08-21", submission_id=9001,
            version_id=v1, status="Standing", changed=False, observed_at="t1",
        )
    with db.transaction(populated_db):
        v2, changed = db.record_announcement(
            populated_db, _record(full_body="<p>revised</p>"), observed_at="t2"
        )
        db.record_daily(
            populated_db, target_date="2026-08-21", submission_id=9001,
            version_id=v2, status="Standing", changed=changed, observed_at="t2",
        )
    # Re-run the same day with no further change: the badge must survive.
    with db.transaction(populated_db):
        v3, changed_again = db.record_announcement(
            populated_db, _record(full_body="<p>revised</p>"), observed_at="t3"
        )
        db.record_daily(
            populated_db, target_date="2026-08-21", submission_id=9001,
            version_id=v3, status="Standing", changed=changed_again, observed_at="t3",
        )
    row = populated_db.execute(
        "SELECT changed FROM daily_records WHERE target_date='2026-08-21' "
        "AND submission_id=9001"
    ).fetchone()
    assert row["changed"] == 1


# --- persisted classification -----------------------------------------------


def test_new_standing_and_audience_persist(populated_db):
    counts = db.counts_for_date(populated_db, TARGET_DATE)
    assert counts["new"] == 5
    assert counts["standing"] == 8
    assert counts["unique"] == 13
    assert counts["everyone"] == 3
    assert counts["employee_only"] == 10
    assert counts["student_only"] == 0
    assert counts["employee_view"] == 13
    assert counts["student_view"] == 3


def test_status_matches_first_distribution_date(populated_db):
    for row in db.digest_rows(populated_db, TARGET_DATE):
        dates = json.loads(row["distribution_dates"])
        expected = "New" if min(dates) == TARGET_DATE else "Standing"
        assert row["status"] == expected, row["submission_id"]


def test_first_observed_at_is_separate_from_first_distribution(populated_db):
    row = populated_db.execute(
        "SELECT first_observed_at, first_distribution_date FROM announcements "
        "WHERE submission_id = 6476"
    ).fetchone()
    # Rowan first distributed this on 2026-08-04; DailyMail first saw it later.
    assert row["first_distribution_date"] == "2026-08-04"
    assert row["first_observed_at"].startswith("2026-08-21")


def test_distribution_dates_are_queryable(populated_db):
    rows = populated_db.execute(
        "SELECT distribution_date FROM distribution_dates WHERE submission_id = 6622 "
        "ORDER BY distribution_date"
    ).fetchall()
    assert [r["distribution_date"] for r in rows] == [
        "2026-08-20", "2026-08-21", "2026-08-23"
    ]


def test_daily_presence_records_source_views(populated_db):
    views = populated_db.execute(
        "SELECT source_view FROM daily_presence WHERE target_date = ? "
        "AND submission_id = 6622 ORDER BY source_view",
        (TARGET_DATE,),
    ).fetchall()
    assert [r["source_view"] for r in views] == ["Employee", "Student"]
    only_employee = populated_db.execute(
        "SELECT source_view FROM daily_presence WHERE target_date = ? "
        "AND submission_id = 6602",
        (TARGET_DATE,),
    ).fetchall()
    assert [r["source_view"] for r in only_employee] == ["Employee"]


def test_official_url_stored_per_announcement(populated_db):
    row = populated_db.execute(
        "SELECT official_url FROM announcements WHERE submission_id = 6622"
    ).fetchone()
    assert row["official_url"] == (
        "https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId=6622"
    )


def test_full_body_preserved_verbatim_in_storage(populated_db, employee_fixture):
    source = {
        str(r["Submission"]["Id"]): r["FullBody"]
        for r in employee_fixture["Announcements"]
    }
    for row in db.digest_rows(populated_db, TARGET_DATE):
        submission_id = str(row["submission_id"])
        if submission_id in source:
            assert row["full_body"] == source[submission_id]


def test_submission_body_not_stored(populated_db):
    columns = {
        row[1] for row in populated_db.execute("PRAGMA table_info(announcement_versions)")
    }
    assert "submission_body" not in columns
    assert "SubmissionBody" not in columns


def test_no_forbidden_data_anywhere_in_the_database(populated_db):
    """Sweep every text column for the fields Phase 1 forbids."""
    import re

    tables = [
        row["name"]
        for row in populated_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    ]
    banner = re.compile(r"\b9\d{8}\b")
    for table in tables:
        rows = populated_db.execute(f"SELECT * FROM {table}").fetchall()
        for row in rows:
            for key in row.keys():
                value = row[key]
                if not isinstance(value, str):
                    continue
                for forbidden in FORBIDDEN_KEYS:
                    assert f'"{forbidden}"' not in value, (table, key, forbidden)
                assert not banner.search(value), (table, key, "banner-id-shaped value")


def test_column_names_contain_no_forbidden_fields(populated_db):
    for table in ("announcements", "announcement_versions", "categories",
                  "daily_records", "daily_presence", "curation_results", "deliveries"):
        columns = {
            row[1].lower()
            for row in populated_db.execute(f"PRAGMA table_info({table})")
        }
        for banned in ("external_id", "password", "last_login", "username",
                       "rolesinfo", "submission_body", "is_deleted"):
            assert banned not in columns, (table, banned)
