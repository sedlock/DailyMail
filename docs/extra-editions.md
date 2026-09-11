# Extra Editions — a confirmed coverage gap

**Phase 6, 11 September 2026.** This document answers Phase 0's open question U3
(`docs/site-reconnaissance.md` §13.3) with evidence, and records exactly what
DailyMail does about the answer.

---

## 1. The reported miss

On 31 August 2026 Rowan sent employees an Extra Edition:

```
*** EXTRA EDITION - Mon Aug 31, 2026 ***
A New Chapter for University Advancement
```

From President Ali A. Houshmand, announcing John Zabinski's 1 October retirement
and Brittany Petrella serving as interim vice president for University
Advancement.

DailyMail's 31 August digest did not contain it. That digest reported 25 employee
and 15 student announcements and passed every validation gate, including V1,
which reconciles what we collected against the source's own `TotalCount`.

This is exactly the class of communication the digest exists to carry, so the
first question was whether the collector had lost it.

---

## 2. It was not collected, and it is not collectable

Re-run of the Phase 0 reconnaissance, read-only, on 11 September 2026. Every
request below is `ActionGetHomeData` or another `ScreenDataSet*`/`DataAction*`
**read** action. No write or send endpoint was called.

| Probe | Result |
|---|---|
| `ActionGetHomeData`, single-day mode, `StartDate=2026-08-31` | 24 employee + 14 student records. Not present. |
| `ActionGetHomeData`, **range** mode, `2026-08-31` → `2026-08-31` | Same set. Not present. |
| `ActionGetHomeData`, range mode, `2020-01-01` → `2030-12-31` | 5,372 employee + 4,266 student records. Not present under any wording. |
| `ExtraEdition` across that whole archive | `false` for **every** record. |
| `ExtraEditionDateSent` across that whole archive | set on **no** record. |
| Stored collection artifact for 2026-08-31 | 32 unique announcements, `extra_edition` false on all. |
| Production SQLite, all 304 stored versions | no match for the title, `Zabinski`, `Petrella` or `Houshmand`; `extra_edition = 0` on all. |

So the announcement does not exist in the only public data source DailyMail
reads, on any date, in any query mode. The collector did not lose it.

### 2.1 Where Extra Editions actually live

Scanning all 116 JavaScript assets in the live app manifest for declared screen
actions turns up exactly one read path that knows about the daily-mail
distribution:

```
MainFlow/EmailAdmin/DataActionGetDailyMailAnnouncements
```

Called anonymously, it answers:

```json
{"exception": {"name": "NotRegisteredException",
               "specificType": "RowanAnnouncer_CS.NotSuperAdmin2",
               "message": "SuperAdmin2 role required"}}
```

The only other endpoints that mention an Extra Edition are Rowan's own senders:

* `MainFlow/EmailAdmin/ActionTest_DistributeExtraEditionByDate`
* `MainFlow/EmailAdmin/ActionSendDailyMail`

Both are **write/send** actions. DailyMail must never call either, and cannot:
the client can construct exactly one request shape, and
`tests/test_extra_edition.py` asserts that the package contains no string
literal for any other screen-service path.

The app also declares a role check named `CheckExtraEditionRequesterRole`,
which is further evidence that requesting an Extra Edition is an authenticated
administrative action rather than a public one.

### 2.2 The conclusion

> **There is no deterministic, read-only, public source for Rowan Extra
> Editions.** The Extra Edition an employee receives by email is composed and
> sent behind a `SuperAdmin2` role and is never published to the announcement
> list DailyMail reads.

---

## 3. What was *not* built, and why

Closing this gap needs a new ingestion channel — reading the delivered mail from
a mailbox, or Rowan granting an authenticated read role. Both mean new
credentials and a new trust boundary, so neither was built here. From the task
brief: *"do NOT invent a Gmail/email-scraping subsystem or introduce new mailbox
credentials without user authorization."*

**The exact missing mechanism**, for a separate decision:

1. **Mail ingestion.** Read the recipient's own Rowan mailbox (IMAP or Graph),
   match `*** EXTRA EDITION - <date> ***` in the subject, and ingest the body as
   a synthetic announcement. Needs a mailbox credential DailyMail does not have
   and does not currently want, a second parser for a second HTML format, and an
   identity scheme, since an Extra Edition carries no `SubmissionId`.
2. **An authenticated read role.** Ask Rowan for a read-only service identity
   that may call `DataActionGetDailyMailAnnouncements`. Cleanest by far — it is
   structured data from the system of record, it dedupes by `SubmissionId` for
   free, and it needs no new parser — but it depends on Rowan, not on us.

Neither is started. This document is the record that the gap is known, bounded
and deliberate rather than overlooked.

---

## 4. What *was* built

Everything that does not require a new channel. All of it keys on Rowan's own
`ExtraEdition` boolean and on nothing else, so it is dormant today and correct on
the morning Rowan first sets that flag.

| Layer | Behaviour |
|---|---|
| Collection | `ExtraEdition` is allowlisted in `normalize.SUBMISSION_ALLOWLIST` and stored as `announcement_versions.extra_edition`. Unchanged from Phase 1. |
| `ExtraEditionDateSent` | Remains in `normalize.FORBIDDEN_KEYS`: workflow metadata, never persisted. |
| Status | `dailymail health --json` reports `metrics.database_extra_editions`. It is `0`, and that zero is now visible to an operator without writing a query. |
| Curation (Claude) | The payload carries `extra_edition`, and the system prompt says to rank such an item at or near the top of whichever section it is in. |
| Curation (fallback) | An Extra Edition sorts first within its category group, and gains a large enough Standing score to clear the whole ordinary range. Its rationale says `fallback: EXTRA EDITION, ...`. |
| Rendering | A compact `EXTRA EDITION` badge beside NEW/STANDING/UPDATED, and `EXTRA EDITION` in the plain-text alternative. |
| Sections and families | Unchanged. An Extra Edition is still New or Standing, still belongs to its logical family, still versions and dedupes normally. It is marked, not moved. |

### Identification is the source's, never ours

The badge is driven by the flag alone. An announcement whose *subject* says
`*** EXTRA EDITION - Mon Aug 31, 2026 ***` does not get one, because titles are
author-supplied and a badge inferred from wording is a badge any submitter can
mint. `test_the_badge_is_never_inferred_from_the_subject_line` asserts exactly
that.

---

## 5. The watch

Three checks will notice if the answer changes.

* `test_the_aug_31_extra_edition_is_not_in_any_collected_artifact` fails the day
  the exact announcement appears in a committed artifact.
* `test_no_extra_edition_has_ever_been_observed_in_the_committed_fixtures` fails
  the day a fixture carries the flag.
* `metrics.database_extra_editions` becomes non-zero the day production collects
  one.

Any of those firing means U3 has been answered differently and this document
needs rewriting — which is the point of writing it down.
