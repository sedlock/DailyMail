"""Derived reference records for authoritative sources that are not machine-readable.

Glassboro needs nothing here: Rowan publishes its Glassboro campus map as a
Google My Maps layer whose KML names every lot and gives its coordinate, so
`parking_sources` parses it directly and a refresh can discover a brand-new lot
on its own.

Stratford and Camden are different. Rowan publishes those parking layouts only
as drawn campus maps (PDF), and the corresponding My Maps parking placemarks
carry a use class (`Patient Parking`) but no lot letter. Neither source alone
answers "where is Stratford Lot D-3". So the two were combined once, by hand,
with the derivation recorded below:

1. The official campus-map PDF was text-extracted with per-label page
   coordinates.
2. An affine transform from page space to WGS84 was least-squares fitted using
   the campus's own My Maps *building* placemarks as control points.
3. The fit was checked: residuals at the control points, and a prediction
   checked against a landmark that was not used in the fit.
4. Each transformed lot label was matched to the nearest official My Maps
   *parking* placemark. Within 40 m with a consistent use class, the official
   Rowan point is stored -- it is authoritative and certain to be inside the lot.
   Otherwise the transformed position is stored and the record is marked `low`.

`REFERENCE_NOTES` carries the numbers for each campus so the derivation is
auditable from the code rather than from memory. A source whose fingerprint later
changes is flagged for human review; these records are never silently
regenerated, because step 1 needs a human.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DerivedLocation:
    """One lot whose identity and geometry come from two combined sources."""

    canonical_name: str
    latitude: float
    longitude: float
    location_type: str
    permit_class: str
    confidence: str
    provenance: str
    aliases: tuple[str, ...] = ()


# --- Stratford ---------------------------------------------------------------
#
# Identity and lot letters: https://som.rowan.edu/documents/campus-map-stratford.pdf
# Control points and parking geometry: Rowan-Virtua SOM (Stratford) My Maps,
#   mid=1Sq4QEKv3l7nPp-chZUZpXq5lko4s3PEj
# Affine fit residuals at the five building control points: 2.2, 2.7, 3.1, 8.1,
#   9.3 m (mean 5.1 m).
_MYMAPS = "official Rowan Stratford My Maps parking placemark"
_PDF = "official Rowan-Virtua SOM Stratford campus map"

STRATFORD: tuple[DerivedLocation, ...] = (
    DerivedLocation(
        "Lot A", 39.8302355, -75.0068015, "patient_lot", "Patient", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 22 m away (Patient); the "
        f"parking regulations name Lot A as the only Stratford lot needing no permit",
    ),
    DerivedLocation(
        "Lot B", 39.8298487, -75.0085822, "surface_lot", "Employee", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 3 m away (Staff); listed as a "
        f"permit lot in Rowan's parking regulations",
    ),
    DerivedLocation(
        "Lot C", 39.8311892, -75.0060987, "surface_lot", "Unknown", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 34 m away; the campus map lists "
        f"Lot C as permit parking while the regulations page omits it, so the use "
        f"class is left Unknown",
    ),
    DerivedLocation(
        "Lot E", 39.8307835, -75.0056962, "surface_lot", "Employee", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 20 m away (Permitted Staff); "
        f"listed as a permit lot in Rowan's parking regulations",
    ),
    DerivedLocation(
        "Lot D-1", 39.8291892, -75.0052751, "surface_lot", "Mixed", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 10 m away (Student & Staff)",
        ("Lot D1",),
    ),
    DerivedLocation(
        "Lot D-2", 39.8293334, -75.0046769, "surface_lot", "Mixed", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 20 m away (Student & Staff)",
        ("Lot D2",),
    ),
    DerivedLocation(
        "Lot D-3", 39.8296918, -75.0046233, "surface_lot", "Mixed", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 15 m away (Student & Staff)",
        ("Lot D3",),
    ),
    DerivedLocation(
        "Lot D-4", 39.8300543, -75.0045589, "surface_lot", "Mixed", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 14 m away (Student & Staff)",
        ("Lot D4",),
    ),
    DerivedLocation(
        "Lot D-5", 39.8304292, -75.0046608, "surface_lot", "Mixed", "medium",
        f"{_PDF} label georeferenced to a {_MYMAPS} 24 m away (Student & Staff); "
        f"present on the campus map but not in the regulations lot list",
        ("Lot D5",),
    ),
    DerivedLocation(
        "Lot D-6", 39.830849, -75.005225, "surface_lot", "Unknown", "low",
        f"{_PDF} label georeferenced; nearest official parking placemark is 41 m "
        f"away, so the position is the transformed label rather than a Rowan point",
        ("Lot D6",),
    ),
    DerivedLocation(
        "Lot G", 39.831500, -75.004111, "surface_lot", "Unknown", "low",
        f"{_PDF} label 'Black Tie Lot [LOT G]' georeferenced; no official parking "
        f"placemark within 100 m, so the position is the transformed label only",
        ("Black Tie Lot",),
    ),
)

# --- Camden / CMSRU ----------------------------------------------------------
#
# Identity: https://cmsru.rowan.edu/documents/admin-documents/camden-campus-map.pdf
# Control points: Camden Campus & CMSRU My Maps,
#   mid=1YhmxFZP-QcEFuleJVKQZ-bgFN0qH2ryG (that layer has no parking folder).
# Only two Rowan buildings appear on the CMSRU map, so a two-point north-up
# isotropic transform was used and then checked against a landmark outside the
# fit (Walter Rand Transportation Center garage, 12 m) and against independently
# surveyed garage footprints.
_CMSRU_MAP = "official CMSRU Camden campus map"

CAMDEN: tuple[DerivedLocation, ...] = (
    DerivedLocation(
        "Medical School Parking Garage", 39.940324, -75.120705, "garage",
        "Mixed", "medium",
        f"{_CMSRU_MAP} label georeferenced from the Rowan My Maps points for CMSRU "
        f"and the Joint Health Sciences Center; agrees within 17 m with an "
        f"independently surveyed multi-storey garage footprint",
        ("Medical School Garage", "CMSRU Parking Garage"),
    ),
    DerivedLocation(
        "Hospital Parking Garage", 39.941267, -75.118432, "garage",
        "Visitor", "medium",
        f"{_CMSRU_MAP} label georeferenced as above; agrees within 15 m with an "
        f"independently surveyed multi-storey garage footprint. CMSRU's visitor "
        f"page names this Camden County Improvement Authority garage as the public "
        f"parking for the Medical Education Building",
        ("CCIA Garage", "Cooper Hospital Parking Garage"),
    ),
    DerivedLocation(
        "Camden County College Garage", 39.947404, -75.118494, "garage",
        "Student", "low",
        "Rowan's Camden campus information page names the Camden County College "
        "garage across from the Camden Academic Building as Rowan student parking; "
        "the position is an independently surveyed garage footprint, not a Rowan "
        "coordinate",
        ("Camden County College Parking Garage",),
    ),
    DerivedLocation(
        "Sheridan Parking Garage", 39.941438, -75.113224, "garage",
        "Unknown", "low",
        f"{_CMSRU_MAP} label georeferenced; the label sits far from both control "
        f"points and the position differs by 59 m from an independently surveyed "
        f"garage footprint, so it is recorded as low confidence",
    ),
)

DERIVED: dict[str, tuple[DerivedLocation, ...]] = {
    "stratford": STRATFORD,
    "camden": CAMDEN,
}

REFERENCE_NOTES: dict[str, str] = {
    "stratford": (
        "Affine page->WGS84 fit on five official My Maps building placemarks; "
        "residuals 2.2/2.7/3.1/8.1/9.3 m. Nine of eleven lots take their stored "
        "coordinate from an official Rowan parking placemark within 40 m. Rowan's "
        "parking regulations also name Stratford permit lots F and H, which appear "
        "on neither current map, so no coordinate exists for them and they are not "
        "cached."
    ),
    "camden": (
        "Two-point north-up isotropic fit (CMSRU and Joint Health Sciences Center "
        "My Maps points, 1.283 m per page unit), validated 12 m against a landmark "
        "outside the fit. Rowan publishes no Camden parking geometry of its own."
    ),
}
