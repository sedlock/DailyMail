"""Typed failures with distinct exit codes.

Every message here is written on the assumption that it may be logged. None of
these exceptions may carry a raw Rowan API response body: Phase 1 policy is that
unredacted upstream payloads never reach disk, logs, or diagnostics.
"""

from __future__ import annotations


class DailyMailError(Exception):
    """Base class. `exit_code` is what the CLI returns."""

    exit_code = 1


class UsageError(DailyMailError):
    """Bad CLI input (e.g. a malformed --date)."""

    exit_code = 2


class TlsError(DailyMailError):
    """TLS verification or handshake failure — deliberately distinct from HTTP."""

    exit_code = 3


class DiscoveryError(DailyMailError):
    """Could not discover the OutSystems runtime version tokens."""

    exit_code = 4


class TransportError(DailyMailError):
    """HTTP-level failure: non-200, wrong content type, unparseable body."""

    exit_code = 5


class ValidationError(DailyMailError):
    """A mandatory validation gate failed. Never treat as an empty day."""

    exit_code = 6


class VersionChangedError(DailyMailError):
    """`hasApiVersionChanged` still true after rediscovery and one retry.

    This is the silent-failure mode from Phase 0 §2.3: HTTP 200 with `data: {}`.
    It must never be interpreted as "no announcements today".
    """

    exit_code = 7
