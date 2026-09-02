"""The test suite's outer boundary: what must never be reached, ever.

Every other protection in `conftest` is a *patch* -- an XDG environment
variable, a stubbed collector, a blocked parking fetch. Patches are the right
tool for shaping what a test sees, and they are the wrong tool for guaranteeing
what a test cannot do, because anything that installs a patch can be undone.
That is not hypothetical: `test_a_calendar_failure_never_costs_the_digest` used
to call `monkeypatch.undo()` to drop one patch it had installed itself, and
because a `monkeypatch` instance is shared by every fixture in a test's scope,
the call also dropped the temporary XDG directories, the stubbed collector, the
stubbed SMTP send and the parking/route network guards. The next line of that
test ran `run_daily` against the operator's real state directory, the real
Rowan endpoint and the real Gmail credentials.

So the boundary is a CPython audit hook instead. An audit hook cannot be
removed, replaced, shadowed or monkeypatched away once installed: there is no
API to uninstall one. That makes "the normal suite touches nothing real" a
property of the *process* rather than a property of every individual test
remembering to be careful, and it means a future test that reintroduces the
same mistake fails loudly on the attempt instead of quietly succeeding against
production.

Refused, for every test outside the two documented opt-in live modules:

  * DNS resolution or a socket connection to anything but loopback -- which is
    what reaching `apps.rowan.edu` or `smtp.gmail.com` requires;
  * any SMTP conversation at all;
  * opening anything under the real `~/.config/dailymail`,
    `~/.local/share/dailymail` or `~/.local/state/dailymail`, which covers the
    production database, the run lock, the collection artifacts and the Gmail
    App Password;
  * `sqlite3.connect` against the production database;
  * invoking `systemctl`/`loginctl`/`systemd-run`, or the production release
    entrypoint;
  * mutating anything under `/mnt/bench/releases/dailymail`.

Nothing here ever records a credential *value*: a refusal carries the path or
the host and port, and never the bytes behind them.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path

# Resolved once, at import, from the real environment -- before any fixture has
# had the chance to redirect `XDG_*`. A test that unsets those variables gets
# the operator's real directories back, which is exactly what has to be caught.
#
# `Path.home()` rather than a resolved path, because that is the expression
# `settings.config_dir`, `settings.data_dir` and `config.state_dir` themselves
# fall back to; deriving these any other way would let the two disagree about
# what "production" means on a machine with a symlinked home.
REAL_HOME = Path.home()

PRODUCTION_CONFIG_DIR = REAL_HOME / ".config" / "dailymail"
PRODUCTION_DATA_DIR = REAL_HOME / ".local" / "share" / "dailymail"
PRODUCTION_STATE_DIR = REAL_HOME / ".local" / "state" / "dailymail"
PRODUCTION_CREDENTIALS = PRODUCTION_CONFIG_DIR / "credentials.env"
PRODUCTION_DATABASE = PRODUCTION_DATA_DIR / "dailymail.sqlite3"


def _watched_prefixes() -> tuple[str, ...]:
    """Both the literal and the fully resolved form of each directory.

    Code that resolves a path before opening it would otherwise slip past a
    prefix match on the unresolved one, and vice versa.
    """
    directories = [PRODUCTION_CONFIG_DIR, PRODUCTION_DATA_DIR, PRODUCTION_STATE_DIR]
    prefixes = {str(directory) for directory in directories}
    for directory in directories:
        try:
            prefixes.add(str(directory.resolve()))
        except OSError:  # pragma: no cover - an unresolvable home is still watched
            pass
    return tuple(sorted(prefixes))


PRODUCTION_STATE_PATHS: tuple[str, ...] = _watched_prefixes()

PRODUCTION_RELEASE_ROOT = "/mnt/bench/releases/dailymail"

CREDENTIAL_FILENAME = "credentials.env"

# The two modules documented as opt-in probes against live services. They exist
# precisely to make real requests, and they skip themselves unless their own
# environment flag is set (`DAILYMAIL_LIVE_PARKING`, `DAILYMAIL_VISUAL_QA`).
OPT_IN_LIVE_MODULES = frozenset({"test_parking_live.py", "test_visual_qa.py"})

LOOPBACK_HOSTS = frozenset({"localhost", "localhost.localdomain", "ip6-localhost"})
LOOPBACK_PREFIXES = ("127.", "::1", "0.0.0.0", "fe80::1")

SYSTEMD_BINARIES = ("systemctl", "loginctl", "systemd-run", "journalctl")


class HermeticBoundaryViolation(AssertionError):
    """Raised *instead of* a real external operation, never after one."""


# Refusals nothing asked for. This list staying empty is the suite's proof that
# it made no real network call, spoke no SMTP and read no production state.
VIOLATIONS: list[dict[str, str]] = []

# Refusals a test deliberately provoked to prove the boundary is live. Recorded
# separately so probing the guard does not look like breaching it.
PROBES: list[dict[str, str]] = []

_installed = False
_relaxed = 0
_expecting: list[frozenset[str]] = []
_current_test = "<session>"
_recording = False


# --- classification ----------------------------------------------------------


def _is_loopback(host: object) -> bool:
    if not isinstance(host, (str, bytes)):
        return False
    text = os.fsdecode(host) if isinstance(host, bytes) else host
    text = text.strip("[]").lower()
    return text in LOOPBACK_HOSTS or text.startswith(LOOPBACK_PREFIXES)


def _production_state_hit(text: str) -> str | None:
    for prefix in PRODUCTION_STATE_PATHS:
        if text == prefix or text.startswith(prefix + "/"):
            return prefix
    return None


def _decode(value: object) -> str | None:
    if isinstance(value, (str, bytes, os.PathLike)):
        try:
            return os.fsdecode(value)
        except (UnicodeDecodeError, TypeError, ValueError):
            return None
    return None


def _writes(mode: object, flags: object) -> bool:
    if isinstance(mode, str) and any(character in mode for character in "wxa+"):
        return True
    if isinstance(flags, int):
        writable = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(flags & writable)
    return False


# --- the hook ----------------------------------------------------------------


def _refuse(kind: str, detail: str) -> None:
    """Record the attempt and raise before the operation can happen."""
    global _recording
    entry = {"kind": kind, "detail": detail, "test": _current_test}
    if _recording:  # pragma: no cover - defensive against nested audit events
        raise HermeticBoundaryViolation(f"{kind}: {detail}")
    try:
        _recording = True
        expected = _expecting and kind in _expecting[-1]
        (PROBES if expected else VIOLATIONS).append(entry)
    finally:
        _recording = False
    raise HermeticBoundaryViolation(
        f"the hermetic test boundary refused {kind}: {detail}. "
        "Inject the dependency instead of reaching the real one; if this is a "
        "deliberate live probe it belongs in an opt-in module."
    )


def _refuse_mutation(event: str, path: str | None) -> None:
    if not path:
        return
    if path.startswith(PRODUCTION_RELEASE_ROOT):
        _refuse("release-mutation", f"{event} {path}")
    if _production_state_hit(path) is not None:
        _refuse("production-path", f"{event} {path}")


def _hook(event: str, arguments: tuple) -> None:
    if _relaxed:
        return

    if event == "socket.getaddrinfo":
        if not _is_loopback(arguments[0]):
            _refuse("dns", f"resolve {arguments[0]!r}:{arguments[1]!r}")
        return

    if event == "socket.connect":
        address = arguments[1]
        host = address[0] if isinstance(address, tuple) and address else address
        # An `AF_UNIX` address is a filesystem path, not an external endpoint.
        if isinstance(address, tuple) and not _is_loopback(host):
            _refuse("socket", f"connect to {address!r}")
        return

    if event in ("socket.gethostbyname", "socket.gethostbyaddr"):
        if not _is_loopback(arguments[0]):
            _refuse("dns", f"{event} {arguments[0]!r}")
        return

    if event == "smtplib.connect":
        _refuse("smtp", f"SMTP connect to {arguments[1]!r}:{arguments[2]!r}")
        return

    if event == "smtplib.send":
        _refuse("smtp", "SMTP payload write")
        return

    if event == "open":
        path = _decode(arguments[0])
        if path is None or not path.startswith("/"):
            return
        hit = _production_state_hit(path)
        if hit is not None:
            kind = (
                "credential"
                if os.path.basename(path) == CREDENTIAL_FILENAME
                else "production-path"
            )
            _refuse(kind, f"open {path}")
        if path.startswith(PRODUCTION_RELEASE_ROOT) and _writes(
            arguments[1] if len(arguments) > 1 else None,
            arguments[2] if len(arguments) > 2 else None,
        ):
            _refuse("release-mutation", f"open for write {path}")
        return

    if event == "sqlite3.connect":
        path = _decode(arguments[0])
        if path and _production_state_hit(path) is not None:
            _refuse("production-sqlite", f"sqlite3.connect {path}")
        return

    # Metadata operations never go through `open`, so a rename, an unlink or a
    # `mkdir` would otherwise be an unguarded way to change production.
    if event in ("os.rename", "os.link", "os.symlink"):
        for candidate in arguments[:2]:
            _refuse_mutation(event, _decode(candidate))
        return

    if event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.truncate",
                 "os.utime"):
        _refuse_mutation(event, _decode(arguments[0]))
        return

    if event == "subprocess.Popen":
        argv = arguments[1] if len(arguments) > 1 else ()
        parts = [text for text in (_decode(item) for item in argv or ()) if text]
        rendered = " ".join(parts)
        if any(os.path.basename(parts[0] if parts else "") == binary
               for binary in SYSTEMD_BINARIES) or any(
            binary in rendered for binary in SYSTEMD_BINARIES
        ):
            _refuse("systemd", f"subprocess {rendered[:160]}")
        if PRODUCTION_RELEASE_ROOT in rendered:
            _refuse("production-subprocess", f"subprocess {rendered[:160]}")
        return


def install() -> None:
    """Install the boundary once. There is deliberately no uninstall."""
    global _installed
    if _installed:
        return
    sys.addaudithook(_hook)
    _installed = True


def is_installed() -> bool:
    return _installed


# --- scoping -----------------------------------------------------------------


@contextmanager
def relaxed():
    """Permit real egress: only for the documented opt-in live modules."""
    global _relaxed
    _relaxed += 1
    try:
        yield
    finally:
        _relaxed -= 1


@contextmanager
def expect_blocked(*kinds: str):
    """Provoke the boundary on purpose without it counting as a breach.

    The operation is still refused -- that is the point -- but the refusal is
    filed under `PROBES` rather than `VIOLATIONS`, so a test that proves the
    guard is alive does not make the suite look like it reached production.
    """
    _expecting.append(frozenset(kinds))
    try:
        yield PROBES
    finally:
        _expecting.pop()


def enter_test(nodeid: str) -> None:
    global _current_test
    _current_test = nodeid


def summary() -> dict[str, object]:
    return {
        "installed": _installed,
        "violations": list(VIOLATIONS),
        "probes_blocked": len(PROBES),
        "watched_state_paths": list(PRODUCTION_STATE_PATHS),
        "watched_release_root": PRODUCTION_RELEASE_ROOT,
    }
