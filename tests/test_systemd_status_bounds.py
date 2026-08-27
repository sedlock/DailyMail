"""The status inspection path must not hang an operator health command."""

from __future__ import annotations

import subprocess

from dailymail import systemd_units


def test_status_uses_short_per_command_and_total_deadlines(monkeypatch):
    calls: list[float] = []
    clock = iter([0.0, 0.0, 2.0, 4.0, 6.0, 6.0, 6.0, 6.0, 6.0])

    def fake_systemctl(*args, timeout):
        calls.append(timeout)
        return subprocess.CompletedProcess(args, 0, "enabled\n", "")

    monkeypatch.setattr(systemd_units, "_systemctl", fake_systemctl)
    monkeypatch.setattr(systemd_units, "_loginctl", lambda *args, **kwargs: None)
    monkeypatch.setattr(systemd_units.time, "monotonic", lambda: next(clock))

    observed = systemd_units.status()

    assert len(calls) == 3
    assert all(
        timeout <= systemd_units.STATUS_COMMAND_TIMEOUT_SECONDS for timeout in calls
    )
    assert observed["last_result"] == ""


def test_bounded_subprocess_returns_timeout_result(monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])

    monkeypatch.setattr(systemd_units.subprocess, "run", timeout)

    result = systemd_units._systemctl("show", "dailymail.service", timeout=0.1)

    assert result.returncode == 124
    assert "timed out" in result.stderr
