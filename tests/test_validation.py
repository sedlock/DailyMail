"""The validation gates, especially the empty-day vs broken-collector distinction."""

from __future__ import annotations

import pytest

from dailymail import validate
from dailymail.errors import ValidationError, VersionChangedError
from dailymail.validate import ValidationReport, check_structural

from conftest import TARGET_DATE, as_api_response, recategorize


def _structural(payload, *, status=200, content_type="application/json; charset=utf-8"):
    return check_structural(
        payload, http_status=status, content_type=content_type, context="test"
    )


def test_healthy_response_passes_structural_gate(employee_fixture):
    payload = as_api_response(
        announcements=employee_fixture["Announcements"],
        categories=employee_fixture["Categories"],
        total_count=employee_fixture["TotalCount"],
    )
    parsed = _structural(payload)
    assert parsed.total_count == 13
    assert len(parsed.announcements) == 13
    assert len(parsed.categories) == 33


def test_genuine_zero_day_is_accepted(empty_day_fixture):
    """A real empty day keeps the full category registry: that is the tell."""
    payload = as_api_response(
        announcements=[],
        categories=empty_day_fixture["Categories"],
        total_count=empty_day_fixture["TotalCount"],
    )
    parsed = _structural(payload)
    assert parsed.total_count == 0
    assert parsed.announcements == []
    assert len(parsed.categories) == 33

    # ...and it survives the dataset gates as a legitimate zero.
    report = ValidationReport()
    validate.check_audience_dataset(
        audience="Employees",
        target_date=TARGET_DATE,
        total_count=0,
        announcements=[],
        categories=empty_day_fixture["Categories"],
        page_size=100,
        report=report,
    )
    assert report.warnings == []


def test_broken_apiversion_response_is_rejected(broken_apiversion_fixture):
    """HTTP 200 + data:{} + hasApiVersionChanged must never read as 'no news'."""
    payload = broken_apiversion_fixture["response"]
    assert payload["data"] == {}
    with pytest.raises(VersionChangedError):
        _structural(payload)


def test_empty_data_without_version_flag_still_rejected():
    payload = {
        "versionInfo": {"hasApiVersionChanged": False},
        "data": {},
        "rolesInfo": ",",
    }
    with pytest.raises(ValidationError, match="`data` is missing or empty"):
        _structural(payload)


def test_empty_category_registry_is_rejected():
    """Zero announcements AND zero categories is a broken response, not a quiet day."""
    payload = as_api_response(announcements=[], categories=[], total_count=0)
    with pytest.raises(ValidationError, match="Categories is empty"):
        _structural(payload)


@pytest.mark.parametrize("status", [204, 302, 403, 500, 503])
def test_non_200_rejected(status, employee_fixture):
    payload = as_api_response(
        announcements=[], categories=employee_fixture["Categories"], total_count=0
    )
    with pytest.raises(ValidationError, match=f"HTTP {status}"):
        _structural(payload, status=status)


def test_non_json_content_type_rejected(employee_fixture):
    payload = as_api_response(
        announcements=[], categories=employee_fixture["Categories"], total_count=0
    )
    with pytest.raises(ValidationError, match="is not JSON"):
        _structural(payload, content_type="text/html; charset=utf-8")


def test_server_exception_rejected_and_message_is_short():
    payload = {
        "data": {},
        "exception": {
            "name": "ServerException",
            "message": "Invalid Login",
            "stack": "SHOULD-NOT-BE-SURFACED",
        },
    }
    with pytest.raises(ValidationError) as caught:
        _structural(payload)
    assert "Invalid Login" in str(caught.value)
    assert "SHOULD-NOT-BE-SURFACED" not in str(caught.value)


def test_missing_total_count_rejected(employee_fixture):
    payload = as_api_response(
        announcements=[], categories=employee_fixture["Categories"], total_count=0
    )
    del payload["data"]["TotalCount"]
    with pytest.raises(ValidationError, match="TotalCount is absent"):
        _structural(payload)


@pytest.mark.parametrize("bad", ["", "abc", "1.5", None, True, [], "-3"])
def test_non_integer_or_negative_total_count_rejected(bad, employee_fixture):
    payload = as_api_response(
        announcements=[], categories=employee_fixture["Categories"], total_count=0
    )
    payload["data"]["TotalCount"] = bad
    with pytest.raises(ValidationError):
        _structural(payload)


def test_total_count_accepts_string_or_int(employee_fixture):
    """Rowan sends a string; be tolerant but strict."""
    for value in ("13", 13, " 13 "):
        payload = as_api_response(
            announcements=employee_fixture["Announcements"],
            categories=employee_fixture["Categories"],
            total_count=0,
        )
        payload["data"]["TotalCount"] = value
        assert _structural(payload).total_count == 13


def test_missing_announcements_key_rejected(employee_fixture):
    payload = as_api_response(
        announcements=[], categories=employee_fixture["Categories"], total_count=0
    )
    del payload["data"]["Announcements"]
    with pytest.raises(ValidationError, match="Announcements key is absent"):
        _structural(payload)


# --- count reconciliation and category cross-footing --------------------------


def test_count_reconciliation_detects_short_collection(employee_fixture):
    report = ValidationReport()
    with pytest.raises(ValidationError, match="collected 12 unique announcements"):
        validate.check_audience_dataset(
            audience="Employees",
            target_date=TARGET_DATE,
            total_count=13,
            announcements=employee_fixture["Announcements"][:12],
            categories=employee_fixture["Categories"],
            page_size=100,
            report=report,
        )


def test_category_cross_foot_detects_mismatch(employee_fixture):
    """sum(category.Count) != TotalCount is an independent completeness failure."""
    categories = [dict(c) for c in employee_fixture["Categories"]]
    for category in categories:
        if category["Count"] != "0":
            category["Count"] = str(int(category["Count"]) + 1)
            break
    report = ValidationReport()
    with pytest.raises(ValidationError, match="sum of category counts"):
        validate.check_audience_dataset(
            audience="Employees",
            target_date=TARGET_DATE,
            total_count=13,
            announcements=employee_fixture["Announcements"],
            categories=categories,
            page_size=100,
            report=report,
        )


def test_category_cross_foot_passes_on_real_fixture(employee_fixture):
    report = ValidationReport()
    validate.check_audience_dataset(
        audience="Employees",
        target_date=TARGET_DATE,
        total_count=13,
        announcements=employee_fixture["Announcements"],
        categories=employee_fixture["Categories"],
        page_size=100,
        report=report,
    )
    assert any("sum(category.Count) == TotalCount" in line for line in report.passed)


def test_duplicate_submission_id_rejected(employee_fixture):
    """13 unique ids delivered as 14 rows: the count gates pass, V6 must catch it."""
    announcements = list(employee_fixture["Announcements"])
    announcements.append(announcements[0])  # same SubmissionId twice
    report = ValidationReport()
    with pytest.raises(ValidationError, match="duplicate SubmissionIds"):
        validate.check_audience_dataset(
            audience="Employees",
            target_date=TARGET_DATE,
            total_count=13,
            announcements=announcements,
            categories=employee_fixture["Categories"],
            page_size=100,
            report=report,
        )


@pytest.mark.parametrize("bad_id", ["0", "-5", "abc", "", "6622a"])
def test_non_positive_integer_submission_id_rejected(bad_id, employee_fixture):
    record = json_clone(employee_fixture["Announcements"][0])
    record["Submission"]["Id"] = bad_id
    categories = recategorize(employee_fixture["Categories"], [record])
    report = ValidationReport()
    with pytest.raises(ValidationError, match="positive integer"):
        validate.check_audience_dataset(
            audience="Employees",
            target_date=TARGET_DATE,
            total_count=1,
            announcements=[record],
            categories=categories,
            page_size=100,
            report=report,
        )


def json_clone(obj):
    import json as _json

    return _json.loads(_json.dumps(obj))


def categories_summing_to(categories: list[dict], total: int) -> list[dict]:
    """Category registry whose counts sum to `total`, regardless of which
    category the test record claims. Keeps V2 out of the way."""
    out = [dict(c, Count="0") for c in categories]
    out[0]["Count"] = str(total)
    return out


# --- audience gates ----------------------------------------------------------


def test_unknown_request_audience_refused(employee_fixture):
    report = ValidationReport()
    with pytest.raises(ValidationError, match="refusing to accept audience"):
        validate.check_audience_dataset(
            audience="Bogus",
            target_date=TARGET_DATE,
            total_count=13,
            announcements=employee_fixture["Announcements"],
            categories=employee_fixture["Categories"],
            page_size=100,
            report=report,
        )


def test_record_with_impossible_audience_rejected(employee_fixture):
    """A Students-only record must never appear in the Employee view."""
    record = json_clone(employee_fixture["Announcements"][0])
    record["Submission"]["Audience"] = "Students"
    categories = recategorize(employee_fixture["Categories"], [record])
    report = ValidationReport()
    with pytest.raises(ValidationError, match="cannot appear in this view"):
        validate.check_audience_dataset(
            audience="Employees",
            target_date=TARGET_DATE,
            total_count=1,
            announcements=[record],
            categories=categories,
            page_size=100,
            report=report,
        )


def test_date_echo_rejects_record_missing_target_date(employee_fixture):
    record = json_clone(employee_fixture["Announcements"][0])
    record["DistributionDates"] = {"List": ["2026-09-01", "2026-09-08"]}
    categories = recategorize(employee_fixture["Categories"], [record])
    report = ValidationReport()
    with pytest.raises(ValidationError, match="does not list 2026-08-20"):
        validate.check_audience_dataset(
            audience="Employees",
            target_date=TARGET_DATE,
            total_count=1,
            announcements=[record],
            categories=categories,
            page_size=100,
            report=report,
        )


# --- required fields ---------------------------------------------------------


@pytest.mark.parametrize(
    "mutate,expected",
    [
        (lambda r: r["Submission"].__setitem__("Title", "   "), "empty title"),
        (lambda r: r.__setitem__("FullBody", ""), "empty FullBody"),
        (
            lambda r: r["Submission"].__setitem__("SubmittedStatus", "Pending"),
            "expected 'Approved'",
        ),
        (
            lambda r: r["Submission"].__setitem__("Category", 99999),
            "did not return in this response",
        ),
    ],
)
def test_required_field_failures(mutate, expected, employee_fixture):
    record = json_clone(employee_fixture["Announcements"][0])
    mutate(record)
    # Cross-foot deliberately made to agree, so the per-record gate is the one
    # under test rather than V2.
    categories = categories_summing_to(employee_fixture["Categories"], 1)
    report = ValidationReport()
    with pytest.raises(ValidationError, match=expected):
        validate.check_audience_dataset(
            audience="Employees",
            target_date=TARGET_DATE,
            total_count=1,
            announcements=[record],
            categories=categories,
            page_size=100,
            report=report,
        )


def test_unknown_category_is_accepted_when_backend_returns_it(employee_fixture):
    """New categories are valid; only unresolvable ones fail."""
    record = json_clone(employee_fixture["Announcements"][0])
    record["Submission"]["Category"] = 4242
    record["Category"] = {
        "Id": 4242,
        "Title": "Brand New Category",
        "Rank": 0,
        "Is_Active": True,
        "RestrictSubmitting": False,
        "Color": "teal-dark",
        "ApprovalProcess": "Organization",
    }
    categories = [dict(c, Count="0") for c in employee_fixture["Categories"]]
    categories.append(
        {
            "Id": 4242,
            "Title": "Brand New Category",
            "Count": "1",
            "Rank": 0,
            "Color": "teal-dark",
            "Selected": False,
        }
    )
    report = ValidationReport()
    validate.check_audience_dataset(
        audience="Employees",
        target_date=TARGET_DATE,
        total_count=1,
        announcements=[record],
        categories=categories,
        page_size=100,
        report=report,
    )  # must not raise


def test_soft_warnings_do_not_fail_the_run(employee_fixture):
    record = json_clone(employee_fixture["Announcements"][0])
    record["Submission"]["ContactRowanEmail"] = ""
    record["Submission"]["Event"] = True
    record["Submission"]["EventName"] = ""
    record["Submission"]["EventDate"] = "1900-01-01"
    categories = recategorize(employee_fixture["Categories"], [record])
    report = ValidationReport()
    validate.check_audience_dataset(
        audience="Employees",
        target_date=TARGET_DATE,
        total_count=1,
        announcements=[record],
        categories=categories,
        page_size=100,
        report=report,
    )
    joined = " ".join(report.warnings)
    assert "no contact email" in joined
    assert "no event name" in joined
    assert "no event date" in joined


# --- parity ------------------------------------------------------------------


def test_parity_passes_on_real_fixtures(employee_fixture, student_fixture):
    employee_ids = {r["Submission"]["Id"] for r in employee_fixture["Announcements"]}
    student_ids = {r["Submission"]["Id"] for r in student_fixture["Announcements"]}
    by_id = {
        r["Submission"]["Id"]: r["Submission"]["Audience"]
        for r in employee_fixture["Announcements"] + student_fixture["Announcements"]
    }
    report = ValidationReport()
    split = validate.check_audience_parity(
        employee_ids=employee_ids,
        student_ids=student_ids,
        source_audience_by_id=by_id,
        report=report,
    )
    assert split == {"everyone": 3, "employee_only": 10, "student_only": 0}


def test_parity_detects_both_not_in_both_views():
    report = ValidationReport()
    with pytest.raises(ValidationError, match="Marked Both but not in both views"):
        validate.check_audience_parity(
            employee_ids={"1"},
            student_ids=set(),
            source_audience_by_id={"1": "Both"},
            report=report,
        )


def test_parity_detects_intersection_not_marked_both():
    report = ValidationReport()
    with pytest.raises(ValidationError, match="in both views but not marked Both"):
        validate.check_audience_parity(
            employee_ids={"1"},
            student_ids={"1"},
            source_audience_by_id={"1": "Employees"},
            report=report,
        )
