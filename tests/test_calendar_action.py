"""The calendar action itself: ICS generation, the Outlook link, titles, safety.

Everything here is deterministic. No test in this file may reach the network or
launch a model -- the whole point of the design is that the calendar payload is
a pure function of stored announcement data.
"""

from __future__ import annotations

from datetime import date, datetime, time

import pytest

from dailymail import calendar_action, events
from conftest import REFERENCE, make_town_hall_row


@pytest.fixture
def town_hall():
    candidate, _ = events.detect_candidate(make_town_hall_row(), reference_date=REFERENCE)
    assert candidate is not None
    return candidate


@pytest.fixture
def description(town_hall):
    return calendar_action.build_description(
        town_hall,
        official_url="https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId=6694",
        contact_line="Sarah Fobes · Provost's Office · fobes@rowan.edu",
        travel_note=(
            "Actual event: 10:00 AM–12:00 PM.\n"
            "This calendar entry also reserves 10 minutes before and 10 minutes "
            "after for travel from/to 201 Mullica Hill Rd, Glassboro, NJ 08028."
        ),
        body_text=make_town_hall_row()["body_text"],
    )


@pytest.fixture
def action(town_hall, description):
    blocks = calendar_action.build_blocks(
        town_hall,
        content_hash="hash-6694",
        description=description,
        official_url="https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId=6694",
        title="Provost's Town Hall & Social",
        travel_minutes_before=10,
        travel_minutes_after=10,
        travel_mode="walk",
    )
    ics = calendar_action.build_ics(
        blocks,
        timezone=town_hall.timezone,
        dtstamp=calendar_action.official_ics_dtstamp("2026-08-28"),
    )
    return calendar_action.CalendarAction(
        submission_id="6694",
        title="Provost's Town Hall & Social",
        start=town_hall.start_datetime,
        end=town_hall.end_datetime,
        timezone=town_hall.timezone,
        location=town_hall.location,
        attendance_mode="hybrid",
        description=description,
        action_url=calendar_action.build_action_url(
            town_hall, description=description, title="Provost's Town Hall & Social"
        ),
        mechanism=calendar_action.MECHANISM_BOTH,
        ics_text=ics,
        ics_filename=calendar_action.slugify_filename("Provost's Town Hall & Social"),
        travel_minutes_before=10,
        travel_minutes_after=10,
        travel_mode="walk",
        blocks=blocks,
        registration_urls=town_hall.registration_urls,
        official_url="https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId=6694",
    )


# --- title cleanup -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Provost's Town Hall - Oct 14", "Provost's Town Hall"),
        ("CMSRU Research Day - November 3, 2026", "CMSRU Research Day"),
        ("Parking Lot W Closure Saturday, 8/29", "Parking Lot W Closure"),
        ("EVENT: Graduate Open House", "Graduate Open House"),
        ("[FACULTY] Digital Accessibility Briefing", "Digital Accessibility Briefing"),
        ("Homecoming (October 24)", "Homecoming"),
        # Preserved: proper names, acronyms, and a title that is only a date.
        ("RIPPAC Fall '26 Open House - 9/9", "RIPPAC Fall '26 Open House"),
        ("Graduate Open House", "Graduate Open House"),
        ("Oct 14", "Oct 14"),
    ],
)
def test_calendar_titles_shed_artefacts_but_never_gain_words(raw, expected):
    assert calendar_action.clean_calendar_title(raw) == expected


def test_title_cleanup_never_returns_empty():
    assert calendar_action.clean_calendar_title("", fallback="Fallback") == "Fallback"
    assert calendar_action.clean_calendar_title("10/14")


# --- ICS structure -----------------------------------------------------------


def test_ics_is_a_well_formed_vcalendar(action):
    assert action.ics_text.startswith("BEGIN:VCALENDAR\r\n")
    assert action.ics_text.rstrip().endswith("END:VCALENDAR")
    assert "VERSION:2.0" in action.ics_text
    assert "METHOD:PUBLISH" in action.ics_text
    assert "PRODID:-//DailyMail//Rowan Announcer digest//EN" in action.ics_text


def test_ics_uses_crlf_line_endings(action):
    assert "\r\n" in action.ics_text
    assert "\n" not in action.ics_text.replace("\r\n", "")


def test_ics_carries_a_real_vtimezone_for_eastern(action):
    assert "BEGIN:VTIMEZONE" in action.ics_text
    assert "TZID:America/New_York" in action.ics_text
    assert "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU" in action.ics_text


def test_ics_folds_long_lines_at_seventy_five_octets(action):
    for line in action.ics_text.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, line[:80]


def test_folding_never_splits_a_multibyte_character():
    line = "DESCRIPTION:" + "é" * 200
    folded = calendar_action.fold_line(line)
    # Round-trips exactly once the RFC 5545 unfolding is undone.
    assert folded.replace("\r\n ", "") == line


def test_ics_holds_three_events_when_travel_applies(action):
    assert action.ics_text.count("BEGIN:VEVENT") == 3
    kinds = [block.kind for block in action.blocks]
    assert kinds == ["travel_outbound", "event", "travel_return"]


def test_the_real_event_keeps_its_advertised_time(action):
    event = next(block for block in action.blocks if block.kind == "event")
    assert event.start == datetime(2026, 10, 14, 10, 0)
    assert event.end == datetime(2026, 10, 14, 12, 0)
    assert "DTSTART;TZID=America/New_York:20261014T100000" in action.ics_text
    assert "DTEND;TZID=America/New_York:20261014T120000" in action.ics_text


def test_travel_blocks_bracket_the_event_without_moving_it(action):
    outbound = next(b for b in action.blocks if b.kind == "travel_outbound")
    inbound = next(b for b in action.blocks if b.kind == "travel_return")
    assert outbound.end == datetime(2026, 10, 14, 10, 0)
    assert outbound.start == datetime(2026, 10, 14, 9, 50)
    assert inbound.start == datetime(2026, 10, 14, 12, 0)
    assert inbound.end == datetime(2026, 10, 14, 12, 10)


def test_travel_blocks_say_the_real_event_time(action):
    outbound = next(b for b in action.blocks if b.kind == "travel_outbound")
    assert "starts at 10:00 AM" in outbound.description


def test_no_travel_means_a_single_vevent(town_hall, description):
    blocks = calendar_action.build_blocks(
        town_hall, content_hash="h", description=description, official_url="https://x",
    )
    ics = calendar_action.build_ics(
        blocks, timezone="America/New_York",
        dtstamp=calendar_action.official_ics_dtstamp("2026-08-28"),
    )
    assert ics.count("BEGIN:VEVENT") == 1


def test_uids_are_stable_across_runs_and_distinct_per_block(town_hall, description):
    def build():
        return calendar_action.build_blocks(
            town_hall, content_hash="hash-6694", description=description,
            official_url="https://x", travel_minutes_before=10,
            travel_minutes_after=10,
        )

    first, second = build(), build()
    assert [b.uid for b in first] == [b.uid for b in second]
    assert len({b.uid for b in first}) == 3


def test_a_content_change_changes_the_uid(town_hall, description):
    a = calendar_action.build_uid("6694", "hash-a", "event")
    b = calendar_action.build_uid("6694", "hash-b", "event")
    assert a != b


def test_dtstamp_is_deterministic_not_wall_clock(action):
    assert "DTSTAMP:20260828T000000Z" in action.ics_text


def test_generating_the_same_action_twice_is_byte_identical(town_hall, description):
    def build():
        blocks = calendar_action.build_blocks(
            town_hall, content_hash="hash-6694", description=description,
            official_url="https://x", travel_minutes_before=10, travel_minutes_after=10,
        )
        return calendar_action.build_ics(
            blocks, timezone="America/New_York",
            dtstamp=calendar_action.official_ics_dtstamp("2026-08-28"),
        )

    assert build() == build()


# --- escaping and injection --------------------------------------------------


def test_ics_text_escaping_follows_rfc_5545():
    assert calendar_action.escape_ics("a,b") == "a\\,b"
    assert calendar_action.escape_ics("a;b") == "a\\;b"
    assert calendar_action.escape_ics("a\\b") == "a\\\\b"
    assert calendar_action.escape_ics("a\nb") == "a\\nb"
    # Backslash first, or our own escapes would be double-escaped.
    assert calendar_action.escape_ics("\\,") == "\\\\\\,"


def test_punctuation_ampersands_and_apostrophes_survive_the_ics(action):
    assert "Provost's Town Hall & Social" in action.ics_text
    assert "Chamberlain Student Center\\, Eynon Ballroom" in action.ics_text


def test_unicode_survives_the_ics(action):
    assert "–" in action.ics_text or "–" in action.ics_text
    action.ics_text.encode("utf-8")  # must not raise


def test_control_characters_are_stripped_not_escaped():
    assert calendar_action.sanitize_text("a\x00b\x07c") == "abc"
    assert calendar_action.sanitize_text("a\r\nb") == "a\nb"


def _content_lines(ics: str) -> list[str]:
    """The lines a parser sees: unfolded, and split on real CRLF only."""
    return ics.replace("\r\n ", "").split("\r\n")


def test_a_body_cannot_inject_an_ics_property(town_hall):
    """A newline inside announcement text must be escaped, never emitted raw.

    The escaped form `\\n` legitimately contains the substring `BEGIN:VEVENT`,
    so this asserts on parsed *content lines* -- what an iCalendar reader
    actually acts on -- rather than on a substring of the file.
    """
    hostile = "Free coffee\r\nEND:VEVENT\r\nBEGIN:VEVENT\r\nSUMMARY:Injected"
    block = calendar_action.CalendarBlock(
        uid="u@x", summary=calendar_action.sanitize_text(hostile),
        start=datetime(2026, 10, 14, 10), end=datetime(2026, 10, 14, 11),
    )
    ics = calendar_action.build_ics(
        [block], timezone="America/New_York",
        dtstamp=calendar_action.official_ics_dtstamp("2026-08-28"),
    )
    lines = _content_lines(ics)
    assert lines.count("BEGIN:VEVENT") == 1
    assert lines.count("END:VEVENT") == 1
    assert "SUMMARY:Injected" not in lines
    # The hostile text survives as inert, correctly escaped content.
    summary = next(line for line in lines if line.startswith("SUMMARY:"))
    assert summary == (
        "SUMMARY:Free coffee\\nEND:VEVENT\\nBEGIN:VEVENT\\nSUMMARY:Injected"
    )


def test_a_title_cannot_inject_a_mime_header_through_the_filename():
    name = calendar_action.slugify_filename('evil"\r\nBcc: attacker@example.com')
    assert "\r" not in name and "\n" not in name and '"' not in name
    assert "/" not in name and "\\" not in name
    assert name.endswith(".ics")


def test_slugified_filenames_stay_reasonable():
    assert calendar_action.slugify_filename("Provost's Town Hall & Social") == (
        "Provost-s-Town-Hall-Social.ics"
    )
    assert calendar_action.slugify_filename("!!!") == "event.ics"
    assert len(calendar_action.slugify_filename("x" * 500)) <= 64


# --- the Outlook deep link ---------------------------------------------------


def test_action_url_is_an_outlook_compose_deeplink(action):
    assert action.action_url.startswith(
        "https://outlook.office.com/calendar/deeplink/compose?"
    )
    assert "path=%2Fcalendar%2Faction%2Fcompose" in action.action_url
    assert "rru=addevent" in action.action_url


def test_action_url_carries_the_real_time_not_the_travel_padded_one(action):
    assert "startdt=2026-10-14T10%3A00%3A00" in action.action_url
    assert "enddt=2026-10-14T12%3A00%3A00" in action.action_url


def test_action_url_encodes_punctuation_ampersands_and_apostrophes(action):
    assert "subject=Provost%27s%20Town%20Hall%20%26%20Social" in action.action_url
    assert "+" not in action.action_url.split("subject=")[1].split("&")[0]


def test_action_url_encodes_unicode(town_hall):
    url = calendar_action.build_action_url(
        town_hall, description="curly ’ quote", title="Café Résumé"
    )
    assert "Caf%C3%A9" in url
    assert all(ord(char) < 128 for char in url)


def test_action_url_has_no_character_a_mail_client_would_break_on(action):
    for char in ("\n", "\r", " ", '"', "<", ">"):
        assert char not in action.action_url


def test_a_very_long_description_trims_the_body_never_the_event(town_hall):
    url = calendar_action.build_action_url(
        town_hall, description="x" * 40_000, title="Provost's Town Hall"
    )
    assert len(url) <= calendar_action.MAX_ACTION_URL_CHARS
    assert "startdt=2026-10-14T10%3A00%3A00" in url
    assert "subject=Provost%27s%20Town%20Hall" in url


# --- description -------------------------------------------------------------


def test_description_states_the_actual_event_time(description):
    assert "Wednesday, October 14, 2026" in description
    assert "10:00 AM–12:00 PM (Eastern)" in description


def test_description_preserves_the_internal_schedule(description):
    assert "Schedule" in description
    assert "10:00 AM–11:15 AM — Presentation and Q&A" in description
    assert "11:15 AM–12:00 PM — Social" in description


def test_description_records_hybrid_and_the_virtual_option(description):
    assert "Hybrid event." in description
    assert "Virtual option: WebEx" in description


def test_description_carries_the_in_person_location(description):
    assert "Chamberlain Student Center, Eynon Ballroom" in description


def test_description_carries_the_real_registration_link(description):
    assert "https://rowan.co1.qualtrics.com/jfe/form/SV_cIRdpy6HaBFYqBU" in description


def test_description_carries_the_contact(description):
    assert "Sarah Fobes" in description


def test_description_carries_the_official_announcement_url(description):
    assert (
        "https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId=6694"
        in description
    )


def test_description_explains_the_travel_reservation(description):
    assert "Actual event: 10:00 AM–12:00 PM." in description
    assert "reserves 10 minutes before and 10 minutes after" in description
    assert "201 Mullica Hill Rd" in description


def test_description_never_leaks_internal_ranking_notes(description):
    lowered = description.lower()
    for banned in ("relevance", "urgency", "rationale", "fallback score",
                   "curation", "submission_id", "confidence"):
        assert banned not in lowered


def test_description_never_invents_a_link(town_hall):
    text = calendar_action.build_description(town_hall, official_url="https://x")
    import re

    for url in re.findall(r"https?://\S+", text):
        assert url in (
            "https://x",
            "https://rowan.co1.qualtrics.com/jfe/form/SV_cIRdpy6HaBFYqBU",
        )


def test_an_assumed_end_time_is_declared_not_presented_as_fact():
    candidate = events.EventCandidate(
        submission_id="1", title="Briefing", event_date=date(2026, 10, 14),
        start=time(10, 0), end=None,
    )
    text = calendar_action.build_description(candidate, official_url="https://x")
    assert "one hour assumed" in text


# --- validation --------------------------------------------------------------


def test_validate_accepts_a_good_action(action):
    calendar_action.validate_action(action)


def test_validate_rejects_an_inverted_time(action):
    action.end = action.start
    with pytest.raises(ValueError):
        calendar_action.validate_action(action)


def test_validate_rejects_a_non_outlook_url(action):
    action.action_url = "https://evil.example.com/?x=1"
    with pytest.raises(ValueError):
        calendar_action.validate_action(action)


def test_validate_rejects_a_truncated_ics(action):
    action.ics_text = action.ics_text[:200]
    with pytest.raises(ValueError):
        calendar_action.validate_action(action)


def test_validate_rejects_an_unusable_filename(action):
    action.ics_filename = "../../etc/passwd.ics"
    with pytest.raises(ValueError):
        calendar_action.validate_action(action)


def test_validate_rejects_an_empty_title(action):
    action.title = "   "
    with pytest.raises(ValueError):
        calendar_action.validate_action(action)


# --- display helpers ---------------------------------------------------------


def test_when_line_is_compact_enough_for_a_phone(action):
    assert action.when_line == "Wed, Oct 14 · 10:00 AM–12:00 PM"
    assert len(action.when_line) < 40


def test_location_line_names_the_place_and_the_mode(action):
    assert action.location_line == (
        "Chamberlain Student Center, Eynon Ballroom · In person / Hybrid"
    )


def test_travel_line_is_self_contained(action):
    assert action.travel_line == "Includes 10 min travel before and after"


def test_an_estimated_travel_line_says_so(action):
    action.travel_estimated = True
    assert action.travel_line.endswith("(estimated)")


def test_asymmetric_travel_reads_correctly(action):
    action.travel_minutes_before, action.travel_minutes_after = 25, 20
    assert action.travel_line == "Includes 25 min travel before and 20 min after"
