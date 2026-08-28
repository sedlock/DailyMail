"""Logical-repeat detection: has the reader already been sent this?

Grounded in what Rowan Announcer submitters actually did in DailyMail's first
production week. Rather than extending an announcement's distribution dates,
they created a new SubmissionId carrying the identical body -- nine distinct
announcements, twelve reposts, all of which arrived labelled New.

The failure mode in the other direction is worse, so the bar is high: a monthly
`Faculty Senate Meeting` must stay New, and anything ambiguous stays New too.
"""

from __future__ import annotations

import pytest

from dailymail import db, repeats

TARGET = "2026-08-28"
YESTERDAY = "2026-08-27"

# The real 6686/6687 body, which Rowan published twice under two SubmissionIds
# two minutes apart, with only the distribution dates differing.
ACCESSIBILITY_TITLE = "Digital Accessibility at Rowan: What Faculty Need to Know"
ACCESSIBILITY_HTML = (
    "<h2>Digital Accessibility at Rowan: What Faculty Need to Know</h2>"
    "<p>Rowan University is committed to making its websites and course "
    "materials accessible to all users. In accordance with Title II of the "
    "Americans with Disabilities Act, the University requires its web content "
    "to meet WCAG 2.1 Level AA.</p>"
    '<p><a href="https://go.rowan.edu/accessibilityguide">Rowan Guide for '
    "Accessibility</a></p>"
)
ACCESSIBILITY_TEXT = (
    "Digital Accessibility at Rowan: What Faculty Need to Know\n"
    "Rowan University is committed to making its websites and course materials "
    "accessible to all users. In accordance with Title II of the Americans with "
    "Disabilities Act, the University requires its web content to meet WCAG 2.1 "
    "Level AA.\n"
    "Rowan Guide for Accessibility."
)


def announcement(submission_id, **overrides) -> dict:
    record = {
        "submission_id": submission_id,
        "title": ACCESSIBILITY_TITLE,
        "full_body": ACCESSIBILITY_HTML,
        "body_text": ACCESSIBILITY_TEXT,
        "category_id": 4,
        "source_audience": "Employees",
        "is_event": 0,
        "event_date": None,
        "event_start_time": None,
        "event_location": None,
        "contact_email": "facultycenter@rowan.edu",
        "submitted_by_email": "perry@rowan.edu",
        "status": "New",
        "source_status": "New",
        "last_delivered_date": YESTERDAY,
    }
    record.update(overrides)
    return record


# --- the reported case -------------------------------------------------------


def test_the_digital_accessibility_repost_is_a_logical_repeat():
    """6687 carries byte-identical content to 6686, delivered the day before."""
    match = repeats.compare(announcement(6687), announcement(6686))
    assert match is not None
    assert match.matched_submission_id == 6686
    assert match.method == repeats.METHOD_EXACT
    assert match.confidence >= 0.99
    assert match.body_similarity == 1.0
    assert match.display_status == "Standing"
    assert not match.materially_changed


def test_a_logical_repeat_records_its_evidence():
    match = repeats.compare(announcement(6687), announcement(6686))
    assert match.evidence["same_urls"] is True
    assert match.evidence["same_submitter"] is True
    assert match.evidence["occurrence"] == "both_non_event"
    assert match.evidence["prior_delivered"] == YESTERDAY


def test_find_repeats_only_considers_announcements_rowan_calls_new():
    current = [announcement(6687, source_status="Standing", status="Standing")]
    assert repeats.find_repeats(current, [announcement(6686)]) == {}


def test_find_repeats_picks_the_most_similar_prior():
    history = [
        announcement(6600, body_text=ACCESSIBILITY_TEXT.replace("WCAG 2.1", "WCAG 2.0")),
        announcement(6686),
    ]
    matches = repeats.find_repeats([announcement(6687)], history)
    assert matches["6687"].matched_submission_id == 6686


# --- the vetoes --------------------------------------------------------------


def test_a_different_title_is_never_a_repeat():
    """`Coming soon: X` and `Now showing: X` share a body but not a meaning."""
    prior = announcement(6637, title="Coming soon: Diane Burko's Extraction")
    current = announcement(6638, title="Now showing: Diane Burko's Extraction")
    assert repeats.compare(current, prior) is None


def test_a_different_category_is_never_a_repeat():
    assert repeats.compare(
        announcement(6687, category_id=25), announcement(6686, category_id=4)
    ) is None


def test_a_different_audience_is_never_a_repeat():
    assert repeats.compare(
        announcement(6687, source_audience="Students"), announcement(6686)
    ) is None


def test_a_recurring_event_with_a_new_date_stays_new():
    """The central protection: a monthly meeting is a new commitment each month."""
    body = "The Faculty Senate meets in Bunce Hall. All faculty are welcome."
    prior = announcement(
        7000, title="Faculty Senate Meeting", body_text=body, full_body=f"<p>{body}</p>",
        is_event=1, event_date="2026-09-15", event_start_time="15:00:00",
    )
    current = announcement(
        7001, title="Faculty Senate Meeting", body_text=body, full_body=f"<p>{body}</p>",
        is_event=1, event_date="2026-10-20", event_start_time="15:00:00",
    )
    assert repeats.compare(current, prior) is None


def test_the_same_event_occurrence_reposted_is_a_repeat():
    body = "The Faculty Senate meets in Bunce Hall. All faculty are welcome."
    common = dict(
        title="Faculty Senate Meeting", body_text=body, full_body=f"<p>{body}</p>",
        is_event=1, event_date="2026-09-15", event_start_time="15:00:00",
    )
    match = repeats.compare(
        announcement(7001, **common), announcement(7000, **common)
    )
    assert match is not None
    assert match.evidence["occurrence"] == "same_occurrence"


def test_the_same_event_at_a_different_time_stays_new():
    body = "The Faculty Senate meets in Bunce Hall."
    common = dict(
        title="Faculty Senate Meeting", body_text=body, full_body=f"<p>{body}</p>",
        is_event=1, event_date="2026-09-15",
    )
    assert repeats.compare(
        announcement(7001, event_start_time="18:00:00", **common),
        announcement(7000, event_start_time="15:00:00", **common),
    ) is None


def test_an_event_is_never_a_repeat_of_a_non_event():
    body = "Details about the programme."
    assert repeats.compare(
        announcement(7001, title="Programme", body_text=body, full_body=body,
                     is_event=1, event_date="2026-09-15"),
        announcement(7000, title="Programme", body_text=body, full_body=body,
                     is_event=0),
    ) is None


def test_a_similar_but_genuinely_different_announcement_stays_new():
    """A term swapped throughout is a different announcement, not a revision.

    The real pair (6609/6610) differed in title too. Given the *same* title, the
    tell is that the prior body's distinctive words are gone rather than merely
    added to -- which is what separates a substitution from a revision.
    """
    spss = announcement(
        6609, title="Software licence renewal update",
        body_text="The SPSS licence renews on 1 September. Contact IT to request a "
                  "seat. Existing installations continue to work until then.",
        full_body="<p>The SPSS licence renews on 1 September.</p>",
    )
    mathematica = announcement(
        6610, title="Software licence renewal update",
        body_text="The Mathematica licence renews on 1 October. Contact IT to "
                  "request a seat. Existing installs continue to work until then.",
        full_body="<p>The Mathematica licence renews on 1 October.</p>",
    )
    assert repeats.compare(mathematica, spss) is None


def test_an_ambiguous_similarity_stays_new():
    prior = announcement(6686)
    current = announcement(
        6687,
        body_text=ACCESSIBILITY_TEXT + (
            "\nA new mandatory training module is now available and must be "
            "completed before the end of the semester. Deans will receive "
            "completion reports each month."
        ),
    )
    assert repeats.compare(current, prior) is None


def test_an_announcement_is_never_a_repeat_of_itself():
    assert repeats.compare(announcement(6686), announcement(6686)) is None


# --- materially changed reposts ----------------------------------------------


def test_a_substantively_revised_repost_is_standing_and_updated():
    """The same announcement, genuinely revised: Standing, but flagged UPDATED."""
    prior = announcement(6686)
    current = announcement(
        6687,
        body_text=ACCESSIBILITY_TEXT
        + "\nThe Faculty Center will run monthly accessibility clinics.",
    )
    match = repeats.compare(current, prior)
    assert match is not None
    assert match.method == repeats.METHOD_CHANGED
    assert match.materially_changed
    assert match.display_status == "Standing"
    assert 0.85 <= match.body_similarity < 0.97
    assert match.evidence["distinctive_words_removed"] == []


def test_a_trivially_reworded_repost_is_a_plain_repeat_not_an_update():
    prior = announcement(6686)
    current = announcement(
        6687,
        body_text=ACCESSIBILITY_TEXT.replace("WCAG 2.1 Level AA",
                                             "WCAG 2.1 Level AA by April 2027"),
    )
    match = repeats.compare(current, prior)
    assert match is not None
    assert match.method == repeats.METHOD_NEAR
    assert not match.materially_changed


def test_a_near_identical_repost_from_the_same_submitter_is_a_repeat():
    prior = announcement(6686)
    current = announcement(
        6687, body_text=ACCESSIBILITY_TEXT.replace("committed", "commited")
    )
    match = repeats.compare(current, prior)
    assert match is not None
    assert match.method in (repeats.METHOD_NEAR, repeats.METHOD_EXACT)
    assert not match.materially_changed


# --- normalization -----------------------------------------------------------


def test_titles_normalize_past_emoji_and_decorative_punctuation():
    assert repeats.normalize_title(
        '🌎 "Free Travel, Real Diplomacy: Apply for Spring 2027 Model UN/AU"'
    ) == repeats.normalize_title(
        "Free Travel, Real Diplomacy: Apply for Spring 2027 Model UN/AU"
    )


def test_titles_normalize_past_curly_quotes_and_case():
    assert repeats.normalize_title("Provost’s Town Hall") == repeats.normalize_title(
        "PROVOST'S TOWN HALL"
    )


def test_body_similarity_is_one_for_identical_text():
    assert repeats.body_similarity("a b c", "a b c") == 1.0


def test_body_similarity_is_zero_for_empty_text():
    assert repeats.body_similarity("", "anything") == 0.0


def test_very_different_lengths_short_circuit_cheaply():
    assert repeats.body_similarity("short", "x" * 10_000) < 0.1


def test_a_family_key_groups_the_reposts_of_one_announcement():
    a = repeats.family_key(ACCESSIBILITY_TITLE, 4, "Employees")
    b = repeats.family_key(ACCESSIBILITY_TITLE.upper(), 4, "Employees")
    assert a == b
    assert repeats.family_key(ACCESSIBILITY_TITLE, 25, "Employees") != a


# --- persistence -------------------------------------------------------------


def test_a_repeat_match_is_stored_and_reusable(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    try:
        with db.transaction(connection):
            for submission_id in (6686, 6687):
                connection.execute(
                    "INSERT INTO announcements (submission_id, source_audience, "
                    "first_observed_at, last_observed_at, official_url) "
                    "VALUES (?, 'Employees', ?, ?, ?)",
                    (submission_id, db.now_utc(), db.now_utc(),
                     db.official_url(submission_id)),
                )
        match = repeats.compare(announcement(6687), announcement(6686))
        with db.transaction(connection):
            db.record_repeat_match(connection, match.as_record())
        stored = db.repeat_match(connection, 6687)
        assert stored["matched_submission_id"] == 6686
        assert stored["method"] == repeats.METHOD_EXACT
        assert stored["family_key"] == match.family_key
        first_detected = stored["first_detected_at"]

        # Re-recording keeps the original detection time.
        with db.transaction(connection):
            db.record_repeat_match(connection, match.as_record())
        assert db.repeat_match(connection, 6687)["first_detected_at"] == first_detected
    finally:
        connection.close()
