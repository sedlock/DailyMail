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
map, works out which announcements describe an event worth putting on the
reader's calendar, asks Claude to rank the day, renders one email containing the
**complete** text of every announcement, and sends it.

The email is the product: no navigation, no clicking through, no summaries. Each
announcement appears once, in full, with its event details, contact/submitter/
approver metadata, and a direct link to the official Rowan page for verification.

## Quick start

```sh
uv sync
uv run pytest                      # 1,211 tests, no network required

uv run dailymail run-daily         # the full pipeline for today
uv run dailymail status            # runs, deliveries, timer state
uv run dailymail health --json     # read-only controlpanel.status.v1 document
uv run dailymail status-snapshot refresh      # republish the status snapshot
uv run dailymail db-status         # database and category state
uv run dailymail parking-status    # the parking reference cache
uv run dailymail families          # logical repeat families
uv run dailymail sessions --date 2026-09-01   # detected calendar sittings
```

## Deployment and ControlPanel remediation

Development happens here. Production activation happens through ControlPanel.

`dailymail.timer` executes whatever immutable release ControlPanel currently
marks `current`, so a deployment is an activation, not a sync of this checkout:

```sh
/mnt/bench/releases/dailymail/current/.venv/bin/dailymail
```

When development is finished and validated, hand ControlPanel the exact commit:

```sh
git -C /mnt/bench/src/DailyMail rev-parse HEAD
controlpanel release build-activate --target dailymail \
    --commit <that-40-character-sha> --source dailymail-development
```

ControlPanel proves the SHA is a committed object in this repository, extracts
exactly that object with `git archive` — so nothing uncommitted or untracked can
reach production, whatever state the working tree is in — runs the registered
validation over that extraction in a no-network sandbox, builds the immutable
artifact, activates it, and verifies it. It records who asked, why, and whether
the commit is on GitHub main. `controlpanel release validate-commit` runs the
same gate on its own and changes nothing.

`./scripts/install.sh` remains for **bootstrap and development**: it creates the
local environment and writes the user units the timer needs. It is no longer the
production deployment path, and syncing this checkout does not change what
production runs.

```sh
cd /mnt/bench/src/DailyMail
./scripts/install.sh            # locked dependency sync + unit write/reload only
./scripts/install.sh --status   # health JSON + exact user-unit status, read-only
./scripts/install.sh --remove   # remove user units only; retain all data
```

`install.sh` has a fixed-path guard and runs only from
`/mnt/bench/src/DailyMail`; it accepts no repository, database, unit, or command
override. It preserves the SQLite history, collection artifacts, diagnostics,
configuration, and credentials. It does not invoke Rowan retrieval, Claude
curation, SMTP, or `dailymail.service`.
See `docs/operations.md` for the backup, rollback, and deliberate live
verification contract.

Preview without sending:

```sh
uv run dailymail run-daily --dry-run
uv run dailymail render --date 2026-08-21 --inline-images --out /tmp/preview
```

Sixteen further tests are opt-in, because they leave the machine or launch a
browser:

```sh
DAILYMAIL_LIVE_PARKING=1 uv run pytest tests/test_parking_live.py
DAILYMAIL_VISUAL_QA=1    uv run pytest tests/test_visual_qa.py
```

"No network required" is enforced, not intended. `tests/hermetic_boundary.py`
installs a CPython audit hook — which, unlike a fixture, has no uninstall — and
refuses any attempt to resolve Rowan's host, speak SMTP, read the real
credentials, touch the production database, state directory or run lock,
invoke systemd, or mutate the immutable release. Every run prints what it
reached, and a refused access fails the run. `docs/operations.md` §11 has the
rules, including why `monkeypatch.undo()` is forbidden in a test body.

Operational reference — commands, exit codes, failure behaviour, troubleshooting:
**`docs/operations.md`**. Parking geography, sources and cache:
**`docs/parking-enrichment.md`**. Calendar actions, travel reservation and
logical-repeat detection: **`docs/calendar-and-repeats.md`**. Durable repeat
families, multi-session calendars, the render colour boundary and browser QA:
**`docs/logical-families-and-sessions.md`**. Mobile prose alignment, the
NEW→STANDING transition and calendar relevance:
**`docs/mobile-rendering-and-relevance.md`**. Why Rowan's Extra Editions are a
confirmed coverage gap: **`docs/extra-editions.md`**. Why `health` publishes a
snapshot instead of reading a WAL database an observer cannot open:
**`docs/status-snapshot.md`**.

## How it works

```
06:30 ET  systemd timer
          -> collect      two JSON requests to Rowan's OutSystems screen service
          -> validate     nine gates; a failure means no digest, not a bad digest
          -> persist      SQLite history, versioned only on real content change
          -> repeats      has the reader already been sent this, under a new ID?
                          resolved against durable logical families
          -> events       which announcements are real, schedulable events,
                          and how many selectable sittings each one offers
          -> parking       cached lot geography; a hit is a dictionary lookup
          -> curate       Claude ranks and judges calendar relevance; fallback if it fails
          -> calendar     Outlook deep link + RFC 5545 .ics, with travel holds
          -> render       deterministic HTML + plain text from stored state
          -> send         Gmail STARTTLS, idempotent per date
```

Thirteen things are worth knowing:

* **The collector is deterministic and Claude is not in it.** Announcement data
  comes from the app's own JSON endpoint, not from browser scraping — Rowan's UI
  renders only the first 20 announcements behind a "Load More" button, so a DOM
  scraper silently loses data. Claude only ever decides *ordering*.
* **A stale API version returns HTTP 200 with an empty payload**, which looks
  exactly like a quiet news day. The collector detects it, rediscovers its
  tokens, retries once, then fails loudly. It never reports that as "no news".
* **New vs Standing comes from Rowan's own data** (`min(DistributionDates) ==
  today`), validated 13/13 against a real Rowan digest — not from when DailyMail
  happened to first see an announcement. One second question is asked on top of
  it: Rowan submitters routinely repost an announcement under a *new*
  SubmissionId rather than extending its distribution dates — nine announcements
  did so in the first production week — so something the reader was sent three
  days ago arrives labelled New. A conservative deterministic check demotes
  those to `STANDING`, while a genuinely new occurrence of a recurring event
  stays New. Rowan's own classification is kept, never overwritten. Once two
  SubmissionIds are known to be the same logical announcement they become a
  durable *family*, so a later repost resolves against the original however long
  ago it was delivered — and a morning on which the check cannot run no longer
  loses that knowledge, which is exactly what happened on 1 September 2026.
* **Curation output is accepted only as a permutation.** Reclassifying, dropping,
  inventing or duplicating an announcement is rejected, so announcement text that
  tries to instruct the model cannot change what gets delivered.
* **A Claude problem never costs you the email.** It falls back to a transparent
  deterministic ranking and delivers the complete digest anyway.
* **Inline images are real.** Rowan bodies embed `data:` URIs over 6 MB. Those are
  decoded, verified, downscaled and re-attached as CID parts within a byte budget;
  anything undecodable becomes a link to the source instead of a broken image.
* **A relevant event gets an `Add to Calendar` button.** Rowan's own `Event`
  boolean is *false* for the Provost's Town Hall, so detection reads the body:
  the date, the two-phase schedule, the room and the hybrid note are all there.
  Claude judges only *relevance* — riding along on the ranking call that already
  runs, for about a hundred extra tokens — while every date, time, location and
  link comes from deterministic extraction. The button is an Outlook compose
  deep link; beside it is a standards-compliant `.ics` that also reserves
  realistic travel either side, without ever altering the advertised event time.
  `docs/calendar-and-repeats.md`.
* **One announcement can offer a choice of sittings, and each one gets its own
  button.** The Provost's Coffee Hours advertises `Thursday, September 10 from
  11:00-12:30` and `Monday, September 21 from 2:30-4:00`. Spanning both as one
  appointment would be wrong and picking one silently would be worse, so a
  sitting is recognized only where the source itself pins one date to one time
  range — which is why `by Monday, September 7th. These sessions are limited to
  10-12 people` never becomes a third one. Relevance is still judged once for
  the series, so this costs no extra model call.
  `docs/logical-families-and-sessions.md`.
* **A status probe that cannot read the database still tells the truth.**
  `health` opened the live SQLite read-only — and a WAL database cannot be read
  at all without a `-shm` sidecar it must *create* in the database's directory,
  which ControlPanel's read-only collector cannot do. 6,786 of 6,801 collections
  returned a valid but blank document and every run was seen a day late. So
  DailyMail now publishes a bounded, redacted snapshot of its own committed
  state after each run and delivery transition, and `health` reads that when the
  database is unreadable — labelled as a snapshot, with its timestamp, and with
  the live database error still attached. `docs/status-snapshot.md`.
* **The digest owns its own alignment, as well as its own colours.** An
  announcement that justified every paragraph of its body rendered with rivers of
  whitespace down a 390px Outlook mobile pane, while the announcement above it —
  which carried no style at all — read normally. Normal prose is now pinned left
  in the render derivative, and `justify`, arbitrary `right`, `word-spacing` and
  `text-align-last` are dropped; a table cell's alignment, a figure's layout and
  compact centred content are deliberately kept. The stored source keeps every
  byte. `docs/mobile-rendering-and-relevance.md`.
* **You can see where today's news ends.** Every card was already labelled, but
  three screens into a forty-announcement scroll that was not enough. The
  crossing from New to Standing now carries a full-width barrier — gold rule,
  heavier label, a caption naming what follows — and every Standing card sits on
  a slightly warmer surface than a New one. Only the surface moves: same ink,
  same badges, same contrast, so continuing never reads as disabled.
* **A calendar button is decided by what an event *is*.** `Hollybush Tour` was
  offered because prose about Lyndon Johnson in 1967 contains the word
  *president*; the Wellness Center Open House was withheld because *free food*
  appears in its list of refreshments. Relevance now reads the title, the
  category, the audience and the announcement's own opening at full weight, the
  rest of the body at a quarter weight with a hard clamp, and the words that name
  *who* an event belongs to only from the title. Replayed over every
  deterministic decision ever recorded, two change and none regresses.
* **The digest owns its own colours.** An announcement that painted its body in
  a shade three points from the design's own accent rendered as one long
  headline in Outlook mobile's dark mode, because both inverted to the same
  peach. Source text colours are now dropped from the render derivative and
  DailyMail's are inlined; the stored source keeps every byte. Emphasis, lists,
  tables and links all still work. A Playwright suite asserts the computed
  colours at three viewports, and reuses the browser another project already
  installed rather than adding one.
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
  repeats.py                                                          logical repeats + families
  events.py travel.py calendar_action.py calendar_enrich.py           calendar actions + sessions
  curate.py                                                           Claude ranking + fallback
  atomic.py status_snapshot.py                                        status projection
  sanitize.py images.py render.py templates/                          the email
  mailer.py                                                           MIME + SMTP
  parking*.py                                                         parking enrichment
  daily.py maintenance.py systemd_units.py cli.py                     orchestration + ops
docs/
  site-reconnaissance.md    Phase 0: how the source was reverse-engineered
  collector-architecture.md Phase 1: the collector and its validation gates
  operations.md             Phase 2: running it
  parking-enrichment.md     Phase 3: parking geography, sources and cache
  calendar-and-repeats.md   Phase 4: calendar actions, travel, logical repeats
  logical-families-and-sessions.md
                            Phase 5: durable repeat families, multi-session
                            calendars, render colour policy, browser QA
  mobile-rendering-and-relevance.md
                            Phase 6: prose alignment, the NEW->STANDING
                            transition, calendar relevance, curation timeout
  extra-editions.md         Phase 6: the confirmed Extra Edition coverage gap
  status-snapshot.md        Phase 7: the WAL status probe, and the bounded
                            snapshot health reads when the database cannot be
artifacts/reconnaissance/   sanitized fixtures the test suite runs against
artifacts/parking/          snapshots of Rowan's authoritative parking sources
artifacts/qa/fixtures/      the 1 and 9 September rendering regression cases
tools/recon/                Phase 0 probes, manual diagnostics only
tools/qa/                   browser rendering QA, diagnostics only
tests/                      1,211 tests, fixture- and mock-driven
```

## Configuration

`~/.config/dailymail/config.toml` — recipient, timezone, send time, Claude model,
image budgets, retention, parking refresh policy, calendar relevance threshold
and travel policy, and the category priority order (first five locked).

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
