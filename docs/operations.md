# DailyMail Operations (Phase 2 — production)

Companion to `docs/site-reconnaissance.md` (Phase 0),
`docs/collector-architecture.md` (Phase 1), `docs/parking-enrichment.md`
(Phase 3) and `docs/calendar-and-repeats.md` (Phase 4), all of which remain
accurate. This document covers the production
system: history, curation, rendering, delivery and scheduling.

---

## 1. Daily pipeline

`dailymail.timer` fires `dailymail.service` at **06:30 America/New_York**, which
runs `uv run --frozen dailymail run-daily --trigger timer`:

```
overlap lock
  -> collect (Phase 1 collector, unchanged)   retry: now, +5 min, +20 min
  -> validate (Phase 1 gates V1-V9, V12)      structural failure = no retry
  -> persist to SQLite                        versions only on real change
  -> detect substantive updates
  -> logical repeats                          already delivered under another ID?
  -> event candidates                         which announcements are real events
  -> parking enrichment                       cache hit = a dictionary lookup
  -> curate (Claude; deterministic fallback on any failure)
       ranking, plus calendar relevance on the same call
  -> calendar enrichment                      Outlook link + .ics + travel holds
  -> render HTML + plain text deterministically
  -> verify the rendering, then the message
  -> send via Gmail STARTTLS (idempotent per date+recipient)
  -> record delivery
  -> backup + retention
```

Typical wall clock is ~95 s, almost all of it the Claude call. Collection itself
is two HTTP requests and under two seconds.

### What each failure does

| Failure | Behaviour |
|---|---|
| Transient collection (network, TLS, stale `apiVersion`) | Retried at +5 min then +20 min |
| Collection validation (count/audience/date/identity) | **Not** retried — the data is wrong, not the connection |
| No validated dataset after retries | No digest. Operator alert emailed: `DailyMail ATTENTION REQUIRED - <date>` |
| Claude unavailable, slow, quota-limited, or returns unusable output | Deterministic fallback ordering; the complete digest still goes out; `curation_method='fallback'` recorded |
| Render failure or an incomplete rendering | **Nothing sent.** Recorded, alert emailed |
| SMTP failure | Retried up to 3 times, then recorded and exit non-zero. Deliberately **no** alert email — that is the channel that just failed |
| Already delivered today | Exits cleanly without resending |
| Parking source unreachable, resolver failure, or an unresolvable lot | Recorded in `runs.parking_stats`; the announcement shows the official campus parking map instead. **No alert**, and the digest is unaffected |
| Event parse failure, invalid model relevance output, unresolvable venue, route lookup failure, or calendar-link generation failure | Recorded in `runs.calendar_stats` and `calendar_recommendations`. At worst one announcement loses its calendar button; a routing failure only makes the travel estimate rougher. **No alert**, and the digest is unaffected |
| Logical-repeat detection fails | The day is left exactly as Rowan classified it. **No alert** |

---

## 2. Commands

```sh
cd /mnt/bench/src/DailyMail

# production
uv run dailymail run-daily                       # today, Eastern
uv run dailymail run-daily --date 2026-08-20
uv run dailymail run-daily --dry-run             # render + validate, send nothing
uv run dailymail run-daily --force-resend        # deliberate duplicate send

# inspection
uv run dailymail status                          # runs, deliveries, timer
uv run dailymail health --json                   # read-only controlpanel.status.v1 JSON
uv run dailymail status --json                   # compatibility alias for the same JSON
uv run dailymail db-status                       # schema, counts, categories, backups
uv run dailymail render --date 2026-08-20 --inline-images --out /tmp/preview
uv run dailymail render --date 2026-08-20 --no-calendar   # skip calendar enrichment
uv run dailymail inspect --date 2026-08-20       # the collection artifact
uv run dailymail collect --date 2026-08-20       # collect only, no DB or email

# send a date that is already collected
uv run dailymail send --date 2026-08-20
uv run dailymail send --date 2026-08-20 --force-resend

# setup / scheduling
uv run dailymail db-init                         # create schema, import artifacts
uv run dailymail install-timer                   # write + enable units
uv run dailymail install-timer --print-only      # show the units

# parking reference data (docs/parking-enrichment.md)
uv run dailymail parking-status                  # cache, campuses, sources, misses
uv run dailymail parking-status --list
uv run dailymail parking-lookup "Lot O-1"        # one record, with its map URL
uv run dailymail parking-refresh                 # re-check authoritative sources
uv run dailymail parking-refresh --campus glassboro
uv run dailymail parking-set glassboro:lot:o-1 --description "..." \
    --latitude 39.712482 --longitude -75.120453  # pinned manual correction

# logical repeat families and calendar sessions
# (docs/logical-families-and-sessions.md)
uv run dailymail families                        # every durable family
uv run dailymail families --submission 6769      # one ID's family and evidence
uv run dailymail families --recheck 2026-09-01   # re-resolve one date, read-only
uv run dailymail families --recheck 2026-09-01 --apply    # persist + audit
uv run dailymail sessions --date 2026-09-01      # detected calendar sittings
uv run dailymail sessions --date 2026-09-01 --submission 6702 --ics

# tests (§11 "The hermetic test boundary" for what the normal run may touch)
uv run pytest                                    # hermetic, no network
DAILYMAIL_LIVE_PARKING=1 uv run pytest tests/test_parking_live.py
DAILYMAIL_VISUAL_QA=1 uv run pytest tests/test_visual_qa.py

# browser rendering QA (diagnostics only, never on the production path)
uv run python tools/qa/build_page.py
node tools/qa/visual-regression.mjs
```

### Logs and scheduling

```sh
journalctl --user -u dailymail.service -n 200 --no-pager
journalctl --user -u dailymail.service --since today
systemctl --user list-timers dailymail.timer --all

systemctl --user start dailymail.service         # run now, through systemd
systemctl --user disable --now dailymail.timer   # stop scheduling
systemctl --user enable --now dailymail.timer    # resume scheduling
```

### Deployment, remediation and rollback contract

For a DailyMail code deployment or a ControlPanel-approved remediation, run the
checked-in installer from the canonical checkout:

```sh
cd /mnt/bench/src/DailyMail
./scripts/install.sh
```

The installer has one fixed repository path:
`/mnt/bench/src/DailyMail`. Its guard rejects worktrees, copied scripts and
other paths. It performs `uv sync --frozen` and delegates exact unit generation
and daemon reload to `dailymail install-timer --no-enable`. It intentionally does
not start or newly enable the persistent timer: a missed persistent timer can
run immediately and contact Rowan or send email. Existing enabled timers remain
enabled after the unit reload. A newly installed timer can be explicitly enabled
only after the operator accepts that normal schedule semantics apply:

```sh
systemctl --user enable --now dailymail.timer
```

The inspection command is read-only at the application level and does not sync
the environment:

```sh
./scripts/install.sh --status
```

It runs exactly `dailymail health --json` through the installed environment,
then `systemctl --user status dailymail.service dailymail.timer --no-pager
--full`. It never creates a database/configuration, retrieves Rowan data, or
sends email. Run `./scripts/install.sh` first if the local virtual environment
does not exist.

Removal only removes DailyMail user-unit files and disables its timer:

```sh
./scripts/install.sh --remove
```

It deliberately preserves all application state and credentials. In particular,
it does not delete `~/.local/share/dailymail/`, `~/.local/state/dailymail/`,
`~/.config/dailymail/config.toml`, or `~/.config/dailymail/credentials.env`.

Before a code rollout, take a SQLite-consistent state backup (including no
credentials) with a destination outside the DailyMail state directory:

```sh
sqlite3 ~/.local/share/dailymail/dailymail.sqlite3 \
  ".backup '/safe/backups/dailymail-before-deploy.sqlite3'"
```

Do not copy only the live `.sqlite3` file while WAL is active. A code rollback
uses the approved ControlPanel lifecycle: ControlPanel creates a dedicated
revert branch from `main`, opens a reviewed revert PR, waits for required CI and
review, and merges that PR into `main`. It is a code rollback, not a database
rollback. Only after merged `main` is current locally, run the same
no-business-job installer:

```sh
git -C /mnt/bench/src/DailyMail switch main
git -C /mnt/bench/src/DailyMail pull --ff-only origin main
cd /mnt/bench/src/DailyMail && ./scripts/install.sh
```

Normal live verification is deliberately separate from installation. Starting
`dailymail.service`, invoking `run-daily`, or enabling/starting a missed timer
can contact Rowan and may send the configured digest. Do not use those commands
to test a deployment unless those business side effects are intended.

### ControlPanel health contract

`uv run dailymail health --json` is the stable machine-readable integration
surface. It emits `schema_version: "controlpanel.status.v1"`, `project`, an
UTC observation timestamp, overall health, separate retrieval and digest
components, schedule state, bounded recent runs, parking-cache summaries and
problems. Components use the shared `id`, `name`, `health` and `summary`
fields, while metrics are deliberately flat scalar values and recent runs use
the shared start/finish/success/summary shape. It reads the existing SQLite database in read-only mode and asks
systemd only for unit state; it never creates configuration, a database, a
credential file, a run, or any network traffic. Errors and stored run summaries
are bounded and redact email addresses, Bearer/Authorization tokens,
password/token/secret assignments and URI userinfo. Systemd and loginctl status
queries have short per-command timeouts and one total status deadline.

The response intentionally contains no recipient, SMTP status detail, message
IDs, announcement bodies, or credentials. A missing/unreadable database is an
explicit `overall.health: "unknown"` observation rather than a bootstrap or a
false healthy response.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success, including a validated zero-announcement day or a skipped duplicate |
| 1 | Run completed with a recorded failure |
| 2 | Usage error |
| 3 | TLS verification failure |
| 4 | Token discovery failure |
| 5 | HTTP/transport failure |
| 6 | Collector validation failure |
| 7 | `apiVersion` still stale after rediscovery and retry |
| 8 | Credential problem (missing, wrong mode, unreadable) |
| 9 | SMTP failure |
| 10 | Another run holds the lock |
| 11 | Render failure — nothing was sent |

---

## 3. Storage layout

| Path | Contents |
|---|---|
| `~/.local/share/dailymail/dailymail.sqlite3` | The durable database (WAL) |
| `~/.local/state/dailymail/collections/` | Phase 1 normalized artifacts (recent window) |
| `~/.local/state/dailymail/backups/` | ~30 daily SQLite backups |
| `~/.local/state/dailymail/diagnostics/` | Sent `.eml` and previews, 30 days |
| `~/.local/state/dailymail/dailymail.lock` | Overlap lock |
| `~/.config/dailymail/config.toml` | Non-secret configuration |
| `~/.config/dailymail/credentials.env` | Gmail App Password, mode 0600 |
| `~/.config/systemd/user/dailymail.{service,timer}` | Scheduling |

Retention: SQLite history is kept indefinitely; backups keep the newest 30;
diagnostics and collection artifacts age out (30 and 14 days, the latter never
dropping below the three most recent).

---

## 4. Database schema (version 4)

Plain `sqlite3`: foreign keys on, WAL, 10 s busy timeout, explicit transactions.
One integer version in `schema_meta`; no migration framework.

| Table | Purpose |
|---|---|
| `runs` | One row per production run: counts, collector validation, curation method, email status, error, trigger |
| `categories` | Rowan's dynamic registry plus `manual_priority` (config) and `inferred_priority` (curation-estimated), `first_seen_at` / `last_seen_at` |
| `announcements` | One durable row per `SubmissionId`: audience, category, `first_distribution_date`, `first_observed_at`, `current_version_id`, official URL |
| `announcement_versions` | A version per *substantive* content change, with the full source HTML, derived text, event/contact/submitter/approver fields and a canonical content hash |
| `distribution_dates` | Rowan's explicit dates, queryable |
| `daily_presence` | `(target_date, submission_id, source_view)` — independent audit of which query returned each announcement |
| `daily_records` | Per-day status (`New`/`Standing`), the version shown, and a sticky `changed` flag |
| `curation_results` | Section, rank, relevance/urgency, rationale, method, model |
| `deliveries` | Recipient, timestamps, Message-ID, content hash, SMTP status, size, forced flag |
| `parking_locations` | One durable row per parking facility: campus-scoped canonical id, type, permit class, description, coordinates, provenance, confidence, manual-override pins |
| `parking_aliases` | Every spelling matched, normalized, unique per campus |
| `parking_sources` | Per-source fingerprint, status and verification timestamps |
| `parking_landmarks` | Named campus features, for description evidence and campus disambiguation |
| `announcement_parking_locations` | Which announcement referenced which lot, on which date, how it matched |
| `parking_unresolved` | Candidates that could not be resolved, with attempt counts and reasons |
| `event_venues` | One durable row per normalized venue: coordinate, provenance, travel mode, outbound/return minutes, routing source and fingerprint |
| `calendar_recommendations` | One row per (date, announcement): candidate evidence, offer decision, relevance score/reason/method, attendance mode, title, event times, travel, mechanism, action URL, validation status, withheld reason |
| `repeat_matches` | Which prior SubmissionId a repost was matched to, with method, confidence, similarity, evidence and its durable family |
| `logical_announcement_families` | One durable family per logical announcement: key, canonical SubmissionId, normalized title, category, audience, member count, confidence, method |
| `logical_announcement_members` | Which SubmissionIds belong to a family, what each matched, and which one is canonical |
| `display_status_corrections` | Append-only audit of retrospective changes to what the digest shows, and of a skipped repeat stage (`submission_id = 0`) |

`runs.parking_stats` and `runs.calendar_stats` each hold one JSON object of
per-run counters. `calendar_recommendations.session_count` and `.sessions` record
how many selectable sittings an announcement offered and what each one carries.
`daily_records.display_status` holds what the digest shows;
`daily_records.status` keeps Rowan's own classification and is never
overwritten. Every upgrade is additive — `CREATE TABLE IF NOT EXISTS` plus
guarded `ALTER TABLE ... ADD COLUMN` — transactional, idempotent, and it
neither rewrites nor reads any existing announcement row.

**Temp storage is pinned to memory** (`PRAGMA temp_store = MEMORY`) on every
connection. This is not a performance tweak: SQLite's default writes a
materialized sort or GROUP BY spill to a file in `$TMPDIR`, and on
1 September 2026 the filesystem holding this host's `TMPDIR` was mounted
read-only across the 06:30 run. The spill failed, `SQLITE_CANTOPEN` surfaced as
"unable to open database file", and the entire logical-repeat stage was skipped
for that day's digest. A working database plus an unwritable, unrelated temp
directory must not be able to change what the reader is told.
See `docs/logical-families-and-sessions.md` §1.

### Substantive change detection

A new version is created only when the hash of the **user-visible** fields
changes: title, body HTML, category, audience, status, all event fields, and all
contact/submitter/approver fields, with whitespace collapsed and distribution
dates sorted. Every timestamp is deliberately excluded, so an `UpdatedDate`
touch or a re-approval cannot manufacture an `UPDATED` badge. Rowan's native
`UpdatedDate`/`UpdatedByName` are stored as supporting evidence but do not decide.

The per-day `changed` flag is sticky: re-running a date keeps the badge.

---

## 5. New versus Standing

Derivation is unchanged from Phase 1 and still source-driven:

```
New       when min(DistributionDates) == digest date
Standing  otherwise
```

`first_observed_at` is stored separately and is never used for this
classification. Validated 13/13 against Rowan's own Employee Daily Mail for
2026-08-20.

Phase 4 adds one **separate** question on top, asked only of announcements Rowan
calls New: *has this reader already been sent this, under a different
SubmissionId?* Rowan submitters routinely repost rather than extend a
distribution list — nine announcements did so in the first production week — so
identical text arrives labelled New days after the reader received it.

A conservative deterministic check (identical normalized title, same category
and audience, body similarity ≥ 0.85, compatible event occurrence, and almost no
distinctive words lost) sets `display_status = 'Standing'`. A genuinely new
occurrence of a recurring event stays New, and anything ambiguous stays New.
Rowan's own classification is preserved in `daily_records.status`.
`docs/calendar-and-repeats.md` §6.

---

## 6. Audience

`Submission.Audience` remains authoritative, and Phase 1's cross-view parity gate
(V9) still runs every day.

| Source | Badge |
|---|---|
| `Employees` | `EMPLOYEE` |
| `Students` | `STUDENT` |
| `Both` | `EVERYONE` |

`daily_presence` independently records which query returned each announcement, so
the field and the observed membership can be reconciled after the fact.

---

## 7. Curation

Claude ranks; it does nothing else. It does not collect, rewrite, summarize,
generate HTML, or send.

### Invocation

```
claude --print --model <configured> --output-format json
       --json-schema <schema>
       --tools ""                 # no tools at all: no shell, no files, no browser, no mail
       --strict-mcp-config        # no MCP servers
       --disable-slash-commands
       --no-session-persistence
       --system-prompt <ranking instructions>
```

The dataset arrives on **stdin**, never on the command line. The child process
runs in an empty temporary directory (so no project context or `CLAUDE.md` is
picked up) with `GMAIL_*` stripped from its environment. Model is configurable in
`config.toml`; the default is `sonnet` — capable without requiring the most
expensive tier.

### What is sent

Submission ID, section, category and its configured priority, audience, cleaned
subject, plain-text body (truncated to 3,500 characters, **email addresses
redacted**), first distribution date, days since, distribution-date count,
previous appearances, previous deliveries, updated flag, and event date/time/
location. A recursive assertion refuses to send the payload if it contains a
forbidden field name, any email address, or a data URI.

Never sent: credentials, contact/submitter/approver identities or addresses,
Banner IDs, internal IDs, raw HTML, or images.

### Output contract

The result is accepted only as a **faithful permutation**. Rejected outright:
an unknown ID, a duplicate, a missing ID, a changed section, or a non-integer
rank. Suggested positions for a new category are accepted only for categories we
actually asked about, and never for priorities 1–5, which are manually locked.

Because acceptance is structural, announcement text that tries to instruct the
model cannot alter control flow — the worst case is that the ordering is rejected
and the deterministic fallback runs. The system prompt also states explicitly
that announcement content is untrusted data.

### Fallback ordering

*New*: configured category priority, then updated status, then event proximity,
then newest first.

*Standing*: a transparent additive score —

```
  max(0, 120 - category_priority * 3)     category priority
+ max(0, 20 - days_since_first_dist)      recency
+ 25 if updated                           updated since last seen
+ max(0, 30 - days_until_event)           imminent event or deadline
+ 6 if audience is Both                   breadth of impact
- 2 * previous_appearances                repetition penalty
```

ties broken by newest `SubmissionId`. Every fallback row records its score in
`curation_results.rationale`.

---

## 8. Category priority

`config.toml` holds the ordered list. **Positions 1–5 are locked**: Official,
Technology, Facilities, Human Resources, Public Safety.

A category Rowan introduces is ingested immediately and its announcements always
render. Curation is asked to estimate where it belongs; the estimate is stored as
`inferred_priority` and can never overwrite a `manual_priority` or claim a locked
position. A category with neither lands just past the known list rather than
being dropped.

---

## 9. Email

### Subject

`Curated Rowan Daily Mail - August 21 2026` — no comma before the year.
Alerts use `DailyMail ATTENTION REQUIRED - August 21 2026`.

### Structure

```
multipart/alternative
  +- text/plain          full digest as text, with every source URL
  +- multipart/related
       +- text/html      table layout, 680px, inline CSS
       +- image/jpeg     one part per embedded image, referenced by cid:
```

### Design

Compact brown header with a gold rule, a one-line stats strip, then `NEW`
grouped by category priority and `STANDING` in a single ranked list. Each card
carries status/updated/audience badges, category, the subject as a hyperlink,
event box, a parking-location callout when the announcement names a lot, the
**complete** announcement body, contact/submitter/approver metadata, and a
`View official announcement` button. No hero image, no branding
block, no category directory, no navigation required to read the content.

Rowan brown `#57150B` and gold `#FFCC00` are used for hierarchy and accent only,
on a white background with near-black text.

Two density measures matter for classic Outlook, which ignores `<style>`:
empty author paragraphs are removed, and every block element gets an inline
margin so spacing matches the design rather than Outlook's 1em defaults.

### Sanitization

Three passes: nh3 with `data:` allowed so images survive, our own rewrite pass,
then nh3 again with `data:` forbidden. Scheme-less hosts such as
`go.rowan.edu/x` become `https://go.rowan.edu/x`; `file://` and other unusable
schemes are stripped while the link text is kept; inline styles are filtered
against a property allowlist. The stored source HTML is never modified —
rendering uses a derivative.

### Images

Inline `data:` URIs and external HTTPS images are decoded, verified as genuine
images, EXIF-rotated, downscaled to 1500 px, and compressed (quality first, then
dimensions) to fit 1.5 MB per image and 8 MB per message, then attached as CID
parts. Anything undecodable, insecure, or over budget is replaced with a visible
`Image available in the official announcement` link. A bad image never fails the
digest. Measured: the 1.21 MB inline image in announcement 6602 became a 91 KB
JPEG at 1500×600.

### Calendar action

An announcement describing a genuinely relevant scheduled event gets one compact
`CALENDAR` block between its metadata and its body: the event name, `Wed, Oct 14
· 10:00 AM–12:00 PM`, the location and attendance mode, an `Add to Calendar`
button, and a line stating any travel reserved. The button is an Outlook
compose deep link; a standards-compliant `.ics` is attached beside it, carrying
the travel holds as separate VEVENTs. The plain-text alternative carries
`Add to calendar: <URL>` and the attachment name. Like parking, the block is
additive and is deliberately **not** part of the digest content hash. Full
detail in `docs/calendar-and-repeats.md`.

### Duplicate title and category

Rowan bodies often open by repeating their own subject, which the card already
shows. That first block is suppressed at render time only, and only on an exact
normalized match with no link, image or extra text — the stored `full_body` is
never modified. A New card inside a category group does not repeat the group's
category; a Standing card, which sits in one globally ranked list, keeps it.

### Parking location

An announcement naming a parking facility gets one compact block between its
metadata and its body: the lot name, a one-sentence plain-English location, and
`Open in Google Maps` built from stored coordinates. The announcement text is
never altered — the block is additive, and it is deliberately **not** part of the
digest content hash. Unresolvable lots get a one-line link to the official campus
parking map. Full detail in `docs/parking-enrichment.md`.

### Idempotency

Before sending, the digest's content hash is computed and `deliveries` is checked
for a successful send for that date and recipient. If one exists, a normal run
exits cleanly without resending — even if the content has since changed, which is
logged for the next normal run rather than triggering another email. Use
`--force-resend` to override deliberately.

---

## 10. Credentials

`~/.config/dailymail/credentials.env`, mode **0600**, in a directory that must
not be group- or world-readable. Checked on every load; a wrong mode is a hard
failure with the `chmod` to run.

`GMAIL_SMTP_USER` and `GMAIL_APP_PASSWORD`. Whitespace is stripped from the
password because Google displays App Passwords in groups of four.

The value is read only inside the sending process. It is never logged, never put
on a command line, never placed in a systemd unit, never sent to Claude, and
never committed. `SmtpCredentials.__repr__` redacts it, a `scrub()` helper
filters it out of anything about to be logged, and the message is scanned for it
one last time before the send.

---

## 11. Security and data boundary

Phase 1's allowlist boundary is intact. Never persisted, rendered, logged, or
sent to Claude: the `User` wrapper, `External_Id` / `SubmittedByExternalId` /
`ApproverExternalId`, internal user IDs, password fields, `Last_Login`,
`rolesInfo`, and raw unfiltered API responses. A recursive audit runs at runtime
before the collection artifact is written, again before ingest, and again on the
curation payload; the test suite sweeps every text column in the database.

Published contact, submitter and approver details **are** retained — they are
part of the announcement as Rowan publishes it and appear in the digest.

### The hermetic test boundary

`uv run pytest` must reach nothing real, and that is enforced rather than
intended. `tests/hermetic_boundary.py` installs a CPython audit hook at
collection time and refuses, before the operation happens, any attempt to:

* resolve or connect to anything but loopback — which is what reaching
  `apps.rowan.edu` or `smtp.gmail.com` requires;
* open an SMTP conversation at all;
* open anything under the real `~/.config/dailymail`,
  `~/.local/share/dailymail` or `~/.local/state/dailymail`, which covers the
  production database, the run lock, the collection artifacts and the App
  Password;
* `sqlite3.connect` the production database;
* run `systemctl`/`loginctl`/`journalctl` or the production entrypoint;
* write to, rename, unlink or relink anything under
  `/mnt/bench/releases/dailymail`.

An audit hook is used deliberately: there is no API to remove one, so unlike a
fixture it cannot be dropped by the test it is protecting. The suite's own
fixtures — temporary XDG directories, fixture credentials, the stubbed
collector and SMTP, the blocked parking and route lookups — *shape* what a test
sees; the boundary *forbids* what no test may do. Every run prints what it
reached, and a refused access fails the run even if every test passed.

`monkeypatch.undo()` is forbidden in a test body, and
`tests/test_hermetic_boundary_regression.py` fails if it reappears. It reverts
the whole scope's patch stack, including every autouse fixture's, so it can
only ever remove more than the caller installed; a test that wants one patch to
stop applying wants `monkeypatch.context()`. This is not theoretical — a
`monkeypatch.undo()` in `test_a_calendar_failure_never_costs_the_digest` had
the pipeline running against the operator's real state directory, the real
Rowan endpoint and the real Gmail credentials, and was what ControlPanel's
release gate refused to validate.

The two opt-in live modules (`test_parking_live.py`, `test_visual_qa.py`) exist
to make real requests and are exempted; they skip themselves unless their own
environment flag is set.

---

## 12. Troubleshooting

**Nothing arrived this morning.**
```sh
uv run dailymail status
journalctl --user -u dailymail.service --since today
```
`status` shows the run row (including `email_status`) and the timer's next run.

**`skipped_duplicate`.** Already delivered for that date. Intentional. Use
`--force-resend` if you really want another copy.

**`curation_method` is `fallback`.** The digest was complete and correct; only
the ordering was deterministic. The reason is in the run's `error_summary`.

**Collector validation failed.** Read the message — it names the gate (V1–V9).
A stale `apiVersion` self-heals; a count or parity mismatch means Rowan's data or
API changed and warrants a look at `docs/site-reconnaissance.md`.

**SMTP authentication failed.** The App Password was revoked or mistyped.
Regenerate it in Google Account settings and rewrite `credentials.env` (mode
0600). Nothing else needs changing.

**Preview what would be sent, without sending.**
```sh
uv run dailymail render --date 2026-08-21 --inline-images --out /tmp/preview
```
`--inline-images` converts CID parts back to data URIs so the file opens in a
browser.

**An announcement was labelled NEW that the reader has already been sent.**
```sh
uv run dailymail status                          # look for a skipped stage
uv run dailymail families --recheck <date>       # read-only: what would change
uv run dailymail families --recheck <date> --apply
```
`status` prints `ATTENTION: the stage was skipped on N date(s)` when
logical-repeat resolution failed for a day, with the exception text. `--recheck`
re-resolves that date and `--apply` persists the corrections; neither touches
Rowan's own `status`, rewrites run history, or resends anything.
See `docs/logical-families-and-sessions.md` §1.

**An announcement rendered in the wrong colour.**
```sh
uv run python tools/qa/build_page.py
node tools/qa/visual-regression.mjs --full-page
ls artifacts/qa/screenshots/
```
The browser QA suite measures the *computed* colour of body prose, headings and
links at three viewports and under a dark-mode approximation, which is the class
of defect a string assertion cannot see. To check a live date instead, render it
and grep the derivative: every colour in the email should be one DailyMail chose,
and no `rgb(...)` declaration should survive.

---

## 13. Known limitations

* Curation costs roughly $0.20–0.25 per run at the default model and takes
  16–240 seconds, with a median near 90 — the bulk of the daily runtime. The
  timeout is `420` seconds, raised from 240 after 9 September 2026, when a
  working call was killed at 240.07 s against an observed maximum successful run
  of 237.0 s. Set `curation.enabled = false` in `config.toml` to use the
  deterministic ordering for free.
* Rowan uses `00:00:00` to mean "no event time", so a genuine midnight event is
  indistinguishable from an absent one.
* `ExtraEdition` has never been observed true, and Phase 6 established why: an
  Extra Edition bypasses `ActionGetHomeData` altogether, and its only read path
  needs a `SuperAdmin2` role. Rowan's Extra Edition of 31 August 2026 is a
  confirmed, documented coverage gap that closing would require a new ingestion
  channel. The flag is still collected, surfaced as
  `metrics.database_extra_editions` in `dailymail health --json`, and wired end
  to end -- badge, curation priority, fallback ordering -- so the day Rowan sets
  it, nothing needs writing. `docs/extra-editions.md`.
* If Rowan edits an announcement's distribution dates after publication, the
  derived `New`/`Standing` status can shift between runs. `first_distribution_date`
  is stored so this is detectable.
* The `Add to Calendar` button uses Outlook's compose deep link, which Microsoft
  has not documented. That is why it is paired with a standards-based `.ics`
  attachment rather than relied on alone; if the endpoint ever changes, the
  attachment still works. Only the attachment can carry travel holds — the
  compose link takes a single event.
* Travel routing uses the public OSRM demo endpoint, at most once per newly seen
  venue. It is deliberately not a dependency: a failure yields a conservative
  distance-based estimate, tagged as estimated in the callout.
* Images are re-encoded as JPEG flattened onto white. That suits the white email
  background and text-heavy flyers; a logo relying on transparency over a dark
  background would look different from the source.
* Author-supplied text colours are dropped from announcement bodies at render
  time. The stored source keeps them, and every other inline style the author
  asked for survives — but a colour that is meaningful in the source is lost.
  That is a deliberate trade: an announcement painted three points from the
  design's own accent rendered as one long headline in Outlook mobile's dark
  mode. See `docs/logical-families-and-sessions.md` §4.
* Outlook mobile dark mode is *approximated* in browser QA by a uniform CSS
  inversion, not reproduced. It reproduces the property that broke — body prose
  and the headline must stay distinct colours — but is not Outlook's exact
  transform, and no host here can run Outlook to check.
* A durable repeat family's member list grows forward from the point detection
  first matched two IDs. The canonical original is always a member, which is
  what a future repost resolves against, but the list is not a complete history
  of the announcement.
