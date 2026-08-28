"""Detecting an announcement the reader has already been sent, under a new ID.

This exists because of something Rowan Announcer submitters actually do, not a
hypothetical. Rather than extending an existing announcement's distribution
dates, they create a *new* SubmissionId with the same body. In the first week of
production this happened to nine distinct announcements:

    Academic Integrity: Resources and Reminders for Faculty  6625 6626 6627 6628
    Rowan University Turnitin Policy                         6629 6630 6631
    2027 Searle Scholars Program                             6496 6497 6498
    Coming soon: A modernized Cayuse platform                6665 6667 6668
    Limited Submission: Andrew Carnegie Fellows              6578 6579 6580
    Digital Accessibility at Rowan                           6686 6687
    ...

Each new ID has `min(DistributionDates) == today`, so Rowan's own semantics --
which are correct, and which DailyMail deliberately trusts -- classify it as
New. It is genuinely new *to Rowan*. It is not new to the reader, who was sent
the identical text three days ago.

So this does not change how New is derived. It adds a second, separate question
asked only of announcements Rowan calls New: *has this reader already been sent
this?* The answer changes `display_status` alone; `daily_records.status` keeps
Rowan's own classification untouched and queryable.

The bar is deliberately high, because the failure mode in the other direction is
worse. `Faculty Senate Meeting` legitimately recurs monthly, and demoting a new
occurrence to Standing would hide a genuinely new commitment. So:

* a repeat needs a normalized-identical title AND near-identical body,
* an event whose date differs from the prior one is never a repeat,
* an event compared against a non-event is never a repeat,
* and anything ambiguous stays New.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from dataclasses import dataclass, field

log = logging.getLogger("dailymail.repeats")

# A repost carries the identical body. The tiny slack here absorbs an author
# fixing a typo on the way through, not a rewrite.
IDENTICAL_BODY = 0.995
NEAR_IDENTICAL_BODY = 0.97
# Below this, "same title" is not evidence of anything: two announcements can
# share a title and say entirely different things.
MATERIALLY_CHANGED_BODY = 0.85
# How many of the prior announcement's distinctive words a revision may drop
# before it stops looking like a revision and starts looking like a different
# announcement wearing the same title.
MAX_REMOVED_DISTINCTIVE_WORDS = 2

METHOD_EXACT = "title+body_identical"
METHOD_NEAR = "title+body_near_identical"
METHOD_CHANGED = "title+body_materially_changed"


@dataclass
class RepeatMatch:
    """One determination that a new SubmissionId repeats a delivered one."""

    submission_id: int
    matched_submission_id: int
    family_key: str
    method: str
    confidence: float
    body_similarity: float
    materially_changed: bool = False
    evidence: dict = field(default_factory=dict)

    @property
    def display_status(self) -> str:
        return "Standing"

    def as_record(self) -> dict:
        return {
            "submission_id": self.submission_id,
            "matched_submission_id": self.matched_submission_id,
            "family_key": self.family_key,
            "method": self.method,
            "confidence": self.confidence,
            "body_similarity": self.body_similarity,
            "materially_changed": self.materially_changed,
            "evidence": json.dumps(self.evidence, sort_keys=True),
        }


# --- normalization -----------------------------------------------------------

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_WS = re.compile(r"\s+")


def normalize_title(text: str | None) -> str:
    """Fold a subject to its comparison key.

    Emoji, decorative quotes and stray punctuation are noise here: Rowan
    submitters routinely add or drop a 🌎 between reposts of the same notice.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", str(text))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower()
    folded = _PUNCT.sub(" ", folded)
    return _WS.sub(" ", folded).strip()


def normalize_body(text: str | None) -> str:
    """Fold body text for similarity comparison. Structure-insensitive."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", str(text))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower()
    folded = _PUNCT.sub(" ", folded)
    return _WS.sub(" ", folded).strip()


def body_similarity(left: str | None, right: str | None) -> float:
    """0.0-1.0 similarity of two normalized bodies.

    Length is checked first because `SequenceMatcher` on two very different
    multi-kilobyte bodies is the expensive case and can never score highly
    anyway.
    """
    a, b = normalize_body(left), normalize_body(right)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    longer, shorter = max(len(a), len(b)), min(len(a), len(b))
    if shorter / longer < 0.80:
        return shorter / longer
    return difflib.SequenceMatcher(None, a, b).ratio()


# Words too common to distinguish two announcements from each other.
_COMMON = frozenset(
    """
    about above after also announcement any are been before being both can
    contact continue during each following from have here into more most must
    news other over please rowan should some students faculty staff such than
    that their them then there these they this those through university until
    using very what when where which while will with within would your available
    information please email visit link click here open available time date
    """.split()
)


def distinctive_tokens(text: str | None) -> frozenset[str]:
    """The words that make one announcement recognizably itself."""
    return frozenset(
        token
        for token in normalize_body(text).split()
        if len(token) >= 4 and not token.isdigit() and token not in _COMMON
    )


def _urls(html: str | None) -> frozenset[str]:
    if not html:
        return frozenset()
    from html import unescape

    return frozenset(
        unescape(match.group(1)).strip().rstrip("/")
        for match in re.finditer(
            r'href\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE
        )
        if match.group(1).lower().startswith(("http://", "https://"))
    )


def family_key(title: str | None, category_id, audience: str | None) -> str:
    """Groups the reposts of one announcement. Stable and human-readable."""
    return f"{normalize_title(title)}|{category_id}|{audience or ''}"


# --- comparison --------------------------------------------------------------


def _value(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _event_compatible(current, prior) -> tuple[bool, str]:
    """Is the prior announcement about the *same occurrence* as this one?

    This is the guard that keeps a monthly `Faculty Senate Meeting` New. A new
    date is a new commitment, and no amount of body similarity changes that.
    """
    current_is_event = bool(_value(current, "is_event", 0))
    prior_is_event = bool(_value(prior, "is_event", 0))
    if current_is_event != prior_is_event:
        return False, "event_status_differs"
    if not current_is_event:
        return True, "both_non_event"
    current_date = _value(current, "event_date")
    prior_date = _value(prior, "event_date")
    if current_date != prior_date:
        return False, "different_event_date"
    if _value(current, "event_start_time") != _value(prior, "event_start_time"):
        return False, "different_event_time"
    return True, "same_occurrence"


def compare(current, prior) -> RepeatMatch | None:
    """Decide whether `current` logically repeats `prior`. Conservative.

    Every hard requirement below is a veto, not a weight: this is not a fuzzy
    score that a strong body match can talk its way past.
    """
    current_title = normalize_title(_value(current, "title"))
    prior_title = normalize_title(_value(prior, "title"))
    if not current_title or current_title != prior_title:
        return None
    if int(_value(current, "submission_id", 0)) == int(
        _value(prior, "submission_id", -1)
    ):
        return None

    if _value(current, "category_id") != _value(prior, "category_id"):
        return None
    if _value(current, "source_audience") != _value(prior, "source_audience"):
        return None

    compatible, occurrence = _event_compatible(current, prior)
    if not compatible:
        return None

    similarity = body_similarity(
        _value(current, "body_text"), _value(prior, "body_text")
    )
    if similarity < MATERIALLY_CHANGED_BODY:
        return None

    current_urls = _urls(_value(current, "full_body"))
    prior_urls = _urls(_value(prior, "full_body"))
    same_urls = current_urls == prior_urls
    same_contact = _value(current, "contact_email") == _value(prior, "contact_email")
    same_submitter = _value(current, "submitted_by_email") == _value(
        prior, "submitted_by_email"
    )

    evidence = {
        "title": "normalized_identical",
        "body_similarity": round(similarity, 4),
        "same_category": True,
        "same_audience": True,
        "same_urls": same_urls,
        "same_contact": same_contact,
        "same_submitter": same_submitter,
        "occurrence": occurrence,
        "prior_delivered": _value(prior, "last_delivered_date"),
    }

    if similarity >= IDENTICAL_BODY and same_urls:
        # The strongest case, and by far the commonest one in practice: byte-for
        # byte the same announcement under a new number.
        return RepeatMatch(
            submission_id=int(_value(current, "submission_id")),
            matched_submission_id=int(_value(prior, "submission_id")),
            family_key=family_key(
                _value(current, "title"),
                _value(current, "category_id"),
                _value(current, "source_audience"),
            ),
            method=METHOD_EXACT,
            confidence=0.99,
            body_similarity=similarity,
            evidence=evidence,
        )

    if similarity >= NEAR_IDENTICAL_BODY and same_urls and same_submitter:
        return RepeatMatch(
            submission_id=int(_value(current, "submission_id")),
            matched_submission_id=int(_value(prior, "submission_id")),
            family_key=family_key(
                _value(current, "title"),
                _value(current, "category_id"),
                _value(current, "source_audience"),
            ),
            method=METHOD_NEAR,
            confidence=0.92,
            body_similarity=similarity,
            evidence=evidence,
        )

    # In the ambiguous band, similarity alone is not enough. A *revision* adds or
    # rewrites text; a *substitution* swaps the announcement's subject matter
    # while keeping its shape. `SPSS licence renewal` and `Mathematica licence
    # renewal` are 93% identical and are not the same announcement -- what tells
    # them apart is that the prior one's distinctive words are gone, not merely
    # added to. So a repost may gain words freely and lose almost none.
    removed = distinctive_tokens(_value(prior, "body_text")) - distinctive_tokens(
        _value(current, "body_text")
    )
    evidence["distinctive_words_removed"] = sorted(removed)[:8]

    if (
        similarity >= MATERIALLY_CHANGED_BODY
        and same_submitter
        and same_contact
        and len(removed) <= MAX_REMOVED_DISTINCTIVE_WORDS
    ):
        # Same announcement, genuinely revised. Standing is right, but the
        # reader needs to know it changed, so this carries the UPDATED badge.
        return RepeatMatch(
            submission_id=int(_value(current, "submission_id")),
            matched_submission_id=int(_value(prior, "submission_id")),
            family_key=family_key(
                _value(current, "title"),
                _value(current, "category_id"),
                _value(current, "source_audience"),
            ),
            method=METHOD_CHANGED,
            confidence=0.80,
            body_similarity=similarity,
            materially_changed=True,
            evidence=evidence,
        )

    return None


def find_repeats(rows, history) -> dict[str, RepeatMatch]:
    """Match each New announcement against what the reader has already received.

    `history` is the delivered corpus from `db.delivered_history`. The best (most
    similar) prior match wins, and only announcements Rowan itself classifies as
    New are considered -- a Standing announcement is already labelled correctly.
    """
    by_title: dict[str, list] = {}
    for prior in history:
        by_title.setdefault(normalize_title(_value(prior, "title")), []).append(prior)

    matches: dict[str, RepeatMatch] = {}
    for row in rows:
        source_status = _value(row, "source_status") or _value(row, "status")
        if source_status != "New":
            continue
        key = normalize_title(_value(row, "title"))
        candidates = by_title.get(key) or []
        best: RepeatMatch | None = None
        for prior in candidates:
            match = compare(row, prior)
            if match is None:
                continue
            if best is None or match.body_similarity > best.body_similarity:
                best = match
        if best is not None:
            matches[str(_value(row, "submission_id"))] = best
    return matches
