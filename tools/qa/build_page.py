"""Render the visual-regression page the Playwright suite asserts against.

Diagnostics only. This is deliberately *not* on the production path: it builds a
digest from committed fixtures rather than from the live database, so the QA page
is byte-stable, contains no personal data, and needs neither Rowan nor Claude.

    uv run python tools/qa/build_page.py --out artifacts/qa/pages

Produces `render-regression.html` plus a `.txt` alternative and a small JSON
manifest naming what each card is supposed to prove.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dailymail import (  # noqa: E402
    calendar_enrich,
    db,
    parking as parking_module,
    render,
    settings as settings_module,
)

FIXTURE = REPO / "artifacts" / "qa" / "fixtures" / "render-regression.json"

# The fixtures were captured on this date and their session dates are relative
# to it, so the page is reproducible rather than drifting with the wall clock.
TARGET_DATE = "2026-09-01"

# Every card is offered a calendar action so the multi-session control is always
# on the page, whatever the relevance model would have said on the day.
_ALWAYS_RELEVANT = {
    "offer": True,
    "confidence": 1.0,
    "reason": "visual regression fixture",
    "attendance_mode": None,
    "suggested_title": None,
}


def _seed(connection, announcements: list[dict]) -> None:
    stamp = db.now_utc()
    with db.transaction(connection):
        for index, entry in enumerate(announcements):
            db.upsert_category(
                connection,
                category_id=entry["category_id"],
                title=f"QA Category {entry['category_id']}",
                rowan_rank=index + 1,
                color=None,
                is_active=True,
                manual_priority=index + 1,
            )
            status = entry.get("status") or "New"
            record = {
                "submission_id": entry["submission_id"],
                "title": entry["title"],
                "full_body": entry["full_body"],
                "body_text": entry["body_text"],
                "source_audience": entry["source_audience"],
                "category_id": entry["category_id"],
                "distribution_dates": [TARGET_DATE],
                "first_distribution_date": TARGET_DATE,
                "status": status,
                "is_event": entry.get("is_event") or 0,
                "event_name": entry.get("event_name"),
                "event_date": entry.get("event_date"),
                "event_start_time": entry.get("event_start_time"),
                "event_end_time": entry.get("event_end_time"),
                "event_location": entry.get("event_location"),
                "extra_edition": entry.get("extra_edition") or 0,
            }
            version_id, _ = db.record_announcement(
                connection, record, observed_at=stamp
            )
            db.record_daily(
                connection,
                target_date=TARGET_DATE,
                submission_id=entry["submission_id"],
                version_id=version_id,
                status=status,
                changed=bool(entry.get("changed")),
                observed_at=stamp,
            )


def _isolate_state() -> Path:
    """Point the database and state directories at a scratch location.

    Not optional, and not left to the caller. This script seeds announcements
    and renders a digest, and both of those write: run without isolation it
    inserts its fixtures into the live history, re-points that day's records at
    them and rewrites the day's calendar decisions. It did exactly that once,
    which is why the isolation now lives here rather than in a wrapper.

    `XDG_CONFIG_HOME` is deliberately left alone. Configuration is read-only and
    the QA page should reflect the real settings -- the base address, the travel
    policy, the relevance threshold -- rather than a set of defaults.
    """
    scratch = Path(
        os.environ.get("DAILYMAIL_QA_STATE")
        or tempfile.mkdtemp(prefix="dailymail-qa-")
    )
    os.environ["XDG_DATA_HOME"] = str(scratch / "data")
    os.environ["XDG_STATE_HOME"] = str(scratch / "state")
    resolved = db.database_path()
    if not resolved.is_relative_to(scratch):
        raise SystemExit(
            f"refusing to run: the database resolved to {resolved}, which is "
            f"outside the scratch directory {scratch}"
        )
    return scratch


def build(out_dir: Path) -> dict:
    fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
    announcements = fixture["announcements"]

    scratch = _isolate_state()
    settings = settings_module.load()
    connection = db.connect()
    db.initialize(connection)
    try:
        _seed(connection, announcements)
        rows = db.digest_rows(connection, TARGET_DATE)
        counts = db.counts_for_date(connection, TARGET_DATE)

        candidates, diagnostics = calendar_enrich.detect_candidates(
            rows, target_date=TARGET_DATE, settings=settings
        )
        judgements = [
            {"submission_id": str(entry["submission_id"]), "calendar": _ALWAYS_RELEVANT}
            for entry in announcements
        ]
        actions, metrics = calendar_enrich.enrich_digest(
            connection,
            rows,
            target_date=TARGET_DATE,
            settings=settings,
            candidates=candidates,
            diagnostics=diagnostics,
            curation_entries=judgements,
            curation_method="claude",
            # Diagnostics must not reach out to a routing service.
            allow_routing=False,
            router=lambda origin, destination, *, settings: None,
        )

        ordering = {
            str(entry["submission_id"]): {"model_rank": index + 1}
            for index, entry in enumerate(announcements)
        }
        # Parking callouts are supplied from the fixture rather than resolved,
        # so the page exercises the callout markup without a database of lots
        # and without a network call.
        parking = {
            str(entry["submission_id"]): [
                parking_module.ParkingCallout(**spot) for spot in entry["parking"]
            ]
            for entry in announcements
            if entry.get("parking")
        }
        digest = render.render_digest(
            rows,
            target_date=TARGET_DATE,
            counts=counts,
            ordering=ordering,
            curation_method="claude",
            settings=settings,
            parking=parking,
            calendar=actions,
        )
    finally:
        connection.close()

    out_dir.mkdir(parents=True, exist_ok=True)
    html_path = out_dir / "render-regression.html"
    html_path.write_text(digest.html, encoding="utf-8")
    (out_dir / "render-regression.txt").write_text(digest.text, encoding="utf-8")

    manifest = {
        "target_date": TARGET_DATE,
        "scratch_state": str(scratch),
        "html": html_path.name,
        "body_color": render.BODY_INK,
        "accent_color": render.BODY_ACCENT,
        "counts": {"new": counts["new"], "standing": counts["standing"]},
        "parking_callouts": digest.parking_callouts,
        "calendar": {
            "actions": metrics.actions_offered,
            "session_actions": metrics.session_actions_offered,
            "multi_session": metrics.multi_session_announcements,
        },
        "cards": [
            {
                "submission_id": str(entry["submission_id"]),
                "kind": entry["kind"],
                "why": entry["why"],
                "status": entry.get("status") or "New",
                "changed": bool(entry.get("changed")),
                "parking": bool(entry.get("parking")),
                "sessions": (
                    actions[str(entry["submission_id"])].session_count
                    if str(entry["submission_id"]) in actions
                    else 0
                ),
                "ics_filenames": [
                    session.ics_filename
                    for session in actions[str(entry["submission_id"])].sessions
                ]
                if str(entry["submission_id"]) in actions
                else [],
            }
            for entry in announcements
        ],
    }
    (out_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out",
        default=str(REPO / "artifacts" / "qa" / "pages"),
        help="directory for the generated page (default: artifacts/qa/pages)",
    )
    args = parser.parse_args(argv)
    manifest = build(Path(args.out))
    print(f"QA PAGE OK date={manifest['target_date']} -> {args.out}")
    print(f"  scratch state: {manifest['scratch_state']}")
    print(
        f"  calendar: {manifest['calendar']['actions']} action(s), "
        f"{manifest['calendar']['session_actions']} session control(s), "
        f"{manifest['calendar']['multi_session']} multi-session"
    )
    for card in manifest["cards"]:
        print(
            f"  {card['submission_id']} {card['kind']:<24} "
            f"sessions={card['sessions']} {card['ics_filenames']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - diagnostic entry point
    raise SystemExit(main())
