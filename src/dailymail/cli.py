"""Command line interface.

    uv run dailymail collect
    uv run dailymail collect --date 2026-08-20
    uv run dailymail inspect --date 2026-08-20

Success is one concise line. Announcement bodies are never printed.
Any failure exits non-zero with a distinct code (see `errors.py`).
"""

from __future__ import annotations

import argparse
import json
import sys

from . import collect, config
from .errors import DailyMailError, UsageError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dailymail",
        description="Deterministic collector for Rowan Announcer announcements.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect_parser = subparsers.add_parser(
        "collect", help="Collect and validate one day of announcements."
    )
    collect_parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        default=None,
        help="Target date. Defaults to today in America/New_York.",
    )
    collect_parser.add_argument(
        "--page-size",
        type=int,
        default=config.PAGE_SIZE,
        help=f"Pagination window (default {config.PAGE_SIZE}).",
    )

    inspect_parser = subparsers.add_parser(
        "inspect", help="Summarize a previously written collection artifact."
    )
    inspect_parser.add_argument(
        "--date", metavar="YYYY-MM-DD", required=True, help="Date to inspect."
    )
    return parser


def _cmd_collect(args: argparse.Namespace) -> int:
    target_date = collect.parse_target_date(args.date)
    if args.page_size <= 0:
        raise UsageError("--page-size must be a positive integer")

    artifact = collect.run_collection(
        target_date=target_date, page_size=args.page_size
    )
    path = collect.write_artifact(artifact)

    counts = artifact["counts"]
    source = artifact["source_counts"]
    print(
        f"COLLECT OK date={target_date} "
        f"employees={source['employees_total_count']} "
        f"students={source['students_total_count']} "
        f"unique={counts['unique']} "
        f"new={counts['new']} standing={counts['standing']} "
        f"categories={counts['categories']}"
    )

    runtime = artifact["runtime"]
    print(
        f"  tokens={runtime['token_fingerprints']['api_version']} "
        f"rediscovered={runtime['token_rediscovery_performed']} "
        f"changed_since_prior={runtime['tokens_changed_since_prior_run']} "
        f"pages={runtime['pages_fetched']} "
        f"{runtime['duration_seconds']}s"
    )
    print(
        f"  audience: everyone={counts['everyone']} "
        f"employee_only={counts['employee_only']} "
        f"student_only={counts['student_only']}"
    )
    print(f"  wrote {path}")

    warnings = artifact["validation"]["warnings"]
    if warnings:
        print(f"  {len(warnings)} validation warning(s):", file=sys.stderr)
        for warning in warnings:
            print(f"    WARN {warning}", file=sys.stderr)
    if artifact.get("new_categories_since_prior_run"):
        for entry in artifact["new_categories_since_prior_run"]:
            print(f"  NEW CATEGORY id={entry['id']} title={entry['title']!r}")
    return 0


def _cmd_inspect(args: argparse.Namespace) -> int:
    target_date = collect.parse_target_date(args.date)
    path = config.collection_path(target_date)
    if not path.is_file():
        raise UsageError(f"no collection artifact at {path}")
    artifact = json.loads(path.read_text(encoding="utf-8"))

    counts = artifact["counts"]
    source = artifact["source_counts"]
    print(f"date={artifact['target_date']}  generated={artifact['generated_at_utc']}")
    print(
        f"employees={source['employees_total_count']} "
        f"students={source['students_total_count']} unique={counts['unique']} "
        f"new={counts['new']} standing={counts['standing']} "
        f"categories={counts['categories']}"
    )
    print(
        f"everyone={counts['everyone']} employee_only={counts['employee_only']} "
        f"student_only={counts['student_only']}"
    )
    validation = artifact["validation"]
    print(
        f"validation: {len(validation['passed'])} gate result(s), "
        f"{validation['warning_count']} warning(s)"
    )
    for warning in validation["warnings"]:
        print(f"  WARN {warning}")

    print("\nannouncements (no bodies):")
    for record in artifact["announcements"]:
        diagnostics = record["body_diagnostics"]
        event = " EVENT" if record.get("is_event") else ""
        category = (record.get("category") or {}).get("title")
        images = (
            f" img={diagnostics['image_count']}"
            f"/data={diagnostics['data_uri_count']}"
            if diagnostics["image_count"]
            else ""
        )
        print(
            f"  {record['submission_id']:>6} {record['status']:<8} "
            f"{record['audience_label']:<8} {str(category)[:26]:<26} "
            f"dates={len(record['distribution_dates'])} "
            f"body={diagnostics['body_bytes']}B{images}{event}  "
            f"{(record['title'] or '')[:52]}"
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            return _cmd_collect(args)
        if args.command == "inspect":
            return _cmd_inspect(args)
        parser.error(f"unknown command {args.command!r}")
        return 2
    except DailyMailError as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return exc.exit_code
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
