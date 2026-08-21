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
daily_send_time = "07:00"

[curation]
# Executable is resolved via PATH if not absolute. systemd's PATH is minimal, so
# an absolute path is safer for production.
claude_executable = "{claude_executable}"
# A capable production model rather than the most expensive option.
model = "sonnet"
timeout_seconds = 240
enabled = true

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
        daily_send_time=schedule.get("daily_send_time", "07:00"),
        claude_executable=curation.get("claude_executable")
        or _default_claude_executable(),
        claude_model=curation.get("model", "sonnet"),
        curation_timeout_seconds=int(curation.get("timeout_seconds", 240)),
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
        category_priority=priority,
        locked_category_count=int(
            categories.get("locked_count", LOCKED_CATEGORY_COUNT)
        ),
    )
