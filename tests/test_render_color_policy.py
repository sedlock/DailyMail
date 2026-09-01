"""The render-time colour boundary around announcement body copy.

The reported defect, from the 1 September 2026 digest. Rowan announcement 6612,
`Nominate the PROFessional(s) of the Month`, wraps every paragraph of its body
in the same declaration:

    <span style="color:rgb(90,19,0);">...</span>

`rgb(90,19,0)` is `#5A1300`. DailyMail's own accent -- the colour its headlines
are painted in -- is `#57150B`. Three points apart. In light rendering the card
merely looked odd; in Outlook mobile's dark mode, which force-inverts the whole
design, the author's dark maroon and the design's dark maroon inverted to the
*same* peach, and the entire article read as one long headline.

The control is 6736, the OSEC reminders announcement, which carries no `color`
declaration at all and therefore inherits the digest's ink -- and which rendered
correctly on the same morning, in the same client, for exactly that reason.

Two boundaries are asserted here. The security allowlist in `filter_style` is
unchanged: `color` is still an inert property and still survives sanitization,
because deciding what is *safe* is a different question from deciding what is
*legible inside DailyMail's card*. The presentation policy is what the renderer
applies on top, and it is a render derivative only -- the stored `FullBody` keeps
every source byte.
"""

from __future__ import annotations

import re

import pytest

from conftest import OSEC_BODY, PROFESSIONAL_BODY, regression_record
from dailymail import db, render, sanitize

TARGET = "2026-09-01"

# The colour the source imposed, and the two DailyMail imposes.
LEAKED = "rgb(90,19,0)"
INK = render.BODY_INK
ACCENT = render.BODY_ACCENT
POLICY = render.BODY_COLOR_POLICY


def _sanitized(html: str) -> str:
    out, _stats, _images = sanitize.sanitize_body(html, color_policy=POLICY)
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


# --- the reported case -------------------------------------------------------


def test_the_professional_body_no_longer_inherits_the_headline_colour():
    assert LEAKED in PROFESSIONAL_BODY, "the fixture must carry the real defect"
    out = _sanitized(PROFESSIONAL_BODY)
    assert LEAKED not in out
    assert "rgb(" not in out
    for style in _styles_for(out, "p"):
        assert f"color:{INK}" in style


def test_every_paragraph_of_the_professional_body_gets_the_digest_ink():
    out = _sanitized(PROFESSIONAL_BODY)
    paragraphs = _styles_for(out, "p")
    assert len(paragraphs) == 6, "the announcement has six paragraphs"
    assert all(f"color:{INK}" in style for style in paragraphs)


def test_the_osec_control_still_renders_correctly():
    """The announcement that was already right must not change meaning."""
    assert "color:" not in OSEC_BODY
    out = _sanitized(OSEC_BODY)
    for style in _styles_for(out, "p"):
        assert f"color:{INK}" in style
    for style in _styles_for(out, "a"):
        assert f"color:{ACCENT}" in style
    # Its content is untouched.
    assert "Memorial Hall, Suite 157." in out
    assert "Title IX Sexual Harassment/Sexual Assault" in out


def test_the_stored_source_body_is_never_modified(settings_obj):
    """`FullBody` is authoritative. Only the render derivative is repainted."""
    connection = db.connect()
    db.initialize(connection)
    try:
        record = regression_record("6612")
        with db.transaction(connection):
            db.upsert_category(
                connection, category_id=record["category_id"], title="Human Resources",
                rowan_rank=1, color=None, is_active=True, manual_priority=4,
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
            ordering={"6612": {"model_rank": 1}},
            curation_method="fallback", settings=settings_obj,
        )
        assert LEAKED not in digest.html

        stored = connection.execute(
            "SELECT full_body FROM announcement_versions WHERE submission_id = 6612"
        ).fetchone()["full_body"]
        assert stored == PROFESSIONAL_BODY
        assert LEAKED in stored
    finally:
        connection.close()


# --- what the policy must preserve -------------------------------------------


@pytest.mark.parametrize(
    "source,tag",
    [
        ('<p><strong style="color:red">x</strong></p>', "strong"),
        ('<p><b style="color:red">x</b></p>', "b"),
        ('<p><em style="color:red">x</em></p>', "em"),
        ('<p><i style="color:red">x</i></p>', "i"),
        ('<p><u style="color:red">x</u></p>', "u"),
    ],
)
def test_emphasis_survives_the_colour_policy(source, tag):
    out = _sanitized(source)
    assert f"<{tag}" in out
    assert "color:red" not in out


def test_lists_headings_and_tables_survive():
    source = (
        '<h3 style="color:red">Heading</h3>'
        '<ul style="color:red"><li style="color:red">One</li>'
        '<li>Two</li></ul>'
        '<ol><li>Three</li></ol>'
        '<table><tr><td style="color:red">Cell</td>'
        '<th style="background-color:red">Head</th></tr></table>'
        '<blockquote style="color:red">Quoted</blockquote>'
    )
    out = _sanitized(source)
    assert "color:red" not in out
    assert "background-color:red" not in out
    for tag in ("h3", "ul", "ol", "li", "table", "tr", "td", "th", "blockquote"):
        assert f"<{tag}" in out
    assert "One" in out and "Two" in out and "Three" in out
    assert "Cell" in out and "Head" in out and "Quoted" in out


def test_body_headings_take_the_accent_and_prose_takes_the_ink():
    out = _sanitized('<h3 style="color:red">Heading</h3><p>Prose</p>')
    assert f"color:{ACCENT}" in _styles_for(out, "h3")[0]
    assert f"color:{INK}" in _styles_for(out, "p")[0]


def test_a_body_link_keeps_the_digest_link_colour():
    out = _sanitized(
        '<p>See <a href="https://rowan.edu/x" style="color:#1155cc">here</a>.</p>'
    )
    assert "#1155cc" not in out
    assert f"color:{ACCENT}" in _styles_for(out, "a")[0]
    assert 'href="https://rowan.edu/x"' in out


def test_author_layout_styles_are_untouched():
    """Only colour is DailyMail's to choose; spacing and alignment are not."""
    out = _sanitized(
        '<p style="margin-left:40px;text-align:center;font-size:18px;'
        'color:rgb(90,19,0)">Indented</p>'
    )
    style = _styles_for(out, "p")[0]
    assert "margin-left:40px" in style
    assert "text-align:center" in style
    assert "font-size:18px" in style
    assert "rgb(90,19,0)" not in style
    assert f"color:{INK}" in style


def test_the_injected_paragraph_margin_still_arrives():
    out = _sanitized('<p style="color:rgb(90,19,0)">x</p>')
    style = _styles_for(out, "p")[0]
    assert "margin:0 0 9px 0" in style
    assert f"color:{INK}" in style


def test_a_source_background_colour_cannot_hide_the_text():
    """A dark source background plus DailyMail's dark ink would be unreadable."""
    out = _sanitized('<p><span style="background-color:#000000">Invisible</span></p>')
    assert "#000000" not in out
    assert "Invisible" in out


def test_the_policy_is_idempotent():
    once = _sanitized(PROFESSIONAL_BODY)
    twice = sanitize.apply_body_color_policy(once, POLICY)
    assert once == twice


def test_the_policy_cannot_reintroduce_unsafe_content():
    hostile = (
        '<p style="color:red">ok</p><script>alert(1)</script>'
        '<a href="javascript:x" style="color:red">b</a>'
        '<p style="background:url(javascript:alert(1));color:red">c</p>'
        '<iframe src=x></iframe>'
    )
    out = _sanitized(hostile)
    for banned in ("script", "alert", "javascript:", "<iframe", "url("):
        assert banned not in out


def test_malformed_nesting_does_not_break_the_policy():
    """An unclosed span must not swallow the rest of the card."""
    out = _sanitized(
        '<p><span style="color:rgb(90,19,0)">first<p>second</p>'
    )
    assert "first" in out and "second" in out
    assert "rgb(90,19,0)" not in out
    assert out.count("<p") >= 2


# --- the security allowlist is deliberately unchanged ------------------------


def test_the_security_allowlist_still_permits_colour():
    """`filter_style` answers "is this inert?", not "is this legible here?".

    Conflating the two would mean a future decision about the palette had to be
    argued as a security change, which is how palettes end up unarguable.
    """
    assert "color" in sanitize.ALLOWED_STYLE_PROPERTIES
    assert sanitize.filter_style("color:red;position:fixed") == "color:red"


def test_sanitize_body_without_a_policy_keeps_the_author_colour():
    out, _stats, _images = sanitize.sanitize_body('<p style="color:red">x</p>')
    assert "color:red" in out


def test_the_policy_can_be_switched_off():
    import dataclasses

    off = dataclasses.replace(POLICY, strip_source_colors=False)
    out, _stats, _images = sanitize.sanitize_body(
        PROFESSIONAL_BODY, color_policy=off
    )
    assert LEAKED in out, "an off switch that does nothing is worse than none"


# --- the whole digest --------------------------------------------------------


def test_a_rendered_digest_carries_no_source_colour_at_all(settings_obj):
    """Both regression cases in one email, checked as a whole."""
    connection = db.connect()
    db.initialize(connection)
    try:
        with db.transaction(connection):
            for submission_id, category in (("6612", 4), ("6736", 4)):
                record = regression_record(submission_id)
                db.upsert_category(
                    connection, category_id=record["category_id"],
                    title="Human Resources", rowan_rank=1, color=None,
                    is_active=True, manual_priority=category,
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
            ordering={"6612": {"model_rank": 1}, "6736": {"model_rank": 2}},
            curation_method="fallback", settings=settings_obj,
        )
    finally:
        connection.close()

    declared = {
        match.group(1).strip().lower()
        for match in re.finditer(r"color\s*:\s*([^;\"']+)", digest.html)
    }
    # Every colour in the email is one the digest chose. `rgb(...)` was the
    # source's own notation; DailyMail only ever emits hex.
    assert not any(value.startswith("rgb(") for value in declared), sorted(declared)
    assert INK.lower() in declared
    assert ACCENT.lower() in declared
    # Both announcements are still complete.
    assert "who should be Rowan’s PROFessional(s) of the Month?" in digest.html
    assert "Memorial Hall, Suite 157." in digest.html
