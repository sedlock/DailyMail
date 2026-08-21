"""The daily pipeline's failure and idempotency behaviour, plus ops plumbing."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from dailymail import daily, db, maintenance, mailer, systemd_units
from dailymail.daily import RunLock, collect_with_retry
from dailymail.errors import TlsError, TransportError, ValidationError, VersionChangedError

from conftest import TARGET_DATE, artifact_from_fixtures


# --- overlap lock ------------------------------------------------------------


def test_lock_prevents_overlapping_runs():
    with RunLock():
        with pytest.raises(daily.LockHeld):
            with RunLock():
                pass


def test_lock_is_released_after_use():
    with RunLock():
        pass
    with RunLock():  # must not raise
        pass


# --- collection retry policy -------------------------------------------------


def test_transient_failures_are_retried_then_succeed(settings_obj, monkeypatch):
    calls = {"n": 0}
    slept: list[int] = []

    def flaky(*, target_date, page_size):
        calls["n"] += 1
        if calls["n"] < 3:
            raise TransportError("connection reset")
        return {"target_date": target_date, "ok": True}

    monkeypatch.setattr(daily.collector, "run_collection", flaky)
    monkeypatch.setattr(daily.collector, "write_artifact", lambda artifact: Path("/dev/null"))

    artifact, attempts = collect_with_retry(
        TARGET_DATE, settings_obj, sleeper=slept.append
    )
    assert artifact["ok"] is True
    assert attempts == 3
    assert slept == list(settings_obj.retry_delays_seconds)


def test_retry_schedule_matches_configuration(settings_obj):
    assert settings_obj.retry_delays_seconds == (300, 900)


def test_transient_failure_gives_up_after_the_schedule(settings_obj, monkeypatch):
    def always_fails(*, target_date, page_size):
        raise TlsError("certificate verify failed")

    monkeypatch.setattr(daily.collector, "run_collection", always_fails)
    with pytest.raises(TransportError, match="after 3 attempt"):
        collect_with_retry(TARGET_DATE, settings_obj, sleeper=lambda _: None)


def test_validation_failure_is_not_retried(settings_obj, monkeypatch):
    calls = {"n": 0}

    def structural(*, target_date, page_size):
        calls["n"] += 1
        raise ValidationError("V2 count mismatch")

    monkeypatch.setattr(daily.collector, "run_collection", structural)
    with pytest.raises(ValidationError):
        collect_with_retry(TARGET_DATE, settings_obj, sleeper=lambda _: None)
    assert calls["n"] == 1, "a data problem must not be retried"


def test_stale_api_version_is_treated_as_transient(settings_obj, monkeypatch):
    calls = {"n": 0}

    def stale_then_ok(*, target_date, page_size):
        calls["n"] += 1
        if calls["n"] == 1:
            raise VersionChangedError("hasApiVersionChanged")
        return {"target_date": target_date}

    monkeypatch.setattr(daily.collector, "run_collection", stale_then_ok)
    monkeypatch.setattr(daily.collector, "write_artifact", lambda a: Path("/dev/null"))
    _, attempts = collect_with_retry(TARGET_DATE, settings_obj, sleeper=lambda _: None)
    assert attempts == 2


# --- full pipeline, with collection and SMTP stubbed -------------------------


@pytest.fixture
def stub_pipeline(monkeypatch, settings_obj, employee_fixture, student_fixture):
    """Wire run_daily to fixture data and a fake SMTP, returning a call log."""
    sent: list = []
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)

    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: dict(artifact, target_date=target_date),
    )
    monkeypatch.setattr(daily.collector, "write_artifact", lambda a: Path("/dev/null"))
    monkeypatch.setattr(mailer, "sender_address", lambda: "digest-test@gmail.com")
    monkeypatch.setattr(daily.mailer, "sender_address", lambda: "digest-test@gmail.com")

    def fake_send(prepared, settings):
        sent.append(prepared)
        return "accepted (stub)"

    monkeypatch.setattr(daily.mailer, "send", fake_send)
    # Skip the real Claude call; exercise the fallback ordering path.
    monkeypatch.setattr(
        daily.curate, "curate",
        lambda rows, target_date, settings: daily.curate.CurationOutcome(
            method="fallback", model=None,
            entries=daily.curate.fallback_rank(rows, target_date, settings),
            error="stubbed",
        ),
    )
    return sent


def test_full_run_sends_once_and_records_delivery(stub_pipeline, settings_obj):
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "success"
    assert result.email_status == "sent"
    assert len(stub_pipeline) == 1
    assert result.counts["unique"] == 13
    assert result.counts["new"] == 5
    assert result.counts["standing"] == 8
    assert result.subject == "Curated Rowan Daily Mail - August 20 2026"
    assert result.message_id and result.message_bytes

    connection = db.connect()
    row = db.successful_delivery(connection, TARGET_DATE, settings_obj.recipient)
    assert row is not None
    assert row["state"] == "sent"
    assert row["smtp_status"] == "accepted (stub)"
    connection.close()


def test_second_normal_run_does_not_resend(stub_pipeline, settings_obj):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert len(stub_pipeline) == 1
    second = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert second.status == "success"
    assert second.email_status == "skipped_duplicate"
    assert len(stub_pipeline) == 1, "a normal re-run must not send again"


def test_force_resend_sends_again(stub_pipeline, settings_obj):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    result = daily.run_daily(
        target_date=TARGET_DATE, settings=settings_obj, force_resend=True
    )
    assert result.email_status == "sent"
    assert len(stub_pipeline) == 2

    connection = db.connect()
    rows = list(
        connection.execute(
            "SELECT forced FROM deliveries WHERE target_date = ? ORDER BY delivery_id",
            (TARGET_DATE,),
        )
    )
    assert [r["forced"] for r in rows] == [0, 1]
    connection.close()


def test_changed_content_after_send_does_not_auto_resend(stub_pipeline, settings_obj):
    """Recorded for the next normal run, not spammed immediately."""
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    connection = db.connect()
    with db.transaction(connection):
        connection.execute(
            "UPDATE announcement_versions SET full_body = full_body || '<p>more</p>' "
            "WHERE submission_id = 6622"
        )
    connection.close()
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.email_status == "skipped_duplicate"
    assert len(stub_pipeline) == 1


def test_dry_run_renders_without_sending(stub_pipeline, settings_obj):
    result = daily.run_daily(
        target_date=TARGET_DATE, settings=settings_obj, dry_run=True
    )
    assert result.email_status == "dry_run"
    assert stub_pipeline == []
    previews = list(maintenance.diagnostics_dir().glob(f"{TARGET_DATE}-preview.*"))
    assert len(previews) == 2


def test_run_is_recorded_in_the_runs_table(stub_pipeline, settings_obj):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj, trigger="timer")
    connection = db.connect()
    row = connection.execute(
        "SELECT * FROM runs ORDER BY run_id DESC LIMIT 1"
    ).fetchone()
    assert row["status"] == "success"
    assert row["target_date"] == TARGET_DATE
    assert row["trigger"] == "timer"
    assert row["curation_method"] == "fallback"
    assert row["email_status"] == "sent"
    assert row["unique_count"] == 13
    assert row["employee_count"] == 13
    assert row["student_count"] == 3
    assert row["completed_at"]
    connection.close()


def test_curation_results_persisted(stub_pipeline, settings_obj):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    connection = db.connect()
    rows = list(
        connection.execute(
            "SELECT * FROM curation_results WHERE target_date = ?", (TARGET_DATE,)
        )
    )
    assert len(rows) == 13
    assert all(r["method"] == "fallback" for r in rows)
    assert all(r["rationale"] for r in rows)
    assert all(r["section"] in ("New", "Standing") for r in rows)
    connection.close()


def test_collection_failure_sends_alert_and_no_digest(
    monkeypatch, settings_obj, employee_fixture, student_fixture
):
    alerts: list = []

    def failing(*, target_date, page_size):
        raise ValidationError("V2 [Employees]: collected 12 but TotalCount is 13")

    monkeypatch.setattr(daily.collector, "run_collection", failing)
    monkeypatch.setattr(daily.mailer, "sender_address", lambda: "digest-test@gmail.com")
    monkeypatch.setattr(
        daily.mailer, "send", lambda prepared, settings: alerts.append(prepared) or "ok"
    )

    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "failed"
    assert "ValidationError" in result.error
    assert len(alerts) == 1
    assert alerts[0].subject == "DailyMail ATTENTION REQUIRED - August 20 2026"

    connection = db.connect()
    assert db.successful_delivery(connection, TARGET_DATE, settings_obj.recipient) is None
    row = connection.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert row["status"] == "failed"
    assert row["collector_validation"] == "failed"
    connection.close()


def test_smtp_failure_records_and_does_not_alert_by_email(
    monkeypatch, stub_pipeline, settings_obj
):
    """Do not try to report an SMTP outage through SMTP."""
    attempts: list = []

    def broken_send(prepared, settings):
        attempts.append(prepared)
        raise mailer.SmtpError("connection refused")

    monkeypatch.setattr(daily.mailer, "send", broken_send)

    with pytest.raises(mailer.SmtpError):
        daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    assert len(attempts) == 1, "no alert email attempted over the failed channel"
    connection = db.connect()
    row = connection.execute(
        "SELECT * FROM deliveries ORDER BY delivery_id DESC LIMIT 1"
    ).fetchone()
    assert row["state"] == "failed"
    assert "connection refused" in row["error_summary"]
    run = connection.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert run["email_status"] == "failed"
    connection.close()


def test_render_failure_sends_nothing(monkeypatch, stub_pipeline, settings_obj):
    def broken_render(*args, **kwargs):
        raise RuntimeError("template exploded")

    monkeypatch.setattr(daily.render, "render_digest", broken_render)
    monkeypatch.setattr(daily.mailer, "send", lambda p, s: "should not happen")

    with pytest.raises(daily.RenderFailure):
        daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    connection = db.connect()
    assert db.successful_delivery(connection, TARGET_DATE, settings_obj.recipient) is None
    run = connection.execute("SELECT * FROM runs ORDER BY run_id DESC LIMIT 1").fetchone()
    assert run["status"] == "failed"
    assert run["email_status"] == "not_attempted"
    connection.close()


def test_incomplete_render_is_refused(stub_pipeline, settings_obj, monkeypatch):
    """A digest missing an announcement must never be sent."""
    real = daily.render.render_digest

    def truncating(rows, **kwargs):
        digest = real(rows, **kwargs)
        digest.submission_ids = digest.submission_ids[:-1]
        return digest

    monkeypatch.setattr(daily.render, "render_digest", truncating)
    with pytest.raises(daily.RenderFailure, match="does not match stored set"):
        daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    # No digest went out. An operator alert is expected and is not a digest.
    digests = [
        p for p in stub_pipeline
        if p.subject.startswith("Curated Rowan Daily Mail - ")
    ]
    assert digests == []
    assert all(p.subject.startswith("DailyMail ATTENTION REQUIRED") for p in stub_pipeline)

    connection = db.connect()
    assert db.successful_delivery(connection, TARGET_DATE, settings_obj.recipient) is None
    connection.close()


def test_maintenance_runs_after_success(stub_pipeline, settings_obj):
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.maintenance.get("backup")
    assert Path(result.maintenance["backup"]).is_file()
    assert result.maintenance["database_bytes"] > 0


def test_inferred_category_persisted_from_curation(
    monkeypatch, stub_pipeline, settings_obj, employee_fixture, student_fixture
):
    """A category Rowan added gets its estimated slot recorded."""
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    artifact["category_registry"].append(
        {"id": 4242, "title": "Emergency Operations", "rank": 0, "color": "red"}
    )
    record = dict(artifact["announcements"][0])
    record["category_id"] = 4242
    artifact["announcements"][0] = record
    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: dict(artifact, target_date=target_date),
    )
    monkeypatch.setattr(
        daily.curate, "curate",
        lambda rows, target_date, settings: daily.curate.CurationOutcome(
            method="claude", model="m",
            entries=daily.curate.fallback_rank(rows, target_date, settings),
            inferred_categories={"Emergency Operations": 11},
        ),
    )
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    connection = db.connect()
    row = connection.execute(
        "SELECT * FROM categories WHERE category_id = 4242"
    ).fetchone()
    assert row["inferred_priority"] == 11
    assert row["manual_priority"] is None
    locked = connection.execute(
        "SELECT * FROM categories WHERE title = 'Official'"
    ).fetchone()
    assert locked["manual_priority"] == 1
    assert locked["inferred_priority"] is None
    connection.close()


# --- maintenance -------------------------------------------------------------


def test_backup_is_a_valid_database(populated_db):
    path = maintenance.backup_database(populated_db)
    assert path.is_file()
    assert path.stat().st_size > 0
    restored = sqlite3.connect(str(path))
    restored.row_factory = sqlite3.Row
    count = restored.execute("SELECT COUNT(*) FROM announcements").fetchone()[0]
    assert count == 13
    assert restored.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    restored.close()


def test_backup_permissions_are_private(populated_db):
    import stat as stat_module

    path = maintenance.backup_database(populated_db)
    assert stat_module.S_IMODE(path.stat().st_mode) == 0o600


def test_backup_retention_keeps_the_newest(populated_db):
    for index in range(8):
        maintenance.backup_database(populated_db, stamp=f"2026010{index}T000000Z")
    maintenance.prune_backups(keep=5)
    remaining = sorted(maintenance.backups_dir().glob("dailymail-*.sqlite3"))
    assert len(remaining) == 5
    assert remaining[-1].name.endswith("20260107T000000Z.sqlite3")


def test_backup_retention_never_deletes_everything(populated_db):
    maintenance.backup_database(populated_db, stamp="one")
    maintenance.prune_backups(keep=0)
    assert len(list(maintenance.backups_dir().glob("*.sqlite3"))) == 1


def test_diagnostics_retention_removes_old_files(settings_obj):
    import os
    import time

    old = maintenance.save_diagnostic("old.html", "<p>old</p>")
    new = maintenance.save_diagnostic("new.html", "<p>new</p>")
    ancient = time.time() - 60 * 60 * 24 * 45
    os.utime(old, (ancient, ancient))

    removed = maintenance.prune_diagnostics(days=30)
    assert old in removed
    assert not old.exists()
    assert new.exists()


def test_collection_artifact_retention_keeps_a_recent_window(settings_obj):
    import os
    import time

    from dailymail import config as collector_config

    directory = collector_config.collections_dir()
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for day in range(1, 9):
        path = directory / f"2026-06-{day:02d}.json"
        path.write_text("{}", encoding="utf-8")
        paths.append(path)
    ancient = time.time() - 60 * 60 * 24 * 60
    for path in paths[:5]:
        os.utime(path, (ancient, ancient))

    removed = maintenance.prune_collection_artifacts(days=14, keep_minimum=3)
    assert len(removed) == 5
    assert len(list(directory.glob("*.json"))) == 3


def test_artifact_retention_respects_keep_minimum(settings_obj):
    import os
    import time

    from dailymail import config as collector_config

    directory = collector_config.collections_dir()
    directory.mkdir(parents=True, exist_ok=True)
    ancient = time.time() - 60 * 60 * 24 * 90
    for day in range(1, 4):
        path = directory / f"2026-05-{day:02d}.json"
        path.write_text("{}", encoding="utf-8")
        os.utime(path, (ancient, ancient))
    assert maintenance.prune_collection_artifacts(days=14, keep_minimum=3) == []


# --- systemd units -----------------------------------------------------------


@pytest.fixture
def plan(tmp_path):
    return systemd_units.build_plan(
        working_dir=Path("/home/sedlock/src/DailyMail"),
        send_time="07:00",
        timezone="America/New_York",
        credentials_path=Path("/home/sedlock/.config/dailymail/credentials.env"),
        directory=tmp_path / "units",
    )


def test_timer_schedules_seven_am_eastern_persistently(plan):
    assert "OnCalendar=*-*-* 07:00:00 America/New_York" in plan.timer_text
    assert "Persistent=true" in plan.timer_text
    assert "WantedBy=timers.target" in plan.timer_text
    assert "Unit=dailymail.service" in plan.timer_text


def test_service_uses_absolute_paths_and_working_directory(plan):
    assert "WorkingDirectory=/home/sedlock/src/DailyMail" in plan.service_text
    exec_line = next(
        line for line in plan.service_text.splitlines() if line.startswith("ExecStart=")
    )
    binary = exec_line.split("=", 1)[1].split()[0]
    assert binary.startswith("/"), "uv must be referenced absolutely"
    assert "run-daily" in exec_line
    assert "--trigger timer" in exec_line


def test_service_path_includes_uv_and_claude(plan):
    path_line = next(
        line for line in plan.service_text.splitlines()
        if line.startswith("Environment=PATH=")
    )
    import shutil

    for tool in ("uv", "claude"):
        location = shutil.which(tool)
        if location:
            assert str(Path(location).parent) in path_line, tool


def test_units_contain_no_secrets(plan, fake_credentials):
    combined = plan.service_text + plan.timer_text
    assert "abcdefghijklmnop" not in combined
    assert "GMAIL_APP_PASSWORD=" not in combined
    # It may *mention* the path, which is how an operator finds it.
    assert "credentials.env" in plan.service_text


def test_service_is_oneshot_with_a_timeout(plan):
    assert "Type=oneshot" in plan.service_text
    assert "TimeoutStartSec=" in plan.service_text


def test_write_units_creates_both_files(plan):
    systemd_units.write_units(plan)
    assert plan.service_path.is_file()
    assert plan.timer_path.is_file()
    assert plan.service_path.read_text() == plan.service_text


def test_calendar_expression_is_valid_for_systemd(plan):
    """Validate with systemd itself rather than trusting the string."""
    import shutil
    import subprocess

    if not shutil.which("systemd-analyze"):
        pytest.skip("systemd-analyze unavailable")
    calendar = next(
        line.split("=", 1)[1]
        for line in plan.timer_text.splitlines()
        if line.startswith("OnCalendar=")
    )
    result = subprocess.run(
        ["systemd-analyze", "calendar", calendar],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "Next elapse" in result.stdout


def test_build_plan_honours_configured_send_time(tmp_path):
    plan = systemd_units.build_plan(
        working_dir=Path("/x"), send_time="6:05", timezone="America/New_York",
        credentials_path=Path("/y"), directory=tmp_path,
    )
    assert "OnCalendar=*-*-* 06:05:00 America/New_York" in plan.timer_text
