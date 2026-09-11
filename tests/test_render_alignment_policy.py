"""The render-time alignment boundary around announcement body copy.

The reported defect, from the 9 September 2026 digest read on Outlook mobile in
dark reader mode. Rowan announcement 6846,
`Student-Led Discussion of "Comparing Strategic and Systemic Periods of
Starvation" RCHGHR`, opens every paragraph of its body with:

    <p style="margin-left:0in;text-align:justify;">

Full justification on a 390px pane, with no hyphenation engine to call on,
stretches the inter-word spacing of every line but the last. The reader gets
rivers of whitespace running down the column and an announcement that is
measurably harder to read than the one above it.

The control is 6926, `September 11 Memorial Service — 25th Anniversary`, which
carries no `style` attribute at all, inherits the digest's own left alignment,
and read correctly in the same client on the same morning for exactly that
reason.

Two boundaries are asserted here, exactly as for the colour policy. The security
allowlist in `filter_style` is unchanged: `text-align` is an inert property and
still survives sanitization, because deciding what is *safe* is a different
question from deciding what is *legible on a narrow viewport*. The presentation
policy is what the renderer applies on top, and it is a render derivative only --
the stored `FullBody` keeps every source byte.

What the policy deliberately does *not* touch is asserted too: a table cell's
alignment is the author saying something true about the data, and compact centred
content is a deliberate visual choice.
"""

from __future__ import annotations

import re

import pytest

from conftest import JUSTIFIED_BODY, MEMORIAL_BODY, regression_record
from dailymail import db, render, sanitize

TARGET = "2026-09-01"

POLICY = render.BODY_ALIGNMENT_POLICY
COLOR_POLICY = render.BODY_COLOR_POLICY


def _aligned(html: str) -> str:
    out, _stats, _images = sanitize.sanitize_body(html, alignment_policy=POLICY)
    return out


def _full(html: str) -> str:
    out, _stats, _images = sanitize.sanitize_body(
        html, color_policy=COLOR_POLICY, alignment_policy=POLICY
    )
    return out


def _open_tags(html: str):
    return re.finditer(r"<([a-zA-Z][a-zA-Z0-9]*)((?:\s[^>]*)?)>", html)


def _styles_for(html: str, tag: str) -> list[str]:
    found = []
    for match in _open_tags(html):
        if match.group(1).lower() != tag:
            continue
        style = re.search(r'style="([^"]*)"', match.group(2) or "")
        found.append(style.group(1) if style else "")
    return found


def _alignments_for(html: str, tag: str) -> list[str | None]:
    out = []
    for style in _styles_for(html, tag):
        match = re.search(r"text-align\s*:\s*([a-z-]+)", style)
        out.append(match.group(1) if match else None)
    return out


# --- the reported case -------------------------------------------------------


def test_the_fixture_still_carries_the_real_defect():
    """A regression test whose input has been cleaned up proves nothing."""
    assert "text-align:justify" in JUSTIFIED_BODY.replace(" ", "")
    assert JUSTIFIED_BODY.count("justify") >= 4


def test_the_justified_announcement_renders_as_ordinary_left_aligned_prose():
    out = _aligned(JUSTIFIED_BODY)
    assert "justify" not in out
    assert _alignments_for(out, "p") == ["left"] * len(_alignments_for(out, "p"))
    assert _alignments_for(out, "p"), "the body must still have paragraphs"


def test_the_september_11_control_is_unchanged_in_meaning():
    """The control had nothing to strip; it must simply keep reading correctly."""
    assert "text-align" not in MEMORIAL_BODY
    out = _aligned(MEMORIAL_BODY)
    assert "justify" not in out
    assert set(_alignments_for(out, "p")) == {"left"}


def test_the_regression_and_its_control_now_compute_identically():
    bad = set(_alignments_for(_aligned(JUSTIFIED_BODY), "p"))
    good = set(_alignments_for(_aligned(MEMORIAL_BODY), "p"))
    assert bad == good == {"left"}


def test_the_stored_source_body_is_never_modified(settings_obj):
    """The policy is a render derivative. SQLite keeps every source byte."""
    connection = db.connect()
    db.initialize(connection)
    try:
        with db.transaction(connection):
            db.upsert_category(
                connection, category_id=7, title="Social and Cultural Events",
                rowan_rank=1, color=None, is_active=True, manual_priority=1,
            )
            record = regression_record("6846", distribution_dates=[TARGET],
                                       first_distribution_date=TARGET)
            version_id, _ = db.record_announcement(
                connection, record, observed_at=db.now_utc()
            )
            db.record_daily(
                connection, target_date=TARGET, submission_id=record["submission_id"],
                version_id=version_id, status="New", changed=False,
                observed_at=db.now_utc(),
            )
        rows = db.digest_rows(connection, TARGET)
        stored = [row for row in rows if str(row["submission_id"]) == "6846"][0]
        assert stored["full_body"] == JUSTIFIED_BODY
        assert "text-align:justify" in stored["full_body"].replace(" ", "")

        digest = render.render_digest(
            rows, target_date=TARGET,
            counts=db.counts_for_date(connection, TARGET),
            ordering={"6846": {"model_rank": 1}},
            curation_method="fallback", settings=settings_obj,
        )
        assert "justify" not in digest.html
        # ...and the source is still byte-identical after rendering.
        again = db.digest_rows(connection, TARGET)
        assert [row for row in again if str(row["submission_id"]) == "6846"][0][
            "full_body"
        ] == JUSTIFIED_BODY
    finally:
        connection.close()


# --- every route by which justification could arrive -------------------------


@pytest.mark.parametrize(
    "source",
    [
        '<p style="text-align:justify">prose</p>',
        '<p style="text-align: JUSTIFY;">prose</p>',
        '<p style="margin-left:0in;text-align:justify;">prose</p>',
        '<div style="text-align:justify"><p>prose</p></div>',
        '<blockquote style="text-align:justify">prose</blockquote>',
        "<p align=\"justify\">prose</p>",
        "<div align=\"justify\"><p>prose</p></div>",
        '<li style="text-align:justify">prose</li>',
        '<h2 style="text-align:justify">heading</h2>',
    ],
)
def test_justification_never_survives_whatever_shape_it_arrives_in(source):
    out = _aligned(source)
    assert "justify" not in out.lower()


def test_the_align_attribute_is_removed_from_prose_rather_than_merely_overridden():
    """`align=` is a presentational attribute Outlook still honours."""
    out = _aligned('<p align="justify">prose</p>')
    assert "align=" not in out.replace("text-align", "")
    assert "text-align:left" in out


def test_word_spacing_used_to_support_justification_cannot_leak():
    source = (
        '<p style="text-align:justify;word-spacing:8px;text-align-last:justify;'
        'text-justify:inter-word">prose</p>'
    )
    out = _aligned(source)
    for leaked in ("justify", "word-spacing", "text-align-last", "text-justify"):
        assert leaked not in out.lower()
    assert "text-align:left" in out


def test_the_security_allowlist_already_drops_the_justification_helpers():
    """Belt and braces: these were never allowlisted, and must stay that way."""
    for prop in ("word-spacing", "text-align-last", "text-justify"):
        assert prop not in sanitize.ALLOWED_STYLE_PROPERTIES
    kept = sanitize.filter_style("word-spacing:8px;text-align-last:justify;color:#111")
    assert "word-spacing" not in kept and "text-align-last" not in kept
    assert "color:#111" in kept


def test_an_alignment_set_on_a_wrapper_cannot_reach_the_blocks_inside_it():
    out = _aligned('<div style="text-align:justify"><p>one</p><p>two</p></div>')
    assert _alignments_for(out, "p") == ["left", "left"]
    assert _alignments_for(out, "div") == ["left"]


def test_arbitrary_right_alignment_in_ordinary_prose_is_neutralized():
    out = _aligned('<p style="text-align:right">a right-aligned paragraph</p>')
    assert _alignments_for(out, "p") == ["left"]
    assert "text-align:right" not in out


def test_prose_with_no_alignment_at_all_is_pinned_left_explicitly():
    """Explicit, because Outlook inherits from whatever encloses the card."""
    out = _aligned("<p>plain prose</p>")
    assert _alignments_for(out, "p") == ["left"]


# --- what the policy must NOT touch ------------------------------------------


def test_a_meaningful_table_cell_alignment_survives():
    source = (
        "<table><tr>"
        '<th align="right" style="text-align:right">Amount</th>'
        "</tr><tr>"
        '<td align="right" style="text-align:right">1,250.00</td>'
        "</tr></table>"
    )
    out = _aligned(source)
    assert 'align="right"' in out
    assert "text-align:right" in out


def test_a_table_cell_is_not_forced_to_the_prose_alignment():
    out = _aligned('<table><tr><td style="text-align:center">mid</td></tr></table>')
    assert "text-align:center" in out


def test_compact_centred_content_stays_centred():
    out = _aligned('<p style="text-align:center">Register by 30 September</p>')
    assert "text-align:center" in out


def test_a_centred_block_holding_a_whole_article_is_treated_as_prose():
    body = "Every sentence of this announcement is centred. " * 12
    out = _aligned(f'<p style="text-align:center">{body}</p>')
    assert "text-align:left" in out
    assert "center" not in out


def test_a_compact_centred_wrapper_keeps_its_children_centred():
    out = _aligned('<div style="text-align:center"><p>Save the date</p></div>')
    assert _alignments_for(out, "div") == ["center"]
    assert _alignments_for(out, "p") == ["center"]


def test_lists_still_render_as_lists():
    out = _aligned("<ul><li>one</li><li>two</li></ul><ol><li>three</li></ol>")
    assert out.count("<li") == 3
    assert "<ul" in out and "<ol" in out
    assert _alignments_for(out, "li") == ["left", "left", "left"]


def test_author_layout_styles_other_than_alignment_are_untouched():
    out = _aligned('<p style="margin-left:24px;font-size:18px">indented</p>')
    assert "margin-left:24px" in out
    assert "font-size:18px" in out


def test_emphasis_and_links_survive_the_alignment_policy():
    out = _aligned(
        '<p style="text-align:justify">plain <strong>bold</strong> '
        '<em>italic</em> <a href="https://example.org/x">link</a></p>'
    )
    assert "<strong>bold</strong>" in out
    assert "<em>italic</em>" in out
    assert 'href="https://example.org/x"' in out


def test_images_and_figures_keep_their_own_layout():
    out = _aligned(
        '<figure style="text-align:center">'
        '<img src="https://example.org/a.png" alt="a">'
        '<figcaption style="text-align:center">caption</figcaption>'
        "</figure>"
    )
    assert out.count("text-align:center") == 2


# --- policy mechanics --------------------------------------------------------


def test_the_policy_is_idempotent():
    once = _aligned(JUSTIFIED_BODY)
    twice = sanitize.apply_body_alignment_policy(once, POLICY)
    assert once == twice


def test_the_policy_composes_with_the_colour_policy():
    out = _full(JUSTIFIED_BODY)
    assert "justify" not in out
    for style in _styles_for(out, "p"):
        assert f"color:{render.BODY_INK}" in style
        assert "text-align:left" in style


def test_the_policy_cannot_reintroduce_unsafe_content():
    hostile = (
        '<p style="text-align:justify">safe</p>'
        '<script>alert(1)</script>'
        '<p style="background:url(javascript:alert(1));text-align:justify">x</p>'
    )
    out = _full(hostile)
    for bad in ("<script", "javascript:", "url(", "alert("):
        assert bad not in out.lower()


def test_malformed_nesting_does_not_break_the_policy():
    out = _aligned('<div style="text-align:justify"><p>unclosed<div>nested</p>')
    assert "justify" not in out
    assert "unclosed" in out and "nested" in out


def test_sanitize_body_without_a_policy_keeps_the_author_alignment():
    """The sanitizer's own contract is unchanged; the policy is opt-in."""
    out, _stats, _images = sanitize.sanitize_body(JUSTIFIED_BODY)
    assert "text-align:justify" in out.replace(" ", "")


def test_the_policy_can_be_switched_off():
    import dataclasses

    off = dataclasses.replace(POLICY, normalize=False)
    out, _stats, _images = sanitize.sanitize_body(
        JUSTIFIED_BODY, alignment_policy=off
    )
    assert "text-align:justify" in out.replace(" ", "")


def test_a_rendered_digest_carries_no_source_justification_at_all(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    try:
        with db.transaction(connection):
            for submission_id, category in (("6846", 7), ("6926", 7)):
                db.upsert_category(
                    connection, category_id=category, title="Social and Cultural Events",
                    rowan_rank=1, color=None, is_active=True, manual_priority=1,
                )
                record = regression_record(
                    submission_id, distribution_dates=[TARGET],
                    first_distribution_date=TARGET, status="New",
                )
                version_id, _ = db.record_announcement(
                    connection, record, observed_at=db.now_utc()
                )
                db.record_daily(
                    connection, target_date=TARGET,
                    submission_id=record["submission_id"], version_id=version_id,
                    status="New", changed=False, observed_at=db.now_utc(),
                )
        rows = db.digest_rows(connection, TARGET)
        digest = render.render_digest(
            rows, target_date=TARGET,
            counts=db.counts_for_date(connection, TARGET),
            ordering={"6846": {"model_rank": 1}, "6926": {"model_rank": 2}},
            curation_method="fallback", settings=settings_obj,
        )
        assert "justify" not in digest.html
        assert "word-spacing" not in digest.html
        assert digest.html.count("text-align:left") > 0
    finally:
        connection.close()
