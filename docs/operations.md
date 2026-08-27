# DailyMail Operations (Phase 2 — production)

Companion to `docs/site-reconnaissance.md` (Phase 0),
`docs/collector-architecture.md` (Phase 1) and `docs/parking-enrichment.md`
(Phase 3), all of which remain accurate. This document covers the production
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
  -> parking enrichment                       cache hit = a dictionary lookup
  -> curate (Claude; deterministic fallback on any failure)
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

---

## 2. Commands

```sh
cd ~/src/DailyMail

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

# tests
uv run pytest
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
cd ~/src/DailyMail
./scripts/install.sh
```

The installer has one fixed repository path: the repository containing the
script. It performs `uv sync --frozen` and delegates exact unit generation and
daemon reload to `dailymail install-timer --no-enable`. It intentionally does
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
is a source-control revert followed by the same no-business-job install step;
it is not a database rollback:

```sh
git -C ~/src/DailyMail revert --no-edit <deployment-commit>
cd ~/src/DailyMail && ./scripts/install.sh
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

## 4. Database schema (version 2)

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

`runs.parking_stats` holds one JSON object of per-run parking counters. The
upgrade from version 1 is additive — `CREATE TABLE IF NOT EXISTS` plus one
`ALTER TABLE runs ADD COLUMN` — transactional, idempotent, and it neither
rewrites nor reads any existing announcement row.

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

Unchanged from Phase 1 and still source-driven:

```
New       when min(DistributionDates) == digest date
Standing  otherwise
```

`first_observed_at` is stored separately and is never used for this
classification. Validated 13/13 against Rowan's own Employee Daily Mail for
2026-08-20.

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

---

## 13. Known limitations

* Curation costs roughly $0.20–0.25 per run at the default model and takes 60–95
  seconds — the bulk of the daily runtime. Set `curation.enabled = false` in
  `config.toml` to use the deterministic ordering for free.
* Rowan uses `00:00:00` to mean "no event time", so a genuine midnight event is
  indistinguishable from an absent one.
* `ExtraEdition` has never been observed true. It is stored so a special edition
  becomes visible if one ever appears, but its delivery path is unverified.
* If Rowan edits an announcement's distribution dates after publication, the
  derived `New`/`Standing` status can shift between runs. `first_distribution_date`
  is stored so this is detectable.
* Images are re-encoded as JPEG flattened onto white. That suits the white email
  background and text-heavy flyers; a logo relying on transparency over a dark
  background would look different from the source.
