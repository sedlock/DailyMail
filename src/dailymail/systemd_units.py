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
import time
from dataclasses import dataclass
from pathlib import Path

SERVICE_NAME = "dailymail.service"
TIMER_NAME = "dailymail.timer"
COMMAND_TIMEOUT_SECONDS = 10.0
STATUS_COMMAND_TIMEOUT_SECONDS = 2.0
STATUS_DEADLINE_SECONDS = 6.0

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
# One second of slack, and deliberately no RandomizedDelaySec: the requested
# time is the delivery time, so jitter that pushes the run materially later is
# not wanted here.
AccuracySec=1s
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


def canonical_project_dir(start: Path) -> Path:
    """Prefer a $HOME-based path over a bind-mount alias for the same directory.

    The repository is reachable both as ~/src/DailyMail and via a mount alias.
    Both are the same inode, but the home path is the one the operator uses and
    the one referenced in documentation, so units should say that.
    """
    start = start.resolve()
    try:
        target = start.stat()
    except OSError:
        return start
    for candidate in (Path.home() / "src" / start.name, Path.home() / start.name):
        try:
            if candidate.stat().st_ino == target.st_ino and (
                candidate.stat().st_dev == target.st_dev
            ):
                return candidate
        except OSError:
            continue
    return start


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

    working_dir = canonical_project_dir(Path(working_dir))
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


def _run_bounded(argv: list[str], *, timeout: float) -> subprocess.CompletedProcess:
    """Run one fixed local command with a bounded, non-throwing result."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            check=False,
            timeout=max(0.1, timeout),
        )
    except subprocess.TimeoutExpired as exc:
        stdout = (
            exc.stdout.decode() if isinstance(exc.stdout, bytes) else exc.stdout or ""
        )
        stderr = (
            exc.stderr.decode() if isinstance(exc.stderr, bytes) else exc.stderr or ""
        )
        return subprocess.CompletedProcess(
            argv, 124, stdout, (stderr + " command timed out").strip()
        )


def _systemctl(
    *args: str, timeout: float = COMMAND_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess:
    return _run_bounded(["systemctl", "--user", *args], timeout=timeout)


def _loginctl(
    *args: str, timeout: float = COMMAND_TIMEOUT_SECONDS
) -> subprocess.CompletedProcess:
    return _run_bounded(["loginctl", *args], timeout=timeout)


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


def linger_enabled(
    user: str | None = None, *, timeout: float = COMMAND_TIMEOUT_SECONDS
) -> bool:
    """Whether the user manager runs while logged out (reboot durability)."""
    name = user or os.environ.get("USER") or ""
    result = _loginctl("show-user", name, "-p", "Linger", timeout=timeout)
    return "Linger=yes" in result.stdout


def enable_linger(user: str | None = None) -> tuple[bool, str]:
    """Attempt to enable lingering. Returns (succeeded, detail)."""
    name = user or os.environ.get("USER") or ""
    if linger_enabled(name):
        return True, "already enabled"
    result = _loginctl("enable-linger", name)
    if result.returncode == 0 and linger_enabled(name):
        return True, "enabled"
    return False, (result.stderr or result.stdout or "unknown failure").strip()[:300]


def status() -> dict:
    """Operational view of the installed units within one short deadline."""
    deadline = time.monotonic() + STATUS_DEADLINE_SECONDS

    def run_status(*args: str) -> subprocess.CompletedProcess:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return subprocess.CompletedProcess(
                ["systemctl", "--user", *args], 124, "", "status deadline exceeded"
            )
        return _systemctl(*args, timeout=min(STATUS_COMMAND_TIMEOUT_SECONDS, remaining))

    timer_enabled = run_status("is-enabled", TIMER_NAME).stdout.strip()
    timer_active = run_status("is-active", TIMER_NAME).stdout.strip()
    service_load = run_status("show", SERVICE_NAME, "-p", "LoadState", "--value")
    listing = run_status(
        "list-timers", TIMER_NAME, "--all", "--no-pager", "--no-legend"
    ).stdout.strip()
    next_run = run_status(
        "show", TIMER_NAME, "-p", "NextElapseUSecRealtime", "--value"
    ).stdout.strip()
    last_result = run_status(
        "show", SERVICE_NAME, "-p", "Result", "--value"
    ).stdout.strip()
    remaining = deadline - time.monotonic()
    linger = (
        linger_enabled(timeout=min(STATUS_COMMAND_TIMEOUT_SECONDS, remaining))
        if remaining > 0
        else False
    )
    return {
        "service": SERVICE_NAME,
        "timer": TIMER_NAME,
        "service_load_state": service_load.stdout.strip(),
        "timer_enabled": timer_enabled,
        "timer_active": timer_active,
        "next_elapse": next_run,
        "last_result": last_result,
        "list_timers": listing,
        "linger": linger,
    }
