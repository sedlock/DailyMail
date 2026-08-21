"""SMTP credential loading.

Rules enforced here:
  * the file must exist, be a regular file, and be mode 0600
  * the parent directory must not be group- or world-readable
  * contents are never printed, logged, or included in exception messages
  * the value is only ever held in memory inside the sending process
  * incidental spaces are stripped -- Google displays App Passwords in groups
    of four, and users paste them that way
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from .errors import DailyMailError
from .settings import credentials_path


class CredentialError(DailyMailError):
    exit_code = 8


@dataclass(frozen=True)
class SmtpCredentials:
    username: str
    password: str

    def __repr__(self) -> str:  # pragma: no cover - defensive
        return f"SmtpCredentials(username={self.username!r}, password=<redacted>)"

    __str__ = __repr__


def _parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, separator, value = line.partition("=")
        if not separator:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key.strip()] = value
    return values


def check_permissions(path: Path | None = None) -> Path:
    """Validate the credential file's location and mode. Reads nothing."""
    target = path or credentials_path()
    if not target.exists():
        raise CredentialError(
            f"credential file not found at {target}. Create it with "
            "GMAIL_SMTP_USER and GMAIL_APP_PASSWORD, mode 0600."
        )
    if not target.is_file():
        raise CredentialError(f"{target} is not a regular file")

    file_mode = stat.S_IMODE(target.stat().st_mode)
    if file_mode != 0o600:
        raise CredentialError(
            f"{target} has mode {file_mode:04o}; refusing to load. "
            f"Fix with: chmod 600 {target}"
        )

    dir_mode = stat.S_IMODE(target.parent.stat().st_mode)
    if dir_mode & (stat.S_IRGRP | stat.S_IROTH | stat.S_IWGRP | stat.S_IWOTH):
        raise CredentialError(
            f"{target.parent} has mode {dir_mode:04o}; it must not be readable or "
            f"writable by group or others. Fix with: chmod 700 {target.parent}"
        )
    return target


def load(path: Path | None = None) -> SmtpCredentials:
    """Load credentials. Call this only inside the sending process."""
    target = check_permissions(path)
    values = _parse_env(target.read_text(encoding="utf-8"))

    username = (values.get("GMAIL_SMTP_USER") or "").strip()
    # Google shows App Passwords grouped with spaces; strip all whitespace.
    raw_password = values.get("GMAIL_APP_PASSWORD") or ""
    password = "".join(raw_password.split())

    missing = [
        name
        for name, value in (
            ("GMAIL_SMTP_USER", username),
            ("GMAIL_APP_PASSWORD", password),
        )
        if not value
    ]
    if missing:
        raise CredentialError(
            f"{target} is missing or has empty values for: {', '.join(missing)}"
        )
    if "@" not in username:
        raise CredentialError(
            "GMAIL_SMTP_USER does not look like an email address"
        )
    return SmtpCredentials(username=username, password=password)


def scrub(text: str, credentials: SmtpCredentials | None) -> str:
    """Belt-and-braces filter for anything about to be logged."""
    if not credentials:
        return text
    for secret in (credentials.password, credentials.password.lower()):
        if secret and secret in text:
            text = text.replace(secret, "<redacted>")
    return text


def environ_without_secrets() -> dict[str, str]:
    """Environment for subprocesses (e.g. Claude) with credentials removed."""
    blocked = {"GMAIL_APP_PASSWORD", "GMAIL_SMTP_USER"}
    return {k: v for k, v in os.environ.items() if k not in blocked}
