"""Generation and installation of the user-level service and timer.

No secrets go in a unit file. The credential stays in
`~/.config/dailymail/credentials.env` and is read by the process itself.

Absolute executable paths are used because a systemd user unit gets a minimal
PATH and would not otherwise find `uv` or `claude`.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "dailymail.service"
TIMER_NAME = "dailymail.timer"

SERVICE_TEMPLATE = """\
[Unit]
Description=DailyMail: curated Rowan Announcer digest
Documentation=file://{working_dir}/README.md
# Only meaningful when the machine is actually online.
Wants=network-online.target
After=network-online.target

[Service]
Type=oneshot
WorkingDirectory={working_dir}
# systemd gives user units a minimal PATH; uv and claude live outside it.
Environment=PATH={path}
Environment=PYTHONUNBUFFERED=1
# No credentials here by design. The SMTP App Password is read at runtime from
# {credentials_path} (mode 0600) inside the sending process only.
ExecStart={uv} run --frozen dailymail run-daily --trigger timer
# A single run is bounded; if it wedges, fail rather than hang forever.
TimeoutStartSec=1800
Nice=10
IOSchedulingClass=idle
# Modest hardening. The app needs $HOME for config, state and the Claude CLI's
# own credentials, so ProtectHome is deliberately not enabled.
NoNewPrivileges=yes
PrivateTmp=yes
ProtectKernelTunables=yes
ProtectControlGroups=yes
RestrictSUIDSGID=yes

[Install]
WantedBy=default.target
"""

TIMER_TEMPLATE = """\
[Unit]
Description=DailyMail: run the curated Rowan digest daily at {send_time} {timezone}

[Timer]
# Explicit timezone so the schedule follows Eastern time, including DST,
# regardless of the host's local timezone.
OnCalendar={calendar}
# Run a missed occurrence once the user manager is available again, so a reboot
# or an outage does not silently skip a day.
Persistent=true
AccuracySec=1min
RandomizedDelaySec=60
Unit={service}

[Install]
WantedBy=timers.target
"""


@dataclass
class UnitPlan:
    service_path: Path
    timer_path: Path
    service_text: str
    timer_text: str


def unit_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path.home() / ".config"
    return root / "systemd" / "user"


def _which(name: str, fallback: str) -> str:
    return shutil.which(name) or fallback


def build_plan(
    *,
    working_dir: Path,
    send_time: str,
    timezone: str,
    credentials_path: Path,
    uv_path: str | None = None,
    extra_path_dirs: list[str] | None = None,
    directory: Path | None = None,
) -> UnitPlan:
    uv = uv_path or _which("uv", "/usr/bin/uv")
    claude = _which("claude", "")

    path_dirs: list[str] = []
    for candidate in [str(Path(uv).parent)] + (extra_path_dirs or []):
        if candidate and candidate not in path_dirs:
            path_dirs.append(candidate)
    if claude:
        claude_dir = str(Path(claude).parent)
        if claude_dir not in path_dirs:
            path_dirs.append(claude_dir)
    for standard in ("/usr/local/bin", "/usr/bin", "/bin"):
        if standard not in path_dirs:
            path_dirs.append(standard)

    hour, _, minute = send_time.partition(":")
    calendar = f"*-*-* {int(hour):02d}:{int(minute or 0):02d}:00 {timezone}"

    target = directory or unit_dir()
    return UnitPlan(
        service_path=target / SERVICE_NAME,
        timer_path=target / TIMER_NAME,
        service_text=SERVICE_TEMPLATE.format(
            working_dir=working_dir,
            path=":".join(path_dirs),
            uv=uv,
            credentials_path=credentials_path,
        ),
        timer_text=TIMER_TEMPLATE.format(
            calendar=calendar,
            send_time=send_time,
            timezone=timezone,
            service=SERVICE_NAME,
        ),
    )


def write_units(plan: UnitPlan) -> None:
    plan.service_path.parent.mkdir(parents=True, exist_ok=True)
    plan.service_path.write_text(plan.service_text, encoding="utf-8")
    plan.timer_path.write_text(plan.timer_text, encoding="utf-8")


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["systemctl", "--user", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def install(plan: UnitPlan, *, enable: bool = True) -> dict:
    """Write units, reload, and enable the timer."""
    write_units(plan)
    results = {
        "service_path": str(plan.service_path),
        "timer_path": str(plan.timer_path),
    }
    reload_result = _systemctl("daemon-reload")
    results["daemon_reload"] = reload_result.returncode == 0
    if reload_result.returncode != 0:
        results["daemon_reload_error"] = reload_result.stderr.strip()[:300]

    if enable:
        enable_result = _systemctl("enable", "--now", TIMER_NAME)
        results["enabled"] = enable_result.returncode == 0
        if enable_result.returncode != 0:
            results["enable_error"] = enable_result.stderr.strip()[:300]
    return results


def linger_enabled(user: str | None = None) -> bool:
    """Whether the user manager runs while logged out (reboot durability)."""
    name = user or os.environ.get("USER") or ""
    result = subprocess.run(
        ["loginctl", "show-user", name, "-p", "Linger"],
        capture_output=True, text=True, check=False,
    )
    return "Linger=yes" in result.stdout


def enable_linger(user: str | None = None) -> tuple[bool, str]:
    """Attempt to enable lingering. Returns (succeeded, detail)."""
    name = user or os.environ.get("USER") or ""
    if linger_enabled(name):
        return True, "already enabled"
    result = subprocess.run(
        ["loginctl", "enable-linger", name],
        capture_output=True, text=True, check=False,
    )
    if result.returncode == 0 and linger_enabled(name):
        return True, "enabled"
    return False, (result.stderr or result.stdout or "unknown failure").strip()[:300]


def status() -> dict:
    """Operational view of the installed units."""
    timer_enabled = _systemctl("is-enabled", TIMER_NAME).stdout.strip()
    timer_active = _systemctl("is-active", TIMER_NAME).stdout.strip()
    service_load = _systemctl("show", SERVICE_NAME, "-p", "LoadState", "--value")
    listing = _systemctl(
        "list-timers", TIMER_NAME, "--all", "--no-pager", "--no-legend"
    ).stdout.strip()
    next_run = _systemctl(
        "show", TIMER_NAME, "-p", "NextElapseUSecRealtime", "--value"
    ).stdout.strip()
    last_result = _systemctl(
        "show", SERVICE_NAME, "-p", "Result", "--value"
    ).stdout.strip()
    return {
        "service": SERVICE_NAME,
        "timer": TIMER_NAME,
        "service_load_state": service_load.stdout.strip(),
        "timer_enabled": timer_enabled,
        "timer_active": timer_active,
        "next_elapse": next_run,
        "last_result": last_result,
        "list_timers": listing,
        "linger": linger_enabled(),
    }
