"""Opt-in live probes against Rowan's real parking sources.

Excluded from the normal suite: every test here needs the network, and the whole
point of the rest of the suite is that it does not. Run deliberately, when you
want to know whether Rowan has changed something:

    DAILYMAIL_LIVE_PARKING=1 uv run pytest tests/test_parking_live.py -v

These are the tests that would catch a My Maps layer being deleted, a lot being
renamed, or the parking-map PDF being replaced -- the failures that would
otherwise show up months later as an unresolved lot in somebody's inbox.
"""

from __future__ import annotations

import os

import pytest

from dailymail import parking, parking_reference, parking_sources

pytestmark = pytest.mark.skipif(
    not os.environ.get("DAILYMAIL_LIVE_PARKING"),
    reason="set DAILYMAIL_LIVE_PARKING=1 to probe Rowan's live parking sources",
)


@pytest.fixture(scope="module")
def fetched() -> dict:
    out = {}
    for source in parking_sources.SOURCES:
        try:
            out[source.source_id] = parking_sources.fetch(source)
        except parking_sources.SourceFetchError as exc:
            out[source.source_id] = exc
    return out


def test_every_registered_source_is_reachable(fetched):
    failures = {
        source_id: str(value)
        for source_id, value in fetched.items()
        if isinstance(value, Exception)
    }
    assert not failures, f"unreachable authoritative source(s): {failures}"


def test_the_glassboro_layer_still_exposes_parsable_lot_geometry(fetched):
    parsed = parking_sources.parse(fetched["glassboro-mymaps"])
    assert len(parsed.locations) >= 30, "Glassboro lot count collapsed"
    names = {record.canonical_name for record in parsed.locations}
    # A representative spread across the campus and across use classes.
    for expected in ("Lot O-1", "Lot A", "Lot H", "Lot Z-1", "Rowan Boulevard Garage"):
        assert expected in names, f"{expected} vanished from Rowan's official layer"
    assert parsed.landmarks, "no building landmarks in the Glassboro layer"


def test_o1_is_still_where_we_cached_it(fetched):
    parsed = parking_sources.parse(fetched["glassboro-mymaps"])
    o1 = next(r for r in parsed.locations if r.canonical_name == "Lot O-1")
    assert o1.permit_class == "Employee"
    # Within a lot's width of the coordinate this feature was built around.
    assert parking.distance_metres(
        o1.latitude, o1.longitude, 39.712482, -75.120453
    ) < 60


def test_the_glassboro_landmarks_used_for_o1_still_exist(fetched):
    parsed = parking_sources.parse(fetched["glassboro-mymaps"])
    names = {landmark.name for landmark in parsed.landmarks}
    assert "James Hall" in names
    assert "Richard Wackar Stadium" in names


def test_the_stratford_control_points_have_not_moved(fetched):
    """The Stratford lot derivation is anchored to these five building points."""
    parsed = parking_sources.parse(fetched["stratford-mymaps"])
    by_name = {
        parking.normalize_name(landmark.name): landmark for landmark in parsed.landmarks
    }
    control = {
        "113 laurel road": (39.829687, -75.0086499),
        "univeristy educational center": (39.8294727, -75.007518),
        "academic center": (39.8296061, -75.0064812),
        "science center": (39.8300135, -75.0055346),
        "rowan medicine": (39.8310487, -75.0068129),
    }
    for key, (latitude, longitude) in control.items():
        assert key in by_name, f"Stratford control point {key!r} is gone"
        landmark = by_name[key]
        moved = parking.distance_metres(
            landmark.latitude, landmark.longitude, latitude, longitude
        )
        assert moved < 25, f"{key} moved {moved:.0f} m; re-derive the Stratford lots"


def test_the_stratford_parking_points_backing_the_derivation_still_exist(fetched):
    parsed = parking_sources.parse(fetched["stratford-mymaps"])
    assert parsed.unnamed_parking >= 10, (
        "Stratford's parking placemark count dropped; the lot derivation in "
        "parking_reference may no longer hold"
    )
    # And every stored Stratford coordinate should sit close to one of them.
    from xml.etree import ElementTree as ET

    document = ET.fromstring(fetched["stratford-mymaps"].content).find(
        "{http://www.opengis.net/kml/2.2}Document"
    )
    points = []
    for folder in document.findall("{http://www.opengis.net/kml/2.2}Folder"):
        name = (folder.findtext("{http://www.opengis.net/kml/2.2}name") or "").lower()
        if "parking" not in name:
            continue
        for coordinates in folder.iter("{http://www.opengis.net/kml/2.2}coordinates"):
            longitude, latitude, *_ = coordinates.text.strip().split(",")
            points.append((float(latitude), float(longitude)))
    assert points

    for entry in parking_reference.STRATFORD:
        if entry.confidence == "low":
            continue  # low-confidence records are georeferenced, not point-matched
        nearest = min(
            parking.distance_metres(entry.latitude, entry.longitude, *point)
            for point in points
        )
        assert nearest < 5, (
            f"{entry.canonical_name} no longer matches an official Rowan parking "
            f"placemark (nearest {nearest:.0f} m)"
        )


def test_the_camden_layer_still_has_no_parking_folder(fetched):
    """If Rowan ever adds one, we should switch Camden off the derived table."""
    parsed = parking_sources.parse(fetched["camden-mymaps"])
    assert parsed.locations == []
    assert parsed.landmarks, "Camden layer lost its building placemarks"
    names = {parking.normalize_name(l.name) for l in parsed.landmarks}
    assert "camden academic building" in names


def test_the_official_pdf_and_html_sources_are_the_expected_document_type(fetched):
    expectations = {
        "glassboro-parking-map-pdf": b"%PDF",
        "stratford-som-campus-map": b"%PDF",
        "camden-cmsru-campus-map": b"%PDF",
    }
    for source_id, magic in expectations.items():
        assert fetched[source_id].content.startswith(magic), source_id
    for source_id in ("glassboro-parking-regulations", "camden-cmsru-visitors"):
        text = fetched[source_id].text.lower()
        assert "parking" in text, source_id


def test_the_regulations_page_still_documents_the_lot_designations(fetched):
    text = " ".join(fetched["glassboro-parking-regulations"].text.split()).lower()
    # The classifications the Glassboro records lean on.
    assert "o-1" in text
    for word in ("employee", "commuter", "resident"):
        assert word in text


def test_fingerprints_are_stable_across_two_fetches():
    source = parking_sources.SOURCES_BY_ID["glassboro-mymaps"]
    first = parking_sources.fetch(source)
    second = parking_sources.fetch(source)
    assert first.fingerprint == second.fingerprint


def test_a_generated_maps_url_for_o1_is_well_formed_and_reachable():
    """The link the reader taps. A HEAD is enough; we do not scrape Maps."""
    import httpx

    from dailymail.tls import build_ssl_context

    url = parking.maps_url(39.712482, -75.120453)
    with httpx.Client(
        timeout=20.0, verify=build_ssl_context(), follow_redirects=True
    ) as client:
        response = client.head(url)
    assert response.status_code < 400, f"{url} -> {response.status_code}"
