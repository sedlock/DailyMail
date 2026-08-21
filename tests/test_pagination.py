"""Real StartIndex/MaxRecords pagination, including the truncation guards."""

from __future__ import annotations

import json

import pytest

from dailymail.client import AnnouncerClient
from dailymail.discovery import RuntimeVersions
from dailymail.errors import ValidationError

from conftest import MockAnnouncer, as_api_response

VERSIONS = RuntimeVersions(
    module_version="mv", api_version="av", csrf_token="csrf"
)


def _synthetic_records(count: int, *, start_id: int = 1000, date: str = "2026-08-20"):
    return [
        {
            "FullBody": f"<p>Body {i}</p>",
            "ShortBody": f"Body {i}",
            "DistributionDates": {"List": [date]},
            "Category": {"Id": 3, "Title": "Public Safety", "Rank": 3,
                         "Is_Active": True, "RestrictSubmitting": False,
                         "Color": "orange-darker", "ApprovalProcess": "Organization"},
            "Submission": {
                "Id": str(start_id + i),
                "Title": f"Announcement {i}",
                "Audience": "Employees",
                "Category": 3,
                "SubmittedStatus": "Approved",
                "ContactRowanEmail": "x@rowan.edu",
                "Event": False,
            },
        }
        for i in range(count)
    ]


def _paged(records, page_size, categories):
    """Split records into API-shaped pages the way the server would."""
    total = len(records)
    pages = []
    for offset in range(0, max(total, 1), page_size):
        pages.append(
            as_api_response(
                announcements=records[offset : offset + page_size],
                categories=categories,
                total_count=total,
            )
        )
    return pages


def _categories(total: int):
    return [{"Id": 3, "Title": "Public Safety", "Count": str(total), "Rank": 3,
             "Color": "orange-darker", "Selected": False}]


def test_single_page_day_makes_one_request():
    records = _synthetic_records(13)
    mock = MockAnnouncer(
        pages_by_audience={"Employees": _paged(records, 100, _categories(13))}
    )
    with mock.client() as http:
        result = AnnouncerClient(http, VERSIONS).fetch_audience(
            audience="Employees", target_date="2026-08-20", page_size=100
        )
    assert result.total_count == 13
    assert len(result.announcements) == 13
    assert result.pages_fetched == 1
    assert len(mock.requests) == 1


def test_multi_page_day_is_fully_collected():
    """250 records at page size 100 must take 3 pages and lose nothing."""
    records = _synthetic_records(250)
    mock = MockAnnouncer(
        pages_by_audience={"Employees": _paged(records, 100, _categories(250))}
    )
    with mock.client() as http:
        result = AnnouncerClient(http, VERSIONS).fetch_audience(
            audience="Employees", target_date="2026-08-20", page_size=100
        )
    assert result.total_count == 250
    assert len(result.announcements) == 250
    assert result.pages_fetched == 3
    ids = [r["Submission"]["Id"] for r in result.announcements]
    assert len(set(ids)) == 250
    assert [r["start_index"] for r in mock.requests] == [0, 100, 200]


def test_pagination_walks_start_index_in_page_size_steps():
    records = _synthetic_records(45)
    mock = MockAnnouncer(
        pages_by_audience={"Employees": _paged(records, 20, _categories(45))}
    )
    with mock.client() as http:
        AnnouncerClient(http, VERSIONS).fetch_audience(
            audience="Employees", target_date="2026-08-20", page_size=20
        )
    assert [r["start_index"] for r in mock.requests] == [0, 20, 40]
    assert all(r["max_records"] == 20 for r in mock.requests)


def test_no_hardcoded_ceiling_on_records_per_day():
    """A day far larger than any observed must still complete."""
    records = _synthetic_records(1234)
    mock = MockAnnouncer(
        pages_by_audience={"Employees": _paged(records, 100, _categories(1234))}
    )
    with mock.client() as http:
        result = AnnouncerClient(http, VERSIONS).fetch_audience(
            audience="Employees", target_date="2026-08-20", page_size=100
        )
    assert len(result.announcements) == 1234


def test_short_page_before_total_is_a_loud_failure():
    """Server claims 250 but stops returning rows: must fail, not truncate."""
    records = _synthetic_records(120)
    pages = [
        as_api_response(
            announcements=records[0:100], categories=_categories(250), total_count=250
        ),
        as_api_response(
            announcements=[], categories=_categories(250), total_count=250
        ),
    ]
    mock = MockAnnouncer(pages_by_audience={"Employees": pages})
    with mock.client() as http:
        with pytest.raises(ValidationError, match="returned no records"):
            AnnouncerClient(http, VERSIONS).fetch_audience(
                audience="Employees", target_date="2026-08-20", page_size=100
            )


def test_non_advancing_pagination_detected():
    """A server that keeps returning the same page must not loop forever."""
    records = _synthetic_records(100)
    repeated = as_api_response(
        announcements=records, categories=_categories(250), total_count=250
    )
    mock = MockAnnouncer(pages_by_audience={"Employees": [repeated, repeated]})
    with mock.client() as http:
        with pytest.raises(ValidationError, match="added no new records"):
            AnnouncerClient(http, VERSIONS).fetch_audience(
                audience="Employees", target_date="2026-08-20", page_size=100
            )


def test_total_count_changing_mid_pagination_is_rejected():
    records = _synthetic_records(150)
    pages = [
        as_api_response(
            announcements=records[0:100], categories=_categories(150), total_count=150
        ),
        as_api_response(
            announcements=records[100:150], categories=_categories(151), total_count=151
        ),
    ]
    mock = MockAnnouncer(pages_by_audience={"Employees": pages})
    with mock.client() as http:
        with pytest.raises(ValidationError, match="TotalCount changed mid-pagination"):
            AnnouncerClient(http, VERSIONS).fetch_audience(
                audience="Employees", target_date="2026-08-20", page_size=100
            )


def test_zero_announcement_day_makes_exactly_one_request():
    mock = MockAnnouncer(
        pages_by_audience={
            "Employees": [
                as_api_response(
                    announcements=[], categories=_categories(0), total_count=0
                )
            ]
        }
    )
    with mock.client() as http:
        result = AnnouncerClient(http, VERSIONS).fetch_audience(
            audience="Employees", target_date="1999-01-01", page_size=100
        )
    assert result.total_count == 0
    assert result.announcements == []
    assert result.pages_fetched == 1


# --- request shape -----------------------------------------------------------


def test_request_uses_single_day_sentinel_and_discovered_tokens():
    records = _synthetic_records(1)
    mock = MockAnnouncer(
        pages_by_audience={"Employees": _paged(records, 100, _categories(1))}
    )
    with mock.client() as http:
        AnnouncerClient(http, VERSIONS).fetch_audience(
            audience="Employees", target_date="2026-08-20", page_size=100
        )
    sent = mock.requests[0]
    assert sent["end_date"] == "1900-01-01"     # single-day mode
    assert sent["start_date"] == "2026-08-20"
    assert sent["api_version"] == "av"          # discovered, not hardcoded
    assert sent["module_version"] == "mv"
    assert sent["csrf"] == "csrf"


@pytest.mark.parametrize("audience", ["Bogus", "", "employees", "Both", "All"])
def test_client_refuses_to_put_unknown_audience_on_the_wire(audience):
    """Rowan answers an unknown audience with a misleading partial result."""
    mock = MockAnnouncer(pages_by_audience={})
    with mock.client() as http:
        with pytest.raises(ValidationError, match="refusing to request audience"):
            AnnouncerClient(http, VERSIONS).fetch_page(
                audience=audience,
                target_date="2026-08-20",
                start_index=0,
                max_records=100,
            )
    assert mock.requests == []


def test_non_positive_max_records_refused():
    mock = MockAnnouncer(pages_by_audience={})
    with mock.client() as http:
        for bad in (0, -1):
            with pytest.raises(ValidationError, match="MaxRecords must be positive"):
                AnnouncerClient(http, VERSIONS).fetch_page(
                    audience="Employees",
                    target_date="2026-08-20",
                    start_index=0,
                    max_records=bad,
                )
    assert mock.requests == []


def test_only_the_gethomedata_endpoint_is_ever_called(two_audience_mock):
    """No write endpoint and no detail page may be requested."""
    import httpx

    from dailymail.collect import run_collection

    seen: list[str] = []
    inner = two_audience_mock.transport()

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return inner.handler(request)

    with httpx.Client(transport=httpx.MockTransport(recording)) as http:
        run_collection(target_date="2026-08-20", http=http)

    allowed_suffixes = (
        "moduleservices/moduleinfo",
        "RowanAnnouncer.MainFlow.Home.mvc.js",
        "OutSystems.js",
        "MainFlow/Home/ActionGetHomeData",
    )
    for path in seen:
        assert path.endswith(allowed_suffixes), f"unexpected endpoint: {path}"
    forbidden = ("SaveSubmission", "UpdateSubmissionStatus", "SaveVisitorClicks",
                 "SendDailyMail", "Announcement?", "DoLogin", "CreateCalendarInvite")
    joined = " ".join(seen)
    for token in forbidden:
        assert token not in joined


def test_duplicate_id_within_a_page_is_reported_not_absorbed():
    """De-duplicating silently would hide a short page behind a correct-looking count."""
    records = _synthetic_records(5)
    page = as_api_response(
        announcements=records + [records[0]],
        categories=_categories(6),
        total_count=6,
    )
    mock = MockAnnouncer(pages_by_audience={"Employees": [page]})
    with mock.client() as http:
        with pytest.raises(ValidationError, match="appeared twice within the page"):
            AnnouncerClient(http, VERSIONS).fetch_audience(
                audience="Employees", target_date="2026-08-20", page_size=100
            )
