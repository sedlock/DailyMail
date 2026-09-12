"""One implementation of "this text may be published".

`health` has scrubbed error text since it first published the status contract:
URI userinfo, bearer tokens, `password=`-style assignments and email addresses
are removed, and the result is bounded. That was enough while `health` was the
only thing that wrote anything outward.

It is not enough now. The status snapshot is a *file on disk* built from the same
`error_summary` columns, and it is read back by `dailymail status-snapshot show`,
so text that used to be scrubbed on the way out of `health` would have reached
a terminal, an operator's scrollback and a log unscrubbed. The reachability is
real rather than theoretical: `mailer.send` raises `recipients refused: [...]`
carrying the recipient address, and `daily.py` writes `str(exc)` of that into
both `runs.error_summary` and `deliveries.error_summary`.

So the rules live here, and both the writer and the reader use them.
"""

from __future__ import annotations

import re

# `api_key=...`, `password: ...`, `secret=...`. `bearer` is excluded here because
# the next pattern handles it and keeps the word.
SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(?:access[_-]?token|api[_-]?key|authorization|credential|"
    r"password|secret|token)\b\s*([=:])\s*(?!bearer\b)[^\s,;]+"
)
BEARER_TOKEN = re.compile(r"(?i)\b(?:(authorization)\s*[:=]\s*)?(bearer)\s+[^\s,;]+")
URI_USERINFO = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
EMAIL_ADDRESS = re.compile(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b")

DEFAULT_LIMIT = 300


def safe_text(value: object, *, limit: int = DEFAULT_LIMIT) -> str | None:
    """Return actionable, bounded text with no secret or personal material.

    Whitespace is collapsed first so a multi-line traceback becomes one readable
    line and the limit means what it says.
    """
    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    text = URI_USERINFO.sub(r"\1[redacted]@", text)

    def redact_bearer(match: re.Match[str]) -> str:
        prefix = "Authorization: " if match.group(1) else ""
        return f"{prefix}Bearer [redacted]"

    text = BEARER_TOKEN.sub(redact_bearer, text)
    text = SENSITIVE_ASSIGNMENT.sub("[redacted]", text)
    text = EMAIL_ADDRESS.sub("[redacted-email]", text)
    return text[:limit]
