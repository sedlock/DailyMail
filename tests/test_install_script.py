"""Installer contract tests with fake local executables; no production state."""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[1]
INSTALLER = REPOSITORY / "scripts" / "install.sh"


def _executable(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _environment(tmp_path: Path) -> tuple[dict[str, str], Path]:
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    call_log = tmp_path / "calls.log"
    _executable(
        fake_bin / "uv",
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'uv %s\\n\' "$*" >> "$CALL_LOG"\n'
        "if [[ \"$*\" == *'dailymail health --json'* ]]; then\n"
        "  printf '%s\\n' '{\"schema_version\":\"controlpanel.status.v1\"}'\n"
        "fi\n",
    )
    _executable(
        fake_bin / "systemctl",
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'printf \'systemctl %s\\n\' "$*" >> "$CALL_LOG"\n'
        "printf 'fake systemd status\\n'\n",
    )
    environment = {
        **os.environ,
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "CALL_LOG": str(call_log),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_STATE_HOME": str(tmp_path / "state"),
    }
    return environment, call_log


def _run(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], str]:
    environment, call_log = _environment(tmp_path)
    result = subprocess.run(
        [str(INSTALLER), *args],
        cwd=REPOSITORY,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )
    return result, call_log.read_text(encoding="utf-8") if call_log.exists() else ""


def test_install_uses_frozen_sync_and_never_starts_daily_work(tmp_path):
    result, calls = _run(tmp_path)

    assert result.returncode == 0, result.stderr
    assert "uv sync --frozen" in calls
    assert "uv run --frozen dailymail install-timer --no-enable" in calls
    assert "run-daily" not in calls
    assert " collect" not in calls
    assert " send" not in calls
    assert "not started by this script" in result.stdout


def test_status_is_nosync_health_and_exact_unit_status(tmp_path):
    result, calls = _run(tmp_path, "--status")

    assert result.returncode == 0, result.stderr
    assert "uv run --frozen --no-sync dailymail health --json" in calls
    assert (
        "systemctl --user status dailymail.service dailymail.timer --no-pager --full"
        in calls
    )
    assert "dailymail health --json" not in result.stderr
    assert not (tmp_path / "data" / "dailymail.sqlite3").exists()


def test_remove_only_removes_units_and_preserves_state_and_credentials(tmp_path):
    environment, call_log = _environment(tmp_path)
    unit_dir = Path(environment["XDG_CONFIG_HOME"]) / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    service = unit_dir / "dailymail.service"
    timer = unit_dir / "dailymail.timer"
    service.write_text("service", encoding="utf-8")
    timer.write_text("timer", encoding="utf-8")
    database = Path(environment["XDG_DATA_HOME"]) / "dailymail" / "dailymail.sqlite3"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"sqlite-state")
    credential = Path(environment["XDG_CONFIG_HOME"]) / "dailymail" / "credentials.env"
    credential.parent.mkdir(parents=True)
    credential.write_text("test-credential-material\n", encoding="utf-8")

    result = subprocess.run(
        [str(INSTALLER), "--remove"],
        cwd=REPOSITORY,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert not service.exists()
    assert not timer.exists()
    assert database.read_bytes() == b"sqlite-state"
    assert credential.read_text(encoding="utf-8") == "test-credential-material\n"
    calls = call_log.read_text(encoding="utf-8")
    assert "systemctl --user disable --now dailymail.timer" in calls
    assert "systemctl --user daemon-reload" in calls
    assert "dailymail.service stop" not in calls
