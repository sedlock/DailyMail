"""The daily calendar path: candidate -> relevance -> venue -> action -> callout.

Enrichment, not infrastructure. Every branch here is wrapped so that a failed
parse, an invalid model judgement, an unresolvable venue, a routing outage or a
malformed link costs exactly one calendar button and nothing else. The
announcement still renders in full and the digest still goes out -- and none of
these is an `ATTENTION REQUIRED` condition. They are recorded instead, in
`calendar_recommendations`, with the reason.

The relevance decision comes from the curation call that already runs, so the
feature adds a few hundred tokens rather than a second 60-90 second model
invocation. When curation falls back to deterministic ordering, so does calendar
relevance: a transparent keyword-and-category score, using the same reader model
the system prompt describes. The button keeps working on a day Claude does not.
"""

from __future__ import annotations

import json
import logging
import re
import time as _time
from dataclasses import dataclass, field
from datetime import date, datetime

from . import calendar_action, db, events, travel
from .settings import Settings

log = logging.getLogger("dailymail.calendar")

METHOD_CLAUDE = "claude"
METHOD_DETERMINISTIC = "deterministic"


@dataclass
class CalendarMetrics:
    """One row of counters per run, in the same spirit as the parking metrics."""

    candidates_detected: int = 0
    actions_offered: int = 0
    withheld_by_relevance: int = 0
    withheld_other: int = 0
    parse_failures: int = 0
    travel_enriched: int = 0
    venue_cache_hits: int = 0
    venue_cache_misses: int = 0
    venue_unresolved: int = 0
    route_lookups: int = 0
    route_failures: int = 0
    titles_cleaned: int = 0
    build_failures: int = 0
    # Announcements offering more than one selectable sitting, and the total
    # number of calendar controls those sittings produced.
    multi_session_announcements: int = 0
    session_actions_offered: int = 0
    duration_seconds: float = 0.0
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "candidates": self.candidates_detected,
            "offered": self.actions_offered,
            "withheld_relevance": self.withheld_by_relevance,
            "withheld_other": self.withheld_other,
            "parse_failures": self.parse_failures,
            "travel_enriched": self.travel_enriched,
            "venue_hits": self.venue_cache_hits,
            "venue_misses": self.venue_cache_misses,
            "venue_unresolved": self.venue_unresolved,
            "route_lookups": self.route_lookups,
            "route_failures": self.route_failures,
            "titles_cleaned": self.titles_cleaned,
            "build_failures": self.build_failures,
            "multi_session": self.multi_session_announcements,
            "session_actions": self.session_actions_offered,
            "duration_seconds": round(self.duration_seconds, 3),
            "errors": self.errors[:5],
        }


# --- deterministic relevance -------------------------------------------------
#
# Used when curation is unavailable, and as the floor under the model's
# judgement. The weights encode the reader described in the curation system
# prompt: a senior technology executive who is also a Rowan parent.
#
# The first version of this scorer searched one flat haystack -- title, whole
# body, extracted title and location concatenated -- for every phrase it knew.
# Two production results on 9 September 2026 showed why that is the wrong shape.
#
#   * 6815, `Hollybush Tour`, scored 0.85 and was offered because the word
#     `president` appears in its history: "the University's history through the
#     legacy of its presidents, and the 1967 summit between President Lyndon B.
#     Johnson and Soviet Premier Alexei Kosygin". An ordinary building tour was
#     promoted to a senior-leadership event by two sentences of prose about
#     1967. (The tour is in fact worth offering -- but for its own reasons, and
#     this scorer now reaches that conclusion without ever reading the word.)
#   * 6783, `You're Invited to the Wellness Center Open House!`, scored 0.20 and
#     was withheld, because `free food` deep in the body cost it 0.25 while
#     `emergency` -- from "Emergency Medical Services" in a list of the
#     departments attending -- gave back 0.25. A broad student-service open
#     house with a date, a time and a room lost its button to two incidental
#     phrases neither of which describes the event.
#
# So relevance now reasons from what the event *is*, in this order:
#
#   1. the event title (extracted title, announcement subject, heading)
#   2. the Rowan category
#   3. the audience it was published to
#   4. the announcement's own opening -- where it states its purpose
#   5. breadth: is this for everybody, or for a particular few?
#   6. role relevance to this reader
#
# Body prose beyond the opening is *supporting evidence only*: it is scored at a
# quarter weight and its total contribution is clamped to +/- BODY_INFLUENCE_CAP,
# so no amount of incidental wording can carry an event over the threshold or
# push a genuine one under it. The signals that identify *who* an event belongs
# to -- president, provost, town hall -- are focus-only and are never read out of
# body prose at all, which is exactly the Hollybush defect.

# Evidence is graded by where it sits, because where a phrase appears says how
# much it is claiming about the event. In the title it *is* the event; in the
# opening sentences it is part of how the announcement describes itself; further
# down it is a detail. "FREE FOOD!" in 6927's subject is the event. The same
# words in 6783's list of what is on offer are not.
IDENTITY_INFLUENCE_WEIGHT = 1.0
PURPOSE_INFLUENCE_WEIGHT = 0.5
BODY_INFLUENCE_WEIGHT = 0.25
# How much the body below the opening may move a score in either direction.
BODY_INFLUENCE_CAP = 0.10
# How much of the announcement counts as its own statement of purpose.
PURPOSE_CHARS = 260
# Ceilings, so a title that happens to hit several phrases cannot run away.
FOCUS_TOPIC_CAP = 0.40
ROUTINE_PENALTY_CAP = -0.60
BREADTH_CAP = 0.25
BASE_SCORE = 0.30

# Who the event belongs to. FOCUS ONLY: these are read from the title, the
# extracted event title and the announcement's own heading, never from body
# prose, because a historical mention of a president does not make a tour a
# leadership event.
_IDENTITY_SIGNALS = (
    ("town hall", 0.40), ("provost", 0.35), ("president", 0.35),
    ("chancellor", 0.35), ("cabinet", 0.30), ("board of trustees", 0.35),
    ("state of the university", 0.40), ("dean s", 0.15), ("trustee", 0.25),
    ("inauguration", 0.30), ("commencement", 0.40), ("convocation", 0.25),
)

# What the event is about. Read from the focus text: title, extracted title,
# heading, location and the announcement's opening statement of purpose.
_TOPIC_SIGNALS = (
    # technology and enterprise systems
    ("cybersecurity", 0.40), ("information security", 0.35), ("data security", 0.30),
    ("enterprise system", 0.35), ("information technology", 0.30), ("banner", 0.25),
    ("erp", 0.25), ("artificial intelligence", 0.25), ("system migration", 0.25),
    ("outage", 0.30), ("phishing", 0.25),
    # institutional operations and policy
    ("accreditation", 0.30), ("strategic plan", 0.30), ("open enrollment", 0.35),
    ("budget", 0.25), ("payroll", 0.25), ("governance", 0.25), ("policy", 0.20),
    ("benefits", 0.20), ("briefing", 0.25), ("summit", 0.20),
    # safety
    ("emergency", 0.25), ("public safety", 0.25), ("safety", 0.20),
    ("active shooter", 0.35), ("severe weather", 0.25),
    # broad campus and community occasions
    ("open house", 0.20), ("memorial", 0.20), ("anniversary", 0.10),
    ("ribbon cutting", 0.25), ("groundbreaking", 0.25), ("dedication", 0.15),
    # student and parent logistics
    ("move in", 0.30), ("family weekend", 0.30), ("parents", 0.25),
    ("orientation", 0.25), ("financial aid", 0.25), ("commencement", 0.40),
    # broad student services a parent cares about
    ("wellness", 0.20), ("health services", 0.25), ("counseling", 0.20),
    ("counselling", 0.20), ("student health", 0.25),
)

# Routine, narrow or promotional. Scored on the focus text, where an
# announcement says what it is; `FREE FOOD!` in a subject is the event, the same
# words in a list of refreshments are not.
_ROUTINE_SIGNALS = (
    ("general body meeting", 0.35), ("interest meeting", 0.30), ("club", 0.30),
    ("intramural", 0.30), ("trivia", 0.35), ("karaoke", 0.35), ("bingo", 0.35),
    ("game night", 0.35), ("movie night", 0.35), ("open mic", 0.35),
    ("late night", 0.30), ("tailgate", 0.25), ("greek life", 0.30),
    ("sorority", 0.35), ("fraternity", 0.35), ("giveaway", 0.25),
    ("merchandise", 0.25), ("planetarium", 0.25), ("swim lesson", 0.35),
    ("welcome week", 0.20), ("beach day", 0.30), ("off campus trip", 0.25),
    ("free food", 0.25), ("happy hour", 0.25), ("mixer", 0.25),
    ("student led", 0.20), ("book club", 0.30), ("drop in", 0.20),
    ("info session", 0.20), ("information session", 0.20),
    ("resume review", 0.25), ("breakfast", 0.15), ("cookout", 0.25),
    ("bbq", 0.25), ("ticket", 0.15), ("audition", 0.25), ("open skate", 0.30),
)

# Explicit breadth, read from the focus text.
_BREADTH_SIGNALS = (
    ("all employees", 0.20), ("all faculty", 0.15), ("all staff", 0.15),
    ("faculty and staff", 0.15), ("university community", 0.20),
    ("entire rowan", 0.20), ("campus wide", 0.20), ("campus community", 0.15),
    ("open to all", 0.15), ("all are welcome", 0.15), ("everyone", 0.10),
)

# Rowan categories whose events are, on their own, likely to matter here.
_STRONG_CATEGORIES = {
    "Official": 0.30, "Technology": 0.30, "Public Safety": 0.25,
    "Human Resources": 0.25, "Facilities": 0.20, "Glassboro Campus": 0.20,
    "Finance": 0.20, "Registrar": 0.20, "Well-being and Health": 0.20,
    "Research": 0.10, "Faculty": 0.10, "Academics": 0.05,
    "Academic and Career Success": 0.05,
}
_WEAK_CATEGORIES = {
    "Athletic Events": -0.25, "Clubs and Organizations": -0.30,
    "Campus Activities": -0.25, "Social and Cultural Events": -0.20,
    "Our Stories!": -0.25, "Volunteer Opportunities": -0.15,
}

# Audience, as published by Rowan. `Both` is the broadest thing the source can
# say, and a student-only event is no longer penalised for being student-only --
# a parent-relevant student service is exactly the kind of thing this reader
# wants. Narrowness is expressed by the routine signals instead.
_AUDIENCE_WEIGHTS = {"Both": 0.10, "Employees": 0.05, "Students": 0.0}
# An announcement published to everyone, carrying no routine or narrowing marker
# at all, is broadly applicable by construction.
_BROAD_AUDIENCE_BONUS = 0.05

_MATCH_NORMALIZE = re.compile(r"[^a-z0-9]+")


def _normalize_for_match(text: str) -> str:
    """Lowercase, and reduce every run of punctuation or space to one space.

    So `Drop- In`, `drop-in` and `Drop In` are the same phrase, and a table's
    worth of curly quotes and emoji cannot hide one.
    """
    return f" {_MATCH_NORMALIZE.sub(' ', (text or '').lower()).strip()} "


def _hits(haystack: str, signals, *, scale: float = 1.0):
    """Every matching phrase and its weight, at `scale` of its listed value."""
    for phrase, weight in signals:
        if f" {phrase} " in haystack:
            yield phrase, weight * scale


def _capped(total: float, cap: float) -> float:
    return min(total, cap) if cap >= 0 else max(total, cap)


def deterministic_relevance(row, candidate) -> tuple[float, str]:
    """A transparent, explainable relevance score in 0.0-1.0.

    Deliberately conservative: the base sits below the default threshold, so an
    event has to earn its button rather than merely fail to disqualify itself.
    The returned reason names every signal that moved the score and marks the
    ones that came from body prose, so the audit row shows not just what was
    decided but from which part of the announcement.
    """
    body = str(_value(row, "body_text") or "")
    purpose = body[:PURPOSE_CHARS]
    remainder = body[PURPOSE_CHARS:]

    # What the event IS: its names, its place, and its own opening sentences.
    identity_text = _normalize_for_match(
        " ".join(
            str(part or "")
            for part in (
                _value(row, "title"),
                candidate.title,
                getattr(candidate, "heading_hint", None),
                candidate.location,
                getattr(candidate, "audience", None),
            )
        )
    )
    purpose_text = _normalize_for_match(purpose)
    body_text = _normalize_for_match(remainder)

    score = BASE_SCORE
    hits: list[str] = []

    def graded(signals):
        """Every match, once, at the weight of the strongest place it appears."""
        for phrase, weight in signals:
            for text, scale, label in (
                (identity_text, IDENTITY_INFLUENCE_WEIGHT, ""),
                (purpose_text, PURPOSE_INFLUENCE_WEIGHT, "purpose:"),
            ):
                if f" {phrase} " in text:
                    yield phrase, weight * scale, label
                    break

    # 1-2. who the event belongs to, and what it is about.
    topic_total = 0.0
    for phrase, weight in _hits(identity_text, _IDENTITY_SIGNALS):
        topic_total += weight
        hits.append(phrase)
    for phrase, weight, label in graded(_TOPIC_SIGNALS):
        topic_total += weight
        hits.append(f"{label}{phrase}")
    score += _capped(topic_total, FOCUS_TOPIC_CAP)

    # 3. routine / narrow markers, graded the same way.
    routine_total = 0.0
    for phrase, weight, label in graded(_ROUTINE_SIGNALS):
        routine_total -= weight
        hits.append(f"-{label}{phrase}")
    score += _capped(routine_total, ROUTINE_PENALTY_CAP)

    # 4. breadth: who is this for?
    audience = _value(row, "source_audience")
    breadth_total = _AUDIENCE_WEIGHTS.get(audience, 0.0)
    if audience == "Both" and not routine_total:
        breadth_total += _BROAD_AUDIENCE_BONUS
        hits.append("broad audience")
    for phrase, weight, label in graded(_BREADTH_SIGNALS):
        breadth_total += weight
        hits.append(f"{label}{phrase}")
    score += _capped(breadth_total, BREADTH_CAP)

    # 5. the Rowan category, which is the source's own statement of kind.
    category = _value(row, "category_title") or ""
    score += _STRONG_CATEGORIES.get(category, 0.0)
    score += _WEAK_CATEGORIES.get(category, 0.0)
    if category in _STRONG_CATEGORIES or category in _WEAK_CATEGORIES:
        hits.append(f"category:{category}")

    # 6. the rest of the body: supporting evidence, and clamped so that it can
    #    never be the reason an event does or does not get a button.
    body_total = 0.0
    body_hits: list[str] = []
    for phrase, weight in _hits(
        body_text, _TOPIC_SIGNALS, scale=BODY_INFLUENCE_WEIGHT
    ):
        body_total += weight
        body_hits.append(f"body:{phrase}")
    for phrase, weight in _hits(
        body_text, _ROUTINE_SIGNALS, scale=BODY_INFLUENCE_WEIGHT
    ):
        body_total -= weight
        body_hits.append(f"body:-{phrase}")
    body_total = max(-BODY_INFLUENCE_CAP, min(BODY_INFLUENCE_CAP, body_total))
    score += body_total
    hits.extend(body_hits)

    score = max(0.0, min(1.0, score))
    detail = ", ".join(hits[:8]) or "no distinguishing signal"
    return score, f"deterministic relevance {score:.2f} ({detail})"


def _value(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


# --- orchestration -----------------------------------------------------------


def detect_candidates(rows, *, target_date: str, settings: Settings):
    """Every schedulable event in one day. Returns `(candidates, diagnostics)`.

    `candidates` maps a submission id to the *list of sessions* that
    announcement offers -- one entry for an ordinary event, one per advertised
    sitting for a series. Relevance is still judged once per announcement, so
    this costs no extra model call.

    Runs *before* curation so the relevance question can ride along on the call
    that already happens. Never raises.
    """
    candidates: dict[str, list[events.EventCandidate]] = {}
    diagnostics: dict[str, dict] = {}
    if not settings.calendar_enabled:
        return candidates, diagnostics

    reference = date.fromisoformat(target_date)
    for row in rows:
        submission_id = str(row["submission_id"])
        try:
            sessions, diagnosis = events.detect_series(
                row, reference_date=reference
            )
        except Exception as exc:  # noqa: BLE001 - never lose an announcement
            diagnostics[submission_id] = {
                "evidence": [],
                "reason": f"parse_error: {type(exc).__name__}",
            }
            log.info("event parse failed for %s: %s", submission_id, exc)
            continue
        diagnostics[submission_id] = diagnosis
        if sessions:
            candidates[submission_id] = sessions
    return candidates, diagnostics


def primary_candidates(candidates: dict) -> dict:
    """The one session per announcement that stands for the whole series.

    Relevance, the curation payload and the audit row are all announcement-level
    questions; this is the view they take of a session list.
    """
    out = {}
    for submission_id, value in (candidates or {}).items():
        out[submission_id] = value[0] if isinstance(value, list) else value
    return out


def _sessions_for(candidates: dict, submission_id: str) -> list:
    value = (candidates or {}).get(submission_id)
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def enrich_digest(
    connection,
    rows,
    *,
    target_date: str,
    settings: Settings,
    candidates: dict,
    diagnostics: dict,
    curation_entries: list[dict] | None = None,
    curation_method: str = "fallback",
    curation_model: str | None = None,
    allow_routing: bool = True,
    router=None,
    persist: bool = True,
) -> tuple[dict[str, calendar_action.CalendarAction], CalendarMetrics]:
    """Turn detected candidates into validated calendar actions.

    Returns `{submission_id: CalendarAction}` plus counters. Never raises: this
    is enrichment, and a digest without a calendar button is a working digest.

    `persist=False` computes the actions without writing
    `calendar_recommendations`. That is what a *preview* wants: the stored rows
    are the audit trail of what a given morning's run actually decided, and
    re-rendering a past date months later under changed code must not overwrite
    them with what today's code would have decided. `dailymail render` learned
    this the hard way while validating the Phase 6 relevance change.
    """
    started = _time.monotonic()
    metrics = CalendarMetrics()
    actions: dict[str, calendar_action.CalendarAction] = {}
    records: list[dict] = []

    if not settings.calendar_enabled:
        metrics.duration_seconds = _time.monotonic() - started
        return actions, metrics

    metrics.parse_failures = sum(
        1
        for diagnosis in diagnostics.values()
        if str(diagnosis.get("reason") or "").startswith("parse_error")
    )
    metrics.candidates_detected = len(candidates)

    judgements = {
        str(entry["submission_id"]): entry.get("calendar")
        for entry in (curation_entries or [])
        if entry.get("calendar")
    }
    rows_by_id = {str(row["submission_id"]): row for row in rows}
    offered = 0

    for submission_id, row in rows_by_id.items():
        sessions = _sessions_for(candidates, submission_id)
        candidate = sessions[0] if sessions else None
        diagnosis = diagnostics.get(submission_id) or {}
        if candidate is None:
            records.append(
                _record(
                    submission_id, row,
                    is_candidate=False,
                    evidence=diagnosis,
                    withheld_reason=diagnosis.get("reason") or "not_an_event",
                )
            )
            continue

        try:
            action, record = _build_one(
                connection,
                row=row,
                sessions=sessions,
                diagnosis=diagnosis,
                judgement=judgements.get(submission_id),
                target_date=target_date,
                settings=settings,
                metrics=metrics,
                allow_routing=allow_routing,
                router=router,
                budget_exhausted=offered >= settings.calendar_max_actions,
                curation_method=curation_method,
                curation_model=curation_model,
            )
        except Exception as exc:  # noqa: BLE001 - one bad event, not a bad digest
            metrics.build_failures += 1
            metrics.errors.append(
                f"{submission_id}: {type(exc).__name__}: {str(exc)[:160]}"
            )
            log.warning("calendar action failed for %s: %s", submission_id, exc)
            records.append(
                _record(
                    submission_id, row,
                    is_candidate=True,
                    evidence=diagnosis,
                    withheld_reason=f"build_error: {type(exc).__name__}",
                    validation_status="failed",
                )
            )
            continue

        records.append(record)
        if action is not None:
            actions[submission_id] = action
            offered += 1
            metrics.actions_offered += 1
            metrics.session_actions_offered += action.session_count
            if action.is_multi_session:
                metrics.multi_session_announcements += 1
            if action.has_travel:
                metrics.travel_enriched += 1

    if persist:
        try:
            db.save_calendar_recommendations(connection, target_date, records)
        except Exception as exc:  # noqa: BLE001 - the audit trail is not the product
            metrics.errors.append(f"persist: {type(exc).__name__}: {str(exc)[:160]}")
            log.warning("could not persist calendar recommendations: %s", exc)

    metrics.duration_seconds = _time.monotonic() - started
    return actions, metrics


def _build_one(
    connection,
    *,
    row,
    sessions,
    diagnosis,
    judgement,
    target_date,
    settings,
    metrics,
    allow_routing,
    router,
    budget_exhausted,
    curation_method,
    curation_model,
):
    submission_id = str(row["submission_id"])
    # Relevance, attendance mode and the title are announcement-level decisions,
    # made once from the first session and applied to every sitting. That is the
    # whole reason a multi-session announcement costs no extra model call.
    candidate = sessions[0]

    # --- relevance ------------------------------------------------------
    if judgement is not None:
        score = float(judgement["confidence"]) if judgement["offer"] else 0.0
        reason = judgement.get("reason") or "model judgement"
        method, model = METHOD_CLAUDE, curation_model
        if not judgement["offer"]:
            reason = f"model declined: {reason}"
    else:
        score, reason = deterministic_relevance(row, candidate)
        method, model = METHOD_DETERMINISTIC, None

    offer = score >= settings.calendar_relevance_threshold

    # --- attendance mode -------------------------------------------------
    mode = candidate.attendance_mode
    if judgement is not None and judgement.get("attendance_mode"):
        proposed = judgement["attendance_mode"]
        # The model may resolve a hybrid event, and only that. It can never turn
        # a room into a webinar or invent a venue that was not extracted.
        if candidate.attendance_mode == "hybrid" and proposed in (
            "in_person", "virtual", "hybrid"
        ):
            mode = proposed
        elif proposed == candidate.attendance_mode:
            mode = proposed
    if mode == "hybrid" and candidate.location:
        # Rowan says "in-person preferred" often enough that a hybrid event with
        # a real room defaults to attending it.
        physical_preferred = "in-person preferred" in (
            (row["body_text"] or "") + (row["full_body"] or "")
        ).lower()
        if physical_preferred and judgement is None:
            mode = "hybrid"

    # --- title ------------------------------------------------------------
    # Deterministic cleanup first, so a day without Claude still produces a
    # recognizable calendar entry rather than `Provost's Town Hall - Oct 14`.
    title = calendar_action.clean_calendar_title(
        candidate.title, fallback=str(row["title"] or "")
    )
    if title != candidate.title:
        metrics.titles_cleaned += 1
    if judgement is not None and judgement.get("suggested_title"):
        suggested = judgement["suggested_title"]
        if suggested != title:
            metrics.titles_cleaned += 1
        title = suggested

    if not offer:
        metrics.withheld_by_relevance += 1
        return None, _record(
            submission_id, row,
            is_candidate=True, evidence=diagnosis, candidate=candidate,
            score=score, reason=reason, method=method, model=model,
            attendance_mode=mode, title=title,
            withheld_reason="below_relevance_threshold",
        )

    if budget_exhausted:
        metrics.withheld_other += 1
        return None, _record(
            submission_id, row,
            is_candidate=True, evidence=diagnosis, candidate=candidate,
            score=score, reason=reason, method=method, model=model,
            attendance_mode=mode, title=title,
            withheld_reason="max_actions_reached",
        )

    # --- venue and travel --------------------------------------------------
    # The venue belongs to the announcement, so it is resolved once. Travel
    # minutes are a property of the place, not of the sitting, so the same plan
    # applies to every session -- but each session gets its own holds, at its
    # own times, in its own .ics.
    resolution, plan = _venue_and_travel(
        connection,
        candidate=candidate,
        attendance_mode=mode,
        settings=settings,
        metrics=metrics,
        allow_routing=allow_routing,
        router=router,
        row=row,
    )

    # --- build one session at a time ---------------------------------------
    official_url = row["official_url"]
    built: list[calendar_action.CalendarSession] = []
    for session_candidate in sessions:
        built.append(
            _build_session(
                session_candidate,
                row=row,
                title=title,
                mode=mode,
                plan=plan,
                settings=settings,
                target_date=target_date,
                official_url=official_url,
                multi=len(sessions) > 1,
            )
        )

    action = calendar_action.CalendarAction.from_sessions(
        submission_id=submission_id,
        title=title,
        timezone=candidate.timezone,
        location=candidate.location,
        attendance_mode=mode,
        sessions=built,
        registration_urls=list(candidate.registration_urls),
        official_url=official_url,
    )
    calendar_action.validate_action(action)

    return action, _record(
        submission_id, row,
        is_candidate=True, evidence=diagnosis, candidate=candidate,
        score=score, reason=reason, method=method, model=model,
        attendance_mode=mode, title=title, offer=True,
        resolution=resolution, plan=plan, action=action,
    )


def _build_session(
    candidate,
    *,
    row,
    title,
    mode,
    plan,
    settings,
    target_date,
    official_url,
    multi,
):
    """One sitting's description, deep link, VEVENTs and `.ics`.

    Called once for an ordinary event and once per advertised sitting for a
    series. Nothing here is announcement-level: everything it needs has already
    been decided by the caller, which is what keeps a series from re-asking the
    model or re-resolving the venue.
    """
    travel_note = None
    if plan.required:
        travel_note = (
            f"Actual event: {calendar_action._clock(candidate.start_datetime)}–"
            f"{calendar_action._clock(candidate.end_datetime)}.\n"
            f"This calendar entry also reserves {plan.minutes_before} minutes "
            f"before and {plan.minutes_after} minutes after for travel "
            f"from/to {settings.calendar_base_address}."
        )
        if plan.estimated:
            travel_note += "\n(Travel time is a distance-based estimate.)"

    description = calendar_action.build_description(
        candidate,
        official_url=official_url,
        contact_line=_contact_line(row),
        travel_note=travel_note,
        body_text=row["body_text"],
    )
    action_url = calendar_action.build_action_url(
        candidate, description=description, title=title
    )

    blocks = calendar_action.build_blocks(
        candidate,
        content_hash=row["content_hash"],
        description=description,
        official_url=official_url,
        title=title,
        travel_minutes_before=plan.minutes_before if plan.required else 0,
        travel_minutes_after=plan.minutes_after if plan.required else 0,
        travel_mode=plan.mode,
    )

    ics_text = None
    ics_filename = None
    if settings.calendar_attach_ics:
        ics_text = calendar_action.build_ics(
            blocks,
            timezone=candidate.timezone,
            dtstamp=calendar_action.official_ics_dtstamp(target_date),
        )
        # A series dates its attachments so the reader can tell which file is
        # which; a single event keeps the name production already produces.
        ics_filename = calendar_action.slugify_filename(
            title,
            suffix=candidate.event_date.isoformat() if multi else None,
        )

    return calendar_action.CalendarSession(
        index=candidate.session_index,
        count=candidate.session_count,
        title=title,
        start=candidate.start_datetime,
        end=candidate.end_datetime,
        timezone=candidate.timezone,
        location=candidate.location,
        attendance_mode=mode,
        description=description,
        action_url=action_url,
        mechanism=(
            calendar_action.MECHANISM_BOTH
            if ics_text
            else calendar_action.MECHANISM_DEEPLINK
        ),
        ics_text=ics_text,
        ics_filename=ics_filename,
        travel_minutes_before=plan.minutes_before if plan.required else 0,
        travel_minutes_after=plan.minutes_after if plan.required else 0,
        travel_mode=plan.mode,
        travel_estimated=plan.estimated,
        blocks=blocks,
    )


def _venue_and_travel(
    connection, *, candidate, attendance_mode, settings, metrics, allow_routing,
    router, row,
):
    """Resolve the venue and work out the travel holds, using the cache first."""
    if attendance_mode == "virtual" or not candidate.location:
        return (
            travel.VenueResolution(normalized_venue="", display_name=""),
            travel.TravelPlan(
                required=False,
                reason="virtual_attendance" if attendance_mode == "virtual"
                else "no_physical_location",
            ),
        )

    campus_hint = _campus_hint(row)
    resolution = travel.resolve_venue(
        connection, candidate.location, campus_hint=campus_hint
    )
    if not resolution.located:
        metrics.venue_unresolved += 1
        return resolution, travel.TravelPlan(
            required=False, reason="venue_unresolved"
        )

    fingerprint = travel.routing_fingerprint(settings, resolution)
    cached = db.venue_by_normalized(connection, resolution.normalized_venue)
    today = date.today()
    if travel.cache_is_fresh(cached, settings, fingerprint, today=today):
        metrics.venue_cache_hits += 1
        plan = travel.plan_from_cache(cached)
        resolution.venue_id = int(cached["venue_id"])
        return resolution, plan

    metrics.venue_cache_misses += 1
    call = router
    if call is None and allow_routing:
        def call(origin, destination, *, settings):  # noqa: ANN001
            metrics.route_lookups += 1
            try:
                return travel.osrm_duration_seconds(
                    origin, destination, settings=settings
                )
            except Exception:
                metrics.route_failures += 1
                raise
    elif call is None:
        def call(origin, destination, *, settings):  # noqa: ANN001
            return None

    plan = travel.plan_travel(
        resolution, attendance_mode=attendance_mode, settings=settings, router=call
    )
    try:
        resolution.venue_id = db.upsert_venue(
            connection,
            {
                "normalized_venue": resolution.normalized_venue,
                "display_name": resolution.display_name,
                "campus": resolution.campus,
                "latitude": resolution.latitude,
                "longitude": resolution.longitude,
                "source": resolution.source,
                "source_detail": resolution.source_detail,
                "travel_mode": plan.mode,
                "outbound_minutes": plan.minutes_before if plan.required else 0,
                "return_minutes": plan.minutes_after if plan.required else 0,
                "distance_metres": plan.distance_metres,
                "routing_source": plan.routing_source,
                "routing_fingerprint": fingerprint,
                "routing_estimated": plan.estimated,
            },
        )
        connection.commit()
    except Exception as exc:  # noqa: BLE001 - a cache miss is not a failure
        log.info("could not cache venue %r: %s", resolution.display_name, exc)
    return resolution, plan


def _campus_hint(row) -> str | None:
    from . import parking

    category = (_value(row, "category_title") or "").strip()
    for campus, spec in parking.CAMPUSES.items():
        if category in spec.get("categories", ()):
            return campus
    text = " ".join(
        str(_value(row, key) or "").lower()
        for key in ("title", "body_text", "event_location")
    )
    for campus, spec in parking.CAMPUSES.items():
        if any(keyword in text for keyword in spec.get("keywords", ())):
            return campus
    return None


def _contact_line(row) -> str | None:
    """The published contact, exactly as the digest already shows it."""
    parts = [
        _value(row, "contact_name"),
        _value(row, "contact_department"),
        _value(row, "contact_email"),
        _value(row, "contact_phone"),
    ]
    line = " · ".join(str(part) for part in parts if part)
    return line or None


def _record(
    submission_id, row, *, is_candidate, evidence, candidate=None, score=None,
    reason=None, method=None, model=None, attendance_mode=None, title=None,
    offer=False, resolution=None, plan=None, action=None,
    withheld_reason=None, validation_status="ok",
) -> dict:
    """One auditable row for `calendar_recommendations`."""
    return {
        "submission_id": int(submission_id),
        "version_id": _value(row, "version_id"),
        "is_event_candidate": int(bool(is_candidate)),
        "candidate_evidence": json.dumps(
            {
                "evidence": (evidence or {}).get("evidence") or [],
                "reason": (evidence or {}).get("reason"),
            },
            sort_keys=True,
        ),
        "offer_calendar": int(bool(offer)),
        "relevance_score": score,
        "relevance_reason": (reason or None) and str(reason)[:300],
        "relevance_method": method,
        "relevance_model": model,
        "attendance_mode": attendance_mode,
        "calendar_title": title,
        "event_date": candidate.event_date.isoformat() if candidate else None,
        "start_datetime": (
            candidate.start_datetime.isoformat() if candidate else None
        ),
        "end_datetime": candidate.end_datetime.isoformat() if candidate else None,
        "timezone": candidate.timezone if candidate else None,
        "location": candidate.location if candidate else None,
        "venue_id": resolution.venue_id if resolution else None,
        "travel_required": int(bool(plan.required)) if plan else 0,
        "travel_mode": plan.mode if plan else None,
        "travel_minutes_before": plan.minutes_before if plan else None,
        "travel_minutes_after": plan.minutes_after if plan else None,
        "travel_estimated": int(bool(plan.estimated)) if plan else 0,
        "mechanism": action.mechanism if action else None,
        "action_url": action.action_url if action else None,
        "ics_filename": action.ics_filename if action else None,
        "ics_bytes": len(action.ics_bytes) if action and action.ics_bytes else None,
        "validation_status": validation_status,
        "withheld_reason": withheld_reason,
        "session_count": (
            action.session_count
            if action
            else (candidate.session_count if candidate else None)
        ),
        # One auditable line per offered sitting: date, time, and which file
        # carries it. This is what makes "did the reader get two buttons?"
        # answerable from the database alone.
        "sessions": json.dumps(
            [
                {
                    "index": session.index,
                    "start": session.start.isoformat(),
                    "end": session.end.isoformat(),
                    "ics_filename": session.ics_filename,
                    "travel_minutes_before": session.travel_minutes_before,
                    "travel_minutes_after": session.travel_minutes_after,
                }
                for session in action.sessions
            ],
            sort_keys=True,
        )
        if action
        else None,
    }
