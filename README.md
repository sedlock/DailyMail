# DailyMail

A deterministic collector for [Rowan Announcer](https://apps.rowan.edu/RowanAnnouncer/)
announcements. Eventually this will produce one information-dense daily digest
email; right now it collects and validates the data that digest will be built from.

## Status

| Phase | Scope | State |
|---|---|---|
| 0 | Reconnaissance of the live application | complete — `docs/site-reconnaissance.md` |
| 1 | Validated read-only collector, normalized daily dataset | complete — `docs/collector-architecture.md` |
| 2+ | SQLite history, change detection, Claude curation, Outlook email, scheduling | not started |

## Quick start

```sh
uv sync

uv run dailymail collect                      # today, in America/New_York
uv run dailymail collect --date 2026-08-20    # a specific date
uv run dailymail inspect --date 2026-08-20    # summarize what was collected

uv run pytest                                 # 157 tests, no network required
```

Successful output:

```
COLLECT OK date=2026-08-20 employees=13 students=3 unique=13 new=5 standing=8 categories=33
```

Collections are written to `$XDG_STATE_HOME/dailymail/collections/YYYY-MM-DD.json`
(default `~/.local/state/dailymail/collections/`) — outside the repository, and
never committed.

## How it works, briefly

Rowan Announcer is an OutSystems React single-page app: the served HTML contains
no announcement data, and its UI renders only the first 20 announcements behind a
"Load More" button. So the collector talks to the app's own JSON screen service,
`ActionGetHomeData`, which returns complete bodies and metadata plus an explicit
`TotalCount` — two requests per day, one per audience, no browser.

Three things are worth knowing up front:

* **A stale `apiVersion` returns HTTP 200 with an empty payload.** That looks
  exactly like a quiet news day. The collector checks `hasApiVersionChanged` on
  every response, rediscovers its tokens and retries once, then fails loudly. It
  never reports that condition as "no announcements".
* **`apps.rowan.edu` serves an incomplete certificate chain.** It omits the
  InCommon intermediate. That intermediate is vendored and added to the public
  roots; verification is never disabled.
* **The API over-exposes PII** (Banner IDs, usernames, `Last_Login`, a `Password`
  key). The data model is an allowlist, and the drop policy is enforced at
  runtime before anything is written.

Full detail in `docs/collector-architecture.md`.

## Repository layout

```
src/dailymail/            the collector
  certs/                  vendored InCommon intermediate + provenance
docs/
  site-reconnaissance.md    Phase 0 report
  site-reconnaissance.json  Phase 0 machine-readable findings
  collector-architecture.md Phase 1 design
artifacts/reconnaissance/ sanitized fixtures used by the test suite
tools/recon/              Phase 0 probes, kept for manual diagnostics only
tests/                    157 tests, fixture- and mock-driven
```

## Scope discipline

This project touches a live university system. The collector is read-only by
construction: it can build exactly one request shape, never fetches announcement
detail pages (which would trigger Rowan's own visitor-log write), and never calls
any of the write endpoints catalogued in `docs/site-reconnaissance.md` §4.3.
