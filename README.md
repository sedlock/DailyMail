# DailyMail

A curated daily digest of [Rowan Announcer](https://apps.rowan.edu/RowanAnnouncer/)
announcements, delivered as one information-dense, Outlook-compatible email.

**Status: in production.** A systemd user timer runs the pipeline every day at
07:00 America/New_York.

## What it does

Every morning DailyMail collects every announcement valid for that date from both
the Employee and Student views, validates completeness against the source's own
counts, records history in SQLite, works out what is new and what has
substantively changed, asks Claude to rank the day, renders one email containing
the **complete** text of every announcement, and sends it.

The email is the product: no navigation, no clicking through, no summaries. Each
announcement appears once, in full, with its event details, contact/submitter/
approver metadata, and a direct link to the official Rowan page for verification.

## Quick start

```sh
uv sync
uv run pytest                      # 399 tests, no network required

uv run dailymail run-daily         # the full pipeline for today
uv run dailymail status            # runs, deliveries, timer state
uv run dailymail db-status         # database and category state
```

Preview without sending:

```sh
uv run dailymail run-daily --dry-run
uv run dailymail render --date 2026-08-21 --inline-images --out /tmp/preview
```

Operational reference — commands, exit codes, failure behaviour, troubleshooting:
**`docs/operations.md`**.

## How it works

```
07:00 ET  systemd timer
          -> collect      two JSON requests to Rowan's OutSystems screen service
          -> validate     nine gates; a failure means no digest, not a bad digest
          -> persist      SQLite history, versioned only on real content change
          -> curate       Claude ranks; deterministic fallback if anything fails
          -> render       deterministic HTML + plain text from stored state
          -> send         Gmail STARTTLS, idempotent per date
```

Six things are worth knowing:

* **The collector is deterministic and Claude is not in it.** Announcement data
  comes from the app's own JSON endpoint, not from browser scraping — Rowan's UI
  renders only the first 20 announcements behind a "Load More" button, so a DOM
  scraper silently loses data. Claude only ever decides *ordering*.
* **A stale API version returns HTTP 200 with an empty payload**, which looks
  exactly like a quiet news day. The collector detects it, rediscovers its
  tokens, retries once, then fails loudly. It never reports that as "no news".
* **New vs Standing comes from Rowan's own data** (`min(DistributionDates) ==
  today`), validated 13/13 against a real Rowan digest — not from when DailyMail
  happened to first see an announcement.
* **Curation output is accepted only as a permutation.** Reclassifying, dropping,
  inventing or duplicating an announcement is rejected, so announcement text that
  tries to instruct the model cannot change what gets delivered.
* **A Claude problem never costs you the email.** It falls back to a transparent
  deterministic ranking and delivers the complete digest anyway.
* **Inline images are real.** Rowan bodies embed `data:` URIs over 6 MB. Those are
  decoded, verified, downscaled and re-attached as CID parts within a byte budget;
  anything undecodable becomes a link to the source instead of a broken image.

## Repository layout

```
src/dailymail/
  collect.py client.py discovery.py validate.py normalize.py tls.py   Phase 1 collector
  db.py ingest.py                                                     history + versioning
  curate.py                                                           Claude ranking + fallback
  sanitize.py images.py render.py templates/                          the email
  mailer.py                                                           MIME + SMTP
  daily.py maintenance.py systemd_units.py cli.py                     orchestration + ops
docs/
  site-reconnaissance.md    Phase 0: how the source was reverse-engineered
  collector-architecture.md Phase 1: the collector and its validation gates
  operations.md             Phase 2: running it
artifacts/reconnaissance/   sanitized fixtures the test suite runs against
tools/recon/                Phase 0 probes, manual diagnostics only
tests/                      399 tests, fixture- and mock-driven
```

## Configuration

`~/.config/dailymail/config.toml` — recipient, timezone, send time, Claude model,
image budgets, retention, and the category priority order (first five locked).

`~/.config/dailymail/credentials.env` (mode 0600) — `GMAIL_SMTP_USER` and
`GMAIL_APP_PASSWORD`. Read only inside the sending process; never logged, never
in a systemd unit, never sent to Claude, never committed.

## Scope discipline

This touches a live university system and stays read-only by construction: one
request shape, no announcement detail pages (which would trigger Rowan's own
visitor-log write), and none of the write endpoints catalogued in
`docs/site-reconnaissance.md` §4.3.

Dependencies are deliberately few: `httpx`, `certifi`, `jinja2`, `pillow`, `nh3`.
No ORM, no migration framework, no containers, no web server, no queue.
