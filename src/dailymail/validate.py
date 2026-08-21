"""Mandatory validation gates (Phase 0 §12, rules V1-V9 and V12).

The governing requirement: never confuse "Rowan published nothing today" with
"the collector broke and returned nothing". Those two look identical if you only
look at the announcement count, and are trivially distinguishable if you look at
the response structure:

    genuine empty day -> TotalCount "0", Announcements [], Categories FULLY populated
    stale apiVersion  -> data {}, no TotalCount, no Categories, hasApiVersionChanged

So the structural gate runs first, on every single response, and a zero count is
only ever reported after that gate has passed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import config
from .errors import ValidationError, VersionChangedError

JSON_CONTENT_TYPES = ("application/json", "text/json")


@dataclass
class ParsedResponse:
    """A response that has cleared the V1 structural gate."""

    total_count: int
    announcements: list[dict]
    categories: list[dict]
    module_version_changed: bool


@dataclass
class ValidationReport:
    """Outcome of every gate, recorded in the collection artifact."""

    passed: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def ok(self, rule: str, detail: str) -> None:
        self.passed.append(f"{rule}: {detail}")

    def warn(self, rule: str, detail: str) -> None:
        self.warnings.append(f"{rule}: {detail}")

    def as_dict(self) -> dict:
        return {
            "passed": list(self.passed),
            "warnings": list(self.warnings),
            "warning_count": len(self.warnings),
        }


def _parse_count(value: object, what: str) -> int:
    """Rowan returns counts as strings; accept int too, reject anything else."""
    if isinstance(value, bool):
        raise ValidationError(f"V1: {what} was a boolean, expected a count")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text.lstrip("-").isdigit():
            raise ValidationError(f"V1: {what} is not an integer")
        parsed = int(text)
    else:
        raise ValidationError(f"V1: {what} has unexpected type {type(value).__name__}")
    if parsed < 0:
        raise ValidationError(f"V1: {what} is negative ({parsed})")
    return parsed


def check_structural(
    payload: dict,
    *,
    http_status: int,
    content_type: str,
    context: str,
) -> ParsedResponse:
    """Gate V1. Raises rather than returning a partial result.

    `VersionChangedError` is raised separately from `ValidationError` so the
    caller can rediscover tokens and retry exactly once.
    """
    if http_status != 200:
        raise ValidationError(f"V1 [{context}]: HTTP {http_status}, expected 200")

    normalized_type = (content_type or "").split(";")[0].strip().lower()
    if normalized_type not in JSON_CONTENT_TYPES:
        raise ValidationError(
            f"V1 [{context}]: content-type {normalized_type!r} is not JSON"
        )

    if not isinstance(payload, dict):
        raise ValidationError(f"V1 [{context}]: response body is not a JSON object")

    if "exception" in payload:
        # Surface only the platform's own short message, never the whole body.
        exception = payload.get("exception")
        message = (
            exception.get("message") if isinstance(exception, dict) else None
        ) or "unspecified"
        raise ValidationError(f"V1 [{context}]: server exception: {message}")

    version_info = payload.get("versionInfo") or {}
    if version_info.get("hasApiVersionChanged") is True:
        raise VersionChangedError(
            f"[{context}] hasApiVersionChanged is true: the discovered apiVersion is "
            "stale. Rowan returns HTTP 200 with an empty payload in this case; this "
            "is a collector failure, NOT an empty announcement day."
        )

    data = payload.get("data")
    if not isinstance(data, dict) or not data:
        raise ValidationError(
            f"V1 [{context}]: `data` is missing or empty — the hallmark of a broken "
            "call, not of a day without announcements"
        )

    categories = _unwrap_list(data.get("Categories"), "Categories", context)
    if not categories:
        raise ValidationError(
            f"V1 [{context}]: Categories is empty. Rowan returns the full category "
            "registry even on days with no announcements, so an empty registry means "
            "the response is not usable"
        )

    if "TotalCount" not in data:
        raise ValidationError(f"V1 [{context}]: TotalCount is absent")
    total_count = _parse_count(data["TotalCount"], f"[{context}] TotalCount")

    if "Announcements" not in data:
        raise ValidationError(f"V1 [{context}]: Announcements key is absent")
    announcements = _unwrap_list(data.get("Announcements"), "Announcements", context)

    return ParsedResponse(
        total_count=total_count,
        announcements=announcements,
        categories=categories,
        module_version_changed=bool(version_info.get("hasModuleVersionChanged")),
    )


def _unwrap_list(value: object, what: str, context: str) -> list:
    """Accept the API's `{"List": [...]}` wrapper or a bare list (fixtures)."""
    if isinstance(value, dict):
        inner = value.get("List")
        if not isinstance(inner, list):
            raise ValidationError(f"V1 [{context}]: {what}.List is not a list")
        return inner
    if isinstance(value, list):
        return value
    raise ValidationError(f"V1 [{context}]: {what} has unexpected shape")


def check_audience_dataset(
    *,
    audience: str,
    target_date: str,
    total_count: int,
    announcements: list[dict],
    categories: list[dict],
    page_size: int,
    report: ValidationReport,
) -> None:
    """Gates V2, V3, V4, V5, V6, V7, V8 for one audience's assembled dataset."""
    if audience not in config.REQUEST_AUDIENCES:
        raise ValidationError(
            f"V4: refusing to accept audience {audience!r}; Rowan silently returns "
            f"only the 'Both' subset for unrecognised values. "
            f"Allowed: {list(config.REQUEST_AUDIENCES)}"
        )

    # V2 -- completeness against the server's own count.
    ids = [record.get("Submission", {}).get("Id") for record in announcements]
    unique_ids = {str(i) for i in ids if i is not None}
    if len(unique_ids) != total_count:
        raise ValidationError(
            f"V2 [{audience}]: collected {len(unique_ids)} unique announcements but "
            f"TotalCount is {total_count}"
        )
    report.ok("V2", f"{audience}: unique collected == TotalCount == {total_count}")

    # V2 -- independent server-side cross-foot.
    category_sum = sum(
        _parse_count(category.get("Count", 0), f"[{audience}] category Count")
        for category in categories
    )
    if category_sum != total_count:
        raise ValidationError(
            f"V2 [{audience}]: sum of category counts is {category_sum} but "
            f"TotalCount is {total_count}"
        )
    report.ok("V2", f"{audience}: sum(category.Count) == TotalCount == {total_count}")

    # V3 -- a page exactly the size of the window is evidence of truncation.
    if total_count and len(announcements) == page_size and total_count > page_size:
        raise ValidationError(
            f"V3 [{audience}]: collected exactly one page ({page_size}) against "
            f"TotalCount {total_count}; pagination did not complete"
        )
    report.ok("V3", f"{audience}: no page-boundary truncation (page_size={page_size})")

    # V6 -- identity integrity.
    if len(ids) != len(unique_ids):
        duplicates = sorted({str(i) for i in ids if ids.count(i) > 1})
        raise ValidationError(
            f"V6 [{audience}]: duplicate SubmissionIds within one day: {duplicates}"
        )
    for raw_id in ids:
        text = str(raw_id).strip()
        if not text.isdigit() or int(text) <= 0:
            raise ValidationError(
                f"V6 [{audience}]: SubmissionId {raw_id!r} is not a positive integer"
            )
    report.ok("V6", f"{audience}: {len(unique_ids)} SubmissionIds unique and positive")

    known_category_ids = {category.get("Id") for category in categories}

    for record in announcements:
        submission = record.get("Submission") or {}
        submission_id = submission.get("Id")

        # V4 -- audience echo.
        source_audience = submission.get("Audience")
        if source_audience not in (audience, "Both"):
            raise ValidationError(
                f"V4 [{audience}]: SubmissionId {submission_id} has source audience "
                f"{source_audience!r}, which cannot appear in this view"
            )

        # V5 -- date echo. Rowan silently substitutes today for a bad date, so
        # every record must actually carry the date we asked for.
        dates = _unwrap_list(
            record.get("DistributionDates"), "DistributionDates", audience
        )
        if target_date not in dates:
            raise ValidationError(
                f"V5 [{audience}]: SubmissionId {submission_id} does not list "
                f"{target_date} in its distribution dates"
            )

        # V7 -- required fields.
        if not str(submission.get("Title") or "").strip():
            raise ValidationError(
                f"V7 [{audience}]: SubmissionId {submission_id} has an empty title"
            )
        if not str(record.get("FullBody") or "").strip():
            raise ValidationError(
                f"V7 [{audience}]: SubmissionId {submission_id} has an empty FullBody"
            )
        if not dates:
            raise ValidationError(
                f"V7 [{audience}]: SubmissionId {submission_id} has no distribution dates"
            )
        status = submission.get("SubmittedStatus")
        if status != "Approved":
            raise ValidationError(
                f"V7 [{audience}]: SubmissionId {submission_id} has status {status!r}, "
                "expected 'Approved'"
            )
        category_id = submission.get("Category")
        if category_id not in known_category_ids:
            raise ValidationError(
                f"V7 [{audience}]: SubmissionId {submission_id} references category "
                f"{category_id!r}, which the backend did not return in this response"
            )

        # V8 -- soft checks. These record data quality, they do not fail the run.
        if not str(submission.get("ContactRowanEmail") or "").strip():
            report.warn(
                "V8", f"{audience}: SubmissionId {submission_id} has no contact email"
            )
        if submission.get("Event"):
            if not str(submission.get("EventName") or "").strip():
                report.warn(
                    "V8",
                    f"{audience}: SubmissionId {submission_id} is an event with no "
                    "event name",
                )
            if submission.get("EventDate") in config.DATE_SENTINELS:
                report.warn(
                    "V8",
                    f"{audience}: SubmissionId {submission_id} is an event with no "
                    "event date",
                )

    report.ok(
        "V4", f"{audience}: every record's source audience is '{audience}' or 'Both'"
    )
    report.ok("V5", f"{audience}: every record lists {target_date}")
    report.ok("V7", f"{audience}: required fields present on all {len(announcements)} records")


def check_audience_parity(
    *,
    employee_ids: set[str],
    student_ids: set[str],
    source_audience_by_id: dict[str, str],
    report: ValidationReport,
) -> dict[str, int]:
    """Gate V9. The `Audience` field and set membership must agree exactly.

    Phase 0 measured 0 disagreements across 5,985 announcements, so any
    disagreement here is a genuine anomaly rather than expected noise.
    """
    both_by_field = {
        i for i, a in source_audience_by_id.items() if a == "Both"
    }
    employees_only_by_field = {
        i for i, a in source_audience_by_id.items() if a == "Employees"
    }
    students_only_by_field = {
        i for i, a in source_audience_by_id.items() if a == "Students"
    }

    intersection = employee_ids & student_ids
    if both_by_field != intersection:
        only_field = sorted(both_by_field - intersection)
        only_sets = sorted(intersection - both_by_field)
        raise ValidationError(
            "V9: records whose Audience is 'Both' do not match the intersection of "
            f"the two query results. Marked Both but not in both views: {only_field}; "
            f"in both views but not marked Both: {only_sets}"
        )

    stray = employees_only_by_field & student_ids
    if stray:
        raise ValidationError(
            f"V9: Employee-only records appeared in the Student view: {sorted(stray)}"
        )
    stray = students_only_by_field & employee_ids
    if stray:
        raise ValidationError(
            f"V9: Student-only records appeared in the Employee view: {sorted(stray)}"
        )

    missing = employees_only_by_field - employee_ids
    if missing:
        raise ValidationError(
            f"V9: records marked 'Employees' absent from the Employee view: {sorted(missing)}"
        )
    missing = students_only_by_field - student_ids
    if missing:
        raise ValidationError(
            f"V9: records marked 'Students' absent from the Student view: {sorted(missing)}"
        )

    report.ok(
        "V9",
        f"audience field and set membership agree: {len(both_by_field)} Everyone, "
        f"{len(employees_only_by_field)} Employee-only, "
        f"{len(students_only_by_field)} Student-only",
    )
    return {
        "everyone": len(both_by_field),
        "employee_only": len(employees_only_by_field),
        "student_only": len(students_only_by_field),
    }
