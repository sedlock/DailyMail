"""Backups and retention.

The database is backed up with SQLite's own backup API, never by copying a live
WAL database out from under itself.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config as collector_config
from . import db
from .settings import Settings, data_dir


def backups_dir() -> Path:
    return collector_config.state_dir() / "backups"


def diagnostics_dir() -> Path:
    return collector_config.state_dir() / "diagnostics"


def backup_database(connection: sqlite3.Connection, *, stamp: str | None = None) -> Path:
    """Consistent online backup via `Connection.backup()`."""
    target_dir = backups_dir()
    target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    label = stamp or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = target_dir / f"dailymail-{label}.sqlite3"

    with sqlite3.connect(str(destination)) as backup_connection:
        connection.backup(backup_connection)
    destination.chmod(0o600)
    return destination


def prune_backups(keep: int) -> list[Path]:
    existing = sorted(backups_dir().glob("dailymail-*.sqlite3"))
    removed = []
    for path in existing[: max(0, len(existing) - max(1, keep))]:
        try:
            path.unlink()
            removed.append(path)
        except OSError:
            pass
    return removed


def _prune_by_age(directory: Path, days: int, pattern: str = "*") -> list[Path]:
    if not directory.is_dir():
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, days))).timestamp()
    removed = []
    for path in directory.glob(pattern):
        if not path.is_file():
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            pass
    return removed


def prune_diagnostics(days: int) -> list[Path]:
    return _prune_by_age(diagnostics_dir(), days)


def prune_collection_artifacts(days: int, *, keep_minimum: int = 3) -> list[Path]:
    """Trim imported Phase 1 artifacts to a recent diagnostic window.

    Never drops below `keep_minimum` files, so there is always something recent
    to inspect even after a long quiet period.
    """
    directory = collector_config.collections_dir()
    if not directory.is_dir():
        return []
    artifacts = sorted(directory.glob("*.json"))
    if len(artifacts) <= keep_minimum:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(0, days))).timestamp()
    removed = []
    # Oldest first, stopping so `keep_minimum` newest always survive.
    for path in artifacts[: len(artifacts) - keep_minimum]:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                removed.append(path)
        except OSError:
            pass
    return removed


def save_diagnostic(name: str, content: bytes | str) -> Path:
    """Persist a rendered digest or failure detail for later inspection."""
    directory = diagnostics_dir()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = directory / name
    mode = "wb" if isinstance(content, bytes) else "w"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf-8")
    path.chmod(0o600)
    return path


def run_maintenance(connection: sqlite3.Connection, settings: Settings) -> dict:
    """Post-run housekeeping. Never fatal to a successful digest."""
    result: dict = {}
    try:
        backup = backup_database(connection)
        result["backup"] = str(backup)
        result["backup_bytes"] = backup.stat().st_size
    except (sqlite3.Error, OSError) as exc:
        result["backup_error"] = f"{type(exc).__name__}: {exc}"

    result["backups_pruned"] = len(prune_backups(settings.backups_keep))
    result["diagnostics_pruned"] = len(prune_diagnostics(settings.diagnostics_days))
    result["artifacts_pruned"] = len(
        prune_collection_artifacts(settings.collection_artifacts_days)
    )
    result["database_bytes"] = (
        db.database_path().stat().st_size if db.database_path().exists() else 0
    )
    return result
