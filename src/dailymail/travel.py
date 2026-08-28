"""Where an event actually is, and how long it takes to get there and back.

The reader's problem is not "what time is the town hall" -- the announcement
says that. It is that a 10:00 event at the far side of campus quietly consumes
09:45 and 12:10 too, and Outlook will happily book a meeting into both.

So an offered calendar action reserves realistic travel. Three rules keep that
honest:

* **The advertised time is never altered.** Travel is separate calendar blocks
  either side of the real event, never a padded start time.
* **"Remote" means physical distance, not "virtual".** A WebEx-only session
  needs no travel; a room 400 m away still needs a few minutes.
* **Routing is never a dependency.** The public OSRM endpoint is consulted at
  most once per *new* venue, with a short timeout, and any failure degrades to a
  conservative distance estimate that is tagged as estimated. A routing outage
  can never withhold a calendar action -- it only makes the number rougher.

Venue geography is reused, not rebuilt. Rowan's own campus map already lives in
`parking_landmarks` from Phase 3, so "Chamberlain Student Center, Eynon
Ballroom" resolves to a cached coordinate with no network call at all. Venues
repeat weekly, so `event_venues` caches the resolved coordinate *and* its travel
time and the same ballroom is never measured twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from . import db, parking
from .settings import Settings

log = logging.getLogger("dailymail.calendar")

# Resolution provenance, most trustworthy first.
SOURCE_LANDMARK = "campus_landmark"
SOURCE_PARKING = "parking_location"
SOURCE_CAMPUS = "campus_centroid"
SOURCE_UNKNOWN = "unresolved"

MODE_NONE = "none"
MODE_WALK = "walk"
MODE_DRIVE = "drive"

# Room-level qualifiers that sit inside a building. Stripping them is what turns
# "Chamberlain Student Center, Eynon Ballroom" into a name the campus map knows.
_ROOM_WORDS = (
    "ballroom", "room", "rm", "suite", "auditorium", "theater", "theatre",
    "lounge", "lobby", "atrium", "gallery", "gym", "gymnasium", "floor",
    "conference room", "boardroom", "board room", "classroom", "lab",
    "laboratory", "studio", "cafe", "cafeteria", "pavilion", "patio", "deck",
    "north", "south", "east", "west", "lower level", "concourse", "mezzanine",
)

_ROOM_NUMBER = re.compile(r"\b(?:room|rm|suite|ste)\.?\s*[a-z]?-?\d+[a-z]?\b", re.I)
_FLOOR = re.compile(r"\b\d+(?:st|nd|rd|th)\s+floor\b", re.I)


def normalize_venue(text: str | None) -> str:
    """Fold a venue string to its cache key.

    `Chamberlain Student Center, Eynon Ballroom` and `chamberlain student
    center - eynon  ballroom` share one key, so the cache hits on the spelling
    variations Rowan submitters actually produce.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", str(text))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower().replace("&", " and ")
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    return " ".join(folded.split())


def venue_fragments(venue: str | None) -> list[str]:
    """Progressively broader lookups for one venue string, best guess first.

    A room name never appears on a campus map; its building always does. So the
    full string is tried, then each comma/dash-separated part, then each part
    with its room qualifier removed.
    """
    if not venue:
        return []
    text = " ".join(str(venue).split())
    parts = [text]
    # Split on separators, and on a dash that has whitespace on at least one
    # side. `Center- Eynon Ballroom` is a separator; `Rowan-Virtua` is a name.
    for piece in re.split(r"[,;|]|\s[-–—]|[-–—]\s", text):
        piece = piece.strip(" .,;:-")
        if piece and piece not in parts:
            parts.append(piece)

    expanded: list[str] = []
    for part in parts:
        if part not in expanded:
            expanded.append(part)
        stripped = _FLOOR.sub(" ", _ROOM_NUMBER.sub(" ", part))
        tokens = [token for token in stripped.split() if token]
        while tokens and normalize_venue(tokens[-1]) in {
            normalize_venue(word) for word in _ROOM_WORDS
        }:
            tokens.pop()
        rebuilt = " ".join(tokens).strip(" .,;:-")
        if rebuilt and rebuilt not in expanded and len(rebuilt.split()) >= 2:
            expanded.append(rebuilt)
    return [part for part in expanded if normalize_venue(part)]


@dataclass
class VenueResolution:
    """Where a venue is, and how we know."""

    normalized_venue: str
    display_name: str
    latitude: float | None = None
    longitude: float | None = None
    campus: str | None = None
    source: str = SOURCE_UNKNOWN
    source_detail: str | None = None
    venue_id: int | None = None

    @property
    def located(self) -> bool:
        return self.latitude is not None and self.longitude is not None


@dataclass
class TravelPlan:
    """The travel holds to reserve, and whether the numbers are measured."""

    required: bool
    mode: str = MODE_NONE
    minutes_before: int = 0
    minutes_after: int = 0
    distance_metres: float | None = None
    routing_source: str | None = None
    estimated: bool = False
    reason: str | None = None

    def as_dict(self) -> dict:
        return {
            "required": self.required,
            "mode": self.mode,
            "minutes_before": self.minutes_before,
            "minutes_after": self.minutes_after,
            "distance_metres": (
                round(self.distance_metres) if self.distance_metres else None
            ),
            "routing_source": self.routing_source,
            "estimated": self.estimated,
            "reason": self.reason,
        }


NO_TRAVEL = TravelPlan(required=False, reason="not_required")


# --- venue resolution --------------------------------------------------------


def resolve_venue(connection, venue: str | None, *, campus_hint: str | None = None):
    """Locate a venue, cheapest source first. Returns a :class:`VenueResolution`.

    Order: the venue cache, then Phase 3's cached campus landmarks, then the
    cached parking facilities (a lot closure names a real place), then the
    campus centroid. There is no geocoding call here at all -- Rowan's own map
    data already covers its own venues, and building a second campus map would
    be duplicated work with a worse source.
    """
    key = normalize_venue(venue)
    if not key:
        return VenueResolution(normalized_venue="", display_name="", source=SOURCE_UNKNOWN)

    cached = db.venue_by_normalized(connection, key)
    if cached is not None and cached["latitude"] is not None:
        return VenueResolution(
            normalized_venue=key,
            display_name=cached["display_name"],
            latitude=cached["latitude"],
            longitude=cached["longitude"],
            campus=cached["campus"],
            source=cached["source"],
            source_detail=cached["source_detail"],
            venue_id=int(cached["venue_id"]),
        )

    display = " ".join(str(venue).split())
    for fragment in venue_fragments(venue):
        hit = _landmark_lookup(connection, fragment, campus_hint)
        if hit is not None:
            return VenueResolution(
                normalized_venue=key,
                display_name=display,
                latitude=hit["latitude"],
                longitude=hit["longitude"],
                campus=hit["campus"],
                source=SOURCE_LANDMARK,
                source_detail=hit["name"],
            )

    for fragment in venue_fragments(venue):
        hit = _parking_lookup(connection, fragment, campus_hint)
        if hit is not None:
            return VenueResolution(
                normalized_venue=key,
                display_name=display,
                latitude=hit["latitude"],
                longitude=hit["longitude"],
                campus=hit["campus"],
                source=SOURCE_PARKING,
                source_detail=hit["canonical_name"],
            )

    return VenueResolution(
        normalized_venue=key, display_name=display, campus=campus_hint,
        source=SOURCE_UNKNOWN,
    )


def _landmark_lookup(connection, fragment: str, campus_hint: str | None):
    """Exact then containment match against Phase 3's cached campus landmarks."""
    normalized = parking.normalize_name(fragment)
    if not normalized:
        return None
    rows = list(
        connection.execute(
            "SELECT campus, name, normalized_name, latitude, longitude "
            "FROM parking_landmarks WHERE normalized_name = ?",
            (normalized,),
        )
    )
    if not rows:
        # `Eynon Ballroom` is inside `Chamberlain Student Center`; a containment
        # match is what connects the two without a second dataset.
        rows = [
            row
            for row in connection.execute(
                "SELECT campus, name, normalized_name, latitude, longitude "
                "FROM parking_landmarks"
            )
            if _contains(row["normalized_name"], normalized)
        ]
    return _pick_by_campus(rows, campus_hint)


def _parking_lookup(connection, fragment: str, campus_hint: str | None):
    normalized = parking.normalize_name(fragment)
    if not normalized:
        return None
    rows = list(
        connection.execute(
            """
            SELECT l.campus, l.canonical_name, l.latitude, l.longitude
              FROM parking_locations l
              JOIN parking_aliases a ON a.location_id = l.location_id
             WHERE a.normalized_alias = ? AND l.is_active = 1
               AND l.latitude IS NOT NULL
            """,
            (normalized,),
        )
    )
    return _pick_by_campus(rows, campus_hint)


def _contains(haystack: str, needle: str) -> bool:
    if not haystack or not needle or len(needle) < 6:
        return False
    return f" {needle} " in f" {haystack} "


def _pick_by_campus(rows, campus_hint: str | None):
    """One unambiguous row, or nothing. A name on two campuses is not evidence."""
    if not rows:
        return None
    if len(rows) == 1:
        return rows[0]
    if campus_hint:
        narrowed = [row for row in rows if row["campus"] == campus_hint]
        if len(narrowed) == 1:
            return narrowed[0]
    campuses = {row["campus"] for row in rows}
    return rows[0] if len(campuses) == 1 else None


# --- travel calculation ------------------------------------------------------


def _round_up(minutes: float, increment: int) -> int:
    increment = max(1, increment)
    return int(math.ceil(minutes / increment) * increment)


def plan_travel(
    resolution: VenueResolution,
    *,
    attendance_mode: str,
    settings: Settings,
    router=None,
) -> TravelPlan:
    """Decide the travel holds for one event.

    `attendance_mode` is the *decided* mode, after any model judgement about a
    hybrid event has been reconciled with the deterministic evidence.
    """
    if not settings.calendar_travel_enabled:
        return TravelPlan(required=False, reason="travel_disabled")
    if attendance_mode == "virtual":
        return TravelPlan(required=False, reason="virtual_attendance")
    if not resolution.located:
        return TravelPlan(required=False, reason="venue_unresolved")

    distance = parking.distance_metres(
        settings.calendar_base_latitude,
        settings.calendar_base_longitude,
        resolution.latitude,
        resolution.longitude,
    )

    # Effectively at base: nothing to reserve.
    if distance < 60:
        return TravelPlan(
            required=False, distance_metres=distance, reason="at_base_location"
        )

    if distance <= settings.calendar_walk_max_metres:
        # A campus walk is geometry, not routing. Straight-line distance is
        # scaled for paths that do not run diagonally across buildings.
        walk_seconds = (distance * 1.25) / max(0.5, settings.calendar_walking_speed_mps)
        minutes = walk_seconds / 60.0 + settings.calendar_walk_padding_minutes
        block = _clamp_minutes(minutes, settings)
        return TravelPlan(
            required=True,
            mode=MODE_WALK,
            minutes_before=block,
            minutes_after=block,
            distance_metres=distance,
            routing_source="campus_walk_estimate",
            estimated=False,
            reason="on_campus_walk",
        )

    seconds, routing_source, estimated = _drive_seconds(
        resolution, settings=settings, router=router, distance=distance
    )
    drive_minutes = seconds / 60.0
    before = _clamp_minutes(
        drive_minutes + settings.calendar_drive_arrival_padding_minutes, settings
    )
    after = _clamp_minutes(
        drive_minutes + settings.calendar_drive_return_padding_minutes, settings
    )
    return TravelPlan(
        required=True,
        mode=MODE_DRIVE,
        minutes_before=before,
        minutes_after=after,
        distance_metres=distance,
        routing_source=routing_source,
        estimated=estimated,
        reason="off_campus_drive",
    )


def _clamp_minutes(minutes: float, settings: Settings) -> int:
    rounded = _round_up(minutes, settings.calendar_travel_rounding_minutes)
    rounded = max(settings.calendar_travel_min_minutes, rounded)
    return min(settings.calendar_travel_max_minutes, rounded)


def _drive_seconds(resolution, *, settings, router, distance) -> tuple[float, str, bool]:
    """Route time for a real drive, or a conservative estimate if that fails."""
    if settings.calendar_routing_enabled:
        call = router if router is not None else osrm_duration_seconds
        try:
            seconds = call(
                (settings.calendar_base_latitude, settings.calendar_base_longitude),
                (resolution.latitude, resolution.longitude),
                settings=settings,
            )
        except Exception as exc:  # noqa: BLE001 - routing is never critical
            log.info("route lookup failed for %s: %s", resolution.display_name, exc)
            seconds = None
        if seconds is not None and seconds > 0:
            return float(seconds), "osrm", False

    # Fallback: straight-line distance scaled for real roads at a modest
    # average speed. Deliberately pessimistic -- an over-reserved 5 minutes is
    # cheaper than a missed departure.
    road_metres = distance * 1.35
    seconds = road_metres / (40_000 / 3600)  # 40 km/h door to door
    return seconds, "distance_estimate", True


def osrm_duration_seconds(origin, destination, *, settings: Settings, client=None):
    """One read-only OSRM route lookup. Returns seconds, or None.

    Called at most once per newly seen venue, never on a cache hit, and behind a
    short timeout. Nothing about the reader is sent: two coordinates, no
    identifiers, no announcement text, no credentials.
    """
    import httpx

    url = (
        f"{settings.calendar_routing_url}/route/v1/driving/"
        f"{origin[1]:.6f},{origin[0]:.6f};"
        f"{destination[1]:.6f},{destination[0]:.6f}"
    )
    params = {"overview": "false", "alternatives": "false", "steps": "false"}
    timeout = settings.calendar_routing_timeout_seconds
    if client is not None:
        response = client.get(url, params=params, timeout=timeout)
    else:
        with httpx.Client(timeout=timeout, follow_redirects=False) as http:
            response = http.get(url, params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "Ok":
        return None
    routes = payload.get("routes") or []
    if not routes:
        return None
    duration = routes[0].get("duration")
    return float(duration) if duration is not None else None


def routing_fingerprint(settings: Settings, resolution: VenueResolution) -> str:
    """Identifies the inputs a cached travel time was computed from."""
    payload = json.dumps(
        {
            "base": [settings.calendar_base_latitude, settings.calendar_base_longitude],
            "venue": [resolution.latitude, resolution.longitude],
            "walk_max": settings.calendar_walk_max_metres,
            "speed": settings.calendar_walking_speed_mps,
            "pads": [
                settings.calendar_walk_padding_minutes,
                settings.calendar_drive_arrival_padding_minutes,
                settings.calendar_drive_return_padding_minutes,
            ],
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def cache_is_fresh(row, settings: Settings, fingerprint: str, *, today: date) -> bool:
    """True when a cached venue's travel time can be reused without recomputing."""
    if row is None or row["outbound_minutes"] is None:
        return False
    if row["routing_fingerprint"] != fingerprint:
        return False
    try:
        verified = datetime.fromisoformat(row["last_verified_at"]).date()
    except (TypeError, ValueError):
        return False
    return verified >= today - timedelta(days=settings.calendar_venue_cache_days)


def plan_from_cache(row) -> TravelPlan:
    return TravelPlan(
        required=bool(row["outbound_minutes"] or row["return_minutes"]),
        mode=row["travel_mode"] or MODE_NONE,
        minutes_before=int(row["outbound_minutes"] or 0),
        minutes_after=int(row["return_minutes"] or 0),
        distance_metres=row["distance_metres"],
        routing_source=row["routing_source"],
        estimated=bool(row["routing_estimated"]),
        reason="venue_cache_hit",
    )
