"""Command line interface.

    uv run dailymail run-daily                      # the production pipeline
    uv run dailymail run-daily --date 2026-08-20
    uv run dailymail run-daily --force-resend
    uv run dailymail collect                        # collect only
    uv run dailymail inspect --date 2026-08-20      # inspect a collection
    uv run dailymail render --date 2026-08-20       # render without sending
    uv run dailymail send --date 2026-08-20         # send an already-stored day
    uv run dailymail status                         # recent runs and deliveries
    uv run dailymail db-status                      # database statistics
    uv run dailymail db-init                        # create/import history
    uv run dailymail install-timer                  # systemd service + timer
    uv run dailymail parking-status                 # parking cache summary
    uv run dailymail parking-lookup "Lot O-1"       # verify one parking record
    uv run dailymail parking-refresh                # re-check authoritative sources
    uv run dailymail parking-set --id ... --lat ... # manual correction

Success is one concise line. Announcement bodies are never printed, and no
credential is ever logged. Any failure exits non-zero with a distinct code
(see `errors.py`).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys

from . import collect, config
from .errors import DailyMailError, UsageError


def _setup_logging(verbose: bool = False) -> None:
    """Concise structured lines for the systemd journal."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
        force=True,
    )


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

    daily = subparsers.add_parser(
        "run-daily", help="Full production pipeline: collect, persist, curate, send."
    )
    daily.add_argument("--date", metavar="YYYY-MM-DD", default=None,
                       help="Digest date. Defaults to today in the configured timezone.")
    daily.add_argument("--force-resend", action="store_true",
                       help="Send even if this date was already delivered.")
    daily.add_argument("--dry-run", action="store_true",
                       help="Render and validate, but do not send.")
    daily.add_argument("--trigger", default="manual",
                       help="Recorded in the runs table (e.g. 'timer').")
    daily.add_argument("-v", "--verbose", action="store_true")

    render_parser = subparsers.add_parser(
        "render", help="Render a stored date to HTML and text without sending."
    )
    render_parser.add_argument("--date", metavar="YYYY-MM-DD", required=True)
    render_parser.add_argument("--out", metavar="DIR", default=None,
                               help="Directory for the .html/.txt preview "
                                    "(default: the diagnostics directory).")
    render_parser.add_argument("--no-parking", action="store_true",
                               help="Skip parking enrichment in the preview.")
    render_parser.add_argument("--no-calendar", action="store_true",
                               help="Skip calendar enrichment in the preview.")
    render_parser.add_argument("--inline-images", action="store_true",
                               help="Inline CID images as data URIs so the preview "
                                    "can be opened directly in a browser.")

    send_parser = subparsers.add_parser(
        "send", help="Send the digest for an already-collected date."
    )
    send_parser.add_argument("--date", metavar="YYYY-MM-DD", required=True)
    send_parser.add_argument("--force-resend", action="store_true")
    send_parser.add_argument("-v", "--verbose", action="store_true")

    status_parser = subparsers.add_parser("status", help="Recent runs and deliveries.")
    status_parser.add_argument("--limit", type=int, default=10)
    status_parser.add_argument(
        "--json", action="store_true",
        help="Emit the versioned, read-only ControlPanel status document.",
    )

    health_parser = subparsers.add_parser(
        "health", help="Read-only machine-readable health for ControlPanel."
    )
    health_parser.add_argument(
        "--json", action="store_true", required=True,
        help="Emit controlpanel.status.v1 JSON.",
    )

    subparsers.add_parser("db-status", help="Database statistics.")

    init_parser = subparsers.add_parser(
        "db-init", help="Create the database and import existing collection artifacts."
    )
    init_parser.add_argument("--no-import", action="store_true",
                             help="Create the schema without importing artifacts.")

    parking_status = subparsers.add_parser(
        "parking-status", help="Parking reference cache: counts, sources, misses."
    )
    parking_status.add_argument("--campus", default=None,
                                help="Restrict to one campus (glassboro, stratford, "
                                     "camden, sewell).")
    parking_status.add_argument("--list", action="store_true",
                                help="List every cached parking location.")

    parking_lookup = subparsers.add_parser(
        "parking-lookup",
        help="Look up one parking facility by name, alias or canonical id.",
    )
    parking_lookup.add_argument("name", help='e.g. "Lot O-1" or glassboro:lot:o-1')
    parking_lookup.add_argument("--campus", default=None)

    parking_refresh_parser = subparsers.add_parser(
        "parking-refresh",
        help="Re-check the authoritative parking sources and cache what changed.",
    )
    parking_refresh_parser.add_argument("--campus", default=None)
    parking_refresh_parser.add_argument("--source", default=None,
                                        help="Refresh a single source id.")
    parking_refresh_parser.add_argument(
        "--no-descriptions", action="store_true",
        help="Skip generating plain-English descriptions (no Claude call).")
    parking_refresh_parser.add_argument("-v", "--verbose", action="store_true")

    parking_set = subparsers.add_parser(
        "parking-set",
        help="Manually correct a parking record. Overridden fields are pinned and "
             "never overwritten by an automated refresh.",
    )
    parking_set.add_argument("canonical_id", help="e.g. glassboro:lot:o-1")
    parking_set.add_argument("--description", default=None)
    parking_set.add_argument("--latitude", type=float, default=None)
    parking_set.add_argument("--longitude", type=float, default=None)
    parking_set.add_argument("--permit-class", default=None,
                             help="Employee, Student, Patient, Visitor, Resident, "
                                  "Commuter, Mixed or Unknown.")
    parking_set.add_argument("--location-type", default=None,
                             help="surface_lot, garage, patient_lot, visitor_lot "
                                  "or other.")
    parking_set.add_argument("--canonical-name", default=None)
    parking_set.add_argument("--confidence", default=None,
                             choices=["low", "medium", "high"])
    parking_set.add_argument("--inactive", action="store_true",
                             help="Mark the facility as no longer in service.")
    parking_set.add_argument("--active", action="store_true",
                             help="Mark the facility as in service again.")
    parking_set.add_argument("--alias", action="append", default=[],
                             help="Add a manual alias (repeatable).")
    parking_set.add_argument("--clear-overrides", action="store_true",
                             help="Release all pinned fields back to automated "
                                  "refresh.")

    families_parser = subparsers.add_parser(
        "families",
        help="Logical repeat families: which SubmissionIds are one announcement.",
    )
    families_parser.add_argument(
        "--submission", type=int,
        help="Show the family one SubmissionId belongs to, and why it joined.",
    )
    families_parser.add_argument(
        "--key", help="Show the family with this exact family_key.",
    )
    families_parser.add_argument(
        "--recheck", metavar="YYYY-MM-DD",
        help="Re-resolve one date's repeats read-only and print what would change.",
    )
    families_parser.add_argument(
        "--apply", action="store_true",
        help="With --recheck, persist the corrections and record why. Never "
             "rewrites Rowan's own status and never resends a digest.",
    )

    sessions_parser = subparsers.add_parser(
        "sessions",
        help="Calendar sessions detected for a date, and what each one carries.",
    )
    sessions_parser.add_argument(
        "--date", required=True, metavar="YYYY-MM-DD",
        help="The digest date to inspect.",
    )
    sessions_parser.add_argument(
        "--submission", type=int, help="Restrict to one announcement.",
    )
    sessions_parser.add_argument(
        "--ics", action="store_true",
        help="Print each session's complete .ics payload.",
    )

    timer_parser = subparsers.add_parser(
        "install-timer", help="Install and enable the systemd user service and timer."
    )
    timer_parser.add_argument("--no-enable", action="store_true",
                              help="Write the units without enabling the timer.")
    timer_parser.add_argument("--print-only", action="store_true",
                              help="Show the units without writing them.")

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


# --- production pipeline -----------------------------------------------------


def _open_db():
    from . import db

    connection = db.connect()
    db.initialize(connection)
    return connection


def _cmd_run_daily(args: argparse.Namespace) -> int:
    from . import daily, settings as settings_module

    _setup_logging(getattr(args, "verbose", False))
    settings_module.ensure_config()
    settings = settings_module.load()
    target = collect.parse_target_date(args.date) if args.date else None

    result = daily.run_daily(
        target_date=target,
        settings=settings,
        force_resend=args.force_resend,
        dry_run=args.dry_run,
        trigger=args.trigger,
    )

    counts = result.counts or {}
    print(
        f"RUN {result.status.upper()} date={result.target_date} "
        f"employees={counts.get('employee_view', 0)} "
        f"students={counts.get('student_view', 0)} "
        f"unique={counts.get('unique', 0)} new={counts.get('new', 0)} "
        f"standing={counts.get('standing', 0)} updated={counts.get('changed', 0)}"
    )
    print(
        f"  audience: everyone={counts.get('everyone', 0)} "
        f"employee_only={counts.get('employee_only', 0)} "
        f"student_only={counts.get('student_only', 0)}"
    )
    print(
        f"  curation: {result.curation_method} "
        f"model={result.curation_model or '-'} {result.curation_seconds or 0}s"
        + (f" ({result.curation_error})" if result.curation_error else "")
    )
    images = result.image_stats or {}
    if images.get("seen"):
        print(
            f"  images: seen={images['seen']} embedded={images['embedded']} "
            f"omitted={images['omitted']} "
            f"{images['original_bytes'] // 1024}KB -> {images['final_bytes'] // 1024}KB"
        )
    if result.parking and result.parking.get("mentions_detected"):
        park = result.parking
        print(
            f"  parking: mentions={park['mentions_detected']} "
            f"hits={park['cache_hits']} misses={park['cache_misses']} "
            f"refreshes={park['source_refreshes']} agent={park['resolver_calls']} "
            f"new={park['new_resolutions']} unresolved={park['unresolved']} "
            f"{park['duration_seconds']}s"
        )
    if result.calendar and result.calendar.get("candidates"):
        cal = result.calendar
        print(
            f"  calendar: candidates={cal['candidates']} offered={cal['offered']} "
            f"withheld={cal['withheld_relevance'] + cal['withheld_other']} "
            f"travel={cal['travel_enriched']} "
            f"venue(hit={cal['venue_hits']},miss={cal['venue_misses']},"
            f"unresolved={cal['venue_unresolved']}) "
            f"routes={cal['route_lookups']} {cal['duration_seconds']}s"
        )
    if result.repeat_overrides:
        print(
            f"  repeats: {result.repeat_overrides} announcement(s) shown as "
            f"Standing (already delivered under a previous SubmissionId)"
        )
    print(
        f"  email: {result.email_status}"
        + (f" size={result.message_bytes}B" if result.message_bytes else "")
        + (f" id={result.message_id}" if result.message_id else "")
    )
    if result.maintenance:
        maint = result.maintenance
        print(
            f"  maintenance: backup={'ok' if maint.get('backup') else 'FAILED'} "
            f"db={maint.get('database_bytes', 0) // 1024}KB "
            f"pruned(backups={maint.get('backups_pruned', 0)},"
            f"diag={maint.get('diagnostics_pruned', 0)},"
            f"artifacts={maint.get('artifacts_pruned', 0)})"
        )
    if result.status != "success":
        print(f"  error: {result.error}", file=sys.stderr)
        return 1
    return 0


def _cmd_render(args: argparse.Namespace) -> int:
    import base64
    from pathlib import Path

    from . import (
        curate,
        db,
        maintenance,
        parking_enrich,
        render,
        settings as settings_module,
    )

    settings_module.ensure_config()
    settings = settings_module.load()
    target = collect.parse_target_date(args.date)

    connection = _open_db()
    try:
        rows = db.digest_rows(connection, target)
        if not rows:
            raise UsageError(
                f"no stored announcements for {target}. "
                f"Run: uv run dailymail run-daily --date {target}"
            )
        counts = db.counts_for_date(connection, target)
        stored = list(
            connection.execute(
                "SELECT submission_id, section, final_rank, method FROM "
                "curation_results WHERE target_date = ?",
                (target,),
            )
        )
        if stored:
            ordering = {
                str(row["submission_id"]): {"model_rank": row["final_rank"]}
                for row in stored
            }
            method = stored[0]["method"]
        else:
            entries = curate.fallback_rank(rows, target, settings)
            ordering = {entry["submission_id"]: entry for entry in entries}
            method = "fallback"

        # A preview resolves parking from the cache only: no source fetch, no
        # resolver call, so rendering a historical date is fast and repeatable.
        parking_callouts: dict = {}
        parking_metrics = None
        if not args.no_parking:
            parking_callouts, parking_metrics = parking_enrich.enrich_digest(
                connection, rows, target_date=target, settings=settings,
                allow_refresh=False, allow_resolver=False,
            )

        # A preview rebuilds the calendar actions from stored state and the
        # venue cache only: no route lookup, no model call, so a historical
        # date renders fast and identically every time.
        calendar_actions: dict = {}
        calendar_metrics = None
        if not args.no_calendar:
            from . import calendar_enrich

            candidates, diagnostics = calendar_enrich.detect_candidates(
                rows, target_date=target, settings=settings
            )
            stored_judgements = _stored_calendar_judgements(connection, target)
            calendar_actions, calendar_metrics = calendar_enrich.enrich_digest(
                connection, rows, target_date=target, settings=settings,
                candidates=candidates, diagnostics=diagnostics,
                curation_entries=stored_judgements,
                curation_method=method,
                allow_routing=False,
            )

        digest = render.render_digest(
            rows, target_date=target, counts=counts, ordering=ordering,
            curation_method=method, settings=settings, parking=parking_callouts,
            calendar=calendar_actions,
        )
    finally:
        connection.close()

    html = digest.html
    if args.inline_images:
        for image in digest.images:
            html = html.replace(
                f"cid:{image.cid}",
                "data:%s;base64,%s"
                % (image.content_type, base64.b64encode(image.data).decode()),
            )

    if args.out:
        directory = Path(args.out)
        directory.mkdir(parents=True, exist_ok=True)
        html_path = directory / f"{target}.html"
        text_path = directory / f"{target}.txt"
        html_path.write_text(html, encoding="utf-8")
        text_path.write_text(digest.text, encoding="utf-8")
    else:
        html_path = maintenance.save_diagnostic(f"{target}-preview.html", html)
        text_path = maintenance.save_diagnostic(f"{target}-preview.txt", digest.text)

    print(
        f"RENDER OK date={target} announcements={len(digest.submission_ids)} "
        f"html={len(html) // 1024}KB text={len(digest.text) // 1024}KB "
        f"images={digest.image_stats['embedded']}/{digest.image_stats['seen']} "
        f"ordering={method}"
    )
    print(f"  subject: {digest.subject}")
    if calendar_metrics is not None and calendar_metrics.candidates_detected:
        print(
            f"  calendar: {calendar_metrics.candidates_detected} candidate(s), "
            f"{calendar_metrics.actions_offered} action(s), "
            f"{calendar_metrics.withheld_by_relevance} withheld by relevance, "
            f"{calendar_metrics.travel_enriched} with travel, "
            f"{calendar_metrics.venue_cache_hits} venue hit(s), "
            f"{calendar_metrics.duration_seconds:.3f}s"
        )
        if calendar_metrics.multi_session_announcements:
            print(
                f"  sessions: {calendar_metrics.session_actions_offered} calendar "
                f"control(s) across {calendar_metrics.actions_offered} "
                f"announcement(s); "
                f"{calendar_metrics.multi_session_announcements} offer more than one"
            )
        for submission_id, action in sorted(calendar_actions.items()):
            for session in action.sessions:
                suffix = (
                    f" [session {session.index}/{session.count}]"
                    if session.count > 1
                    else ""
                )
                print(
                    f"    {submission_id}: {session.title}{suffix} — "
                    f"{session.when_line}"
                    + (f" — {session.travel_line}" if session.travel_line else "")
                    + (f" — {session.ics_filename}" if session.ics_filename else "")
                )
    if digest.duplicate_titles_suppressed:
        print(
            f"  rendering: {digest.duplicate_titles_suppressed} duplicate "
            f"title block(s) suppressed"
        )
    if parking_metrics is not None and parking_metrics.mentions_detected:
        print(
            f"  parking: {parking_metrics.mentions_detected} mention(s), "
            f"{digest.parking_callouts} callout(s), "
            f"{digest.parking_unresolved} fallback(s), "
            f"{parking_metrics.cache_hits} cache hit(s), "
            f"{parking_metrics.resolver_calls} agent call(s), "
            f"{parking_metrics.duration_seconds:.3f}s"
        )
    print(f"  {html_path}")
    print(f"  {text_path}")
    return 0


def _stored_calendar_judgements(connection, target_date: str) -> list[dict]:
    """Replay a stored day's calendar relevance decisions for a preview.

    A preview must never call the model, and re-scoring deterministically would
    show something different from what was actually sent. So the recorded
    judgement is reused verbatim when there is one.
    """
    from . import db

    entries: list[dict] = []
    try:
        rows = db.calendar_recommendations(connection, target_date)
    except Exception:  # noqa: BLE001 - an older date simply has no record
        return entries
    for row in rows:
        if not row["is_event_candidate"] or row["relevance_score"] is None:
            continue
        if row["relevance_method"] != "claude":
            continue
        entries.append(
            {
                "submission_id": str(row["submission_id"]),
                "calendar": {
                    "offer": bool(row["offer_calendar"])
                    or row["withheld_reason"] == "max_actions_reached",
                    "confidence": float(row["relevance_score"]),
                    "reason": row["relevance_reason"],
                    "attendance_mode": row["attendance_mode"],
                    "suggested_title": row["calendar_title"],
                },
            }
        )
    return entries


def _cmd_send(args: argparse.Namespace) -> int:
    """Send a date that is already collected and stored."""
    from . import daily, settings as settings_module

    _setup_logging(getattr(args, "verbose", False))
    settings_module.ensure_config()
    settings = settings_module.load()
    target = collect.parse_target_date(args.date)

    from . import calendar_enrich, curate, db, mailer, parking_enrich, render

    connection = _open_db()
    try:
        rows = db.digest_rows(connection, target)
        if not rows:
            raise UsageError(
                f"no stored announcements for {target}. "
                f"Run: uv run dailymail run-daily --date {target}"
            )
        counts = db.counts_for_date(connection, target)
        stored = list(
            connection.execute(
                "SELECT submission_id, final_rank, method FROM curation_results "
                "WHERE target_date = ?",
                (target,),
            )
        )
        if stored:
            ordering = {
                str(r["submission_id"]): {"model_rank": r["final_rank"]} for r in stored
            }
            method = stored[0]["method"]
        else:
            entries = curate.fallback_rank(rows, target, settings)
            ordering = {e["submission_id"]: e for e in entries}
            method = "fallback"

        parking_callouts, _ = parking_enrich.enrich_digest(
            connection, rows, target_date=target, settings=settings,
            allow_refresh=False, allow_resolver=False,
        )
        # Rebuild the calendar actions from stored state so a deliberate resend
        # carries exactly what the original run decided, without a model call.
        candidates, diagnostics = calendar_enrich.detect_candidates(
            rows, target_date=target, settings=settings
        )
        calendar_actions, _ = calendar_enrich.enrich_digest(
            connection, rows, target_date=target, settings=settings,
            candidates=candidates, diagnostics=diagnostics,
            curation_entries=_stored_calendar_judgements(connection, target),
            curation_method=method, allow_routing=False,
        )
        digest = render.render_digest(
            rows, target_date=target, counts=counts, ordering=ordering,
            curation_method=method, settings=settings, parking=parking_callouts,
            calendar=calendar_actions,
        )
        daily._verify_digest(digest, rows)

        existing = db.successful_delivery(connection, target, settings.recipient)
        if existing is not None and not args.force_resend:
            print(
                f"SEND SKIPPED date={target} already delivered to "
                f"{settings.recipient} (delivery {existing['delivery_id']}). "
                "Use --force-resend to override."
            )
            return 0

        prepared = mailer.build_message(
            settings=settings, sender=mailer.sender_address(),
            subject=digest.subject, html=digest.html, text=digest.text,
            images=digest.images,
            calendar_attachments=digest.calendar_attachments,
        )
        daily._verify_message(prepared, digest)
        status = mailer.send(prepared, settings)
        delivery_id = db.record_delivery(
            connection,
            target_date=target,
            recipient=settings.recipient,
            content_hash_value=digest.content_hash,
            state="sent",
            message_id=prepared.message_id,
            smtp_status=status,
            message_bytes=prepared.size_bytes,
            image_count=prepared.image_count,
            forced=args.force_resend,
            sent_at=db.now_utc(),
        )
    finally:
        connection.close()

    print(
        f"SEND OK date={target} to={settings.recipient} "
        f"size={prepared.size_bytes}B images={prepared.image_count} "
        f"calendar={prepared.calendar_count} delivery={delivery_id}"
    )
    print(f"  subject: {prepared.subject}")
    print(f"  message-id: {prepared.message_id}")
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    if args.json:
        return _cmd_health(args)

    from . import db, settings as settings_module, systemd_units

    settings = settings_module.load()
    connection = _open_db()
    try:
        runs = list(
            connection.execute(
                "SELECT * FROM runs ORDER BY run_id DESC LIMIT ?", (args.limit,)
            )
        )
        deliveries = list(
            connection.execute(
                "SELECT * FROM deliveries ORDER BY delivery_id DESC LIMIT ?",
                (args.limit,),
            )
        )
        stats = db.statistics(connection)
        corrections = db.display_status_corrections(connection)
    finally:
        connection.close()

    print(f"database: {db.database_path()}  schema v{stats['schema_version']}")
    print(
        f"  {stats['announcements']} announcements, {stats['versions']} versions, "
        f"{stats['observations']} observations, {stats['categories']} categories, "
        f"{stats['dates_covered']} dates"
    )
    print(f"\nrecent runs (newest first):")
    if not runs:
        print("  (none)")
    for run in runs:
        print(
            f"  #{run['run_id']:<4} {run['target_date']}  {run['status']:<8} "
            f"unique={run['unique_count'] or 0:<3} new={run['new_count'] or 0:<3} "
            f"standing={run['standing_count'] or 0:<3} upd={run['changed_count'] or 0:<2} "
            f"curation={run['curation_method'] or '-':<8} email={run['email_status'] or '-':<18} "
            f"{run['trigger'] or '-'}"
        )
        if run["error_summary"]:
            print(f"        error: {run['error_summary'][:150]}")

    print(f"\nrecent deliveries:")
    if not deliveries:
        print("  (none)")
    for row in deliveries:
        print(
            f"  #{row['delivery_id']:<4} {row['target_date']}  {row['state']:<8} "
            f"{row['recipient']}  {(row['message_bytes'] or 0) // 1024}KB "
            f"img={row['image_count'] or 0} "
            f"{'FORCED ' if row['forced'] else ''}{row['sent_at'] or ''}"
        )
        if row["error_summary"]:
            print(f"        error: {row['error_summary'][:150]}")

    print(
        f"\ncalendar: {stats['calendar_actions_offered']} action(s) offered across "
        f"{stats['calendar_recommendations']} evaluation(s), "
        f"{stats['event_venues']} cached venue(s)"
    )
    for run in runs[:1]:
        if run["calendar_stats"]:
            print(f"  last run: {run['calendar_stats'][:200]}")

    print(
        f"\nlogical repeats: {stats['repeat_matches']} match(es) across "
        f"{stats['logical_families']} durable famil"
        f"{'y' if stats['logical_families'] == 1 else 'ies'} "
        f"({stats['logical_family_members']} member(s))"
    )
    skipped = [row for row in corrections if row["submission_id"] == 0]
    if skipped:
        # A skipped stage means a digest went out with Rowan's own labels
        # instead of the reader's. It is not an alert condition, but it must not
        # be invisible either -- on 2026-09-01 a journal WARNING was the only
        # trace, and the mislabelled digest had already been sent.
        print(f"  ATTENTION: the stage was skipped on {len(skipped)} date(s):")
        for row in skipped[-5:]:
            print(f"    {row['target_date']}  {row['reason'][:120]}")
        print("    re-resolve with: dailymail families --recheck <date> --apply")
    applied = [row for row in corrections if row["submission_id"] != 0]
    if applied:
        dates = sorted({row["target_date"] for row in applied})
        print(
            f"  {len(applied)} display-status correction(s) recorded on "
            f"{len(dates)} date(s): {', '.join(dates[-5:])}"
        )

    print(
        f"\nparking cache: {stats['parking_locations']} location(s), "
        f"{stats['parking_aliases']} alias(es), {stats['parking_sources']} source(s), "
        f"{stats['parking_unresolved']} unresolved"
    )
    for run in runs[:1]:
        if run["parking_stats"]:
            print(f"  last run: {run['parking_stats'][:200]}")

    timer = systemd_units.status()
    print(
        f"\nsystemd: {timer['timer']} enabled={timer['timer_enabled'] or '-'} "
        f"active={timer['timer_active'] or '-'} linger={timer['linger']}"
    )
    if timer["list_timers"]:
        print(f"  {timer['list_timers']}")
    print(f"\nlogs: journalctl --user -u {timer['service']} -n 100 --no-pager")
    return 0


def _cmd_health(args: argparse.Namespace) -> int:
    """Print the stable status contract without changing DailyMail state."""
    from . import health

    print(json.dumps(health.build_status(), sort_keys=True, separators=(",", ":")))
    return 0


def _cmd_db_status(args: argparse.Namespace) -> int:
    from . import db, maintenance

    connection = _open_db()
    try:
        stats = db.statistics(connection)
        categories = list(
            connection.execute(
                "SELECT category_id, title, manual_priority, inferred_priority, "
                "first_seen_at FROM categories ORDER BY "
                "COALESCE(manual_priority, inferred_priority, 999), category_id"
            )
        )
        dates = list(
            connection.execute(
                "SELECT target_date, COUNT(*) AS n, "
                "SUM(CASE WHEN status='New' THEN 1 ELSE 0 END) AS new_n, "
                "SUM(changed) AS changed_n "
                "FROM daily_records GROUP BY target_date ORDER BY target_date DESC "
                "LIMIT 20"
            )
        )
    finally:
        connection.close()

    path = db.database_path()
    size = path.stat().st_size if path.exists() else 0
    print(f"path        : {path}")
    print(f"size        : {size / 1024:.0f} KB")
    for key in (
        "schema_version", "announcements", "versions", "observations",
        "daily_records", "categories", "distribution_dates", "runs",
        "deliveries", "dates_covered", "parking_locations", "parking_aliases",
        "parking_sources", "parking_unresolved",
    ):
        print(f"{key:<12}: {stats[key]}")

    print(f"\ncategories ({len(categories)}), by effective priority:")
    for row in categories:
        marker = (
            f"manual {row['manual_priority']}"
            if row["manual_priority"] is not None
            else (
                f"inferred {row['inferred_priority']}"
                if row["inferred_priority"] is not None
                else "unplaced"
            )
        )
        print(f"  {row['category_id']:>4}  {row['title'][:44]:<44} {marker}")

    print("\ndates stored (newest first):")
    for row in dates:
        print(
            f"  {row['target_date']}  {row['n']:>3} announcements "
            f"({row['new_n']} new, {row['changed_n'] or 0} updated)"
        )

    backups = sorted(maintenance.backups_dir().glob("dailymail-*.sqlite3"))
    print(f"\nbackups: {len(backups)} in {maintenance.backups_dir()}")
    if backups:
        print(f"  newest: {backups[-1].name} ({backups[-1].stat().st_size / 1024:.0f} KB)")
    return 0


def _cmd_db_init(args: argparse.Namespace) -> int:
    from . import db, ingest, settings as settings_module

    _setup_logging()
    settings_module.ensure_config()
    settings = settings_module.load()
    connection = db.connect()
    version = db.initialize(connection)
    try:
        print(f"DB OK {db.database_path()} schema v{version}")
        if not args.no_import:
            result = ingest.import_existing_collections(connection, settings)
            for entry in result["imported"]:
                print(
                    f"  imported {entry['target_date']}: "
                    f"{entry['announcements']} announcements, "
                    f"{entry['categories']} categories, "
                    f"{entry['new_versions']} new version(s), "
                    f"{entry['changed_count']} changed"
                )
            for entry in result["skipped"]:
                print(f"  SKIPPED {entry['file']}: {entry['reason'][:160]}")
            if not result["imported"] and not result["skipped"]:
                print(f"  no collection artifacts found in {result['source']}")
        stats = db.statistics(connection)
        print(
            f"  now: {stats['announcements']} announcements, {stats['versions']} versions, "
            f"{stats['observations']} observations, {stats['categories']} categories, "
            f"{stats['dates_covered']} dates"
        )
    finally:
        connection.close()
    return 0


def _cmd_install_timer(args: argparse.Namespace) -> int:
    from pathlib import Path

    from . import settings as settings_module, systemd_units

    settings_module.ensure_config()
    settings = settings_module.load()
    plan = systemd_units.build_plan(
        working_dir=Path(__file__).resolve().parents[2],
        send_time=settings.daily_send_time,
        timezone=settings.timezone,
        credentials_path=settings_module.credentials_path(),
    )
    if args.print_only:
        print(f"# {plan.service_path}\n{plan.service_text}")
        print(f"# {plan.timer_path}\n{plan.timer_text}")
        return 0

    linger_ok, linger_detail = systemd_units.enable_linger()
    result = systemd_units.install(plan, enable=not args.no_enable)
    status = systemd_units.status()

    print(f"TIMER {'OK' if result.get('enabled', True) else 'PARTIAL'}")
    print(f"  service: {result['service_path']}")
    print(f"  timer  : {result['timer_path']}")
    print(f"  daemon-reload: {result['daemon_reload']}")
    print(f"  enabled: {status['timer_enabled']}  active: {status['timer_active']}")
    print(f"  next   : {status['list_timers'] or '(not scheduled)'}")
    print(f"  linger : {linger_ok} ({linger_detail})")
    if not linger_ok:
        print(
            "  WARNING: lingering is not enabled, so the timer will not run while "
            "logged out. Run: loginctl enable-linger $USER",
            file=sys.stderr,
        )
    return 0


# --- parking -----------------------------------------------------------------


def _parking_line(row) -> str:
    coordinates = (
        f"{row['latitude']:.6f},{row['longitude']:.6f}"
        if row["latitude"] is not None
        else "(no coordinate)"
    )
    flags = []
    if row["manual_override"]:
        flags.append("MANUAL")
    if not row["is_active"]:
        flags.append("INACTIVE")
    if not (row["description"] or "").strip():
        flags.append("NO-DESC")
    return (
        f"  {row['canonical_id']:<34} {row['canonical_name'][:26]:<26} "
        f"{row['location_type']:<12} {row['permit_class']:<9} "
        f"{row['confidence']:<6} {coordinates:<22} {' '.join(flags)}"
    ).rstrip()


def _cmd_parking_status(args: argparse.Namespace) -> int:
    from . import parking, parking_store

    campus = _valid_campus(getattr(args, "campus", None))
    connection = _open_db()
    try:
        stats = parking_store.statistics(connection)
        locations = (
            parking_store.all_locations(connection, campus=campus)
            if args.list
            else []
        )
        unresolved = parking_store.unresolved_rows(connection)
    finally:
        connection.close()

    print(
        f"parking cache: {stats['locations']} location(s), {stats['aliases']} alias(es), "
        f"{stats['with_coordinates']} with coordinates, "
        f"{stats['with_description']} with a description"
    )
    if stats["inactive"]:
        print(f"  {stats['inactive']} inactive location(s)")
    print(f"  manual overrides: {stats['manual_overrides']}")

    print("\nby campus:")
    if not stats["by_campus"]:
        print("  (empty -- run: uv run dailymail parking-refresh)")
    for name, entry in sorted(stats["by_campus"].items()):
        display = parking.CAMPUSES.get(name, {}).get("display", name)
        print(
            f"  {display:<12} {entry['locations']:>3} location(s), "
            f"{stats['aliases_by_campus'].get(name, 0):>3} alias(es), "
            f"{entry['with_coordinates']} geo, {entry['with_description']} described, "
            f"{entry['manual_overrides']} manual"
        )

    if stats["by_type"]:
        print("\nby type:   " + "  ".join(
            f"{key}={value}" for key, value in sorted(stats["by_type"].items())
        ))
    if stats["by_permit"]:
        print("by permit: " + "  ".join(
            f"{key}={value}" for key, value in sorted(stats["by_permit"].items())
        ))

    oldest = stats["oldest_verification"]
    print(
        f"\noldest verification: "
        + (f"{oldest['canonical_id']} at {oldest['at']}" if oldest else "(none)")
    )
    print(f"last source refresh : {stats['last_source_refresh'] or '(never)'}")

    print("\nsources:")
    if not stats["sources"]:
        print("  (none checked yet)")
    for source in stats["sources"]:
        if campus and source["campus"] != campus:
            continue
        print(
            f"  {source['source_id']:<30} {source['campus']:<10} "
            f"{source['last_status'] or '-':<15} "
            f"seen={source['locations_seen'] if source['locations_seen'] is not None else '-':<4} "
            f"verified={source['last_verified_at'] or '-'}"
        )
        if source["last_error"]:
            print(f"      error: {source['last_error'][:140]}")

    print(f"\nunresolved candidates: {len(unresolved)}")
    for row in unresolved[:15]:
        print(
            f"  {row['matched_text'][:28]:<30} campus={row['campus_hint'] or '?':<10} "
            f"seen={row['attempts']} resolver={row['resolver_calls']} "
            f"{(row['last_reason'] or '')[:70]}"
        )

    if args.list:
        print(f"\nlocations ({len(locations)}):")
        for row in locations:
            print(_parking_line(row))
    return 0


def _cmd_parking_lookup(args: argparse.Namespace) -> int:
    from . import parking, parking_sources, parking_store

    campus = _valid_campus(getattr(args, "campus", None))
    connection = _open_db()
    try:
        matches = parking_store.find_locations(connection, args.name, campus=campus)
        aliases = {
            int(row["location_id"]): row["names"]
            for row in connection.execute(
                "SELECT location_id, GROUP_CONCAT(alias, ' | ') AS names "
                "FROM parking_aliases GROUP BY location_id"
            )
        }
    finally:
        connection.close()

    if not matches:
        print(f"no parking location matches {args.name!r}", file=sys.stderr)
        print(
            "  try: uv run dailymail parking-status --list",
            file=sys.stderr,
        )
        return 1

    if len(matches) > 1:
        print(
            f"{len(matches)} locations match {args.name!r} -- this is exactly the "
            f"cross-campus ambiguity the resolver refuses to guess through:"
        )
    for row in matches:
        campus_display = parking.CAMPUSES.get(row["campus"], {}).get(
            "display", row["campus"]
        )
        print(f"\n{row['canonical_name']}  [{row['canonical_id']}]")
        print(f"  campus       : {campus_display}")
        print(f"  type         : {row['location_type']}")
        print(f"  permit / use : {row['permit_class']}")
        print(f"  description  : {row['description'] or '(none cached)'}")
        if row["latitude"] is not None:
            print(f"  coordinates  : {row['latitude']:.6f}, {row['longitude']:.6f}")
            print(f"  google maps  : {parking.maps_url(row['latitude'], row['longitude'])}")
        else:
            url, label = parking_sources.fallback_map_url(row["campus"])
            print("  coordinates  : (none cached)")
            print(f"  fallback map : {url}  ({label})")
        print(f"  confidence   : {row['confidence']}")
        print(f"  active       : {'yes' if row['is_active'] else 'no'}")
        print(f"  source       : {row['source_id'] or '-'} ({row['source_type'] or '-'})")
        print(f"  source url   : {row['source_url'] or '-'}")
        if row["source_map_id"]:
            print(f"  source map id: {row['source_map_id']}")
        if row["source_fingerprint"]:
            print(f"  fingerprint  : {row['source_fingerprint'][:16]}...")
        if row["provenance"]:
            print(f"  provenance   : {row['provenance']}")
        print(f"  description by: {row['description_method'] or '-'}"
              f" {row['description_model'] or ''}".rstrip())
        print(f"  first seen   : {row['first_discovered_at']}")
        print(f"  last verified: {row['last_verified_at']}")
        print(f"  last changed : {row['last_changed_at']}")
        if row["manual_override"]:
            print(f"  MANUAL OVERRIDE on: {row['override_fields']}")
        print(f"  aliases      : {aliases.get(int(row['location_id']), '-')}")
    return 0


def _cmd_parking_refresh(args: argparse.Namespace) -> int:
    from . import parking_refresh, settings as settings_module

    _setup_logging(getattr(args, "verbose", False))
    settings_module.ensure_config()
    settings = settings_module.load()
    campus = _valid_campus(getattr(args, "campus", None))

    connection = _open_db()
    try:
        outcome = parking_refresh.refresh(
            connection,
            settings,
            campus=campus,
            source_ids=[args.source] if args.source else None,
            describe=not args.no_descriptions,
        )
    finally:
        connection.close()

    failures = [entry for entry in outcome.sources if entry.status == "error"]
    print(
        f"PARKING REFRESH {'PARTIAL' if failures else 'OK'} "
        f"sources={len(outcome.sources)} "
        f"changed={sum(1 for e in outcome.sources if e.changed)} "
        f"locations_touched={outcome.locations_touched}"
    )
    for entry in outcome.sources:
        print(
            f"  {entry.source_id:<30} {entry.status:<15} "
            f"seen={entry.locations_seen:<3} +{entry.inserted} ~{entry.updated} "
            f"={entry.unchanged} pinned={entry.overrides_preserved} "
            f"landmarks={entry.landmarks}"
            + (f" unnamed_parking={entry.unnamed_parking}" if entry.unnamed_parking else "")
        )
        if entry.error:
            print(f"      error: {entry.error[:160]}", file=sys.stderr)
        for warning in entry.warnings[:5]:
            print(f"      WARN {warning[:160]}", file=sys.stderr)
    if outcome.review_needed:
        print(
            "  REVIEW: hand-derived source(s) changed upstream; records left "
            f"untouched: {', '.join(outcome.review_needed)}",
            file=sys.stderr,
        )
    print(
        f"  descriptions: {outcome.descriptions_written} written, "
        f"{len(outcome.descriptions_rejected)} rejected, "
        f"{outcome.description_calls} agent call(s), "
        f"${outcome.description_cost_usd:.4f}"
        + (f", model={outcome.description_model}" if outcome.description_model else "")
    )
    for canonical_id, reason in list(outcome.descriptions_rejected.items())[:10]:
        print(f"      REJECTED {canonical_id}: {reason[:140]}", file=sys.stderr)
    if outcome.description_error:
        print(f"      description error: {outcome.description_error}", file=sys.stderr)
    return 1 if failures else 0


def _cmd_parking_set(args: argparse.Namespace) -> int:
    from . import db, parking, parking_store

    connection = _open_db()
    try:
        if args.clear_overrides:
            row = parking_store.clear_manual_override(connection, args.canonical_id)
            if row is None:
                raise UsageError(f"no parking location {args.canonical_id!r}")
            print(f"PARKING OVERRIDE CLEARED {args.canonical_id}")
            return 0

        fields: dict = {}
        if args.description is not None:
            fields["description"] = " ".join(args.description.split())
        if args.latitude is not None:
            fields["latitude"] = args.latitude
        if args.longitude is not None:
            fields["longitude"] = args.longitude
        if args.permit_class is not None:
            if args.permit_class not in parking.PERMIT_CLASSES:
                raise UsageError(
                    f"--permit-class must be one of {list(parking.PERMIT_CLASSES)}"
                )
            fields["permit_class"] = args.permit_class
        if args.location_type is not None:
            if args.location_type not in parking.LOCATION_TYPES:
                raise UsageError(
                    f"--location-type must be one of {list(parking.LOCATION_TYPES)}"
                )
            fields["location_type"] = args.location_type
        if args.canonical_name is not None:
            fields["canonical_name"] = " ".join(args.canonical_name.split())
        if args.confidence is not None:
            fields["confidence"] = args.confidence
        if args.inactive and args.active:
            raise UsageError("--inactive and --active are mutually exclusive")
        if args.inactive:
            fields["is_active"] = False
        if args.active:
            fields["is_active"] = True

        if not fields and not args.alias:
            raise UsageError(
                "nothing to change; pass at least one of --description, --latitude, "
                "--longitude, --permit-class, --location-type, --canonical-name, "
                "--confidence, --inactive/--active or --alias"
            )

        row = None
        if fields:
            try:
                row = parking_store.set_manual_override(
                    connection, args.canonical_id, fields
                )
            except parking.ParkingDataError as exc:
                raise UsageError(str(exc)) from exc
        else:
            row = parking_store.location_by_canonical_id(connection, args.canonical_id)
            if row is None:
                raise UsageError(f"no parking location {args.canonical_id!r}")

        if args.alias:
            with db.transaction(connection):
                added = parking_store.add_aliases(
                    connection, int(row["location_id"]), row["campus"], args.alias,
                    origin="manual",
                )
            print(f"  {added} alias(es) added of {len(args.alias)} requested")
    finally:
        connection.close()

    print(f"PARKING SET OK {args.canonical_id}")
    for key in sorted(fields):
        print(f"  {key} = {fields[key]!r}  (pinned against automated refresh)")
    if row["latitude"] is not None:
        print(f"  google maps: {parking.maps_url(row['latitude'], row['longitude'])}")
    return 0


def _print_family(connection, family) -> None:
    from . import db

    print(f"family {family['family_id']}  {family['family_key']}")
    print(
        f"  canonical: {family['canonical_submission_id']}   "
        f"members: {family['member_count']}   "
        f"method: {family['match_method']}   "
        f"confidence: {family['confidence']}"
    )
    print(f"  created: {family['created_at']}   last seen: {family['last_seen_at']}")
    for member in db.family_members(connection, family["family_id"]):
        marker = "*" if member["is_canonical"] else " "
        matched = member["matched_submission_id"]
        print(
            f"  {marker} {member['submission_id']}  "
            f"first seen {member['first_seen'][:10]}  "
            + (f"matched {matched}  " if matched else "anchor       ")
            + f"{member['match_method'] or ''}"
        )
        dates = db.dates_with_record(connection, member["submission_id"])
        shown = ", ".join(
            f"{row['target_date']}={row['display_status'] or row['status']}"
            for row in dates
        )
        if shown:
            print(f"      appeared: {shown}")


def _cmd_families(args: argparse.Namespace) -> int:
    """Inspect the durable logical families, and re-check one date's decisions."""
    from . import daily, db

    if args.recheck:
        return _recheck_repeats(args)

    connection = db.connect_readonly()
    try:
        if args.submission is not None:
            family = db.family_for_submission(connection, args.submission)
            if family is None:
                print(
                    f"submission {args.submission} belongs to no logical family"
                )
                match = db.repeat_match(connection, args.submission)
                if match is not None:
                    print(
                        f"  but it does have a repeat match: repeats "
                        f"{match['matched_submission_id']} "
                        f"({match['method']}, confidence {match['confidence']})"
                    )
                return 0
            _print_family(connection, family)
            match = db.repeat_match(connection, args.submission)
            if match is not None:
                print("  repeat-match evidence:")
                for key, value in sorted(json.loads(match["evidence"] or "{}").items()):
                    print(f"    {key}: {value}")
            return 0

        if args.key:
            family = db.family_by_key(connection, args.key)
            if family is None:
                print(f"no family with key {args.key!r}")
                return 1
            _print_family(connection, family)
            return 0

        rows = list(
            connection.execute(
                "SELECT * FROM logical_announcement_families "
                "ORDER BY member_count DESC, family_id"
            )
        )
        if not rows:
            print("no logical families recorded yet")
            return 0
        print(f"{len(rows)} logical famil{'y' if len(rows) == 1 else 'ies'}\n")
        for family in rows:
            _print_family(connection, family)
            print()
        return 0
    finally:
        connection.close()


def _recheck_repeats(args: argparse.Namespace) -> int:
    """Re-resolve one past date's repeat classification. Read-only by default.

    This is the operator path for a day the stage was skipped -- as happened on
    2026-09-01, when SQLite could not create a temp file. It never touches
    `daily_records.status`, never rewrites run history, and never resends
    anything: it corrects what future renderings of that date will show, and
    records why in `display_status_corrections`.
    """
    from . import daily, db

    target = args.recheck
    connection = db.connect() if args.apply else db.connect_readonly()
    try:
        rows = db.digest_rows(connection, target)
        if not rows:
            raise UsageError(f"no stored records for {target}")
        before = {
            int(row["submission_id"]): row["display_status"] for row in rows
        }
        matches = daily.resolve_logical_repeats(connection, rows, target)
        changes = [
            (submission_id, match)
            for submission_id, match in sorted(
                matches.items(), key=lambda item: int(item[0])
            )
            if before.get(int(submission_id)) != match.display_status
        ]
        print(
            f"RECHECK {target}: {len(rows)} announcement(s), "
            f"{len(matches)} logical repeat(s), {len(changes)} change(s)"
        )
        titles = {str(row["submission_id"]): row["title"] for row in rows}
        for submission_id, match in sorted(
            matches.items(), key=lambda item: int(item[0])
        ):
            was = before.get(int(submission_id)) or "New (Rowan)"
            arrow = "->" if was != match.display_status else "=="
            print(
                f"  {submission_id} {was} {arrow} {match.display_status}"
                f"{' + UPDATED' if match.materially_changed else ''}"
            )
            print(
                f"      repeats {match.matched_submission_id} "
                f"({match.method}, similarity {match.body_similarity:.4f}, "
                f"via {match.evidence.get('candidate_source')}, "
                f"delivered {match.evidence.get('prior_delivered')})"
            )
            print(f"      {titles.get(submission_id, '')[:88]}")

        if not args.apply:
            print("\nread-only: pass --apply to persist these corrections")
            return 0

        reason = (
            f"dailymail families --recheck {target} --apply: re-resolved under "
            f"the durable logical-family model"
        )
        written = daily.persist_logical_repeats(connection, matches, target)
        with db.transaction(connection):
            for submission_id, match in changes:
                db.record_display_status_correction(
                    connection,
                    target_date=target,
                    submission_id=int(submission_id),
                    previous_display_status=before.get(int(submission_id)),
                    new_display_status=match.display_status,
                    reason=reason,
                )
        print(
            f"\napplied: {written} display status(es) written, "
            f"{len(changes)} correction(s) recorded"
        )
        print("Rowan's own status and the run history are unchanged.")
        print("No digest was resent; this affects future rendering and history.")
        return 0
    finally:
        connection.close()


def _cmd_sessions(args: argparse.Namespace) -> int:
    """What the calendar path detected for a date, session by session."""
    from datetime import date as _date

    from . import calendar_enrich, db, events, settings as settings_module

    settings = settings_module.load()
    connection = db.connect_readonly()
    try:
        rows = db.digest_rows(connection, args.date)
        if not rows:
            raise UsageError(f"no stored records for {args.date}")
        if args.submission is not None:
            rows = [
                row for row in rows
                if int(row["submission_id"]) == args.submission
            ]
            if not rows:
                raise UsageError(
                    f"submission {args.submission} has no record for {args.date}"
                )
        reference = _date.fromisoformat(args.date)
        offered = withheld = 0
        for row in rows:
            sessions, diagnostics = events.detect_series(
                row, reference_date=reference
            )
            if not sessions:
                withheld += 1
                if args.submission is not None:
                    print(
                        f"{row['submission_id']}: no calendar session "
                        f"({diagnostics.get('reason')})"
                    )
                    print(f"  evidence: {diagnostics.get('evidence')}")
                continue
            offered += 1
            print(f"{row['submission_id']}: {row['title'][:78]}")
            print(
                f"  {len(sessions)} session(s)   "
                f"evidence: {', '.join(sessions[0].evidence)}"
            )
            for session in sessions:
                end = session.end.isoformat() if session.end else "(assumed +1h)"
                print(
                    f"    #{session.session_index}/{session.session_count} "
                    f"{session.event_date.isoformat()} "
                    f"{session.start.isoformat()}-{end}   "
                    f"mode={session.attendance_mode} "
                    f"location={session.location or '-'}"
                )
            # Series-level, so read it from the series rather than whichever
            # sitting the loop above happened to finish on.
            if sessions[0].registration_urls:
                print(f"    registration: {sessions[0].registration_urls[0]}")
            print()

        stored = {
            record["submission_id"]: record
            for record in db.calendar_recommendations(connection, args.date)
        }
        if args.submission is None:
            print(
                f"{offered} announcement(s) with sessions, {withheld} without, "
                f"from {len(rows)} record(s) on {args.date}"
            )
        for record in stored.values():
            if not record["offer_calendar"]:
                continue
            if args.submission is not None and (
                record["submission_id"] != args.submission
            ):
                continue
            print(
                f"offered {record['submission_id']}: "
                f"{record['calendar_title']}  "
                f"sessions={record['session_count']}  "
                f"mechanism={record['mechanism']}"
            )
            for entry in json.loads(record["sessions"] or "[]"):
                print(
                    f"    #{entry['index']} {entry['start']} -> {entry['end']}  "
                    f"{entry['ics_filename']}  travel="
                    f"{entry['travel_minutes_before']}/"
                    f"{entry['travel_minutes_after']}"
                )

        if args.ics:
            _print_session_ics(connection, args, settings, rows)
        return 0
    finally:
        connection.close()


def _print_session_ics(connection, args, settings, rows) -> None:
    """Rebuild and print the calendar payloads, without writing anything."""
    from . import calendar_enrich

    candidates, diagnostics = calendar_enrich.detect_candidates(
        rows, target_date=args.date, settings=settings
    )
    actions, _metrics = calendar_enrich.enrich_digest(
        connection, rows, target_date=args.date, settings=settings,
        candidates=candidates, diagnostics=diagnostics,
        curation_entries=[
            {
                "submission_id": str(row["submission_id"]),
                "calendar": {
                    "offer": True, "confidence": 1.0,
                    "reason": "cli --ics inspection",
                },
            }
            for row in rows
        ],
        curation_method="claude",
        allow_routing=False,
    )
    for submission_id, action in sorted(actions.items()):
        for session in action.sessions:
            print(f"\n--- {submission_id} {session.ics_filename} ---")
            print(session.ics_text)


def _valid_campus(value: str | None) -> str | None:
    from . import parking

    if value is None:
        return None
    campus = value.strip().lower()
    if campus not in parking.CAMPUSES:
        raise UsageError(
            f"unknown campus {value!r}; known campuses are "
            f"{sorted(parking.CAMPUSES)}"
        )
    return campus


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "collect":
            return _cmd_collect(args)
        if args.command == "inspect":
            return _cmd_inspect(args)
        if args.command == "run-daily":
            return _cmd_run_daily(args)
        if args.command == "render":
            return _cmd_render(args)
        if args.command == "send":
            return _cmd_send(args)
        if args.command == "status":
            return _cmd_status(args)
        if args.command == "health":
            return _cmd_health(args)
        if args.command == "db-status":
            return _cmd_db_status(args)
        if args.command == "db-init":
            return _cmd_db_init(args)
        if args.command == "families":
            return _cmd_families(args)
        if args.command == "sessions":
            return _cmd_sessions(args)
        if args.command == "install-timer":
            return _cmd_install_timer(args)
        if args.command == "parking-status":
            return _cmd_parking_status(args)
        if args.command == "parking-lookup":
            return _cmd_parking_lookup(args)
        if args.command == "parking-refresh":
            return _cmd_parking_refresh(args)
        if args.command == "parking-set":
            return _cmd_parking_set(args)
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
