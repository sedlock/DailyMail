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

**Durable families.** The comparison above answers "is this the same
announcement as that one?" It does not, on its own, remember the answer. That
mattered on 1 September 2026: the Cayuse family had already been established
(6768 was matched to 6665 on 31 August) but the day's comparison ran from
scratch against the delivered corpus, that stage failed, and 6769 shipped as
NEW. Even without the failure, a pairwise comparison against a moving corpus
eventually loses the original.

So a confirmed match now also records a *logical family*: a durable set of
SubmissionIds that are the same logical announcement, keyed on normalized title,
category and audience. A later repost is compared against the family's canonical
and recent members regardless of when they were delivered.

The family only ever widens the candidate pool. Every veto below still applies
to every comparison, so this cannot manufacture a false positive; it can only
stop a true positive being missed for want of a candidate to compare against.
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
    # Set once the durable family this match belongs to has been resolved or
    # created. `None` simply means the caller did not supply family storage.
    family_id: int | None = None
    # The family's own identity, carried so persistence does not have to
    # re-derive it by parsing `family_key` back apart. Not part of `as_record`:
    # `repeat_matches` stores the finding, the family tables store the identity.
    normalized_title: str = ""
    category_id: int | None = None
    source_audience: str | None = None
    content_hash: str | None = None

    @property
    def display_status(self) -> str:
        return "Standing"

    @property
    def via_family(self) -> bool:
        """True when the prior reached the comparison through its family.

        Recorded because it is the interesting case: without durable families
        that candidate would never have been offered for comparison at all.
        """
        return bool(self.evidence.get("candidate_source") == "family")

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
            "family_id": self.family_id,
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


def _body_dates(text: str | None) -> frozenset:
    """The calendar days a body names, as comparison tokens.

    Reuses `events`' date patterns but deliberately *not* `events.parse_dates`.
    That function resolves a date for scheduling: it infers an omitted year from
    a reference date and refuses a date whose stated weekday disagrees with it.
    Both behaviours are right there and wrong here -- this only ever asks whether
    two bodies name the same days, so inferring a year would make the answer
    depend on when the comparison ran, and dropping "Wednesday, September 16"
    for disagreeing with an invented year would silently empty the set and turn
    the veto below into a no-op.

    So the token is `MM-DD`, or `YYYY-MM-DD` where the source stated a year.
    """
    from . import events

    folded = events._fold(text)
    tokens: set[str] = set()
    spans: list[tuple[int, int]] = []
    try:
        for match in events._LONG_DATE.finditer(folded):
            month = events.MONTHS.get(match.group("month").lower().rstrip("."))
            if month is None:
                continue
            try:
                day = int(match.group("day"))
            except (TypeError, ValueError):
                continue
            if not 1 <= day <= 31:
                continue
            year = match.group("year")
            tokens.add(
                f"{int(year):04d}-{month:02d}-{day:02d}" if year
                else f"{month:02d}-{day:02d}"
            )
            spans.append((match.start(), match.end()))
        for match in events._NUMERIC_DATE.finditer(folded):
            if any(start <= match.start() < end for start, end in spans):
                continue
            month, day = int(match.group("month")), int(match.group("day"))
            if not (1 <= month <= 12 and 1 <= day <= 31):
                continue
            year = match.group("year")
            if year:
                value = int(year)
                if value < 100:
                    value += 2000
                tokens.add(f"{value:04d}-{month:02d}-{day:02d}")
            else:
                tokens.add(f"{month:02d}-{day:02d}")
    except Exception:  # noqa: BLE001 - a comparison aid, never a hard dependency
        return frozenset()
    return frozenset(tokens)


def _dates_compatible(current, prior) -> tuple[bool, str]:
    """`_event_compatible`, for announcements whose dates live only in prose.

    Rowan's structured `Event` boolean is false for most of what is really an
    event, so `_event_compatible` cannot see a date change in a body like:

        Saturdays at 4 p.m. from July 11 through August 29
        Saturdays at 4 p.m. from September 5 through November 21

    That is the *next run* of the planetarium show, not a revision of the last
    one, and it scored 0.96 similar because everything except the dates is
    boilerplate. So if both bodies name dates and the sets differ, they are not
    the same occurrence. Byte-identical bodies are exempt: they cannot disagree
    about a date in the first place.
    """
    current_dates = _body_dates(_value(current, "body_text"))
    prior_dates = _body_dates(_value(prior, "body_text"))
    if not current_dates or not prior_dates:
        return True, "no_body_dates_to_compare"
    if current_dates != prior_dates:
        return False, "different_body_dates"
    return True, "same_body_dates"


def _is_spelling_only(prior_text: str | None, current_text: str | None) -> bool:
    """Did the repost only fix how words are spelled, not what they say?

    A room moving from `Robinson 102` to `Business 208` and an author fixing
    `commited` to `committed` both land in the near-identical band, and only one
    of them is something the reader must be told about. The tell is what happened
    to the words that went missing: a correction replaces a word with a close
    neighbour of itself, while a substantive edit swaps in something unrelated.
    """
    removed = distinctive_tokens(prior_text) - distinctive_tokens(current_text)
    if not removed:
        return True  # words were only added; nothing the reader knew has changed
    added = distinctive_tokens(current_text) - distinctive_tokens(prior_text)
    for token in removed:
        if not any(
            difflib.SequenceMatcher(None, token, candidate).ratio() >= 0.80
            for candidate in added
        ):
            return False
    return True


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


def _identity(current) -> dict:
    """The family identity fields every `RepeatMatch` carries, in one place."""
    return {
        "family_key": family_key(
            _value(current, "title"),
            _value(current, "category_id"),
            _value(current, "source_audience"),
        ),
        "normalized_title": normalize_title(_value(current, "title")),
        "category_id": _value(current, "category_id"),
        "source_audience": _value(current, "source_audience"),
        "content_hash": _value(current, "content_hash"),
    }


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

    if similarity < 1.0:
        dates_ok, date_finding = _dates_compatible(current, prior)
        if not dates_ok:
            return None
    else:
        date_finding = "identical_body"

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
        "body_dates": date_finding,
        "prior_delivered": _value(prior, "last_delivered_date"),
    }

    if similarity >= IDENTICAL_BODY and same_urls:
        # The strongest case, and by far the commonest one in practice: byte-for
        # byte the same announcement under a new number.
        return RepeatMatch(
            submission_id=int(_value(current, "submission_id")),
            matched_submission_id=int(_value(prior, "submission_id")),
            **_identity(current),
            method=METHOD_EXACT,
            confidence=0.99,
            body_similarity=similarity,
            evidence=evidence,
        )

    if similarity >= NEAR_IDENTICAL_BODY and same_urls and same_submitter:
        # Near-identical is not identical. Rowan announcement 6618 moved its film
        # screening from Robinson 102 to Business 208 and changed nothing else,
        # scoring 0.9874 -- the reader needs Standing *and* the UPDATED badge, or
        # they walk to the wrong building. A spelling fix does not earn one.
        spelling_only = _is_spelling_only(
            _value(prior, "body_text"), _value(current, "body_text")
        )
        evidence["near_identical_change"] = (
            "spelling_only" if spelling_only else "substantive"
        )
        return RepeatMatch(
            submission_id=int(_value(current, "submission_id")),
            matched_submission_id=int(_value(prior, "submission_id")),
            **_identity(current),
            method=METHOD_NEAR,
            confidence=0.92,
            body_similarity=similarity,
            materially_changed=not spelling_only,
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
            **_identity(current),
            method=METHOD_CHANGED,
            confidence=0.80,
            body_similarity=similarity,
            materially_changed=True,
            evidence=evidence,
        )

    return None


def _candidate_pool(
    row,
    *,
    by_title: dict[str, list],
    family_provider=None,
) -> tuple[list, dict[int, str], str]:
    """Every prior worth comparing `row` against, and where each one came from.

    Two sources, unioned and de-duplicated by SubmissionId:

    1. the delivered corpus, bucketed by normalized title -- what the reader has
       actually been sent, which is the only thing that justifies a demotion;
    2. the durable family for this announcement's own family key, which reaches
       members the delivered-corpus bucket may no longer surface.

    Returns `(candidates, source_by_id, key)`.
    """
    title_key = normalize_title(_value(row, "title"))
    key = family_key(
        _value(row, "title"),
        _value(row, "category_id"),
        _value(row, "source_audience"),
    )

    pool: dict[int, object] = {}
    source: dict[int, str] = {}
    for prior in by_title.get(title_key) or []:
        try:
            identifier = int(_value(prior, "submission_id", -1))
        except (TypeError, ValueError):
            continue
        pool[identifier] = prior
        source[identifier] = "delivered_history"

    if family_provider is not None:
        try:
            members = family_provider(key) or []
        except Exception as exc:  # noqa: BLE001 - families are an optimisation
            log.warning("could not load family %r: %s", key, exc)
            members = []
        for member in members:
            try:
                identifier = int(_value(member, "submission_id", -1))
            except (TypeError, ValueError):
                continue
            if identifier in pool:
                continue
            pool[identifier] = member
            source[identifier] = "family"

    return [pool[key_id] for key_id in sorted(pool)], source, key


def _hydrate(candidates: list, source: dict[int, str], load_bodies) -> list:
    """Fetch the comparison bodies for a shortlist, merging them onto the index.

    `load_bodies` is optional so the pure-data tests can go on passing whole
    rows. In production it is `db.announcement_bodies`, which reads `body_text`
    and `full_body` for the two or three candidates that survived title
    bucketing rather than for the entire delivered corpus.
    """
    if load_bodies is None or not candidates:
        return candidates
    wanted = [int(_value(row, "submission_id", -1)) for row in candidates]
    try:
        bodies = load_bodies([value for value in wanted if value >= 0]) or {}
    except Exception as exc:  # noqa: BLE001 - fall back to what we already have
        log.warning("could not load candidate bodies: %s", exc)
        return candidates

    hydrated = []
    for row in candidates:
        identifier = int(_value(row, "submission_id", -1))
        body_row = bodies.get(identifier)
        if body_row is None:
            continue  # no body, nothing to compare; never guess
        merged = dict(body_row)
        # Delivery dates live on the index row, content on the body row.
        for key in ("first_delivered_date", "last_delivered_date", "delivered_days"):
            value = _value(row, key)
            if value is not None:
                merged[key] = value
        merged["candidate_source"] = source.get(identifier, "delivered_history")
        hydrated.append(merged)
    return hydrated


def find_repeats(
    rows,
    history,
    *,
    family_provider=None,
    load_bodies=None,
) -> dict[str, RepeatMatch]:
    """Match each New announcement against what the reader has already received.

    `history` is the delivered index from `db.delivered_history`. `family_provider`
    maps a family key to that family's members, and `load_bodies` fetches the
    comparison payload for a shortlist of SubmissionIds; both are optional, and
    omitting them reduces this to the original single-pass behaviour over whole
    rows.

    The best (most similar) prior wins, and only announcements Rowan itself
    classifies as New are considered -- a Standing announcement is already
    labelled correctly.
    """
    by_title: dict[str, list] = {}
    for prior in history:
        by_title.setdefault(normalize_title(_value(prior, "title")), []).append(prior)

    matches: dict[str, RepeatMatch] = {}
    for row in rows:
        source_status = _value(row, "source_status") or _value(row, "status")
        if source_status != "New":
            continue
        candidates, source, key = _candidate_pool(
            row, by_title=by_title, family_provider=family_provider
        )
        candidates = _hydrate(candidates, source, load_bodies)
        best: RepeatMatch | None = None
        for prior in candidates:
            match = compare(row, prior)
            if match is None:
                continue
            origin = _value(prior, "candidate_source") or source.get(
                int(_value(prior, "submission_id", -1)), "delivered_history"
            )
            match.evidence["candidate_source"] = origin
            # A prior the reader was never actually sent cannot justify hiding
            # today's announcement as something already seen -- family
            # membership widens the search, it does not lower the bar.
            if not _value(prior, "last_delivered_date"):
                continue
            if best is None or _better(match, best):
                best = match
        if best is not None:
            best.family_key = key
            matches[str(_value(row, "submission_id"))] = best
    return matches


def _better(candidate: RepeatMatch, incumbent: RepeatMatch) -> bool:
    """Higher similarity wins; ties go to the *earliest* SubmissionId.

    The tie-break matters because reposts are byte-identical: every member of a
    family scores exactly 1.0 against a new one. Anchoring on the earliest ID
    makes the recorded match point at the family's original rather than at
    whichever member the query happened to return first, so the audit trail is
    stable across runs.
    """
    if candidate.body_similarity != incumbent.body_similarity:
        return candidate.body_similarity > incumbent.body_similarity
    return candidate.matched_submission_id < incumbent.matched_submission_id
