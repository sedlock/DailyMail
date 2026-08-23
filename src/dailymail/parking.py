"""Parking geography: normalization, alias generation, mention detection.

Pure domain logic. No network, no database, no subprocess -- everything here is
a deterministic function of its arguments, which is what makes the daily hot
path (a cache hit) free.

Three ideas carry the whole feature:

* **Normalization** absorbs the ways Rowan writes a lot code. `O-1`, `O 1` and
  `O1` all normalize to `o 1`, so the alias table stays small and a new spelling
  does not need a new row.
* **Detection is pattern-first and case-sensitive.** `Parking Lot O-1` matches;
  `the lot will remain closed` -- which is real text from the very announcement
  this feature was built for -- does not, because `lot` is lowercase and `will`
  is not a lot code.
* **Campus is part of identity.** `Lot A` exists at Glassboro *and* Stratford.
  A name alone is never an answer.
"""

from __future__ import annotations

import math
import re
import unicodedata
from dataclasses import dataclass, field

# --- campuses ----------------------------------------------------------------

# Rowan's official parking landing pages, used for the graceful fallback link
# when a lot cannot be resolved. Campus keys are the identity prefix in a
# canonical id such as `glassboro:lot:o-1`.
CAMPUSES: dict[str, dict] = {
    "glassboro": {
        "display": "Glassboro",
        "map_url": "https://sites.rowan.edu/publicsafety/parking/parking-map-printable.html",
        "map_label": "Glassboro campus parking map",
        # Rowan Announcer category titles that imply this campus.
        "categories": ("Glassboro Campus",),
        "keywords": ("glassboro", "main campus", "rowan boulevard", "route 322"),
    },
    "stratford": {
        "display": "Stratford",
        "map_url": "https://som.rowan.edu/oursom/campus/stratford/map.html",
        "map_label": "Stratford campus map",
        "categories": ("Stratford Campus",),
        "keywords": (
            "stratford",
            "rowan-virtua",
            "rowan virtua",
            "school of osteopathic medicine",
            "laurel road",
        ),
    },
    "camden": {
        "display": "Camden",
        "map_url": "https://cmsru.rowan.edu/resources/visitors/",
        "map_label": "CMSRU visitor parking information",
        "categories": ("CMSRU",),
        "keywords": (
            "camden",
            "cmsru",
            "cooper medical school",
            "medical education building",
            "camden academic building",
        ),
    },
    "sewell": {
        "display": "Sewell",
        "map_url": "https://www.rowan.edu/about/visiting/sewell.html",
        "map_label": "Sewell campus map",
        "categories": (),
        "keywords": ("sewell", "tanyard road"),
    },
}

DEFAULT_CAMPUS_ORDER = ("glassboro", "stratford", "camden", "sewell")

LOCATION_TYPES = (
    "surface_lot",
    "garage",
    "patient_lot",
    "visitor_lot",
    "other",
)

PERMIT_CLASSES = (
    "Employee",
    "Student",
    "Patient",
    "Visitor",
    "Resident",
    "Commuter",
    "Mixed",
    "Unknown",
)

CONFIDENCE_ORDER = {"low": 0, "medium": 1, "high": 2}


def confidence_at_least(value: str | None, minimum: str) -> bool:
    return CONFIDENCE_ORDER.get(value or "low", 0) >= CONFIDENCE_ORDER.get(minimum, 1)


# --- normalization -----------------------------------------------------------

# Expanded so `Rowan Blvd. Parking Garage` and `Rowan Boulevard Garage` collapse
# to the same key. Deliberately short: every entry is a real abbreviation seen in
# Rowan's own parking material.
_ABBREVIATIONS = {
    "blvd": "boulevard",
    "st": "street",
    "ave": "avenue",
    "av": "avenue",
    "rd": "road",
    "dr": "drive",
    "apt": "apartment",
    "apts": "apartments",
    "bldg": "building",
    "ctr": "center",
    "n": "north",
    "s": "south",
    "e": "east",
    "w": "west",
}

# `n`/`s`/`e`/`w` are only directions when they are not the whole lot code, so
# expansion is skipped for a single-token value such as the normalized form of
# `Lot W`.
_DIRECTION_TOKENS = frozenset({"n", "s", "e", "w"})

_LETTER_DIGIT = re.compile(r"(?<=[a-z])(?=\d)")
_DIGIT_LETTER = re.compile(r"(?<=\d)(?=[a-z])")


def normalize_name(text: str | None) -> str:
    """Fold one parking-facility name to its comparison key.

    `Parking Lot O-1`, `Parking Lot O1` and `parking lot  o 1` all give
    `parking lot o 1`.
    """
    if not text:
        return ""
    folded = unicodedata.normalize("NFKD", str(text))
    folded = "".join(ch for ch in folded if not unicodedata.combining(ch))
    folded = folded.lower()
    folded = re.sub(r"[^a-z0-9]+", " ", folded)
    folded = _LETTER_DIGIT.sub(" ", folded)
    folded = _DIGIT_LETTER.sub(" ", folded)
    tokens = folded.split()
    if len(tokens) > 1:
        tokens = [
            _ABBREVIATIONS.get(token, token)
            if not (token in _DIRECTION_TOKENS and len(tokens) <= 2)
            else token
            for token in tokens
        ]
    return " ".join(tokens)


def slugify(text: str) -> str:
    """`Lot O-1` -> `o-1`, `Rowan Boulevard Garage` -> `rowan-boulevard`."""
    normalized = normalize_name(text)
    for prefix in ("parking lot ", "lot ", "parking garage ", "garage "):
        if normalized.startswith(prefix):
            normalized = normalized[len(prefix):]
            break
    for suffix in (" parking garage", " garage", " parking lot", " lot", " parking"):
        if normalized.endswith(suffix):
            normalized = normalized[: -len(suffix)]
            break
    return "-".join(normalized.split()) or "unnamed"


def canonical_id(campus: str, location_type: str, name: str) -> str:
    """`glassboro:lot:o-1`, `camden:garage:medical-school`.

    Campus is part of the identity because `Lot A` is not unique across Rowan.
    """
    kind = "garage" if location_type == "garage" else "lot"
    return f"{campus}:{kind}:{slugify(name)}"


# --- alias generation --------------------------------------------------------

_LOT_CODE = re.compile(r"^(?:parking\s+)?lot\s+(?P<code>.+)$")
_GARAGE_NAME = re.compile(r"^(?P<name>.+?)\s+(?:parking\s+)?garage$")


def generate_aliases(canonical_name: str) -> list[str]:
    """Every spelling of a facility we are willing to match, most specific first.

    A bare code (`O-1`) is included so a pattern hit can be looked up directly,
    but `scannable_alias()` refuses to sweep it across free text -- otherwise
    `Lot A` would match "a lot of students".
    """
    name = " ".join((canonical_name or "").split())
    if not name:
        return []
    aliases: list[str] = [name]
    normalized = normalize_name(name)

    match = _LOT_CODE.match(normalized)
    if match:
        code = match.group("code")
        display = " ".join(part.upper() if len(part) <= 2 else part.title()
                           for part in code.split())
        joined = code.replace(" ", "")
        hyphenated = "-".join(code.split()) if len(code.split()) > 1 else code
        # Hyphenated first: `O-1` is how Rowan writes it, so that is the form
        # stored for display. The un-hyphenated variants normalize to the same
        # key and are only here to document the spellings we accept.
        pretty = hyphenated.upper()
        for candidate in (
            f"Parking Lot {pretty}",
            f"Lot {pretty}",
            f"{pretty} Lot",
            pretty,
            f"Parking Lot {display}",
            f"Lot {display}",
            f"{display} Lot",
            f"Lot {joined.upper()}",
            f"{joined.upper()} Lot",
            display,
        ):
            aliases.append(candidate)
    else:
        # Named facility: accept it with and without a trailing `Lot`/`Parking`.
        aliases.extend([f"{name} Lot", f"Parking {name}", f"{name} Parking"])
        garage = _GARAGE_NAME.match(normalized)
        if garage:
            stem = garage.group("name").title()
            aliases.extend([f"{stem} Garage", f"{stem} Parking Garage"])

    seen: dict[str, str] = {}
    for alias in aliases:
        key = normalize_name(alias)
        if key and key not in seen:
            seen[key] = alias
    return list(seen.values())


def scannable_alias(alias: str) -> bool:
    """Whether an alias is distinctive enough to sweep across announcement text.

    Requires at least two tokens *and* either a digit or a word that is not just
    a one- or two-letter lot code. `lot o 1` and `rowan boulevard garage` pass;
    `lot a` and `a` do not, because ordinary English produces them by accident.
    """
    normalized = normalize_name(alias)
    tokens = normalized.split()
    if len(tokens) < 2:
        return False
    filler = {"lot", "parking", "garage"}
    payload = [token for token in tokens if token not in filler]
    if not payload:
        return False
    # A real word ("Rowan", "Chestnut", "Ellis") is distinctive on its own.
    if any(len(token) > 2 and not token.isdigit() for token in payload):
        return True
    # Otherwise the alias is only a lot code, and it needs both a `Lot`/`Garage`
    # anchor and a digit: `lot o 1` is safe to sweep, `o 1` and `lot a` are not.
    return len(payload) < len(tokens) and any(token.isdigit() for token in payload)


# --- mention detection -------------------------------------------------------

# A lot code as Rowan writes it: `A`, `O-1`, `D2`, `Z-1`, `301`.
_CODE = r"(?:[A-Z]{1,2}[\-‐-― ]?\d{1,2}|[A-Z]{1,2}|\d{3})"

# `Parking Lot O-1` / `Lot O-1`. Case-sensitive on purpose: lowercase "lot" in
# running prose is never a lot name.
_RE_LOT_PREFIX = re.compile(
    rf"\b(?:Parking\s+)?Lot\s+(?P<code>{_CODE})(?![A-Za-z0-9\-])"
)
# `O-1 Lot`
_RE_LOT_SUFFIX = re.compile(
    rf"\b(?P<code>{_CODE})\s+(?:Parking\s+)?Lot\b"
)
# `Lots A, B and C` -- one sentence, several lots.
_RE_LOT_LIST = re.compile(
    rf"\bLots\s+(?P<codes>{_CODE}(?:\s*(?:,|and|&|/|or)\s*{_CODE})+)"
)
# `Rowan Boulevard Garage`, `Townhouse Parking Garage`
_RE_GARAGE = re.compile(
    r"\b(?P<name>[A-Z][A-Za-z'’.\-]*(?:\s+(?:[A-Z][A-Za-z'’.\-]*|of|the|at))"
    r"{0,3})\s+(?:Parking\s+)?Garage\b"
)
# A bare `Parking Garage` with no name in front of it.
_RE_BARE_GARAGE = re.compile(r"\b(?:the\s+)?Parking\s+Garage\b")

_CODE_TOKEN = re.compile(_CODE)

# Words that look like a garage name but are the sentence, not the facility.
_GARAGE_NAME_STOPWORDS = frozenset(
    {"the", "a", "an", "this", "that", "our", "new", "any", "all", "each", "no"}
)

PARKING_CONTEXT = re.compile(
    r"\b(park|parking|lot|lots|garage|permit|shuttle|vehicle|car|driveway)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Mention:
    """One parking reference found in an announcement."""

    matched_text: str          # exactly what the announcement said
    lookup_keys: tuple[str, ...]  # normalized keys to try, most specific first
    method: str                # how it was found
    order: int                 # first character offset, for stable ordering
    weak: bool = False         # single bare letter: real if cached, never researched

    @property
    def primary_key(self) -> str:
        return self.lookup_keys[0] if self.lookup_keys else ""


def _keys_for_code(code: str) -> tuple[str, ...]:
    normalized = normalize_name(code)
    return tuple(
        dict.fromkeys(
            key
            for key in (
                f"lot {normalized}",
                f"parking lot {normalized}",
                f"{normalized} lot",
                normalized,
            )
            if key.strip()
        )
    )


def _display_code(code: str) -> str:
    return " ".join(code.split())


def detect_mentions(
    *,
    title: str | None = None,
    body_text: str | None = None,
    event_location: str | None = None,
    named_aliases: dict[str, str] | None = None,
) -> list[Mention]:
    """Find every parking reference in one announcement, deterministically.

    `named_aliases` maps a normalized cached alias to its display form; only
    aliases that pass `scannable_alias()` should be supplied. They are swept
    case-insensitively so `chestnut hall lot` is found however it is written.
    """
    segments = [
        (title or "", "title"),
        (body_text or "", "body"),
        (event_location or "", "event_location"),
    ]
    found: dict[str, Mention] = {}
    offset = 0

    for text, _origin in segments:
        if not text:
            offset += 1
            continue

        for match in _RE_LOT_LIST.finditer(text):
            for code_match in _CODE_TOKEN.finditer(match.group("codes")):
                code = code_match.group(0)
                _add(
                    found,
                    Mention(
                        matched_text=f"Lot {_display_code(code)}",
                        lookup_keys=_keys_for_code(code),
                        method="pattern_lot_list",
                        order=offset + match.start() + code_match.start(),
                        weak=_is_weak_code(code),
                    ),
                )

        for pattern, method in (
            (_RE_LOT_PREFIX, "pattern_lot_prefix"),
            (_RE_LOT_SUFFIX, "pattern_lot_suffix"),
        ):
            for match in pattern.finditer(text):
                code = match.group("code")
                _add(
                    found,
                    Mention(
                        matched_text=match.group(0).strip(),
                        lookup_keys=_keys_for_code(code),
                        method=method,
                        order=offset + match.start(),
                        weak=_is_weak_code(code),
                    ),
                )

        for match in _RE_GARAGE.finditer(text):
            words = match.group("name").split()
            while words and words[0].lower() in _GARAGE_NAME_STOPWORDS:
                words.pop(0)
            while words and words[-1].lower() in _GARAGE_NAME_STOPWORDS:
                words.pop()
            name = " ".join(words)
            if not name or name.lower() == "parking":
                continue
            phrase = f"{name} Garage"
            normalized = normalize_name(phrase)
            _add(
                found,
                Mention(
                    matched_text=match.group(0).strip(),
                    lookup_keys=(normalized, normalize_name(name)),
                    method="pattern_garage",
                    order=offset + match.start(),
                ),
            )

        for alias_key, display in (named_aliases or {}).items():
            position = _find_alias(text, alias_key)
            if position is None:
                continue
            _add(
                found,
                Mention(
                    matched_text=display,
                    lookup_keys=(alias_key,),
                    method="alias_scan",
                    order=offset + position,
                ),
            )

        if _RE_BARE_GARAGE.search(text) and not any(
            m.method == "pattern_garage" for m in found.values()
        ):
            match = _RE_BARE_GARAGE.search(text)
            _add(
                found,
                Mention(
                    matched_text=match.group(0).strip(),
                    lookup_keys=("parking garage",),
                    method="pattern_bare_garage",
                    order=offset + match.start(),
                ),
            )

        offset += len(text) + 1

    return _merge_overlapping(
        sorted(found.values(), key=lambda m: (m.order, m.primary_key))
    )


def _merge_overlapping(mentions: list[Mention]) -> list[Mention]:
    """Collapse mentions that point at the same facility.

    The pattern detector and the alias scan legitimately both fire on
    `Parking Lot O-1` -- one keyed `lot o 1`, the other `parking lot o 1`. They
    are one lot, so the earlier mention absorbs the other's lookup keys.
    """
    merged: list[Mention] = []
    for mention in mentions:
        keys = set(mention.lookup_keys)
        for index, existing in enumerate(merged):
            if keys & set(existing.lookup_keys):
                combined = tuple(
                    dict.fromkeys(existing.lookup_keys + mention.lookup_keys)
                )
                merged[index] = Mention(
                    matched_text=existing.matched_text,
                    lookup_keys=combined,
                    method=existing.method,
                    order=existing.order,
                    weak=existing.weak and mention.weak,
                )
                break
        else:
            merged.append(mention)
    return merged


def _is_weak_code(code: str) -> bool:
    """A bare one-letter code. Resolvable from cache, never worth researching."""
    return len(code.strip()) == 1 and code.strip().isalpha()


def _add(found: dict[str, Mention], mention: Mention) -> None:
    """Keep one mention per lookup key -- repeated mentions must not duplicate."""
    key = mention.primary_key
    if not key:
        return
    existing = found.get(key)
    if existing is None or mention.order < existing.order:
        found[key] = mention


def _find_alias(text: str, normalized_alias: str) -> int | None:
    """Locate a normalized alias in raw text, respecting token boundaries."""
    tokens = normalized_alias.split()
    if not tokens:
        return None
    pattern = r"\b" + r"[\s\-‐-―.,]*".join(
        re.escape(token) for token in tokens
    ) + r"\b"
    match = re.search(pattern, text, re.IGNORECASE)
    return match.start() if match else None


# --- campus inference --------------------------------------------------------


def infer_campus(
    *,
    category_title: str | None = None,
    title: str | None = None,
    body_text: str | None = None,
    event_location: str | None = None,
    landmarks: dict[str, str] | None = None,
) -> tuple[str | None, str]:
    """Best-supported campus for one announcement, with the reason why.

    Returns `(campus, evidence)`; campus is None when the announcement offers no
    campus evidence at all. Category wins, then a named campus building, then a
    campus keyword -- and any disagreement between two campuses yields None
    rather than a guess.
    """
    if category_title:
        for campus, spec in CAMPUSES.items():
            if category_title in spec["categories"]:
                return campus, f"category {category_title!r}"

    haystack = " ".join(
        part for part in (title, body_text, event_location) if part
    )
    if not haystack:
        return None, "no campus evidence"
    lowered = haystack.lower()

    if landmarks:
        hits: set[str] = set()
        detail = ""
        for landmark, campus in landmarks.items():
            if len(landmark) < 6:
                continue
            if re.search(rf"\b{re.escape(landmark)}\b", lowered):
                hits.add(campus)
                if not detail:
                    detail = landmark
        if len(hits) == 1:
            return hits.pop(), f"building {detail!r} named in the announcement"

    keyword_hits: dict[str, str] = {}
    for campus, spec in CAMPUSES.items():
        for keyword in spec["keywords"]:
            if re.search(rf"\b{re.escape(keyword)}\b", lowered):
                keyword_hits.setdefault(campus, keyword)
    if len(keyword_hits) == 1:
        campus, keyword = next(iter(keyword_hits.items()))
        return campus, f"keyword {keyword!r}"
    if len(keyword_hits) > 1:
        return None, (
            "conflicting campus keywords: "
            + ", ".join(sorted(keyword_hits.values()))
        )
    return None, "no campus evidence"


# --- Google Maps link --------------------------------------------------------

MAPS_URL_TEMPLATE = "https://www.google.com/maps/search/?api=1&query={lat},{lon}"


def maps_url(latitude: float, longitude: float) -> str:
    """A documented, stable Google Maps URL centred on the coordinate itself.

    Deliberately not an opaque `goo.gl/maps` share link: those cannot be
    regenerated, verified or corrected from stored data.
    """
    return MAPS_URL_TEMPLATE.format(
        lat=f"{float(latitude):.6f}".rstrip("0").rstrip("."),
        lon=f"{float(longitude):.6f}".rstrip("0").rstrip("."),
    )


# --- validation --------------------------------------------------------------

# A generous box around Rowan's New Jersey footprint: Glassboro, Sewell,
# Stratford and Camden all sit inside it, and anything outside is a mistake.
ROWAN_BOUNDS = (39.60, 40.05, -75.30, -74.90)  # lat_min, lat_max, lon_min, lon_max


class ParkingDataError(ValueError):
    """A parking record failed validation and must not be cached."""


def validate_coordinates(latitude, longitude) -> tuple[float, float]:
    try:
        lat = float(latitude)
        lon = float(longitude)
    except (TypeError, ValueError) as exc:
        raise ParkingDataError(f"non-numeric coordinate {latitude!r},{longitude!r}") from exc
    if math.isnan(lat) or math.isnan(lon) or math.isinf(lat) or math.isinf(lon):
        raise ParkingDataError("coordinate is not a finite number")
    lat_min, lat_max, lon_min, lon_max = ROWAN_BOUNDS
    if not (lat_min <= lat <= lat_max and lon_min <= lon <= lon_max):
        raise ParkingDataError(
            f"coordinate {lat},{lon} is outside Rowan's service area {ROWAN_BOUNDS}"
        )
    return lat, lon


DESCRIPTION_MIN_WORDS = 6
DESCRIPTION_MAX_WORDS = 30

_MARKETING = re.compile(
    r"\b(convenient(ly)?|beautiful|state[- ]of[- ]the[- ]art|world[- ]class|"
    r"exciting|wonderful|premier|vibrant|welcoming|amazing)\b",
    re.IGNORECASE,
)
_COORDINATE_LIKE = re.compile(r"-?\d{2}\.\d{3,}")
_PROPER_NOUN = re.compile(r"\b[A-Z][a-zA-Z'’]+(?:\s+[A-Z][a-zA-Z'’]+)*")

# Words that legitimately start a sentence or name a permit class, so they must
# not be mistaken for a landmark the model invented.
_DESCRIPTION_ALLOWED_WORDS = frozenset(
    {
        word.lower()
        for word in (
            "Employee", "Student", "Patient", "Visitor", "Resident", "Commuter",
            "Mixed", "Unknown", "Staff", "Faculty", "Permit", "Parking", "Lot",
            "Lots", "Garage", "Surface", "Campus", "North", "South", "East",
            "West", "Northeast", "Northwest", "Southeast", "Southwest", "Rowan",
            "University", "The", "A", "An", "Immediately", "Directly", "Just",
            "Adjacent", "Between", "Behind", "Beside", "Across", "Opposite",
            "Off", "On", "Near", "At", "Along", "Next", "Inside", "Rowan-Virtua",
        )
    }
)


def validate_description(text: str | None, *, evidence_names: list[str]) -> str:
    """Accept a cached description only if it is short, concrete and grounded.

    The grounding check is the important one: every proper noun in the sentence
    must appear in the evidence that was supplied. A landmark the model invented
    is rejected outright rather than cached and shown to the reader.
    """
    value = " ".join((text or "").split())
    if not value:
        raise ParkingDataError("description is empty")
    words = value.split()
    if len(words) < DESCRIPTION_MIN_WORDS:
        raise ParkingDataError(f"description is only {len(words)} words: {value!r}")
    if len(words) > DESCRIPTION_MAX_WORDS:
        raise ParkingDataError(f"description is {len(words)} words, too long: {value!r}")
    if _COORDINATE_LIKE.search(value):
        raise ParkingDataError("description restates a coordinate")
    if _MARKETING.search(value):
        raise ParkingDataError(f"description contains marketing prose: {value!r}")
    if "http://" in value or "https://" in value:
        raise ParkingDataError("description contains a URL")

    evidence_tokens = {
        token
        for name in evidence_names
        for token in normalize_name(name).split()
        if token
    }
    for candidate in _PROPER_NOUN.findall(value):
        for token in normalize_name(candidate).split():
            if token in _DESCRIPTION_ALLOWED_WORDS or token.isdigit():
                continue
            if token not in evidence_tokens:
                raise ParkingDataError(
                    f"description names {candidate!r}, which is not in the supplied "
                    f"evidence; refusing to cache a possibly invented landmark"
                )
    return value


# --- geometry helpers --------------------------------------------------------

_COMPASS = (
    "north", "north-northeast", "northeast", "east-northeast",
    "east", "east-southeast", "southeast", "south-southeast",
    "south", "south-southwest", "southwest", "west-southwest",
    "west", "west-northwest", "northwest", "north-northwest",
)


def offset_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> tuple[float, float]:
    """Local flat-earth offset in metres: (east, north). Good to metres locally."""
    north = (lat2 - lat1) * 111_320.0
    east = (lon2 - lon1) * 111_320.0 * math.cos(math.radians(lat1))
    return east, north


def distance_metres(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    east, north = offset_metres(lat1, lon1, lat2, lon2)
    return math.hypot(east, north)


def compass_direction(lat1: float, lon1: float, lat2: float, lon2: float) -> str:
    """Direction from point 1 to point 2, as a word a reader can act on."""
    east, north = offset_metres(lat1, lon1, lat2, lon2)
    angle = (math.degrees(math.atan2(east, north)) + 360.0) % 360.0
    return _COMPASS[int((angle + 11.25) // 22.5) % 16]


def representative_point(ring: list[tuple[float, float]]) -> tuple[float, float]:
    """A point that is inside the polygon, not merely its arithmetic centre.

    The area centroid is used when it falls inside the ring; an irregular or
    L-shaped lot can put its centroid on a building, so in that case the
    midpoint of the longest interior horizontal chord is used instead.
    """
    if not ring:
        raise ParkingDataError("empty polygon")
    if len(ring) < 3:
        return ring[0]
    points = list(ring)
    if points[0] == points[-1]:
        points = points[:-1]

    area = cx = cy = 0.0
    count = len(points)
    for index in range(count):
        lat1, lon1 = points[index]
        lat2, lon2 = points[(index + 1) % count]
        cross = lon1 * lat2 - lon2 * lat1
        area += cross
        cx += (lon1 + lon2) * cross
        cy += (lat1 + lat2) * cross
    if abs(area) < 1e-15:
        return points[0]
    area *= 0.5
    centroid = (cy / (6.0 * area), cx / (6.0 * area))
    if point_in_ring(centroid, points):
        return centroid

    # Scan the widest interior chord at the centroid's latitude band.
    latitudes = sorted({lat for lat, _ in points})
    best: tuple[float, tuple[float, float]] | None = None
    for index in range(len(latitudes) - 1):
        lat = (latitudes[index] + latitudes[index + 1]) / 2.0
        crossings = []
        for edge in range(count):
            lat1, lon1 = points[edge]
            lat2, lon2 = points[(edge + 1) % count]
            if (lat1 > lat) != (lat2 > lat):
                crossings.append(lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1))
        crossings.sort()
        for pair in range(0, len(crossings) - 1, 2):
            width = crossings[pair + 1] - crossings[pair]
            candidate = (lat, (crossings[pair] + crossings[pair + 1]) / 2.0)
            if best is None or width > best[0]:
                best = (width, candidate)
    return best[1] if best else points[0]


def point_in_ring(point: tuple[float, float], ring: list[tuple[float, float]]) -> bool:
    lat, lon = point
    inside = False
    count = len(ring)
    for index in range(count):
        lat1, lon1 = ring[index]
        lat2, lon2 = ring[(index + 1) % count]
        if (lat1 > lat) != (lat2 > lat):
            crossing = lon1 + (lat - lat1) * (lon2 - lon1) / (lat2 - lat1)
            if lon < crossing:
                inside = not inside
    return inside


# --- render model ------------------------------------------------------------


@dataclass
class ParkingCallout:
    """One rendered parking block. Additive: never replaces announcement text."""

    matched_text: str
    resolved: bool
    canonical_name: str | None = None
    campus: str | None = None
    campus_display: str | None = None
    permit_class: str | None = None
    location_type: str | None = None
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    map_url: str | None = None
    fallback_map_url: str | None = None
    fallback_map_label: str | None = None
    confidence: str | None = None
    reason: str | None = None

    @property
    def heading(self) -> str:
        if self.resolved and self.canonical_name:
            if self.campus_display and self.campus != "glassboro":
                return f"{self.canonical_name} · {self.campus_display}"
            return self.canonical_name
        return "Parking location"


@dataclass
class ParkingMetrics:
    """Proof that the design is cheap: one row of counters per run."""

    mentions_detected: int = 0
    cache_hits: int = 0
    cache_misses: int = 0
    source_refreshes: int = 0
    resolver_calls: int = 0
    new_resolutions: int = 0
    unresolved: int = 0
    ambiguous: int = 0
    announcements_enriched: int = 0
    errors: list[str] = field(default_factory=list)
    duration_seconds: float = 0.0

    def as_dict(self) -> dict:
        return {
            "mentions_detected": self.mentions_detected,
            "cache_hits": self.cache_hits,
            "cache_misses": self.cache_misses,
            "source_refreshes": self.source_refreshes,
            "resolver_calls": self.resolver_calls,
            "new_resolutions": self.new_resolutions,
            "unresolved": self.unresolved,
            "ambiguous": self.ambiguous,
            "announcements_enriched": self.announcements_enriched,
            "errors": self.errors[:5],
            "duration_seconds": round(self.duration_seconds, 3),
        }
