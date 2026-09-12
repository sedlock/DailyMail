"""Building the actual `Add to Calendar` action, deterministically.

Two mechanisms, chosen after checking what Microsoft actually documents.

**The `.ics` attachment is the authoritative one.** RFC 5545 iCalendar is a real
standard, and Microsoft documents its own handling of it in [MS-STANOICAL] --
including, usefully, that Outlook itself exports `.ics` files attached to mail
with a `Content-Type` of `application/octet-stream`, reserving `text/calendar`
for iMIP scheduling messages. That distinction matters here: a `text/calendar`
part would turn the whole digest into a meeting request, and MS-STANOICAL V0343
confirms Outlook treats only the *first* such part as scheduling data anyway. So
each offered event is attached the way Outlook itself would attach one, the
digest stays a newsletter, and opening the attachment imports every VEVENT in
it -- which is what lets travel holds travel with the event.

**The button is an Outlook deep link**, because a button has to be a hyperlink
and a hyperlink cannot address a MIME part (`cid:` works for `<img src>`, not
for opening an attachment). `outlook.office.com/calendar/deeplink/compose` opens
the Microsoft 365 calendar composer pre-filled, and the reader presses Save. It
is community-documented rather than Microsoft-documented, so it is deliberately
*not* the only mechanism: it carries the single real event at its real time, and
the standards-based attachment beside it carries the complete import including
travel. That is the documented compromise.

Nothing here is model-generated. Every value is escaped, every URL comes from
the source announcement, and every string is stripped of the control characters
that make header and MIME injection possible.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import quote, urlencode

from .events import EventCandidate

PRODID = "-//DailyMail//Rowan Announcer digest//EN"
ICS_VERSION = "2.0"
METHOD = "PUBLISH"

OUTLOOK_COMPOSE_URL = "https://outlook.office.com/calendar/deeplink/compose"

MECHANISM_DEEPLINK = "outlook_deeplink"
MECHANISM_ICS = "ics_attachment"
MECHANISM_BOTH = "outlook_deeplink+ics_attachment"

# Outlook's composer tolerates far more, but a URL this long stops being a
# reliable mail-client hyperlink. The description is trimmed, never the event.
MAX_ACTION_URL_CHARS = 1900

# Control characters that must never survive into a header, a filename or an
# iCalendar property. Stripped rather than escaped: none of them are legitimate
# in announcement prose, and every one of them is a way to inject a new line.
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def sanitize_text(value: str | None, *, limit: int | None = None) -> str:
    """Remove control characters and normalize newlines. Injection-safe."""
    if value is None:
        return ""
    text = _CONTROL.sub("", str(value))
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = "\n".join(line.rstrip() for line in text.split("\n")).strip()
    if limit is not None and len(text) > limit:
        text = text[:limit].rstrip() + "…"
    return text


def escape_ics(value: str) -> str:
    """RFC 5545 §3.3.11 TEXT escaping.

    Backslash first, or the escapes we add would themselves be escaped.
    """
    return (
        value.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_line(line: str) -> str:
    """RFC 5545 §3.1 content line folding at 75 **octets**, not characters.

    Folding on characters would split a multi-byte sequence and produce a file
    some parsers reject outright, so this counts UTF-8 bytes and never breaks
    inside one.
    """
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
        return line
    pieces: list[str] = []
    buffer = bytearray()
    limit = 75
    for char in line:
        char_bytes = char.encode("utf-8")
        if len(buffer) + len(char_bytes) > limit:
            pieces.append(buffer.decode("utf-8"))
            buffer = bytearray()
            limit = 74  # continuation lines carry a leading space
        buffer.extend(char_bytes)
    if buffer:
        pieces.append(buffer.decode("utf-8"))
    return "\r\n ".join(pieces)


def _stamp(value: datetime) -> str:
    return value.strftime("%Y%m%dT%H%M%S")


def slugify_filename(
    value: str, *, fallback: str = "event", suffix: str | None = None
) -> str:
    """A safe, predictable attachment filename. No path, no CR/LF, no quotes.

    `suffix` is appended verbatim after slugification and reserved out of the
    length budget, so a multi-session announcement produces
    `provost-s-coffee-hours-focus-on-research-2026-09-10.ics` and
    `...-2026-09-21.ics` rather than two files the reader cannot tell apart.
    """
    tail = ""
    if suffix:
        tail = "-" + re.sub(r"[^A-Za-z0-9]+", "-", _CONTROL.sub("", suffix)).strip("-")
    budget = max(8, 60 - len(tail))
    cleaned = _CONTROL.sub("", value or "")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", cleaned).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)[:budget].strip("-")
    return f"{cleaned or fallback}{tail}.ics"


# --- VTIMEZONE ---------------------------------------------------------------
#
# A local event is not a UTC instant: it is 10:00 in Glassboro. Emitting a TZID
# with a real VTIMEZONE keeps that true no matter where the reader's device is,
# and Outlook resolves it directly. The US rules below have been stable since
# the Energy Policy Act took effect in 2007.
_VTIMEZONE_AMERICA_NEW_YORK = [
    "BEGIN:VTIMEZONE",
    "TZID:America/New_York",
    "X-LIC-LOCATION:America/New_York",
    "BEGIN:DAYLIGHT",
    "TZOFFSETFROM:-0500",
    "TZOFFSETTO:-0400",
    "TZNAME:EDT",
    "DTSTART:20070311T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=3;BYDAY=2SU",
    "END:DAYLIGHT",
    "BEGIN:STANDARD",
    "TZOFFSETFROM:-0400",
    "TZOFFSETTO:-0500",
    "TZNAME:EST",
    "DTSTART:20071104T020000",
    "RRULE:FREQ=YEARLY;BYMONTH=11;BYDAY=1SU",
    "END:STANDARD",
    "END:VTIMEZONE",
]

_VTIMEZONES = {"America/New_York": _VTIMEZONE_AMERICA_NEW_YORK}


@dataclass
class CalendarBlock:
    """One VEVENT: the real event, or a travel hold either side of it."""

    uid: str
    summary: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    url: str | None = None
    transparent: bool = False
    kind: str = "event"
    # URLs DailyMail deliberately placed in the description, and therefore the
    # only ones that may render as clickable links in the appointment body.
    linkable_urls: frozenset[str] = frozenset()

    def lines(self, *, timezone: str, dtstamp: datetime) -> list[str]:
        tz_prefix = f";TZID={timezone}" if timezone in _VTIMEZONES else ""
        out = [
            "BEGIN:VEVENT",
            f"UID:{self.uid}",
            f"DTSTAMP:{_stamp(dtstamp)}Z",
            f"DTSTART{tz_prefix}:{_stamp(self.start)}",
            f"DTEND{tz_prefix}:{_stamp(self.end)}",
            f"SUMMARY:{escape_ics(self.summary)}",
        ]
        if self.description:
            out.append(f"DESCRIPTION:{escape_ics(self.description)}")
            # Outlook renders X-ALT-DESC when present, which is what keeps the
            # registration and announcement links clickable in the appointment.
            out.append(
                "X-ALT-DESC;FMTTYPE=text/html:"
                + escape_ics(
                    _html_description(
                        self.description, linkable=self.linkable_urls
                    )
                )
            )
        if self.location:
            out.append(f"LOCATION:{escape_ics(self.location)}")
        if self.url:
            out.append(f"URL:{escape_ics(self.url)}")
        out.append("TRANSP:TRANSPARENT" if self.transparent else "TRANSP:OPAQUE")
        out.append(f"X-DAILYMAIL-BLOCK:{self.kind}")
        out.append("END:VEVENT")
        return out


_URL_IN_TEXT = re.compile(r"https?://[^\s<>\"]+")


def _html_description(text: str, *, linkable: frozenset[str] = frozenset()) -> str:
    """A minimal HTML rendering of the description. Only known URLs become links.

    The description quotes the announcement, and announcement prose is untrusted
    third-party text that can contain a URL nobody chose to publish as a link.
    The email itself only ever renders an anchor the source actually authored,
    so this matches that: a URL is linkified only if it is one DailyMail put
    there deliberately -- the registration links and the official Rowan page.
    Everything else stays inert, escaped text.
    """
    from html import escape as html_escape

    def replace(match: re.Match) -> str:
        raw = match.group(0)
        trimmed = raw.rstrip(".,;:)")
        tail = raw[len(trimmed):]
        if trimmed not in linkable:
            return html_escape(raw, quote=False)
        safe = html_escape(trimmed, quote=True)
        return f'<a href="{safe}">{safe}</a>{html_escape(tail, quote=False)}'

    parts: list[str] = []
    cursor = 0
    for match in _URL_IN_TEXT.finditer(text):
        parts.append(html_escape(text[cursor:match.start()], quote=False))
        parts.append(replace(match))
        cursor = match.end()
    parts.append(html_escape(text[cursor:], quote=False))
    return "<html><body>" + "".join(parts).replace("\n", "<br>") + "</body></html>"


_MODE_LABELS = {
    "in_person": "In person",
    "virtual": "Virtual",
    "hybrid": "In person / Hybrid",
}


@dataclass
class CalendarSession:
    """One selectable sitting: its own button, its own `.ics`, its own travel.

    An announcement that advertises a choice of dates produces one of these per
    date. Everything the reader acts on lives here rather than on the series, so
    a two-session Coffee Hours announcement cannot end up with one ambiguous
    button and one ambiguous attachment.
    """

    index: int
    count: int
    title: str
    start: datetime
    end: datetime
    timezone: str
    location: str | None
    attendance_mode: str
    description: str
    action_url: str
    mechanism: str
    ics_text: str | None = None
    ics_filename: str | None = None
    travel_minutes_before: int = 0
    travel_minutes_after: int = 0
    travel_mode: str = "none"
    travel_estimated: bool = False
    blocks: list[CalendarBlock] = field(default_factory=list)

    @property
    def has_travel(self) -> bool:
        return bool(self.travel_minutes_before or self.travel_minutes_after)

    @property
    def ics_bytes(self) -> bytes | None:
        return self.ics_text.encode("utf-8") if self.ics_text else None

    @property
    def when_line(self) -> str:
        """`Wed, Oct 14 · 10:00 AM–12:00 PM`, compact enough for a phone."""
        day = self.start.strftime("%a, %b %-d")
        return f"{day} · {_clock(self.start)}–{_clock(self.end)}"

    @property
    def day_label(self) -> str:
        """`Thu, Sep 10` -- the shortest unambiguous label for a button."""
        return self.start.strftime("%a, %b %-d")

    @property
    def button_label(self) -> str:
        """What the tap target says.

        A single-session announcement keeps the wording production already uses.
        A series names its date instead, because "Add to Calendar" twice in one
        card tells the reader nothing about which sitting they are choosing.
        """
        if self.count <= 1:
            return "Add to Calendar"
        return f"Add {self.day_label}"

    @property
    def travel_line(self) -> str | None:
        """One compact line, complete in itself so a template never appends."""
        if not self.has_travel:
            return None
        if self.travel_minutes_before == self.travel_minutes_after:
            line = f"Includes {self.travel_minutes_before} min travel before and after"
        else:
            line = (
                f"Includes {self.travel_minutes_before} min travel before and "
                f"{self.travel_minutes_after} min after"
            )
        return f"{line} (estimated)" if self.travel_estimated else line

    @property
    def attachment_line(self) -> str | None:
        if not self.ics_filename:
            return None
        suffix = " — includes the travel holds" if self.has_travel else ""
        return f"Attached: {self.ics_filename}{suffix}"

    @property
    def mode_label(self) -> str:
        return _MODE_LABELS.get(self.attendance_mode, "")

    @property
    def location_line(self) -> str | None:
        parts = [part for part in (self.location, self.mode_label) if part]
        return " · ".join(parts) or None


@dataclass
class CalendarAction:
    """One announcement's calendar offer: the series, and its sessions.

    The top-level fields describe the *first* session and are what the renderer,
    the mailer, the audit trail and every existing caller already read. `sessions`
    is the authoritative list; for the overwhelmingly common single-session case
    it holds exactly one entry synthesized from those same fields, so nothing
    that predates multi-session support has to know the concept exists.
    """

    submission_id: str
    title: str
    start: datetime
    end: datetime
    timezone: str
    location: str | None
    attendance_mode: str
    description: str
    action_url: str
    mechanism: str
    ics_text: str | None = None
    ics_filename: str | None = None
    travel_minutes_before: int = 0
    travel_minutes_after: int = 0
    travel_mode: str = "none"
    travel_estimated: bool = False
    blocks: list[CalendarBlock] = field(default_factory=list)
    registration_urls: list[str] = field(default_factory=list)
    official_url: str | None = None
    # Set only for a genuine series. Left empty for the overwhelmingly common
    # single-session announcement, whose one session is *derived* from the
    # fields above on every access rather than copied -- so the two can never
    # drift apart, and existing callers that build or adjust a CalendarAction
    # directly go on working without knowing sessions exist.
    session_list: list[CalendarSession] = field(default_factory=list, repr=False)

    @property
    def sessions(self) -> list[CalendarSession]:
        """Every sitting the reader can act on. Never empty."""
        return self.session_list or [self._head_session()]

    def _head_session(self) -> CalendarSession:
        return CalendarSession(
            index=1, count=1, title=self.title, start=self.start,
            end=self.end, timezone=self.timezone, location=self.location,
            attendance_mode=self.attendance_mode,
            description=self.description, action_url=self.action_url,
            mechanism=self.mechanism, ics_text=self.ics_text,
            ics_filename=self.ics_filename,
            travel_minutes_before=self.travel_minutes_before,
            travel_minutes_after=self.travel_minutes_after,
            travel_mode=self.travel_mode,
            travel_estimated=self.travel_estimated,
            blocks=list(self.blocks),
        )

    @classmethod
    def from_sessions(
        cls,
        *,
        submission_id: str,
        title: str,
        timezone: str,
        location: str | None,
        attendance_mode: str,
        sessions: list[CalendarSession],
        registration_urls: list[str] | None = None,
        official_url: str | None = None,
    ) -> CalendarAction:
        """Build a series from its sessions, mirroring the first onto the series."""
        if not sessions:
            raise ValueError("a calendar action needs at least one session")
        first = sessions[0]
        return cls(
            submission_id=submission_id,
            title=title,
            start=first.start,
            end=first.end,
            timezone=timezone,
            location=location,
            attendance_mode=attendance_mode,
            description=first.description,
            action_url=first.action_url,
            mechanism=first.mechanism,
            ics_text=first.ics_text,
            ics_filename=first.ics_filename,
            travel_minutes_before=first.travel_minutes_before,
            travel_minutes_after=first.travel_minutes_after,
            travel_mode=first.travel_mode,
            travel_estimated=first.travel_estimated,
            blocks=list(first.blocks),
            registration_urls=list(registration_urls or []),
            official_url=official_url,
            session_list=list(sessions) if len(sessions) > 1 else [],
        )

    @property
    def session_count(self) -> int:
        return len(self.sessions)

    @property
    def is_multi_session(self) -> bool:
        return len(self.sessions) > 1

    @property
    def sessions_line(self) -> str | None:
        """`2 sessions — choose the one you will attend`, or nothing at all."""
        if not self.is_multi_session:
            return None
        return (
            f"{len(self.sessions)} sessions — choose the one you will attend"
        )

    @property
    def has_travel(self) -> bool:
        return any(session.has_travel for session in self.sessions)

    @property
    def ics_bytes(self) -> bytes | None:
        return self.ics_text.encode("utf-8") if self.ics_text else None

    # --- display helpers, used by the templates ---------------------------
    #
    # These describe the *first* sitting, computed from this object's own fields
    # so that adjusting one is immediately visible. The templates render from
    # `sessions` instead, because that is what the reader chooses between.
    @property
    def when_line(self) -> str:
        return self._head_session().when_line

    @property
    def travel_line(self) -> str | None:
        return self._head_session().travel_line

    @property
    def attachment_line(self) -> str | None:
        return self._head_session().attachment_line

    @property
    def mode_label(self) -> str:
        return _MODE_LABELS.get(self.attendance_mode, "")

    @property
    def location_line(self) -> str | None:
        parts = [part for part in (self.location, self.mode_label) if part]
        return " · ".join(parts) or None


def _clock(value: datetime) -> str:
    """`10:00 AM`. Minutes are always shown: this is an action, not prose."""
    return value.strftime("%-I:%M %p")


# --- title cleanup -----------------------------------------------------------
#
# A calendar title has to be recognizable in a crowded week, so it sheds the
# artefacts an announcement subject carries and the calendar already supplies.
# This is subtractive only: it removes source text, it never adds any, so it
# cannot introduce a claim the announcement did not make. The model may propose
# a better title on top of this, but only from words already in the source.

_MONTHS_RE = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?"
    r"|aug(?:ust)?|sep(?:t)?(?:ember)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_WEEKDAYS_RE = (
    r"mon(?:day)?|tue(?:s)?(?:day)?|wed(?:nesday)?|thu(?:r)?(?:s)?(?:day)?"
    r"|fri(?:day)?|sat(?:urday)?|sun(?:day)?"
)

# A trailing date the calendar entry itself already carries.
_TRAILING_DATE = re.compile(
    r"(?:\s*[-–—:|(\[]\s*|\s+)"
    rf"(?:(?:{_WEEKDAYS_RE})\s*,?\s*)?"
    rf"(?:(?:{_MONTHS_RE})\.?\s+\d{{1,2}}(?:st|nd|rd|th)?(?:\s*,?\s*\d{{4}})?"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?)"
    r"\s*[)\]]?\s*$",
    re.IGNORECASE,
)

# A bracketed label at the front: `[FACULTY]`, `[EVENT]`. Removable on its own,
# because brackets are never part of a real event name.
_LEADING_BRACKET = re.compile(r"^\s*\[[^\]]{1,30}\]\s*[:\-–—]?\s*")

# A shouted label followed by a separator: `EVENT:`, `REMINDER -`. The separator
# is required, so a genuine acronym in a name (`RIPPAC Fall Open House`) stays.
_LEADING_LABEL = re.compile(r"^\s*\(?[A-Z][A-Z &/'-]{2,24}\)?\s*(?::|[-–—])\s+")

_LEADING_EVENT_WORD = re.compile(r"^\s*event\s*[:\-–—]\s+", re.IGNORECASE)

# A trailing parenthetical qualifier: `Provost's Coffee Hours (Focus on Research)`.
# Flattened to ` - Focus on Research`, because a calendar list truncates and a
# parenthesis is the first thing to be cut off. Purely typographic -- the words
# are the source's own, in the source's own order, with nothing added.
_TRAILING_PARENTHETICAL = re.compile(r"^(?P<head>.+?)\s*\((?P<tail>[^()]{3,40})\)\s*$")

# Qualifiers that say how to attend rather than what the event is. Flattening
# these would read as part of the name.
_NON_QUALIFIER_PARENTHETICALS = frozenset(
    {
        "hybrid", "virtual", "online", "in person", "in-person", "remote",
        "free", "no charge", "rsvp", "tba", "tbd", "cancelled", "canceled",
        "new", "updated", "zoom", "webex", "teams", "optional", "required",
    }
)


def _flatten_trailing_parenthetical(title: str) -> str:
    match = _TRAILING_PARENTHETICAL.match(title)
    if not match:
        return title
    head = match.group("head").strip(" -–—:|,;")
    tail = match.group("tail").strip()
    if len(head) < 3 or not re.search(r"[A-Za-z]", head):
        return title
    if not re.search(r"[A-Za-z]", tail):
        return title
    folded = tail.casefold().strip(" .")
    if folded in _NON_QUALIFIER_PARENTHETICALS:
        return title
    # A date in the parenthesis is handled by `_TRAILING_DATE`, not here.
    if re.search(r"\d{1,2}[/-]\d{1,2}", tail):
        return title
    return f"{head} - {tail}"


def clean_calendar_title(raw: str | None, *, fallback: str = "") -> str:
    """Strip subject artefacts a calendar entry does not need.

    Never returns empty, and never returns something the source did not say.
    """
    title = " ".join(str(raw or "").split())
    if not title:
        return " ".join(str(fallback or "").split())

    previous = None
    while previous != title:
        previous = title
        title = _LEADING_EVENT_WORD.sub("", title)
        title = _LEADING_BRACKET.sub("", title)
        title = _LEADING_LABEL.sub("", title)
        trimmed = _TRAILING_DATE.sub("", title)
        if trimmed != title:
            # The separator that introduced the date is now dangling, so it goes
            # too. Conditional on the date actually having been removed: an
            # unconditional strip turned `Town Hall (Hybrid)` into
            # `Town Hall (Hybrid` by eating a perfectly balanced bracket.
            stripped = trimmed.strip(" -–—:|(),[]")
            # Only accept the trim if something recognizable is left; a title
            # that is *only* a date keeps its date rather than becoming nothing.
            if len(stripped) >= 3 and re.search(r"[A-Za-z]", stripped):
                title = stripped
        title = _flatten_trailing_parenthetical(title)
        title = title.strip(" -–—:|,;")
        title = " ".join(title.split())

    return title or " ".join(str(raw or fallback or "").split())


# --- UID ---------------------------------------------------------------------


def session_suffix(candidate: EventCandidate, kind: str) -> str:
    """The UID/attachment discriminator for one sitting.

    A single-session announcement keeps the suffixes production already emits
    (`event`, `travel-out`, `travel-back`), so re-rendering a past day still
    produces byte-identical calendar data. Only a genuine series adds the date.
    """
    if candidate.session_count <= 1:
        return kind
    return f"{kind}-{candidate.event_date.isoformat()}"


def build_uid(submission_id: str, content_hash: str | None, suffix: str) -> str:
    """Stable across re-runs, distinct per block, unique enough to be a real UID.

    Deriving it from the announcement's own content hash means re-rendering the
    same day produces byte-identical calendar data -- which is what makes
    `calendar_recommendations` idempotent rather than merely repeatable.
    """
    seed = f"{submission_id}:{content_hash or ''}:{suffix}"
    digest = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    return f"{digest}-{suffix}@dailymail.rowan"


# --- description -------------------------------------------------------------


def build_description(
    candidate: EventCandidate,
    *,
    official_url: str,
    contact_line: str | None = None,
    travel_note: str | None = None,
    body_text: str | None = None,
) -> str:
    """An information-rich description assembled from the source, not paraphrase.

    Deliberately absent: DailyMail's own ranking scores, curation rationale and
    any other internal diagnostic. The reader gets the announcement, not our
    notes about it.
    """
    sections: list[str] = []

    when = candidate.event_date.strftime("%A, %B %-d, %Y")
    header = [f"{when}", f"{_clock(candidate.start_datetime)}–{_clock(candidate.end_datetime)} (Eastern)"]
    if candidate.end_is_assumed:
        header.append("(End time not stated by the announcement; one hour assumed.)")
    sections.append("\n".join(header))

    if len(candidate.segments) > 1:
        schedule = ["Schedule"]
        for segment in candidate.segments:
            start = _clock(datetime.combine(candidate.event_date, segment.start))
            end = (
                _clock(datetime.combine(candidate.event_date, segment.end))
                if segment.end
                else None
            )
            span = f"{start}–{end}" if end else start
            label = segment.label.strip() if segment.label else None
            schedule.append(f"  {span}" + (f" — {_titlecase(label)}" if label else ""))
        sections.append("\n".join(schedule))

    if candidate.is_session_of_series:
        # The reader is holding one sitting of several. Saying which, and what
        # the alternatives were, is the difference between a useful appointment
        # and one they cannot reconcile against the email a week later.
        listing = [
            f"This is session {candidate.session_index} of "
            f"{candidate.session_count} offered by the announcement."
        ]
        for index, (day, start, end) in enumerate(
            candidate.sibling_sessions, start=1
        ):
            when_text = day.strftime("%A, %B %-d")
            clock = _clock(datetime.combine(day, start))
            if end is not None:
                clock = f"{clock}–{_clock(datetime.combine(day, end))}"
            marker = "  * " if index == candidate.session_index else "    "
            suffix = "  (this entry)" if index == candidate.session_index else ""
            listing.append(f"{marker}{when_text}, {clock}{suffix}")
        listing.append(
            "  Registering for one session does not reserve the others."
        )
        sections.append("Sessions offered\n" + "\n".join(listing))

    location_lines: list[str] = []
    if candidate.location:
        location_lines.append(f"Location\n  {candidate.location}")
    if candidate.attendance_mode == "hybrid":
        location_lines.append("  Hybrid event.")
    if candidate.virtual_detail:
        location_lines.append(f"  Virtual option: {candidate.virtual_detail}")
    if location_lines:
        sections.append("\n".join(location_lines))

    if candidate.registration_urls:
        registration = ["Registration"]
        registration += [f"  {url}" for url in candidate.registration_urls[:3]]
        sections.append("\n".join(registration))

    if candidate.audience:
        sections.append(f"Audience\n  {candidate.audience}")

    if body_text:
        trimmed = sanitize_text(body_text, limit=1200)
        if trimmed:
            sections.append(f"From the announcement\n{trimmed}")

    if contact_line:
        sections.append(f"Contact\n  {contact_line}")

    if travel_note:
        sections.append(travel_note)

    sections.append(f"Official Rowan announcement\n  {official_url}")
    return sanitize_text("\n\n".join(section for section in sections if section))


def _titlecase(label: str) -> str:
    """Restore readable case to a lowercased phase label like `presentation and q&a`."""
    small = {"and", "or", "the", "a", "an", "of", "for", "with", "to"}
    words = label.split()
    out = []
    for index, word in enumerate(words):
        if word in ("q&a", "q&as"):
            out.append(word.upper())
        elif index > 0 and word in small:
            out.append(word)
        else:
            out.append(word[:1].upper() + word[1:])
    return " ".join(out)


# --- ICS ---------------------------------------------------------------------


def build_blocks(
    candidate: EventCandidate,
    *,
    content_hash: str | None,
    description: str,
    official_url: str,
    title: str | None = None,
    travel_minutes_before: int = 0,
    travel_minutes_after: int = 0,
    travel_mode: str = "none",
) -> list[CalendarBlock]:
    """The real event, plus a travel hold either side when travel is warranted.

    The event block always keeps the advertised start and end. Travel is never
    folded into it -- an inflated "10:00-12:00" that is really "09:50-12:10"
    would be a falsified event time, and the reader would have no way to see it.
    """
    event_title = title or candidate.title
    verb = "Drive to" if travel_mode == "drive" else "Travel to"
    return_verb = "Drive from" if travel_mode == "drive" else "Travel from"
    place = candidate.location or event_title

    blocks: list[CalendarBlock] = []
    if travel_minutes_before > 0:
        blocks.append(
            CalendarBlock(
                uid=build_uid(
                    candidate.submission_id, content_hash,
                    session_suffix(candidate, "travel-out"),
                ),
                summary=sanitize_text(f"{verb} {place}", limit=120),
                start=candidate.start_datetime - timedelta(minutes=travel_minutes_before),
                end=candidate.start_datetime,
                description=(
                    f"Travel time reserved before {event_title}.\n"
                    f"The event itself starts at {_clock(candidate.start_datetime)}."
                ),
                location=candidate.location or "",
                kind="travel_outbound",
            )
        )

    blocks.append(
        CalendarBlock(
            uid=build_uid(
                candidate.submission_id, content_hash,
                session_suffix(candidate, "event"),
            ),
            summary=sanitize_text(event_title, limit=200),
            start=candidate.start_datetime,
            end=candidate.end_datetime,
            description=description,
            location=candidate.location or "",
            url=official_url,
            kind="event",
            linkable_urls=frozenset(
                [official_url, *candidate.registration_urls[:3]]
            ),
        )
    )

    if travel_minutes_after > 0:
        blocks.append(
            CalendarBlock(
                uid=build_uid(
                    candidate.submission_id, content_hash,
                    session_suffix(candidate, "travel-back"),
                ),
                summary=sanitize_text(f"{return_verb} {place}", limit=120),
                start=candidate.end_datetime,
                end=candidate.end_datetime + timedelta(minutes=travel_minutes_after),
                description=(
                    f"Return travel reserved after {event_title}.\n"
                    f"The event itself ends at {_clock(candidate.end_datetime)}."
                ),
                location=candidate.location or "",
                kind="travel_return",
            )
        )
    return blocks


def build_ics(
    blocks: list[CalendarBlock], *, timezone: str, dtstamp: datetime
) -> str:
    """A complete VCALENDAR. CRLF line endings and folded lines, per RFC 5545."""
    if not blocks:
        raise ValueError("a VCALENDAR needs at least one VEVENT")
    lines = [
        "BEGIN:VCALENDAR",
        f"VERSION:{ICS_VERSION}",
        f"PRODID:{PRODID}",
        "CALSCALE:GREGORIAN",
        f"METHOD:{METHOD}",
    ]
    lines += _VTIMEZONES.get(timezone, [])
    for block in blocks:
        lines += block.lines(timezone=timezone, dtstamp=dtstamp)
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_line(line) for line in lines) + "\r\n"


# --- Outlook deep link -------------------------------------------------------


def build_action_url(
    candidate: EventCandidate, *, description: str, title: str
) -> str:
    """The `Add to Calendar` target: an Outlook 365 compose deep link.

    Carries the *real* event only, at its real advertised time. Travel holds
    are not representable here, which is precisely why the standards-based
    `.ics` is attached beside it.

    Times are sent without a trailing `Z` so Outlook composes them in the
    reader's own calendar timezone, which for a Glassboro event is the same
    Eastern wall clock the announcement advertises.
    """
    body = sanitize_text(description)
    parameters = {
        "path": "/calendar/action/compose",
        "rru": "addevent",
        "startdt": candidate.start_datetime.strftime("%Y-%m-%dT%H:%M:%S"),
        "enddt": candidate.end_datetime.strftime("%Y-%m-%dT%H:%M:%S"),
        "subject": sanitize_text(title, limit=200),
    }
    if candidate.location:
        parameters["location"] = sanitize_text(candidate.location, limit=200)

    url = _compose(parameters, body)
    # Trim the description rather than any authoritative field if the composer
    # link would get impractically long for a mail client.
    while len(url) > MAX_ACTION_URL_CHARS and len(body) > 200:
        body = body[: int(len(body) * 0.7)].rstrip() + "…"
        url = _compose(parameters, body)
    if len(url) > MAX_ACTION_URL_CHARS:
        url = _compose(parameters, "")
    return url


def _compose(parameters: dict, body: str) -> str:
    payload = dict(parameters)
    if body:
        payload["body"] = body
    # `quote_via=quote` keeps spaces as %20 rather than `+`: Outlook's composer
    # renders a literal `+` in the subject otherwise.
    return f"{OUTLOOK_COMPOSE_URL}?{urlencode(payload, quote_via=quote)}"


def validate_action(action: CalendarAction) -> None:
    """Last gate. A malformed calendar action must never reach the email.

    Every session is checked, not merely the series head, and the sessions are
    checked against each other: two sittings that share a start time or an
    attachment filename would be indistinguishable to the reader, which is a
    defect even though each one is individually well formed.
    """
    if not action.title.strip():
        raise ValueError("calendar action has no title")
    if not action.sessions:
        raise ValueError("calendar action has no sessions")

    starts: set[datetime] = set()
    filenames: set[str] = set()
    for session in action.sessions:
        _validate_session(session)
        if session.start in starts:
            raise ValueError(
                f"two calendar sessions both start at {session.start.isoformat()}"
            )
        starts.add(session.start)
        if session.ics_filename:
            if session.ics_filename in filenames:
                raise ValueError(
                    f"two calendar sessions share the attachment name "
                    f"{session.ics_filename!r}"
                )
            filenames.add(session.ics_filename)


def _validate_session(session: CalendarSession) -> None:
    if not session.title.strip():
        raise ValueError("calendar session has no title")
    if session.end <= session.start:
        raise ValueError("calendar session ends before it starts")
    if not session.action_url.startswith(OUTLOOK_COMPOSE_URL + "?"):
        raise ValueError("calendar session URL is not an Outlook compose link")
    if any(char in session.action_url for char in ("\n", "\r", " ", '"', "<", ">")):
        raise ValueError("calendar session URL contains an unusable character")
    if session.ics_text is not None:
        if not session.ics_text.startswith("BEGIN:VCALENDAR"):
            raise ValueError("ICS payload is not a VCALENDAR")
        if not session.ics_text.rstrip().endswith("END:VCALENDAR"):
            raise ValueError("ICS payload is truncated")
        if "\r\n" not in session.ics_text:
            raise ValueError("ICS payload is not CRLF-delimited")
        if _CONTROL.search(session.ics_text):
            raise ValueError("ICS payload contains a control character")
        if session.ics_filename and (
            "/" in session.ics_filename
            or "\\" in session.ics_filename
            or not session.ics_filename.endswith(".ics")
        ):
            raise ValueError(f"unusable ICS filename {session.ics_filename!r}")


def official_ics_dtstamp(target_date: str) -> datetime:
    """A deterministic DTSTAMP.

    RFC 5545 requires one, but using "now" would make otherwise-identical
    calendar data differ between runs and defeat idempotency. The digest date is
    stable, meaningful and sufficient.
    """
    return datetime.combine(date.fromisoformat(target_date), datetime.min.time())
