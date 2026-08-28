"""The whole pipeline with calendar and repeat detection wired in.

These are the tests that matter operationally: they run `run_daily` end to end
and assert that the two new subsystems changed what the reader sees without
changing anything about how the digest is produced, validated or delivered.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import TARGET_DATE, artifact_from_fixtures
from dailymail import calendar_enrich, daily, db, mailer, render, travel


@pytest.fixture
def pipeline(monkeypatch, settings_obj, employee_fixture, student_fixture):
    """`run_daily` against fixture data and a fake SMTP. Returns the sent list."""
    sent: list = []
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)

    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: dict(artifact, target_date=target_date),
    )
    monkeypatch.setattr(daily.collector, "write_artifact", lambda a: Path("/dev/null"))
    monkeypatch.setattr(daily.mailer, "sender_address", lambda: "digest-test@gmail.com")
    monkeypatch.setattr(mailer, "sender_address", lambda: "digest-test@gmail.com")
    monkeypatch.setattr(
        daily.mailer, "send", lambda prepared, settings: sent.append(prepared) or "ok"
    )
    monkeypatch.setattr(
        daily.curate, "curate",
        lambda rows, target_date, settings, **kwargs: daily.curate.CurationOutcome(
            method="fallback", model=None,
            entries=daily.curate.fallback_rank(rows, target_date, settings),
            error="stubbed",
        ),
    )
    return sent


def test_a_normal_run_records_calendar_metrics(pipeline, settings_obj):
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "success"
    assert "candidates" in result.calendar

    connection = db.connect()
    try:
        run = connection.execute(
            "SELECT calendar_stats FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        assert json.loads(run["calendar_stats"])["candidates"] == (
            result.calendar["candidates"]
        )
        # Every announcement is evaluated, so the audit trail is complete.
        stored = db.calendar_recommendations(connection, TARGET_DATE)
        assert len(stored) == result.counts["unique"]
    finally:
        connection.close()


def test_calendar_enrichment_does_not_change_counts_or_classification(
    pipeline, settings_obj
):
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.counts["unique"] == 13
    assert result.counts["new"] + result.counts["standing"] == 13


def test_the_source_body_is_never_modified_by_the_new_subsystems(
    pipeline, settings_obj
):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    connection = db.connect()
    try:
        before = [
            tuple(row)
            for row in connection.execute(
                "SELECT submission_id, content_hash, full_body, title "
                "FROM announcement_versions ORDER BY submission_id"
            )
        ]
        daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
        after = [
            tuple(row)
            for row in connection.execute(
                "SELECT submission_id, content_hash, full_body, title "
                "FROM announcement_versions ORDER BY submission_id"
            )
        ]
        assert before == after
    finally:
        connection.close()


def test_a_calendar_failure_never_costs_the_digest(
    pipeline, settings_obj, monkeypatch
):
    monkeypatch.setattr(
        daily.calendar_enrich, "enrich_digest",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("calendar exploded")),
    )
    with pytest.raises(RuntimeError):
        daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    # The orchestration deliberately does not swallow a programming error, but
    # every *expected* failure inside enrichment is contained -- proven by
    # replacing the internals rather than the entry point.
    monkeypatch.undo()

    monkeypatch.setattr(
        calendar_enrich.calendar_action, "build_action_url",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("nope")),
    )
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "success"
    assert result.email_status in ("sent", "skipped_duplicate")


def test_a_calendar_failure_never_sends_an_operator_alert(
    pipeline, settings_obj, monkeypatch
):
    alerts: list = []
    monkeypatch.setattr(daily, "_try_alert", lambda *a, **k: alerts.append(a))
    monkeypatch.setattr(
        calendar_enrich.calendar_action, "build_ics",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("bad ics")),
    )
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "success"
    assert alerts == []


def test_a_dry_run_makes_no_route_lookup(pipeline, settings_obj, monkeypatch):
    """`conftest` already fails a real lookup; this asserts the count is zero."""
    result = daily.run_daily(
        target_date=TARGET_DATE, settings=settings_obj, dry_run=True
    )
    assert result.calendar["route_lookups"] == 0
    assert result.email_status == "dry_run"


# --- logical repeats through the pipeline ------------------------------------


def _plain_text(prepared) -> str:
    """The text/plain alternative, whatever the message's outer shape is."""
    part = next(
        candidate
        for candidate in prepared.message.walk()
        if candidate.get_content_type() == "text/plain"
    )
    return part.get_content()


def _deliver(connection, target_date, settings):
    db.record_delivery(
        connection, target_date=target_date, recipient=settings.recipient,
        content_hash_value="hash", state="sent", sent_at=db.now_utc(),
    )


def test_a_repost_of_a_delivered_announcement_is_shown_as_standing(
    pipeline, settings_obj, monkeypatch, employee_fixture, student_fixture
):
    """The reported defect: a new SubmissionId with identical text arrived New."""
    first = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert first.email_status == "sent"

    # Rowan reposts announcement 6625 as 7625, distributed the following day.
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    original = next(
        record for record in artifact["announcements"]
        if str(record["submission_id"]) == "6625"
    )
    repost = dict(
        original, submission_id=7625, distribution_dates=["2026-08-21"],
        first_distribution_date="2026-08-21", status="New",
    )
    next_day = dict(
        artifact, target_date="2026-08-21", announcements=[repost],
        counts=dict(artifact["counts"], unique=1),
    )
    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: next_day,
    )

    result = daily.run_daily(target_date="2026-08-21", settings=settings_obj)
    assert result.repeat_overrides == 1
    assert result.counts["new"] == 0
    assert result.counts["standing"] == 1

    connection = db.connect()
    try:
        row = db.digest_rows(connection, "2026-08-21")[0]
        assert row["source_status"] == "New", "Rowan's own semantics are preserved"
        assert row["status"] == "Standing", "the digest shows Standing"
        assert row["repeat_of_submission_id"] == 6625
        match = db.repeat_match(connection, 7625)
        assert match["confidence"] >= 0.99
        assert json.loads(match["evidence"])["body_similarity"] == 1.0
    finally:
        connection.close()

    body = _plain_text(pipeline[-1])
    assert "[STANDING" in body
    assert "[NEW" not in body


def test_a_repost_the_reader_never_received_stays_new(
    pipeline, settings_obj, monkeypatch, employee_fixture, student_fixture
):
    """Only a *delivered* announcement justifies demoting its repost."""
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    original = next(
        record for record in artifact["announcements"]
        if str(record["submission_id"]) == "6625"
    )
    # Collect the first day without sending, so nothing was ever delivered.
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj, dry_run=True)

    repost = dict(
        original, submission_id=7625, distribution_dates=["2026-08-21"],
        first_distribution_date="2026-08-21", status="New",
    )
    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: dict(
            artifact, target_date="2026-08-21", announcements=[repost],
            counts=dict(artifact["counts"], unique=1),
        ),
    )
    result = daily.run_daily(target_date="2026-08-21", settings=settings_obj)
    assert result.repeat_overrides == 0
    assert result.counts["new"] == 1


def test_repeat_detection_is_idempotent_across_reruns(
    pipeline, settings_obj, monkeypatch, employee_fixture, student_fixture
):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    original = next(
        r for r in artifact["announcements"] if str(r["submission_id"]) == "6625"
    )
    repost = dict(
        original, submission_id=7625, distribution_dates=["2026-08-21"],
        first_distribution_date="2026-08-21", status="New",
    )
    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: dict(
            artifact, target_date="2026-08-21", announcements=[repost],
            counts=dict(artifact["counts"], unique=1),
        ),
    )
    first = daily.run_daily(target_date="2026-08-21", settings=settings_obj)
    second = daily.run_daily(target_date="2026-08-21", settings=settings_obj)
    assert first.repeat_overrides == second.repeat_overrides == 1

    connection = db.connect()
    try:
        assert connection.execute(
            "SELECT COUNT(*) FROM repeat_matches"
        ).fetchone()[0] == 1
    finally:
        connection.close()


def test_a_repeat_detection_failure_leaves_the_day_as_rowan_classified_it(
    pipeline, settings_obj, monkeypatch
):
    monkeypatch.setattr(
        daily.repeats, "find_repeats",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("comparison failed")),
    )
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "success"
    assert result.repeat_overrides == 0
    assert result.counts["new"] == 5


# --- the message -------------------------------------------------------------


def test_an_ics_attachment_makes_the_message_multipart_mixed(settings_obj):
    from dailymail import calendar_action

    class _Fake:
        ics_bytes = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
        ics_filename = "Event.ics"

    prepared = mailer.build_message(
        settings=settings_obj, sender="x@gmail.com", subject="Curated Rowan Daily Mail - August 20 2026",
        html="<p>hi</p>", text="hi", images=[], calendar_attachments=[_Fake()],
    )
    assert prepared.message.get_content_type() == "multipart/mixed"
    assert prepared.calendar_count == 1
    assert prepared.calendar_filenames == ("Event.ics",)
    types = [part.get_content_type() for part in prepared.message.walk()]
    assert "multipart/alternative" in types
    assert "text/plain" in types
    assert "text/html" in types
    # `application/octet-stream` is what Outlook itself uses for a non-iMIP
    # `.ics` attachment; `text/calendar` would make the digest a meeting request.
    assert "application/octet-stream" in types
    assert "text/calendar" not in types


def test_no_calendar_attachment_leaves_the_message_shape_untouched(settings_obj):
    prepared = mailer.build_message(
        settings=settings_obj, sender="x@gmail.com",
        subject="Curated Rowan Daily Mail - August 20 2026",
        html="<p>hi</p>", text="hi", images=[],
    )
    assert prepared.message.get_content_type() == "multipart/alternative"
    assert prepared.calendar_count == 0


def test_two_events_get_distinct_attachment_names(settings_obj):
    class _Fake:
        def __init__(self):
            self.ics_bytes = b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"
            self.ics_filename = "Town-Hall.ics"

    prepared = mailer.build_message(
        settings=settings_obj, sender="x@gmail.com",
        subject="Curated Rowan Daily Mail - August 20 2026",
        html="<p>hi</p>", text="hi", images=[],
        calendar_attachments=[_Fake(), _Fake()],
    )
    assert prepared.calendar_filenames == ("Town-Hall.ics", "Town-Hall-2.ics")


def test_an_unusable_attachment_is_skipped_not_fatal(settings_obj):
    class _Broken:
        ics_bytes = None
        ics_filename = None

    prepared = mailer.build_message(
        settings=settings_obj, sender="x@gmail.com",
        subject="Curated Rowan Daily Mail - August 20 2026",
        html="<p>hi</p>", text="hi", images=[], calendar_attachments=[_Broken()],
    )
    assert prepared.calendar_count == 0
