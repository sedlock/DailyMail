# Parking Enrichment (Phase 3 — production)

Companion to `docs/site-reconnaissance.md` (Phase 0), `docs/collector-architecture.md`
(Phase 1) and `docs/operations.md` (Phase 2), all of which remain accurate.

---

## 1. The problem

Rowan publishes parking announcements that are operationally useful and
geographically useless:

> **Parking Lot O-1 Closure**
> Parking Lot O-1 will be closed on Wednesday, August 19, 2026 at 10 pm. The lot
> will remain closed until Monday, August 24, 2026.

Nothing in that tells you where Lot O-1 is. DailyMail now adds one compact block
above the announcement body:

```
PARKING LOCATION

Lot O-1
Employee lot immediately west of James Hall, just south of Richard Wackar Stadium.
Open in Google Maps ›
```

The announcement text itself is untouched. The enrichment is purely additive.

---

## 2. Architecture

Parking geography is **reference data**, not announcement data. It changes on the
scale of years, it is shared across every announcement, and it lives in its own
SQLite tables that a parking refresh can rewrite without ever touching
announcement history.

```
announcement (title, derived body text, event location)
        |
        v
deterministic mention detector            regex + the alias cache; no model
        |
        v
parking cache lookup                      one SQL query per run
        |
        +-- hit ------------> enrich immediately            <- the normal case
        |
        +-- miss --> refresh the campus authoritative source
                        |
                        +-- resolved --> cache and enrich
                        |
                        +-- still unknown --> targeted resolver agent
                                                  |
                                                  +-- high confidence --> cache
                                                  |
                                                  +-- uncertain --> fallback link
```

**The expensive path is exceptional by construction.** A cached lot costs one
dictionary lookup: no HTTP request, no subprocess, no model, measured at a few
milliseconds for a whole digest. Once DailyMail knows where Lot O-1 is, it never
rediscovers it.

### Separation from curation

The daily curation call is unchanged: `--tools ""`, no MCP servers, ranking only,
and a payload that contains no parking field of any kind. Parking work happens in
separate invocations with separate tool policy, and only on bootstrap, on a cache
miss, or on a deliberate refresh. See §8.

---

## 3. Authoritative sources

The find that made this feature cheap: Rowan's official
[Main Glassboro Campus](https://www.rowan.edu/about/visiting/main.html) page
embeds a **Google My Maps** layer, and My Maps publishes any layer as KML at

```
https://www.google.com/maps/d/kml?mid=<MID>&forcekml=1
```

That KML has a `Parking` folder with one placemark per lot: the lot name in the
description (`Name: Lot O-1`), a coordinate, and the use class as the placemark
title (`Employee Parking`). It is machine-readable official geometry, so
Glassboro needs no geocoding at all and a refresh can discover a lot Rowan adds
tomorrow.

| Source id | Campus | Type | What we take from it |
|---|---|---|---|
| `glassboro-mymaps` | Glassboro | My Maps KML (`mid=1c2Qlz4nAV57oTio6HbOTgmYTwOoqimKW`) | **38 named lots and garages with coordinates and use class**, plus 79 building landmarks |
| `glassboro-parking-map-pdf` | Glassboro | Public Safety printable map (2025-2026) | Fingerprint; the reader-facing fallback link |
| `glassboro-parking-regulations` | Glassboro | Parking rules & regulations | Permit classification per lot (Glassboro and Stratford) |
| `stratford-mymaps` | Stratford | My Maps KML (`mid=1Sq4QEKv3l7nPp-chZUZpXq5lko4s3PEj`) | 5 building control points, 13 parking points (use class, **no lot letter**) |
| `stratford-som-campus-map` | Stratford | Rowan-Virtua SOM campus map PDF | The lot letters |
| `camden-mymaps` | Camden | My Maps KML (`mid=1YhmxFZP-QcEFuleJVKQZ-bgFN0qH2ryG`) | Building landmarks; **no parking folder exists** |
| `camden-cmsru-campus-map` | Camden | CMSRU Camden campus map PDF | The named garages |
| `camden-cmsru-visitors` | Camden | CMSRU visitor information | Which garage is public parking for the Medical Education Building |
| `sewell-mymaps` | Sewell | My Maps KML (`mid=1AhzykQJLby6YoadivTofklMIfpMNfsA`) | Two parking points, both unnamed — nothing addressable to cache |

Every source is fetched read-only over verified TLS. No Rowan write endpoint is
touched, no authenticated page is requested, and Google Maps itself is never
scraped — Rowan already publishes the geometry.

### Structured geometry: what exists and what does not

Rowan's My Maps parking placemarks are **points, not polygons**. The code
supports polygons (`parking.representative_point()` computes an interior point
rather than a naive centroid, so an L-shaped lot cannot place its marker on a
building), but Rowan supplies points, so the official point is used as published.

The Lot O-1 point was cross-checked against an independently surveyed outline of
the same lot: Rowan's coordinate falls **inside** that outline, 4 m from its
centroid. `tests/test_parking_core.py` keeps that check as a regression.

### Stratford and Camden: a hand derivation, recorded in code

Rowan publishes those layouts only as drawn maps, and the corresponding My Maps
parking placemarks carry a use class but no lot letter. Neither source alone
answers "where is Stratford Lot D-3". They were combined once, by hand, and the
derivation is recorded in `src/dailymail/parking_reference.py`:

1. Text-extract the official campus-map PDF with per-label page coordinates.
2. Least-squares fit an affine page→WGS84 transform using that campus's own
   My Maps **building** placemarks as control points.
3. Check it — residuals at the control points, plus a prediction against a
   landmark not used in the fit.
4. Match each transformed lot label to the nearest official My Maps **parking**
   placemark. Within 40 m with a consistent use class, store the official Rowan
   point (authoritative, and certainly inside the lot). Otherwise store the
   transformed position and mark the record `low`.

Stratford control-point residuals: 2.2 / 2.7 / 3.1 / 8.1 / 9.3 m. Nine of eleven
Stratford lots take their coordinate from an official Rowan parking placemark.

Camden has only two Rowan buildings on the CMSRU map, so a two-point north-up
isotropic fit was used (1.283 m per page unit) and validated 12 m against a
landmark outside the fit. Two of the four Camden garages agree within 17 m with
independently surveyed garage footprints; those are `medium`, the rest are `low`.

Because step 1 needs a human, a hand-derived source whose fingerprint later
changes is **flagged for review and its records are left alone** — never silently
regenerated. `parking-status` shows the flag; `parking-refresh` prints
`REVIEW: hand-derived source(s) changed upstream`.

---

## 4. Schema (version 2)

Additive migration, transactional and idempotent: `CREATE TABLE IF NOT EXISTS`
plus one `ALTER TABLE runs ADD COLUMN parking_stats`. No existing table is
rewritten and no announcement history is touched.

| Table | Purpose |
|---|---|
| `parking_locations` | One row per facility. Canonical id, campus, canonical + normalized name, type, permit class, description, lat/lon, provenance, confidence, active flag, `manual_override` / `override_fields`, first-discovered / last-verified / last-changed |
| `parking_aliases` | Every spelling we will match, normalized, `UNIQUE (campus, normalized_alias)`, with a `scannable` flag |
| `parking_sources` | Per-source fingerprint, version, last retrieved/verified/changed, status, error, lot count |
| `parking_landmarks` | Named campus features from the same layers, for description evidence and campus disambiguation — cached so both work offline |
| `announcement_parking_locations` | Audit trail: which announcement referenced which lot on which date, how it matched, at what confidence |
| `parking_unresolved` | Candidates that could not be resolved, with attempt and resolver-call counts and the last reason |

`runs.parking_stats` holds one JSON object of per-run counters.

### Canonical identity

```
glassboro:lot:o-1        glassboro:lot:a        stratford:lot:a
camden:garage:medical-school                    glassboro:garage:rowan-boulevard
```

**Campus is part of the identity, deliberately.** `Lot A` exists at Glassboro
*and* Stratford; `Lot B`, `Lot C`, `Lot E` and `Lot G` collide too. A lot name
alone is never an answer.

`UNIQUE (campus, normalized_name)` on locations and `UNIQUE (campus,
normalized_alias)` on aliases mean a within-campus lookup is always unambiguous,
while cross-campus collisions are allowed — they are exactly what campus
disambiguation exists to resolve.

---

## 5. Alias matching

Most of the variation lives in the **normalizer** rather than in extra rows, so a
new spelling usually needs no new data:

```
Lot O-1  ->  lot o 1        Lot O1   ->  lot o 1        LOT O—1  ->  lot o 1
Rowan Blvd. Parking Garage  ->  rowan boulevard parking garage
411 Ellis St.               ->  411 ellis street
```

Rows are still generated for the reorderings and prefixes Rowan actually uses:
`Parking Lot O-1`, `Lot O-1`, `O-1 Lot`, `Lot O1`, `O1`, `O-1`.

A `scannable` flag decides which aliases may be swept across free announcement
text. An alias qualifies only if it has a real word (`Rowan`, `Chestnut`,
`Ellis`) or a `Lot`/`Garage` anchor **plus** a digit. So `lot o 1` and
`rowan boulevard garage` are swept; `lot a` and `o 1` are not, because ordinary
English produces them by accident — "a lot a few blocks away" would otherwise
match Lot A.

---

## 6. Mention detection

Deterministic and case-sensitive, over the announcement title, derived plain-text
body, and the event location when the announcement is an event.

| Pattern | Example |
|---|---|
| `(Parking )?Lot <CODE>` | `Parking Lot O-1`, `Lot A`, `Lot 301` |
| `<CODE> (Parking )?Lot` | `O-1 Lot` |
| `Lots <CODE>, <CODE> and <CODE>` | `Lots A, B-1 and C-1` |
| `<Name> (Parking )?Garage` | `Rowan Boulevard Garage`, `Townhouse Parking Garage` |
| bare `Parking Garage` | resolved only if the campus has exactly one garage |
| any scannable cached alias | `Edgewood Park Apartments Lot`, `411 Ellis Street` |

`<CODE>` is `[A-Z]{1,2}(-?\d{1,2})?` or three digits.

**Case matters, and it is what stops the false positives.** `Lot` must be
capitalized and so must the code, so the real body text of announcement 6622 —
*"The lot will remain closed until Monday"* — does not match, and neither does
"there is a lot of interest", "lots of students", or "parking is available in the
lot behind the building". `tests/test_parking_core.py` pins all of them.

A bare **single-letter** code that is not in the catalog (`Lot I will be closed`)
is recorded as a miss and then dropped: it is far more likely to be prose than a
lot Rowan forgot to publish, so it never triggers a source refresh or a resolver
call. Codes with a digit or two letters do escalate.

Repeated mentions of one lot collapse to one callout, including across spellings:
`Parking Lot O-1` in the title, `Lot O-1` in the body and `O-1 Lot` in a footer
are one lot, once.

---

## 7. Campus disambiguation

In priority order, and it refuses to guess:

1. **Category** — `Glassboro Campus` → glassboro, `Stratford Campus` → stratford,
   `CMSRU` → camden.
2. **A named building or facility** — `Rowan Medicine` → stratford,
   `Richard Wackar Stadium` → glassboro, `Rowan Boulevard Garage` → glassboro.
   Sourced from `parking_landmarks`, so it needs no network. A name that occurs
   on two campuses is dropped rather than allowed to mislead.
3. **A campus keyword** — `glassboro`, `stratford`, `camden`, `cmsru`,
   `rowan-virtua`, `laurel road`, …

Two campuses' worth of evidence yields **no** campus. If exactly one cached
location fits, it is used; if more than one does, the lot is left unresolved and
the reader gets the official campus map link instead. `Lot O-1` needs no campus
evidence at all, because only Glassboro has one — that is not a guess.

---

## 8. Cache-miss resolution

1. **Refresh** the authoritative source for the inferred campus (all campuses if
   none was inferred), at most once per run.
2. **Retry** deterministic resolution.
3. **Targeted resolver**, at most once per candidate per run.
4. **Cache only on adequate evidence**, otherwise fall back.

### The targeted resolver

The one place web access is granted:

```
claude --print --model <parking model> --output-format json
       --json-schema <resolver schema>
       --tools "WebSearch,WebFetch"
       --allowedTools WebSearch WebFetch
       --strict-mcp-config --disable-slash-commands --no-session-persistence
       --system-prompt <resolver instructions>
```

It runs in an empty temporary directory with `GMAIL_*` stripped. It receives, on
stdin and nothing else:

* the parking candidate and its normalized keys
* the campus candidates and the evidence behind them
* a ≤600-character announcement excerpt with email addresses redacted
* the facility names already cached for those campuses
* the authoritative source URLs

It never receives SMTP credentials, the recipient, contact/submitter/approver
identities, Banner IDs, raw announcement HTML, database access, mail tools,
shell, or the curation context. A recursive assertion refuses to send the payload
if it contains a forbidden key, an email address, or a data URI.

Its answer is cached only if **all** of these hold: `resolved` is true,
confidence is at least `medium`, the campus is one the announcement evidence
allows, there is a canonical name, the coordinates are finite and inside Rowan's
service area, and at least one `https://` source is cited. Otherwise the lot is
recorded unresolved and the digest continues. Coordinates are never estimated and
a description is never invented.

Announcement text and fetched web pages are untrusted input, stated in the system
prompt and — more importantly — enforced structurally: a resolver answer that
contradicts the campus evidence is rejected, so injected text can only cause a
rejection, never a wrong lot.

### Description generation

Cached descriptions are written by a separate, **no-tools** invocation from
evidence computed deterministically from official coordinates: the nearest named
landmarks within 400 m, each with its distance in metres and compass direction
from the lot. The model phrases; it does not gather and it does no geometry.

Every sentence is then validated before it is stored, and rejected if it is
shorter than 6 or longer than 30 words, restates a coordinate, contains marketing
language or a URL, or — the important one — **names a proper noun that was not in
the supplied evidence**. That grounding check, not the prompt, is what stops an
invented landmark reaching the reader. A rejected description leaves the field
null, and a lot with no description shows the fallback rather than a guess.

---

## 9. Refresh policy

| Trigger | Behaviour |
|---|---|
| Bootstrap | `uv run dailymail parking-refresh` |
| Cache miss | The relevant campus source is refreshed immediately, once per run |
| Staleness | A source not verified for **180 days** is refreshed at the start of the next run that detects a parking mention |
| Manual | `parking-refresh`, optionally `--campus` or `--source` |

**The fingerprint is what makes this cheap.** Every source is SHA-256 hashed on
retrieval. If the bytes are unchanged, `last_verified_at` moves forward and
nothing else happens: no re-upsert, no description regeneration, no model call. A
second `parking-refresh` reports `locations_touched=0` and every source
`unchanged`.

A description already in the cache is never replaced by a source refresh, and a
manually pinned field is never replaced at all.

---

## 10. Google Maps links

Stored coordinates, generated link — never an opaque share URL, because those
cannot be regenerated, verified or corrected from stored data:

```
https://www.google.com/maps/search/?api=1&query=<LAT>,<LON>
```

The coordinate is the facility itself, not a nearby building. Lot O-1:

```
https://www.google.com/maps/search/?api=1&query=39.712482,-75.120453
```

---

## 11. Email rendering

The callout sits between the announcement's title/metadata and its original
body. Outlook-safe nested presentation tables, every style inline, no flexbox, no
background image, no web font. Rowan brown `#57150B` for the label and the link,
gold `#FFCC00` as a 3 px left rule only — a note, not a competitor to the
NEW/UPDATED badges above it.

`Open in Google Maps ›` is an `inline-block` anchor with real padding, so it is a
proper tap target on a phone.

The plain-text alternative carries the same information:

```
PARKING LOCATION
  Lot O-1 -- Employee lot immediately west of James Hall, just south of Richard Wackar Stadium.
  Map: https://www.google.com/maps/search/?api=1&query=39.712482,-75.120453
```

Multiple lots render once each in mention order, capped at
`parking.max_callouts` (default 6). An unresolved lot gets one compact line:

```
Parking location could not be resolved automatically.
View the official Glassboro campus parking map ›
```

**Parking is not part of the digest content hash.** The hash identifies the
announcement content shown, and enrichment is reference data *about* the
announcement. Keeping it out means a parking-catalog refresh cannot look like a
content change and cannot disturb delivery idempotency.

Curation ranking is deliberately **not** influenced by parking. The established
ranking model is unchanged and the curation payload gains no parking field.

---

## 12. Failure behaviour

Parking enrichment is useful, not critical.

| Failure | Behaviour |
|---|---|
| Authoritative source unreachable | Recorded on the source row; other sources still refresh; the lot falls back |
| Malformed source document | Recorded as a source error; cached records untouched |
| Hand-derived source changed upstream | Flagged `changed_review`; records left alone for a human |
| Resolver times out, errors, or returns unusable output | Recorded; the lot falls back |
| Description rejected by validation | Field left null; the lot falls back |
| Parking cache missing or corrupt | Enrichment produces nothing; the digest is unaffected |
| Any unexpected exception per announcement | Caught, recorded in metrics, that announcement simply has no callout |

`enrich_digest` never raises. **No operator `ATTENTION REQUIRED` alert is ever
sent for a parking problem alone** — an unresolved lot is a metric, not an
incident. An announcement is never suppressed because its parking could not be
resolved.

---

## 13. Cost and instrumentation

Per-run counters in `runs.parking_stats` and in the `run-daily` output:

```
parking: mentions=1 hits=1 misses=0 refreshes=0 agent=0 new=0 unresolved=0 0.004s
```

Tracked: mentions detected, cache hits, cache misses, source refreshes, resolver
calls, new resolutions, unresolved, announcements enriched, errors, runtime.

A normal cached day adds **zero HTTP requests, zero model calls and single-digit
milliseconds** to a ~95 s pipeline. The one-time bootstrap is nine HTTP GETs and
a handful of batched description calls. A cache miss costs one campus refresh
(three or four GETs); a resolver call is the only per-incident model cost, and it
happens once per unknown lot, ever, because the answer is then cached.

---

## 14. Correcting a parking record

Automated data is occasionally wrong. Fix it once and it stays fixed: an
overridden field is pinned and **no automated refresh will overwrite it**.

```sh
# See what is cached, and how it got there.
uv run dailymail parking-lookup "Lot O-1"

# Correct the description, the coordinates, or both.
uv run dailymail parking-set glassboro:lot:o-1 \
  --description "Employee lot immediately west of James Hall, just south of Richard Wackar Stadium." \
  --latitude 39.712482 --longitude -75.120453

# Correct a classification or a name.
uv run dailymail parking-set glassboro:lot:h --permit-class Mixed
uv run dailymail parking-set stratford:lot:c --location-type patient_lot

# Promote a low-confidence record you have verified yourself.
uv run dailymail parking-set stratford:lot:g --confidence medium

# Teach it a spelling Rowan uses that we do not generate.
uv run dailymail parking-set glassboro:lot:o-1 --alias "Lot O1 (James Hall)"

# Retire a lot Rowan has closed permanently, or bring it back.
uv run dailymail parking-set glassboro:lot:k --inactive
uv run dailymail parking-set glassboro:lot:k --active

# Hand a field back to automated refresh.
uv run dailymail parking-set glassboro:lot:o-1 --clear-overrides
```

Overridable fields: `canonical_name`, `location_type`, `permit_class`,
`description`, `latitude`, `longitude`, `confidence`, `is_active`. Coordinates are
validated against Rowan's service area, so a transposed or zeroed pair is
refused. `parking-lookup` prints `MANUAL OVERRIDE on: [...]` for any pinned
record, and `parking-status` counts them.

To make a lot show up when it currently falls back, give it a description and a
coordinate and set `--confidence medium` or higher: `parking.min_confidence`
(default `medium`) is the bar for rendering.

---

## 15. Commands

```sh
uv run dailymail parking-status                     # counts, campuses, sources, misses
uv run dailymail parking-status --list              # every cached facility
uv run dailymail parking-status --campus glassboro
uv run dailymail parking-lookup "Lot O-1"           # full record + generated map URL
uv run dailymail parking-lookup glassboro:lot:o-1
uv run dailymail parking-refresh                    # re-check every source
uv run dailymail parking-refresh --campus glassboro
uv run dailymail parking-refresh --source glassboro-mymaps
uv run dailymail parking-refresh --no-descriptions  # no Claude call at all
uv run dailymail parking-set <canonical-id> ...     # manual correction (see §14)

uv run dailymail render --date 2026-08-20 --out /tmp/preview   # preview the callout
uv run dailymail render --date 2026-08-20 --no-parking         # without it
```

`parking-status` reports locations, aliases, coordinates and descriptions per
campus, counts by type and permit class, manual overrides, unresolved candidates
with their reasons, the oldest verification timestamp, the last refresh, and the
status of every source.

---

## 16. Tests

Hermetic. Snapshots of all four My Maps layers live in
`artifacts/parking/fixtures/`, so the entire refresh path — fetch, fingerprint,
parse, upsert, describe — runs with no network and no subprocess. Both agents are
driven through injected runners.

Live probes are opt-in and separate:

```sh
DAILYMAIL_LIVE_PARKING=1 uv run pytest tests/test_parking_live.py -v
```

They check that every source is still reachable, that the Glassboro layer still
parses to a full lot list, that Lot O-1 has not moved, that the five Stratford
control points are still where the derivation assumed, that the Camden layer
still has no parking folder, and that a generated Maps URL still resolves.

---

## 17. Schedule

The production timer runs at **06:30 America/New_York**, every day, with
`Persistent=true`, `AccuracySec=1s` and **no** `RandomizedDelaySec` — the
requested time is the delivery time, so jitter that pushes the run materially
later is not wanted. See `docs/operations.md` §1.

---

## 18. Known limitations

* Rowan's parking geometry is points, not polygons. Polygon handling exists and
  is tested, but nothing currently supplies one.
* Stratford and Camden coordinates come from a recorded hand derivation (§3), not
  from machine-readable Rowan geometry. Four records are `low` confidence and do
  not render: `stratford:lot:d-6`, `stratford:lot:g`,
  `camden:garage:camden-county-college`, `camden:garage:sheridan`.
* Rowan's parking regulations name Stratford permit lots **F** and **H** that
  appear on neither current map. No coordinate exists for them, so they are not
  cached — a mention would fall back to the Stratford campus map.
* Sewell's two parking placemarks are unnamed, so nothing addressable can be
  cached for that campus.
* Stratford `Lot C`'s use class is left `Unknown`: the campus map lists it as
  permit parking and the regulations page omits it.
* A hand-derived source that changes upstream needs a human to re-derive it. The
  system detects and reports this; it cannot fix it by itself.
* A lot referenced only by a description ("the lot behind Bunce Hall") is not
  detected. Detection is name-based on purpose.
