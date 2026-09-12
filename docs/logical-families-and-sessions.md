# Logical families, multi-session calendars, and the render colour boundary (Phase 5)

Companion to `docs/site-reconnaissance.md` (Phase 0),
`docs/collector-architecture.md` (Phase 1), `docs/operations.md` (Phase 2),
`docs/parking-enrichment.md` (Phase 3) and `docs/calendar-and-repeats.md`
(Phase 4). Phase 4 remains accurate except where noted here.

Phase 5 is remediation. Four defects reached the reader in the digest of
**1 September 2026**, and this document is the record of what caused each one,
what changed, and how to inspect it.

| # | Symptom in the 1 September digest | Root cause | Fixed by |
|---|---|---|---|
| 1 | A Cayuse announcement posted for the fourth time rendered `NEW` | The logical-repeat stage raised `SQLITE_CANTOPEN` and was skipped for the whole digest | §1, §2 |
| 2 | Provost's Coffee Hours rendered `NEW` despite delivery on 31 August | Same skipped stage | §1, §2 |
| 3 | Coffee Hours got no DailyMail calendar control at all | Multi-sitting announcements were withheld as `multiple_distinct_dates` | §3 |
| 4 | `Nominate the PROFessional(s) of the Month` rendered entirely in the headline colour | The source painted its own body in a near-identical shade of the design's accent | §4 |

---

## 1. Why the repeat stage was skipped

The journal for the 06:30 run carries one line about it:

```
Sep 01 06:30:02 entropy dailymail[28034]: WARNING dailymail
  logical-repeat detection failed: unable to open database file
```

"Unable to open database file" is SQLite's message for `SQLITE_CANTOPEN`, and
the database itself was fine — the digest was collected, persisted, curated,
rendered and sent normally. The file SQLite could not open was a **temp file**.

Three facts combine into the failure:

1. `delivered_history()` selected `body_text` and `full_body` for every
   announcement ever delivered, and sorted the result. That made SQLite
   materialize the entire delivered corpus into a temp B-tree — `USE TEMP B-TREE
   FOR GROUP BY` plus `USE TEMP B-TREE FOR ORDER BY` in the query plan — which
   was 119 rows and 5.9 MB after twelve days of production, well past the 2 MB
   page cache.
2. `PRAGMA temp_store` was at its default, so that spill goes to a *file*, in
   the first writable directory among `$SQLITE_TMPDIR`, `$TMPDIR`, `/var/tmp`,
   `/usr/tmp`, `/tmp`. This host's systemd user manager exports
   `TMPDIR=/mnt/bench/tmp/pytest`.
3. `/mnt/bench` was mounted **read-only from roughly 02:43 until 08:07:09 ET**
   that morning — the kernel log shows `EXT4-fs (sda1): mounted filesystem
   825aeff1... r/w` only after the 08:06 reboot, and other services on the host
   logged `[Errno 30] Read-only file system` for `/mnt/bench/...` continuously
   through 06:30.

So the temp-file create failed, and `_apply_logical_repeats` — which correctly
catches everything, because a classification problem must never cost the reader
their digest — logged a warning and returned 0. Every announcement in that
digest kept Rowan's own label.

The classification logic was not at fault. Re-running the same comparison
against the same data finds all seven repeats, including both reported ones.

### What changed

**The coupling is gone.** `db._apply_temp_store()` pins `PRAGMA temp_store =
MEMORY` on every connection. A working database plus an unwritable, unrelated
temp directory can no longer change what the reader is told.

**The query no longer needs a spill.** `delivered_history()` is now the *index*
pass: it selects titles and comparison keys, no bodies, and no `ORDER BY`.
`repeats.find_repeats` buckets by normalized title, and `db.announcement_bodies`
then reads `body_text`/`full_body` for the two or three candidates that
survived. On 1 September that is 6 bodies instead of 119, and it stays that way
as the corpus grows.

**A skip is now visible.** A failed stage writes a row to
`display_status_corrections` with `submission_id = 0` and the exception text,
and `dailymail status` prints it under `logical repeats:` with the command to
re-resolve. On 1 September a journal WARNING was the only trace, and the
mislabelled digest had already been sent.

## 2. Durable logical families

Fixing the crash is necessary and not sufficient. `repeat_matches` records one
*pairwise* finding — 6768 repeats 6665 — and carries no forward memory:

* a repost is compared only against announcements still in the delivered
  corpus, so the original eventually ages out of reach; and
* a day on which the stage fails loses the finding outright, and the next repost
  starts again from nothing.

The Cayuse family had already been established on 31 August and 6769 still
shipped as `NEW` the next morning.

### The model

Schema **v4** adds two tables. A family is keyed on the same
`normalized title | category | audience` triple `repeat_matches.family_key`
already used, so an existing installation's findings migrate into it directly.

```
logical_announcement_families
    family_id  family_key  canonical_submission_id  normalized_title
    category_id  source_audience  member_count  confidence  match_method
    created_at  last_seen_at

logical_announcement_members
    family_id  submission_id  matched_submission_id  content_hash
    match_confidence  match_method  is_canonical  first_seen  last_seen
```

The **canonical** member is the lowest SubmissionId in the family. Rowan
allocates them monotonically, so that is the original posting; it only ever
moves earlier, never later, so re-running a day cannot rewrite history.

`repeat_matches` gains a `family_id` column, and the v4 migration **backfills**
families from the pairwise findings already recorded — so the Cayuse family
(6665 → 6668 → 6768 → 6769) is resolvable immediately rather than after the next
repost happens to fall inside a window.

### The one property that matters

**A family only ever widens the candidate pool. It never lowers the bar.**

`repeats.find_repeats` unions two candidate sources per announcement:

1. the delivered index, bucketed by normalized title;
2. every member of the family matching this announcement's own family key,
   regardless of when it was delivered.

Both go through the same `compare()`, with every Phase 4 veto intact, and a
candidate the reader was never actually *sent* is discarded before it can
justify anything. So the family can only stop a true positive being missed for
want of a candidate — it cannot manufacture a false positive.

Ties are broken on the earliest SubmissionId. Reposts are byte-identical, so
every family member scores exactly 1.0 against a new one; anchoring on the
original keeps the recorded match stable across runs.

### Two new vetoes

Re-running the corrected classification over the production corpus surfaced two
cases the Phase 4 rules got wrong. Both are now vetoed.

**Dates in prose.** Rowan's structured `Event` flag is false for most of what is
really an event, so `_event_compatible` could not see a date change in:

```
- Saturdays at 4 p.m. from July 11 through August 29
+ Saturdays at 4 p.m. from September 5 through November 21, no shows October 3 and 24
```

That is the *next run* of the planetarium show, not a revision of the last one,
and it scored 0.96 similar because everything except the dates is boilerplate.
Announcement 6749 was accordingly demoted to Standing, hiding a season the
reader had never been told about. `_dates_compatible` now compares the calendar
days each body names: if both name dates and the sets differ, they are not the
same occurrence. Byte-identical bodies are exempt, since they cannot disagree
about a date.

The comparison deliberately does *not* reuse `events.parse_dates`, which
resolves a date for scheduling — inferring an omitted year and rejecting a
weekday that disagrees with it. Here the token is `MM-DD` (or `YYYY-MM-DD` when
the source stated a year), so the answer is a pure function of the text.

**A changed detail in the near-identical band.** Announcement 6618 moved its
film screening from `Robinson 102` to `Business 208` and changed nothing else,
scoring 0.9874. Standing was right; no `UPDATED` badge was not — a reader acting
on what they remember walks to the wrong building. A repost in the 0.97–0.995
band now carries `UPDATED` unless the change is a **spelling correction**, which
is detected by asking whether every word that disappeared has a close neighbour
among the words that appeared. `commited` → `committed` does; `Robinson` →
`Business` does not.

### Effect on the 1 September digest

| SubmissionId | Repeats | Method | Shown |
|---|---|---|---|
| 6555 | 6552 | identical | STANDING |
| 6574 | 6572 | identical | STANDING |
| 6612 | 6476 | identical | STANDING |
| 6618 | 6617 | near-identical, room changed | STANDING + UPDATED |
| 6702 | 6701 | identical | STANDING |
| 6769 | 6665 | identical | STANDING |
| 6775 | 6772 | identical | STANDING |

26 New / 19 Standing, from 33 New / 12 Standing as delivered. Rowan's own
`daily_records.status` is unchanged for every one of them, and each correction
is recorded in `display_status_corrections` with its reason.

## 3. Multi-session calendar actions

### What was wrong

Phase 4 withheld any announcement whose body named more than one date, as
`multiple_distinct_dates`. Refusing to span two dates as one appointment, and
refusing to silently pick one of them, were both right. Throwing the
announcement away was not — because Rowan's Provost's Coffee Hours is a real
shape, and a common one:

```
Our August sessions will be focused on research. The dates are:
Thursday, September 10 from 11:00-12:30
Monday, September 21 from 2:30-4:00

... register your interest by completing this form (go.rowan.edu/CoffeeSept26)
by Monday, September 7th. These sessions are limited to 10-12 people ...
```

The reader is being offered a **choice of sitting**. With no DailyMail control,
what they got instead was Outlook mobile's automatic date-linking, which
produces an appointment with no subject worth reading, no location, no
registration link, no contact and no description.

### Session extraction

`events.parse_sessions` recognizes a session list only where the source itself
pins one date to one time range definitely enough that no inference is needed:

* exactly one date and exactly one time range on the line;
* separated by at most 24 characters of connective text;
* with no sentence break and no other digit between them;
* on a line no longer than 160 characters; and
* not phrased as a recurrence.

The paragraph above fails three of those at once, which is why `10-12 people`
never becomes a third sitting: the gap from `September 7th` to `10-12` crosses a
full stop, runs to 31 characters, and sits on a 300-character line.

Two ranges on the *same* day are still phases of one event, which is what
`merge_segments` is for. A list of more than `MAX_SESSIONS` (6) is a term
timetable rather than a choice, and is withheld as `too_many_sessions`. One
definite sitting among several dates is not a session *list*, so it keeps the
conservative refusal.

Each sitting becomes its own `EventCandidate`, sharing the series' location,
registration links, audience, attendance mode and heading hint, and carrying
`session_index`, `session_count` and `sibling_sessions`.

### One decision, several controls

Relevance, attendance mode and the title are **announcement-level** decisions,
made once from the first session and applied to every sitting. So a
multi-session announcement costs **no extra model call** — the curation payload
gains a `sessions` list of `{date, start_time, end_time}` so the model can see
it is a choice of dates rather than a multi-week commitment, and the prompt
tells it explicitly not to pick one. The venue is resolved once, and the travel
plan — a property of the place, not of the sitting — is applied to each sitting
at its own times.

`CalendarAction` is now the series and `CalendarSession` is the sitting. For the
overwhelmingly common single-session announcement the one session is *derived*
from the action's own fields on every access rather than copied, so the two
cannot drift and every caller that predates the concept works unchanged.

Each session gets its own Outlook compose deep link, its own VEVENTs, its own
UIDs and its own attachment name:

```
Provost-s-Coffee-Hours-Focus-on-Research-2026-09-10.ics
Provost-s-Coffee-Hours-Focus-on-Research-2026-09-21.ics
```

A single-session announcement keeps the UID suffixes and filename production
already emits (`event`, `travel-out`, `travel-back`; `Provost-s-Town-Hall.ics`),
so re-rendering a past day still produces byte-identical calendar data.

`validate_action` checks every session and checks them against each other: two
sittings sharing a start time or an attachment filename are rejected even though
each one is individually well formed.

### What the reader sees

```
CALENDAR
Provost's Coffee Hours - Focus on Research
2 sessions — choose the one you will attend

  Thu, Sep 10 · 11:00 AM–12:30 PM
  [ Add Thu, Sep 10 › ]
  Attached: Provost-s-Coffee-Hours-Focus-on-Research-2026-09-10.ics

  Mon, Sep 21 · 2:30 PM–4:00 PM
  [ Add Mon, Sep 21 › ]
  Attached: Provost-s-Coffee-Hours-Focus-on-Research-2026-09-21.ics
```

A single-session card still says `Add to Calendar`; only a series names its
dates, because "Add to Calendar" twice in one card tells the reader nothing
about which sitting they are choosing. The plain-text alternative carries the
same choice, one URL and one attachment name per sitting.

Each `.ics` description names which sitting it is, lists the alternatives, and
says that registering for one does not reserve the others.

### The calendar title

`clean_calendar_title` now flattens a trailing parenthetical qualifier:
`Provost's Coffee Hours (Focus on Research)` → `Provost's Coffee Hours - Focus
on Research`. A calendar list truncates, and a parenthesis is the first thing to
be cut off. It is purely typographic — the words are the source's own, in the
source's own order, with nothing added — and it declines qualifiers that state
*how to attend* rather than *what the event is* (`(Hybrid)`, `(Virtual)`,
`(Free)`), and anything containing a date.

While fixing that, one latent defect went with it: the trailing-date trim ran
its leftover-separator strip unconditionally, so `Town Hall (Hybrid)` became
`Town Hall (Hybrid`. The strip is now conditional on a date actually having been
removed.

### Registration links

Rowan authors routinely put the verb in the prose and the bare host in the
anchor: `register your interest by completing this form
(go.rowan.edu/CoffeeSept26)`. Anchor text alone missed it, so
`registration_urls` now also reads the prose preceding a link — scoped to the
anchor's **own block**, because the Town Hall body says "WebEx (Register for the
link)" in one list item and links an unrelated feedback form two paragraphs
later, and a flat character window wrongly called that a registration URL.

### Client-generated date links

Outlook and iOS may still auto-link the dates in the announcement body, and
that is left alone: mangling the source text to suppress it would be a worse
defect than a redundant blue link. The DailyMail controls are visually distinct
from it — a solid brown block with white bold text, not blue underlined prose —
and are the only ones carrying the subject, description, location, links and
travel. Nothing about them depends on the client's recognition; the browser QA
suite asserts that every anchor in the digest is one DailyMail generated.

## 4. The render colour boundary

### What was wrong

Announcement 6612 wraps every paragraph of its body in one declaration:

```html
<p><span style="color:rgb(90,19,0);">PROFessional(s) of the Month is for …</span></p>
```

`rgb(90,19,0)` is `#5A1300`. DailyMail's own accent — the colour its headlines
are painted in — is `#57150B`. Three points apart.

In light rendering the card merely looked odd. In Outlook mobile's dark mode,
which force-inverts the whole design, the author's dark maroon and the design's
dark maroon invert to the **same peach**, and the entire article reads as one
long headline. The control that rendered correctly on the same morning in the
same client, 6736 (OSEC), simply carries no `color` declaration at all and
inherits the digest's ink.

It was not an isolated case: 6694 (the Town Hall) paints its body `#57140c` and
6702 (Coffee Hours) paints its `#1f1f1f`.

### The policy

`sanitize.filter_style` is the **security** allowlist and is unchanged — `color`
is still an inert property and still survives sanitization. Deciding what is
*safe* is a different question from deciding what is *legible inside DailyMail's
card*, and conflating them would mean a future decision about the palette had to
be argued as a security change.

`sanitize.apply_body_color_policy` is the **presentation** pass, applied by the
renderer after sanitization:

| Element | Colour |
|---|---|
| `p div li td th blockquote pre code caption dt dd figcaption small` | `#1a1a1a` (body ink) |
| `h1`–`h6` | `#57150B` (accent) |
| `a` | `#57150B` (accent) |
| `span strong em i u img ul ol table` … | inherited from the block; source colour removed |

Source `color` and `background-color` declarations are dropped everywhere. A
source `background-color:#000` with DailyMail's dark ink would be unreadable, so
both go. Everything else the author asked for — margins, alignment, font size,
weight, style, decoration, lists, tables — is untouched.

The stored `FullBody` keeps every source byte. This is a render derivative, like
duplicate-title suppression beside it.

### Result

Body prose across the whole 1 September digest computes to `#1a1a1a`, body links
to `#57150B`, headlines to `#57150B`, and no `rgb(...)` declaration survives
anywhere. Under a uniform dark-mode inversion, prose and headline remain
distinct colours — the property that broke.

## 5. Browser QA

The digest is deterministic HTML and the production path has **no browser
dependency and must not acquire one**. But the 1 September colour defect was
invisible to every string assertion in a green 873-test suite, because the defect
was in what the markup *computed to* after inheritance, in a narrow viewport,
under a client-side inversion. That is what a browser is good for.

### Tooling

Playwright is **reused, never installed**. `tools/qa/visual-regression.mjs`
resolves it in order:

1. the repository's own `node_modules` (pinned in `package.json`);
2. the `@playwright/cli` bundle another project already installed globally at
   `~/.npm-global/lib/node_modules/@playwright/cli`.

If neither is present it prints where it looked and exits 2 rather than
downloading a second browser stack. In production on entropy it resolves to
Playwright 1.62.1 / Chromium 151 from the repository; the global bundle carries
1.63.0-alpha / Chromium 152 as the fallback.

### Running it

```sh
# 1. build the page from committed fixtures (no Rowan, no Claude, no network)
uv run python tools/qa/build_page.py

# 2. assert DOM/CSS invariants at three viewports and save screenshots
node tools/qa/visual-regression.mjs

# both, as one opt-in pytest module
DAILYMAIL_VISUAL_QA=1 uv run pytest tests/test_visual_qa.py -v
```

`build_page.py` **isolates its own state**: it redirects `XDG_DATA_HOME` and
`XDG_STATE_HOME` to a scratch directory and refuses to run if the database does
not resolve inside it. It seeds announcements and renders a digest, and both of
those write — run without isolation it inserts its fixtures into the live
history. `XDG_CONFIG_HOME` is deliberately left alone so the page reflects the
real travel policy and relevance threshold.

### What it asserts

135 invariants across 390×844, 430×932 and 1280×900:

* body prose computes to the digest ink, per card
* body headings and body links compute to the accent
* the card headline is the accent and differs from its prose
* bold, italic and underline survive the colour policy
* one calendar control per offered sitting, each with a distinguishable label
* each session button targets a distinct time
* buttons are at least a 40 px tap target and fit the viewport
* status and audience badges are present, unclipped, and not the colour of their
  own background
* no horizontal overflow
* under a uniform dark-mode inversion, prose and headline stay distinct

`tests/test_visual_qa.py` also runs the suite against a page built with the
colour policy **switched off** and requires it to fail with `rgb(90, 19, 0)` —
a regression check that cannot fail is not a regression check.

Screenshots land in `artifacts/qa/screenshots/` and the generated page in
`artifacts/qa/pages/`, both gitignored. The *fixtures* in
`artifacts/qa/fixtures/render-regression.json` **are** committed: they are the
regression cases, verbatim from production, with the contact/submitter/approver
blocks omitted entirely and any email address in the body prose replaced with
`redacted@example.invalid`. Neither is involved in what they test.

## 6. Pipeline order

`daily.PIPELINE_ORDER` states the intended sequence, and
`tests/test_pipeline_order.py` asserts it:

```
 1. collect                 8. calendar relevance (rides on curation)
 2. validate                9. parking enrichment
 3. persist                10. calendar enrichment (venue, travel, .ics)
 4. source semantics       11. render
 5. logical-repeat family  12. send
 6. display_status
 7. event/session extraction
```

Stage 6 is load-bearing. `daily_records.status` keeps Rowan's classification
forever; `daily_records.display_status` is written by stages 5–6 **alone**.
`ingest.record_daily` deliberately omits the column on conflict, curation is
accepted only as a permutation and never sees it, and everything downstream
reads `COALESCE(display_status, status)`. Each of those is a test.

## 7. Manual diagnostics

### Inspect a logical family

```sh
uv run dailymail families                      # every family, largest first
uv run dailymail families --submission 6769    # the family one ID belongs to
uv run dailymail families --key 'coming soon a modernized cayuse platform to better support our research enterprise|5|Employees'
```

`--submission` also prints the pairwise match evidence, including
`candidate_source` — `delivered_history` or `family` — which says whether the
durable model was what found it.

### Re-check a date's repeat classification

```sh
uv run dailymail families --recheck 2026-09-01           # read-only
uv run dailymail families --recheck 2026-09-01 --apply   # persist + audit
```

This is the operator path for a day the stage was skipped. It never touches
`daily_records.status`, never rewrites run history, and never resends anything:
it corrects what future renderings of that date show and records why in
`display_status_corrections`.

### Inspect calendar sessions

```sh
uv run dailymail sessions --date 2026-09-01
uv run dailymail sessions --date 2026-09-01 --submission 6702
uv run dailymail sessions --date 2026-09-01 --submission 6702 --ics
```

Prints each detected sitting with its date, times, attendance mode, location and
evidence; then the persisted decision with its attachment names and travel
minutes. `--ics` prints the complete calendar payloads.

### Render a preview

```sh
uv run dailymail render --date 2026-09-01 --out /tmp/preview
```

Reports one line per offered session, including `[session n/m]` and the
attachment name. A preview never calls the model and never makes a route lookup.

### Screenshot QA

```sh
uv run python tools/qa/build_page.py
node tools/qa/visual-regression.mjs --full-page
ls artifacts/qa/screenshots/
```

### Query the state directly

```sh
python3 - <<'PY'
import sqlite3, os
db = os.path.expanduser("~/.local/share/dailymail/dailymail.sqlite3")
con = sqlite3.connect(f"file:{db}?mode=ro", uri=True); con.row_factory = sqlite3.Row

for row in con.execute("""
    SELECT f.family_key, f.canonical_submission_id, m.submission_id, m.is_canonical
      FROM logical_announcement_families f
      JOIN logical_announcement_members m ON m.family_id = f.family_id
     ORDER BY f.family_id, m.submission_id"""):
    print(dict(row))

for row in con.execute(
    "SELECT * FROM display_status_corrections ORDER BY correction_id"):
    print(dict(row))

for row in con.execute("""
    SELECT submission_id, calendar_title, session_count, sessions
      FROM calendar_recommendations
     WHERE target_date = '2026-09-01' AND offer_calendar = 1"""):
    print(dict(row))
PY
```

## 8. Storage

Schema version **4**. Additive and idempotent, exactly like v2 and v3:
`CREATE TABLE IF NOT EXISTS` plus guarded `ALTER TABLE ... ADD COLUMN`. No
existing table is rewritten, so a failure leaves v3 intact.

| Table / column | Purpose |
|---|---|
| `logical_announcement_families` | One durable family per logical announcement: key, canonical SubmissionId, normalized title, category, audience, member count, confidence, method, timestamps |
| `logical_announcement_members` | Membership: family, SubmissionId, what it matched, content hash, confidence, method, canonical flag, first/last seen |
| `display_status_corrections` | Append-only audit of retrospective changes to what the digest shows, and of stage skips (`submission_id = 0`) |
| `repeat_matches.family_id` | Links a pairwise finding to its durable family |
| `calendar_recommendations.session_count` | How many selectable sittings the announcement offered |
| `calendar_recommendations.sessions` | One JSON entry per sitting: index, start, end, attachment name, travel minutes |

The v4 migration backfills families from existing `repeat_matches` rows, so an
installation upgrading in place keeps everything it had already learned.

### Backup and migration procedure

Unchanged from `docs/operations.md`. For the record, what was done on
1 September 2026:

```sh
# 1. verified backup before any migration
#    -> dailymail-v3-pre-logical-family-schema-20260901T192555Z.sqlite3
#    integrity_check ok, foreign_key_check clean, schema_version 3
# 2. db.initialize() applied v4 transactionally and backfilled 10 families
# 3. daily.resolve_logical_repeats() re-resolved 2026-09-01 read-only
# 4. daily.persist_logical_repeats() wrote 7 display statuses in one transaction
# 5. each change recorded in display_status_corrections with its reason
# 6. integrity_check ok, foreign_key_check clean
```

## 9. Security

Every Phase 1–4 boundary is intact and none is widened.
`tests/test_hermeticity.py` asserts each of these.

* The curation payload gained exactly one field: a `sessions` list of
  `{date, start_time, end_time}`, all read back from our own extraction. It
  still carries no email address, no SMTP credential, no forbidden Rowan `User`
  field, no ICS payload and no action URL.
* The `dailymail` package imports no browser module, and no file under
  `src/dailymail/` mentions Playwright or Chromium — checked by grep, because an
  import guard would not catch a subprocess call.
* The browser QA suite is opt-in behind `DAILYMAIL_VISUAL_QA=1`, and its tooling
  lives in `tools/qa/`, outside the shipped wheel. `pyproject.toml` names no
  browser dependency.
* Routing remains injected. The normal test suite cannot open a socket, and a
  multi-session announcement makes at most one route lookup — for its venue, not
  per sitting.
* Calendar text is still stripped of control characters before reaching an
  iCalendar property, an attachment filename or a URL, and per-session
  attachment names are slugified and checked for uniqueness.

## 10. Known limitations

* **Family membership grows forward.** The v4 backfill seeds families from
  recorded pairwise findings, so a repost that predates repeat detection — 6667
  in the Cayuse family — is not a member. The family's canonical original is,
  which is what a future repost resolves against, so this costs nothing; but a
  family's member list is not a complete history of the announcement.
* **`repeats.body_similarity` is not symmetric.** `difflib.SequenceMatcher`
  applies its autojunk heuristic to sequences over 200 elements, so long bodies
  can score differently depending on argument order. `compare()` always calls it
  as `(current, prior)`, and the asymmetry only ever produces a *lower* score —
  which is the conservative direction — so this is documented rather than
  changed.
* **Announcement 6747 remains Standing on 31 August.** It was demoted under the
  Phase 4 rules before the body-date veto existed. The delivered digest said
  Standing, so the stored history is left telling the truth about what was sent;
  the rule change prevents recurrence.
* **Outlook mobile dark mode is approximated, not reproduced.** The browser QA
  suite applies a uniform CSS inversion, which reproduces the property that
  broke — prose and headline must stay distinct — but is not Outlook's exact
  transform. `[data-ogsc]` overrides were considered and deliberately not added:
  they are untestable on this host, and with source colours stripped the design
  now inverts uniformly, which is what made the OSEC control render correctly in
  the first place.
