"""End-to-end collection against the sanitized fixtures.

The permanent acceptance test: the 2026-08-20 Employee view must contain the five
subjects Rowan's own Daily Mail listed as new that day, all classified `New`, and
the remaining eight classified `Standing` exactly as Phase 0 recorded.
"""

from __future__ import annotations

import json

import pytest

from dailymail import config
from dailymail.collect import parse_target_date, run_collection, write_artifact
from dailymail.errors import UsageError
from dailymail.normalize import FORBIDDEN_KEYS, assert_no_forbidden_fields

from conftest import (
    EXPECTED_NEW_IDS_2026_08_20,
    EXPECTED_STANDING_IDS_2026_08_20,
    KNOWN_NEW_SUBJECTS_2026_08_20,
    MOCK_API_VERSION,
    TARGET_DATE,
    MockAnnouncer,
    as_api_response,
)


@pytest.fixture
def artifact(two_audience_mock):
    with two_audience_mock.client() as http:
        return run_collection(target_date=TARGET_DATE, http=http)


def _normalize_subject(text: str) -> str:
    return "".join(text.lower().split())


# --- the headline regression -------------------------------------------------


def test_five_known_new_subjects_present_and_classified_new(artifact):
    by_subject = {
        _normalize_subject(r["title"]): r for r in artifact["announcements"]
    }
    for subject in KNOWN_NEW_SUBJECTS_2026_08_20:
        key = _normalize_subject(subject)
        assert key in by_subject, f"missing known-new subject: {subject}"
        assert by_subject[key]["status"] == "New", subject
        assert by_subject[key]["first_distribution_date"] == TARGET_DATE, subject


def test_new_and_standing_ids_match_phase_0_exactly(artifact):
    new_ids = {r["submission_id"] for r in artifact["announcements"] if r["status"] == "New"}
    standing_ids = {
        r["submission_id"] for r in artifact["announcements"] if r["status"] == "Standing"
    }
    assert new_ids == EXPECTED_NEW_IDS_2026_08_20
    assert standing_ids == EXPECTED_STANDING_IDS_2026_08_20


def test_counts_match_phase_0(artifact):
    assert artifact["source_counts"]["employees_total_count"] == 13
    assert artifact["source_counts"]["students_total_count"] == 3
    counts = artifact["counts"]
    assert counts["unique"] == 13
    assert counts["new"] == 5
    assert counts["standing"] == 8
    assert counts["everyone"] == 3
    assert counts["employee_only"] == 10
    assert counts["student_only"] == 0
    assert counts["categories"] == 33


def test_audience_mapping_matches_phase_0(artifact):
    labels = {r["submission_id"]: r["audience_label"] for r in artifact["announcements"]}
    # Phase 0 §5: 6492, 6622 and 6623 were the three "Both" records that day.
    assert labels["6492"] == "Everyone"
    assert labels["6622"] == "Everyone"
    assert labels["6623"] == "Everyone"
    assert labels["6602"] == "Employee"
    assert labels["6625"] == "Employee"


def test_source_query_membership_recorded(artifact):
    by_id = {r["submission_id"]: r for r in artifact["announcements"]}
    assert by_id["6622"]["source_query_membership"] == ["Employees", "Students"]
    assert by_id["6602"]["source_query_membership"] == ["Employees"]


def test_validation_report_has_no_warnings_on_clean_fixtures(artifact):
    assert artifact["validation"]["warning_count"] == 0
    assert artifact["validation"]["passed"]


# --- artifact shape ----------------------------------------------------------


def test_artifact_contains_every_required_section(artifact):
    for key in (
        "schema_version",
        "target_date",
        "generated_at_utc",
        "collector",
        "runtime",
        "source_counts",
        "counts",
        "category_registry",
        "category_counts_by_audience",
        "announcements",
        "validation",
    ):
        assert key in artifact, key
    assert artifact["target_date"] == TARGET_DATE
    assert artifact["collector"]["detail_pages_fetched"] == 0
    assert artifact["collector"]["browser_used"] is False


def test_category_registry_is_complete_and_dynamic(artifact):
    registry = artifact["category_registry"]
    assert len(registry) == 33
    titles = {entry["title"] for entry in registry}
    assert "Public Safety" in titles
    assert "Well-being and Health" in titles
    # Zero-count categories are retained: the registry is the full backend list.
    counted = artifact["category_counts_by_audience"]["Employees"]
    assert sum(counted.values()) == 13
    assert any(value == 0 for value in counted.values())


def test_every_announcement_carries_the_target_date(artifact):
    for record in artifact["announcements"]:
        assert TARGET_DATE in record["distribution_dates"]


def test_full_body_retained_and_submission_body_absent(artifact):
    for record in artifact["announcements"]:
        assert record["full_body"]
        assert "submission_body" not in record
    rendered = json.dumps(artifact)
    assert "SubmissionBody" not in rendered
    assert "REDACTED_BASE64_DUPLICATE" not in rendered


def test_body_text_derived_for_every_record(artifact):
    for record in artifact["announcements"]:
        assert isinstance(record["body_text"], str)
        assert "<p>" not in record["body_text"]
        assert record["body_diagnostics"]["body_bytes"] > 0


def test_no_forbidden_field_anywhere_in_artifact(artifact):
    assert_no_forbidden_fields(artifact)
    rendered = json.dumps(artifact)
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in rendered, key


def test_tokens_are_never_written_to_the_artifact(artifact):
    rendered = json.dumps(artifact)
    assert MOCK_API_VERSION not in rendered
    assert "MOCKcsrfToken00000000=" not in rendered
    fingerprints = artifact["runtime"]["token_fingerprints"]
    assert set(fingerprints) == {"module_version", "api_version", "csrf_token"}
    assert all(len(value) == 12 for value in fingerprints.values())


def test_runtime_records_pagination_and_duration(artifact):
    runtime = artifact["runtime"]
    assert runtime["pages_fetched"] == {"Employees": 1, "Students": 1}
    assert runtime["token_rediscovery_performed"] is False
    assert isinstance(runtime["duration_seconds"], float)


# --- genuine empty day, end to end ------------------------------------------


def test_empty_day_collects_successfully_with_zero_announcements(empty_day_fixture):
    """A real quiet day is a success, not an error."""
    empty = as_api_response(
        announcements=[],
        categories=empty_day_fixture["Categories"],
        total_count=0,
    )
    mock = MockAnnouncer(
        pages_by_audience={"Employees": [empty], "Students": [empty]}
    )
    with mock.client() as http:
        artifact = run_collection(target_date="1999-01-01", http=http)

    assert artifact["counts"]["unique"] == 0
    assert artifact["counts"]["new"] == 0
    assert artifact["counts"]["standing"] == 0
    assert artifact["announcements"] == []
    # Crucially, the category registry is still fully captured.
    assert artifact["counts"]["categories"] == 33


# --- date handling ----------------------------------------------------------


def test_default_date_is_today_in_rowan_local():
    from datetime import datetime
    from zoneinfo import ZoneInfo

    expected = datetime.now(ZoneInfo(config.ROWAN_TIMEZONE)).strftime("%Y-%m-%d")
    assert parse_target_date(None) == expected


def test_explicit_date_accepted():
    assert parse_target_date("2026-08-20") == "2026-08-20"
    assert parse_target_date("  2026-08-20  ") == "2026-08-20"


@pytest.mark.parametrize(
    "bad",
    [
        "not-a-date",
        "2026-8-20",       # not zero-padded
        "20-08-2026",
        "2026/08/20",
        "2026-13-01",      # impossible month
        "2026-02-30",      # impossible day
        "2026-08-20T00:00",
        "",
        "today",
    ],
)
def test_malformed_dates_rejected_before_any_request(bad):
    """Rowan silently substitutes its own date, so we must never send these."""
    with pytest.raises(UsageError):
        parse_target_date(bad)


def test_malformed_date_never_reaches_the_network(two_audience_mock):
    with pytest.raises(UsageError):
        parse_target_date("2026-8-20")
    assert two_audience_mock.requests == []


# --- artifact writing -------------------------------------------------------


def test_write_artifact_is_single_file_and_reloadable(artifact, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = write_artifact(artifact)
    assert path == tmp_path / "dailymail" / "collections" / f"{TARGET_DATE}.json"
    assert path.is_file()
    # Exactly one file: no separate raw/unredacted copy.
    written = list(path.parent.iterdir())
    assert written == [path], written
    reloaded = json.loads(path.read_text(encoding="utf-8"))
    assert reloaded["counts"] == artifact["counts"]
    assert_no_forbidden_fields(reloaded)


def test_write_artifact_leaves_no_temp_files(artifact, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    write_artifact(artifact)
    leftovers = [p.name for p in (tmp_path / "dailymail" / "collections").iterdir()
                 if p.suffix == ".tmp"]
    assert leftovers == []
