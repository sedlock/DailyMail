"""Deterministic rendering of the digest.

Claude never touches this. The email is built entirely from validated database
content plus an accepted ordering, so the output is a pure function of stored
state and reproducible from it.
"""

from __future__ import annotations

import hashlib
import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape
from markupsafe import Markup

from . import sanitize
from .images import EmbeddedImage, ImageProcessor
from .normalize import html_to_text
from .settings import UNKNOWN_CATEGORY_PRIORITY, Settings

TEMPLATE_DIR = str(Path(__file__).parent / "templates")

AUDIENCE_LABELS = {"Employees": "EMPLOYEE", "Students": "STUDENT", "Both": "EVERYONE"}


@dataclass
class RenderedDigest:
    target_date: str
    subject: str
    html: str
    text: str
    images: list[EmbeddedImage]
    counts: dict
    content_hash: str
    curation_method: str
    image_stats: dict
    submission_ids: list[str] = field(default_factory=list)
    link_normalizations: int = 0
    links_dropped: int = 0
    parking_callouts: int = 0
    parking_unresolved: int = 0
    calendar_callouts: int = 0
    calendar_attachments: list = field(default_factory=list)
    duplicate_titles_suppressed: int = 0


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html", "j2"], default_for_string=False),
        undefined=StrictUndefined,
        trim_blocks=False,
        lstrip_blocks=False,
    )


def _format_time(value: str | None) -> str | None:
    if not value:
        return None
    try:
        parsed = datetime.strptime(value, "%H:%M:%S")
    except ValueError:
        return value
    return parsed.strftime("%-I:%M %p") if parsed.minute else parsed.strftime("%-I %p")


def _event_when(event_date: str | None, start: str | None, end: str | None) -> str | None:
    parts: list[str] = []
    if event_date:
        try:
            parts.append(date.fromisoformat(event_date).strftime("%A, %B %-d, %Y"))
        except ValueError:
            parts.append(event_date)
    start_text, end_text = _format_time(start), _format_time(end)
    if start_text and end_text:
        parts.append(f"{start_text} – {end_text}")
    elif start_text:
        parts.append(start_text)
    return " · ".join(parts) or None


def _person_line(name, department, job_title, email, phone) -> str | None:
    """Compact one-line rendering of a published contact block."""
    primary = " ".join(filter(None, [name]))
    details = [value for value in (job_title, department) if value]
    tail = [value for value in (email, phone) if value]
    pieces = []
    if primary:
        pieces.append(primary)
    if details:
        pieces.append(", ".join(details))
    if tail:
        pieces.append(" · ".join(tail))
    return " — ".join(pieces) or None


def clean_subject(raw: str | None) -> str:
    """Fix accidental whitespace defects only; never rewrite editorially."""
    return " ".join((raw or "").split())


def build_items(
    rows,
    ordering: dict[str, dict],
    settings: Settings,
    processor: ImageProcessor,
    parking: dict[str, list] | None = None,
    calendar: dict | None = None,
):
    """Turn database rows into render-ready items, sanitizing bodies as we go."""
    items: dict[str, dict] = {}
    total_normalized = 0
    total_dropped = 0
    suppressed_titles = 0

    for row in rows:
        submission_id = str(row["submission_id"])
        official_url = row["official_url"]

        safe_html, link_stats, _seen = sanitize.sanitize_body(
            row["full_body"] or "",
            image_resolver=processor.resolver_for(submission_id, official_url),
        )
        total_normalized += len(link_stats.normalized)
        total_dropped += link_stats.dropped

        # Render-time only. The stored `full_body` is authoritative and is never
        # rewritten; this drops a leading block that merely repeats a headline
        # the card already shows above it.
        title = clean_subject(row["title"])
        action = (calendar or {}).get(submission_id)
        # Two headlines can precede the body: the card's own subject, and -- when
        # an event earned one -- the calendar callout's event name. A first body
        # block that exactly repeats either is redundant either way.
        headlines = [title]
        if action is not None and action.title and action.title != title:
            headlines.append(action.title)

        title_removed = False
        for headline in headlines:
            safe_html, removed = sanitize.suppress_duplicate_heading(
                safe_html, headline
            )
            if removed:
                title_removed = True
                suppressed_titles += 1
                break

        meta_lines: list[dict] = []
        contact = _person_line(
            row["contact_name"], row["contact_department"], row["contact_job_title"],
            row["contact_email"], row["contact_phone"],
        )
        if contact:
            meta_lines.append({"label": "Contact", "value": contact})
        submitted = _person_line(
            row["submitted_by_name"], row["submitted_by_department"],
            row["submitted_by_job_title"], row["submitted_by_email"],
            row["submitted_by_phone"],
        )
        if submitted:
            meta_lines.append({"label": "Submitted by", "value": submitted})
        approved = _person_line(
            row["approved_by_name"], row["approved_by_department"],
            row["approved_by_job_title"], row["approved_by_email"],
            row["approved_by_phone"],
        )
        if approved:
            meta_lines.append({"label": "Approved by", "value": approved})
        if row["changed"] and row["source_updated_by_name"]:
            meta_lines.append(
                {"label": "Updated by", "value": row["source_updated_by_name"]}
            )

        event = None
        if row["is_event"]:
            when = _event_when(row["event_date"], row["event_start_time"], row["event_end_time"])
            if when or row["event_name"] or row["event_location"]:
                event = {
                    "name": row["event_name"],
                    "when": when,
                    "location": row["event_location"],
                }

        body_text = (row["body_text"] or "").strip() or html_to_text(row["full_body"] or "")
        # The plain-text alternative is derived separately, so it is checked
        # independently: an entity or a wrapper tag can make the HTML block fail
        # the match while the text line matches cleanly, or the reverse.
        for headline in headlines:
            body_text, removed = sanitize.suppress_duplicate_text_heading(
                body_text, headline
            )
            if removed:
                if not title_removed:
                    suppressed_titles += 1
                break

        items[submission_id] = {
            "submission_id": submission_id,
            "title": title,
            "official_url": official_url,
            "status": row["status"],
            "changed": bool(row["changed"]),
            "audience_label": AUDIENCE_LABELS.get(row["source_audience"], "EVERYONE"),
            "category_title": row["category_title"] or f"Category {row['category_id']}",
            "category_priority": (
                row["manual_priority"]
                if row["manual_priority"] is not None
                else (
                    row["inferred_priority"]
                    if row["inferred_priority"] is not None
                    else UNKNOWN_CATEGORY_PRIORITY
                )
            ),
            "body_html": Markup(safe_html),
            "body_text": "\n".join(
                textwrap.fill(line, width=72, replace_whitespace=False) or ""
                for line in body_text.splitlines()
            ).strip(),
            "meta_lines": meta_lines,
            "event": event,
            "content_hash": row["content_hash"],
            "model_rank": ordering.get(submission_id, {}).get("model_rank", 9999),
            # Additive enrichment. Empty for almost every announcement, and never
            # part of `content_hash`: it is reference data about the announcement,
            # not the announcement's own published content.
            "parking": list((parking or {}).get(submission_id) or []),
            # Additive, like parking: reference data about the announcement, not
            # the announcement's own published content, and never hashed.
            "calendar": action,
            # Context-aware: a card inside a category group does not repeat the
            # group's own heading. Grouping flips this off; anything rendered
            # outside a group keeps its category.
            "show_category": True,
        }

    return items, total_normalized, total_dropped, suppressed_titles


def group_new(items: dict[str, dict]) -> list[dict]:
    """Group the New section by category, in configured priority order.

    Within a category, the accepted ranking decides order. Empty groups are
    never emitted.
    """
    new_items = [item for item in items.values() if item["status"] == "New"]
    buckets: dict[tuple[int, str], list[dict]] = {}
    for item in new_items:
        buckets.setdefault(
            (item["category_priority"], item["category_title"]), []
        ).append(item)

    groups = []
    for (priority, title) in sorted(buckets):
        entries = sorted(
            buckets[(priority, title)],
            key=lambda item: (item["model_rank"], -int(item["submission_id"])),
        )
        if entries:
            for entry in entries:
                # The group heading above already states the category; repeating
                # it on every card is noise.
                entry["show_category"] = False
            groups.append({"category": title, "priority": priority, "announcements": entries})
    return groups


def order_standing(items: dict[str, dict]) -> list[dict]:
    """Standing is a single globally ranked list."""
    standing = [item for item in items.values() if item["status"] == "Standing"]
    for item in standing:
        # Standing is one globally ranked list with no category headings, so a
        # Standing card must keep its own category label.
        item["show_category"] = True
    return sorted(
        standing, key=lambda item: (item["model_rank"], -int(item["submission_id"]))
    )


def digest_content_hash(target_date: str, ordered_ids: list[str], items: dict) -> str:
    """Identifies this exact digest: the day, the order, and the content shown."""
    payload = {
        "target_date": target_date,
        "order": ordered_ids,
        "versions": {sid: items[sid]["content_hash"] for sid in ordered_ids},
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()


def render_digest(
    rows,
    *,
    target_date: str,
    counts: dict,
    ordering: dict[str, dict],
    curation_method: str,
    settings: Settings,
    http_client=None,
    parking: dict[str, list] | None = None,
    calendar: dict | None = None,
) -> RenderedDigest:
    processor = ImageProcessor(settings, http_client=http_client)
    items, normalized, dropped, suppressed_titles = build_items(
        rows, ordering, settings, processor, parking=parking, calendar=calendar
    )

    new_groups = group_new(items)
    standing_items = order_standing(items)

    ordered_ids = [item["submission_id"] for group in new_groups for item in group["announcements"]]
    ordered_ids += [item["submission_id"] for item in standing_items]

    parsed_date = date.fromisoformat(target_date)
    digest_date_long = parsed_date.strftime("%A, %B %-d, %Y")
    subject = settings.subject_for(target_date)

    preheader_bits = []
    for group in new_groups:
        for item in group["announcements"]:
            preheader_bits.append(item["title"])
            if len(preheader_bits) >= 3:
                break
        if len(preheader_bits) >= 3:
            break
    preheader = (
        f"{counts['new']} new, {counts['standing']} standing. "
        + " · ".join(preheader_bits)
    )[:180]

    context = {
        "subject": subject,
        "preheader": preheader,
        "target_date": target_date,
        "digest_date_long": digest_date_long,
        "counts": counts,
        "new_groups": new_groups,
        "standing_items": standing_items,
        "curation_method": curation_method,
        "images_embedded": processor.stats.embedded,
    }

    environment = _environment()
    html = environment.get_template("email.html.j2").render(**context)

    text_environment = Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=False,
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    text = text_environment.get_template("email.txt.j2").render(**context)
    # Collapse the blank-line runs Jinja leaves behind in plain text.
    while "\n\n\n\n" in text:
        text = text.replace("\n\n\n\n", "\n\n\n")

    return RenderedDigest(
        target_date=target_date,
        subject=subject,
        html=html,
        text=text,
        images=processor.images,
        counts=counts,
        content_hash=digest_content_hash(target_date, ordered_ids, items),
        curation_method=curation_method,
        image_stats=processor.stats.as_dict(),
        submission_ids=ordered_ids,
        link_normalizations=normalized,
        links_dropped=dropped,
        parking_callouts=sum(
            1 for spots in (parking or {}).values() for spot in spots if spot.resolved
        ),
        parking_unresolved=sum(
            1 for spots in (parking or {}).values() for spot in spots if not spot.resolved
        ),
        calendar_callouts=sum(
            1 for sid in ordered_ids if items[sid].get("calendar") is not None
        ),
        calendar_attachments=[
            items[sid]["calendar"]
            for sid in ordered_ids
            if items[sid].get("calendar") is not None
            and items[sid]["calendar"].ics_text
        ],
        duplicate_titles_suppressed=suppressed_titles,
    )
