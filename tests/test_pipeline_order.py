"""The order the pipeline runs in, and the one decision nothing may overwrite.

`daily.PIPELINE_ORDER` states the intended sequence. The stage that matters here
is `display_status`: it is written by logical-repeat resolution and by nothing
else, ever. If a later stage could touch it, the 1 September regression would
have a second way to happen -- and the failure would be invisible, because the
digest would still be internally consistent, just wrong about what the reader
had already been sent.

So the assertions are about ownership rather than sequence numbers:

  * re-ingesting a date does not clear a decision already made for it;
  * curation cannot change it, because curation is accepted only as a
    permutation and never sees the column;
  * calendar and parking enrichment run after it and write to their own tables;
  * rendering reads `COALESCE(display_status, status)` and nothing else; and
  * Rowan's own `status` survives all of it, unchanged and queryable.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import TARGET_DATE, artifact_from_fixtures
from dailymail import daily, db


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
    monkeypatch.setattr(daily.mailer, "sender_address", lambda: "digest@example.invalid")
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


def test_the_intended_order_is_stated_in_one_place():
    assert daily.PIPELINE_ORDER.index("persist") < daily.PIPELINE_ORDER.index(
        "logical_repeat_family"
    )
    assert daily.PIPELINE_ORDER.index("logical_repeat_family") < (
        daily.PIPELINE_ORDER.index("display_status")
    )
    # Everything that could plausibly overwrite the decision comes after it.
    later = daily.PIPELINE_ORDER[
        daily.PIPELINE_ORDER.index("display_status") + 1 :
    ]
    assert set(later) == {
        "event_sessions",
        "calendar_relevance",
        "parking_enrichment",
        "calendar_enrichment",
        "render",
        "send",
    }


def test_repeat_resolution_runs_before_curation_sees_the_rows(
    pipeline, settings_obj, monkeypatch, employee_fixture, student_fixture
):
    """Curation must be handed the status the digest will actually show.

    Otherwise a demoted announcement would be ranked as New and then rendered
    as Standing, and the two halves of the digest would disagree.
    """
    seen: list[list[str]] = []
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    original = next(
        record_ for record_ in artifact["announcements"]
        if str(record_["submission_id"]) == "6625"
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
    monkeypatch.setattr(
        daily.curate, "curate",
        lambda rows, target_date, settings, **kwargs: (
            seen.append([row["status"] for row in rows])
            or daily.curate.CurationOutcome(
                method="fallback", model=None,
                entries=daily.curate.fallback_rank(rows, target_date, settings),
            )
        ),
    )
    result = daily.run_daily(target_date="2026-08-21", settings=settings_obj)
    assert result.repeat_overrides == 1
    assert seen[-1] == ["Standing"], "curation saw the demoted status, not Rowan's"


def test_reingesting_a_date_does_not_clear_the_repeat_decision(
    pipeline, settings_obj, monkeypatch, employee_fixture, student_fixture
):
    """The most likely way to lose it: `record_daily` on a second run."""
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
    daily.run_daily(target_date="2026-08-21", settings=settings_obj)

    connection = db.connect()
    try:
        before = db.daily_record(connection, "2026-08-21", 7625)
        assert before["display_status"] == "Standing"

        # A plain re-ingest of the same date, with no repeat stage at all.
        with db.transaction(connection):
            db.record_daily(
                connection, target_date="2026-08-21", submission_id=7625,
                version_id=before["version_id"], status="New", changed=False,
                observed_at=db.now_utc(),
            )
        after = db.daily_record(connection, "2026-08-21", 7625)
        assert after["display_status"] == "Standing", (
            "re-ingest must not discard a decision already made for this date"
        )
        assert after["status"] == "New", "Rowan's own classification is preserved"
    finally:
        connection.close()


def test_calendar_and_parking_enrichment_cannot_touch_display_status(
    pipeline, settings_obj, monkeypatch, employee_fixture, student_fixture
):
    """Both run after stage 6, and both write only to their own tables."""
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
    daily.run_daily(target_date="2026-08-21", settings=settings_obj)

    connection = db.connect()
    try:
        statuses = {
            (row["target_date"], row["submission_id"]): row["display_status"]
            for row in connection.execute(
                "SELECT target_date, submission_id, display_status FROM daily_records"
            )
        }
        # Re-run the two enrichment stages on their own and prove nothing moved.
        rows = db.digest_rows(connection, "2026-08-21")
        candidates, diagnostics = daily.calendar_enrich.detect_candidates(
            rows, target_date="2026-08-21", settings=settings_obj
        )
        daily.calendar_enrich.enrich_digest(
            connection, rows, target_date="2026-08-21", settings=settings_obj,
            candidates=candidates, diagnostics=diagnostics,
            curation_method="fallback", allow_routing=False,
        )
        daily.parking_enrich.enrich_digest(
            connection, rows, target_date="2026-08-21", settings=settings_obj
        )
        after = {
            (row["target_date"], row["submission_id"]): row["display_status"]
            for row in connection.execute(
                "SELECT target_date, submission_id, display_status FROM daily_records"
            )
        }
        assert statuses == after
    finally:
        connection.close()


def test_the_digest_renders_the_display_status_and_records_the_source(
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
    daily.run_daily(target_date="2026-08-21", settings=settings_obj)

    connection = db.connect()
    try:
        row = db.digest_rows(connection, "2026-08-21")[0]
        assert row["status"] == "Standing"        # what the digest shows
        assert row["source_status"] == "New"      # what Rowan said
        assert row["display_status"] == "Standing"
        counts = db.counts_for_date(connection, "2026-08-21")
        assert counts["new"] == 0 and counts["standing"] == 1
    finally:
        connection.close()


def test_a_skipped_repeat_stage_leaves_a_durable_trace(
    pipeline, settings_obj, monkeypatch
):
    """The 1 September failure mode: a WARNING line was the only evidence.

    A skipped stage still must not fail the run -- the reader gets the digest
    with Rowan's own labels, which is the safe direction -- but it must be
    answerable afterwards from the database, not only from the journal.
    """
    monkeypatch.setattr(
        daily.repeats, "find_repeats",
        lambda *a, **k: (_ for _ in ()).throw(
            __import__("sqlite3").OperationalError("unable to open database file")
        ),
    )
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "success"
    assert result.repeat_overrides == 0

    connection = db.connect()
    try:
        notes = db.display_status_corrections(connection, TARGET_DATE)
        assert len(notes) == 1
        assert notes[0]["submission_id"] == 0, "a stage-level note, not a record"
        assert notes[0]["new_display_status"] is None
        assert "logical-repeat stage skipped" in notes[0]["reason"]
        assert "unable to open database file" in notes[0]["reason"]
    finally:
        connection.close()


def test_an_audit_note_that_cannot_be_written_still_does_not_fail_the_run(
    pipeline, settings_obj, monkeypatch
):
    """Belt and braces: the trace is diagnostics, never the product."""
    monkeypatch.setattr(
        daily.repeats, "find_repeats",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
    )
    monkeypatch.setattr(
        daily.db, "record_display_status_correction",
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("no audit table")),
    )
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "success"
    assert result.email_status in ("sent", "skipped_duplicate")
