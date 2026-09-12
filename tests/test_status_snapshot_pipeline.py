"""A full `run_daily` publishes a usable snapshot, through the real pipeline.

The unit tests drive `start_run`/`finish_run`/`record_delivery` directly. This
drives the orchestration that calls them, with the collector, curation and SMTP
stubbed exactly as the rest of the pipeline suite stubs them -- so what is being
asserted is that a *real run* leaves behind a snapshot ControlPanel can read,
not that three functions work in isolation.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from conftest import TARGET_DATE, artifact_from_fixtures
from dailymail import config, daily, db, health, status_snapshot, systemd_units


@pytest.fixture
def pipeline(monkeypatch, settings_obj, employee_fixture, student_fixture):
    """The same wiring `test_pipeline_order.pipeline` installs.

    Duplicated deliberately rather than imported, exactly as
    `test_hermetic_boundary_regression` duplicates it: a module whose subject is
    what a real run leaves behind should own the stubs that make the run real.
    """
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


@pytest.fixture(autouse=True)
def timer_status(monkeypatch):
    monkeypatch.setattr(
        systemd_units,
        "status",
        lambda: {
            "service": "dailymail.service",
            "timer": "dailymail.timer",
            "service_load_state": "loaded",
            "timer_enabled": "enabled",
            "timer_active": "active",
            "next_elapse": "Sat 2026-09-12 06:30:00 EDT",
            "last_result": "success",
            "list_timers": "",
            "linger": True,
        },
    )


def test_a_full_run_leaves_a_snapshot_a_collector_can_read(pipeline, settings_obj):
    assert not config.status_snapshot_path().exists()
    result = daily.run_daily(target_date=TARGET_DATE, trigger="timer", settings=settings_obj)
    assert result.email_status == "sent"

    # The file exists, is private, and is bounded.
    path = config.status_snapshot_path()
    assert path.is_file()
    assert path.stat().st_size < config.STATUS_SNAPSHOT_MAX_BYTES

    document = status_snapshot.read()
    latest = document["recent_runs"][0]
    assert latest["status"] == "success"
    assert latest["email_status"] == "sent"
    assert latest["target_date"] == TARGET_DATE
    assert latest["completed_at"] is not None
    # The delivery transition is in it too, not inferred from the run.
    assert document["latest_delivery"]["state"] == "sent"
    assert document["last_confirmed_delivery"]["state"] == "sent"


def test_the_snapshot_from_a_real_run_reproduces_the_database_document(
    pipeline, settings_obj, monkeypatch
):
    daily.run_daily(target_date=TARGET_DATE, trigger="timer", settings=settings_obj)
    from_db = health.build_status("database")

    def cantopen(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "connect_readonly", cantopen)
    from_snapshot = health.build_status("auto")

    assert from_snapshot["status_data_source"] == "snapshot"
    assert from_db["recent_runs"] == from_snapshot["recent_runs"]
    assert from_db["health"] == from_snapshot["health"] == "healthy"
    assert (
        from_db["metrics"]["latest_success_target_date"]
        == from_snapshot["metrics"]["latest_success_target_date"]
    )
    # The live failure is still reported even though the document is complete --
    # and because the document *is* complete, it is reported as a probe failure
    # rather than as incompleteness.
    assert (
        from_snapshot["status_database_probe_error"] == "unable to open database file"
    )
    assert from_snapshot["adapter_errors"] == []
