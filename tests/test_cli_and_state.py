"""CLI contract, exit codes, and the prior-run comparison features."""

from __future__ import annotations

import json

import pytest

from dailymail import cli, config
from dailymail.collect import run_collection, write_artifact
from dailymail.errors import (
    DiscoveryError,
    TlsError,
    TransportError,
    UsageError,
    ValidationError,
    VersionChangedError,
)

from conftest import TARGET_DATE, MockAnnouncer, as_api_response


# --- exit codes are distinct and stable -------------------------------------


def test_exit_codes_are_distinct_and_nonzero():
    codes = {
        UsageError: 2,
        TlsError: 3,
        DiscoveryError: 4,
        TransportError: 5,
        ValidationError: 6,
        VersionChangedError: 7,
    }
    for error, expected in codes.items():
        assert error.exit_code == expected
        assert error.exit_code != 0
    assert len(set(codes.values())) == len(codes)


def test_tls_failure_has_its_own_exit_code_distinct_from_http():
    assert TlsError.exit_code != TransportError.exit_code


def test_malformed_date_exits_2(capsys):
    code = cli.main(["collect", "--date", "2026-8-20"])
    assert code == 2
    assert "UsageError" in capsys.readouterr().err


def test_non_positive_page_size_exits_2():
    assert cli.main(["collect", "--date", "2026-08-20", "--page-size", "0"]) == 2


def test_inspect_missing_artifact_exits_2(capsys):
    code = cli.main(["inspect", "--date", "2026-08-20"])
    assert code == 2
    assert "no collection artifact" in capsys.readouterr().err


# --- CLI output contract -----------------------------------------------------


def test_collect_success_line_format(two_audience_mock, monkeypatch, capsys):
    def fake_run(*, target_date, page_size=100, http=None):
        with two_audience_mock.client() as client:
            return run_collection(target_date=target_date, http=client)

    monkeypatch.setattr(cli.collect, "run_collection", fake_run)
    code = cli.main(["collect", "--date", TARGET_DATE])
    assert code == 0
    out = capsys.readouterr().out
    first = out.splitlines()[0]
    assert first == (
        "COLLECT OK date=2026-08-20 employees=13 students=3 unique=13 "
        "new=5 standing=8 categories=33"
    )


def test_collect_never_prints_announcement_bodies(
    two_audience_mock, monkeypatch, capsys
):
    def fake_run(*, target_date, page_size=100, http=None):
        with two_audience_mock.client() as client:
            return run_collection(target_date=target_date, http=client)

    monkeypatch.setattr(cli.collect, "run_collection", fake_run)
    cli.main(["collect", "--date", TARGET_DATE])
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert "<p>" not in combined
    assert "data:image" not in combined
    # A distinctive phrase from a real fixture body must not be echoed.
    assert "Parking Lot O-1 will be closed" not in combined


def test_inspect_prints_no_bodies(two_audience_mock, capsys):
    with two_audience_mock.client() as http:
        artifact = run_collection(target_date=TARGET_DATE, http=http)
    write_artifact(artifact)

    code = cli.main(["inspect", "--date", TARGET_DATE])
    assert code == 0
    out = capsys.readouterr().out
    assert "<p>" not in out
    assert "data:image" not in out
    assert "6622" in out          # ids are listed
    assert "Parking Lot" in out   # titles are listed
    assert "will be closed" not in out  # body text is not


# --- prior-run comparison ----------------------------------------------------


def _artifact_for(mock, date):
    with mock.client() as http:
        return run_collection(target_date=date, http=http)


def _seed_prior_artifact(date, *, fingerprints, category_ids):
    """Write a minimal previous collection so the comparison logic has an input.

    Seeding the file directly (rather than collecting a second date) keeps this
    focused on the comparison, and avoids fighting the V5 date-echo gate: the
    fixtures' records only carry 2026-08-20.
    """
    path = config.collection_path(date)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "target_date": date,
                "runtime": {"token_fingerprints": fingerprints},
                "category_registry": [{"id": i, "title": f"cat{i}"} for i in category_ids],
                "counts": {},
                "announcements": [],
            }
        ),
        encoding="utf-8",
    )
    return path


def _mock_fingerprints():
    from dailymail.discovery import fingerprint

    from conftest import MOCK_API_VERSION, MOCK_CSRF, MOCK_MODULE_VERSION

    return {
        "module_version": fingerprint(MOCK_MODULE_VERSION),
        "api_version": fingerprint(MOCK_API_VERSION),
        "csrf_token": fingerprint(MOCK_CSRF),
    }


def test_no_prior_run_reports_none(two_audience_mock):
    artifact = _artifact_for(two_audience_mock, TARGET_DATE)
    assert artifact["runtime"]["prior_run_compared"] is None
    assert artifact["runtime"]["tokens_changed_since_prior_run"] is None


def test_tokens_changed_since_prior_run_detected(two_audience_mock, employee_fixture):
    _seed_prior_artifact(
        "2026-08-19",
        fingerprints={
            "module_version": "aaaaaaaaaaaa",
            "api_version": "bbbbbbbbbbbb",
            "csrf_token": "cccccccccccc",
        },
        category_ids=[c["Id"] for c in employee_fixture["Categories"]],
    )
    artifact = _artifact_for(two_audience_mock, TARGET_DATE)
    assert artifact["runtime"]["prior_run_compared"] == "2026-08-19"
    assert artifact["runtime"]["tokens_changed_since_prior_run"] is True
    assert any("republished" in w for w in artifact["validation"]["warnings"])


def test_tokens_unchanged_since_prior_run(two_audience_mock, employee_fixture):
    _seed_prior_artifact(
        "2026-08-19",
        fingerprints=_mock_fingerprints(),
        category_ids=[c["Id"] for c in employee_fixture["Categories"]],
    )
    artifact = _artifact_for(two_audience_mock, TARGET_DATE)
    assert artifact["runtime"]["tokens_changed_since_prior_run"] is False
    assert not any("republished" in w for w in artifact["validation"]["warnings"])


def test_new_category_reported_against_prior_run(employee_fixture, student_fixture):
    """A category Rowan adds later must be surfaced, not silently absorbed."""
    _seed_prior_artifact(
        "2026-08-19",
        fingerprints=_mock_fingerprints(),
        category_ids=[c["Id"] for c in employee_fixture["Categories"]],
    )

    extended = [dict(c) for c in employee_fixture["Categories"]]
    extended.append(
        {"Id": 77, "Title": "Emergency Notices", "Count": "0", "Rank": 0,
         "Color": "red-dark", "Selected": False}
    )
    with_new_category = MockAnnouncer(
        pages_by_audience={
            "Employees": [
                as_api_response(
                    announcements=employee_fixture["Announcements"],
                    categories=extended,
                    total_count=13,
                )
            ],
            "Students": [
                as_api_response(
                    announcements=student_fixture["Announcements"],
                    categories=student_fixture["Categories"],
                    total_count=3,
                )
            ],
        }
    )
    artifact = _artifact_for(with_new_category, TARGET_DATE)

    assert artifact["counts"]["categories"] == 34
    assert artifact["new_categories_since_prior_run"] == [
        {"id": 77, "title": "Emergency Notices"}
    ]
    assert any("new categories observed" in w for w in artifact["validation"]["warnings"])


def test_unknown_category_does_not_fail_collection(employee_fixture, student_fixture):
    """New categories are valid data, never a hard error."""
    extended = [dict(c) for c in employee_fixture["Categories"]]
    extended.append(
        {"Id": 999, "Title": "Totally New", "Count": "0", "Rank": 0,
         "Color": "x", "Selected": False}
    )
    mock = MockAnnouncer(
        pages_by_audience={
            "Employees": [
                as_api_response(
                    announcements=employee_fixture["Announcements"],
                    categories=extended,
                    total_count=13,
                )
            ],
            "Students": [
                as_api_response(
                    announcements=student_fixture["Announcements"],
                    categories=student_fixture["Categories"],
                    total_count=3,
                )
            ],
        }
    )
    artifact = _artifact_for(mock, TARGET_DATE)
    assert artifact["counts"]["unique"] == 13
    assert 999 in {entry["id"] for entry in artifact["category_registry"]}


def test_artifact_written_under_xdg_state_home(two_audience_mock, isolated_state_dir):
    artifact = _artifact_for(two_audience_mock, TARGET_DATE)
    path = write_artifact(artifact)
    assert path == isolated_state_dir / "dailymail" / "collections" / f"{TARGET_DATE}.json"
    assert config.collections_dir() == isolated_state_dir / "dailymail" / "collections"
