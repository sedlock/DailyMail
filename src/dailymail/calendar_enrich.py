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
            "duration_seconds": round(self.duration_seconds, 3),
            "errors": self.errors[:5],
        }


# --- deterministic relevance -------------------------------------------------
#
# Used when curation is unavailable, and as the floor under the model's
# judgement. The weights encode the reader described in the curation system
# prompt: a senior technology executive who is also a Rowan parent.

_STRONG_SIGNALS = (
    ("town hall", 0.45), ("provost", 0.40), ("president", 0.40),
    ("chancellor", 0.40), ("cabinet", 0.35), ("board of trustees", 0.35),
    ("state of the university", 0.40), ("commencement", 0.40),
    ("convocation", 0.30), ("cybersecurity", 0.40), ("information security", 0.35),
    ("data security", 0.30), ("enterprise system", 0.35), ("banner", 0.25),
    ("erp", 0.25), ("migration", 0.20), ("policy", 0.20), ("governance", 0.25),
    ("strategic plan", 0.30), ("budget", 0.25), ("open enrollment", 0.35),
    ("benefits", 0.20), ("move-in", 0.30), ("move in", 0.30),
    ("family weekend", 0.30), ("parents", 0.25), ("registration", 0.20),
    ("accreditation", 0.30), ("emergency", 0.25), ("safety", 0.20),
    ("briefing", 0.25), ("leadership", 0.25), ("all-employee", 0.35),
    ("all employees", 0.35), ("faculty and staff", 0.20), ("research", 0.15),
    ("artificial intelligence", 0.25), ("ai ", 0.15), ("information technology", 0.30),
)

_WEAK_SIGNALS = (
    ("club", -0.30), ("intramural", -0.30), ("trivia", -0.35), ("karaoke", -0.35),
    ("bingo", -0.35), ("game night", -0.35), ("tailgate", -0.25),
    ("student organization", -0.25), ("greek life", -0.30), ("sorority", -0.35),
    ("fraternity", -0.35), ("shuttle", -0.20), ("free food", -0.25),
    ("giveaway", -0.30), ("merchandise", -0.30), ("planetarium", -0.25),
    ("swim lesson", -0.35), ("intramurals", -0.30), ("welcome week", -0.20),
    ("student center patio", -0.15), ("open mic", -0.35), ("movie night", -0.35),
)

# Rowan categories whose events are, on their own, likely to matter here.
_STRONG_CATEGORIES = {
    "Official": 0.35, "Technology": 0.35, "Public Safety": 0.25,
    "Human Resources": 0.25, "Facilities": 0.20, "Glassboro Campus": 0.15,
    "Finance": 0.20, "Registrar": 0.20, "Research": 0.10,
}
_WEAK_CATEGORIES = {
    "Athletic Events": -0.30, "Clubs and Organizations": -0.35,
    "Campus Activities": -0.25, "Social and Cultural Events": -0.20,
    "Our Stories!": -0.25, "Volunteer Opportunities": -0.15,
}


def deterministic_relevance(row, candidate) -> tuple[float, str]:
    """A transparent, explainable relevance score in 0.0-1.0.

    Deliberately conservative: the base sits below the default threshold, so an
    event has to earn its button rather than merely fail to disqualify itself.
    """
    haystack = " ".join(
        str(part or "").lower()
        for part in (
            _value(row, "title"), _value(row, "body_text"), candidate.title,
            candidate.location,
        )
    )
    score = 0.30
    hits: list[str] = []

    for phrase, weight in _STRONG_SIGNALS:
        if phrase in haystack:
            score += weight
            hits.append(phrase)
    for phrase, weight in _WEAK_SIGNALS:
        if phrase in haystack:
            score += weight
            hits.append(f"-{phrase}")

    category = _value(row, "category_title") or ""
    score += _STRONG_CATEGORIES.get(category, 0.0)
    score += _WEAK_CATEGORIES.get(category, 0.0)
    if category in _STRONG_CATEGORIES or category in _WEAK_CATEGORIES:
        hits.append(f"category:{category}")

    audience = _value(row, "source_audience")
    if audience == "Employees":
        score += 0.05
    elif audience == "Students":
        score -= 0.10

    score = max(0.0, min(1.0, score))
    detail = ", ".join(hits[:6]) or "no distinguishing signal"
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

    Runs *before* curation so the relevance question can ride along on the call
    that already happens. Never raises.
    """
    candidates: dict[str, events.EventCandidate] = {}
    diagnostics: dict[str, dict] = {}
    if not settings.calendar_enabled:
        return candidates, diagnostics

    reference = date.fromisoformat(target_date)
    for row in rows:
        submission_id = str(row["submission_id"])
        try:
            candidate, diagnosis = events.detect_candidate(
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
        if candidate is not None:
            candidates[submission_id] = candidate
    return candidates, diagnostics


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
) -> tuple[dict[str, calendar_action.CalendarAction], CalendarMetrics]:
    """Turn detected candidates into validated calendar actions.

    Returns `{submission_id: CalendarAction}` plus counters. Never raises: this
    is enrichment, and a digest without a calendar button is a working digest.
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
        candidate = candidates.get(submission_id)
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
                candidate=candidate,
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
            if action.has_travel:
                metrics.travel_enriched += 1

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
    candidate,
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

    # --- build ------------------------------------------------------------
    official_url = row["official_url"]
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

    ics_text = None
    ics_filename = None
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
    if settings.calendar_attach_ics:
        ics_text = calendar_action.build_ics(
            blocks,
            timezone=candidate.timezone,
            dtstamp=calendar_action.official_ics_dtstamp(target_date),
        )
        ics_filename = calendar_action.slugify_filename(title)

    action = calendar_action.CalendarAction(
        submission_id=submission_id,
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
    }
