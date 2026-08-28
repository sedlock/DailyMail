"""Event detection and extraction: what is a calendar event, and what is not.

The acceptance case throughout is Rowan announcement 6694, the Provost's Town
Hall of 14 October 2026. It is the interesting case precisely because Rowan's
own `Event` boolean is *false* for it: everything the calendar needs lives in
the body, which is why detection cannot rely on that flag.
"""

from __future__ import annotations

from datetime import date, time

import pytest

from conftest import REFERENCE, TOWN_HALL_BODY, TOWN_HALL_TEXT, make_town_hall_row
from dailymail import events


@pytest.fixture
def town_hall():
    candidate, diagnostics = events.detect_candidate(
        make_town_hall_row(), reference_date=REFERENCE
    )
    assert candidate is not None, diagnostics
    return candidate


# --- candidate detection -----------------------------------------------------


def test_rowan_structured_event_fields_make_a_candidate():
    row = make_town_hall_row(
        title="Graduate Open House",
        body_text="Join us to learn about graduate programs.",
        full_body="<p>Join us to learn about graduate programs.</p>",
        is_event=1,
        event_date="2026-09-26",
        event_start_time="10:00:00",
        event_end_time="13:00:00",
        event_location="Chamberlain Student Center",
    )
    candidate, _ = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is not None
    assert candidate.event_date == date(2026, 9, 26)
    assert candidate.start == time(10, 0)
    assert candidate.end == time(13, 0)
    assert "source_event_flag" in candidate.evidence


def test_explicit_body_date_and_time_make_a_candidate_without_the_event_flag(town_hall):
    """The acceptance case: Rowan's Event boolean is false and it still works."""
    assert town_hall.event_date == date(2026, 10, 14)
    assert "source_event_flag" not in town_hall.evidence
    assert "labelled_time_range" in town_hall.evidence


def test_an_ordinary_announcement_is_not_an_event():
    row = make_town_hall_row(
        title="Rowan University Turnitin Policy",
        body_text=(
            "Rowan University uses Turnitin to support academic integrity. "
            "Faculty may enable it in Canvas at any time."
        ),
        full_body="<p>Rowan University uses Turnitin.</p>",
    )
    candidate, diagnostics = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is None
    assert diagnostics["reason"] in ("no_event_date", "no_event_time", "no_event_evidence")


def test_a_dated_deadline_is_not_a_calendar_event():
    row = make_town_hall_row(
        title="Limited Submission Opportunity: Carnegie Fellows",
        body_text=(
            "Internal applications are due by October 14, 2026. "
            "Submit your materials to the Office of Research."
        ),
        full_body="<p>Internal applications are due by October 14, 2026.</p>",
    )
    candidate, diagnostics = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is None
    assert diagnostics["reason"] in ("no_event_time", "deadline_not_event")


def test_several_independent_dates_are_withheld_rather_than_guessed():
    """Two coffee hours a fortnight apart are not one appointment."""
    body = (
        "Provost's Coffee Hours are small-group conversations.\n"
        "Our sessions are:\n"
        "Thursday, September 10 from 11:00-12:30\n"
        "Monday, September 21 from 2:30-4:00\n"
    )
    row = make_town_hall_row(
        title="Provost's Coffee Hours", body_text=body, full_body=f"<p>{body}</p>"
    )
    candidate, diagnostics = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is None
    assert diagnostics["reason"] == "multiple_distinct_dates"


def test_a_recurring_class_timetable_is_not_one_event():
    body = (
        "Classes start September 1!\n"
        "Glassboro\n"
        "Schedule: 9 a.m. - 2:35 p.m. Monday - Thursday\n"
    )
    row = make_town_hall_row(
        title="English Language Program", body_text=body, full_body=f"<p>{body}</p>"
    )
    candidate, diagnostics = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is None
    assert diagnostics["reason"] == "recurring_schedule"


def test_missing_event_time_fails_gracefully():
    row = make_town_hall_row(
        title="Homecoming",
        body_text="Homecoming is on Saturday, October 24, 2026. Join the celebration!",
        full_body="<p>Homecoming is on Saturday, October 24, 2026.</p>",
    )
    candidate, diagnostics = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is None
    assert diagnostics["reason"] == "no_event_time"


def test_rowan_midnight_means_no_time_not_a_midnight_event():
    row = make_town_hall_row(
        title="Some Event",
        body_text="An announcement with no stated time.",
        full_body="<p>An announcement with no stated time.</p>",
        is_event=1,
        event_date="2026-09-26",
        event_start_time="00:00:00",
    )
    candidate, diagnostics = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is None
    assert diagnostics["reason"] == "no_event_time"


# --- the acceptance event ----------------------------------------------------


def test_town_hall_date_is_october_14_2026(town_hall):
    assert town_hall.event_date == date(2026, 10, 14)


def test_town_hall_weekday_and_date_agree(town_hall):
    assert town_hall.event_date.strftime("%A") == "Wednesday"
    assert "weekday_confirmed" in town_hall.evidence


def test_town_hall_starts_at_ten(town_hall):
    assert town_hall.start == time(10, 0)


def test_town_hall_ends_at_noon(town_hall):
    """Two consecutive phases become one 10:00-12:00 block, not two events."""
    assert town_hall.end == time(12, 0)


def test_town_hall_preserves_its_internal_schedule(town_hall):
    spans = [(seg.start, seg.end) for seg in town_hall.segments]
    assert (time(10, 0), time(11, 15)) in spans
    assert (time(11, 15), time(12, 0)) in spans
    labels = " ".join(seg.label or "" for seg in town_hall.segments)
    assert "presentation and q&a" in labels
    assert "social" in labels


def test_town_hall_timezone_is_eastern(town_hall):
    assert town_hall.timezone == "America/New_York"
    assert events.DEFAULT_TIMEZONE == "America/New_York"


def test_town_hall_is_hybrid(town_hall):
    assert town_hall.attendance_mode == "hybrid"
    assert town_hall.virtual_detail == "WebEx"


def test_town_hall_location_is_the_eynon_ballroom(town_hall):
    assert town_hall.location == "Chamberlain Student Center, Eynon Ballroom"


def test_town_hall_keeps_the_real_registration_link(town_hall):
    assert town_hall.registration_urls == [
        "https://rowan.co1.qualtrics.com/jfe/form/SV_cIRdpy6HaBFYqBU"
    ]


def test_town_hall_keeps_every_source_link(town_hall):
    assert "https://rowan.co1.qualtrics.com/jfe/form/SV_4V0aCKxrPRWxvwO" in (
        town_hall.source_urls
    )


def test_town_hall_audience_is_bounded_not_the_whole_body(town_hall):
    assert town_hall.audience == (
        "This event is open to all faculty, staff, and managers"
    )


def test_town_hall_heading_hint_comes_from_the_body(town_hall):
    assert town_hall.heading_hint == "Provost’s Town Hall & Social"


# --- validation --------------------------------------------------------------


def test_a_weekday_contradicting_its_date_is_refused():
    """14 October 2026 is a Wednesday. `Monday, October 14` is a contradiction."""
    found = events.parse_dates("Monday, October 14, 2026", reference=REFERENCE)
    assert found == []


def test_a_weekday_agreeing_with_its_date_is_accepted():
    found = events.parse_dates("Wednesday, October 14, 2026", reference=REFERENCE)
    assert found == [(date(2026, 10, 14), True)]


def test_an_impossible_date_is_refused():
    assert events.parse_dates("February 30, 2026", reference=REFERENCE) == []


def test_an_omitted_year_resolves_forward_not_backward():
    found = events.parse_dates("October 14", reference=date(2026, 12, 1))
    assert found == [(date(2027, 10, 14), False)]


def test_end_before_start_is_refused():
    row = make_town_hall_row(
        is_event=1,
        event_date="2026-09-26",
        event_start_time="14:00:00",
        event_end_time="10:00:00",
        body_text="An event.",
        full_body="<p>An event.</p>",
    )
    candidate, diagnostics = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is None
    assert diagnostics["reason"] == "end_before_start"


def test_overlapping_timetables_are_refused():
    body = "Glassboro 9:00 - 2:35\nCamden 10:00 - 1:00\nTown hall session"
    row = make_town_hall_row(title="Two schedules", body_text=body, full_body=f"<p>{body}</p>")
    candidate, diagnostics = events.detect_candidate(row, reference_date=REFERENCE)
    assert candidate is None


# --- time parsing ------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("10:00 - 11:15", (time(10, 0), time(11, 15))),
        ("11:15 - 12:00", (time(11, 15), time(12, 0))),
        ("2:30-4:00", (time(14, 30), time(16, 0))),
        ("11:00-12:30", (time(11, 0), time(12, 30))),
        ("9:00 AM - 5:00 PM", (time(9, 0), time(17, 0))),
        ("7:00 p.m. to 11:00 p.m.", (time(19, 0), time(23, 0))),
        ("10 - 11 AM", (time(10, 0), time(11, 0))),
    ],
)
def test_time_ranges_resolve_sensibly(text, expected):
    segments = events.parse_time_ranges(text)
    assert segments, text
    assert (segments[0].start, segments[0].end) == expected


def test_consecutive_phases_merge_into_one_span():
    segments = events.parse_time_ranges(
        "10:00 - 11:15 - Presentation\n11:15 - 12:00 - Social"
    )
    assert events.merge_segments(segments) == (time(10, 0), time(12, 0))


def test_segments_that_overlap_are_not_sequential():
    segments = events.parse_time_ranges("9:00 - 2:35\n10:00 - 1:00")
    assert not events.segments_are_sequential(segments)


def test_repeated_identical_ranges_are_deduplicated():
    segments = events.parse_time_ranges("12:30 - 1:30\n12:30 - 1:30\n12:30 - 1:30")
    assert len(segments) == 1


# --- attendance --------------------------------------------------------------


def test_a_webex_only_session_is_virtual():
    mode, physical, virtual = events.detect_attendance(
        "Join us on WebEx. Registration required for the link.", None
    )
    assert mode == "virtual"
    assert physical is None
    assert virtual == "WebEx"


def test_a_room_with_no_virtual_option_is_in_person():
    mode, physical, _ = events.detect_attendance(
        "Location: Bunce Hall Auditorium", "Bunce Hall Auditorium"
    )
    assert mode == "in_person"
    assert physical == "Bunce Hall Auditorium"


def test_a_bare_hybrid_location_is_not_a_place_to_walk_to():
    mode, physical, _ = events.detect_attendance("Location: Hybrid", "Hybrid")
    assert physical is None
    assert mode in ("hybrid", "unknown", "virtual")


def test_registration_links_are_never_invented():
    assert events.registration_urls("<p>Register at the door.</p>") == []
    assert events.registration_urls(None) == []


def test_safelinks_hrefs_are_entity_decoded():
    html = '<a href="https://example.com/a?b=1&amp;c=2">Register</a>'
    assert events.registration_urls(html) == ["https://example.com/a?b=1&c=2"]
