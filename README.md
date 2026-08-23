# DailyMail

A curated daily digest of [Rowan Announcer](https://apps.rowan.edu/RowanAnnouncer/)
announcements, delivered as one information-dense, Outlook-compatible email.

**Status: in production.** A systemd user timer runs the pipeline every day at
06:30 America/New_York.

## What it does

Every morning DailyMail collects every announcement valid for that date from both
the Employee and Student views, validates completeness against the source's own
counts, records history in SQLite, works out what is new and what has
substantively changed, resolves any parking lot it mentions to a real place on a
map, asks Claude to rank the day, renders one email containing the **complete**
text of every announcement, and sends it.

The email is the product: no navigation, no clicking through, no summaries. Each
announcement appears once, in full, with its event details, contact/submitter/
approver metadata, and a direct link to the official Rowan page for verification.

## Quick start

```sh
uv sync
uv run pytest                      # 632 tests, no network required

uv run dailymail run-daily         # the full pipeline for today
uv run dailymail status            # runs, deliveries, timer state
uv run dailymail db-status         # database and category state
uv run dailymail parking-status    # the parking reference cache
```

Preview without sending:

```sh
uv run dailymail run-daily --dry-run
uv run dailymail render --date 2026-08-21 --inline-images --out /tmp/preview
```

Eleven further tests probe Rowan's live parking sources and are opt-in:

```sh
DAILYMAIL_LIVE_PARKING=1 uv run pytest tests/test_parking_live.py
```

Operational reference — commands, exit codes, failure behaviour, troubleshooting:
**`docs/operations.md`**. Parking geography, sources and cache:
**`docs/parking-enrichment.md`**.

## How it works

```
06:30 ET  systemd timer
          -> collect      two JSON requests to Rowan's OutSystems screen service
          -> validate     nine gates; a failure means no digest, not a bad digest
          -> persist      SQLite history, versioned only on real content change
          -> parking       cached lot geography; a hit is a dictionary lookup
          -> curate       Claude ranks; deterministic fallback if anything fails
          -> render       deterministic HTML + plain text from stored state
          -> send         Gmail STARTTLS, idempotent per date
```

Seven things are worth knowing:

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
* **"Parking Lot O-1 will be closed" now tells you where that is.** Rowan's own
  Glassboro campus map is a Google My Maps layer, and My Maps publishes it as KML
  — so the lot names, coordinates and permit classes are official machine-readable
  data, cached once in SQLite. A parking announcement gets one compact block with
  a plain-English location and a Google Maps link built from stored coordinates.
  A cached lot costs no network call and no model call; `Lot A` exists on two
  campuses, so without campus evidence it shows the official map rather than
  guessing. `docs/parking-enrichment.md`.

## Repository layout

```
src/dailymail/
  collect.py client.py discovery.py validate.py normalize.py tls.py   Phase 1 collector
  db.py ingest.py                                                     history + versioning
  curate.py                                                           Claude ranking + fallback
  sanitize.py images.py render.py templates/                          the email
  mailer.py                                                           MIME + SMTP
  parking*.py                                                         parking enrichment
  daily.py maintenance.py systemd_units.py cli.py                     orchestration + ops
docs/
  site-reconnaissance.md    Phase 0: how the source was reverse-engineered
  collector-architecture.md Phase 1: the collector and its validation gates
  operations.md             Phase 2: running it
  parking-enrichment.md     Phase 3: parking geography, sources and cache
artifacts/reconnaissance/   sanitized fixtures the test suite runs against
artifacts/parking/          snapshots of Rowan's authoritative parking sources
tools/recon/                Phase 0 probes, manual diagnostics only
tests/                      632 tests, fixture- and mock-driven
```

## Configuration

`~/.config/dailymail/config.toml` — recipient, timezone, send time, Claude model,
image budgets, retention, parking refresh policy, and the category priority order
(first five locked).

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
