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


def slugify_filename(value: str, *, fallback: str = "event") -> str:
    """A safe, predictable attachment filename. No path, no CR/LF, no quotes."""
    cleaned = _CONTROL.sub("", value or "")
    cleaned = re.sub(r"[^A-Za-z0-9]+", "-", cleaned).strip("-")
    cleaned = re.sub(r"-{2,}", "-", cleaned)[:60].strip("-")
    return f"{cleaned or fallback}.ics"


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


@dataclass
class CalendarAction:
    """Everything the renderer, the mailer and the audit trail need."""

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

    @property
    def has_travel(self) -> bool:
        return bool(self.travel_minutes_before or self.travel_minutes_after)

    @property
    def ics_bytes(self) -> bytes | None:
        return self.ics_text.encode("utf-8") if self.ics_text else None

    # --- display helpers, used by the templates ---------------------------
    @property
    def when_line(self) -> str:
        """`Wed, Oct 14 · 10:00 AM–12:00 PM`, compact enough for a phone."""
        day = self.start.strftime("%a, %b %-d")
        return f"{day} · {_clock(self.start)}–{_clock(self.end)}"

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
        return {
            "in_person": "In person",
            "virtual": "Virtual",
            "hybrid": "In person / Hybrid",
        }.get(self.attendance_mode, "")

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
        stripped = _TRAILING_DATE.sub("", title).strip(" -–—:|(),[]")
        # Only accept the trim if something recognizable is left; a title that
        # is *only* a date keeps its date rather than becoming nothing.
        if len(stripped) >= 3 and re.search(r"[A-Za-z]", stripped):
            title = stripped
        title = title.strip(" -–—:|,;")
        title = " ".join(title.split())

    return title or " ".join(str(raw or fallback or "").split())


# --- UID ---------------------------------------------------------------------


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
                uid=build_uid(candidate.submission_id, content_hash, "travel-out"),
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
            uid=build_uid(candidate.submission_id, content_hash, "event"),
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
                uid=build_uid(candidate.submission_id, content_hash, "travel-back"),
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
    """Last gate. A malformed calendar action must never reach the email."""
    if not action.title.strip():
        raise ValueError("calendar action has no title")
    if action.end <= action.start:
        raise ValueError("calendar action ends before it starts")
    if not action.action_url.startswith(OUTLOOK_COMPOSE_URL + "?"):
        raise ValueError("calendar action URL is not an Outlook compose link")
    if any(char in action.action_url for char in ("\n", "\r", " ", '"', "<", ">")):
        raise ValueError("calendar action URL contains an unusable character")
    if action.ics_text is not None:
        if not action.ics_text.startswith("BEGIN:VCALENDAR"):
            raise ValueError("ICS payload is not a VCALENDAR")
        if not action.ics_text.rstrip().endswith("END:VCALENDAR"):
            raise ValueError("ICS payload is truncated")
        if "\r\n" not in action.ics_text:
            raise ValueError("ICS payload is not CRLF-delimited")
        if _CONTROL.search(action.ics_text):
            raise ValueError("ICS payload contains a control character")
        if action.ics_filename and (
            "/" in action.ics_filename
            or "\\" in action.ics_filename
            or not action.ics_filename.endswith(".ics")
        ):
            raise ValueError(f"unusable ICS filename {action.ics_filename!r}")


def official_ics_dtstamp(target_date: str) -> datetime:
    """A deterministic DTSTAMP.

    RFC 5545 requires one, but using "now" would make otherwise-identical
    calendar data differ between runs and defeat idempotency. The digest date is
    stable, meaningful and sufficient.
    """
    return datetime.combine(date.fromisoformat(target_date), datetime.min.time())
