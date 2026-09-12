"""The NEW -> STANDING transition, and the surface that keeps it visible.

The digest already labelled every card and already carried a
`STANDING -- N continuing announcements` heading. On an Outlook-mobile scroll
through forty announcements that was still easy to lose: the heading was a 7px
grey strip, and once it was three screens behind you, nothing on the card in
front of you said which half of the digest you were in.

Two changes, asserted here.

* One barrier, once, at the crossing. It keeps the section text -- that sentence
  is genuinely useful -- and adds the design's own gold accent rule, a heavier
  label band and a caption band, with real vertical space above it.
* A persistent surface. A Standing card sits on a slightly warmer neutral than a
  New one. Only the surface moves: the body ink, the headline accent, and the
  NEW/STANDING/UPDATED badges are all unchanged, so a continuing announcement
  reads as continuing rather than as disabled.

The computed-style half of this lives in the browser QA suite, which measures
what the markup resolves to at three viewports and under a dark-mode
approximation. What is asserted here is the markup contract those measurements
depend on.
"""

from __future__ import annotations

import re

import pytest

from conftest import regression_record
from dailymail import db, render

TARGET = "2026-09-01"

NEW_SURFACE = "#ffffff"
STANDING_SURFACE = "#f4f2f1"


def _seed(connection, entries):
    """`entries` is a list of (fixture id, status, changed)."""
    stamp = db.now_utc()
    with db.transaction(connection):
        for index, (submission_id, status, changed) in enumerate(entries):
            record = regression_record(
                submission_id,
                distribution_dates=[TARGET],
                first_distribution_date=TARGET,
                status=status,
            )
            db.upsert_category(
                connection,
                category_id=record["category_id"],
                title=f"Category {record['category_id']}",
                rowan_rank=index + 1,
                color=None,
                is_active=True,
                manual_priority=index + 1,
            )
            version_id, _ = db.record_announcement(
                connection, record, observed_at=stamp
            )
            db.record_daily(
                connection,
                target_date=TARGET,
                submission_id=record["submission_id"],
                version_id=version_id,
                status=status,
                changed=changed,
                observed_at=stamp,
            )


@pytest.fixture
def digest_for(settings_obj):
    def build(entries):
        connection = db.connect()
        db.initialize(connection)
        try:
            _seed(connection, entries)
            rows = db.digest_rows(connection, TARGET)
            return render.render_digest(
                rows,
                target_date=TARGET,
                counts=db.counts_for_date(connection, TARGET),
                ordering={
                    str(entry[0]): {"model_rank": index + 1}
                    for index, entry in enumerate(entries)
                },
                curation_method="fallback",
                settings=settings_obj,
            )
        finally:
            connection.close()

    return build


MIXED = [
    ("6612", "New", False),
    ("6736", "New", False),
    ("6783", "Standing", False),
    ("6815", "Standing", True),
]
ONLY_NEW = [("6612", "New", False), ("6736", "New", False)]
ONLY_STANDING = [("6783", "Standing", False), ("6815", "Standing", True)]


# --- the one-time transition -------------------------------------------------


def test_a_digest_with_both_sections_carries_exactly_one_barrier(digest_for):
    html = digest_for(MIXED).html
    assert html.count("standing-transition") == 1


def test_the_barrier_is_never_duplicated_per_card(digest_for):
    """Four announcements, two of them Standing, still one crossing."""
    html = digest_for(MIXED).html
    assert html.count("standing-transition") == 1
    assert html.count("card-standing") == 2


def test_a_digest_with_no_standing_announcements_has_no_barrier(digest_for):
    html = digest_for(ONLY_NEW).html
    assert "standing-transition" not in html
    assert "card-standing" not in html
    assert html.count("card-new") == 2


def test_a_digest_with_no_new_announcements_still_marks_the_section(digest_for):
    """Nothing to cross *from*, but the section still needs its heading."""
    html = digest_for(ONLY_STANDING).html
    assert html.count("standing-transition") == 1
    assert "card-new" not in html


def test_the_barrier_keeps_the_section_text(digest_for):
    html = digest_for(MIXED).html
    assert "STANDING" in html
    assert "2 continuing announcements" in html


def test_the_section_text_is_singular_for_one_announcement(digest_for):
    html = digest_for([("6612", "New", False), ("6783", "Standing", False)]).html
    assert "1 continuing announcement<" in html
    assert "continuing announcements" not in html


def test_the_barrier_uses_the_designs_gold_accent(digest_for):
    html = digest_for(MIXED).html
    barrier = re.search(
        r'standing-transition.*?</table>', html, re.DOTALL
    ).group(0)
    assert "#FFCC00" in barrier
    # A rule, a label band and a caption band: three distinct surfaces.
    backgrounds = set(re.findall(r"background:(#[0-9A-Fa-f]{6})", barrier))
    assert len(backgrounds) >= 3, backgrounds


def test_the_barrier_gives_itself_vertical_breathing_room(digest_for):
    html = digest_for(MIXED).html
    assert 'class="standing-transition" style="padding:26px 0 0 0' in html


def test_the_barrier_does_not_add_a_second_banner(digest_for):
    """Deliberately a barrier, not another hero: no image, no huge type."""
    barrier = re.search(
        r'standing-transition.*?</table>', digest_for(MIXED).html, re.DOTALL
    ).group(0)
    assert "<img" not in barrier
    sizes = [int(v) for v in re.findall(r"font-size:(\d+)px", barrier)]
    assert sizes and max(sizes) <= 14


# --- the persistent surface --------------------------------------------------


def test_standing_cards_get_their_own_surface(digest_for):
    html = digest_for(MIXED).html
    standing = re.findall(r'<td class="pad card card-standing" style="([^"]*)"', html)
    assert len(standing) == 2
    assert all(f"background:{STANDING_SURFACE}" in style for style in standing)


def test_new_cards_keep_the_treatment_they_had(digest_for):
    html = digest_for(MIXED).html
    new = re.findall(r'<td class="pad card card-new" style="([^"]*)"', html)
    assert len(new) == 2
    assert all(f"background:{NEW_SURFACE}" in style for style in new)


def test_the_two_surfaces_actually_differ(digest_for):
    assert NEW_SURFACE != STANDING_SURFACE
    html = digest_for(MIXED).html
    assert f"background:{STANDING_SURFACE}" in html
    assert f"background:{NEW_SURFACE}" in html


def test_standing_body_copy_keeps_the_same_ink_as_new(digest_for):
    """A different surface, never dimmer text."""
    html = digest_for(MIXED).html
    bodies = re.findall(r'<div class="body-copy" style="([^"]*)"', html)
    assert len(bodies) == 4
    assert all(f"color:{render.BODY_INK}" in style for style in bodies)


def test_the_standing_badge_is_unchanged(digest_for):
    html = digest_for(MIXED).html
    assert html.count(">STANDING</td>") == 2


def test_the_updated_badge_is_unchanged(digest_for):
    """UPDATED must stay easy to pick out on the new Standing surface."""
    html = digest_for(MIXED).html
    assert html.count(">UPDATED</td>") == 1
    updated = re.search(r'<td style="([^"]*)">UPDATED</td>', html).group(1)
    assert "background:#FFCC00" in updated
    assert "color:#3d2600" in updated


def test_the_new_badge_is_unchanged(digest_for):
    html = digest_for(MIXED).html
    assert html.count(">NEW</td>") == 2
    new_badge = re.search(r'<td style="([^"]*)">NEW</td>', html).group(1)
    assert "background:#57150B" in new_badge
    assert "color:#ffffff" in new_badge


def test_an_updated_standing_card_shows_both_badges(digest_for):
    html = digest_for(MIXED).html
    card = re.search(
        r'<td class="pad card card-standing".*?>UPDATED</td>', html, re.DOTALL
    )
    assert card is not None
    assert ">STANDING</td>" in card.group(0)


# --- what must not have moved ------------------------------------------------


def test_the_plain_text_alternative_still_separates_the_sections(digest_for):
    text = digest_for(MIXED).text
    assert text.count("STANDING -- 2 continuing announcements") == 1
    assert "NEW -- 2 announcements" in text


def test_the_stats_strip_still_reports_both_counts(digest_for):
    html = digest_for(MIXED).html
    assert re.search(r">2</strong> new", html)
    assert re.search(r">2</strong> standing", html)


def test_every_announcement_still_appears_exactly_once(digest_for):
    digest = digest_for(MIXED)
    assert sorted(digest.submission_ids) == ["6612", "6736", "6783", "6815"]
    for submission_id in ("6612", "6736", "6783", "6815"):
        assert digest.html.count(f"SubmissionId={submission_id}") >= 1


def test_the_surface_is_driven_by_display_status_not_by_rowans_own(
    settings_obj,
):
    """A logical repeat shown as Standing gets the Standing surface.

    Rowan's own classification stays in `source_status`; `display_status` is
    what the digest shows, and it is what the card must follow.
    """
    connection = db.connect()
    db.initialize(connection)
    try:
        stamp = db.now_utc()
        with db.transaction(connection):
            record = regression_record(
                "6783", distribution_dates=[TARGET],
                first_distribution_date=TARGET, status="New",
            )
            db.upsert_category(
                connection, category_id=record["category_id"], title="Health",
                rowan_rank=1, color=None, is_active=True, manual_priority=1,
            )
            version_id, _ = db.record_announcement(
                connection, record, observed_at=stamp
            )
            db.record_daily(
                connection, target_date=TARGET, submission_id=record["submission_id"],
                version_id=version_id, status="New", changed=False, observed_at=stamp,
            )
            connection.execute(
                "UPDATE daily_records SET display_status = 'Standing' "
                "WHERE target_date = ? AND submission_id = ?",
                (TARGET, record["submission_id"]),
            )
        rows = db.digest_rows(connection, TARGET)
        assert rows[0]["source_status"] == "New"
        assert rows[0]["status"] == "Standing"
        digest = render.render_digest(
            rows, target_date=TARGET,
            counts=db.counts_for_date(connection, TARGET),
            ordering={"6783": {"model_rank": 1}},
            curation_method="fallback", settings=settings_obj,
        )
        assert "card-standing" in digest.html
        assert "card-new" not in digest.html
    finally:
        connection.close()
