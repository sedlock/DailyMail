"""What the deterministic relevance scorer reads, and what it refuses to read.

Two results from the 9 September 2026 digest, both from the fallback path
because curation had timed out that morning, are the whole reason this file
exists.

`6815 Hollybush Tour` was OFFERED at 0.85, and the stored reason says why:

    deterministic relevance 0.85 (president, category:Glassboro Campus)

The word `president` is in this sentence of the body:

    the University's history through the legacy of its presidents, and the 1967
    summit between President Lyndon B. Johnson and Soviet Premier Alexei Kosygin

An ordinary building tour was promoted to a senior-leadership event by two
sentences about 1967. The tour is in fact worth offering -- but not for that.

`6783 You're Invited to the Wellness Center Open House!` was WITHHELD at 0.20:

    deterministic relevance 0.20 (emergency, -free food)

Both of those phrases are incidental. `free food` is in a list of what is on
offer; `emergency` is the E of `Emergency Medical Services` in a list of the
departments attending. A broad student-service open house with a date, a time
and a room lost its button to two phrases, neither of which describes the event.

So the scorer now reads the event's title, its category, its audience and its own
opening statement of purpose at full weight, and everything deeper in the body at
a quarter weight with a hard clamp. The signals that say *who* an event belongs
to are focus-only and are never read out of body prose at all.

The delivery mechanism is untouched and asserted to be untouched: this file
changes which events are offered a control, never how that control works.
"""

from __future__ import annotations

import sqlite3
from datetime import date, time

import pytest

from conftest import HOLLYBUSH_BODY, HOLLYBUSH_TEXT, WELLNESS_BODY, WELLNESS_TEXT
from dailymail import calendar_enrich, events, settings as settings_module

THRESHOLD = 0.6  # the shipped default; asserted against settings below.


def row(**fields) -> dict:
    base = {
        "submission_id": 9000,
        "title": "",
        "body_text": "",
        "source_audience": "Both",
        "category_title": None,
    }
    base.update(fields)
    return base


def candidate(title, *, location=None, heading_hint=None, audience=None):
    return events.EventCandidate(
        submission_id="9000",
        title=title,
        event_date=date(2026, 9, 30),
        start=time(15, 30),
        end=time(16, 30),
        location=location,
        heading_hint=heading_hint,
        audience=audience,
    )


def score(fields, cand):
    return calendar_enrich.deterministic_relevance(row(**fields), cand)


def offered(fields, cand) -> bool:
    value, _reason = score(fields, cand)
    return value >= THRESHOLD


# --- the shipped threshold ---------------------------------------------------


def test_the_threshold_this_file_reasons_about_is_the_shipped_one(settings_obj):
    assert settings_obj.calendar_relevance_threshold == THRESHOLD


# --- the two reported cases --------------------------------------------------

WELLNESS = row(
    submission_id=6783,
    title="You're Invited to the Wellness Center Open House!",
    body_text=WELLNESS_TEXT,
    source_audience="Students",
    category_title="Well-being and Health",
)
WELLNESS_CANDIDATE = candidate(
    "Wellness Center Open House", location="Wellness Center"
)

HOLLYBUSH = row(
    submission_id=6815,
    title="Hollybush Tour",
    body_text=HOLLYBUSH_TEXT,
    source_audience="Both",
    category_title="Glassboro Campus",
)
HOLLYBUSH_CANDIDATE = candidate("Hollybush Tour", location="Hollybush building")


def test_the_wellness_open_house_is_now_offered():
    value, reason = calendar_enrich.deterministic_relevance(
        WELLNESS, WELLNESS_CANDIDATE
    )
    assert value >= THRESHOLD, reason
    assert "open house" in reason


def test_free_food_no_longer_decides_the_wellness_case():
    """`free food` is in this announcement's own opening, so it is real evidence
    and still costs the event something. What it may not do is decide: it is
    counted at the reduced weight its position earns, and the open house is
    offered either way."""
    assert "free food" in WELLNESS_TEXT.lower()
    with_food, reason = calendar_enrich.deterministic_relevance(
        WELLNESS, WELLNESS_CANDIDATE
    )
    without = calendar_enrich.deterministic_relevance(
        row(
            **{
                **WELLNESS,
                "body_text": WELLNESS_TEXT.lower().replace("free food", "snacks"),
            }
        ),
        WELLNESS_CANDIDATE,
    )[0]
    # The reason names where the evidence came from, not just what it was.
    assert "-purpose:free food" in reason
    assert with_food >= THRESHOLD and without >= THRESHOLD
    assert without - with_food <= 0.25 * calendar_enrich.PURPOSE_INFLUENCE_WEIGHT + 1e-9


def test_free_food_in_a_subject_line_is_the_event_and_still_counts_fully():
    """The other half of the same rule. 6927 shouted it; 6783 listed it."""
    shouted = row(
        title="Join SUP for their General Body Meeting - TONIGHT! FREE FOOD!",
        body_text="Come along and get involved.",
        source_audience="Students",
        category_title="Campus Activities",
    )
    _value, reason = calendar_enrich.deterministic_relevance(
        shouted, candidate("SUPdate General Body Meeting")
    )
    assert "-free food" in reason and "-purpose:free food" not in reason
    assert not offered(shouted, candidate("SUPdate General Body Meeting"))


def test_the_hollybush_tour_is_still_offered():
    value, reason = calendar_enrich.deterministic_relevance(
        HOLLYBUSH, HOLLYBUSH_CANDIDATE
    )
    assert value >= THRESHOLD, reason


def test_the_hollybush_tour_is_not_offered_because_of_the_word_president():
    """The exact defect: the reason must not name it, and removing the prose
    that contains it must not change the decision."""
    assert "president" in HOLLYBUSH_TEXT.lower(), "the fixture must carry the prose"
    value, reason = calendar_enrich.deterministic_relevance(
        HOLLYBUSH, HOLLYBUSH_CANDIDATE
    )
    assert "president" not in reason.lower()

    scrubbed = HOLLYBUSH_TEXT
    for word in ("presidents", "President", "president"):
        scrubbed = scrubbed.replace(word, "figures")
    without, _ = calendar_enrich.deterministic_relevance(
        row(**{**HOLLYBUSH, "body_text": scrubbed}), HOLLYBUSH_CANDIDATE
    )
    assert without >= THRESHOLD
    assert abs(value - without) <= calendar_enrich.BODY_INFLUENCE_CAP


def test_incidental_leadership_prose_cannot_make_an_event_relevant_on_its_own():
    """The general property, stated directly: an ordinary club talk stays
    ordinary however many presidents its body happens to name."""
    plain = row(
        title='Student-Led Discussion of "Comparing Periods of Starvation"',
        body_text="A student discussion hosted by the association.",
        source_audience="Both",
        category_title="Social and Cultural Events",
    )
    stuffed = row(
        **{
            **plain,
            "body_text": (
                "A student discussion hosted by the association, led by our "
                "association President Anna Cherian, about President Lyndon B. "
                "Johnson, the university's presidents, the provost and the "
                "board of trustees and the chancellor and the cabinet."
            ),
        }
    )
    cand = candidate("Student-Led Discussion", location="CSC 127")
    assert not offered(plain, cand)
    assert not offered(stuffed, cand)
    plain_score = calendar_enrich.deterministic_relevance(plain, cand)[0]
    stuffed_score = calendar_enrich.deterministic_relevance(stuffed, cand)[0]
    assert abs(plain_score - stuffed_score) <= calendar_enrich.BODY_INFLUENCE_CAP


def test_a_leadership_title_still_counts_at_full_weight():
    """Focus-only does not mean ignored: in the title it is exactly the signal
    it was always meant to be."""
    town_hall = row(
        title="Provost's Town Hall - Oct 14",
        body_text="We welcome all faculty, staff, and managers.",
        source_audience="Employees",
        category_title="Glassboro Campus",
    )
    value, reason = calendar_enrich.deterministic_relevance(
        town_hall,
        candidate(
            "Provost's Town Hall",
            location="Chamberlain Student Center, Eynon Ballroom",
            heading_hint="Provost's Town Hall & Social",
        ),
    )
    assert value >= THRESHOLD
    assert "town hall" in reason and "provost" in reason


def test_the_provost_coffee_hours_series_is_still_offered():
    coffee = row(
        title="Provost's Coffee Hours (Focus on Research)",
        body_text=(
            "Provost's Coffee Hours are dedicated times to engage in "
            "small-group conversations with the Provost."
        ),
        source_audience="Employees",
        category_title="Glassboro Campus",
    )
    assert offered(coffee, candidate("Provost's Coffee Hours (Focus on Research)"))


# --- negative controls -------------------------------------------------------


@pytest.mark.parametrize(
    "title, body, audience, category",
    [
        (
            "Late Night @ The Rec",
            "Join us at the Student Recreation Center for games and music.",
            "Students",
            "Campus Activities",
        ),
        (
            "Join SUP for their General Body Meeting - TONIGHT! FREE FOOD!",
            "Join us at our general body SUPdate meeting to learn how to get "
            "involved and gain leadership skills.",
            "Students",
            "Campus Activities",
        ),
        (
            "Tickets STILL Available for Cape May Beach Day - SUP Off-Campus Trip",
            "Soak up the summer sun. Trip includes transportation.",
            "Students",
            "Campus Activities",
        ),
        (
            "Welcome Week: Shop & Style with RAH & SUP",
            "Free Walmart shopping trip for new students.",
            "Students",
            "Campus Activities",
        ),
        (
            '"38 Londres Street" by Philippe Sands, RCHGHR/CBSE Book Club',
            "CBSE Reads / RCHGHR Book Club, exploring international law.",
            "Both",
            "Social and Cultural Events",
        ),
        (
            "Free Resume Review!",
            "Stop by for help polishing your resume. Every Wednesday.",
            "Both",
            "Academic and Career Success",
        ),
    ],
)
def test_routine_student_activity_is_withheld(title, body, audience, category):
    fields = row(
        title=title, body_text=body, source_audience=audience,
        category_title=category,
    )
    value, reason = calendar_enrich.deterministic_relevance(
        fields, candidate(title)
    )
    assert value < THRESHOLD, f"{title}: {reason}"


def test_the_global_threshold_was_not_simply_lowered():
    """The base sits well below the threshold, so an event still has to earn
    its button rather than merely fail to disqualify itself."""
    assert calendar_enrich.BASE_SCORE < THRESHOLD
    bare = row(title="An Event", body_text="It is happening.", source_audience="Both")
    assert not offered(bare, candidate("An Event"))


# --- the property the whole refactor rests on --------------------------------


def test_body_evidence_is_clamped_in_both_directions():
    base = row(
        title="Campus Update", body_text="", source_audience="Both",
        category_title="Glassboro Campus",
    )
    cand = candidate("Campus Update")
    neutral = calendar_enrich.deterministic_relevance(base, cand)[0]

    positive = calendar_enrich.deterministic_relevance(
        row(
            **{
                **base,
                "body_text": " ".join(
                    ["padding"] * 80
                    + [
                        "cybersecurity", "commencement", "open enrollment",
                        "accreditation", "strategic plan", "information technology",
                    ]
                ),
            }
        ),
        cand,
    )[0]
    negative = calendar_enrich.deterministic_relevance(
        row(
            **{
                **base,
                "body_text": " ".join(
                    ["padding"] * 80
                    + ["trivia", "karaoke", "bingo", "free food", "giveaway"]
                ),
            }
        ),
        cand,
    )[0]
    cap = calendar_enrich.BODY_INFLUENCE_CAP
    assert positive - neutral <= cap + 1e-9
    assert neutral - negative <= cap + 1e-9


def test_the_opening_of_a_body_is_read_as_its_statement_of_purpose():
    """An announcement that says what it is in its first sentence is heard."""
    lead = row(
        title="An Invitation",
        body_text=(
            "The university invites the entire Rowan University community to a "
            "campus wide open house about information technology."
        ),
        source_audience="Both",
    )
    buried = row(
        **{
            **lead,
            "body_text": " ".join(["filler"] * 120)
            + " campus wide open house about information technology.",
        }
    )
    assert offered(lead, candidate("An Invitation"))
    lead_score = calendar_enrich.deterministic_relevance(
        lead, candidate("An Invitation")
    )[0]
    buried_score = calendar_enrich.deterministic_relevance(
        buried, candidate("An Invitation")
    )[0]
    assert buried_score < lead_score


def test_the_reason_says_which_evidence_came_from_the_body():
    _value, reason = calendar_enrich.deterministic_relevance(
        WELLNESS, WELLNESS_CANDIDATE
    )
    assert "body:" in reason, reason


def test_matching_is_insensitive_to_punctuation_and_spacing():
    for title in ("Drop-In Hours", "Drop- In Hours", "DROP IN HOURS"):
        fields = row(
            title=title, body_text="", source_audience="Both",
            category_title="Academic and Career Success",
        )
        _value, reason = calendar_enrich.deterministic_relevance(
            fields, candidate(title)
        )
        assert "-drop in" in reason, (title, reason)


def test_a_student_audience_is_no_longer_penalised_for_being_student_facing():
    """A parent-relevant student service is exactly what this reader wants."""
    assert calendar_enrich._AUDIENCE_WEIGHTS["Students"] >= 0.0


def test_the_score_is_always_a_probability():
    for fields, cand in (
        (WELLNESS, WELLNESS_CANDIDATE),
        (HOLLYBUSH, HOLLYBUSH_CANDIDATE),
        (row(title="x", body_text="y"), candidate("x")),
    ):
        value, _ = calendar_enrich.deterministic_relevance(fields, cand)
        assert 0.0 <= value <= 1.0


def test_a_row_missing_optional_columns_still_scores():
    """`digest_rows` is one row shape; a recommendation replay is another."""
    bare = {"title": "Provost's Town Hall", "submission_id": 1}
    value, reason = calendar_enrich.deterministic_relevance(
        bare, candidate("Provost's Town Hall")
    )
    assert 0.0 <= value <= 1.0
    assert "town hall" in reason


def test_a_sqlite_row_works_as_well_as_a_mapping(settings_obj):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    got = connection.execute(
        "SELECT 'Provost''s Town Hall' AS title, '' AS body_text, "
        "'Employees' AS source_audience, 'Glassboro Campus' AS category_title"
    ).fetchone()
    value, reason = calendar_enrich.deterministic_relevance(
        got, candidate("Provost's Town Hall")
    )
    assert value >= THRESHOLD, reason


# --- the AI path, and the promise that it costs no extra call ----------------


def test_calendar_relevance_still_rides_on_the_single_curation_call(
    monkeypatch, settings_obj
):
    """One model invocation per digest, carrying both jobs.

    The whole reason relevance is answered inside the ranking call is that a
    second 60-90 second invocation would be a poor trade for a button. A
    regression that split them would be invisible to every other test here.
    """
    from datetime import date as _date

    from dailymail import curate, db

    connection = db.connect()
    db.initialize(connection)
    calls: list[dict] = []

    def counted(payload, settings):
        calls.append(payload)
        return (
            {
                "rankings": [
                    {
                        "submission_id": str(entry["submission_id"]),
                        "section": entry["section"],
                        "rank": index + 1,
                        "relevance": 80,
                        "urgency": 40,
                        "calendar": {
                            "offer": True,
                            "confidence": 0.9,
                            "reason": "senior leadership event",
                        },
                    }
                    for index, entry in enumerate(
                        payload["new_items"] + payload["standing_items"]
                    )
                ]
            },
            0.01,
            "claude-sonnet-5",
        )

    monkeypatch.setattr(curate, "_invoke_claude", counted)
    try:
        stamp = db.now_utc()
        with db.transaction(connection):
            db.upsert_category(
                connection, category_id=12, title="Glassboro Campus",
                rowan_rank=1, color=None, is_active=True, manual_priority=1,
            )
            record = {
                "submission_id": 9101,
                "title": "Provost's Town Hall - Oct 14",
                "full_body": "<p>Wednesday, October 14, 2026 10:00 - 11:15 in "
                             "Chamberlain Student Center, Eynon Ballroom.</p>",
                "body_text": "Wednesday, October 14, 2026 10:00 - 11:15 in "
                             "Chamberlain Student Center, Eynon Ballroom.",
                "source_audience": "Employees",
                "category_id": 12,
                "distribution_dates": ["2026-09-01"],
                "first_distribution_date": "2026-09-01",
                "status": "New",
                "is_event": 0,
            }
            version_id, _ = db.record_announcement(
                connection, record, observed_at=stamp
            )
            db.record_daily(
                connection, target_date="2026-09-01", submission_id=9101,
                version_id=version_id, status="New", changed=False, observed_at=stamp,
            )
        rows = db.digest_rows(connection, "2026-09-01")

        candidates, diagnostics = calendar_enrich.detect_candidates(
            rows, target_date="2026-09-01", settings=settings_obj
        )
        outcome = curate.curate(
            rows, "2026-09-01", settings_obj,
            event_candidates=calendar_enrich.primary_candidates(candidates),
        )
        actions, _metrics = calendar_enrich.enrich_digest(
            connection, rows, target_date="2026-09-01", settings=settings_obj,
            candidates=candidates, diagnostics=diagnostics,
            curation_entries=outcome.entries, curation_method=outcome.method,
            allow_routing=False, router=lambda *a, **k: None,
        )
        assert len(calls) == 1, f"{len(calls)} model invocations, expected 1"
        assert outcome.method == "claude"
        # The event block travelled on that one payload.
        assert "event_candidate" in calls[0]["new_items"][0]
        # ...and the model's judgement is what decided the button.
        if actions:
            recorded = connection.execute(
                "SELECT relevance_method FROM calendar_recommendations "
                "WHERE submission_id = 9101"
            ).fetchone()
            assert recorded["relevance_method"] == "claude"
    finally:
        connection.close()


def test_the_curation_prompt_states_the_corrected_philosophy():
    """The two paths must not drift apart: what the fallback now reasons from
    is what the model is told to reason from."""
    from dailymail import curate

    prompt = curate.SYSTEM_PROMPT
    assert "broad student-service events" in prompt
    assert "Wording buried" in prompt
    assert "historical mention of a president" in prompt


def test_the_model_still_cannot_supply_calendar_data():
    """Unchanged, and worth restating beside a change to relevance."""
    from dailymail import curate

    calendar = curate.OUTPUT_SCHEMA["properties"]["rankings"]["items"][
        "properties"
    ]["calendar"]
    assert calendar["additionalProperties"] is False
    assert set(calendar["properties"]) == {
        "offer", "confidence", "reason", "attendance_mode", "suggested_title",
    }
    for forbidden in ("date", "start_time", "location", "url", "ics"):
        assert forbidden not in calendar["properties"]


# --- a preview must not rewrite history --------------------------------------


def test_a_preview_render_does_not_rewrite_the_stored_decisions(settings_obj):
    """`calendar_recommendations` is the audit trail of what a given morning
    decided. Re-rendering that date later, under changed code, must leave it
    alone -- otherwise a preview silently rewrites history, which is exactly
    what happened while validating this change against 9 September 2026.
    """
    from dailymail import db

    connection = db.connect()
    db.initialize(connection)
    try:
        stamp = db.now_utc()
        with db.transaction(connection):
            db.upsert_category(
                connection, category_id=12, title="Glassboro Campus",
                rowan_rank=1, color=None, is_active=True, manual_priority=1,
            )
            record = {
                "submission_id": 9201,
                "title": "Hollybush Tour",
                "full_body": f"<p>{HOLLYBUSH_BODY}</p>",
                "body_text": HOLLYBUSH_TEXT,
                "source_audience": "Both",
                "category_id": 12,
                "distribution_dates": ["2026-09-09"],
                "first_distribution_date": "2026-09-09",
                "status": "New",
                "is_event": 1,
                "event_name": "Hollybush Tour",
                "event_date": "2026-09-11",
                "event_start_time": "10:00:00",
                "event_end_time": "14:00:00",
                "event_location": "Hollybush building",
            }
            version_id, _ = db.record_announcement(
                connection, record, observed_at=stamp
            )
            db.record_daily(
                connection, target_date="2026-09-09", submission_id=9201,
                version_id=version_id, status="New", changed=False,
                observed_at=stamp,
            )
        rows = db.digest_rows(connection, "2026-09-09")
        candidates, diagnostics = calendar_enrich.detect_candidates(
            rows, target_date="2026-09-09", settings=settings_obj
        )

        def run(persist):
            return calendar_enrich.enrich_digest(
                connection, rows, target_date="2026-09-09", settings=settings_obj,
                candidates=candidates, diagnostics=diagnostics,
                curation_entries=None, curation_method="fallback",
                allow_routing=False, router=lambda *a, **k: None,
                persist=persist,
            )

        # The morning run writes its decision.
        run(True)
        before = connection.execute(
            "SELECT * FROM calendar_recommendations WHERE target_date = '2026-09-09'"
        ).fetchall()
        assert before, "the run must have recorded something"

        # A later preview computes the same actions and writes nothing.
        actions, _metrics = run(False)
        after = connection.execute(
            "SELECT * FROM calendar_recommendations WHERE target_date = '2026-09-09'"
        ).fetchall()
        assert [tuple(r) for r in after] == [tuple(r) for r in before]
        # ...and the preview still produced a usable action.
        assert actions, "a preview must still render the button it would show"
    finally:
        connection.close()
