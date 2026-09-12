"""Collection orchestration: fetch both audiences, validate, normalize, write."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from . import config, normalize, validate
from .atomic import atomic_write_json
from .client import AnnouncerClient, build_http_client
from .discovery import RuntimeVersions, discover
from .errors import UsageError, ValidationError, VersionChangedError
from .tls import build_ssl_context

SCHEMA_VERSION = 1


def today_in_rowan_local() -> str:
    """Rowan's calendar date. DistributionDates are bare local dates, so using
    UTC here would select the wrong day for several hours each night."""
    return datetime.now(ZoneInfo(config.ROWAN_TIMEZONE)).strftime("%Y-%m-%d")


def parse_target_date(value: str | None) -> str:
    """Validate locally before any request.

    Rowan silently substitutes its own current date for an unparseable value
    (Phase 0 §3.1), so a typo would otherwise produce a confidently wrong
    collection rather than an error.
    """
    if value is None:
        return today_in_rowan_local()
    text = value.strip()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%d")
    except ValueError as exc:
        raise UsageError(
            f"--date {value!r} is not a valid YYYY-MM-DD date. Rowan silently falls "
            "back to its current date for malformed input, so this is rejected here."
        ) from exc
    if parsed.strftime("%Y-%m-%d") != text:
        raise UsageError(
            f"--date {value!r} must be zero-padded YYYY-MM-DD (got a non-canonical form)"
        )
    return text


def _prior_artifact(target_date: str) -> tuple[str, dict] | None:
    """Most recent previously written collection, excluding the target date."""
    directory = config.collections_dir()
    if not directory.is_dir():
        return None
    candidates = sorted(
        p for p in directory.glob("*.json") if p.stem != target_date
    )
    for path in reversed(candidates):
        try:
            return path.stem, json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _fetch_both(
    client: AnnouncerClient, target_date: str, page_size: int
) -> dict[str, object]:
    return {
        audience: client.fetch_audience(
            audience=audience, target_date=target_date, page_size=page_size
        )
        for audience in config.REQUEST_AUDIENCES
    }


def _discover_fetch_retry(
    http, target_date: str, page_size: int
) -> tuple[RuntimeVersions, dict, bool]:
    """Discover tokens, fetch both audiences, rediscover and retry once if the
    apiVersion turns out to be stale.

    A second `VersionChangedError` is allowed to propagate (exit code 7). It must
    never be softened into "no announcements today".
    """
    versions = discover(http)
    try:
        return versions, _fetch_both(
            AnnouncerClient(http, versions), target_date, page_size
        ), False
    except VersionChangedError:
        # Rowan republished between discovery and the call.
        versions = discover(http)
        results = _fetch_both(AnnouncerClient(http, versions), target_date, page_size)
        return versions, results, True


def run_collection(
    *,
    target_date: str,
    page_size: int = config.PAGE_SIZE,
    http=None,
) -> dict:
    """Perform one full collection. Raises on any fatal validation failure.

    `http` exists so the test suite can drive the real pipeline through a mock
    transport instead of the live Rowan service. Production passes nothing.
    """
    started = time.monotonic()
    report = validate.ValidationReport()

    if http is None:
        with build_http_client(build_ssl_context()) as client:
            versions, results, rediscovered = _discover_fetch_retry(
                client, target_date, page_size
            )
        report.ok(
            "V12",
            "TLS verified against certifi roots + pinned InCommon intermediate",
        )
    else:
        versions, results, rediscovered = _discover_fetch_retry(
            http, target_date, page_size
        )
        report.ok("V12", "transport injected by caller; live TLS not exercised")

    report.ok("V1", "structural gate passed on every response")
    if rediscovered:
        report.warn(
            "V13",
            "apiVersion was stale on first attempt; rediscovered and retried once "
            "successfully",
        )

    employee = results["Employees"]
    student = results["Students"]

    for result in (employee, student):
        validate.check_audience_dataset(
            audience=result.audience,
            target_date=target_date,
            total_count=result.total_count,
            announcements=result.announcements,
            categories=result.categories,
            page_size=page_size,
            report=report,
        )
        if result.module_version_changed:
            report.warn(
                "V13",
                f"{result.audience}: hasModuleVersionChanged was true (tolerated by "
                "Rowan; data still returned)",
            )

    by_id: dict[str, dict] = {}
    membership: dict[str, list[str]] = {}
    source_audience_by_id: dict[str, str] = {}
    for result in (employee, student):
        for record in result.announcements:
            submission_id = str(record["Submission"]["Id"])
            by_id.setdefault(submission_id, record)
            membership.setdefault(submission_id, []).append(result.audience)
            source_audience_by_id[submission_id] = record["Submission"]["Audience"]

    employee_ids = {str(r["Submission"]["Id"]) for r in employee.announcements}
    student_ids = {str(r["Submission"]["Id"]) for r in student.announcements}
    audience_split = validate.check_audience_parity(
        employee_ids=employee_ids,
        student_ids=student_ids,
        source_audience_by_id=source_audience_by_id,
        report=report,
    )

    announcements = [
        normalize.normalize_record(
            record,
            target_date=target_date,
            source_query_membership=membership[submission_id],
        )
        for submission_id, record in sorted(by_id.items(), key=lambda kv: int(kv[0]))
    ]

    # The category registry is backend-driven and identical across audiences
    # (Phase 0 §8); merge by id so a divergence cannot silently drop entries.
    merged: dict[object, dict] = {}
    for result in (employee, student):
        for entry in normalize.normalize_category_registry(result.categories):
            merged.setdefault(entry["id"], entry)
    category_registry = sorted(
        merged.values(), key=lambda e: (e["id"] is None, e["id"])
    )
    report.ok("V7", f"category registry captured: {len(category_registry)} categories")

    category_counts = {
        result.audience: {
            str(c.get("Id")): int(str(c.get("Count", 0)).strip() or 0)
            for c in result.categories
        }
        for result in (employee, student)
    }

    prior = _prior_artifact(target_date)
    prior_date, prior_doc = prior if prior else (None, None)

    fingerprints = versions.fingerprints()
    tokens_changed = None
    if prior_doc:
        previous = (prior_doc.get("runtime") or {}).get("token_fingerprints")
        if isinstance(previous, dict):
            tokens_changed = previous != fingerprints
            if tokens_changed:
                report.warn(
                    "V13",
                    f"runtime version tokens differ from the {prior_date} run "
                    "(Rowan republished the module since then)",
                )

    new_categories = None
    if prior_doc:
        prior_ids = {
            entry.get("id") for entry in (prior_doc.get("category_registry") or [])
        }
        if prior_ids:
            fresh = [
                entry
                for entry in category_registry
                if entry["id"] not in prior_ids
            ]
            new_categories = [
                {"id": entry["id"], "title": entry["title"]} for entry in fresh
            ]
            if new_categories:
                report.warn(
                    "V7",
                    "new categories observed since the "
                    f"{prior_date} run: {new_categories}",
                )

    statuses = [record["status"] for record in announcements]
    now = datetime.now(timezone.utc)

    artifact = {
        "schema_version": SCHEMA_VERSION,
        "target_date": target_date,
        "generated_at_utc": now.isoformat(timespec="seconds"),
        "generated_at_rowan_local": now.astimezone(
            ZoneInfo(config.ROWAN_TIMEZONE)
        ).isoformat(timespec="seconds"),
        "collector": {
            "name": "dailymail",
            "method": "ActionGetHomeData (OutSystems screen service), single-day mode",
            "endpoint": f"{config.BASE_URL}/{config.GET_HOME_DATA_PATH}",
            "page_size": page_size,
            "detail_pages_fetched": 0,
            "browser_used": False,
        },
        "runtime": {
            # Fingerprints only: the tokens themselves are never written or logged.
            "token_fingerprints": fingerprints,
            "token_rediscovery_performed": rediscovered,
            "tokens_changed_since_prior_run": tokens_changed,
            "prior_run_compared": prior_date,
            "pages_fetched": {
                result.audience: result.pages_fetched for result in (employee, student)
            },
            "duration_seconds": round(time.monotonic() - started, 3),
        },
        "source_counts": {
            "employees_total_count": employee.total_count,
            "students_total_count": student.total_count,
        },
        "counts": {
            "unique": len(announcements),
            "new": statuses.count("New"),
            "standing": statuses.count("Standing"),
            "employee_only": audience_split["employee_only"],
            "student_only": audience_split["student_only"],
            "everyone": audience_split["everyone"],
            "categories": len(category_registry),
        },
        "category_registry": category_registry,
        "category_counts_by_audience": category_counts,
        "new_categories_since_prior_run": new_categories,
        "announcements": announcements,
        "validation": report.as_dict(),
    }

    # Runtime enforcement of the drop policy, not just a test-time assertion.
    normalize.assert_no_forbidden_fields(artifact)
    return artifact


def write_artifact(artifact: dict) -> Path:
    """Atomically write the single normalized artifact. No raw copy is kept.

    The mechanism moved to `atomic.atomic_write_json` unchanged, so the status
    snapshot and the collection artifact share one proven implementation rather
    than two that drift.
    """
    return atomic_write_json(
        config.collection_path(artifact["target_date"]),
        artifact,
        # Preserved from the original: keys stay in the order the artifact was
        # assembled, which is what makes a day-to-day diff readable.
        sort_keys=False,
    )
