"""Constants and paths. Everything deployment-specific is discovered at runtime."""

from __future__ import annotations

import os
from pathlib import Path

BASE_URL = "https://apps.rowan.edu/RowanAnnouncer"

GET_HOME_DATA_PATH = "screenservices/RowanAnnouncer/MainFlow/Home/ActionGetHomeData"
MODULE_INFO_PATH = "moduleservices/moduleinfo"
HOME_MVC_ASSET = "RowanAnnouncer.MainFlow.Home.mvc.js"
OUTSYSTEMS_JS_ASSET = "OutSystems.js"

VIEW_NAME = "MainFlow.Home"

# Rowan's local timezone. DistributionDates are bare dates, so "today" must be
# resolved here rather than in UTC (Phase 0 §13 item 5).
ROWAN_TIMEZONE = "America/New_York"

# `EndDate` sentinel that selects single-day mode (Phase 0 §4.1).
SINGLE_DAY_END_DATE_SENTINEL = "1900-01-01"

# The closed request enum. Rowan silently returns only the "Both" subset for any
# other value (Phase 0 §12.3), so this must never be widened at runtime.
REQUEST_AUDIENCES = ("Employees", "Students")

# Values Rowan uses to mean "not set" (Phase 0 §7.3).
DATE_SENTINELS = frozenset({"1900-01-01", "1900-01-01T00:00:00", "1900-01-01T00:00:00Z"})
TIME_SENTINEL = "00:00:00"

SOURCE_AUDIENCES = ("Employees", "Students", "Both")

AUDIENCE_LABELS = {
    "Employees": "Employee",
    "Students": "Student",
    "Both": "Everyone",
}

PAGE_SIZE = 100

# Backstop against a pathological pagination loop. At PAGE_SIZE=100 this allows
# 20,000 records for one day; the whole 2020-2030 archive was 5,125.
MAX_PAGES = 200

HTTP_TIMEOUT_SECONDS = 60.0

USER_AGENT = (
    "DailyMail/0.1 (Rowan Announcer digest; +contact: sedlock@rowan.edu) "
    "python-httpx"
)


def state_dir() -> Path:
    """XDG state directory for collection output."""
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "dailymail"


def collections_dir() -> Path:
    return state_dir() / "collections"


def collection_path(target_date: str) -> Path:
    return collections_dir() / f"{target_date}.json"
