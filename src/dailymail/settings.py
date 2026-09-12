"""Non-secret configuration.

Lives in `~/.config/dailymail/config.toml`, created with defaults on first use.
Secrets never appear here -- they stay in `credentials.env` (see `credentials.py`).
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# The user's preference order. Data, not an enum: Rowan can add categories at
# any time and a new one must work on its first day.
#
# The first five are MANUALLY LOCKED -- curation may never reorder them, and the
# inferred-priority mechanism for unknown categories must not touch them.
LOCKED_CATEGORY_COUNT = 5

DEFAULT_CATEGORY_PRIORITY: list[str] = [
    "Official",
    "Technology",
    "Facilities",
    "Human Resources",
    "Public Safety",
    # --- end of locked block ---
    "Web Services",
    "Finance",
    "Payroll",
    "Registrar",
    "Glassboro Campus",
    "Union",
    "Well-being and Health",
    "Academic and Career Success",
    "Academics",
    "Research",
    "Faculty",
    "Student Government",
    "Rowan Online",
    "Rowan On",
    "Library",
    "Shreiber School of Veterinary Medicine",
    "CMSRU",
    "Stratford Campus",
    "Our Deepest Condolences",
    "Campus Activities",
    "Social and Cultural Events",
    "The Arts & Events",
    "Online Events",
    "Volunteer Opportunities",
    "Clubs and Organizations",
    "Athletic Events",
    "Advancement",
    "Our Stories!",
]

# Priority assigned to a category Rowan introduces that curation could not place.
# Sits just past the known list so its announcements always render, never dropped.
UNKNOWN_CATEGORY_PRIORITY = 900


def config_dir() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME")
    return (Path(base) if base else Path.home() / ".config") / "dailymail"


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (Path(base) if base else Path.home() / ".local" / "share") / "dailymail"


def config_path() -> Path:
    return config_dir() / "config.toml"


def credentials_path() -> Path:
    return config_dir() / "credentials.env"


DEFAULT_CONFIG_TOML = """\
# DailyMail configuration. Non-secret settings only.
# SMTP credentials live in credentials.env (mode 0600) alongside this file.

[email]
recipient = "sedlock@rowan.edu"
from_display_name = "Curated Rowan Daily Mail"
subject_prefix = "Curated Rowan Daily Mail"
smtp_host = "smtp.gmail.com"
smtp_port = 587
smtp_timeout_seconds = 60
send_retries = 3

[schedule]
timezone = "America/New_York"
daily_send_time = "06:30"

[curation]
# Executable is resolved via PATH if not absolute. systemd's PATH is minimal, so
# an absolute path is safer for production.
claude_executable = "{claude_executable}"
# A capable production model rather than the most expensive option.
model = "sonnet"
# Headroom, not an estimate. Ranking a full day has taken up to 237s in
# production and on 9 September 2026 it reached the old 240s wall, so the run
# killed a call that was working and shipped the deterministic ordering instead.
# The digest was complete and correct -- the fallback did its job -- but the
# reader lost the curation they were entitled to for the sake of three seconds.
# The whole run is separately bounded by the unit's TimeoutStartSec=1800.
timeout_seconds = 420
enabled = true

[parking]
# Parking-location enrichment. Reference data lives in SQLite and is refreshed
# rarely; a normal cached day costs no network call and no Claude call.
enabled = true
# Refresh an authoritative source only when it has not been verified this long.
refresh_after_days = 180
# Refresh the campus source immediately when a parking mention misses the cache.
refresh_on_miss = true
# The targeted resolver is the last resort for a lot no source knows. It is the
# only parking path granted web access, and never sees curation context.
resolver_enabled = true
resolver_timeout_seconds = 300
# Model for reference-data generation and targeted resolution. Not the curation
# model setting: these are separate invocations with separate tool policy.
model = "sonnet"
# Cached plain-English descriptions are written by a no-tools Claude call from
# collected authoritative evidence, then validated before being stored.
descriptions_enabled = true
description_timeout_seconds = 300
description_batch = 10
# Do not enrich from a record cached below this confidence; show the official
# campus parking map instead.
min_confidence = "medium"
# Cap the callouts on a single announcement so a lot list cannot dominate it.
max_callouts = 6

[calendar]
# Intelligent Add-to-Calendar actions on announcements that describe a real,
# scheduled, relevant event. Enrichment only: it can never block a digest.
enabled = true
# Offer the action only at or above this relevance confidence (0.0-1.0).
# Raise it to be stricter, lower it to see more calendar buttons.
relevance_threshold = 0.6
# Cap the number of calendar actions in one digest.
max_actions = 8
# Attach a standards-compliant .ics per offered event (RFC 5545). This is the
# mechanism that can carry travel holds; the button URL cannot.
attach_ics = true
# Where the reader normally is. Used only to work out travel time; it is a
# calculation reference and is not advertised in the email.
base_address = "201 Mullica Hill Rd, Glassboro, NJ 08028"
base_latitude = 39.70791
base_longitude = -75.11288
base_campus = "glassboro"
# Reserve travel time around events that need physical movement.
travel_enabled = true
# At or below this distance from base the trip is treated as a campus walk.
walk_max_metres = 1200
walking_speed_mps = 1.3
# Padding added to a walk, and to each leg of a drive.
walk_padding_minutes = 3
drive_arrival_padding_minutes = 10
drive_return_padding_minutes = 5
# Travel blocks are rounded up to this many minutes and never exceed the cap.
travel_rounding_minutes = 5
travel_min_minutes = 10
travel_max_minutes = 90
# Read-only OSRM route lookup for genuine drives. Never a hard dependency: a
# failure falls back to a conservative distance estimate, tagged as estimated,
# and never suppresses the calendar action.
routing_enabled = true
routing_url = "https://router.project-osrm.org"
routing_timeout_seconds = 6
# Re-verify a cached venue's travel time only this rarely. Venues repeat weekly.
venue_cache_days = 180

[images]
# Inline data: URI images are decoded, downscaled and re-embedded as CID parts.
max_width_px = 1500
max_bytes_per_image = 1572864      # 1.5 MB
max_total_bytes = 8388608          # 8 MB
initial_jpeg_quality = 82
min_jpeg_quality = 45
download_external = true
external_timeout_seconds = 15
external_max_download_bytes = 26214400   # 25 MB before processing

[retention]
diagnostics_days = 30
backups_keep = 30
collection_artifacts_days = 14

[collector]
page_size = 100
# Transient-failure retry schedule, in seconds (0 = immediate first attempt).
retry_delays_seconds = [300, 900]

# Manual category priority. Order is the priority; the first five are locked and
# must not be reordered by curation. Rowan categories absent from this list are
# ingested normally and given an inferred priority instead of being dropped.
[categories]
priority = [
{category_lines}
]
locked_count = {locked_count}
"""


def render_default_config(claude_executable: str = "claude") -> str:
    lines = ",\n".join(f'  "{name}"' for name in DEFAULT_CATEGORY_PRIORITY)
    return DEFAULT_CONFIG_TOML.format(
        claude_executable=claude_executable,
        category_lines=lines,
        locked_count=LOCKED_CATEGORY_COUNT,
    )


@dataclass(frozen=True)
class Settings:
    recipient: str
    from_display_name: str
    subject_prefix: str
    smtp_host: str
    smtp_port: int
    smtp_timeout_seconds: int
    send_retries: int
    timezone: str
    daily_send_time: str
    claude_executable: str
    claude_model: str
    curation_timeout_seconds: int
    curation_enabled: bool
    image_max_width_px: int
    image_max_bytes: int
    image_max_total_bytes: int
    image_initial_quality: int
    image_min_quality: int
    image_download_external: bool
    image_external_timeout_seconds: int
    image_external_max_download_bytes: int
    diagnostics_days: int
    backups_keep: int
    collection_artifacts_days: int
    page_size: int
    retry_delays_seconds: tuple[int, ...]
    parking_enabled: bool = True
    parking_refresh_days: int = 180
    parking_refresh_on_miss: bool = True
    parking_resolver_enabled: bool = True
    parking_resolver_timeout_seconds: int = 300
    parking_model: str = "sonnet"
    parking_descriptions_enabled: bool = True
    parking_description_timeout_seconds: int = 300
    parking_description_batch: int = 10
    parking_min_confidence: str = "medium"
    parking_max_callouts: int = 6
    calendar_enabled: bool = True
    calendar_relevance_threshold: float = 0.6
    calendar_max_actions: int = 8
    calendar_attach_ics: bool = True
    calendar_base_address: str = "201 Mullica Hill Rd, Glassboro, NJ 08028"
    calendar_base_latitude: float = 39.70791
    calendar_base_longitude: float = -75.11288
    calendar_base_campus: str = "glassboro"
    calendar_travel_enabled: bool = True
    calendar_walk_max_metres: float = 1200.0
    calendar_walking_speed_mps: float = 1.3
    calendar_walk_padding_minutes: int = 3
    calendar_drive_arrival_padding_minutes: int = 10
    calendar_drive_return_padding_minutes: int = 5
    calendar_travel_rounding_minutes: int = 5
    calendar_travel_min_minutes: int = 10
    calendar_travel_max_minutes: int = 90
    calendar_routing_enabled: bool = True
    calendar_routing_url: str = "https://router.project-osrm.org"
    calendar_routing_timeout_seconds: int = 6
    calendar_venue_cache_days: int = 180
    category_priority: tuple[str, ...] = field(default=())
    locked_category_count: int = LOCKED_CATEGORY_COUNT

    def category_priority_map(self) -> dict[str, int]:
        """Category title -> 1-based priority. Lower sorts first."""
        return {name: index + 1 for index, name in enumerate(self.category_priority)}

    def initial_quality_or_default(self) -> int:
        return max(self.image_min_quality, min(95, self.image_initial_quality))

    def subject_for(self, target_date: str) -> str:
        """`Curated Rowan Daily Mail - August 21 2026` (no comma before the year)."""
        from datetime import date

        parsed = date.fromisoformat(target_date)
        return (
            f"{self.subject_prefix} - "
            f"{parsed.strftime('%B')} {parsed.day} {parsed.year}"
        )

    def alert_subject_for(self, target_date: str) -> str:
        from datetime import date

        parsed = date.fromisoformat(target_date)
        return (
            "DailyMail ATTENTION REQUIRED - "
            f"{parsed.strftime('%B')} {parsed.day} {parsed.year}"
        )


def _default_claude_executable() -> str:
    import shutil

    return shutil.which("claude") or "claude"


def ensure_config(path: Path | None = None) -> Path:
    """Create config.toml with defaults if absent. Never overwrites."""
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not target.exists():
        target.write_text(
            render_default_config(_default_claude_executable()), encoding="utf-8"
        )
        target.chmod(0o600)
    return target


def load(path: Path | None = None) -> Settings:
    target = path or config_path()
    raw: dict = {}
    if target.exists():
        raw = tomllib.loads(target.read_text(encoding="utf-8"))

    email = raw.get("email", {})
    schedule = raw.get("schedule", {})
    curation = raw.get("curation", {})
    images = raw.get("images", {})
    retention = raw.get("retention", {})
    collector = raw.get("collector", {})
    categories = raw.get("categories", {})
    parking_cfg = raw.get("parking", {})
    calendar_cfg = raw.get("calendar", {})

    priority = tuple(categories.get("priority") or DEFAULT_CATEGORY_PRIORITY)

    return Settings(
        recipient=email.get("recipient", "sedlock@rowan.edu"),
        from_display_name=email.get("from_display_name", "Curated Rowan Daily Mail"),
        subject_prefix=email.get("subject_prefix", "Curated Rowan Daily Mail"),
        smtp_host=email.get("smtp_host", "smtp.gmail.com"),
        smtp_port=int(email.get("smtp_port", 587)),
        smtp_timeout_seconds=int(email.get("smtp_timeout_seconds", 60)),
        send_retries=int(email.get("send_retries", 3)),
        timezone=schedule.get("timezone", "America/New_York"),
        daily_send_time=schedule.get("daily_send_time", "06:30"),
        claude_executable=curation.get("claude_executable")
        or _default_claude_executable(),
        claude_model=curation.get("model", "sonnet"),
        curation_timeout_seconds=int(curation.get("timeout_seconds", 420)),
        curation_enabled=bool(curation.get("enabled", True)),
        image_max_width_px=int(images.get("max_width_px", 1500)),
        image_max_bytes=int(images.get("max_bytes_per_image", 1572864)),
        image_max_total_bytes=int(images.get("max_total_bytes", 8388608)),
        image_initial_quality=int(images.get("initial_jpeg_quality", 82)),
        image_min_quality=int(images.get("min_jpeg_quality", 45)),
        image_download_external=bool(images.get("download_external", True)),
        image_external_timeout_seconds=int(images.get("external_timeout_seconds", 15)),
        image_external_max_download_bytes=int(
            images.get("external_max_download_bytes", 26214400)
        ),
        diagnostics_days=int(retention.get("diagnostics_days", 30)),
        backups_keep=int(retention.get("backups_keep", 30)),
        collection_artifacts_days=int(retention.get("collection_artifacts_days", 14)),
        page_size=int(collector.get("page_size", 100)),
        retry_delays_seconds=tuple(
            collector.get("retry_delays_seconds", [300, 900])
        ),
        parking_enabled=bool(parking_cfg.get("enabled", True)),
        parking_refresh_days=int(parking_cfg.get("refresh_after_days", 180)),
        parking_refresh_on_miss=bool(parking_cfg.get("refresh_on_miss", True)),
        parking_resolver_enabled=bool(parking_cfg.get("resolver_enabled", True)),
        parking_resolver_timeout_seconds=int(
            parking_cfg.get("resolver_timeout_seconds", 300)
        ),
        parking_model=parking_cfg.get("model") or curation.get("model", "sonnet"),
        parking_descriptions_enabled=bool(
            parking_cfg.get("descriptions_enabled", True)
        ),
        parking_description_timeout_seconds=int(
            parking_cfg.get("description_timeout_seconds", 300)
        ),
        parking_description_batch=int(parking_cfg.get("description_batch", 10)),
        parking_min_confidence=str(parking_cfg.get("min_confidence", "medium")),
        parking_max_callouts=int(parking_cfg.get("max_callouts", 6)),
        calendar_enabled=bool(calendar_cfg.get("enabled", True)),
        calendar_relevance_threshold=float(
            calendar_cfg.get("relevance_threshold", 0.6)
        ),
        calendar_max_actions=int(calendar_cfg.get("max_actions", 8)),
        calendar_attach_ics=bool(calendar_cfg.get("attach_ics", True)),
        calendar_base_address=str(
            calendar_cfg.get("base_address", "201 Mullica Hill Rd, Glassboro, NJ 08028")
        ),
        calendar_base_latitude=float(calendar_cfg.get("base_latitude", 39.70791)),
        calendar_base_longitude=float(calendar_cfg.get("base_longitude", -75.11288)),
        calendar_base_campus=str(calendar_cfg.get("base_campus", "glassboro")),
        calendar_travel_enabled=bool(calendar_cfg.get("travel_enabled", True)),
        calendar_walk_max_metres=float(calendar_cfg.get("walk_max_metres", 1200)),
        calendar_walking_speed_mps=float(calendar_cfg.get("walking_speed_mps", 1.3)),
        calendar_walk_padding_minutes=int(
            calendar_cfg.get("walk_padding_minutes", 3)
        ),
        calendar_drive_arrival_padding_minutes=int(
            calendar_cfg.get("drive_arrival_padding_minutes", 10)
        ),
        calendar_drive_return_padding_minutes=int(
            calendar_cfg.get("drive_return_padding_minutes", 5)
        ),
        calendar_travel_rounding_minutes=max(
            1, int(calendar_cfg.get("travel_rounding_minutes", 5))
        ),
        calendar_travel_min_minutes=int(calendar_cfg.get("travel_min_minutes", 10)),
        calendar_travel_max_minutes=int(calendar_cfg.get("travel_max_minutes", 90)),
        calendar_routing_enabled=bool(calendar_cfg.get("routing_enabled", True)),
        calendar_routing_url=str(
            calendar_cfg.get("routing_url", "https://router.project-osrm.org")
        ).rstrip("/"),
        calendar_routing_timeout_seconds=int(
            calendar_cfg.get("routing_timeout_seconds", 6)
        ),
        calendar_venue_cache_days=int(calendar_cfg.get("venue_cache_days", 180)),
        category_priority=priority,
        locked_category_count=int(
            categories.get("locked_count", LOCKED_CATEGORY_COUNT)
        ),
    )
