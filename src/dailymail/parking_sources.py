"""Authoritative Rowan parking sources: registry, fetch, parse, fingerprint.

Read-only by construction: four GETs of published, unauthenticated documents. No
Rowan write endpoint is touched, no announcement API is called, and nothing here
runs on a normal cached day.

The find that shaped this module: Rowan's official "Main Glassboro Campus" page
embeds a Google My Maps layer, and My Maps exposes that layer as KML at
`/maps/d/kml?mid=<id>&forcekml=1`. The KML has a `Parking` folder with a
placemark per lot -- lot name, coordinate, and a use class in the folder name.
That is machine-readable official geometry, so Glassboro needs no geocoding and a
refresh can discover a lot Rowan adds tomorrow.

The same layers exist for Stratford, Camden and Sewell. Stratford's parking
placemarks carry a use class but no lot letter, Camden's layer has no parking
folder at all, and Sewell's two parking points are unnamed -- so those campuses
are covered by `parking_reference`, with these sources still fetched to fingerprint
them and to supply building landmarks for description evidence.
"""

from __future__ import annotations

import hashlib
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from . import parking, parking_reference
from .parking_store import LocationRecord
from .tls import build_ssl_context

log = logging.getLogger("dailymail.parking")

KML_NS = {"k": "http://www.opengis.net/kml/2.2"}
MYMAPS_KML = "https://www.google.com/maps/d/kml?mid={mid}&forcekml=1"
FETCH_TIMEOUT_SECONDS = 30.0
MAX_SOURCE_BYTES = 16 * 1024 * 1024

# The KML folder names Rowan uses, mapped onto our permit vocabulary.
_FOLDER_PERMIT = {
    "commuter parking": "Commuter",
    "employee parking": "Employee",
    "resident parking": "Resident",
    "visitor parking": "Visitor",
    "student parking": "Student",
    "patient parking": "Patient",
    "staff parking": "Employee",
    "student & staff parking": "Mixed",
    "permitted staff parking": "Employee",
}


@dataclass(frozen=True)
class Source:
    """One authoritative document, and what we are willing to believe from it."""

    source_id: str
    campus: str
    source_type: str
    url: str
    map_id: str | None = None
    # True when lots can be parsed straight out of the document.
    machine_readable: bool = False
    # True when the document is Rowan's public parking map for that campus, i.e.
    # the right link for the unresolved fallback.
    is_campus_map: bool = False
    source_version: str | None = None
    note: str = ""


SOURCES: tuple[Source, ...] = (
    Source(
        source_id="glassboro-mymaps",
        campus="glassboro",
        source_type="google_my_maps_kml",
        url=MYMAPS_KML.format(mid="1c2Qlz4nAV57oTio6HbOTgmYTwOoqimKW"),
        map_id="1c2Qlz4nAV57oTio6HbOTgmYTwOoqimKW",
        machine_readable=True,
        note="Embedded by https://www.rowan.edu/about/visiting/main.html; the "
             "Parking folder names every lot and garage with a coordinate.",
    ),
    Source(
        source_id="glassboro-parking-map-pdf",
        campus="glassboro",
        source_type="parking_map_pdf",
        url="https://sites.rowan.edu/publicsafety/_docs/2025-2026-parking-map.pdf",
        is_campus_map=True,
        source_version="2025-2026",
        note="Public Safety's printable parking map. Drawn, not to scale: usable "
             "for lot inventory and as the reader-facing fallback link, not for "
             "coordinates.",
    ),
    Source(
        source_id="glassboro-parking-regulations",
        campus="glassboro",
        source_type="parking_regulations_html",
        url="https://sites.rowan.edu/publicsafety/parking/rulesandregs.html",
        note="Authoritative permit classification per lot for Glassboro and "
             "Stratford.",
    ),
    Source(
        source_id="stratford-mymaps",
        campus="stratford",
        source_type="google_my_maps_kml",
        url=MYMAPS_KML.format(mid="1Sq4QEKv3l7nPp-chZUZpXq5lko4s3PEj"),
        map_id="1Sq4QEKv3l7nPp-chZUZpXq5lko4s3PEj",
        note="Parking placemarks carry a use class but no lot letter; supplies "
             "the building control points and landmark evidence.",
    ),
    Source(
        source_id="stratford-som-campus-map",
        campus="stratford",
        source_type="campus_map_pdf",
        url="https://som.rowan.edu/documents/campus-map-stratford.pdf",
        is_campus_map=True,
        note="Names every Stratford lot letter. Derivation recorded in "
             "parking_reference.",
    ),
    Source(
        source_id="camden-mymaps",
        campus="camden",
        source_type="google_my_maps_kml",
        url=MYMAPS_KML.format(mid="1YhmxFZP-QcEFuleJVKQZ-bgFN0qH2ryG"),
        map_id="1YhmxFZP-QcEFuleJVKQZ-bgFN0qH2ryG",
        note="No parking folder; supplies Camden building landmarks.",
    ),
    Source(
        source_id="camden-cmsru-campus-map",
        campus="camden",
        source_type="campus_map_pdf",
        url="https://cmsru.rowan.edu/documents/admin-documents/camden-campus-map.pdf",
        is_campus_map=True,
        note="Names the Camden garages. Derivation recorded in parking_reference.",
    ),
    Source(
        source_id="camden-cmsru-visitors",
        campus="camden",
        source_type="parking_information_html",
        url="https://cmsru.rowan.edu/resources/visitors/",
        note="Authoritative statement that public parking for the Medical "
             "Education Building is the CCIA garage beside Cooper University "
             "Hospital.",
    ),
    Source(
        source_id="sewell-mymaps",
        campus="sewell",
        source_type="google_my_maps_kml",
        url=MYMAPS_KML.format(mid="1AhzykQJLby6YoadivTofklMIfpMNfsA"),
        map_id="1AhzykQJLby6YoadivTofklMIfpMNfsA",
        note="Two parking placemarks, both unnamed, so no addressable lot can be "
             "cached from it.",
    ),
)

SOURCES_BY_ID = {source.source_id: source for source in SOURCES}


def sources_for(campus: str | None = None) -> list[Source]:
    if campus is None:
        return list(SOURCES)
    return [source for source in SOURCES if source.campus == campus]


def campus_map_source(campus: str) -> Source | None:
    for source in SOURCES:
        if source.campus == campus and source.is_campus_map:
            return source
    return None


def fallback_map_url(campus: str | None) -> tuple[str, str]:
    """The official parking map link shown when a lot cannot be resolved."""
    if campus and campus in parking.CAMPUSES:
        source = campus_map_source(campus)
        spec = parking.CAMPUSES[campus]
        return (source.url if source else spec["map_url"]), spec["map_label"]
    return (
        "https://sites.rowan.edu/publicsafety/parking/",
        "Rowan parking information",
    )


# --- fetch -------------------------------------------------------------------


class SourceFetchError(RuntimeError):
    """An authoritative source could not be retrieved. Never fatal to a digest."""


@dataclass
class FetchedSource:
    source: Source
    content: bytes
    fingerprint: str

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")


def fingerprint_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def fetch(source: Source, *, client: httpx.Client | None = None) -> FetchedSource:
    """GET one published document. Read-only, size-capped, verified TLS."""
    owned = client is None
    if owned:
        client = httpx.Client(
            timeout=FETCH_TIMEOUT_SECONDS,
            verify=build_ssl_context(),
            follow_redirects=True,
            headers={"User-Agent": "DailyMail parking reference (read-only)"},
        )
    try:
        response = client.get(source.url)
        response.raise_for_status()
        content = response.content[:MAX_SOURCE_BYTES]
    except httpx.HTTPError as exc:
        raise SourceFetchError(f"{source.source_id}: {type(exc).__name__}: {exc}") from exc
    finally:
        if owned:
            client.close()
    if not content:
        raise SourceFetchError(f"{source.source_id}: empty response")
    return FetchedSource(source=source, content=content, fingerprint=fingerprint_bytes(content))


def fetch_local(source: Source, path: Path) -> FetchedSource:
    """Load a snapshot instead of the network. Used by the hermetic test suite."""
    content = path.read_bytes()
    return FetchedSource(source=source, content=content, fingerprint=fingerprint_bytes(content))


# --- KML parsing -------------------------------------------------------------


@dataclass
class Landmark:
    """A named campus feature, used as evidence for a plain-English description."""

    name: str
    campus: str
    category: str
    latitude: float
    longitude: float


@dataclass
class ParsedSource:
    source_id: str
    campus: str
    locations: list[LocationRecord] = field(default_factory=list)
    landmarks: list[Landmark] = field(default_factory=list)
    unnamed_parking: int = 0
    warnings: list[str] = field(default_factory=list)


_LOCATION_FIELD = re.compile(r"Location:\s*([-\d.]+)\s*,\s*([-\d.]+)")
_NAME_FIELD = re.compile(r"Name:\s*(.*?)(?:<br>|$)", re.IGNORECASE)

# Folders that describe people-facing places rather than parking. Departments and
# restrooms are excluded from landmark evidence: they duplicate buildings and add
# noise without adding a recognisable reference point.
_LANDMARK_FOLDER_SKIP = frozenset(
    {"parking", "academic departments", "all gender restrooms", "shuttle stop locations"}
)


def _placemark_name(placemark: ET.Element) -> str:
    """Prefer the name embedded in the description; My Maps folders reuse titles.

    Rowan's Parking folder calls every placemark after its folder ("Commuter
    Parking"), and puts the real lot name in the description as `Name: Lot O-1`.
    """
    description = placemark.findtext("k:description", default="", namespaces=KML_NS) or ""
    match = _NAME_FIELD.search(description)
    if match and match.group(1).strip():
        return " ".join(match.group(1).split())
    return " ".join(
        (placemark.findtext("k:name", default="", namespaces=KML_NS) or "").split()
    )


def _placemark_point(placemark: ET.Element) -> tuple[float, float] | None:
    """A single representative coordinate: the point, or a polygon interior point."""
    rings: list[list[tuple[float, float]]] = []
    for coordinates in placemark.iter(f"{{{KML_NS['k']}}}coordinates"):
        points: list[tuple[float, float]] = []
        for triple in (coordinates.text or "").split():
            parts = triple.split(",")
            if len(parts) < 2:
                continue
            try:
                points.append((float(parts[1]), float(parts[0])))
            except ValueError:
                continue
        if points:
            rings.append(points)
    if not rings:
        return None
    ring = max(rings, key=len)
    if len(ring) == 1:
        return ring[0]
    return parking.representative_point(ring)


def _classify(name: str, permit: str) -> str:
    lowered = name.lower()
    if "garage" in lowered:
        return "garage"
    if permit == "Patient":
        return "patient_lot"
    if permit == "Visitor":
        return "visitor_lot"
    return "surface_lot"


def parse_kml(fetched: FetchedSource) -> ParsedSource:
    """Extract parking locations and building landmarks from a My Maps KML."""
    source = fetched.source
    parsed = ParsedSource(source_id=source.source_id, campus=source.campus)
    try:
        document = ET.fromstring(fetched.content).find("k:Document", KML_NS)
    except ET.ParseError as exc:
        raise SourceFetchError(f"{source.source_id}: malformed KML: {exc}") from exc
    if document is None:
        raise SourceFetchError(f"{source.source_id}: KML has no Document element")

    # name -> permit classes seen. A lot listed under both Employee and Visitor
    # (Glassboro's Lot H) is genuinely Mixed, not whichever folder came last.
    permits: dict[str, set[str]] = {}
    points: dict[str, tuple[float, float]] = {}
    order: list[str] = []

    folders = document.findall("k:Folder", KML_NS) or [document]
    for folder in folders:
        folder_name = " ".join(
            (folder.findtext("k:name", default="", namespaces=KML_NS) or "").split()
        )
        lowered = folder_name.lower()
        is_parking = "parking" in lowered

        for placemark in folder.findall("k:Placemark", KML_NS):
            name = _placemark_name(placemark)
            point = _placemark_point(placemark)
            if point is None:
                continue
            latitude, longitude = point

            if is_parking:
                if not source.machine_readable:
                    # Use class without a lot letter: not addressable by name.
                    parsed.unnamed_parking += 1
                    continue
                # In Rowan's Glassboro layer the placemark's own <name> is the
                # use class ("Employee Parking") and the lot name lives in the
                # description as `Name: Lot O-1`. Fall back to the folder name
                # for a layer that is organised the other way round.
                label = " ".join(
                    (placemark.findtext("k:name", default="", namespaces=KML_NS) or "")
                    .split()
                ).lower()
                permit = _FOLDER_PERMIT.get(label) or _FOLDER_PERMIT.get(
                    lowered, "Unknown"
                )
                if not name or name.lower() in (lowered, label):
                    parsed.unnamed_parking += 1
                    continue
                if name not in permits:
                    order.append(name)
                    points[name] = (latitude, longitude)
                permits.setdefault(name, set()).add(permit)
            elif lowered not in _LANDMARK_FOLDER_SKIP and name:
                parsed.landmarks.append(
                    Landmark(
                        name=name,
                        campus=source.campus,
                        category=folder_name or "Locations",
                        latitude=latitude,
                        longitude=longitude,
                    )
                )

    for name in order:
        classes = {value for value in permits[name] if value != "Unknown"}
        permit = (
            next(iter(classes)) if len(classes) == 1
            else ("Mixed" if classes else "Unknown")
        )
        latitude, longitude = points[name]
        location_type = _classify(name, permit)
        try:
            parking.validate_coordinates(latitude, longitude)
        except parking.ParkingDataError as exc:
            parsed.warnings.append(f"{name}: {exc}")
            continue
        parsed.locations.append(
            LocationRecord(
                canonical_id=parking.canonical_id(source.campus, location_type, name),
                campus=source.campus,
                canonical_name=name,
                location_type=location_type,
                permit_class=permit,
                latitude=latitude,
                longitude=longitude,
                source_id=source.source_id,
                source_type=source.source_type,
                source_url=source.url,
                source_map_id=source.map_id,
                source_fingerprint=fetched.fingerprint,
                provenance=(
                    f"Named parking placemark in Rowan's official "
                    f"{parking.CAMPUSES[source.campus]['display']} Google My Maps "
                    f"layer (mid={source.map_id}); folder use class "
                    f"{sorted(permits[name])}"
                ),
                confidence="high",
            )
        )

    # De-duplicate landmarks that appear in several folders (a building can be
    # both an academic building and an athletics venue).
    unique: dict[tuple[str, int, int], Landmark] = {}
    for landmark in parsed.landmarks:
        key = (
            parking.normalize_name(landmark.name),
            round(landmark.latitude, 5),
            round(landmark.longitude, 5),
        )
        unique.setdefault(key, landmark)
    parsed.landmarks = list(unique.values())
    return parsed


def derived_locations(source: Source, fetched: FetchedSource) -> list[LocationRecord]:
    """Reference records for a campus whose map cannot be parsed automatically."""
    records = []
    for entry in parking_reference.DERIVED.get(source.campus, ()):
        records.append(
            LocationRecord(
                canonical_id=parking.canonical_id(
                    source.campus, entry.location_type, entry.canonical_name
                ),
                campus=source.campus,
                canonical_name=entry.canonical_name,
                location_type=entry.location_type,
                permit_class=entry.permit_class,
                latitude=entry.latitude,
                longitude=entry.longitude,
                source_id=source.source_id,
                source_type=source.source_type,
                source_url=source.url,
                source_map_id=source.map_id,
                source_fingerprint=fetched.fingerprint,
                provenance=entry.provenance,
                confidence=entry.confidence,
                aliases=list(entry.aliases),
            )
        )
    return records


def parse(fetched: FetchedSource) -> ParsedSource:
    """Dispatch on source type. Non-map documents are fingerprinted only."""
    source = fetched.source
    if source.source_type == "google_my_maps_kml":
        return parse_kml(fetched)
    if source.source_type == "campus_map_pdf":
        parsed = ParsedSource(source_id=source.source_id, campus=source.campus)
        parsed.locations = derived_locations(source, fetched)
        return parsed
    # HTML/PDF evidence pages: fingerprinted so a change is visible, but nothing
    # is parsed out of them automatically.
    return ParsedSource(source_id=source.source_id, campus=source.campus)
