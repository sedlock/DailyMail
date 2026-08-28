# Calendar actions and logical repeats (Phase 4)

Companion to `docs/site-reconnaissance.md` (Phase 0),
`docs/collector-architecture.md` (Phase 1), `docs/operations.md` (Phase 2) and
`docs/parking-enrichment.md` (Phase 3), all of which remain accurate.

Phase 4 adds three things: an `Add to Calendar` action on announcements that
describe a genuinely relevant scheduled event, detection of announcements Rowan
reposted under a new SubmissionId, and two render-time cleanups.

---

## 1. Why the Event flag is not enough

The announcement that motivated this feature is 6694, the Provost's Town Hall of
14 October 2026. Rowan's own `Event` boolean is **false** for it, and every
event field is empty. Everything a calendar needs is in the body:

```
Date: Wednesday, October 14, 2026
Times:
  10:00 - 11:15 - Presentation and Q&A
  11:15 - 12:00 - Social
Location: Hybrid
  Chamberlain Student Center, Eynon Ballroom (in-person preferred)
  WebEx (Register for the link)
Who: This event is open to all faculty, staff, and managers
```

So detection is layered, in `src/dailymail/events.py`:

1. Rowan's structured event fields, when it filled them in
2. explicit labelled body structure (`Date:` / `Times:` / `Location:`)
3. an unambiguous date + time-range pairing anywhere in the body

Evidence is recorded per announcement, so a decision can be explained later
without re-deriving it.

### What is deliberately *not* an event

| Withheld because | Example |
|---|---|
| `no_event_date` / `no_event_time` | "Homecoming is on Saturday, October 24" with no time |
| `multiple_distinct_dates` | Provost's Coffee Hours: two sittings a fortnight apart |
| `recurring_schedule` | "9 a.m. - 2:35 p.m. Monday - Thursday" — a term timetable |
| `overlapping_time_blocks` | one announcement carrying Glassboro *and* Camden schedules |
| `deadline_not_event` | "applications are due by October 14" |
| `end_before_start` | contradictory source times |
| a stated weekday that disagrees with its date | `Monday, October 14, 2026` (it is a Wednesday) |

Rowan stores "no event time" as `00:00:00`, so a genuine midnight event is
indistinguishable from an absent one and is treated as absent — the safe
direction, since no time block means no calendar action.

## 2. Relevance

The relevance question rides along on the **curation call that already runs**.
The payload gains a small `event_candidate` block per detected event, and the
strict output schema gains an optional `calendar` object per ranking entry:

```json
{"offer": true, "confidence": 0.95,
 "reason": "Senior leadership town hall; broad institutional relevance.",
 "attendance_mode": "hybrid",
 "suggested_title": "Provost's Town Hall & Social"}
```

Measured overhead on the real 2026-08-28 digest: payload 29,728 → 29,847 bytes,
run 82.3 s → 81.4 s, $0.166 → $0.153. There is no second model invocation.

**What the model may influence:** whether to offer the button, how confident it
is, which way a *hybrid* event is likely to be attended, and — optionally — a
cleaner title.

**What it may never do:** supply or alter a date, a time, a location, a link,
or any calendar payload. Those come from our own extraction. The schema has no
field for them, and `curate._accept_calendar()` discards anything malformed.

A suggested title is accepted only if every word of it already appears in the
announcement, and only if it contains no URL, no address and no date. So a
title can *shed* artefacts and can never gain a claim. `Provost's Town Hall -
Oct 14` → `Provost's Town Hall & Social` is accepted (the body's own heading);
`Provost's Secret Budget Meeting` is rejected.

When curation is unavailable the digest falls back to a transparent
keyword-and-category score with the same reader model, so the button keeps
working on a day Claude does not. The default threshold is 0.6 and is
configurable.

## 3. The calendar mechanism

Two mechanisms, chosen after checking what Microsoft actually documents.

### The `.ics` attachment is the authoritative one

RFC 5545 iCalendar is a real standard and Microsoft documents its own handling
in [MS-STANOICAL]. Two findings there decided the design:

* **V0341** — Outlook itself exports `.ics` files attached to mail with
  `Content-Type: application/octet-stream`, reserving `text/calendar` for iMIP
  scheduling data.
* **V0343** — when several MIME parts carry iMIP data, Outlook treats only the
  *first* as scheduling and the rest as attachments.

So a `text/calendar` part would turn the digest into a meeting request and
silently demote every event after the first. Each offered event is instead
attached the way Outlook itself would attach one: `application/octet-stream`
with a slugified `.ics` filename. The digest stays a newsletter, and opening
an attachment imports every VEVENT in it — travel holds included.

The VCALENDAR carries `METHOD:PUBLISH`, a real `VTIMEZONE` for
`America/New_York`, CRLF line endings and 75-**octet** line folding.

### The button is an Outlook deep link

A button has to be a hyperlink, and a hyperlink cannot address a MIME part:
`cid:` works for `<img src>`, not for opening an attachment. So `Add to
Calendar` targets `outlook.office.com/calendar/deeplink/compose`, which opens
the Microsoft 365 calendar composer pre-filled with the subject, the real start
and end, the location and the description; the reader presses Save.

That endpoint is **community-documented, not Microsoft-documented**, which is
exactly why it is not the only mechanism.

### The documented compromise

| | one click | travel holds | documented by Microsoft |
|---|---|---|---|
| Outlook deep link | yes | no (single event only) | no |
| `.ics` attachment | open, then save | yes (3 VEVENTs) | yes |

Both are emitted. The button carries the real event at its real advertised time;
the attachment beside it carries the complete import. The email callout names
the attachment so the association is unambiguous, and the plain-text alternative
carries `Add to calendar: <URL>` plus the attachment name.

Gmail is a secondary target: it renders the callout table and the button
normally, and offers the `.ics` attachment for download.

## 4. Travel

The reader's problem is not what time the town hall starts — the announcement
says that. It is that a 10:00 event across campus quietly consumes 09:50 and
12:10 too, and Outlook will book a meeting into both.

**Base location:** `201 Mullica Hill Rd, Glassboro, NJ 08028`, geocoded once via
OpenStreetMap Nominatim to `39.70791, -75.11288` and stored in `config.toml`. It
is a calculation reference and is deliberately not advertised in the email.

**"Remote" means physical distance, not "virtual."** A WebEx-only session needs
no travel; a room 430 m away still needs a few minutes.

**Venue resolution reuses Phase 3's campus map.** `Chamberlain Student Center,
Eynon Ballroom` is looked up in `parking_landmarks` — the same authoritative
Rowan My Maps data the parking feature already caches — by trying the full
string, then each comma/dash-separated part, then each part with its room
qualifier removed. A room name never appears on a campus map; its building
always does. Cached parking facilities are the second source. A name that exists
on two campuses without campus evidence stays unresolved rather than guessing.

**Calculation:**

| Distance from base | Method |
|---|---|
| under 60 m | no travel: effectively at base |
| up to 1,200 m | deterministic walk: straight-line × 1.25 ÷ 1.3 m/s, plus 3 min |
| beyond that | OSRM driving time, plus 10 min arrival / 5 min return margin |

Blocks are rounded up to 5 minutes, floored at 10 and capped at 90. Chamberlain
is 433 m from base, which comes out at 10 minutes each way.

**Routing is never a dependency.** The read-only public OSRM endpoint is called
at most once per *newly seen* venue, behind a 6-second timeout, sending two
coordinates and nothing else. Any failure degrades to a conservative
distance-based estimate tagged `estimated` and surfaced in the callout. A
routing outage can never withhold a calendar action.

**`event_venues` caches the coordinate and its travel time.** Venues repeat
weekly, so the same ballroom is measured once. The cache entry records a
fingerprint of the inputs (base coordinate, speed, padding), so changing the
base location invalidates it automatically. Entries are re-verified after 180
days. A later resolution that computed no travel never overwrites good numbers.

**The advertised time is never altered.** Travel is separate VEVENTs either side
of the real event, never a padded start. The event block keeps 10:00–12:00, and
both the travel blocks and the description say so explicitly:

```
Actual event: 10:00 AM–12:00 PM.
This calendar entry also reserves 10 minutes before and 10 minutes after for
travel from/to 201 Mullica Hill Rd, Glassboro, NJ 08028.
```

## 5. Failure behaviour

Calendar enrichment is **noncritical**, in exactly the sense parking already is.

| Failure | Behaviour |
|---|---|
| Event parsing raises | Counted as `parse_failures`; the announcement renders normally |
| Model relevance output invalid | Discarded; the ranking still stands; deterministic score used |
| Venue unresolvable | Action still offered, without travel; `venue_unresolved` recorded |
| Route lookup fails | Conservative estimate, tagged `estimated`; action still offered |
| Link or ICS generation fails | No button for that announcement; `validation_status='failed'` |
| Persisting the audit row fails | Actions still render; the error is recorded in metrics |

None of these is an `ATTENTION REQUIRED` condition and none of them can lose an
announcement. A malformed action is never emitted: `validate_action()` is the
last gate and it rejects an inverted time, a non-Outlook URL, a truncated or
non-CRLF VCALENDAR, a control character, or an unusable filename.

## 6. Logical repeats

### What was actually happening

Rowan Announcer submitters routinely repost an announcement under a **new
SubmissionId** rather than extending its distribution dates. In DailyMail's
first production week this happened to nine distinct announcements and produced
twelve reposts:

| Announcement | SubmissionIds |
|---|---|
| Academic Integrity: Resources and Reminders for Faculty | 6625, 6626, 6627, 6628 |
| Rowan University Turnitin Policy | 6629, 6630, 6631 |
| 2027 Searle Scholars Program | 6496, 6497, 6498 |
| Coming soon: A modernized Cayuse platform | 6665, 6667, 6668 |
| Limited Submission: Andrew Carnegie Fellows | 6578, 6579, 6580 |
| Digital Accessibility at Rowan | 6686, 6687 |
| Internal Limited Submission: Brain Research Foundation | 6572, 6573 |
| New Canvas Module: AI Literacy | 6529, 6530 |
| Academic Integrity Information (students) | 6552, 6553 |

The reported case, 6686 → 6687, was submitted twice **two minutes apart** by the
same author, with byte-identical bodies; only the distribution dates differ
(`["2026-08-27"]` versus `["2026-08-28","2026-08-29"]`).

Each new ID has `min(DistributionDates) == today`, so Rowan's own semantics —
which are correct, and which DailyMail deliberately trusts — classify it New.
It is genuinely new *to Rowan*. It is not new to the reader.

### What was added

`src/dailymail/repeats.py` asks a second, separate question of announcements
Rowan calls New: *has this reader already been sent this?* It compares against
`db.delivered_history()` — announcements in a digest that was **actually
delivered**, not merely collected.

Hard requirements, every one a veto rather than a weight:

* normalized-identical title (emoji, curly quotes and punctuation folded)
* same category and same audience
* body similarity at or above 0.85
* event compatibility: two non-events, or two events at the **same date and
  start time**. A different date or time, or an event compared to a non-event,
  is never a repeat.

Then, by tier:

| Similarity | Extra evidence | Result |
|---|---|---|
| ≥ 0.995 | same links | repeat, confidence 0.99 |
| ≥ 0.97 | same links and same submitter | repeat, confidence 0.92 |
| ≥ 0.85 | same submitter, same contact, and at most 2 of the prior body's distinctive words removed | repeat **and updated**, confidence 0.80 |
| below 0.85 | — | stays New |

That last requirement is what separates a *revision* from a *substitution*. Two
announcements can share a title and 88% of their words and still be different
things — `SPSS licence renewal` and `Mathematica licence renewal` differ exactly
in the words that matter. A repost may gain words freely and must lose almost
none.

Validated against the whole production corpus: 12 true positives, zero false
positives. The pairs correctly left alone include `Coming soon:` versus `Now
showing: Diane Burko's Extraction` (identical bodies, different titles), the two
Alumni Breakfast announcements (identical bodies, different titles), the SPSS
and Mathematica renewals, and every Planetarium show.

### Display semantics

Rowan's own classification is **never overwritten**. `daily_records.status`
keeps it and stays queryable; a new `daily_records.display_status` column holds
what the digest shows. `digest_rows()` exposes both as `source_status` and
`status`. `repeat_matches` records the matched SubmissionId, the method, the
confidence, the body similarity and the evidence.

A materially changed repost also raises the sticky `changed` flag, so it renders
as `STANDING` + `UPDATED` — Rowan gave it a fresh SubmissionId, so the normal
version diff sees a first sighting and no badge, yet relative to what the reader
was sent it did change.

If detection fails for any reason the day is left exactly as Rowan classified
it. That is the safe direction: the reader sees a repeat rather than losing an
announcement.

## 7. Render-time cleanups

Both are presentation defects, so both are fixed in the rendering derivative and
nowhere else. The stored `full_body` is authoritative and is never rewritten.

### Duplicate title

Rowan bodies very often open by repeating their own subject as an `<h2>`, which
the card already shows as its headline. Two headlines can precede the body: the
card's subject, and — when an event earned one — the calendar callout's event
name. A first body block that exactly repeats either is suppressed.

The rule is deliberately narrow. Normalization accounts for whitespace, HTML
entities, curly versus straight quotes, case and trivial trailing punctuation.
Suppression is refused when the block contains a link, an image, or any text the
headline does not already contain. A first paragraph that merely *starts* with
the title and then says something is kept in full.

The plain-text alternative is checked independently, because an entity or a
wrapper tag can make one match and not the other.

### Redundant category

The New section is already grouped under a category heading, so repeating the
category on every card inside that group is noise. Standing is one globally
ranked list with no headings, so a Standing card keeps its label.

This is context-aware rendering, not data deletion: `category_title` is
untouched in the data model, and `show_category` defaults to true, so a card
rendered outside a group keeps its category.

## 8. Configuration

```toml
[calendar]
enabled = true
relevance_threshold = 0.6      # raise to be stricter, lower for more buttons
max_actions = 8
attach_ics = true
base_address = "201 Mullica Hill Rd, Glassboro, NJ 08028"
base_latitude = 39.70791
base_longitude = -75.11288
base_campus = "glassboro"
travel_enabled = true
walk_max_metres = 1200
walking_speed_mps = 1.3
walk_padding_minutes = 3
drive_arrival_padding_minutes = 10
drive_return_padding_minutes = 5
travel_rounding_minutes = 5
travel_min_minutes = 10
travel_max_minutes = 90
routing_enabled = true
routing_url = "https://router.project-osrm.org"
routing_timeout_seconds = 6
venue_cache_days = 180
```

## 9. Storage

Schema version **3**. Additive and idempotent, exactly like the v2 parking
upgrade: `CREATE TABLE IF NOT EXISTS` plus guarded `ALTER TABLE ... ADD COLUMN`.
No existing table is rewritten and no announcement content is read, so a failure
leaves v2 intact.

| Table / column | Purpose |
|---|---|
| `event_venues` | Normalized venue → coordinate, provenance, travel mode, outbound/return minutes, distance, routing source and fingerprint, verification dates |
| `calendar_recommendations` | One row per `(date, announcement)`: candidate evidence, offer decision, relevance score/reason/method/model, attendance mode, title, event start/end, timezone, location, venue, travel, mechanism, action URL, ICS filename and size, validation status, withheld reason |
| `repeat_matches` | Matched prior SubmissionId, family key, method, confidence, body similarity, materially-changed flag, evidence |
| `daily_records.display_status` | What the digest shows; `status` keeps Rowan's own classification |
| `runs.calendar_stats` | One JSON object of per-run calendar counters |

The same announcement on the same date produces byte-identical calendar data:
UIDs derive from the announcement's content hash and `DTSTAMP` from the digest
date, so nothing depends on wall-clock time.

## 10. Security

Every Phase 1–3 boundary is intact and none is widened.

* The curation payload gains only the extracted event block — date, times,
  location, attendance hint, title hint and the body's own heading — all
  email-redacted and length-bounded, and all checked by the existing recursive
  `assert_payload_is_clean()`.
* Claude still runs with `--tools ""`, `--strict-mcp-config`,
  `--disable-slash-commands`, `--no-session-persistence`, in an empty temporary
  directory, with `GMAIL_*` stripped from its environment.
* Calendar URLs and ICS data are generated deterministically **after**
  structured-output validation. The model never produces either.
* Announcement text is untrusted. It is quoted verbatim in the description, as
  the digest already quotes it — but it can never become a date, a time, a
  location, or a link. Only URLs DailyMail placed deliberately (the source's own
  registration links and the official Rowan page) are rendered as clickable in
  the appointment body; every other URL in announcement prose stays inert
  escaped text, matching how the email itself treats a bare URL.
* Calendar text is stripped of control characters before it reaches an
  iCalendar property, an attachment filename, or a URL, so neither MIME nor
  header injection is reachable. Newlines inside announcement text are escaped
  per RFC 5545, never emitted raw.

## 11. Operational metrics

`uv run dailymail status` gains one calendar line and one repeat line, and
`run-daily` reports per-run counters:

```
calendar: candidates=4 offered=1 withheld=3 travel=1
          venue(hit=1,miss=0,unresolved=0) routes=0 0.008s
repeats:  3 announcement(s) shown as Standing (already delivered under a
          previous SubmissionId)
```

`dailymail render --date <d>` reports the same, names each offered event, and
reports how many duplicate title blocks were suppressed. A preview never calls
the model and never makes a route lookup: it replays the stored judgement and
resolves venues from the cache, so a historical date renders identically every
time. `--no-calendar` skips calendar enrichment entirely.
