"""ControlPanel health contract tests; all state is isolated by conftest."""

from __future__ import annotations

import json
import sqlite3

import pytest

from dailymail import cli, db, health, systemd_units


@pytest.fixture
def timer_status(monkeypatch):
    state = {
        "service": "dailymail.service",
        "timer": "dailymail.timer",
        "service_load_state": "loaded",
        "timer_enabled": "enabled",
        "timer_active": "active",
        "next_elapse": "Thu 2026-08-27 06:30:00 EDT",
        "last_result": "success",
        "list_timers": "",
        "linger": True,
    }
    monkeypatch.setattr(systemd_units, "status", lambda: state)
    return state


def _record_run(connection, *, status="success", email_status="sent", error=None):
    run_id = db.start_run(connection, "2026-08-20", trigger="timer")
    db.finish_run(
        connection,
        run_id,
        status=status,
        collector_validation="ok" if status == "success" else "failed",
        email_status=email_status,
        error_summary=error,
        unique_count=13,
        new_count=5,
        standing_count=8,
        changed_count=1,
        employee_count=13,
        student_count=3,
        parking_stats=json.dumps({"cache_hits": 2, "cache_misses": 1}),
    )


def test_health_json_has_stable_components_and_metrics(
    populated_db, timer_status, capsys
):
    _record_run(populated_db)

    assert cli.main(["health", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["schema_version"] == "controlpanel.status.v1"
    assert payload["project"] == "dailymail"
    assert payload["health"] == "healthy"
    assert payload["summary"] == "Latest retrieval succeeded"
    assert [component["id"] for component in payload["components"]] == [
        "daily-retrieval",
        "daily-digest",
    ]
    for component in payload["components"]:
        assert {"id", "name", "health", "summary"} <= component.keys()
        assert component["health"] == "healthy"
        assert component["actions"] == ["run", "enable", "disable", "refresh"]
        assert isinstance(component["evidence"], list)
        assert isinstance(component["freshness_seconds"], int)
    assert payload["metrics"]["latest_success_announcements_unique"] == 13
    assert payload["metrics"]["latest_success_announcements_new"] == 5
    assert payload["metrics"]["latest_run_parking_cache_hits"] == 2
    assert all(
        value is None or isinstance(value, (int, float, str))
        for value in payload["metrics"].values()
    )
    run = payload["recent_runs"][0]
    assert set(run) == {"started_at", "finished_at", "success", "summary", "metrics"}
    assert run["success"] is True
    assert run["metrics"]["duration_seconds"] is not None
    assert all(
        value is None or isinstance(value, (int, float, str))
        for value in run["metrics"].values()
    )


def test_status_json_alias_and_error_redaction(populated_db, timer_status, capsys):
    _record_run(
        populated_db,
        status="failed",
        email_status="failed",
        error="SMTP password=opensesame for mark@example.test was refused",
    )

    assert cli.main(["status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["overall"]["health"] == "failed"
    rendered = json.dumps(payload)
    assert "opensesame" not in rendered
    assert "mark@example.test" not in rendered
    assert "[redacted]" in rendered
    assert "[redacted-email]" in rendered


def test_health_redacts_bearer_tokens_and_uri_userinfo():
    text = health._safe_text(
        "Authorization: Bearer abc.def-123 token=other "
        "postgres://operator:password@db.example.test/app"
    )
    assert text is not None
    assert "abc.def-123" not in text
    assert "other" not in text
    assert "operator:password" not in text
    assert "Bearer [redacted]" in text
    assert "postgres://[redacted]@" in text


def test_dry_run_is_paused_not_digest_delivery_success(
    populated_db, timer_status, capsys
):
    _record_run(populated_db, status="success", email_status="dry_run")

    assert cli.main(["health", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    digest = next(
        item for item in payload["components"] if item["id"] == "daily-digest"
    )
    assert digest["health"] == "paused"
    assert digest["last_success"] is None
    assert payload["recent_runs"][0]["success"] is None
    assert "no digest was sent" in payload["recent_runs"][0]["summary"]


def test_health_without_database_is_unknown_and_does_not_create_one(
    timer_status, capsys
):
    path = db.database_path()
    assert not path.exists()

    assert cli.main(["health", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert payload["overall"]["health"] == "unknown"
    assert payload["problems"][0]["severity"] == "unknown"
    assert not path.exists(), "health must not bootstrap a database"


def test_readonly_connection_refuses_mutation(populated_db):
    populated_db.close()
    connection = db.connect_readonly()
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM runs")
    finally:
        connection.close()
