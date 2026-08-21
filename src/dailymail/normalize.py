"""Projection of raw API records onto DailyMail's allowlisted data model.

Two rules govern this module.

1. **Allowlist, not denylist.** Only fields named in `ALLOWLIST` may leave the
   ingestion layer. Anything Rowan adds in future is dropped by default rather
   than silently propagated.

2. **The API over-exposes PII.** Every raw record embeds a `User` object with a
   username, a 9-digit Banner ID, a `Last_Login` and a `Password` key, plus
   `SubmittedByExternalId` on the submission. None of it reaches disk. See
   `FORBIDDEN_KEYS` and `docs/site-reconnaissance.md` §13 item 4.

The published contact/submitter/approver names and addresses *are* retained:
they are part of the announcement as Rowan publishes it and will appear in the
curated email.
"""

from __future__ import annotations

import re
from html import unescape
from html.parser import HTMLParser

from . import config

# Fields projected out of `Submission`, as source_name -> normalized_name.
SUBMISSION_ALLOWLIST: dict[str, str] = {
    "Id": "submission_id",
    "Title": "title",
    "Audience": "source_audience",
    "Category": "category_id",
    "SubmittedStatus": "submitted_status",
    "SubmittedDate": "submitted_date",
    "ApprovedDate": "approved_date",
    "UpdatedDate": "updated_date",
    "UpdatedByName": "updated_by_name",
    "SubmittedByName": "submitted_by_name",
    "SubmittedByDepartment": "submitted_by_department",
    "SubmittedByJobTitle": "submitted_by_job_title",
    "SubmittedByEmail": "submitted_by_email",
    "SubmittedByPhone": "submitted_by_phone",
    "ApprovedByName": "approved_by_name",
    "ApprovedByDepartment": "approved_by_department",
    "ApprovedByJobTitle": "approved_by_job_title",
    "ApprovedByEmail": "approved_by_email",
    "ApprovedByPhone": "approved_by_phone",
    "ContactName": "contact_name",
    "ContactDepartment": "contact_department",
    "ContactJobTitle": "contact_job_title",
    "ContactRowanEmail": "contact_email",
    "ContactPhone": "contact_phone",
    "Event": "is_event",
    "EventName": "event_name",
    "EventDate": "event_date",
    "EventStartTime": "event_start_time",
    "EventEndTime": "event_end_time",
    "EventLocation": "event_location",
    "EventNoEnd": "event_no_end",
    # Retained purely as a watch flag: Phase 0 U3 could not establish whether a
    # special edition surfaces through this endpoint, and it was false for all
    # 5,125 archive records. Cheap to carry, and it makes the answer observable.
    "ExtraEdition": "extra_edition",
}

# Fields from the announcement wrapper (outside `Submission`).
WRAPPER_ALLOWLIST: dict[str, str] = {
    "FullBody": "full_body",
    "ShortBody": "short_body",
}

# Must never appear anywhere in normalized output. Asserted by the test suite.
FORBIDDEN_KEYS: frozenset[str] = frozenset(
    {
        # Wrapper-level PII object.
        "User",
        # Banner IDs and internal user primary keys.
        "External_Id",
        "ExternalId",
        "SubmittedByExternalId",
        "ApproverExternalId",
        "SubmittedById",
        "ApprovedById",
        "UpdatedById",
        # Credential-adjacent.
        "Password",
        "Last_Login",
        "Username",
        # Framework / workflow noise.
        "rolesInfo",
        "versionInfo",
        "QuestionForApprover",
        "ReadyForSubmission",
        "RejectComment",
        "RejectReasonId",
        "IsDeleted",
        "SelectedCategory",
        "ExtraEditionDateSent",
        "OldSubmissionId",
        # Redundant: byte-identical to FullBody (Phase 0 §10).
        "SubmissionBody",
    }
)

CATEGORY_FIELDS: dict[str, str] = {
    "Id": "id",
    "Title": "title",
    "Rank": "rank",
    "Is_Active": "is_active",
    "RestrictSubmitting": "restrict_submitting",
    "Color": "color",
    "ApprovalProcess": "approval_process",
}

_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_BREAK_RE = re.compile(
    r"</\s*(?:p|div|li|ul|ol|tr|table|h[1-6]|blockquote|figure)\s*>|<\s*br\s*/?\s*>",
    re.IGNORECASE,
)
# Table cells separate with a space rather than a newline, so a row reads as a row.
_CELL_BREAK_RE = re.compile(r"</\s*(?:td|th)\s*>", re.IGNORECASE)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_MULTI_NEWLINE_RE = re.compile(r"\n{3,}")
_DATA_URI_RE = re.compile(r"""(?:src|href)\s*=\s*["'](data:[^"']*)["']""", re.IGNORECASE)
_IMG_RE = re.compile(r"<\s*img\b", re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']*)["']""", re.IGNORECASE)


def normalize_text(value: object) -> str | None:
    """Trim outer whitespace; empty becomes null."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def normalize_date(value: object) -> str | None:
    """Map Rowan's `1900-01-01` family of sentinels to null (Phase 0 §7.3)."""
    text = normalize_text(value)
    if text is None or text in config.DATE_SENTINELS:
        return None
    return text


def normalize_time(value: object) -> str | None:
    """Map the `00:00:00` sentinel to null.

    Caveat worth knowing: Rowan uses `00:00:00` for "not set", so a genuine
    midnight event time is indistinguishable from an absent one. Phase 0 saw no
    midnight events; this is recorded as a known limitation rather than guessed at.
    """
    text = normalize_text(value)
    if text is None or text == config.TIME_SENTINEL:
        return None
    return text


class _TextExtractor(HTMLParser):
    """Minimal HTML-to-text pass for inspection and later curation.

    Not a sanitizer. The archival copy is `full_body`, kept verbatim; the
    renderer in a later phase owns sanitisation and Outlook compatibility.
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._suppress = 0

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag in ("script", "style"):
            self._suppress += 1

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style") and self._suppress:
            self._suppress -= 1

    def handle_data(self, data: str) -> None:
        if not self._suppress:
            self.parts.append(data)


def html_to_text(html: str) -> str:
    """Derive readable plain text from the announcement body."""
    if not html:
        return ""
    # Turn block ends and <br> into newlines before dropping tags, so words from
    # adjacent blocks do not run together.
    spaced = _CELL_BREAK_RE.sub(" ", html)
    spaced = _BLOCK_BREAK_RE.sub("\n", spaced)
    parser = _TextExtractor()
    try:
        parser.feed(spaced)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        # Never let a malformed body fail collection; fall back to tag stripping.
        text = unescape(_TAG_RE.sub("", spaced))
    text = text.replace("\xa0", " ")
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.split("\n"))
    return _MULTI_NEWLINE_RE.sub("\n\n", text).strip()


def body_diagnostics(html: str) -> dict:
    """Cheap metrics about the body. Phase 1 measures images; it does not touch them."""
    html = html or ""
    data_uris = _DATA_URI_RE.findall(html)
    return {
        "body_bytes": len(html.encode("utf-8")),
        "image_count": len(_IMG_RE.findall(html)),
        "data_uri_count": len(data_uris),
        "data_uri_bytes": sum(len(uri) for uri in data_uris),
        "link_count": len(_HREF_RE.findall(html)),
    }


def normalize_category(source: object) -> dict | None:
    if not isinstance(source, dict):
        return None
    return {
        normalized: source.get(raw)
        for raw, normalized in CATEGORY_FIELDS.items()
        if raw in source
    }


def normalize_category_registry(categories: list[dict]) -> list[dict]:
    """Capture the backend's full dynamic category registry.

    Categories are backend-driven and must be upserted, never hardcoded; unknown
    IDs are valid. `count` is audience- and date-scoped, so it is recorded per
    audience rather than as a property of the category itself.
    """
    registry = []
    for category in categories:
        if not isinstance(category, dict):
            continue
        entry = {
            "id": category.get("Id"),
            "title": normalize_text(category.get("Title")),
            "rank": category.get("Rank"),
            "color": normalize_text(category.get("Color")),
        }
        if "Is_Active" in category:
            entry["is_active"] = category.get("Is_Active")
        registry.append(entry)
    registry.sort(key=lambda entry: (entry["id"] is None, entry["id"]))
    return registry


_TEXT_FIELDS = {
    "title",
    "updated_by_name",
    "submitted_by_name",
    "submitted_by_department",
    "submitted_by_job_title",
    "submitted_by_email",
    "submitted_by_phone",
    "approved_by_name",
    "approved_by_department",
    "approved_by_job_title",
    "approved_by_email",
    "approved_by_phone",
    "contact_name",
    "contact_department",
    "contact_job_title",
    "contact_email",
    "contact_phone",
    "event_name",
    "event_location",
    "submitted_status",
    "short_body",
}
_DATE_FIELDS = {"submitted_date", "approved_date", "updated_date", "event_date"}
_TIME_FIELDS = {"event_start_time", "event_end_time"}


def normalize_record(
    record: dict,
    *,
    target_date: str,
    source_query_membership: list[str],
) -> dict:
    """Project one raw announcement onto the allowlisted model."""
    submission = record.get("Submission") or {}
    out: dict = {}

    for raw, normalized in SUBMISSION_ALLOWLIST.items():
        if raw not in submission:
            continue
        value = submission[raw]
        if normalized in _DATE_FIELDS:
            out[normalized] = normalize_date(value)
        elif normalized in _TIME_FIELDS:
            out[normalized] = normalize_time(value)
        elif normalized in _TEXT_FIELDS:
            out[normalized] = normalize_text(value)
        else:
            out[normalized] = value

    for raw, normalized in WRAPPER_ALLOWLIST.items():
        if raw not in record:
            continue
        if normalized == "full_body":
            # Verbatim. Phase 1 does not rewrite archival HTML.
            out[normalized] = record[raw]
        else:
            out[normalized] = normalize_text(record[raw])

    # SubmissionId is exposed as a string; store the integer identity too.
    raw_id = out.get("submission_id")
    out["submission_id"] = str(raw_id) if raw_id is not None else None
    if out["submission_id"] is not None and out["submission_id"].isdigit():
        out["submission_id_int"] = int(out["submission_id"])

    dates = record.get("DistributionDates")
    if isinstance(dates, dict):
        dates = dates.get("List") or []
    distribution_dates = sorted({str(d) for d in (dates or [])})
    out["distribution_dates"] = distribution_dates
    out["first_distribution_date"] = distribution_dates[0] if distribution_dates else None

    # New vs Standing, straight from the source data (Phase 0 §6.1, 13/13).
    # Deliberately NOT "first seen by this program" -- SQLite will track that
    # separately in a later phase.
    out["status"] = (
        "New" if out["first_distribution_date"] == target_date else "Standing"
    )

    source_audience = out.get("source_audience")
    out["audience_label"] = config.AUDIENCE_LABELS.get(source_audience)
    out["source_query_membership"] = sorted(source_query_membership)

    out["category"] = normalize_category(record.get("Category"))

    body = out.get("full_body") or ""
    out["body_text"] = html_to_text(body)
    out["body_diagnostics"] = body_diagnostics(body)

    return out


def assert_no_forbidden_fields(obj: object, path: str = "$") -> None:
    """Recursive audit that no dropped field leaked into the output.

    Run before writing the artifact, so the guarantee is enforced at runtime and
    not merely asserted in tests.
    """
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in FORBIDDEN_KEYS:
                raise AssertionError(
                    f"forbidden field {key!r} present in normalized output at {path}"
                )
            assert_no_forbidden_fields(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for index, value in enumerate(obj):
            assert_no_forbidden_fields(value, f"{path}[{index}]")
