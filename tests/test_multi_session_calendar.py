"""One announcement, several sittings: a calendar action per selectable session.

The acceptance case is Rowan announcement 6702, the Provost's Coffee Hours of
1 September 2026, verbatim from production:

    Our August sessions will be focused on research. The dates are:
    Thursday, September 10 from 11:00-12:30
    Monday, September 21 from 2:30-4:00

    ... register your interest by completing this form
    (go.rowan.edu/CoffeeSept26) by Monday, September 7th. These sessions are
    limited to 10-12 people ...

The previous implementation withheld it outright as `multiple_distinct_dates`.
Refusing to span the two dates as one appointment, and refusing to silently pick
one of them, were both right. Throwing the announcement away was not -- so what
the reader actually got was Outlook's own automatic date-linking, which produces
an event with no subject worth reading, no location, no registration link and no
description.

Three properties are load-bearing here and each has a test:

  * the extraction is definite or it does not happen -- the deadline sentence
    "by Monday, September 7th. These sessions are limited to 10-12 people" holds
    a date and a range and must not become a third sitting;
  * relevance is judged once for the series, so a two-session announcement costs
    no extra model call; and
  * every offered sitting is unambiguously distinguishable -- its own button, its
    own time, its own `.ics` filename.
"""

from __future__ import annotations

from datetime import date, datetime

import pytest

from conftest import (
    COFFEE_HOURS_BODY,
    COFFEE_HOURS_TEXT,
    REFERENCE,
    make_town_hall_row,
)
from dailymail import calendar_action, calendar_enrich, curate, db, events, parking

TARGET = "2026-09-01"
COFFEE_ID = 6702


def coffee_row(**overrides) -> dict:
    row = {
        "submission_id": COFFEE_ID,
        "title": "Provost’s Coffee Hours (Focus on Research)",
        "full_body": COFFEE_HOURS_BODY,
        "body_text": COFFEE_HOURS_TEXT,
        "is_event": 0,
        "event_name": None,
        "event_date": None,
        "event_start_time": None,
        "event_end_time": None,
        "event_location": None,
        "category_title": "Glassboro Campus",
        "category_id": 12,
        "source_audience": "Employees",
        "content_hash": "hash-6702",
        "official_url": (
            "https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId=6702"
        ),
    }
    row.update(overrides)
    return row


@pytest.fixture
def sessions():
    found, diagnostics = events.detect_series(
        coffee_row(), reference_date=date(2026, 9, 1)
    )
    assert found, diagnostics
    return found


# --- extraction --------------------------------------------------------------


def test_two_sittings_are_extracted_from_one_announcement(sessions):
    assert len(sessions) == 2
    assert [
        (s.event_date.isoformat(), s.start.isoformat(), s.end.isoformat())
        for s in sessions
    ] == [
        ("2026-09-10", "11:00:00", "12:30:00"),
        ("2026-09-21", "14:30:00", "16:00:00"),
    ]


def test_the_registration_deadline_never_becomes_a_third_sitting(sessions):
    """`by Monday, September 7th. These sessions are limited to 10-12 people`.

    One date and one time range on the same line, and emphatically not a
    session. Two independent guards reject it: the connective text between them
    crosses a full stop, and the line is far longer than a schedule entry.
    """
    assert date(2026, 9, 7) not in {s.event_date for s in sessions}
    assert len(sessions) == 2


def test_each_session_knows_its_place_in_the_series(sessions):
    assert [s.session_index for s in sessions] == [1, 2]
    assert {s.session_count for s in sessions} == {2}
    assert all(s.is_session_of_series for s in sessions)
    for session in sessions:
        assert session.sibling_sessions == [
            (date(2026, 9, 10), session.sibling_sessions[0][1], session.sibling_sessions[0][2]),
            (date(2026, 9, 21), session.sibling_sessions[1][1], session.sibling_sessions[1][2]),
        ]


def test_series_metadata_is_shared_by_every_session(sessions):
    """Location, links and audience belong to the announcement, not the sitting."""
    first, second = sessions
    assert first.registration_urls == second.registration_urls
    assert first.registration_urls == ["http://go.rowan.edu/CoffeeSept26"]
    assert first.source_urls == second.source_urls
    assert first.attendance_mode == second.attendance_mode
    assert first.location == second.location
    assert first.title == second.title


def test_three_sittings_produce_three_sessions():
    body = (
        "Provost's Coffee Hours are small-group conversations.\n"
        "The dates are:\n"
        "Thursday, September 10 from 11:00-12:30\n"
        "Monday, September 21 from 2:30-4:00\n"
        "Friday, October 9 from 9:00-10:30\n"
    )
    found, _ = events.detect_series(
        coffee_row(body_text=body, full_body=f"<p>{body}</p>"),
        reference_date=date(2026, 9, 1),
    )
    assert [s.event_date.isoformat() for s in found] == [
        "2026-09-10", "2026-09-21", "2026-10-09",
    ]
    assert {s.session_count for s in found} == {3}


def test_a_wall_of_sittings_is_a_timetable_and_is_withheld():
    """Past `MAX_SESSIONS` this is a term schedule, not a choice."""
    lines = "\n".join(
        f"Monday, September {day} from 11:00-12:30" for day in (7, 14, 21, 28)
    ) + "\n" + "\n".join(
        f"Monday, October {day} from 11:00-12:30" for day in (5, 12, 19)
    )
    body = f"Coffee Hours are small-group conversations.\nThe dates are:\n{lines}\n"
    found, diagnostics = events.detect_series(
        coffee_row(body_text=body, full_body=f"<p>{body}</p>"),
        reference_date=date(2026, 9, 1),
    )
    assert found == []
    assert diagnostics["reason"] == "too_many_sessions"


def test_a_recurring_line_is_never_read_as_a_sitting():
    """`Mondays 2:30-4:00` repeats; it is not one appointment to reserve.

    Asserted against `parse_sessions` directly, because that is where the rule
    lives: an open-ended weekly slot is skipped while a dated sitting on the next
    line is kept.
    """
    text = (
        "Coffee Hours are small-group conversations.\n"
        "Mondays 2:30-4:00 through the semester\n"
        "Thursday, September 10 from 11:00-12:30\n"
        "Monday, September 21 from 2:30-4:00\n"
    )
    found = events.parse_sessions(text, reference=date(2026, 9, 1))
    assert [
        (spec.event_date.isoformat(), spec.start.isoformat()) for spec in found
    ] == [("2026-09-10", "11:00:00"), ("2026-09-21", "14:30:00")]


def test_a_body_advertising_a_weekly_slot_offers_nothing():
    """End to end, an open-ended weekly schedule is still withheld entirely."""
    body = (
        "Coffee Hours are small-group conversations.\n"
        "Mondays 2:30-4:00 through the semester\n"
        "Thursday, September 10 from 11:00-12:30\n"
    )
    found, diagnostics = events.detect_series(
        coffee_row(body_text=body, full_body=f"<p>{body}</p>"),
        reference_date=date(2026, 9, 1),
    )
    assert found == []
    # Only one dated sitting, so this never reaches the session path at all --
    # the pre-existing recurrence veto refuses it first, which is the same
    # answer for a better reason.
    assert diagnostics["reason"] == "recurring_schedule"


def test_two_ranges_on_one_day_are_phases_not_sittings():
    """The Town Hall's `10:00-11:15` and `11:15-12:00` are one event in two parts."""
    found, _ = events.detect_series(make_town_hall_row(), reference_date=REFERENCE)
    assert len(found) == 1
    assert found[0].session_count == 1
    assert found[0].start.isoformat() == "10:00:00"
    assert found[0].end.isoformat() == "12:00:00"
    assert len(found[0].segments) == 2


def test_an_ordinary_single_event_is_unchanged_by_the_session_path():
    """The single-session regression: nothing about the Town Hall may move."""
    candidate, diagnostics = events.detect_candidate(
        make_town_hall_row(), reference_date=REFERENCE
    )
    assert candidate is not None, diagnostics
    assert candidate.session_index == 1
    assert candidate.session_count == 1
    assert not candidate.is_session_of_series
    assert candidate.sibling_sessions == []
    assert candidate.location == "Chamberlain Student Center, Eynon Ballroom"
    assert candidate.attendance_mode == "hybrid"


# --- the calendar actions ----------------------------------------------------


@pytest.fixture
def calendar_db(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    with db.transaction(connection):
        connection.execute(
            "INSERT INTO parking_landmarks (campus, name, normalized_name, "
            "latitude, longitude, last_verified_at) VALUES "
            "('glassboro', 'Chamberlain Student Center', ?, 39.708828, "
            "-75.117798, ?)",
            (parking.normalize_name("Chamberlain Student Center"), db.now_utc()),
        )
    yield connection
    connection.close()


def _seed(connection, *records) -> list:
    with db.transaction(connection):
        for record in records:
            db.upsert_category(
                connection, category_id=record["category_id"],
                title=record.get("category_title") or "Glassboro Campus",
                rowan_rank=1, color=None, is_active=True, manual_priority=10,
            )
            payload = {
                "submission_id": record["submission_id"],
                "title": record["title"],
                "full_body": record["full_body"],
                "body_text": record["body_text"],
                "source_audience": record["source_audience"],
                "category_id": record["category_id"],
                "distribution_dates": [TARGET],
                "first_distribution_date": TARGET,
                "status": "New",
                "is_event": record.get("is_event") or 0,
                "event_name": record.get("event_name"),
                "event_date": record.get("event_date"),
                "event_start_time": record.get("event_start_time"),
                "event_end_time": record.get("event_end_time"),
                "event_location": record.get("event_location"),
            }
            version_id, _ = db.record_announcement(
                connection, payload, observed_at=db.now_utc()
            )
            db.record_daily(
                connection, target_date=TARGET,
                submission_id=record["submission_id"], version_id=version_id,
                status="New", changed=False, observed_at=db.now_utc(),
            )
    return db.digest_rows(connection, TARGET)


def _offer(submission_id) -> list[dict]:
    return [
        {
            "submission_id": str(submission_id),
            "calendar": {
                "offer": True, "confidence": 0.95, "reason": "relevant",
                "attendance_mode": None, "suggested_title": None,
            },
        }
    ]


def _enrich(connection, rows, settings, *, judgements=None, method="fallback"):
    candidates, diagnostics = calendar_enrich.detect_candidates(
        rows, target_date=TARGET, settings=settings
    )
    return calendar_enrich.enrich_digest(
        connection, rows, target_date=TARGET, settings=settings,
        candidates=candidates, diagnostics=diagnostics,
        curation_entries=judgements, curation_method=method,
        allow_routing=False,
    )


@pytest.fixture
def coffee_action(calendar_db, settings_obj):
    rows = _seed(calendar_db, coffee_row())
    actions, metrics = _enrich(
        calendar_db, rows, settings_obj,
        judgements=_offer(COFFEE_ID), method="claude",
    )
    assert str(COFFEE_ID) in actions, metrics.as_dict()
    return actions[str(COFFEE_ID)], metrics


def test_each_sitting_gets_its_own_calendar_control(coffee_action):
    action, metrics = coffee_action
    assert action.is_multi_session
    assert action.session_count == 2
    assert metrics.actions_offered == 1
    assert metrics.session_actions_offered == 2
    assert metrics.multi_session_announcements == 1


def test_the_calendar_title_is_clean_and_carries_no_date(coffee_action):
    action, _ = coffee_action
    assert action.title == "Provost’s Coffee Hours - Focus on Research"
    for session in action.sessions:
        assert session.title == action.title
        for fragment in ("Sep", "September", "10", "21", "/"):
            assert fragment not in session.title


def test_each_session_carries_its_own_date_and_times(coffee_action):
    action, _ = coffee_action
    assert [(s.start.isoformat(), s.end.isoformat()) for s in action.sessions] == [
        ("2026-09-10T11:00:00", "2026-09-10T12:30:00"),
        ("2026-09-21T14:30:00", "2026-09-21T16:00:00"),
    ]
    assert {s.timezone for s in action.sessions} == {"America/New_York"}


def test_no_session_spans_the_gap_between_sittings(coffee_action):
    """The failure mode the old rejection was protecting against."""
    action, _ = coffee_action
    for session in action.sessions:
        assert session.start.date() == session.end.date()
        assert (session.end - session.start).total_seconds() <= 2 * 3600


def test_each_session_button_names_its_own_date(coffee_action):
    action, _ = coffee_action
    labels = [session.button_label for session in action.sessions]
    assert labels == ["Add Thu, Sep 10", "Add Mon, Sep 21"]
    assert len(set(labels)) == 2


def test_a_single_session_button_keeps_the_original_wording(calendar_db, settings_obj):
    rows = _seed(calendar_db, make_town_hall_row(category_id=12))
    actions, _ = _enrich(
        calendar_db, rows, settings_obj, judgements=_offer(6694), method="claude"
    )
    action = actions["6694"]
    assert not action.is_multi_session
    assert action.sessions[0].button_label == "Add to Calendar"
    assert action.sessions_line is None


def test_each_session_targets_a_distinct_outlook_deep_link(coffee_action):
    action, _ = coffee_action
    urls = [session.action_url for session in action.sessions]
    assert len(set(urls)) == 2
    assert "startdt=2026-09-10T11%3A00%3A00" in urls[0]
    assert "enddt=2026-09-10T12%3A30%3A00" in urls[0]
    assert "startdt=2026-09-21T14%3A30%3A00" in urls[1]
    assert "enddt=2026-09-21T16%3A00%3A00" in urls[1]
    for url in urls:
        assert url.startswith(calendar_action.OUTLOOK_COMPOSE_URL + "?")


def test_every_session_has_a_unique_non_colliding_attachment_name(coffee_action):
    action, _ = coffee_action
    names = [session.ics_filename for session in action.sessions]
    assert names == [
        "Provost-s-Coffee-Hours-Focus-on-Research-2026-09-10.ics",
        "Provost-s-Coffee-Hours-Focus-on-Research-2026-09-21.ics",
    ]
    assert len(set(names)) == 2
    for name in names:
        assert name.endswith(".ics")
        assert "/" not in name and "\\" not in name


def test_the_ics_for_each_session_holds_that_session(coffee_action):
    action, _ = coffee_action
    first, second = action.sessions
    assert "DTSTART;TZID=America/New_York:20260910T110000" in first.ics_text
    assert "DTEND;TZID=America/New_York:20260910T123000" in first.ics_text
    assert "DTSTART;TZID=America/New_York:20260921T143000" in second.ics_text
    assert "DTEND;TZID=America/New_York:20260921T160000" in second.ics_text
    for session in action.sessions:
        assert session.ics_text.startswith("BEGIN:VCALENDAR")
        assert session.ics_text.rstrip().endswith("END:VCALENDAR")
        assert session.ics_text.count("BEGIN:VEVENT") == 1
        assert "BEGIN:VTIMEZONE" in session.ics_text
        assert "\r\n" in session.ics_text


def test_each_session_gets_a_distinct_calendar_uid(coffee_action):
    """A shared UID would make the second import overwrite the first."""
    action, _ = coffee_action
    uids = [block.uid for session in action.sessions for block in session.blocks]
    assert len(set(uids)) == len(uids)


def test_a_single_event_keeps_the_uids_and_filename_it_already_had(
    calendar_db, settings_obj
):
    """Re-rendering a past day must still produce byte-identical calendar data."""
    rows = _seed(calendar_db, make_town_hall_row(category_id=12))
    actions, _ = _enrich(
        calendar_db, rows, settings_obj, judgements=_offer(6694), method="claude"
    )
    session = actions["6694"].sessions[0]
    assert session.ics_filename == "Provost-s-Town-Hall.ics"
    assert all(
        block.uid.endswith(("-event@dailymail.rowan",
                            "-travel-out@dailymail.rowan",
                            "-travel-back@dailymail.rowan"))
        for block in session.blocks
    )


def test_the_description_says_which_sitting_it_is_and_what_the_others_were(
    coffee_action
):
    action, _ = coffee_action
    first, second = action.sessions
    assert "This is session 1 of 2" in first.description
    assert "This is session 2 of 2" in second.description
    for session in action.sessions:
        assert "Thursday, September 10" in session.description
        assert "Monday, September 21" in session.description
        assert "(this entry)" in session.description
        assert "Registering for one session does not reserve the others" in (
            session.description
        )
    # Only one sitting is marked as the entry, in each file.
    assert first.description.count("(this entry)") == 1
    assert second.description.count("(this entry)") == 1


def test_the_description_carries_the_useful_source_detail(coffee_action):
    action, _ = coffee_action
    description = action.sessions[0].description
    assert "small-group conversations with the Provost" in description
    assert "Light refreshments are provided." in description
    assert "http://go.rowan.edu/CoffeeSept26" in description
    assert (
        "https://apps.rowan.edu/RowanAnnouncer/Announcement?SubmissionId=6702"
        in description
    )
    # Never our own notes about the announcement.
    for banned in ("relevance", "urgency", "rationale", "confidence", "curation"):
        assert banned not in description.lower()


def test_a_series_with_no_room_reserves_no_travel(coffee_action):
    """Coffee Hours names no venue, so there is nothing to route to."""
    action, _ = coffee_action
    assert action.location is None
    assert not action.has_travel
    for session in action.sessions:
        assert session.travel_mode == "none"
        assert session.travel_minutes_before == session.travel_minutes_after == 0
        assert session.travel_line is None
        assert [block.kind for block in session.blocks] == ["event"]


def test_travel_is_reserved_per_session_when_there_is_a_venue(
    calendar_db, settings_obj
):
    """A series in a real room gets a hold either side of *each* sitting.

    Built by moving the Coffee Hours sittings into the Town Hall's ballroom, so
    the venue resolves through the existing cache and travel subsystem rather
    than a second routing implementation.
    """
    body = (
        "Provost's Coffee Hours are small-group conversations.\n"
        "Location: Chamberlain Student Center, Eynon Ballroom\n"
        "The dates are:\n"
        "Thursday, September 10 from 11:00-12:30\n"
        "Monday, September 21 from 2:30-4:00\n"
    )
    rows = _seed(
        calendar_db,
        coffee_row(body_text=body, full_body=f"<p>{body}</p>"),
    )
    actions, metrics = _enrich(
        calendar_db, rows, settings_obj,
        judgements=_offer(COFFEE_ID), method="claude",
    )
    action = actions[str(COFFEE_ID)]
    assert action.session_count == 2
    assert action.location == "Chamberlain Student Center, Eynon Ballroom"
    assert action.has_travel
    for session in action.sessions:
        assert session.travel_minutes_before > 0
        assert session.travel_minutes_after > 0
        assert [block.kind for block in session.blocks] == [
            "travel_outbound", "event", "travel_return",
        ]
        # A travel hold never alters the advertised event time.
        event_block = next(b for b in session.blocks if b.kind == "event")
        assert event_block.start == session.start
        assert event_block.end == session.end
        outbound = next(b for b in session.blocks if b.kind == "travel_outbound")
        assert outbound.end == session.start
    # The venue is resolved once for the announcement, not once per sitting.
    assert metrics.venue_cache_misses + metrics.venue_cache_hits == 1


# --- relevance is asked once --------------------------------------------------


def test_relevance_is_judged_once_for_the_whole_series(calendar_db, settings_obj):
    rows = _seed(calendar_db, coffee_row())
    actions, _ = _enrich(
        calendar_db, rows, settings_obj,
        judgements=_offer(COFFEE_ID), method="claude",
    )
    stored = db.calendar_recommendations(calendar_db, TARGET)
    rows_for_coffee = [r for r in stored if r["submission_id"] == COFFEE_ID]
    assert len(rows_for_coffee) == 1, "one audit row per announcement, not per sitting"
    assert rows_for_coffee[0]["session_count"] == 2
    assert rows_for_coffee[0]["relevance_method"] == calendar_enrich.METHOD_CLAUDE
    import json as _json

    persisted = _json.loads(rows_for_coffee[0]["sessions"])
    assert [entry["index"] for entry in persisted] == [1, 2]
    assert len({entry["ics_filename"] for entry in persisted}) == 2


def test_the_curation_payload_asks_about_the_series_not_each_sitting(settings_obj):
    """No second model call, and no per-session question in the first one."""
    connection = db.connect()
    db.initialize(connection)
    try:
        rows = _seed(connection, coffee_row())
        candidates, _ = calendar_enrich.detect_candidates(
            rows, target_date=TARGET, settings=settings_obj
        )
        payload = curate.build_payload(
            rows, TARGET, settings_obj, event_candidates=candidates
        )
        items = payload["new_items"] + payload["standing_items"]
        entries = [item for item in items if item.get("event_candidate")]
        assert len(entries) == 1, "one event_candidate block for the announcement"
        block = entries[0]["event_candidate"]
        assert block["date"] == "2026-09-10"
        assert [session["date"] for session in block["sessions"]] == [
            "2026-09-10", "2026-09-21",
        ]
    finally:
        connection.close()


def test_declining_the_series_withholds_every_sitting(calendar_db, settings_obj):
    rows = _seed(calendar_db, coffee_row())
    decline = [
        {
            "submission_id": str(COFFEE_ID),
            "calendar": {
                "offer": False, "confidence": 0.9, "reason": "not for this reader",
                "attendance_mode": None, "suggested_title": None,
            },
        }
    ]
    actions, metrics = _enrich(
        calendar_db, rows, settings_obj, judgements=decline, method="claude"
    )
    assert actions == {}
    assert metrics.session_actions_offered == 0
    assert metrics.withheld_by_relevance == 1


def test_a_series_counts_as_one_action_against_the_digest_cap(
    calendar_db, settings_obj
):
    """The cap limits announcements, not buttons: a series is one decision."""
    import dataclasses

    rows = _seed(calendar_db, coffee_row(), make_town_hall_row(category_id=12))
    capped = dataclasses.replace(settings_obj, calendar_max_actions=1)
    actions, metrics = _enrich(
        calendar_db, rows, capped,
        judgements=_offer(COFFEE_ID) + _offer(6694), method="claude",
    )
    assert len(actions) == 1
    assert metrics.withheld_other == 1


# --- validation ---------------------------------------------------------------


def test_validation_rejects_two_sittings_at_the_same_moment():
    """Indistinguishable sittings are a defect even if each one is well formed."""
    def session(index, start_hour, filename):
        return calendar_action.CalendarSession(
            index=index, count=2, title="Coffee Hours",
            start=datetime(2026, 9, 10, start_hour, 0),
            end=datetime(2026, 9, 10, start_hour + 1, 0),
            timezone="America/New_York", location=None,
            attendance_mode="unknown", description="",
            action_url=calendar_action.OUTLOOK_COMPOSE_URL + "?x=1",
            mechanism=calendar_action.MECHANISM_DEEPLINK,
            ics_filename=filename,
        )

    ok = calendar_action.CalendarAction.from_sessions(
        submission_id="1", title="Coffee Hours", timezone="America/New_York",
        location=None, attendance_mode="unknown",
        sessions=[session(1, 11, "a.ics"), session(2, 14, "b.ics")],
    )
    calendar_action.validate_action(ok)

    clashing = calendar_action.CalendarAction.from_sessions(
        submission_id="1", title="Coffee Hours", timezone="America/New_York",
        location=None, attendance_mode="unknown",
        sessions=[session(1, 11, "a.ics"), session(2, 11, "b.ics")],
    )
    with pytest.raises(ValueError, match="both start at"):
        calendar_action.validate_action(clashing)


def test_validation_rejects_two_sittings_sharing_an_attachment_name():
    def session(index, start_hour):
        return calendar_action.CalendarSession(
            index=index, count=2, title="Coffee Hours",
            start=datetime(2026, 9, 10, start_hour, 0),
            end=datetime(2026, 9, 10, start_hour + 1, 0),
            timezone="America/New_York", location=None,
            attendance_mode="unknown", description="",
            action_url=calendar_action.OUTLOOK_COMPOSE_URL + "?x=1",
            mechanism=calendar_action.MECHANISM_DEEPLINK,
            ics_filename="same.ics",
        )

    action = calendar_action.CalendarAction.from_sessions(
        submission_id="1", title="Coffee Hours", timezone="America/New_York",
        location=None, attendance_mode="unknown",
        sessions=[session(1, 11), session(2, 14)],
    )
    with pytest.raises(ValueError, match="share the attachment name"):
        calendar_action.validate_action(action)


def test_a_single_session_action_derives_its_session_from_its_own_fields():
    """No duplicated state: the one session is a view, not a copy."""
    action = calendar_action.CalendarAction(
        submission_id="1", title="Town Hall",
        start=datetime(2026, 10, 14, 10), end=datetime(2026, 10, 14, 12),
        timezone="America/New_York", location="Eynon Ballroom",
        attendance_mode="hybrid", description="",
        action_url=calendar_action.OUTLOOK_COMPOSE_URL + "?x=1",
        mechanism=calendar_action.MECHANISM_DEEPLINK,
    )
    assert action.session_count == 1
    assert not action.is_multi_session
    assert action.sessions[0].start == action.start
    # Adjust the action; the derived session must follow immediately.
    action.travel_minutes_before = action.travel_minutes_after = 25
    assert action.sessions[0].travel_minutes_before == 25
    assert action.has_travel
    assert action.travel_line == "Includes 25 min travel before and after"


# --- rendering ---------------------------------------------------------------


def test_the_email_renders_one_control_per_sitting(calendar_db, settings_obj):
    from dailymail import render

    rows = _seed(calendar_db, coffee_row())
    actions, _ = _enrich(
        calendar_db, rows, settings_obj,
        judgements=_offer(COFFEE_ID), method="claude",
    )
    digest = render.render_digest(
        rows,
        target_date=TARGET,
        counts=db.counts_for_date(calendar_db, TARGET),
        ordering={str(COFFEE_ID): {"model_rank": 1}},
        curation_method="claude",
        settings=settings_obj,
        calendar=actions,
    )
    assert digest.calendar_callouts == 1
    assert digest.calendar_session_actions == 2
    assert digest.html.count('class="cal-button"') == 2
    assert "Add Thu, Sep 10" in digest.html
    assert "Add Mon, Sep 21" in digest.html
    assert "2 sessions — choose the one you will attend" in digest.html
    # One attachment per sitting, in reading order.
    assert [item.ics_filename for item in digest.calendar_attachments] == [
        "Provost-s-Coffee-Hours-Focus-on-Research-2026-09-10.ics",
        "Provost-s-Coffee-Hours-Focus-on-Research-2026-09-21.ics",
    ]


def test_the_plain_text_alternative_carries_every_session(calendar_db, settings_obj):
    """A reader on a text-only client must get the same choice."""
    from dailymail import render

    rows = _seed(calendar_db, coffee_row())
    actions, _ = _enrich(
        calendar_db, rows, settings_obj,
        judgements=_offer(COFFEE_ID), method="claude",
    )
    digest = render.render_digest(
        rows, target_date=TARGET,
        counts=db.counts_for_date(calendar_db, TARGET),
        ordering={str(COFFEE_ID): {"model_rank": 1}},
        curation_method="claude", settings=settings_obj, calendar=actions,
    )
    text = digest.text
    assert "Provost’s Coffee Hours - Focus on Research" in text
    assert "2 sessions — choose the one you will attend" in text
    assert "Thu, Sep 10 · 11:00 AM–12:30 PM" in text
    assert "Mon, Sep 21 · 2:30 PM–4:00 PM" in text
    assert text.count("outlook.office.com/calendar/deeplink/compose") == 2
    for name in (
        "Provost-s-Coffee-Hours-Focus-on-Research-2026-09-10.ics",
        "Provost-s-Coffee-Hours-Focus-on-Research-2026-09-21.ics",
    ):
        assert name in text


def test_the_mailer_attaches_one_ics_per_sitting(calendar_db, settings_obj):
    from dailymail import mailer, render

    rows = _seed(calendar_db, coffee_row())
    actions, _ = _enrich(
        calendar_db, rows, settings_obj,
        judgements=_offer(COFFEE_ID), method="claude",
    )
    digest = render.render_digest(
        rows, target_date=TARGET,
        counts=db.counts_for_date(calendar_db, TARGET),
        ordering={str(COFFEE_ID): {"model_rank": 1}},
        curation_method="claude", settings=settings_obj, calendar=actions,
    )
    prepared = mailer.build_message(
        settings=settings_obj, sender="digest@example.invalid",
        subject=digest.subject, html=digest.html, text=digest.text,
        images=[], calendar_attachments=digest.calendar_attachments,
    )
    assert prepared.calendar_count == 2
    assert prepared.calendar_filenames == (
        "Provost-s-Coffee-Hours-Focus-on-Research-2026-09-10.ics",
        "Provost-s-Coffee-Hours-Focus-on-Research-2026-09-21.ics",
    )
    # Still a newsletter, not a meeting request.
    types = [part.get_content_type() for part in prepared.message.walk()]
    assert types.count("application/octet-stream") == 2
    assert "text/calendar" not in types


def test_client_generated_date_links_are_not_required_for_the_controls(
    calendar_db, settings_obj
):
    """Outlook may auto-link the dates in the body; nothing depends on it.

    The DailyMail controls are anchors to an Outlook compose URL with their own
    background and their own label, built from stored state -- so they work
    identically in a client that never auto-links anything.
    """
    from dailymail import render

    rows = _seed(calendar_db, coffee_row())
    actions, _ = _enrich(
        calendar_db, rows, settings_obj,
        judgements=_offer(COFFEE_ID), method="claude",
    )
    digest = render.render_digest(
        rows, target_date=TARGET,
        counts=db.counts_for_date(calendar_db, TARGET),
        ordering={str(COFFEE_ID): {"model_rank": 1}},
        curation_method="claude", settings=settings_obj, calendar=actions,
    )
    # The source's own dated lines survive verbatim in the body...
    assert "Thursday, September 10 from 11:00-12:30" in digest.html
    # ...and are not themselves links: every anchor in the digest is one we made.
    import re

    for match in re.finditer(r'<a\b[^>]*href="([^"]+)"', digest.html):
        href = match.group(1)
        assert href.startswith(
            ("https://outlook.office.com/", "https://apps.rowan.edu/",
             "http://go.rowan.edu/", "https://sites.rowan.edu/",
             "https://confluence.rowan.edu/", "https://research.rowan.edu/")
        ), href
