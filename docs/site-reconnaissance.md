# Rowan Announcer — Site Reconnaissance Report

**Phase:** 0 (reconnaissance only — no production code written)
**Date of investigation:** 2026-08-21
**Target:** `https://apps.rowan.edu/RowanAnnouncer/`
**Host:** entropy (Linux), Playwright/Chromium 151 + direct HTTP probes
**Scope compliance:** read-only. No logins, submissions, approvals, edits, or
other write actions were performed against Rowan systems. See
§13.11 for one unavoidable side effect of viewing a detail page.

---

## 1. Executive findings

1. **The application is an OutSystems Reactive (React) single-page app.** The
   HTML delivered for `/Home` is a ~2.3 KB shell containing
   `<div id="reactContainer"></div>` and `<noscript>JavaScript is required</noscript>`.
   **There is zero announcement data in the served HTML.** Naive HTML parsing of
   the index page is impossible, not merely inconvenient.

2. **A single JSON endpoint returns everything we need.**
   `POST /RowanAnnouncer/screenservices/RowanAnnouncer/MainFlow/Home/ActionGetHomeData`
   returns, for one audience and one date: the complete announcement list with
   **full HTML bodies**, all contact/submitter/approver metadata, event fields,
   category objects, **the full list of distribution dates per announcement**,
   an explicit `TotalCount`, and per-category counts. No detail-page fetches are
   required at all.

3. **It is callable directly with plain HTTP — no browser, no login, no cookies.**
   The only requirement is an `X-CSRFToken` header. Its value is a hardcoded
   OutSystems platform constant (`AnonymousCSRFToken`) published in
   `scripts/OutSystems.js`. No authentication is required to read any
   announcement content.

4. **The browser UI is *lossy*: it renders only the first 20 announcements**
   behind a "Load More" button, and scrolling does **not** auto-load more. On
   2025-03-05 the server reported 27 announcements and the UI rendered 20.
   A DOM-scraping collector would have silently missed 7. This is the single
   strongest argument against Playwright DOM extraction.

5. **New vs Standing is computable deterministically with no history at all.**
   Each record carries `DistributionDates` — the explicit list of every date the
   announcement runs. Hypothesis tested against your known-good Rowan email for
   2026-08-20: `min(DistributionDates) == target_date` ⟺ Rowan treats it as
   "New". **This held for all 13 records on that date, with zero false positives
   and zero false negatives.** SQLite history therefore becomes a *cross-check
   and change-detector*, not the primary classifier — and there is no cold-start
   problem.

6. **Audience is an authoritative field, not an inference.** `Submission.Audience`
   ∈ `{Employees, Students, Both}`. Tested across the entire archive
   (5,985 distinct announcements): the field predicted Employee-view and
   Student-view membership with **0 mismatches**. `Both` is exactly your
   "Everyone" case.

7. **There is a built-in completeness check.** `sum(Categories[].Count)` equalled
   `TotalCount` in **every** query tested, across both audiences and multiple
   dates. This gives a free, server-computed cross-foot on every collection run.

8. **Historical dates work perfectly and remain complete.** `CurrentDate=2026-08-20`
   returned all 5 subjects from your reference Employee email plus the 8 standing
   items, and every detail record was still available.

9. **Two silent-failure modes exist and must be defended against** (§12):
   a stale `apiVersion` returns **HTTP 200 with `data: {}`**, and an invalid
   `Audience` value silently returns **only the `Both` subset** instead of erroring.

10. **`apps.rowan.edu` serves an incomplete TLS chain.** The leaf is genuine
    (Rowan University, issued by `InCommon RSA Server CA 2`) but the server omits
    the intermediate, so stock `curl`/`requests` fail with
    "unable to get local issuer certificate". The correct fix is to pin the
    intermediate, **not** to disable verification. See §2.5.

---

## 2. Application architecture discovered

### 2.1 Platform
| Property | Value |
|---|---|
| Platform | OutSystems Reactive Web (React front end) |
| Module name | `RowanAnnouncer` |
| Theme module | `Rowan_TH` |
| App key | `e765d296-318e-4c77-b5de-738354fd07e6` |
| Module key | `e1a885c7-412b-45f5-b532-2c4c27f79958` |
| Home screen | `MainFlow.Home` |
| Rendering | 100% client-side; server returns a static SPA shell |
| JavaScript required | **Yes** for the UI; **No** for the JSON API |
| Editor used for bodies | CKEditor 5 Classic (constrains authored HTML) |
| Analytics | Google Analytics `G-Q3PR2WYYHW`, plus OutSystems ECT |

### 2.2 Version tokens (these rotate on redeploy)
| Token | Observed value | Source |
|---|---|---|
| `moduleVersion` | `EEtZtRyG_45VdflpWRQexA` | `GET /moduleservices/moduleinfo` → `.manifest.versionToken` |
| `indexVersionToken` | `793HFACiHRbhpHsEJd5tgw` | `OSManifestLoader.indexVersionToken` in `/` HTML |
| `apiVersion` (GetHomeData) | `C1CrfYuM0JxcZElpjPJRPw` | `scripts/RowanAnnouncer.MainFlow.Home.mvc.js` |
| `AnonymousCSRFToken` | `T6C+9iB49TLra4jEsMeSckDMNhQ=` | `scripts/OutSystems.js` |

All four are **discoverable at runtime**, so the collector can self-heal across
Rowan redeployments instead of shipping hardcoded values. `tools/recon/02-api-probe.py`
demonstrates the discovery.

### 2.3 Token sensitivity (measured)
| Manipulation | Result |
|---|---|
| Correct `apiVersion` | HTTP 200, full data, `hasApiVersionChanged: false` |
| Wrong/empty `apiVersion` | **HTTP 200, `data: {}`, `hasApiVersionChanged: true`** — silent failure |
| Wrong `moduleVersion` | HTTP 200, **full data returned**, `hasModuleVersionChanged: true` (tolerated) |
| `X-CSRFToken` header absent | HTTP **403**, `{"exception":{"message":"Invalid Login"}}` |
| `X-CSRFToken` header present but bogus value | HTTP 200, **full data** — value is not validated, only presence |

### 2.4 Authentication
No authentication is required to read announcements. A `Login` link exists and
`ActionDoLogin` / `IdP_SSO_URL` endpoints are present (SSO), but all
announcement content is anonymously readable. No cookies are set by the app
during anonymous browsing — no `Set-Cookie` was observed on any response.

### 2.5 TLS chain defect (production-relevant)
```
leaf:         CN=apps.rowan.edu, O=Rowan University
intermediate: CN=InCommon RSA Server CA 2, O=Internet2   <-- NOT SENT BY SERVER
root:         CN=USERTrust RSA Certification Authority   <-- present in system store
```
`openssl s_client` reports `Verify return code: 21 (unable to verify the first
certificate)`. The intermediate is published at the leaf's AIA URI
`http://crt.sectigo.com/InCommonRSAServerCA2.crt`.

**Recommendation:** ship the InCommon intermediate as a pinned CA bundle
(system roots + that one intermediate) and keep full verification on. Do **not**
use `verify=False` / `ignoreHTTPSErrors` in production. Verified working:
`curl --cacert <bundle>` → `HTTP 200 ssl_verify_result=0`.

---

## 3. URL / query behaviour

### 3.1 Observed URL → API parameter mapping
Confirmed by capturing the outbound `ActionGetHomeData` body while loading each URL.

| URL | → `Filters.Audience` | → `Filters.StartDate` | → `SelectedCategories` | Result |
|---|---|---|---|---|
| `/Home` | `""` | `1900-01-01` | `[]` | TotalCount 0 |
| `/Home?Audience=Employees` | `Employees` | today | `[]` | TotalCount 13 |
| `/Home?Audience=Students` | `Students` | today | `[]` | TotalCount 8 |
| `/Home?Audience=Employees&CurrentDate=2026-08-20` | `Employees` | `2026-08-20` | `[]` | TotalCount 13 |
| `/Home?Audience=Students&CurrentDate=2026-08-20` | `Students` | `2026-08-20` | `[]` | TotalCount 3 |
| `/Home?Audience=Employees&CurrentDate=2026-08-20&Category=5` | `Employees` | `2026-08-20` | `[5]` | TotalCount 3 |
| `/Home?Audience=Employees&CurrentDate=1999-01-01` | `Employees` | `1999-01-01` | `[]` | TotalCount 0 |
| `/Home?Audience=Bogus` | `Bogus` | `1900-01-01` | `[]` | TotalCount 0 |
| `/Home?Audience=Employees&CurrentDate=not-a-date` | `Employees` | **today** | `[]` | TotalCount 13 |

**Findings:**
* `CurrentDate=YYYY-MM-DD` **is honoured** and maps directly to `Filters.StartDate`.
* An unparseable `CurrentDate` **silently falls back to today** — never assume the
  page reflects the date you asked for; always compare the echoed date.
* `Audience` is required. Bare `/Home` yields no audience and no results.
* `Category=<id>` **does** appear in the URL and maps to `SelectedCategories`
  (note: *not* the `CategoryIds` field, which stayed empty in every observation).
* Announcement detail URL: `/RowanAnnouncer/Announcement?SubmissionId=<ID>` (public).
* `/RowanAnnouncer/Announcement_Details?SubmissionId=<ID>` returns **404** — it is
  an internal screen, not a public route.

### 3.2 Pagination / lazy loading
* The API is paginated via `StartIndex` + `MaxRecords`. The UI always requests
  `StartIndex: 0, MaxRecords: 20`.
* **The UI does not infinite-scroll.** Six full scrolls to page bottom triggered
  **no** additional `ActionGetHomeData` call. Additional records sit behind an
  explicit **"Load More"** control.
* Measured on 2025-03-05 (Employees): server `TotalCount = 27`, DOM rendered
  **20** `SubmissionId` links. **7 announcements were absent from the DOM.**
* `MaxRecords` is honoured to arbitrary size: `MaxRecords: 100000` on a
  2020→2030 range returned all **5,125** records in a single response.
* `StartIndex` beyond the end returns 0 records with `TotalCount` still correct.
* `MaxRecords <= 0` fails loudly: `{"exception":{"message":"Error executing query."}}`.

---

## 4. Network / backend endpoints

### 4.1 The one endpoint we need
```
POST https://apps.rowan.edu/RowanAnnouncer/screenservices/RowanAnnouncer/MainFlow/Home/ActionGetHomeData
Content-Type: application/json; charset=UTF-8
Accept: application/json
X-CSRFToken: T6C+9iB49TLra4jEsMeSckDMNhQ=      <-- presence required; value unvalidated
(no cookies, no auth)

{
  "versionInfo": {
    "moduleVersion": "EEtZtRyG_45VdflpWRQexA",
    "apiVersion":    "C1CrfYuM0JxcZElpjPJRPw"
  },
  "viewName": "MainFlow.Home",
  "inputParameters": {
    "Filters": {
      "Audience": "Employees",              // "Employees" | "Students"
      "StartDate": "2026-08-20",            // the target date
      "EndDate": "1900-01-01",              // sentinel = single-day mode
      "CategoryIds":        { "List": [], "EmptyListItem": {} },
      "SelectedCategories": { "List": [], "EmptyListItem": 0  },
      "keywords": ""
    },
    "StartIndex": 0,
    "MaxRecords": 20,
    "IsCategoryUpdate": false
  }
}
```

**Response** (`application/json; charset=utf-8`):
```
{
  "versionInfo": { "hasModuleVersionChanged": false, "hasApiVersionChanged": false },
  "data": {
    "Categories":    { "List": [ {Id,Title,Count,Rank,Color,Selected} x33 ] },
    "Announcements": { "List": [ <announcement record> ... ] },
    "TotalCount":    "13"
  },
  "rolesInfo": ","
}
```

### 4.2 Date-range mode
Setting `EndDate` to a real date switches to an inclusive **range** query
(matches any announcement with at least one distribution date in range):

| StartDate | EndDate | TotalCount |
|---|---|---|
| 2026-08-20 | 1900-01-01 (sentinel) | 13 |
| 2026-08-01 | 2026-08-31 | 141 |
| 2026-01-01 | 2026-12-31 | 1,756 |
| 2020-01-01 | 2030-12-31 | 5,125 |

Range mode still returns **each SubmissionId at most once** (141 records / 141
unique IDs) — it does not emit one row per distribution date.

### 4.3 Other endpoints discovered (complete inventory)
Harvested by scanning all 116 JS assets in the manifest for
`callServerAction` / `callDataAction` / `ScreenDataSet` declarations.

**Read-only, safe:**
| Endpoint | apiVersion |
|---|---|
| `MainFlow/Home/ActionGetHomeData` | `C1CrfYuM0JxcZElpjPJRPw` |
| `MainFlow/Announcements/ActionGetActiveCategories` | `IXIuCWKtXoXdaRePqmn2Sg` |
| `MainFlow/Announcement/ScreenDataSetGetCompositeSubmission` | `9D6MJemN7BWSrfbp2c1dZw` |
| `MainFlow/Announcement/ScreenDataSetGetDistributionDatesBySubmissionId` | `4FH19u5OeIj8+DoNkPF_Xg` |
| `MainFlow/Announcement/ScreenDataSetGetCategoryListById` | — |
| `MainFlow/Announcement/ActionBinaryDataToText` | `n5z0sC15lkHjxiRWqo+3gQ` |
| `MainFlow/Announcement_Details/ScreenDataSetGetSubmissionById` | — |
| `Common/Menu/DataActionCheck{Approver,Admin,SuperAdmin}Role`, `...CheckSubmitPermission` | various |

**WRITE / privileged — must never be called by DailyMail:**
`MainFlow/Announcement/ActionSaveSubmission`,
`MainFlow/Announcement/ActionUpdateSubmissionStatus`,
`MainFlow/Announcements/ActionUpdateSubmissionStatus`,
`MainFlow/Announcement/ActionCreateApproversForSubmission`,
`MainFlow/Announcement/ActionGetAIRewriteResult`,
`MainFlow/Announcement/ActionSaveVisitorClicks`,
`MainFlow/Home/ActionSaveVisitorClicks`,
`MainFlow/EmailAdmin/ActionSendDailyMail`,
`MainFlow/EmailAdmin/ActionTest_DistributeExtraEditionByDate`,
`MainFlow/AllEntities/ActionImportLegacyAnnouncements`,
`MainFlow/AllSubmissions/ActionTest_CreateSubmissionForEachCategory`,
`MainFlow/CategoryForm/ActionSaveCategory`,
`MainFlow/CategoryAdmin/ActionSaveAlertMessage`,
`MainFlow/CategoryOrganizationForm/ActionSave*` / `ActionDelete*`,
`Common/Login/ActionDoLogin`, `Rowan_TH/Common/UserInfo/ActionDoLogout`.

Note `MainFlow/EmailAdmin/ActionSendDailyMail` — Rowan's own daily-mail sender.
Never touch it.

### 4.4 No GraphQL / no REST-per-resource
No GraphQL endpoint, no RESTful resource URLs, no form POSTs for reading. The
only structured data source is the OutSystems screen-service RPC layer above.

---

## 5. Employee versus Student behaviour

Measured over the whole archive (`2020-01-01` → `2030-12-31`):

| Set | Count |
|---|---|
| Employee view total | 5,125 |
| Student view total | 4,067 |
| Present in **both** views (`Audience == "Both"`) | 3,207 |
| Employee-only (`Audience == "Employees"`) | 1,918 |
| Student-only (`Audience == "Students"`) | 860 |
| Union of both views | 5,985 |

**`Submission.Audience` fully determines view membership — 0 mismatches / 5,985.**
The server filter is effectively `Audience == <requested> OR Audience == 'Both'`.

Consequences for DailyMail:
* Your `Everyone` class is exactly `Audience == "Both"` — directly readable.
* Two API calls per day (one per audience) are sufficient.
* Diffing the two result sets is still worth doing as a **validation** control
  (§12.6), because it must agree with the `Audience` field.

Single-day comparison, 2026-08-20:
* Employees: 13 (IDs 6476, 6492, 6496, 6529, 6538, 6572, 6578, 6591, 6602, 6606, 6622, 6623, 6625)
* Students: 3 (IDs 6492, 6622, 6623) — all `Both`
* Student-only that day: none

---

## 6. Announcement identity behaviour

| Question | Answer | Evidence |
|---|---|---|
| Is `SubmissionId` stable? | **Yes.** Integer PK, exposed as a string. | IDs 6..6641; 5,125/5,125 unique in one range query |
| Same ID in both audience views? | **Yes**, when `Audience == "Both"`. | 3,207 IDs appear in both views |
| Can one announcement have multiple distribution dates? | **Yes** — explicit `DistributionDates.List`. | e.g. 6602 → `[2026-08-20, 2026-08-31, 2026-09-08, 2026-09-16]` |
| Does the same ID recur across dates? | **Yes**, that is exactly the Standing case. | 6529 appears on Aug 18/19/20/21 |
| Are distribution dates contiguous? | **No** — arbitrary submitter-chosen dates. | 6538 → `[08-13, 08-20, 08-27, 09-03]` |
| Does an edited announcement keep its ID? | **Yes.** 283/1,756 records in 2026 have `UpdatedDate`/`UpdatedByName` set and retain their original `Id`. `OldSubmissionId` stays `0`. | — |
| Other stable identifiers? | `OldSubmissionId` — non-zero on exactly **40** legacy-imported records (e.g. `Id=72 → OldSubmissionId=35940`). Not a general-purpose key. | — |

**Recommended primary key:** `SubmissionId`.
**Recommended per-day grain:** `(SubmissionId, distribution_date, audience_view)`.
**New vs Standing:** `min(DistributionDates) == target_date → New`, else `Standing`.

### 6.1 New/Standing validation against your reference email (2026-08-20)
| SubmissionId | first distribution date | # dates | predicted | Rowan email said |
|---|---|---|---|---|
| 6572 | 2026-08-20 | 4 | New | **New** ✓ |
| 6602 | 2026-08-20 | 4 | New | **New** ✓ |
| 6622 | 2026-08-20 | 3 | New | **New** ✓ |
| 6623 | 2026-08-20 | 4 | New | **New** ✓ |
| 6625 | 2026-08-20 | 1 | New | **New** ✓ |
| 6476 | 2026-08-04 | 3 | Standing | (not listed as new) ✓ |
| 6492 | 2026-08-06 | 4 | Standing | (not listed as new) ✓ |
| 6496 | 2026-08-17 | 4 | Standing | (not listed as new) ✓ |
| 6529 | 2026-08-18 | 4 | Standing | (not listed as new) ✓ |
| 6538 | 2026-08-13 | 4 | Standing | (not listed as new) ✓ |
| 6578 | 2026-08-17 | 4 | Standing | (not listed as new) ✓ |
| 6591 | 2026-08-17 | 4 | Standing | (not listed as new) ✓ |
| 6606 | 2026-08-18 | 4 | Standing | (not listed as new) ✓ |

**13/13 correct.** All 5 of your reference subjects were classified New and no
standing item was misclassified.

---

## 7. Complete field inventory

Every field below comes from the **single** `ActionGetHomeData` response — no
detail-page fetch needed. Presence measured over the 1,756 Employee-visible
announcements with a 2026 distribution date.

### 7.1 Announcement wrapper
| Field | Type | Presence | Notes |
|---|---|---|---|
| `FullBody` | HTML string | Always | Rendered announcement body |
| `ShortBody` | plain text | Always | Truncated teaser used on the index card |
| `SelectedCategory` | bool | Always | UI filter artefact, not content |
| `DistributionDates.List` | array of `YYYY-MM-DD` | Always | **The Standing/New key** |
| `Category` | object | Always | See §7.4 |
| `Submission` | object | Always | See §7.2 |
| `User` | object | Always | **PII — see §13.4. Do not store.** |

### 7.2 `Submission` object
| Field | Type | Presence | Notes |
|---|---|---|---|
| `Id` | string(int) | **Always** | The `SubmissionId`. Primary key |
| `Title` | string | **Always** | Subject. Often has leading/trailing spaces — trim |
| `SubmissionBody` | base64 string | **Always** | Base64 of the HTML body |
| `Category` | int | **Always** | FK to category Id |
| `Audience` | enum | **Always** | `Employees` \| `Students` \| `Both` |
| `SubmittedStatus` | string | **Always** | `Approved` for 100% of 5,125 public records |
| `SubmittedDate` | ISO8601 Z | **Always** | |
| `ApprovedDate` | ISO8601 Z | **Always** | |
| `SubmittedByName` | string | **Always** | PII |
| `SubmittedByDepartment` | string | **Always** | Often includes org code, e.g. `(S001627)` |
| `SubmittedByJobTitle` | string | 99.9% | |
| `SubmittedByEmail` | string | **Always** | PII |
| `SubmittedByPhone` | string | 20.3% | Optional |
| `SubmittedById` | int | **Always** | Internal user FK |
| `SubmittedByExternalId` | string | **Always** | **Banner ID (9 digits) — PII, do not store** |
| `ApprovedByName` | string | **Always** | |
| `ApprovedByDepartment` | string | **Always** | |
| `ApprovedByJobTitle` | string | **Always** | |
| `ApprovedByEmail` | string | **Always** | |
| `ApprovedByPhone` | string | 42.0% | Optional |
| `ApprovedById` | int | **Always** | |
| `ApproverExternalId` | string | **never set** (0/1756) | |
| `ContactName` | string | 85.6% | Optional |
| `ContactDepartment` | string | **Always** | |
| `ContactJobTitle` | string | 71.6% | Optional |
| `ContactRowanEmail` | string | **Always** | Often a departmental mailbox |
| `ContactPhone` | string | **Always** | |
| `Event` | bool | 19.4% true | Gate for all event fields |
| `EventName` | string | 19.4% | Only when `Event` |
| `EventDate` | `YYYY-MM-DD` | 19.4% | Sentinel `1900-01-01` when absent |
| `EventStartTime` | `HH:MM:SS` | 19.4% | Sentinel `00:00:00` when absent |
| `EventEndTime` | `HH:MM:SS` | 16.9% | **Often unset even for events** |
| `EventLocation` | string | 19.4% | |
| `EventNoEnd` | bool | never true | |
| `UpdatedDate` | ISO8601 | 16.1% | **Native edit marker** |
| `UpdatedByName` | string | 16.1% | |
| `UpdatedById` | int | 16.1% | |
| `OldSubmissionId` | int | 40 records total | Legacy import linkage |
| `ExtraEdition` | bool | never true | Special-edition flag. Still never true as of 11 Sep 2026 across 9,638 records; §13.3 |
| `ExtraEditionDateSent` | datetime | never set | |
| `IsDeleted` | bool | never true | Server filters deleted rows out |
| `QuestionForApprover` | string | 2.5% | Workflow chatter, not content |
| `ReadyForSubmission` | bool | never true | |
| `RejectComment` / `RejectReasonId` | | never set | Rejected items aren't public |

### 7.3 Sentinel values (must be normalised to NULL)
`1900-01-01`, `1900-01-01T00:00:00`, `00:00:00`, `0`, `""`.

### 7.4 `Category` object
`Id` (int), `Title`, `Rank`, `Is_Active` (bool), `RestrictSubmitting` (bool),
`Color` (design-system token, e.g. `orange-darker`), `ApprovalProcess`
(e.g. `Organization`).

The top-level `data.Categories.List` uses a slightly different shape:
`Id`, `Title`, `Count` (string int), `Rank`, `Color`, `Selected`.

### 7.5 Fields *not* available anywhere
No view/click counts, no expiry date distinct from distribution dates, no
attachments collection (images are inlined into the body), no tags/keywords
field, no explicit "New"/"Standing" flag (it is derived — §6.1).

---

## 8. Category behaviour

* **33 categories** currently exposed. Full ID→title map is in
  `docs/site-reconnaissance.json` under `category_mappings`.
* Categories **do have numeric IDs**, and the ID→name mapping is returned in
  every `ActionGetHomeData` response — no separate call needed.
* Categories are **provided dynamically by the backend**. New categories will
  appear automatically; the collector must upsert, never hardcode.
* **Zero-count / inactive categories are still listed.** On 2026-08-20 Employees:
  33 listed, only 8 with a non-zero count.
* **Employee and Student views expose the identical 33-category set** — the ID
  set was byte-identical across both audiences and across 2025-03-05, 2026-08-20,
  and 2026-08-21. Only the per-category `Count` differs.
* **Category list does not vary by date.**
* `Count` is audience- and date-scoped, and:

  **`sum(Categories[].Count) == TotalCount` in every observation:**

  | Audience | Date | categories listed | non-zero | Σ counts | TotalCount |
  |---|---|---|---|---|---|
  | Employees | 2026-08-20 | 33 | 8 | 13 | 13 ✓ |
  | Students | 2026-08-20 | 33 | 3 | 3 | 3 ✓ |
  | Employees | 2026-08-21 | 33 | 9 | 13 | 13 ✓ |
  | Students | 2026-08-21 | 33 | 7 | 8 | 8 ✓ |
  | Employees | 2025-03-05 | 33 | 12 | 27 | 27 ✓ |
  | Students | 2025-03-05 | 33 | 11 | 24 | 24 ✓ |

* Category filtering is URL-addressable (`?Category=<id>` → `SelectedCategories`)
  but **DailyMail does not need it** — an unfiltered query returns everything.

---

## 9. Historical / date behaviour

* **Arbitrary historical and future dates are directly requestable** and the
  server honours `CurrentDate` / `Filters.StartDate` exactly.
* **2026-08-20 (required validation date): PASSED.** All five subjects from your
  reference Employee email were returned, with full bodies and metadata:

  | Subject | SubmissionId |
  |---|---|
  | Parking Lot O-1 Closure | 6622 |
  | Academic Integrity: Resources and Reminders for Faculty | 6625 |
  | Internal Limited Submission: Brain Research Foundation 2027 Seed Grant Program | 6572 |
  | Profs for a Purpose: Mini-Grant Proposals Open | 6623 |
  | You Belong Series & Certificate Launch Summit 2026 | 6602 |

  Plus 8 standing announcements the email would have listed separately.
  Detail pages for these IDs remain live.

* **Additional dates inspected:** 2026-08-21 (Emp 13 / Stu 8),
  2026-08-22 (6/3), 2026-08-23 (3/3), 2025-03-05 (27/24),
  1999-01-01 (0/0), 2030-06-15 (0/0). Future dates return real scheduled
  announcements — the archive spans 5,125 Employee-visible records from
  2020→2030.

* **Explicit zero-announcement state exists** in both the API (`TotalCount: "0"`,
  empty `Announcements.List`, **but `Categories` still fully populated**) and the
  UI ("No announcements for Jan 1, 1999 / Please select a different date range.").
  This is the foundation of the failure-vs-empty discriminator in §12.1.

---

## 10. Body / HTML characteristics

Measured over 1,756 bodies (all 2026 Employee-visible announcements).

* **Format: HTML** (CKEditor 5 output), not plain text or Markdown.
* **`FullBody` is byte-identical to `base64decode(SubmissionBody)` in 1,756/1,756
  records.** There is no hidden "original" markup — use `FullBody` and treat
  `SubmissionBody` as a redundant duplicate. **The body returned by the backend is
  the same body rendered in the browser.**
* **Tag frequency:**
  `span` 10,873 · `p` 10,818 · `strong` 6,808 · `li` 3,328 · `a` 2,578 ·
  `br` 1,782 · `u` 1,577 · `ul` 887 · `i` 767 · `h4` 165 · `h3` 100 ·
  `sup` 94 · `ol` 84 · `h2` 44 · `td` 37 · `img` 24 · `hr` 15 · `tr` 13 ·
  `figure` 10 · `table` 3 · `tbody` 3
* **Attributes:** `style` 13,355 · `href` 2,578 · `target` 2,158 · `rel` 2,158 ·
  `class` 217 · `src` 24.
* **Inline styles are pervasive** — 942/1,756 bodies contain `style=`. Mostly
  CKEditor colour/size declarations. Relevant to Outlook rendering.
* **Links: 2,572 absolute vs 5 relative.** The 5 non-absolute values are all
  *malformed author input* rather than true site-relative paths:
  `go.rowan.edu/2026researchday`, `rowan.edu/scca`, `Go.Rowan.edu/EduAdventures`,
  `go.rowan.edu/petpreparedness2026`, and one
  `file://rowanads.rowan.edu/home/whiting/Desktop/AFT/whiting@rowan.edu`.
  Others carry stray whitespace or a leading `&nbsp;`. **The collector must
  trim/normalise hrefs and rewrite scheme-less hosts to `https://`; the `file://`
  URL must be stripped.**
* **Images occur (23 bodies) and are almost entirely `data:` URIs** — inline
  base64 PNG/JPEG. Sizes are extreme: individual images up to **6.1 MB**, and the
  five largest bodies are 5.84 / 5.59 / 4.42 / 4.42 / 4.42 MB. Only 3 images used
  external `https://` URLs.
* **Tables occur** but are rare (3 bodies).
* **No `script`, `iframe`, `form`, `object`, `embed`, `svg`, `input`, `style`,
  `link`, `video`, or `audio` element appeared in any of the 1,756 bodies.**
  CKEditor's allow-list makes them unlikely, but bodies are authored content, so
  **sanitisation must still be enforced defensively rather than assumed**.
* **Payload sizes per day** (both audiences, single-day mode):

  | Audience | Date | records | JSON | of which `data:` images |
  |---|---|---|---|---|
  | Employees | 2026-08-20 | 13 | **3,866 KB** | 1,614 KB (41.8%) |
  | Students | 2026-08-20 | 3 | 17.9 KB | 0 |
  | Employees | 2026-08-21 | 13 | 86.0 KB | 0 |
  | Students | 2026-08-21 | 8 | 57.0 KB | 0 |

  A single day can therefore be a few MB, dominated by one or two inline images.
  This matters for both SQLite storage and Outlook email size limits.

* `&nbsp;` entities are common; `ShortBody` contains decoded ` `.
* Titles and bodies contain emoji (🍎, ✏️) and en/em dashes — the pipeline must be
  UTF-8 clean end to end.

---

## 11. Recommended production collection architecture

### Recommendation: **A — direct structured backend/API calls**, with a
### narrowly-scoped Playwright fallback used only for self-check (a light hybrid).

Two `ActionGetHomeData` POSTs per day (Employees + Students), single-day mode,
`MaxRecords` set well above any plausible day (e.g. 500), then validate.

### 11.1 Comparison against the stated criteria

| Criterion | A. API | B. HTTP + HTML parse | C. Playwright DOM | D. Hybrid |
|---|---|---|---|---|
| **Reliability** | **Best.** One documented RPC, stable typed JSON | **Impossible** — index HTML has no data | Fragile: CSS/DOM churn, timing | Good |
| **Completeness** | **Total.** All fields incl. `DistributionDates`, no detail fetches | n/a | **Poor — caps at 20 without clicking "Load More"** | Total |
| **Count validation** | **Excellent:** `TotalCount` + Σ category counts | n/a | Weak — must scrape counts from UI text | Excellent |
| **Resistance to UI change** | **High** — independent of markup | n/a | **Low** | High |
| **Execution time** | ~2 requests, sub-second | n/a | Browser launch + render, ~10-30 s/page | ~Same as A |
| **Complexity** | Low (one POST + token discovery) | n/a | High (browser, waits, clicking) | Low-moderate |
| **Auth requirements** | **None** (CSRF header presence only) | none | none | none |
| **Failure observability** | **Good, but needs explicit checks** (§12) | n/a | Poor — silently renders 20 | Best |
| **Risk of silently missing announcements** | **Low**, given §12 checks | n/a | **HIGH — demonstrated 7/27 missed** | **Lowest** |

### 11.2 Why not C
Option C fails on the criterion you care most about. It demonstrably returns 20
of 27 announcements with no error, and Playwright adds a browser dependency,
~30× the runtime, and sensitivity to every CSS change — while providing *less*
data (no `DistributionDates`, no approver metadata, truncated bodies on cards).

### 11.3 Why not B
Not a judgement call: the index HTML is a 2.3 KB React shell. There is nothing
to parse.

### 11.4 The recommended hybrid element (optional, low cost)
Keep a **weekly or on-alert** Playwright check that loads
`/Home?Audience=X&CurrentDate=<date>`, reads the rendered
`Announcements - <date>` header and the visible category chips, and confirms the
API's `TotalCount` and category counts agree with what a human would see. This
guards against the one thing the API cannot self-report: Rowan changing the
*meaning* of the filters. Do not put it in the daily critical path.

### 11.5 Implementation notes for Phase 1
* Discover all four tokens at runtime (§2.2); cache them, and re-discover on any
  `hasApiVersionChanged: true`.
* Pin the InCommon intermediate; keep TLS verification **on** (§2.5).
* Send a truthful, identifiable `User-Agent` and keep request volume trivial
  (2 requests/day).
* Use single-day mode (`EndDate = 1900-01-01`) for the daily run.
* **Never** call any endpoint in the write list (§4.3). Detail pages are not
  needed, which also avoids `ActionSaveVisitorClicks`.
* Store `FullBody` verbatim as the archival copy; sanitise only at render time.
* Plan for multi-MB `data:` URI images: strip or externalise them for the email
  (Outlook will not render a 6 MB data URI), while keeping a reference.

### 11.6 Explicitly **not** recommended
Claude vision or natural-language browser interpretation as the primary
collector. Deterministic extraction is fully available. Claude's role stays where
you scoped it: curation/ranking after collection.

---

## 12. Validation and failure-detection strategy

The central requirement — *distinguish "no announcements" from "the scraper
returned zero"* — is cleanly solvable, because a **genuine empty day still
returns a fully-populated `Categories` list**, whereas a broken call returns
`data: {}`.

### 12.1 The structural-validity gate (run first, every time)
Measured discriminator:

| Scenario | HTTP | `hasApiVersionChanged` | `data` keys | `TotalCount` | `Categories` |
|---|---|---|---|---|---|
| Healthy populated day | 200 | false | Announcements, Categories, TotalCount | `"13"` | 33 |
| **Genuine empty day** | 200 | false | Announcements, Categories, TotalCount | `"0"` | **33** |
| **Broken: stale apiVersion** | 200 | **true** | **[] (none)** | absent | **0** |

A response is **structurally valid** iff *all* of:
1. HTTP status == 200
2. `Content-Type` is JSON
3. `"exception"` **not** in response
4. `versionInfo.hasApiVersionChanged == false`
5. `"Categories"` present **and** `len(Categories) > 0`
6. `"TotalCount"` present and parses as a non-negative int
7. `"Announcements"` key present (list may legitimately be empty)

**Only if all 7 hold may `TotalCount == 0` be reported as "genuinely no
announcements today."** Otherwise it is a collector failure and must alert, not
send an empty email.

### 12.2 Count reconciliation (completeness)
* `len(Announcements) == int(TotalCount)` — else pagination was truncated.
* `sum(int(c.Count) for c in Categories) == int(TotalCount)` — independent
  server-side cross-foot; held in 6/6 observations.
* Every announcement's `Category` id must exist in the returned `Categories` list.
* If `len(Announcements) == MaxRecords`, treat as **suspected truncation** and
  re-query with a larger `MaxRecords` (or page) even if counts appear to agree.

### 12.3 Echo / parameter validation (catches the silent-audience bug)
An invalid `Audience` does **not** error — it silently returns only the `Both`
subset (measured: `Audience="Bogus"` on 2026-08-20 returned 3 records, exactly
the 3 `Both` items, instead of 13). Therefore:
* Assert the requested date appears in the UI header / that every returned record
  contains the requested date in `DistributionDates`.
* Assert every returned record's `Audience` ∈ {requested, `Both`}.
* Assert at least one record has `Audience == requested` on days where the other
  audience shows more items (or simply assert the audience literal is one of the
  two known-good values *before* sending the request).
* Reject unparseable dates client-side — the server silently substitutes today.

### 12.4 Identity validation
* `SubmissionId` must be unique within a single-day result (measured 13/13 and
  141/141 unique — duplicates are always a bug).
* `SubmissionId` must be a positive integer.
* Cross-run: a `SubmissionId` whose `min(DistributionDates)` changes between runs
  indicates either an edit or an upstream data change — flag it.

### 12.5 Required-field validation
Hard-fail a record missing any of: `Id`, `Title` (after trim), `Audience` ∈
{Employees, Students, Both}, `Category` resolvable, `FullBody` non-empty,
`DistributionDates` non-empty and containing the target date, `SubmittedStatus == "Approved"`.
Soft-warn on: empty `ContactRowanEmail`, `Event == true` with empty `EventName`
or sentinel `EventDate`.

### 12.6 Employee/Student parity
* Recompute the audience classification two ways and require agreement:
  (a) from the `Audience` field, (b) from set membership across the two calls.
  Measured agreement: 5,985/5,985. Any disagreement is a real anomaly.
* `|Both| = |Employee set ∩ Student set|`.

### 12.7 Change detection
* Store a hash of `(Title, FullBody, Category, Audience, Event*, Contact*, DistributionDates)`.
* Prefer the native signal: `UpdatedDate` / `UpdatedByName` (set on 16.1% of
  records) is Rowan's own edit marker. A hash change with no `UpdatedDate` change
  is worth flagging as suspicious (possible upstream mutation or our own parser drift).

### 12.8 Trend / sanity guards
* Alert if today's count is 0 **and** the trailing 14 days were all non-zero
  (statistically improbable on a weekday — note weekends legitimately drop:
  2026-08-23 was a Sunday with 3).
* Alert if any category count or total moves by an implausible factor.
* Alert on TLS verification failure specifically (distinguish it from HTTP error).
* Log the discovered token set each run; alert when `apiVersion` changes so a
  human can confirm nothing else moved.

### 12.9 Detail-page parity (optional)
Not required, since bodies come from the list endpoint. If desired as a spot
check, fetch `/Announcement?SubmissionId=<id>` for one record per run and
confirm the title/body match — but note this triggers Rowan's
`ActionSaveVisitorClicks` (§13.11), so keep it rare or omit it.

---

## 13. Known uncertainties

1. **Token rotation cadence is unknown.** `apiVersion` changes whenever Rowan
   republishes the module. We know the failure is loud *if* checked
   (`hasApiVersionChanged`) and that runtime re-discovery fixes it, but we have not
   observed an actual redeploy. Mitigation: discover-on-start + retry-once-on-change.
2. **`MaxRecords` upper bound / server-side cap is untested at scale.** 100,000
   worked on a 5,125-row range, but Rowan could add a cap later. Mitigation:
   always reconcile against `TotalCount` and page if short.
3. ~~**`ExtraEdition` semantics unverified.**~~ **ANSWERED, 11 September 2026.**
   Rowan issued an Extra Edition on 31 August 2026 (`A New Chapter for University
   Advancement`, from the President). It appears **nowhere** in
   `ActionGetHomeData` — not in single-day mode, not in range mode, and not in
   the whole 2020-2030 archive, which by then was 5,372 employee and 4,266
   student records with `ExtraEdition` false on every one and
   `ExtraEditionDateSent` set on none. The only read path that knows about the
   daily-mail distribution,
   `MainFlow/EmailAdmin/DataActionGetDailyMailAnnouncements`, refuses an
   anonymous caller with `NotRegisteredException: SuperAdmin2 role required`.
   So an Extra Edition bypasses `ActionGetHomeData` entirely and there is no
   deterministic public read source for one. Full evidence, and what DailyMail
   does about it, in `docs/extra-editions.md`.
4. **The API over-exposes PII.** Every record embeds a `User` object with
   `Username`, `External_Id` (9-digit Banner ID), `Last_Login`, and a `Password`
   key (empty in all observations), plus `SubmittedByExternalId`. **DailyMail must
   not persist these.** Fixtures in this repo have them stripped. This is arguably
   worth a courtesy report to Rowan IT — flagging for your decision, not acting on it.
5. **Timezone handling.** `SubmittedDate`/`ApprovedDate` carry `Z`;
   `DistributionDates` are bare dates. "Today" must be computed in Rowan's local
   timezone (America/New_York), not UTC, or the daily run will pick the wrong date
   near midnight. Not yet verified against a real midnight boundary.
6. **Whether `Audience` can be edited after publication** — if an announcement
   flips Employees→Both mid-run, our Everyone classification changes. Not observed.
7. **`rolesInfo: ","`** in every response is unexplained; harmless for anonymous reads.
8. **Weekend/holiday cadence** not fully characterised (2026-08-23 Sunday had 3).
   Needed before tuning the "suspicious zero" alert in §12.8.
9. **`ShortBody` truncation rule** (length, word boundary, entity handling) not
   reverse-engineered. Irrelevant if we always use `FullBody`.
10. Reconnaissance ran from a single host on one day; per-IP rate limiting was
    never triggered and therefore its thresholds are unknown. Daily volume is
    2 requests, so risk is minimal.

### 13.11 One side effect to disclose
Loading `/Announcement?SubmissionId=6622` in a browser (done twice during recon)
causes the app's own front end to POST `ActionSaveVisitorClicks`, writing to
Rowan's visitor log. This is ordinary behaviour for any visitor viewing a public
page, not a write we initiated deliberately — but it is a write, so it is
recorded here. **The recommended API-only collector avoids it entirely**, which is
an additional argument for Option A.

---

## 14. Recommended next implementation checkpoint

**Phase 1 should be deliberately narrow: a validated read-only collector that
writes JSON to disk. No database, no email, no scheduler, no Claude.**

Proposed Phase 1 scope:
1. A small `collect.py` that, for a given date (default: today in
   America/New_York), performs runtime token discovery, issues the two
   single-day `ActionGetHomeData` calls with pinned TLS, and writes raw +
   normalised JSON to disk.
2. Implementation of the §12.1 structural-validity gate and the §12.2 count
   reconciliation, with a non-zero exit code and a clear message on failure.
3. Normalisation: trim titles, map sentinels to NULL, resolve/normalise hrefs,
   classify `New`/`Standing` via `min(DistributionDates)`, classify audience as
   `Employee`/`Student`/`Everyone` via the `Audience` field, and drop the `User`
   object and `*ExternalId` fields at ingest.
4. A regression test that runs the normaliser against the committed sanitised
   fixtures — including `homedata-broken-apiversion.sanitized.json`, which must
   be *rejected*, and `homedata-empty-day.sanitized.json`, which must be accepted
   as a genuine zero.
5. Re-run against 2026-08-20 and assert the five known subjects are present and
   classified New — a permanent end-to-end acceptance test.

Deferred to later phases, as scoped: SQLite schema, change detection across runs,
Claude curation, Outlook HTML email (including a decision on the multi-MB inline
image problem), SMTP credentials, systemd scheduling.

**Open decision to make before Phase 2, not Phase 1:** how to handle inline
`data:` URI images in the email — strip, link back to the announcement, or
downscale and re-embed as CID attachments.

---

## Appendix A — reconnaissance artifacts

| Path | Contents |
|---|---|
| `docs/site-reconnaissance.md` | This report |
| `docs/site-reconnaissance.json` | Machine-readable findings |
| `artifacts/reconnaissance/fixtures/*.sanitized.json` | Committed, PII-stripped test fixtures |
| `artifacts/reconnaissance/endpoint-summary.md` | Endpoint/token summary |
| `tools/recon/01-capture-network.js` | Playwright network capture (Employee/Student) |
| `tools/recon/02-api-probe.py` | Direct API probe + runtime token discovery |
| `tools/recon/03-detail-capture.js` | Detail-page network capture |
| `tools/recon/04-urlparam-map.js` | URL-param → API-parameter mapping |
| `tools/recon/05-ui-states.js` | UI zero-state and Load-More/lazy-load probe |
| `tools/recon/06-make-fixtures.py` | Sanitised fixture generator (documents redaction policy) |

Uncommitted (local only, by design): raw HTML/network dumps and screenshots under
`artifacts/reconnaissance/{html,network,screenshots}/` — they contain the
unredacted `User` PII described in §13.4 and multi-MB base64 images. See
`.gitignore`.

All probe scripts under `tools/recon/` are disposable reconnaissance tooling, not
production code.
