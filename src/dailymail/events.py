"""Deterministic detection and extraction of real, schedulable events.

Rowan's own ``Event`` boolean is *evidence*, not the answer. The Provost's Town
Hall that motivated this feature arrives with ``Event == false`` and no event
fields at all, while its body carries a fully specified date, a two-segment
schedule, a room and a hybrid attendance note. So detection is layered:

    1. Rowan's structured event fields, when it filled them in
    2. explicit labelled structure in the body (``Date:``/``Times:``/``Location:``)
    3. an unlabelled but unambiguous date + time-range pairing in the body

Everything here is deterministic. Claude never supplies a date, a time, a
location or a link; it is asked only whether an event is *relevant* and, at
most, for a cleaner title. That division is the whole security story for this
feature: the model cannot manufacture an appointment because it is never on the
path that produces one.

Validation is strict on purpose. A stated weekday must agree with the stated
date, a start must precede its end, and an announcement advertising several
independent occurrences is *not* one calendar event -- it is withheld rather
than guessed at.
"""

from __future__ import annotations

import re
import unicodedata
from html import unescape
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta

# Rowan events are Eastern unless authoritative source data says otherwise.
DEFAULT_TIMEZONE = "America/New_York"

# Rowan stores "no event time" as midnight, so a genuine midnight event is
# indistinguishable from an absent one (docs/operations.md §13). Treated as
# absent, which is the safe direction: no time block, no calendar action.
_NO_TIME = "00:00:00"

MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4,
    "saturday": 5, "sunday": 6,
    "mon": 0, "tue": 1, "tues": 1, "wed": 2, "thu": 3, "thur": 3, "thurs": 3,
    "fri": 4, "sat": 5, "sun": 6,
}

_WEEKDAY_ALTERNATION = "|".join(sorted(WEEKDAYS, key=len, reverse=True))
_MONTH_ALTERNATION = "|".join(sorted(MONTHS, key=len, reverse=True))

# "Wednesday, October 14, 2026" / "October 14 2026" / "Oct. 14, 2026"
_LONG_DATE = re.compile(
    rf"(?:(?P<weekday>{_WEEKDAY_ALTERNATION})\s*,?\s+)?"
    rf"(?P<month>{_MONTH_ALTERNATION})\.?\s+"
    r"(?P<day>\d{1,2})(?:st|nd|rd|th)?"
    r"(?:\s*,?\s*(?P<year>\d{4}))?",
    re.IGNORECASE,
)

# "10/14/2026" and "10/14/26"
_NUMERIC_DATE = re.compile(
    r"\b(?P<month>\d{1,2})/(?P<day>\d{1,2})(?:/(?P<year>\d{2,4}))?\b"
)

# "10:00 - 11:15", "10:00 AM - 12:00 PM", "2:30-4:00 p.m.", "10 a.m. to noon"
_TIME = r"(?P<{name}>\d{{1,2}})(?::(?P<{name}m>\d{{2}}))?\s*(?P<{name}p>[ap]\.?\s?m\.?)?"
_TIME_RANGE = re.compile(
    _TIME.format(name="s")
    + r"\s*(?:-|--|‐|‑|‒|–|—|to|until|till|through)\s*"
    + _TIME.format(name="e"),
    re.IGNORECASE,
)
_SINGLE_TIME = re.compile(
    r"\b(?P<h>\d{1,2})(?::(?P<m>\d{2}))\s*(?P<p>[ap]\.?\s?m\.?)?\b", re.IGNORECASE
)

# Labelled blocks Rowan submitters actually use.
_LABEL_LINE = re.compile(
    r"^\s*(date|dates|time|times|when|location|where|who|audience|cost|rsvp)\s*:\s*(.*)$",
    re.IGNORECASE,
)

# Language that makes a scheduled gathering plausible. Paired with a real
# date+time before it means anything; on its own it is worth nothing.
EVENT_LANGUAGE = (
    "town hall", "workshop", "seminar", "webinar", "symposium", "conference",
    "lecture", "panel", "forum", "open house", "info session", "information session",
    "orientation", "reception", "ceremony", "commencement", "convocation",
    "meeting", "briefing", "training", "session", "screening", "presentation",
    "kickoff", "kick-off", "fair", "showcase", "summit", "retreat", "luncheon",
    "breakfast", "dinner", "social", "celebration", "tour", "rally", "clinic",
    "book club", "coffee hour", "office hours", "q&a", "colloquium", "defense",
    "graduation", "move-in", "move in", "convening", "gala", "expo",
)

# Deadlines are not calendar events unless the source also gives a time block
# worth reserving. "Applications close October 2" belongs in the body, not on a
# calendar as an appointment.
DEADLINE_LANGUAGE = (
    "deadline", "due date", "apply by", "submit by", "closes", "closing date",
    "last day to", "no later than", "applications open", "application open",
    "nominations", "rsvp by", "register by", "renewal",
)

# A repeating class or office-hours schedule is not one appointment. `9 a.m. -
# 2:35 p.m. Monday - Thursday` is a term timetable; putting it on the calendar
# as a single block on one date would be actively wrong.
_RECURRENCE = re.compile(
    r"\b(?:"
    rf"(?:{_WEEKDAY_ALTERNATION})s\b"
    rf"|every\s+(?:{_WEEKDAY_ALTERNATION})\b"
    rf"|(?:{_WEEKDAY_ALTERNATION})\s*(?:-|--|–|—|through|thru|to)\s*"
    rf"(?:{_WEEKDAY_ALTERNATION})\b"
    r"|weekly|bi-?weekly|monthly|each\s+week|every\s+week|every\s+other\s+week"
    r"|recurring|ongoing\s+series|m\s*/\s*w\s*/\s*f|\bmwf\b|\btu?th\b"
    r")",
    re.IGNORECASE,
)


def looks_recurring(text: str) -> bool:
    """True when the schedule repeats rather than happening once."""
    return bool(_RECURRENCE.search(_fold(text)))


VIRTUAL_MARKERS = (
    "webex", "zoom", "microsoft teams", "ms teams", "teams meeting", "google meet",
    "virtual", "online", "remote", "livestream", "live stream", "webinar",
)

IN_PERSON_MARKERS = (
    "in-person", "in person", "on campus", "on-campus", "hybrid",
)

ATTENDANCE_MODES = ("in_person", "virtual", "hybrid", "unknown")


class EventExtractionError(ValueError):
    """The body looked like an event but could not be resolved into one."""


@dataclass
class TimeSegment:
    """One advertised phase of an event, e.g. ``10:00-11:15 Presentation``."""

    start: time
    end: time | None
    label: str | None = None

    def as_dict(self) -> dict:
        return {
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M") if self.end else None,
            "label": self.label,
        }


@dataclass
class EventCandidate:
    """A normalized, validated, schedulable event derived from stored state.

    Every field here comes from Rowan's own data. Nothing on this object is
    model-generated.
    """

    submission_id: str
    title: str
    event_date: date
    start: time
    end: time | None
    timezone: str = DEFAULT_TIMEZONE
    location: str | None = None
    location_detail: str | None = None
    virtual_detail: str | None = None
    attendance_mode: str = "unknown"
    segments: list[TimeSegment] = field(default_factory=list)
    registration_urls: list[str] = field(default_factory=list)
    source_urls: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    audience: str | None = None
    speaker: str | None = None
    heading_hint: str | None = None

    @property
    def start_datetime(self) -> datetime:
        return datetime.combine(self.event_date, self.start)

    @property
    def end_datetime(self) -> datetime:
        if self.end is not None:
            return datetime.combine(self.event_date, self.end)
        # No advertised end: an hour is the least surprising default and is
        # stated in the description rather than presented as source fact.
        return self.start_datetime + timedelta(hours=1)

    @property
    def end_is_assumed(self) -> bool:
        return self.end is None

    def as_dict(self) -> dict:
        return {
            "submission_id": self.submission_id,
            "title": self.title,
            "event_date": self.event_date.isoformat(),
            "start": self.start.strftime("%H:%M"),
            "end": self.end.strftime("%H:%M") if self.end else None,
            "timezone": self.timezone,
            "location": self.location,
            "attendance_mode": self.attendance_mode,
            "segments": [segment.as_dict() for segment in self.segments],
            "heading_hint": self.heading_hint,
            "evidence": list(self.evidence),
        }


# --- text helpers ------------------------------------------------------------


def _fold(text: str | None) -> str:
    """Lowercase, de-accent and normalize the punctuation authors actually use."""
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", str(text))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    for curly, straight in (("’", "'"), ("‘", "'"), ("“", '"'),
                            ("”", '"'), (" ", " ")):
        folded = folded.replace(curly, straight)
    return folded.lower()


def _year_for(month: int, day: int, *, reference: date) -> int:
    """Pick the year an author omitted: the next occurrence, not the past one."""
    for year in (reference.year, reference.year + 1):
        try:
            candidate = date(year, month, day)
        except ValueError:
            continue
        if candidate >= reference - timedelta(days=1):
            return year
    return reference.year


def parse_dates(text: str, *, reference: date) -> list[tuple[date, bool]]:
    """Every distinct date in `text`, as `(date, weekday_was_stated_and_agreed)`.

    A stated weekday that contradicts the stated date is a contradiction, not a
    detail to smooth over: the date is dropped so the announcement cannot become
    a confidently wrong calendar entry.
    """
    found: dict[date, bool] = {}
    folded = _fold(text)

    for match in _LONG_DATE.finditer(folded):
        month = MONTHS.get(match.group("month").lower().rstrip("."))
        if month is None:
            continue
        try:
            day = int(match.group("day"))
        except (TypeError, ValueError):
            continue
        year_text = match.group("year")
        year = int(year_text) if year_text else _year_for(month, day, reference=reference)
        try:
            parsed = date(year, month, day)
        except ValueError:
            continue
        weekday_text = match.group("weekday")
        if weekday_text is not None:
            expected = WEEKDAYS.get(weekday_text.lower().rstrip("."))
            if expected is not None and expected != parsed.weekday():
                continue  # contradictory: refuse it outright
            found[parsed] = True
        else:
            found.setdefault(parsed, False)

    for match in _NUMERIC_DATE.finditer(folded):
        month, day = int(match.group("month")), int(match.group("day"))
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue
        year_text = match.group("year")
        if year_text:
            year = int(year_text)
            if year < 100:
                year += 2000
        else:
            year = _year_for(month, day, reference=reference)
        try:
            parsed = date(year, month, day)
        except ValueError:
            continue
        found.setdefault(parsed, False)

    return sorted(found.items())


def _meridiem(raw: str | None) -> str | None:
    if not raw:
        return None
    return "pm" if "p" in raw.lower() else "am"


def _resolve_hour(hour: int, meridiem: str | None) -> int | None:
    if not (0 <= hour <= 23):
        return None
    if meridiem == "am":
        return 0 if hour == 12 else hour
    if meridiem == "pm":
        return hour if hour == 12 else hour + 12
    if hour >= 13:
        return hour  # already 24-hour
    # No meridiem. University events cluster 8am-7pm; 1-7 reads as afternoon,
    # 8-11 as morning, 12 as noon.
    if hour == 12:
        return 12
    if 1 <= hour <= 7:
        return hour + 12
    return hour


def parse_time_ranges(text: str) -> list[TimeSegment]:
    """Every `start-end` time range in `text`, in the order they appear."""
    segments: list[TimeSegment] = []
    for line in _fold(text).splitlines():
        for match in _TIME_RANGE.finditer(line):
            start_meridiem = _meridiem(match.group("sp"))
            end_meridiem = _meridiem(match.group("ep"))
            # "10:00 - 11:15 AM": the trailing meridiem governs both ends.
            if start_meridiem is None and end_meridiem is not None:
                start_meridiem = end_meridiem
            start_hour = _resolve_hour(int(match.group("s")), start_meridiem)
            end_hour = _resolve_hour(int(match.group("e")), end_meridiem)
            if start_hour is None or end_hour is None:
                continue
            start_minute = int(match.group("sm") or 0)
            end_minute = int(match.group("em") or 0)
            if start_minute > 59 or end_minute > 59:
                continue
            try:
                start = time(start_hour, start_minute)
                end = time(end_hour, end_minute)
            except ValueError:
                continue
            if end <= start:
                # "11:15 - 12:00" with no meridiem: only a 12-hour rollover can
                # make this coherent, and only if it stays inside the same day.
                if end_meridiem is None and end_hour + 12 <= 23:
                    end = time(end_hour + 12, end_minute)
                if end <= start:
                    continue
            label = _segment_label(line, match.end())
            segments.append(TimeSegment(start=start, end=end, label=label))
    return _dedupe_segments(segments)


def _dedupe_segments(segments: list[TimeSegment]) -> list[TimeSegment]:
    """One entry per distinct span. Bodies repeat their own times constantly.

    A later repeat that carries a phase label beats an earlier bare one, so
    `10:00-11:15` followed by `10:00-11:15 - Presentation` keeps the label.
    """
    unique: dict[tuple, TimeSegment] = {}
    for segment in segments:
        key = (segment.start, segment.end)
        existing = unique.get(key)
        if existing is None or (existing.label is None and segment.label):
            unique[key] = segment
    return list(unique.values())


def _segment_label(line: str, offset: int) -> str | None:
    """The phase name an author writes after a time range: `10-11 - Q&A`."""
    tail = line[offset:].strip()
    tail = tail.lstrip("-–—:·|• ").strip()
    tail = re.sub(r"\s+", " ", tail)
    if not tail or len(tail) > 60:
        return None
    if not re.search(r"[a-z]", tail):
        return None
    return tail


def merge_segments(segments: list[TimeSegment]) -> tuple[time, time | None]:
    """Collapse consecutive phases into the one span the calendar should hold.

    `10:00-11:15 Presentation` followed by `11:15-12:00 Social` is one 10:00-12:00
    block with its internal schedule preserved in the description -- not two
    separate calendar items competing for the same morning.
    """
    if not segments:
        raise EventExtractionError("no time segments to merge")
    ordered = sorted(segments, key=lambda seg: (seg.start, seg.end or seg.start))
    start = ordered[0].start
    end = ordered[0].end
    for segment in ordered[1:]:
        segment_end = segment.end or segment.start
        current_end = end or start
        # Only phases that touch or overlap the running span belong to it.
        if segment.start <= current_end:
            if segment_end > current_end:
                end = segment_end
        else:
            gap = _minutes_between(current_end, segment.start)
            if gap <= 60:
                end = segment_end if segment_end > current_end else current_end
            else:
                break  # a genuinely separate sitting; do not swallow it
    return start, end


def segments_are_sequential(segments: list[TimeSegment]) -> bool:
    """True when the phases form one chain rather than competing schedules.

    `10:00-11:15` then `11:15-12:00` is one event in two parts. `9:00-14:35`
    alongside `10:00-13:00` is two different timetables in the same
    announcement, and merging them would invent a block neither one describes.
    """
    ordered = sorted(segments, key=lambda seg: (seg.start, seg.end or seg.start))
    for previous, current in zip(ordered, ordered[1:]):
        previous_end = previous.end or previous.start
        if current.start < previous_end:
            return False
    return True


def _minutes_between(earlier: time, later: time) -> int:
    return (later.hour * 60 + later.minute) - (earlier.hour * 60 + earlier.minute)


# --- attendance mode ---------------------------------------------------------


def detect_attendance(text: str, location: str | None) -> tuple[str, str | None, str | None]:
    """Classify how the reader would attend. Returns `(mode, physical, virtual)`.

    "Remote" here means physical travel, never "virtual": a virtual-only event
    needs no travel hold, an in-person one does, and a hybrid needs a judgement
    call the source usually makes for us with "in-person preferred".
    """
    folded = _fold(text)
    location_folded = _fold(location)
    has_virtual = any(marker in folded for marker in VIRTUAL_MARKERS)
    physical = _physical_location(text, location)
    has_physical = physical is not None

    if "hybrid" in folded or (has_virtual and has_physical):
        return "hybrid", physical, _virtual_detail(text)
    if has_virtual and not has_physical:
        return "virtual", None, _virtual_detail(text)
    if has_physical:
        return "in_person", physical, None
    return "unknown", None, _virtual_detail(text) if has_virtual else None


def _virtual_detail(text: str) -> str | None:
    folded = _fold(text)
    for marker in ("webex", "zoom", "microsoft teams", "ms teams", "google meet",
                   "livestream", "live stream"):
        if marker in folded:
            return {
                "webex": "WebEx",
                "zoom": "Zoom",
                "microsoft teams": "Microsoft Teams",
                "ms teams": "Microsoft Teams",
                "google meet": "Google Meet",
                "livestream": "Livestream",
                "live stream": "Livestream",
            }[marker]
    if "virtual" in folded or "online" in folded:
        return "Virtual"
    return None


# Words that mean "this is not a room": a bare "Hybrid" or "Virtual" on a
# Location: line is an attendance mode, not somewhere to walk to.
_NON_PHYSICAL_LOCATIONS = {
    "hybrid", "virtual", "online", "remote", "webex", "zoom", "teams",
    "microsoft teams", "google meet", "tbd", "tba", "n/a", "various",
    "virtual event", "online event", "livestream", "web", "webinar",
}


def _physical_location(text: str, explicit: str | None) -> str | None:
    """The best physical place named, or None if the event has no room."""
    if explicit:
        cleaned = " ".join(str(explicit).split())
        if cleaned and _fold(cleaned).strip(" .") not in _NON_PHYSICAL_LOCATIONS:
            return cleaned

    for raw_line in (text or "").splitlines():
        line = " ".join(raw_line.split())
        if not line:
            continue
        match = _LABEL_LINE.match(line)
        if match and match.group(1).lower() in ("location", "where"):
            value = match.group(2).strip()
            if value and _fold(value).strip(" .") not in _NON_PHYSICAL_LOCATIONS:
                return _strip_parenthetical(value)
            continue
        # A "Location: Hybrid" header is routinely followed by the actual room
        # on its own bullet, often with "(in-person preferred)" after it.
        if re.search(r"\(\s*in[- ]person(?:\s+preferred)?\s*\)", line, re.IGNORECASE):
            candidate = _strip_parenthetical(line)
            if candidate and _fold(candidate).strip(" .") not in _NON_PHYSICAL_LOCATIONS:
                return candidate
    return None


def _strip_parenthetical(value: str) -> str:
    return " ".join(re.sub(r"\([^)]*\)", " ", value).split()).strip(" ,;:-")


# --- labelled field extraction ----------------------------------------------


# A labelled block runs until the next label or a blank line. Authors do not
# always leave that blank line, so the run is also bounded: without a cap, a
# trailing `Who:` swallows the entire remainder of the announcement.
_MAX_LABEL_LINES = {"date": 3, "dates": 6, "time": 6, "times": 6, "when": 6,
                    "location": 6, "where": 6, "who": 1, "audience": 1,
                    "cost": 1, "rsvp": 2}
_MAX_LABEL_CHARS = 400


def labelled_fields(text: str) -> dict[str, str]:
    """`Date:`/`Times:`/`Location:`/`Who:` blocks, each with its trailing lines."""
    fields: dict[str, list[str]] = {}
    current: str | None = None
    for raw_line in (text or "").splitlines():
        line = " ".join(raw_line.split())
        if not line:
            current = None
            continue
        match = _LABEL_LINE.match(line)
        if match:
            current = match.group(1).lower()
            fields.setdefault(current, [])
            value = match.group(2).strip()
            if value:
                fields[current].append(value)
            continue
        if current is not None:
            if len(fields[current]) >= _MAX_LABEL_LINES.get(current, 6):
                current = None
                continue
            fields[current].append(line)
    return {
        key: "\n".join(values).strip()[:_MAX_LABEL_CHARS]
        for key, values in fields.items()
        if values
    }


# --- candidate detection -----------------------------------------------------


def _row_value(row, key, default=None):
    try:
        value = row[key]
    except (KeyError, IndexError, TypeError):
        return default
    return default if value is None else value


def _parse_source_time(raw: str | None) -> time | None:
    if not raw or raw == _NO_TIME:
        return None
    for pattern in ("%H:%M:%S", "%H:%M"):
        try:
            return datetime.strptime(raw, pattern).time()
        except ValueError:
            continue
    return None


def extract_urls(html: str | None) -> list[str]:
    """Absolute http(s) links in source order, de-duplicated.

    Only what the announcement itself published. Nothing is ever synthesized:
    a registration URL that is not in the source does not exist.
    """
    if not html:
        return []
    seen: list[str] = []
    for match in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', html, re.IGNORECASE):
        # Stored source HTML is entity-encoded; a Safelinks wrapper arrives full
        # of `&amp;` and is unusable until decoded.
        href = unescape(match.group(1).strip())
        if not href.lower().startswith(("http://", "https://")):
            continue
        if href not in seen:
            seen.append(href)
    return seen


_REGISTRATION_HINTS = (
    "register", "registration", "rsvp", "sign up", "signup", "reserve",
    "tickets", "ticket", "enroll",
)


def registration_urls(html: str | None) -> list[str]:
    """Links whose own anchor text or URL says they are for registering."""
    if not html:
        return []
    found: list[str] = []
    for match in re.finditer(
        r'<a\b[^>]*href\s*=\s*["\']([^"\']+)["\'][^>]*>(.*?)</a>',
        html, re.IGNORECASE | re.DOTALL,
    ):
        href = unescape(match.group(1).strip())
        if not href.lower().startswith(("http://", "https://")):
            continue
        anchor = _fold(unescape(re.sub(r"<[^>]+>", " ", match.group(2))))
        haystack = f"{anchor} {_fold(href)}"
        if any(hint in haystack for hint in _REGISTRATION_HINTS):
            if href not in found:
                found.append(href)
    return found


_FIRST_HEADING = re.compile(
    r"<h[1-6]\b[^>]*>(.*?)</h[1-6]>", re.IGNORECASE | re.DOTALL
)


def first_heading(html: str | None) -> str | None:
    """The announcement's own display heading, verbatim from the source.

    Rowan bodies routinely open with a heading that is a better event name than
    the Announcer subject -- `Provost's Town Hall & Social` under a subject of
    `Provost's Town Hall - Oct 14`. It is offered to curation as a *hint* so a
    cleaner title can be chosen from words the announcement actually used.
    Nothing here invents text.
    """
    if not html:
        return None
    match = _FIRST_HEADING.search(html)
    if not match:
        return None
    text = unescape(re.sub(r"<[^>]+>", " ", match.group(1)))
    text = " ".join(text.split())
    if not text or len(text) > 120 or not re.search(r"[A-Za-z]", text):
        return None
    return text


def detect_candidate(row, *, reference_date: date) -> tuple[EventCandidate | None, dict]:
    """Decide whether one stored announcement is a schedulable event.

    Returns `(candidate_or_None, diagnostics)`. Diagnostics always explain the
    decision so `calendar_recommendations` can record *why* nothing was offered,
    which is what makes the threshold tunable later without guesswork.
    """
    submission_id = str(_row_value(row, "submission_id", ""))
    title = " ".join(str(_row_value(row, "title", "") or "").split())
    body_text = str(_row_value(row, "body_text", "") or "")
    full_body = str(_row_value(row, "full_body", "") or "")
    if not body_text:
        body_text = re.sub(r"<[^>]+>", " ", full_body)

    diagnostics: dict = {"evidence": [], "reason": None}
    evidence: list[str] = diagnostics["evidence"]

    source_is_event = bool(_row_value(row, "is_event", 0))
    source_date_raw = _row_value(row, "event_date")
    source_start = _parse_source_time(_row_value(row, "event_start_time"))
    source_end = _parse_source_time(_row_value(row, "event_end_time"))
    source_location = _row_value(row, "event_location")
    source_name = _row_value(row, "event_name")

    if source_is_event:
        evidence.append("source_event_flag")
    if source_date_raw:
        evidence.append("source_event_date")
    if source_start:
        evidence.append("source_event_start_time")

    searchable = f"{title}\n{body_text}"
    folded = _fold(searchable)
    fields = labelled_fields(body_text)
    if any(key in fields for key in ("date", "dates", "time", "times", "when")):
        evidence.append("labelled_body_schedule")
    if any(phrase in folded for phrase in EVENT_LANGUAGE):
        evidence.append("event_language")

    # --- the date -----------------------------------------------------------
    event_date: date | None = None
    if source_date_raw:
        try:
            event_date = date.fromisoformat(str(source_date_raw)[:10])
        except ValueError:
            event_date = None

    body_dates = parse_dates(
        "\n".join(filter(None, [fields.get("date"), fields.get("dates"),
                                fields.get("when"), title, body_text])),
        reference=reference_date,
    )
    labelled_dates = parse_dates(
        "\n".join(filter(None, [fields.get("date"), fields.get("dates"),
                                fields.get("when")])),
        reference=reference_date,
    )

    if event_date is None:
        pool = labelled_dates or body_dates
        # An announcement advertising several independent occurrences (two
        # coffee hours a fortnight apart) is not one appointment. Withhold
        # rather than silently pick one.
        distinct = {value for value, _ in pool}
        if len(distinct) > 1:
            diagnostics["reason"] = "multiple_distinct_dates"
            return None, diagnostics
        if pool:
            event_date = pool[0][0]
            evidence.append(
                "body_date_labelled" if labelled_dates else "body_date"
            )
    else:
        # Rowan gave us the date; a contradicting body date is a red flag worth
        # recording, but the structured field stays authoritative.
        if body_dates and event_date not in {value for value, _ in body_dates}:
            evidence.append("body_date_differs_from_source")

    if event_date is None:
        diagnostics["reason"] = "no_event_date"
        return None, diagnostics

    if any(stated for value, stated in body_dates if value == event_date):
        evidence.append("weekday_confirmed")

    # --- the time block -----------------------------------------------------
    schedule_text = "\n".join(
        filter(None, [fields.get("time"), fields.get("times"), fields.get("when")])
    )
    segments = parse_time_ranges(schedule_text) if schedule_text else []
    if segments:
        evidence.append("labelled_time_range")
    else:
        segments = parse_time_ranges(body_text)
        if segments:
            evidence.append("body_time_range")

    start: time | None = source_start
    end: time | None = source_end
    if start is not None:
        evidence.append("source_time_block")
        if segments:
            merged_start, merged_end = merge_segments(segments)
            # Rowan's own start wins; the body may still extend the end.
            if merged_start == start and merged_end and (end is None or merged_end > end):
                end = merged_end
    elif segments:
        start, end = merge_segments(segments)
    else:
        single = _first_single_time(schedule_text or body_text)
        if single is not None:
            start, end = single, None
            evidence.append("single_time")

    if start is None:
        diagnostics["reason"] = "no_event_time"
        return None, diagnostics

    if end is not None and end <= start:
        diagnostics["reason"] = "end_before_start"
        return None, diagnostics

    # A repeating timetable is not an appointment. Rowan's own structured event
    # fields override this: if it filled in a specific date and start time, it
    # has already told us which occurrence it means.
    if source_start is None and looks_recurring(
        schedule_text or body_text
    ):
        diagnostics["reason"] = "recurring_schedule"
        return None, diagnostics

    if len(segments) > 1 and not segments_are_sequential(segments):
        diagnostics["reason"] = "overlapping_time_blocks"
        return None, diagnostics

    # --- is this actually an event, or just a dated deadline? ---------------
    is_deadline = any(phrase in folded for phrase in DEADLINE_LANGUAGE)
    strong = {
        "source_event_flag", "source_event_date", "source_event_start_time",
        "labelled_body_schedule", "labelled_time_range", "event_language",
    } & set(evidence)
    if not strong:
        diagnostics["reason"] = "no_event_evidence"
        return None, diagnostics
    if is_deadline and not ({"source_event_flag", "labelled_time_range",
                             "labelled_body_schedule"} & set(evidence)):
        diagnostics["reason"] = "deadline_not_event"
        return None, diagnostics

    mode, physical, virtual = detect_attendance(searchable, source_location)
    if fields.get("location") and _fold(fields["location"]).startswith("hybrid"):
        mode = "hybrid"

    candidate = EventCandidate(
        submission_id=submission_id,
        title=" ".join(str(source_name or title).split()) or title,
        event_date=event_date,
        start=start,
        end=end,
        location=physical,
        location_detail=fields.get("location") or (source_location or None),
        virtual_detail=virtual,
        attendance_mode=mode,
        segments=segments,
        registration_urls=registration_urls(full_body),
        source_urls=extract_urls(full_body),
        evidence=sorted(set(evidence)),
        audience=fields.get("who") or fields.get("audience"),
        heading_hint=first_heading(full_body),
    )
    diagnostics["candidate"] = candidate.as_dict()
    return candidate, diagnostics


def _first_single_time(text: str) -> time | None:
    for line in _fold(text).splitlines():
        match = _SINGLE_TIME.search(line)
        if not match:
            continue
        hour = _resolve_hour(int(match.group("h")), _meridiem(match.group("p")))
        minute = int(match.group("m") or 0)
        if hour is None or minute > 59:
            continue
        try:
            return time(hour, minute)
        except ValueError:
            continue
    return None
