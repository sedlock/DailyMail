"""What must never leave the machine, and what the normal test run may touch.

Three separate promises, all of which the changes for the 1 September fixes had
the opportunity to break:

  * the SMTP App Password never reaches Claude, a log line, or a message body --
    and the curation payload now carries session dates, which is more source
    text than it used to;
  * the forbidden Rowan fields Phase 1 catalogued still never reach Claude --
    the payload grew, so what it grew by has to be checked;
  * the normal `uv run pytest` makes no network call and launches no browser --
    the repository now contains browser code, so "the production path has no
    browser dependency" needs to be a test rather than an intention.
"""

from __future__ import annotations

import json
import re
import socket
import sys
from pathlib import Path

import pytest

from conftest import (
    COFFEE_HOURS_BODY,
    COFFEE_HOURS_TEXT,
    TARGET_DATE,
    artifact_from_fixtures,
)
from dailymail import calendar_enrich, curate, db, ingest

REPO = Path(__file__).resolve().parents[1]

TARGET = "2026-09-01"

# Every field Rowan's API over-exposes and Phase 1 refuses to forward.
FORBIDDEN_IN_PAYLOAD = (
    "Username", "External_Id", "Last_Login", "Password", "Is_Active",
    "User", "Id_User", "Banner",
)


def _coffee_rows(connection):
    record = {
        "submission_id": 6702,
        "title": "Provost’s Coffee Hours (Focus on Research)",
        "full_body": COFFEE_HOURS_BODY,
        "body_text": COFFEE_HOURS_TEXT,
        "source_audience": "Employees",
        "category_id": 12,
        "distribution_dates": [TARGET],
        "first_distribution_date": TARGET,
        "status": "New",
        "is_event": 0,
        "contact_name": "Sarah Fobes",
        "contact_email": "fobes@rowan.edu",
        "submitted_by_name": "Sarah Fobes",
        "submitted_by_email": "fobes@rowan.edu",
        "approved_by_email": "approver@rowan.edu",
    }
    with db.transaction(connection):
        db.upsert_category(
            connection, category_id=12, title="Glassboro Campus", rowan_rank=1,
            color=None, is_active=True, manual_priority=10,
        )
        version_id, _ = db.record_announcement(
            connection, record, observed_at=db.now_utc()
        )
        db.record_daily(
            connection, target_date=TARGET, submission_id=6702,
            version_id=version_id, status="New", changed=False,
            observed_at=db.now_utc(),
        )
    return db.digest_rows(connection, TARGET)


@pytest.fixture
def seeded(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    yield connection
    connection.close()


# --- the credential -----------------------------------------------------------


def test_no_smtp_secret_reaches_the_curation_payload(seeded, settings_obj):
    """The payload grew a session list; the password still is not in it."""
    from dailymail import credentials

    rows = _coffee_rows(seeded)
    candidates, _ = calendar_enrich.detect_candidates(
        rows, target_date=TARGET, settings=settings_obj
    )
    payload = curate.build_payload(
        rows, TARGET, settings_obj, event_candidates=candidates
    )
    encoded = json.dumps(payload)
    creds = credentials.load()
    assert creds.password
    assert creds.password not in encoded
    assert creds.password.replace(" ", "") not in encoded
    assert "GMAIL_APP_PASSWORD" not in encoded
    assert "GMAIL_SMTP_USER" not in encoded


def test_the_session_block_carries_only_dates_and_times(seeded, settings_obj):
    """The one field the multi-session work added to the model's input.

    A session entry is three values, all of them read back from our own
    extraction. Nothing about the reader, the venue cache, the travel plan or
    DailyMail's own scoring goes with it.
    """
    rows = _coffee_rows(seeded)
    candidates, _ = calendar_enrich.detect_candidates(
        rows, target_date=TARGET, settings=settings_obj
    )
    payload = curate.build_payload(
        rows, TARGET, settings_obj, event_candidates=candidates
    )
    item = next(
        entry
        for entry in payload["new_items"] + payload["standing_items"]
        if entry.get("event_candidate")
    )
    sessions = item["event_candidate"]["sessions"]
    assert all(set(entry) == {"date", "start_time", "end_time"} for entry in sessions)


def test_no_email_address_reaches_the_curation_payload(seeded, settings_obj):
    """Redaction still holds now that more body text travels."""
    rows = _coffee_rows(seeded)
    candidates, _ = calendar_enrich.detect_candidates(
        rows, target_date=TARGET, settings=settings_obj
    )
    payload = curate.build_payload(
        rows, TARGET, settings_obj, event_candidates=candidates
    )
    encoded = json.dumps(payload)
    assert not re.search(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", encoded)


def test_no_forbidden_rowan_field_reaches_the_curation_payload(
    seeded, settings_obj, employee_fixture, student_fixture
):
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    ingest.ingest_artifact(seeded, artifact, settings_obj, origin="test")
    rows = db.digest_rows(seeded, TARGET_DATE)
    candidates, _ = calendar_enrich.detect_candidates(
        rows, target_date=TARGET_DATE, settings=settings_obj
    )
    payload = curate.build_payload(
        rows, TARGET_DATE, settings_obj, event_candidates=candidates
    )
    encoded = json.dumps(payload)
    for field in FORBIDDEN_IN_PAYLOAD:
        assert f'"{field}"' not in encoded
    # Nor the identity blocks the digest itself renders.
    for key in ("submitted_by_email", "approved_by_email", "contact_email",
                "submitted_by_name", "approved_by_name"):
        assert key not in encoded


def test_no_ics_payload_or_action_url_reaches_the_curation_payload(
    seeded, settings_obj
):
    """Claude judges relevance. It never sees, and cannot influence, the artefact."""
    rows = _coffee_rows(seeded)
    candidates, _ = calendar_enrich.detect_candidates(
        rows, target_date=TARGET, settings=settings_obj
    )
    payload = curate.build_payload(
        rows, TARGET, settings_obj, event_candidates=candidates
    )
    encoded = json.dumps(payload)
    assert "BEGIN:VCALENDAR" not in encoded
    assert "outlook.office.com" not in encoded
    assert ".ics" not in encoded


# --- the browser --------------------------------------------------------------


def test_the_production_path_imports_no_browser_module():
    """`dailymail` must not depend on Playwright, however indirectly."""
    import dailymail.daily  # noqa: F401 - importing is the assertion
    import dailymail.render  # noqa: F401
    import dailymail.mailer  # noqa: F401

    loaded = {name.split(".")[0] for name in sys.modules}
    for forbidden in ("playwright", "selenium", "pyppeteer"):
        assert forbidden not in loaded


def test_no_production_module_mentions_playwright():
    """Grep, deliberately: an import guard would not catch a subprocess call."""
    offenders = []
    for path in sorted((REPO / "src" / "dailymail").rglob("*.py")):
        text = path.read_text(encoding="utf-8").lower()
        if "playwright" in text or "chromium" in text:
            offenders.append(path.relative_to(REPO))
    assert offenders == []


def test_the_browser_qa_suite_is_opt_in():
    """It must skip without the flag, wherever it is run from."""
    source = (REPO / "tests" / "test_visual_qa.py").read_text(encoding="utf-8")
    assert 'os.environ.get("DAILYMAIL_VISUAL_QA")' in source
    assert "pytestmark" in source
    assert "skipif" in source


def test_browser_qa_tooling_lives_outside_the_package():
    """Diagnostics belong in `tools/`, not in the shipped wheel."""
    assert (REPO / "tools" / "qa" / "visual-regression.mjs").is_file()
    assert (REPO / "tools" / "qa" / "build_page.py").is_file()
    assert not list((REPO / "src" / "dailymail").rglob("*.mjs"))
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    assert "playwright" not in pyproject.lower()


def test_the_qa_page_and_screenshots_are_not_committed():
    """Fixtures yes, output no."""
    gitignore = (REPO / ".gitignore").read_text(encoding="utf-8")
    assert "artifacts/qa/pages/" in gitignore
    assert "artifacts/qa/screenshots/" in gitignore
    # The fixtures themselves must be present and readable.
    fixture = REPO / "artifacts" / "qa" / "fixtures" / "render-regression.json"
    assert fixture.is_file()
    json.loads(fixture.read_text(encoding="utf-8"))


# --- the network --------------------------------------------------------------


def test_the_normal_suite_cannot_open_a_socket(seeded, settings_obj, monkeypatch):
    """A proof by construction that the calendar path makes no call of its own.

    `conftest` already fails an unexpected route lookup; this closes the door at
    the socket instead, so a *new* dependency added anywhere under enrichment
    shows up here rather than as a slow morning in production.
    """
    def refuse(*args, **kwargs):
        raise AssertionError("the test suite must not open a network connection")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)

    rows = _coffee_rows(seeded)
    candidates, diagnostics = calendar_enrich.detect_candidates(
        rows, target_date=TARGET, settings=settings_obj
    )
    actions, metrics = calendar_enrich.enrich_digest(
        seeded, rows, target_date=TARGET, settings=settings_obj,
        candidates=candidates, diagnostics=diagnostics,
        curation_entries=[
            {
                "submission_id": "6702",
                "calendar": {"offer": True, "confidence": 0.95, "reason": "x"},
            }
        ],
        curation_method="claude", allow_routing=False,
    )
    assert "6702" in actions
    assert metrics.route_lookups == 0
    assert metrics.errors == []


def test_routing_is_reached_only_through_the_injected_router(
    seeded, settings_obj, monkeypatch
):
    """Travel is the one part of the calendar path that can touch the network.

    It is injected, so a caller that does not want it -- a dry run, a render, the
    QA page builder -- provably does not get it.
    """
    calls: list = []
    body = (
        "Provost's Coffee Hours are small-group conversations.\n"
        "Location: Chamberlain Student Center, Eynon Ballroom\n"
        "The dates are:\n"
        "Thursday, September 10 from 11:00-12:30\n"
        "Monday, September 21 from 2:30-4:00\n"
    )
    from dailymail import parking

    with db.transaction(seeded):
        seeded.execute(
            "INSERT INTO parking_landmarks (campus, name, normalized_name, "
            "latitude, longitude, last_verified_at) VALUES "
            "('glassboro', 'Chamberlain Student Center', ?, 39.708828, "
            "-75.117798, ?)",
            (parking.normalize_name("Chamberlain Student Center"), db.now_utc()),
        )
    rows = _coffee_rows(seeded)
    with db.transaction(seeded):
        seeded.execute(
            "UPDATE announcement_versions SET body_text = ? WHERE submission_id = 6702",
            (body,),
        )
    rows = db.digest_rows(seeded, TARGET)

    candidates, diagnostics = calendar_enrich.detect_candidates(
        rows, target_date=TARGET, settings=settings_obj
    )

    def router(origin, destination, *, settings):
        calls.append((origin, destination))
        return None

    calendar_enrich.enrich_digest(
        seeded, rows, target_date=TARGET, settings=settings_obj,
        candidates=candidates, diagnostics=diagnostics,
        curation_entries=[
            {
                "submission_id": "6702",
                "calendar": {"offer": True, "confidence": 0.95, "reason": "x"},
            }
        ],
        curation_method="claude", allow_routing=True, router=router,
    )
    # One venue, one lookup at most -- never one per sitting.
    assert len(calls) <= 1
