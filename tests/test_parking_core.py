"""Normalization, alias generation, detection, URLs, validation, geometry.

Pure-function tests. No database, no network, no subprocess -- these cover the
code that runs on every announcement every morning.
"""

from __future__ import annotations

import pytest

from dailymail import parking

# The real historical announcement this feature was built for.
O1_TITLE = "Parking Lot O-1  Closure"
O1_BODY = (
    "Parking Lot O-1 will be closed on Wednesday, August 19, 2026 at 10 pm. "
    "The lot will remain closed until Monday, August 24, 2026."
)


# --- normalization -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Lot O-1", "lot o 1"),
        ("Lot O1", "lot o 1"),
        ("lot  o 1", "lot o 1"),
        ("LOT O—1", "lot o 1"),          # em dash
        ("Parking Lot O-1", "parking lot o 1"),
        ("O-1 Lot", "o 1 lot"),
        ("Lot D-2", "lot d 2"),
        ("Lot A", "lot a"),
        ("411 Ellis Street", "411 ellis street"),
        ("411 Ellis St.", "411 ellis street"),
        ("Rowan Blvd. Parking Garage", "rowan boulevard parking garage"),
        ("Rowan Boulevard Parking Garage", "rowan boulevard parking garage"),
        ("Edgewood Park Apartments Lot", "edgewood park apartments lot"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_name_folds_rowan_spellings(raw, expected):
    assert parking.normalize_name(raw) == expected


def test_normalize_keeps_single_letter_direction_lots_distinct():
    """`Lot W` must not become `Lot West`; `N. Campus Drive` must expand."""
    assert parking.normalize_name("Lot W") == "lot w"
    assert parking.normalize_name("Lot N") == "lot n"
    assert parking.normalize_name("N. Campus Drive") == "north campus drive"


def test_canonical_id_shape_matches_the_documented_form():
    assert parking.canonical_id("glassboro", "surface_lot", "Lot O-1") == "glassboro:lot:o-1"
    assert parking.canonical_id("glassboro", "surface_lot", "Lot A") == "glassboro:lot:a"
    assert parking.canonical_id("stratford", "patient_lot", "Lot A") == "stratford:lot:a"
    assert (
        parking.canonical_id("camden", "garage", "Medical School Parking Garage")
        == "camden:garage:medical-school"
    )


def test_canonical_id_is_campus_scoped():
    """Campus is part of identity, so same-named lots get different ids."""
    assert parking.canonical_id("glassboro", "surface_lot", "Lot A") != parking.canonical_id(
        "stratford", "patient_lot", "Lot A"
    )


# --- alias generation --------------------------------------------------------


def test_generate_aliases_covers_the_spellings_rowan_uses():
    aliases = {parking.normalize_name(a) for a in parking.generate_aliases("Lot O-1")}
    for spelling in ("Parking Lot O-1", "Lot O-1", "O-1 Lot", "Lot O1", "O1", "O-1"):
        assert parking.normalize_name(spelling) in aliases, spelling


def test_generate_aliases_is_deduplicated_by_normalized_form():
    aliases = parking.generate_aliases("Lot O-1")
    keys = [parking.normalize_name(a) for a in aliases]
    assert len(keys) == len(set(keys))


def test_generate_aliases_for_a_named_garage():
    aliases = {parking.normalize_name(a) for a in parking.generate_aliases("Rowan Boulevard Garage")}
    assert parking.normalize_name("Rowan Boulevard Garage") in aliases
    assert parking.normalize_name("Rowan Boulevard Parking Garage") in aliases


def test_scannable_alias_refuses_short_ambiguous_forms():
    """A free-text sweep for `lot a` would match "a lot a few blocks away"."""
    assert not parking.scannable_alias("A")
    assert not parking.scannable_alias("Lot A")
    assert not parking.scannable_alias("O1")
    assert parking.scannable_alias("Lot O-1")
    assert parking.scannable_alias("Rowan Boulevard Garage")
    assert parking.scannable_alias("Edgewood Park Apartments Lot")


# --- detection ---------------------------------------------------------------


def keys_of(mentions):
    return [mention.primary_key for mention in mentions]


def test_detects_the_historical_o1_reference_in_title_and_body():
    mentions = parking.detect_mentions(title=O1_TITLE, body_text=O1_BODY)
    assert keys_of(mentions) == ["lot o 1"]
    assert mentions[0].matched_text.startswith("Parking Lot O-1")
    assert not mentions[0].weak


def test_repeated_mentions_collapse_to_one():
    """`Parking Lot O-1` in the title and again in the body is one lot."""
    body = O1_BODY + " Lot O-1 reopens Monday. O-1 Lot signage will be posted."
    mentions = parking.detect_mentions(title=O1_TITLE, body_text=body)
    assert keys_of(mentions) == ["lot o 1"]


def test_the_o1_body_phrase_the_lot_will_is_not_a_lot_code():
    """The very announcement that motivated this feature contains the trap."""
    mentions = parking.detect_mentions(
        body_text="The lot will remain closed until Monday, August 24, 2026."
    )
    assert mentions == []


@pytest.mark.parametrize(
    "text",
    [
        "There is a lot of interest in this program.",
        "Lots of students attended.",
        "We received a lot a few days ago.",
        "Parking is available in the lot behind the building.",
        "A lot will be announced later.",
        "The parking lot is closed.",
        "Allot time for the survey.",
        "Camelot Hall renovations continue.",
    ],
)
def test_false_positive_avoidance(text):
    assert parking.detect_mentions(body_text=text) == []


@pytest.mark.parametrize(
    "text, expected",
    [
        ("Parking Lot O-1 will close.", ["lot o 1"]),
        ("Lot O-1 will close.", ["lot o 1"]),
        ("The O-1 Lot will close.", ["lot o 1"]),  # same key as `Lot O-1`
        ("Lot O1 will close.", ["lot o 1"]),
        ("Lot A is closed.", ["lot a"]),
        ("Lot D-2 resurfacing begins.", ["lot d 2"]),
        ("Lot 301 is unavailable.", ["lot 301"]),
    ],
)
def test_detects_each_documented_pattern(text, expected):
    assert keys_of(parking.detect_mentions(body_text=text)) == expected


def test_detects_multiple_lots_in_one_announcement():
    mentions = parking.detect_mentions(
        title="Paving in Lot O-1 and Lot D-2",
        body_text="Lot O-1 and Lot D-2 close Friday; Lot Z-1 stays open.",
    )
    assert set(keys_of(mentions)) == {"lot o 1", "lot d 2", "lot z 1"}


def test_detects_a_lot_list():
    mentions = parking.detect_mentions(body_text="Lots A, B-1 and C-1 will be swept.")
    assert set(keys_of(mentions)) == {"lot a", "lot b 1", "lot c 1"}


def test_detects_a_named_garage():
    mentions = parking.detect_mentions(
        body_text="The Rowan Boulevard Garage elevator is out of service."
    )
    assert "rowan boulevard garage" in keys_of(mentions)


def test_bare_parking_garage_is_recognized_but_kept_separate():
    mentions = parking.detect_mentions(body_text="The Parking Garage will close at 6 pm.")
    assert [m.method for m in mentions] == ["pattern_bare_garage"]


def test_a_bare_single_letter_code_is_marked_weak():
    """`Lot I will be closed` must never trigger paid research."""
    mentions = parking.detect_mentions(body_text="Lot I will be closed.")
    assert [m.weak for m in mentions] == [True]
    strong = parking.detect_mentions(body_text="Lot Q-7 will be closed.")
    assert [m.weak for m in strong] == [False]


def test_event_location_is_inspected():
    mentions = parking.detect_mentions(
        title="Career Fair", body_text="Come along.", event_location="Lot O-2 tent"
    )
    assert keys_of(mentions) == ["lot o 2"]


def test_named_alias_scan_finds_a_cached_facility():
    aliases = {"edgewood park apartments lot": "Edgewood Park Apartments Lot"}
    mentions = parking.detect_mentions(
        body_text="Resurfacing of the Edgewood Park Apartments lot begins Monday.",
        named_aliases=aliases,
    )
    assert "edgewood park apartments lot" in keys_of(mentions)


def test_detection_is_stable_and_ordered_by_first_appearance():
    mentions = parking.detect_mentions(
        title="Lot D-2 update", body_text="Also Lot O-1 and then Lot D-2 again."
    )
    assert keys_of(mentions) == ["lot d 2", "lot o 1"]


# --- Google Maps URL ---------------------------------------------------------


def test_maps_url_is_the_documented_stable_form():
    assert (
        parking.maps_url(39.712482, -75.120453)
        == "https://www.google.com/maps/search/?api=1&query=39.712482,-75.120453"
    )


def test_maps_url_is_generated_not_an_opaque_share_link():
    url = parking.maps_url(39.8302355, -75.0068015)
    assert url.startswith("https://www.google.com/maps/search/?api=1&query=")
    assert "goo.gl" not in url
    # Mobile-safe: a plain https link with no scheme handler and no JavaScript.
    assert url.count("?") == 1 and " " not in url


def test_maps_url_trims_trailing_zeros_without_losing_precision():
    assert parking.maps_url(39.8315, -75.004111).endswith("query=39.8315,-75.004111")


# --- coordinate validation ---------------------------------------------------


def test_coordinates_inside_rowans_footprint_are_accepted():
    assert parking.validate_coordinates(39.712482, -75.120453) == (39.712482, -75.120453)
    assert parking.validate_coordinates("39.9412", "-75.1184")[0] == pytest.approx(39.9412)


@pytest.mark.parametrize(
    "latitude, longitude",
    [
        (0.0, 0.0),                      # null island
        (-75.120453, 39.712482),         # transposed
        (39.712482, 75.120453),          # sign dropped
        (51.5074, -0.1278),              # London
        (float("nan"), -75.12),
        (float("inf"), -75.12),
        ("north", "west"),
        (None, None),
    ],
)
def test_bad_coordinates_are_refused(latitude, longitude):
    with pytest.raises(parking.ParkingDataError):
        parking.validate_coordinates(latitude, longitude)


# --- description validation --------------------------------------------------

EVIDENCE = ["James Hall", "Wilson Hall", "Richard Wackar Stadium", "Glassboro"]


def test_a_good_description_is_accepted():
    text = "Employee lot northwest of James Hall, directly south of Richard Wackar Stadium."
    assert parking.validate_description(text, evidence_names=EVIDENCE) == text


def test_a_description_naming_an_unsupported_landmark_is_refused():
    with pytest.raises(parking.ParkingDataError, match="not in the supplied evidence"):
        parking.validate_description(
            "Employee lot beside Bunce Hall near the Student Center.",
            evidence_names=EVIDENCE,
        )


@pytest.mark.parametrize(
    "text, reason",
    [
        ("", "empty"),
        ("Near James Hall.", "words"),
        ("Located at 39.712482, -75.120453 on the campus grid near James Hall.", "coordinate"),
        (
            "Conveniently located employee lot immediately northwest of James Hall today.",
            "marketing",
        ),
        (
            "Employee lot northwest of James Hall, see https://example.com for details.",
            "URL",
        ),
        (" ".join(["word"] * 40), "too long"),
    ],
)
def test_bad_descriptions_are_refused(text, reason):
    with pytest.raises(parking.ParkingDataError):
        parking.validate_description(text, evidence_names=EVIDENCE)


def test_permit_words_and_directions_are_allowed_vocabulary():
    text = "Patient lot between the University Educational Center and the Academic Center."
    assert parking.validate_description(
        text, evidence_names=["University Educational Center", "Academic Center"]
    )


# --- campus inference --------------------------------------------------------

LANDMARKS = {
    "james hall": "glassboro",
    "richard wackar stadium": "glassboro",
    "academic center": "stratford",
    "rowan medicine": "stratford",
    "camden academic building": "camden",
}


def test_category_decides_the_campus():
    campus, why = parking.infer_campus(category_title="Stratford Campus")
    assert campus == "stratford"
    assert "category" in why


def test_a_named_building_decides_the_campus():
    campus, why = parking.infer_campus(
        title="Lot A closure", body_text="Access to Richard Wackar Stadium is unaffected.",
        landmarks=LANDMARKS,
    )
    assert campus == "glassboro"
    assert "building" in why


def test_a_campus_keyword_decides_the_campus():
    campus, _ = parking.infer_campus(body_text="The Stratford campus lot will close.")
    assert campus == "stratford"


def test_no_campus_evidence_yields_none():
    campus, why = parking.infer_campus(title="Lot A closure", body_text="Lot A is closed.")
    assert campus is None
    assert why == "no campus evidence"


def test_conflicting_campus_keywords_yield_none():
    campus, why = parking.infer_campus(
        body_text="Shuttles run between the Glassboro campus and the Camden campus."
    )
    assert campus is None
    assert "conflicting" in why


# --- geometry ----------------------------------------------------------------


def test_compass_direction_and_distance_against_the_official_o1_geometry():
    """Rowan's own coordinates: Lot O-1, James Hall, Richard Wackar Stadium."""
    o1 = (39.712482, -75.120453)
    james_hall = (39.711788, -75.119484)
    stadium = (39.714104, -75.120438)
    assert parking.compass_direction(*o1, *james_hall) == "southeast"
    assert parking.compass_direction(*o1, *stadium) == "north"
    assert parking.distance_metres(*o1, *james_hall) == pytest.approx(113, abs=5)
    assert parking.distance_metres(*o1, *stadium) == pytest.approx(181, abs=5)


def test_representative_point_of_a_convex_ring_is_its_centroid():
    ring = [(39.7120, -75.1210), (39.7120, -75.1200), (39.7130, -75.1200), (39.7130, -75.1210)]
    latitude, longitude = parking.representative_point(ring)
    assert latitude == pytest.approx(39.7125, abs=1e-4)
    assert longitude == pytest.approx(-75.1205, abs=1e-4)
    assert parking.point_in_ring((latitude, longitude), ring)


def test_representative_point_of_an_l_shape_stays_inside_the_polygon():
    """An irregular lot's arithmetic centroid can land on a building."""
    ring = [
        (39.7120, -75.1210), (39.7120, -75.1190), (39.7124, -75.1190),
        (39.7124, -75.1206), (39.7140, -75.1206), (39.7140, -75.1210),
    ]
    point = parking.representative_point(ring)
    assert parking.point_in_ring(point, ring)


def test_the_official_o1_point_lies_inside_the_surveyed_o1_footprint():
    """Cross-check of Rowan's published point against the mapped lot outline."""
    footprint = [
        (39.7127, -75.12005), (39.712515, -75.119821), (39.712416, -75.119732),
        (39.712336, -75.119844), (39.711881, -75.120454), (39.711916, -75.120499),
        (39.711869, -75.120556), (39.712177, -75.120937), (39.71225, -75.121028),
        (39.712336, -75.121136), (39.712371, -75.12109), (39.712383, -75.121103),
        (39.712401, -75.121111), (39.712416, -75.121119), (39.71243, -75.121119),
        (39.712456, -75.12111), (39.712474, -75.1211), (39.712525, -75.121036),
        (39.712551, -75.121002), (39.712588, -75.121043), (39.712611, -75.121045),
        (39.713023, -75.12049), (39.71298, -75.120426), (39.712941, -75.120352),
        (39.712802, -75.120179),
    ]
    assert parking.point_in_ring((39.712482, -75.120453), footprint)


def test_confidence_ordering():
    assert parking.confidence_at_least("high", "medium")
    assert parking.confidence_at_least("medium", "medium")
    assert not parking.confidence_at_least("low", "medium")
    assert not parking.confidence_at_least(None, "medium")
