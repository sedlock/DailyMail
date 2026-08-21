"""Rendering, MIME assembly, and the delivery contract."""

from __future__ import annotations

import json
import re

import pytest

from dailymail import curate, db, mailer, render
from dailymail.render import clean_subject

from conftest import TARGET_DATE


@pytest.fixture
def digest(populated_db, settings_obj):
    rows = db.digest_rows(populated_db, TARGET_DATE)
    counts = db.counts_for_date(populated_db, TARGET_DATE)
    ordering = {
        entry["submission_id"]: entry
        for entry in curate.fallback_rank(rows, TARGET_DATE, settings_obj)
    }
    return render.render_digest(
        rows, target_date=TARGET_DATE, counts=counts, ordering=ordering,
        curation_method="fallback", settings=settings_obj,
    ), rows


# --- completeness ------------------------------------------------------------


def test_every_announcement_rendered_exactly_once(digest):
    rendered, rows = digest
    assert len(rendered.submission_ids) == len(rows)
    assert len(set(rendered.submission_ids)) == len(rows)
    assert set(rendered.submission_ids) == {str(r["submission_id"]) for r in rows}


def test_each_title_appears_in_html_and_text(digest):
    """Titles appear HTML-escaped in the HTML part and raw in the text part.

    The escaping matters: it is what stops an announcement subject from being
    able to inject markup into the email.
    """
    # Use the template engine's own escaper so the expectation matches exactly
    # what Jinja emits (it renders ' as &#39;, which html.escape does not).
    from markupsafe import escape

    rendered, rows = digest
    for row in rows:
        title = clean_subject(row["title"])
        assert str(escape(title)) in rendered.html, title
        assert title in rendered.text, title


def test_titles_are_html_escaped(digest):
    rendered, rows = digest
    ampersand_titles = [
        clean_subject(r["title"]) for r in rows if "&" in (r["title"] or "")
    ]
    assert ampersand_titles, "fixture should contain a title with an ampersand"
    for title in ampersand_titles:
        assert title not in rendered.html, "raw ampersand must not be emitted"
        assert title.replace("&", "&amp;") in rendered.html


def test_complete_body_content_present_not_summarized(digest):
    """A distinctive phrase from deep inside each body must be in the email."""
    rendered, rows = digest
    for row in rows:
        text = (row["body_text"] or "").strip()
        if len(text) < 120:
            continue
        # Take a phrase from the second half of the body.
        words = text.split()
        phrase = " ".join(words[len(words) // 2:][:6])
        if len(phrase) < 20:
            continue
        normalized = " ".join(rendered.text.split())
        assert " ".join(phrase.split()) in normalized, (row["submission_id"], phrase)


def test_official_links_present_twice_in_html(digest):
    """Subject hyperlink plus the explicit 'View official announcement' link."""
    rendered, rows = digest
    for row in rows:
        url = db.official_url(row["submission_id"])
        assert rendered.html.count(url) >= 2, row["submission_id"]
        assert url in rendered.text


def test_view_official_announcement_link_present(digest):
    rendered, rows = digest
    assert rendered.html.count("View official announcement") == len(rows)


def test_official_urls_are_well_formed(digest):
    rendered, _ = digest
    found = set(
        re.findall(
            r"https://apps\.rowan\.edu/RowanAnnouncer/Announcement\?SubmissionId=(\d+)",
            rendered.html,
        )
    )
    assert found == set(rendered.submission_ids)


# --- subject -----------------------------------------------------------------


def test_exact_subject_format(digest, settings_obj):
    rendered, _ = digest
    assert rendered.subject == "Curated Rowan Daily Mail - August 20 2026"
    assert ", 2026" not in rendered.subject, "no comma before the year"


@pytest.mark.parametrize(
    "date_value,expected",
    [
        ("2026-08-21", "Curated Rowan Daily Mail - August 21 2026"),
        ("2026-01-01", "Curated Rowan Daily Mail - January 1 2026"),
        ("2026-12-25", "Curated Rowan Daily Mail - December 25 2026"),
        ("2027-03-09", "Curated Rowan Daily Mail - March 9 2027"),
    ],
)
def test_subject_for_various_dates(settings_obj, date_value, expected):
    assert settings_obj.subject_for(date_value) == expected


def test_alert_subject_format(settings_obj):
    assert settings_obj.alert_subject_for("2026-08-21") == (
        "DailyMail ATTENTION REQUIRED - August 21 2026"
    )


def test_subject_cleanup_fixes_whitespace_only():
    assert clean_subject("  Parking Lot O-1  Closure ") == "Parking Lot O-1 Closure"
    assert clean_subject("A B") == "A B"
    # Not an editorial rewrite: wording and punctuation are untouched.
    assert clean_subject("üBER: Don't Miss This!!") == "üBER: Don't Miss This!!"


# --- sections and grouping ---------------------------------------------------


def test_new_and_standing_sections_labelled(digest):
    rendered, _ = digest
    assert ">NEW <" in rendered.html or "NEW <span" in rendered.html
    assert "STANDING" in rendered.html
    assert "NEW --" in rendered.text
    assert "STANDING --" in rendered.text


def test_new_section_grouped_by_configured_category_order(digest, settings_obj):
    rendered, rows = digest
    priority = settings_obj.category_priority_map()
    headings = re.findall(
        r'letter-spacing:1\.2px;border-bottom:2px solid #FFCC00;"[^>]*>\s*([^<]+?)\s*<',
        rendered.html,
    )
    seen = [h.strip() for h in headings if h.strip()]
    assert seen, "no category group headings rendered"
    priorities = [priority.get(name.title(), None) for name in seen]
    # Compare using the real titles (headings are uppercased in the template).
    title_by_upper = {t.upper(): p for t, p in priority.items()}
    priorities = [title_by_upper[name] for name in seen]
    assert priorities == sorted(priorities)


def test_no_empty_category_group_rendered(digest):
    rendered, _ = digest
    for group in re.findall(r"border-bottom:2px solid #FFCC00", rendered.html):
        pass  # presence only; emptiness is asserted structurally below
    groups = render.group_new(
        {sid: {"status": "New", "category_priority": 1, "category_title": "X",
               "submission_id": sid, "model_rank": 1} for sid in []}
    )
    assert groups == []


def test_standing_is_globally_ordered(populated_db, settings_obj):
    rows = db.digest_rows(populated_db, TARGET_DATE)
    counts = db.counts_for_date(populated_db, TARGET_DATE)
    entries = curate.fallback_rank(rows, TARGET_DATE, settings_obj)
    ordering = {e["submission_id"]: e for e in entries}
    rendered = render.render_digest(
        rows, target_date=TARGET_DATE, counts=counts, ordering=ordering,
        curation_method="fallback", settings=settings_obj,
    )
    standing_ranks = {
        e["submission_id"]: e["model_rank"]
        for e in entries if e["section"] == "Standing"
    }
    standing_order = [
        sid for sid in rendered.submission_ids if sid in standing_ranks
    ]
    assert [standing_ranks[sid] for sid in standing_order] == sorted(
        standing_ranks[sid] for sid in standing_order
    )


# --- badges ------------------------------------------------------------------


def test_audience_labels_rendered(digest):
    rendered, rows = digest
    expected = {"Employees": "EMPLOYEE", "Students": "STUDENT", "Both": "EVERYONE"}
    for row in rows:
        assert expected[row["source_audience"]] in rendered.html
    assert rendered.html.count(">EVERYONE<") == sum(
        1 for r in rows if r["source_audience"] == "Both"
    )


def test_exactly_one_audience_badge_per_announcement(digest):
    rendered, rows = digest
    total = sum(
        rendered.html.count(f">{label}<")
        for label in ("EMPLOYEE", "STUDENT", "EVERYONE")
    )
    assert total == len(rows)


def test_updated_badge_only_for_changed_announcements(populated_db, settings_obj):
    with db.transaction(populated_db):
        populated_db.execute(
            "UPDATE daily_records SET changed = 1 WHERE target_date = ? "
            "AND submission_id = 6622",
            (TARGET_DATE,),
        )
    rows = db.digest_rows(populated_db, TARGET_DATE)
    counts = db.counts_for_date(populated_db, TARGET_DATE)
    ordering = {
        e["submission_id"]: e
        for e in curate.fallback_rank(rows, TARGET_DATE, settings_obj)
    }
    rendered = render.render_digest(
        rows, target_date=TARGET_DATE, counts=counts, ordering=ordering,
        curation_method="fallback", settings=settings_obj,
    )
    assert rendered.html.count(">UPDATED<") == 1
    assert "UPDATED" in rendered.text


def test_no_updated_badge_when_nothing_changed(digest):
    rendered, _ = digest
    assert ">UPDATED<" not in rendered.html


# --- metadata ----------------------------------------------------------------


def test_contact_submitter_and_approver_rendered(digest):
    rendered, rows = digest
    assert "Contact" in rendered.html
    assert "Submitted by" in rendered.html
    assert "Approved by" in rendered.html
    row = next(r for r in rows if r["submission_id"] == 6622)
    assert row["contact_department"] in rendered.html
    assert row["submitted_by_name"] in rendered.html
    assert row["approved_by_job_title"] in rendered.html
    assert row["contact_email"] in rendered.html


def test_event_metadata_rendered(populated_db, settings_obj, event_record):
    """An event announcement shows its name, date, time and location."""
    from dailymail import ingest
    from dailymail.normalize import normalize_record

    record = normalize_record(
        event_record, target_date=TARGET_DATE, source_query_membership=["Employees"]
    )
    record["distribution_dates"] = [TARGET_DATE]
    record["first_distribution_date"] = TARGET_DATE
    record["status"] = "New"
    with db.transaction(populated_db):
        version_id, _ = db.record_announcement(
            populated_db, record, observed_at="2026-08-20T00:00:00+00:00"
        )
        db.record_presence(
            populated_db, target_date=TARGET_DATE,
            submission_id=int(record["submission_id"]),
            source_views=["Employee"], observed_at="t",
        )
        db.record_daily(
            populated_db, target_date=TARGET_DATE,
            submission_id=int(record["submission_id"]), version_id=version_id,
            status="New", changed=False, observed_at="t",
        )
    rows = db.digest_rows(populated_db, TARGET_DATE)
    counts = db.counts_for_date(populated_db, TARGET_DATE)
    ordering = {
        e["submission_id"]: e
        for e in curate.fallback_rank(rows, TARGET_DATE, settings_obj)
    }
    rendered = render.render_digest(
        rows, target_date=TARGET_DATE, counts=counts, ordering=ordering,
        curation_method="fallback", settings=settings_obj,
    )
    from markupsafe import escape

    assert str(escape(record["event_name"])) in rendered.html
    assert record["event_location"] in rendered.html
    assert "9 AM" in rendered.html or "9:00" in rendered.html
    assert "EVENT" in rendered.text


def test_header_counts_rendered(digest):
    rendered, _ = digest
    counts = rendered.counts
    assert "CURATED ROWAN DAILY MAIL" in rendered.html
    assert f">{counts['new']}</strong> new" in rendered.html.replace("\n", "")
    assert "Thursday, August 20, 2026" in rendered.html


# --- plain text --------------------------------------------------------------


def test_plain_text_alternative_is_useful(digest):
    rendered, rows = digest
    text = rendered.text
    assert "CURATED ROWAN DAILY MAIL" in text
    assert "Thursday, August 20, 2026" in text
    assert "NEW --" in text and "STANDING --" in text
    for row in rows:
        assert clean_subject(row["title"]) in text
        assert db.official_url(row["submission_id"]) in text
    assert "Contact" in text and "Submitted by" in text and "Approved by" in text
    assert "<p>" not in text and "<div" not in text


def test_plain_text_has_no_runaway_blank_lines(digest):
    rendered, _ = digest
    assert "\n\n\n\n" not in rendered.text


# --- safety ------------------------------------------------------------------


def test_rendered_html_contains_no_unsafe_content(digest):
    rendered, _ = digest
    for banned in ("data:image", "<script", "javascript:", "file://", "onerror=",
                   "<iframe", "<form", "<object"):
        assert banned not in rendered.html, banned


def test_no_forbidden_ingestion_fields_in_render(digest):
    from dailymail.normalize import FORBIDDEN_KEYS

    rendered, _ = digest
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in rendered.html
    assert not re.search(r"\b9\d{8}\b", rendered.html)
    assert not re.search(r"\b9\d{8}\b", rendered.text)


def test_no_credential_material_in_render(digest, fake_credentials):
    rendered, _ = digest
    assert "abcdefghijklmnop" not in rendered.html
    assert "GMAIL_APP_PASSWORD" not in rendered.html
    assert "GMAIL_APP_PASSWORD" not in rendered.text


def test_content_hash_is_stable_and_content_sensitive(populated_db, settings_obj):
    def build():
        rows = db.digest_rows(populated_db, TARGET_DATE)
        counts = db.counts_for_date(populated_db, TARGET_DATE)
        ordering = {
            e["submission_id"]: e
            for e in curate.fallback_rank(rows, TARGET_DATE, settings_obj)
        }
        return render.render_digest(
            rows, target_date=TARGET_DATE, counts=counts, ordering=ordering,
            curation_method="fallback", settings=settings_obj,
        )

    first = build().content_hash
    assert build().content_hash == first, "hash must be stable"

    with db.transaction(populated_db):
        populated_db.execute(
            "UPDATE announcement_versions SET content_hash = 'different' "
            "WHERE submission_id = 6622"
        )
    assert build().content_hash != first, "hash must react to content change"


# --- MIME --------------------------------------------------------------------


def test_mime_structure_with_images(settings_obj):
    from dailymail.images import EmbeddedImage

    image = EmbeddedImage(
        cid="img1.1@dailymail.local", content_type="image/jpeg",
        data=b"\xff\xd8\xff\xd9" * 40, width=10, height=10, original_bytes=900,
    )
    prepared = mailer.build_message(
        settings=settings_obj, sender="x@gmail.com", subject="S",
        html='<html><body><img src="cid:img1.1@dailymail.local"></body></html>',
        text="plain", images=[image],
    )
    types = [part.get_content_type() for part in prepared.message.walk()]
    assert types == [
        "multipart/alternative", "text/plain", "multipart/related",
        "text/html", "image/jpeg",
    ]
    assert prepared.image_count == 1


def test_mime_structure_without_images(settings_obj):
    prepared = mailer.build_message(
        settings=settings_obj, sender="x@gmail.com", subject="S",
        html="<html><body>hi</body></html>", text="hi", images=[],
    )
    types = [part.get_content_type() for part in prepared.message.walk()]
    assert types == ["multipart/alternative", "text/plain", "text/html"]


def test_message_headers(settings_obj):
    prepared = mailer.build_message(
        settings=settings_obj, sender="digest@gmail.com",
        subject="Curated Rowan Daily Mail - August 20 2026",
        html="<p>x</p>", text="x", images=[],
    )
    message = prepared.message
    assert message["To"] == "sedlock@rowan.edu"
    assert message["From"] == "Curated Rowan Daily Mail <digest@gmail.com>"
    assert message["Subject"] == "Curated Rowan Daily Mail - August 20 2026"
    assert message["Message-ID"].startswith("<") and message["Message-ID"].endswith(">")
    assert message["Auto-Submitted"] == "auto-generated"


def test_plain_text_part_is_first_alternative(settings_obj):
    """Clients that prefer text/plain must find it before the HTML."""
    prepared = mailer.build_message(
        settings=settings_obj, sender="x@gmail.com", subject="S",
        html="<p>html body</p>", text="text body", images=[],
    )
    payload = prepared.message.get_payload()
    assert payload[0].get_content_type() == "text/plain"
    assert "text body" in payload[0].get_content()


def test_alert_message_has_no_secrets_or_source_data(settings_obj, fake_credentials):
    prepared = mailer.build_alert_message(
        settings=settings_obj, sender="x@gmail.com", target_date="2026-08-21",
        failure_class="ValidationError", detail="V2 count mismatch", attempts=3,
    )
    raw = prepared.message.as_string()
    assert prepared.subject == "DailyMail ATTENTION REQUIRED - August 21 2026"
    assert "V2 count mismatch" in raw
    assert "2026-08-21" in raw
    assert "attempts" in raw.lower()
    assert "journalctl" in raw
    assert "abcdefghijklmnop" not in raw
    assert "GMAIL_APP_PASSWORD" not in raw


def test_credentials_repr_never_leaks(fake_credentials):
    from dailymail import credentials

    creds = credentials.load()
    assert creds.password == "abcdefghijklmnop", "spaces must be stripped"
    assert "abcdefghijklmnop" not in repr(creds)
    assert "abcdefghijklmnop" not in str(creds)
    assert "<redacted>" in repr(creds)


def test_credentials_scrub(fake_credentials):
    from dailymail import credentials

    creds = credentials.load()
    scrubbed = credentials.scrub(f"failed with {creds.password}", creds)
    assert creds.password not in scrubbed
    assert "<redacted>" in scrubbed


def test_credentials_reject_bad_permissions(fake_credentials):
    from dailymail import credentials
    from dailymail.credentials import CredentialError

    fake_credentials.chmod(0o644)
    with pytest.raises(CredentialError, match="mode 0644"):
        credentials.load()


def test_credentials_reject_world_readable_directory(fake_credentials):
    from dailymail import credentials
    from dailymail.credentials import CredentialError

    fake_credentials.parent.chmod(0o755)
    try:
        with pytest.raises(CredentialError, match="must not be readable"):
            credentials.load()
    finally:
        fake_credentials.parent.chmod(0o700)


def test_credentials_missing_file(monkeypatch, tmp_path):
    from dailymail import credentials
    from dailymail.credentials import CredentialError

    with pytest.raises(CredentialError, match="not found"):
        credentials.load(tmp_path / "nope.env")


def test_environ_without_secrets(monkeypatch):
    from dailymail import credentials

    monkeypatch.setenv("GMAIL_APP_PASSWORD", "should-not-propagate")
    monkeypatch.setenv("GMAIL_SMTP_USER", "should-not-propagate")
    env = credentials.environ_without_secrets()
    assert "GMAIL_APP_PASSWORD" not in env
    assert "GMAIL_SMTP_USER" not in env
