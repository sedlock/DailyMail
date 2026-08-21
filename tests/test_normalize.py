"""Allowlist, sentinel normalization, New/Standing, audience mapping, body handling."""

from __future__ import annotations

import pytest

from dailymail import normalize
from dailymail.normalize import (
    FORBIDDEN_KEYS,
    assert_no_forbidden_fields,
    body_diagnostics,
    html_to_text,
    normalize_category_registry,
    normalize_date,
    normalize_record,
    normalize_text,
    normalize_time,
)

from conftest import TARGET_DATE

# A deliberately hostile record: every field the drop policy names, present with
# a realistic-looking value. The Phase 0 fixtures already had these stripped, so
# testing against them alone would prove nothing.
HOSTILE_RECORD = {
    "FullBody": "<p>Body <a href='https://rowan.edu'>link</a></p>",
    "ShortBody": "Body link",
    "SelectedCategory": True,
    "DistributionDates": {"List": ["2026-08-20", "2026-08-25"]},
    "Category": {
        "Id": 3,
        "Title": "Public Safety",
        "Rank": 3,
        "Is_Active": True,
        "RestrictSubmitting": False,
        "Color": "orange-darker",
        "ApprovalProcess": "Organization",
    },
    "User": {
        "Id": 90,
        "Name": "Some Person",
        "Username": "person",
        "Password": "",
        "Email": "person@rowan.edu",
        "External_Id": "910009097",
        "Last_Login": "2026-08-20T15:20:36.783Z",
        "Is_Active": True,
    },
    "Submission": {
        "Id": "6622",
        "Title": "  Padded Title  ",
        "Audience": "Both",
        "Category": 3,
        "SubmittedStatus": "Approved",
        "SubmissionBody": "PHA+Ym9keTwvcD4=",
        "SubmittedDate": "2026-08-18T17:50:49Z",
        "ApprovedDate": "2026-08-18T17:50:59Z",
        "UpdatedDate": "1900-01-01T00:00:00",
        "UpdatedByName": "",
        "UpdatedById": 41,
        "SubmittedById": 90,
        "SubmittedByExternalId": "910009097",
        "ApprovedById": 90,
        "ApproverExternalId": "910009098",
        "SubmittedByName": "Some Person",
        "SubmittedByDepartment": "Public Safety (S001627)",
        "SubmittedByJobTitle": "Administrator",
        "SubmittedByEmail": "person@rowan.edu",
        "SubmittedByPhone": "",
        "ApprovedByName": "Some Approver",
        "ApprovedByDepartment": "Public Safety (S001627)",
        "ApprovedByJobTitle": "Administrator",
        "ApprovedByEmail": "approver@rowan.edu",
        "ApprovedByPhone": "(856) 256-4762",
        "ContactName": "",
        "ContactDepartment": "Parking Office",
        "ContactJobTitle": "",
        "ContactRowanEmail": "parking@rowan.edu",
        "ContactPhone": "856-256-4575",
        "Event": False,
        "EventName": "",
        "EventDate": "1900-01-01",
        "EventStartTime": "00:00:00",
        "EventEndTime": "00:00:00",
        "EventLocation": "",
        "EventNoEnd": False,
        "ExtraEdition": False,
        "ExtraEditionDateSent": "1900-01-01T00:00:00",
        "IsDeleted": False,
        "QuestionForApprover": "please approve quickly",
        "ReadyForSubmission": False,
        "RejectComment": "",
        "RejectReasonId": "0",
        "OldSubmissionId": 35940,
    },
}


def _normalized_hostile():
    return normalize_record(
        HOSTILE_RECORD, target_date=TARGET_DATE, source_query_membership=["Employees"]
    )


# --- allowlist / drop policy -------------------------------------------------


def test_forbidden_fields_absent_from_normalized_output():
    out = _normalized_hostile()
    assert_no_forbidden_fields(out)  # must not raise
    for key in FORBIDDEN_KEYS:
        assert key not in out, key


def test_pii_values_do_not_survive_normalization():
    """The specific values, not just the key names, must be gone."""
    import json

    rendered = json.dumps(_normalized_hostile())
    for leaked in (
        "910009097",       # Banner ID
        "910009098",       # approver Banner ID
        "Last_Login",
        "2026-08-20T15:20:36.783Z",
        "please approve quickly",   # QuestionForApprover
        "PHA+Ym9keTwvcD4=",         # SubmissionBody base64
        "35940",                    # OldSubmissionId
    ):
        assert leaked not in rendered, leaked


def test_internal_user_ids_dropped():
    out = _normalized_hostile()
    for key in ("submitted_by_id", "approved_by_id", "updated_by_id"):
        assert key not in out


def test_allowlist_is_closed_against_new_upstream_fields():
    """A field Rowan adds tomorrow must not appear automatically."""
    record = {**HOSTILE_RECORD}
    record["Submission"] = {
        **HOSTILE_RECORD["Submission"],
        "BrandNewUpstreamField": "surprise",
        "AnotherOne": {"nested": "value"},
    }
    out = normalize_record(
        record, target_date=TARGET_DATE, source_query_membership=["Employees"]
    )
    import json

    assert "surprise" not in json.dumps(out)
    assert "BrandNewUpstreamField" not in out


def test_published_contact_fields_are_retained():
    """These are part of the announcement as published and must survive."""
    out = _normalized_hostile()
    assert out["submitted_by_name"] == "Some Person"
    assert out["submitted_by_email"] == "person@rowan.edu"
    assert out["submitted_by_department"] == "Public Safety (S001627)"
    assert out["approved_by_name"] == "Some Approver"
    assert out["approved_by_email"] == "approver@rowan.edu"
    assert out["approved_by_phone"] == "(856) 256-4762"
    assert out["contact_department"] == "Parking Office"
    assert out["contact_email"] == "parking@rowan.edu"
    assert out["contact_phone"] == "856-256-4575"


def test_assert_no_forbidden_fields_actually_detects_a_leak():
    with pytest.raises(AssertionError, match="User"):
        assert_no_forbidden_fields({"announcements": [{"User": {"Username": "x"}}]})
    with pytest.raises(AssertionError, match="SubmissionBody"):
        assert_no_forbidden_fields({"a": {"b": [{"SubmissionBody": "x"}]}})


# --- body handling -----------------------------------------------------------


def test_full_body_retained_verbatim():
    out = _normalized_hostile()
    assert out["full_body"] == HOSTILE_RECORD["FullBody"]


def test_submission_body_omitted():
    out = _normalized_hostile()
    assert "submission_body" not in out
    assert "SubmissionBody" not in out


def test_full_body_not_rewritten_even_when_links_are_malformed():
    """Phase 1 preserves the archival HTML; the renderer fixes links later."""
    body = (
        '<p><a href=" go.rowan.edu/x ">a</a>'
        '<a href="file://rowanads.rowan.edu/home/x">b</a></p>'
    )
    record = {**HOSTILE_RECORD, "FullBody": body}
    out = normalize_record(
        record, target_date=TARGET_DATE, source_query_membership=["Employees"]
    )
    assert out["full_body"] == body
    assert "file://" in out["full_body"]


def test_body_text_derived_and_readable():
    body = "<p>First para.</p><ul><li>Item&nbsp;one</li><li>Item two</li></ul>"
    text = html_to_text(body)
    assert "<" not in text
    # &nbsp; decoded to a normal space.
    assert "Item one" in text
    # Each block lands on its own line rather than welding onto the previous one.
    assert [line for line in text.split("\n") if line] == [
        "First para.",
        "Item one",
        "Item two",
    ]


def test_body_text_strips_script_and_style_content():
    body = "<p>Safe</p><script>alert('x')</script><style>p{color:red}</style>"
    text = html_to_text(body)
    assert "Safe" in text
    assert "alert" not in text
    assert "color:red" not in text


def test_body_diagnostics_counts_images_and_data_uris():
    body = (
        '<p><img src="data:image/png;base64,AAAA"><img src="https://x/y.png">'
        '<a href="https://rowan.edu">l</a></p>'
    )
    diagnostics = body_diagnostics(body)
    assert diagnostics["image_count"] == 2
    assert diagnostics["data_uri_count"] == 1
    assert diagnostics["data_uri_bytes"] > 0
    assert diagnostics["link_count"] == 1
    assert diagnostics["body_bytes"] == len(body.encode())


def test_body_diagnostics_on_empty_body():
    assert body_diagnostics("")["body_bytes"] == 0
    assert body_diagnostics(None)["image_count"] == 0


def test_large_data_uri_image_is_measured_not_transformed(event_record):
    """Phase 1 must not resize, strip or extract inline images."""
    body = '<p><img src="data:image/png;base64,' + ("A" * 5000) + '"></p>'
    record = {**HOSTILE_RECORD, "FullBody": body}
    out = normalize_record(
        record, target_date=TARGET_DATE, source_query_membership=["Employees"]
    )
    assert out["full_body"] == body
    assert out["body_diagnostics"]["data_uri_count"] == 1
    assert out["body_diagnostics"]["data_uri_bytes"] >= 5000


# --- sentinel normalization --------------------------------------------------


@pytest.mark.parametrize(
    "value", ["1900-01-01", "1900-01-01T00:00:00", "1900-01-01T00:00:00Z", "", "   "]
)
def test_date_sentinels_become_null(value):
    assert normalize_date(value) is None


def test_real_dates_preserved():
    assert normalize_date("2026-08-20") == "2026-08-20"
    assert normalize_date("2026-08-18T17:50:49Z") == "2026-08-18T17:50:49Z"


def test_time_sentinel_becomes_null():
    assert normalize_time("00:00:00") is None
    assert normalize_time("") is None
    assert normalize_time("10:00:00") == "10:00:00"


def test_text_sentinels_become_null_and_outer_whitespace_trimmed():
    assert normalize_text("") is None
    assert normalize_text("   ") is None
    assert normalize_text("  hello  ") == "hello"


def test_sentinels_applied_to_record_fields():
    out = _normalized_hostile()
    assert out["updated_date"] is None
    assert out["updated_by_name"] is None
    assert out["event_date"] is None
    assert out["event_start_time"] is None
    assert out["event_end_time"] is None
    assert out["event_name"] is None
    assert out["contact_name"] is None
    assert out["contact_job_title"] is None
    assert out["submitted_by_phone"] is None


def test_legitimate_zeros_and_falses_are_not_converted():
    """Only documented sentinels are nulled; real booleans stay booleans."""
    out = _normalized_hostile()
    assert out["is_event"] is False
    assert out["event_no_end"] is False
    assert out["extra_edition"] is False
    assert out["category_id"] == 3  # a real FK, not a sentinel


def test_title_trimmed_only_at_the_edges():
    out = _normalized_hostile()
    assert out["title"] == "Padded Title"
    # Internal double spaces are part of the published subject; keep them.
    record = {**HOSTILE_RECORD}
    record["Submission"] = {**HOSTILE_RECORD["Submission"], "Title": " Parking Lot O-1  Closure"}
    out2 = normalize_record(
        record, target_date=TARGET_DATE, source_query_membership=["Employees"]
    )
    assert out2["title"] == "Parking Lot O-1  Closure"


# --- New / Standing ----------------------------------------------------------


def test_new_when_first_distribution_date_is_target():
    out = normalize_record(
        HOSTILE_RECORD, target_date="2026-08-20", source_query_membership=["Employees"]
    )
    assert out["first_distribution_date"] == "2026-08-20"
    assert out["status"] == "New"


def test_standing_when_target_is_a_later_distribution_date():
    out = normalize_record(
        HOSTILE_RECORD, target_date="2026-08-25", source_query_membership=["Employees"]
    )
    assert out["status"] == "Standing"


def test_distribution_dates_sorted_and_deduplicated():
    record = {**HOSTILE_RECORD, "DistributionDates": {"List": ["2026-08-25", "2026-08-20", "2026-08-20"]}}
    out = normalize_record(
        record, target_date="2026-08-20", source_query_membership=["Employees"]
    )
    assert out["distribution_dates"] == ["2026-08-20", "2026-08-25"]
    assert out["first_distribution_date"] == "2026-08-20"


def test_bare_list_distribution_dates_accepted():
    record = {**HOSTILE_RECORD, "DistributionDates": ["2026-08-20"]}
    out = normalize_record(
        record, target_date="2026-08-20", source_query_membership=["Employees"]
    )
    assert out["distribution_dates"] == ["2026-08-20"]


# --- audience mapping --------------------------------------------------------


@pytest.mark.parametrize(
    "source,label",
    [("Employees", "Employee"), ("Students", "Student"), ("Both", "Everyone")],
)
def test_audience_label_mapping(source, label):
    record = {**HOSTILE_RECORD}
    record["Submission"] = {**HOSTILE_RECORD["Submission"], "Audience": source}
    out = normalize_record(
        record, target_date=TARGET_DATE, source_query_membership=["Employees"]
    )
    assert out["source_audience"] == source
    assert out["audience_label"] == label


def test_source_query_membership_retained_for_cross_validation():
    out = normalize_record(
        HOSTILE_RECORD,
        target_date=TARGET_DATE,
        source_query_membership=["Students", "Employees"],
    )
    assert out["source_query_membership"] == ["Employees", "Students"]


# --- categories --------------------------------------------------------------


def test_category_metadata_retained(employee_fixture):
    out = normalize_record(
        employee_fixture["Announcements"][0],
        target_date=TARGET_DATE,
        source_query_membership=["Employees"],
    )
    category = out["category"]
    assert category["id"] == 18
    assert category["title"] == "Well-being and Health"
    assert category["color"] == "teal-dark"
    assert category["is_active"] is True
    assert "approval_process" in category


def test_category_registry_normalization_and_ordering(employee_fixture):
    registry = normalize_category_registry(employee_fixture["Categories"])
    assert len(registry) == 33
    ids = [entry["id"] for entry in registry]
    assert ids == sorted(ids)
    assert {"id", "title", "rank", "color"} <= set(registry[0])
    # `Count` is audience/date-scoped, so it must not be baked into the registry.
    assert all("count" not in entry for entry in registry)


def test_registry_accepts_categories_it_has_never_seen():
    registry = normalize_category_registry(
        [{"Id": 9999, "Title": "未来 Category", "Rank": 0, "Color": "teal", "Count": "2"}]
    )
    assert registry == [
        {"id": 9999, "title": "未来 Category", "rank": 0, "color": "teal"}
    ]


# --- event records -----------------------------------------------------------


def test_event_record_fields_populated(event_record):
    out = normalize_record(
        event_record, target_date="2026-03-05", source_query_membership=["Employees"]
    )
    assert out["is_event"] is True
    assert out["event_name"] == "Next Level You: What's Holding You Back?"
    assert out["event_date"] == "2026-03-09"
    assert out["event_start_time"] == "09:00:00"
    assert out["event_end_time"] == "16:00:00"
    assert out["event_location"] == "Chamberlain Student Center"


def test_student_only_record_maps_to_student(student_only_record):
    out = normalize_record(
        student_only_record,
        target_date="2026-04-13",
        source_query_membership=["Students"],
    )
    assert out["source_audience"] == "Students"
    assert out["audience_label"] == "Student"
    assert out["status"] == "New"
