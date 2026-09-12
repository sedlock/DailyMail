"""Render-time cleanup: the duplicate title and the redundant category label.

Both are presentation defects, so both are fixed in the rendering derivative and
nowhere else. The stored source in SQLite is authoritative and is asserted
byte-for-byte unchanged.

The suppression rule is deliberately narrow. Showing a title twice is a mild
irritation; silently deleting a sentence, an image or a unique link would be a
real defect, so anything short of an exact match is left alone.
"""

from __future__ import annotations

import pytest

from dailymail import db, render, sanitize
from dailymail.images import ImageProcessor

TARGET = "2026-08-28"

DUPLICATE_TITLE = "Digital Accessibility at Rowan: What Faculty Need to Know"
DUPLICATE_BODY = (
    f"<h2>{DUPLICATE_TITLE}</h2>"
    "<p>Rowan University is committed to making its websites and course "
    "materials accessible to all users.</p>"
)


# --- the suppression rule ----------------------------------------------------


def test_an_exact_duplicate_heading_is_suppressed():
    html, removed = sanitize.suppress_duplicate_heading(DUPLICATE_BODY, DUPLICATE_TITLE)
    assert removed
    assert DUPLICATE_TITLE not in html
    assert "Rowan University is committed" in html


@pytest.mark.parametrize(
    "heading",
    [
        f"<h2>  {DUPLICATE_TITLE}  </h2>",                    # whitespace
        f"<h2>{DUPLICATE_TITLE.upper()}</h2>",                # case
        f"<h2>{DUPLICATE_TITLE}.</h2>",                       # trailing stop
        "<h2>Digital&nbsp;Accessibility at Rowan: What Faculty Need to Know</h2>",
        "<h2>Digital Accessibility at Rowan: What Faculty Need to&#32;Know</h2>",
        f"<h4><span style='color:red'><strong>{DUPLICATE_TITLE}</strong></span></h4>",
        f"<p><strong>{DUPLICATE_TITLE}</strong></p>",
        f"<div>{DUPLICATE_TITLE}</div>",
    ],
)
def test_trivial_differences_do_not_defeat_suppression(heading):
    html, removed = sanitize.suppress_duplicate_heading(
        heading + "<p>Body text.</p>", DUPLICATE_TITLE
    )
    assert removed, heading
    assert "Body text." in html


def test_curly_and_straight_quotes_are_treated_as_the_same():
    html, removed = sanitize.suppress_duplicate_heading(
        "<h4>Provost’s Town Hall</h4><p>Body.</p>", "Provost's Town Hall"
    )
    assert removed


def test_a_first_paragraph_with_the_title_plus_substance_is_kept():
    body = (
        f"<p>{DUPLICATE_TITLE} is a new requirement that takes effect in "
        "April 2027 and applies to all course materials.</p>"
    )
    html, removed = sanitize.suppress_duplicate_heading(body, DUPLICATE_TITLE)
    assert not removed
    assert html == body


def test_a_linked_heading_is_kept_because_the_link_is_unique_information():
    body = f'<h2><a href="https://go.rowan.edu/x">{DUPLICATE_TITLE}</a></h2><p>b</p>'
    html, removed = sanitize.suppress_duplicate_heading(body, DUPLICATE_TITLE)
    assert not removed
    assert "go.rowan.edu/x" in html


def test_a_heading_containing_an_image_is_kept():
    body = f'<h2><img src="cid:x" alt="{DUPLICATE_TITLE}">{DUPLICATE_TITLE}</h2><p>b</p>'
    _, removed = sanitize.suppress_duplicate_heading(body, DUPLICATE_TITLE)
    assert not removed


def test_a_leading_image_is_never_removed():
    body = f'<p><img src="cid:x"></p><h2>{DUPLICATE_TITLE}</h2><p>b</p>'
    html, removed = sanitize.suppress_duplicate_heading(body, DUPLICATE_TITLE)
    assert not removed
    assert "cid:x" in html


def test_a_different_heading_is_kept():
    """The Provost's Town Hall body heading is not its Announcer subject."""
    body = "<h4>Provost’s Town Hall &amp; Social</h4><p>We welcome all...</p>"
    html, removed = sanitize.suppress_duplicate_heading(
        body, "Provost's Town Hall - Oct 14"
    )
    assert not removed
    assert "Social" in html


def test_a_subtitle_after_the_title_survives():
    body = f"<h2>{DUPLICATE_TITLE}</h2><h3>A guide for course designers</h3><p>b</p>"
    html, removed = sanitize.suppress_duplicate_heading(body, DUPLICATE_TITLE)
    assert removed
    assert "A guide for course designers" in html


def test_only_the_first_occurrence_is_considered():
    body = f"<p>Intro.</p><h2>{DUPLICATE_TITLE}</h2><p>b</p>"
    html, removed = sanitize.suppress_duplicate_heading(body, DUPLICATE_TITLE)
    assert not removed
    assert DUPLICATE_TITLE in html


def test_an_empty_or_malformed_body_is_returned_unchanged():
    assert sanitize.suppress_duplicate_heading("", "x") == ("", False)
    assert sanitize.suppress_duplicate_heading("<p>x", "") == ("<p>x", False)


# --- the plain-text alternative ----------------------------------------------


def test_the_plain_text_duplicate_title_is_removed_too():
    text = f"{DUPLICATE_TITLE}\nRowan University is committed to accessibility."
    out, removed = sanitize.suppress_duplicate_text_heading(text, DUPLICATE_TITLE)
    assert removed
    assert out.startswith("Rowan University is committed")


def test_the_plain_text_first_line_with_substance_is_kept():
    text = f"{DUPLICATE_TITLE} takes effect in April 2027.\nMore detail."
    out, removed = sanitize.suppress_duplicate_text_heading(text, DUPLICATE_TITLE)
    assert not removed
    assert out == text


# --- rendering ---------------------------------------------------------------


def _seed(connection, *records):
    with db.transaction(connection):
        for category_id, title in ((4, "Faculty"), (2, "Technology")):
            db.upsert_category(
                connection, category_id=category_id, title=title, rowan_rank=1,
                color=None, is_active=True, manual_priority=category_id,
            )
        for record in records:
            version_id, _ = db.record_announcement(
                connection, record, observed_at=db.now_utc()
            )
            db.record_daily(
                connection, target_date=TARGET,
                submission_id=record["submission_id"], version_id=version_id,
                status=record["status"], changed=False, observed_at=db.now_utc(),
            )
            if record.get("display_status"):
                db.set_display_status(
                    connection, target_date=TARGET,
                    submission_id=record["submission_id"],
                    display_status=record["display_status"],
                )
    return db.digest_rows(connection, TARGET)


def _record(submission_id, **overrides):
    record = {
        "submission_id": submission_id,
        "title": DUPLICATE_TITLE,
        "full_body": DUPLICATE_BODY,
        "body_text": f"{DUPLICATE_TITLE}\nRowan University is committed to "
                     "making its websites accessible.",
        "source_audience": "Employees",
        "category_id": 4,
        "distribution_dates": [TARGET],
        "first_distribution_date": TARGET,
        "status": "New",
        "is_event": 0,
    }
    record.update(overrides)
    return record


@pytest.fixture
def rendered(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    rows = _seed(
        connection,
        _record(8001),
        _record(8002, title="Standing Announcement",
                full_body="<p>Standing body.</p>", body_text="Standing body.",
                category_id=2, status="Standing"),
    )
    counts = db.counts_for_date(connection, TARGET)
    ordering = {"8001": {"model_rank": 1}, "8002": {"model_rank": 1}}
    digest = render.render_digest(
        rows, target_date=TARGET, counts=counts, ordering=ordering,
        curation_method="fallback", settings=settings_obj,
    )
    yield connection, digest
    connection.close()


def _body_copy(html: str) -> str:
    """Just the announcement body region, excluding headline and preheader."""
    import re

    blocks = re.findall(
        r'<div class="body-copy"[^>]*>(.*?)</div>', html, re.DOTALL
    )
    assert blocks, "no announcement body was rendered"
    return "\n".join(blocks)


def test_the_rendered_card_does_not_repeat_its_own_title(rendered):
    _, digest = rendered
    assert digest.duplicate_titles_suppressed >= 1
    # The headline shows it; the body must not show it again. The hidden
    # preheader legitimately lists New subjects and is not part of the card.
    assert DUPLICATE_TITLE not in _body_copy(digest.html)
    assert digest.html.count(DUPLICATE_TITLE) == 2  # headline + preheader only


def test_the_plain_text_card_does_not_repeat_its_own_title(rendered):
    _, digest = rendered
    assert digest.text.count(DUPLICATE_TITLE) == 1


def test_the_full_announcement_body_still_renders(rendered):
    _, digest = rendered
    assert "Rowan University is committed" in digest.html
    assert "Rowan University is committed" in digest.text


def test_the_stored_source_body_is_unchanged(rendered):
    connection, _ = rendered
    stored = connection.execute(
        "SELECT full_body, body_text, title FROM announcement_versions "
        "WHERE submission_id = 8001"
    ).fetchone()
    assert stored["full_body"] == DUPLICATE_BODY
    assert stored["body_text"].startswith(DUPLICATE_TITLE)
    assert stored["title"] == DUPLICATE_TITLE


def test_a_new_card_inside_a_category_group_does_not_repeat_the_category(rendered):
    _, digest = rendered
    # The group heading states it once, in upper case.
    assert "FACULTY" in digest.html
    assert digest.html.count(">\n        Faculty\n      <") == 0
    assert "] Faculty" not in digest.text  # the plain-text New card


def test_a_standing_card_keeps_its_category(rendered):
    _, digest = rendered
    assert "[STANDING | EMPLOYEE] Technology" in digest.text
    assert "Technology" in digest.html


def test_every_announcement_still_appears_exactly_once(rendered):
    _, digest = rendered
    assert sorted(digest.submission_ids) == ["8001", "8002"]
    for submission_id in ("8001", "8002"):
        url = db.official_url(submission_id)
        assert digest.html.count(url) >= 1
        assert digest.text.count(url) == 1


def test_the_category_is_still_in_the_data_model(rendered):
    """Context-aware rendering, not data deletion."""
    connection, _ = rendered
    rows = db.digest_rows(connection, TARGET)
    assert {row["category_title"] for row in rows} == {"Faculty", "Technology"}

    processor = ImageProcessor.__new__(ImageProcessor)
    items, *_ = render.build_items(
        rows, {}, __import__("dailymail.settings", fromlist=["load"]).load(),
        _NullProcessor(),
    )
    assert all(item["category_title"] for item in items.values())
    assert all(item["show_category"] for item in items.values())


class _NullProcessor:
    """Stands in for the image processor: rendering cleanup never touches images."""

    def resolver_for(self, submission_id, official_url):
        return lambda *args, **kwargs: None


def test_a_repeat_shown_as_standing_keeps_its_category(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    try:
        rows = _seed(
            connection,
            _record(8003, status="New", display_status="Standing"),
        )
        assert rows[0]["source_status"] == "New"
        assert rows[0]["status"] == "Standing"
        counts = db.counts_for_date(connection, TARGET)
        assert counts["standing"] == 1 and counts["new"] == 0

        digest = render.render_digest(
            rows, target_date=TARGET, counts=counts,
            ordering={"8003": {"model_rank": 1}}, curation_method="fallback",
            settings=settings_obj,
        )
        assert "[STANDING | EMPLOYEE] Faculty" in digest.text
        assert "NEW" not in digest.text.split("STANDING")[0].split("\n")[-3:][0]
    finally:
        connection.close()


def test_a_body_heading_that_repeats_the_calendar_title_is_suppressed(settings_obj):
    """Two headlines can precede the body; a body block repeating either is noise.

    The Provost's Town Hall body opens with `Provost's Town Hall & Social`, which
    is *not* its Announcer subject -- so it survives on its own. Once the calendar
    callout displays that exact name directly above the body, showing it a third
    time is redundant.
    """
    from dailymail import calendar_action, calendar_enrich

    connection = db.connect()
    db.initialize(connection)
    try:
        body = "<h4>Provost’s Town Hall &amp; Social</h4><p>We welcome all.</p>"
        rows = _seed(
            connection,
            _record(8010, title="Provost's Town Hall - Oct 14", full_body=body,
                    body_text="Provost’s Town Hall & Social\nWe welcome all."),
        )
        counts = db.counts_for_date(connection, TARGET)
        ordering = {"8010": {"model_rank": 1}}

        # Without a calendar action the body heading is genuinely different
        # from the card title, so it stays.
        plain = render.render_digest(
            rows, target_date=TARGET, counts=counts, ordering=ordering,
            curation_method="fallback", settings=settings_obj,
        )
        assert "Provost’s Town Hall &amp; Social" in _body_copy(plain.html)
        assert plain.duplicate_titles_suppressed == 0

        action = calendar_action.CalendarAction(
            submission_id="8010", title="Provost's Town Hall & Social",
            start=__import__("datetime").datetime(2026, 10, 14, 10),
            end=__import__("datetime").datetime(2026, 10, 14, 12),
            timezone="America/New_York", location="Eynon Ballroom",
            attendance_mode="hybrid", description="",
            action_url=calendar_action.OUTLOOK_COMPOSE_URL + "?x=1",
            mechanism=calendar_action.MECHANISM_DEEPLINK,
        )
        withcal = render.render_digest(
            rows, target_date=TARGET, counts=counts, ordering=ordering,
            curation_method="fallback", settings=settings_obj,
            calendar={"8010": action},
        )
        assert withcal.duplicate_titles_suppressed == 1
        assert "Provost’s Town Hall &amp; Social" not in _body_copy(withcal.html)
        assert "We welcome all." in withcal.html
        # The callout still names the event.
        assert "Provost&#39;s Town Hall &amp; Social" in withcal.html
    finally:
        connection.close()
