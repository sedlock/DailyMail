"""The daily production pipeline.

    lock -> collect -> validate -> persist -> detect changes -> curate
         -> render -> send -> record -> maintain

Failure policy, in one place:
  * transient collection failures are retried on a modest schedule
  * a structural/validation failure is NOT retried and never yields a digest
  * if no validated dataset can be obtained, an operator alert is emailed
  * a Claude failure degrades to deterministic ordering and still delivers
  * a render failure sends nothing at all -- never a partial digest
  * an SMTP failure is recorded locally and exits non-zero; it does not try to
    report itself through the channel that just failed
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from . import collect as collector
from . import config as collector_config
from . import (
    credentials,
    curate,
    db,
    ingest,
    mailer,
    maintenance,
    parking_enrich,
    render,
)
from .errors import (
    DailyMailError,
    TlsError,
    TransportError,
    ValidationError,
    VersionChangedError,
)
from .settings import Settings

log = logging.getLogger("dailymail")

LOCK_NAME = "dailymail.lock"


class LockHeld(DailyMailError):
    exit_code = 10


class RenderFailure(DailyMailError):
    exit_code = 11


@dataclass
class RunResult:
    target_date: str
    status: str
    counts: dict = field(default_factory=dict)
    curation_method: str | None = None
    curation_model: str | None = None
    curation_seconds: float | None = None
    curation_error: str | None = None
    email_status: str = "not_attempted"
    message_id: str | None = None
    message_bytes: int | None = None
    image_stats: dict = field(default_factory=dict)
    subject: str | None = None
    collection_attempts: int = 0
    error: str | None = None
    maintenance: dict = field(default_factory=dict)
    delivery_id: int | None = None
    parking: dict = field(default_factory=dict)


class RunLock:
    """Advisory lock so two runs cannot overlap (timer plus a manual run)."""

    def __init__(self, path: Path | None = None) -> None:
        directory = collector_config.state_dir()
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = path or directory / LOCK_NAME
        self._handle = None

    def __enter__(self):
        self._handle = open(self.path, "w")
        try:
            fcntl.flock(self._handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            self._handle.close()
            self._handle = None
            raise LockHeld(
                f"another DailyMail run holds {self.path}; not starting a second one"
            ) from exc
        self._handle.write(f"{os.getpid()}\n")
        self._handle.flush()
        return self

    def __exit__(self, *exc_info):
        if self._handle is not None:
            fcntl.flock(self._handle, fcntl.LOCK_UN)
            self._handle.close()
            self._handle = None
        return False


def today_local(settings: Settings) -> str:
    return datetime.now(ZoneInfo(settings.timezone)).strftime("%Y-%m-%d")


# --- collection with retry ---------------------------------------------------

# Retried: network/TLS/transport blips and a stale apiVersion.
# Not retried: validation failures, which mean the data itself is wrong.
TRANSIENT = (TransportError, TlsError, VersionChangedError)


def collect_with_retry(
    target_date: str, settings: Settings, *, sleeper=time.sleep
) -> tuple[dict, int]:
    """Collect a validated dataset, retrying transient failures only."""
    delays = list(settings.retry_delays_seconds)
    attempts = 0
    last: Exception | None = None

    for index in range(len(delays) + 1):
        attempts += 1
        try:
            artifact = collector.run_collection(
                target_date=target_date, page_size=settings.page_size
            )
            collector.write_artifact(artifact)
            return artifact, attempts
        except TRANSIENT as exc:
            last = exc
            log.warning(
                "collection attempt %d failed (transient): %s", attempts, exc
            )
            if index < len(delays):
                log.info("retrying collection in %ds", delays[index])
                sleeper(delays[index])
        except ValidationError as exc:
            # Structural/data problem: retrying will not fix it.
            log.error("collection validation failed, not retrying: %s", exc)
            raise

    raise TransportError(
        f"collection failed after {attempts} attempt(s): "
        f"{type(last).__name__}: {last}"
    )


# --- pipeline ----------------------------------------------------------------


def run_daily(
    *,
    target_date: str | None = None,
    settings: Settings,
    force_resend: bool = False,
    dry_run: bool = False,
    trigger: str = "manual",
    sleeper=time.sleep,
) -> RunResult:
    """Execute one complete production run."""
    date_value = target_date or today_local(settings)
    result = RunResult(target_date=date_value, status="running")

    with RunLock():
        connection = db.connect()
        db.initialize(connection)
        run_id = db.start_run(connection, date_value, trigger)

        try:
            # 1-2. Collect and validate.
            try:
                artifact, attempts = collect_with_retry(
                    date_value, settings, sleeper=sleeper
                )
            except (ValidationError, TransportError, TlsError, VersionChangedError) as exc:
                result.collection_attempts = getattr(exc, "attempts", 0) or 1
                result.status = "failed"
                result.error = f"{type(exc).__name__}: {exc}"
                db.finish_run(
                    connection,
                    run_id,
                    status="failed",
                    collector_validation="failed",
                    email_status="alert",
                    error_summary=result.error[:500],
                )
                _try_alert(settings, date_value, type(exc).__name__, str(exc), result)
                return result

            result.collection_attempts = attempts

            # 3-5. Persist; version history and change detection happen here.
            summary = ingest.ingest_artifact(
                connection, artifact, settings, origin=f"collection {date_value}"
            )
            counts = db.counts_for_date(connection, date_value)
            result.counts = counts
            log.info(
                "persisted %s: unique=%d new=%d standing=%d changed=%d",
                date_value, counts["unique"], counts["new"],
                counts["standing"], counts["changed"],
            )

            rows = db.digest_rows(connection, date_value)

            # 6. Parking enrichment. Additive, non-critical, and cheap: a cached
            # lot is a dictionary lookup. Runs after persistence and before
            # rendering, and cannot fail the run -- `enrich_digest` never raises.
            parking_callouts, parking_metrics = parking_enrich.enrich_digest(
                connection, rows, target_date=date_value, settings=settings
            )
            result.parking = parking_metrics.as_dict()
            if parking_metrics.mentions_detected:
                log.info(
                    "parking: %d mention(s), %d cache hit(s), %d miss(es), "
                    "%d resolver call(s), %d unresolved, %.3fs",
                    parking_metrics.mentions_detected, parking_metrics.cache_hits,
                    parking_metrics.cache_misses, parking_metrics.resolver_calls,
                    parking_metrics.unresolved, parking_metrics.duration_seconds,
                )
            for problem in parking_metrics.errors:
                # Recorded, never escalated: one unresolvable lot is not an
                # operator alert.
                log.warning("parking enrichment problem: %s", problem)

            # 7. Curate (Claude, or deterministic fallback).
            outcome = curate.curate(rows, date_value, settings)
            result.curation_method = outcome.method
            result.curation_model = outcome.model
            result.curation_seconds = outcome.duration_seconds
            result.curation_error = outcome.error
            if outcome.error:
                log.warning("curation fell back to deterministic ordering: %s", outcome.error)

            if outcome.inferred_categories:
                _persist_inferred_categories(connection, outcome.inferred_categories)
                rows = db.digest_rows(connection, date_value)

            db.save_curation(
                connection,
                date_value,
                [
                    {
                        "submission_id": entry["submission_id"],
                        "section": entry["section"],
                        "final_rank": entry["model_rank"],
                        "relevance_score": entry["relevance_score"],
                        "urgency_score": entry["urgency_score"],
                        "rationale": entry["rationale"],
                    }
                    for entry in outcome.entries
                ],
                method=outcome.method,
                model=outcome.model,
            )

            # 8. Render deterministically from stored state.
            ordering = {entry["submission_id"]: entry for entry in outcome.entries}
            try:
                digest = render.render_digest(
                    rows,
                    target_date=date_value,
                    counts=counts,
                    ordering=ordering,
                    curation_method=outcome.method,
                    settings=settings,
                    parking=parking_callouts,
                )
                _verify_digest(digest, rows)
            except Exception as exc:
                result.status = "failed"
                result.error = f"render failure: {type(exc).__name__}: {exc}"
                db.finish_run(
                    connection, run_id, status="failed",
                    collector_validation="ok", curation_method=outcome.method,
                    email_status="not_attempted",
                    error_summary=result.error[:500],
                    unique_count=counts["unique"], new_count=counts["new"],
                    standing_count=counts["standing"], changed_count=counts["changed"],
                    employee_count=counts["employee_view"],
                    student_count=counts["student_view"],
                    parking_stats=json.dumps(result.parking),
                )
                log.error("render failed; nothing sent: %s", exc)
                _try_alert(settings, date_value, "RenderFailure", str(exc), result)
                raise RenderFailure(result.error) from exc

            result.image_stats = digest.image_stats
            result.subject = digest.subject

            # 9-11. Send, unless already delivered or explicitly a dry run.
            existing = db.successful_delivery(connection, date_value, settings.recipient)
            if existing is not None and not force_resend:
                result.email_status = "skipped_duplicate"
                result.message_id = existing["message_id"]
                result.message_bytes = existing["message_bytes"]
                log.info(
                    "digest for %s already delivered to %s (delivery %s); not resending",
                    date_value, settings.recipient, existing["delivery_id"],
                )
                if existing["content_hash"] != digest.content_hash:
                    log.info(
                        "content has changed since that delivery; recorded for the "
                        "next normal run rather than resending automatically"
                    )
            elif dry_run:
                result.email_status = "dry_run"
                maintenance.save_diagnostic(f"{date_value}-preview.html", digest.html)
                maintenance.save_diagnostic(f"{date_value}-preview.txt", digest.text)
                log.info("dry run: rendered but not sent")
            else:
                sender = mailer.sender_address()
                prepared = mailer.build_message(
                    settings=settings,
                    sender=sender,
                    subject=digest.subject,
                    html=digest.html,
                    text=digest.text,
                    images=digest.images,
                )
                result.message_bytes = prepared.size_bytes
                result.message_id = prepared.message_id
                _verify_message(prepared, digest)
                try:
                    status = mailer.send(prepared, settings)
                except DailyMailError as exc:
                    result.email_status = "failed"
                    result.error = str(exc)
                    db.record_delivery(
                        connection,
                        target_date=date_value,
                        recipient=settings.recipient,
                        content_hash_value=digest.content_hash,
                        state="failed",
                        message_id=prepared.message_id,
                        smtp_status=None,
                        message_bytes=prepared.size_bytes,
                        image_count=prepared.image_count,
                        forced=force_resend,
                        error_summary=str(exc)[:500],
                    )
                    db.finish_run(
                        connection, run_id, status="failed",
                        collector_validation="ok",
                        curation_method=outcome.method, email_status="failed",
                        error_summary=str(exc)[:500],
                        unique_count=counts["unique"], new_count=counts["new"],
                        standing_count=counts["standing"],
                        changed_count=counts["changed"],
                        employee_count=counts["employee_view"],
                        student_count=counts["student_view"],
                        parking_stats=json.dumps(result.parking),
                    )
                    result.status = "failed"
                    # Deliberately no alert email: the mail channel just failed.
                    log.error("SMTP delivery failed: %s", exc)
                    raise

                result.email_status = "sent"
                result.delivery_id = db.record_delivery(
                    connection,
                    target_date=date_value,
                    recipient=settings.recipient,
                    content_hash_value=digest.content_hash,
                    state="sent",
                    message_id=prepared.message_id,
                    smtp_status=status,
                    message_bytes=prepared.size_bytes,
                    image_count=prepared.image_count,
                    forced=force_resend,
                    sent_at=db.now_utc(),
                )
                maintenance.save_diagnostic(
                    f"{date_value}-sent.eml", prepared.message.as_bytes()
                )
                log.info(
                    "sent %s to %s (%d bytes, %d image(s))",
                    digest.subject, settings.recipient,
                    prepared.size_bytes, prepared.image_count,
                )

            # 12. Housekeeping.
            result.maintenance = maintenance.run_maintenance(connection, settings)
            result.status = "success"
            db.finish_run(
                connection, run_id,
                status="success",
                collector_validation="ok",
                curation_method=outcome.method,
                email_status=result.email_status,
                unique_count=counts["unique"],
                new_count=counts["new"],
                standing_count=counts["standing"],
                changed_count=counts["changed"],
                employee_count=counts["employee_view"],
                student_count=counts["student_view"],
                error_summary=outcome.error[:500] if outcome.error else None,
                parking_stats=json.dumps(result.parking),
            )
            return result
        finally:
            connection.close()


def _persist_inferred_categories(connection, inferred: dict[str, int]) -> None:
    """Store curation's estimated slot for categories absent from config."""
    with db.transaction(connection):
        for title, priority in inferred.items():
            row = connection.execute(
                "SELECT category_id FROM categories WHERE title = ? "
                "AND manual_priority IS NULL",
                (title,),
            ).fetchone()
            if row:
                db.set_inferred_priority(connection, row["category_id"], priority)
                log.info(
                    "recorded inferred priority %d for new category %r",
                    priority, title,
                )


def _verify_digest(digest: render.RenderedDigest, rows) -> None:
    """Refuse to send anything that is not a faithful, complete rendering."""
    expected = {str(row["submission_id"]) for row in rows}
    rendered = digest.submission_ids
    if len(rendered) != len(set(rendered)):
        raise RenderFailure("an announcement was rendered more than once")
    if set(rendered) != expected:
        missing = sorted(expected - set(rendered))
        extra = sorted(set(rendered) - expected)
        raise RenderFailure(
            f"rendered set does not match stored set (missing={missing}, extra={extra})"
        )
    for submission_id in expected:
        url = db.official_url(submission_id)
        if url not in digest.html or url not in digest.text:
            raise RenderFailure(f"submission {submission_id} is missing its official link")
    for banned in ("data:image", "<script", "javascript:", "file://"):
        if banned in digest.html:
            raise RenderFailure(f"rendered HTML contains {banned!r}")


def _verify_message(prepared: mailer.PreparedMessage, digest: render.RenderedDigest) -> None:
    """Last gate before the wire."""
    raw = prepared.message.as_bytes()
    creds = credentials.load()
    if creds.password.encode() in raw:
        raise RenderFailure("refusing to send: credential material found in the message")
    if b"GMAIL_APP_PASSWORD" in raw:
        raise RenderFailure("refusing to send: credential variable name in the message")
    if prepared.size_bytes > 20 * 1024 * 1024:
        raise RenderFailure(
            f"message is {prepared.size_bytes} bytes, which is unreasonably large"
        )
    if not prepared.subject or not prepared.subject.startswith("Curated Rowan Daily Mail - "):
        raise RenderFailure(f"unexpected subject {prepared.subject!r}")


def _try_alert(
    settings: Settings, target_date: str, failure_class: str, detail: str, result: RunResult
) -> None:
    """Best-effort operator alert. Never masks the original failure."""
    try:
        sender = mailer.sender_address()
        alert = mailer.build_alert_message(
            settings=settings,
            sender=sender,
            target_date=target_date,
            failure_class=failure_class,
            detail=detail[:800],
            attempts=result.collection_attempts,
        )
        mailer.send(alert, settings)
        result.email_status = "alert_sent"
        log.info("sent operator alert for %s", target_date)
    except Exception as exc:
        result.email_status = "alert_failed"
        log.error("could not send operator alert: %s", exc)
