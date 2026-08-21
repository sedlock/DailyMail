# DailyMail Collector Architecture (Phase 1)

Companion to `docs/site-reconnaissance.md` (Phase 0), which remains the record of
how the target application was reverse-engineered. This document describes what
was built and why.

**Phase 1 scope:** a deterministic, validated, read-only collector that produces
one normalized daily dataset on disk. No database, no email, no scheduling, no
Claude. Those are later phases.

---

## 1. Collector architecture

One endpoint, two requests per day, no browser.

```
                       ┌─ GET moduleservices/moduleinfo        ─┐
  discovery (§2) ──────┼─ GET …/RowanAnnouncer.MainFlow.Home.mvc.js
                       └─ GET …/OutSystems.js                  ─┘
                                    │  moduleVersion / apiVersion / CSRF token
                                    ▼
  collection      POST …/MainFlow/Home/ActionGetHomeData   × {Employees, Students}
                  (single-day mode, paginated by StartIndex/MaxRecords)
                                    │
                     ┌──────────────┴──────────────┐
                     ▼                             ▼
             structural gate V1            per-audience gates V2-V8
                     │                             │
                     └──────────────┬──────────────┘
                                    ▼
                          cross-audience parity V9
                                    ▼
                    allowlist projection + normalization
                                    ▼
                    runtime drop-policy audit (assertion)
                                    ▼
        $XDG_STATE_HOME/dailymail/collections/YYYY-MM-DD.json
```

### Why this shape

Phase 0 established that the browser UI renders only the first 20 announcements
behind a "Load More" button and does not infinite-scroll: on 2025-03-05 the
server reported 27 and the DOM held 20. A DOM scraper would have silently dropped
7 announcements. The JSON endpoint, by contrast, returns complete bodies and full
metadata and reports its own `TotalCount`. So:

* **No DOM scraping.** The browser is never launched during collection.
* **No detail-page fetches.** `ActionGetHomeData` already returns `FullBody` and
  every contact/approver field. This also avoids triggering Rowan's own
  `ActionSaveVisitorClicks` write, which fires when a detail page is *viewed*.
* **No Claude.** Extraction is fully deterministic. Claude's role begins at
  curation in a later phase.

### Modules

| Module | Responsibility |
|---|---|
| `config.py` | Constants, the closed audience enum, sentinels, XDG paths |
| `tls.py` | SSL context: public roots + the intermediate Rowan omits |
| `discovery.py` | Runtime discovery of the three version tokens |
| `client.py` | The single POST, plus real `StartIndex`/`MaxRecords` pagination |
| `validate.py` | Gates V1-V9; the empty-day vs broken-collector discriminator |
| `normalize.py` | Allowlist projection, sentinels, New/Standing, body diagnostics |
| `collect.py` | Orchestration, cross-audience parity, artifact assembly/write |
| `cli.py` | `collect` / `inspect`, exit codes |
| `errors.py` | Typed failures with distinct exit codes |

The Phase 0 probes under `tools/recon/` are retained for manual diagnostics only.
Nothing in `src/dailymail/` imports them, and they are not on the daily path.

---

## 2. Runtime token discovery

Nothing deployment-specific is hardcoded. Rowan rotates these tokens on every
republish, and a stale `apiVersion` produces the worst possible failure: **HTTP
200 with `data: {}`**, which looks exactly like a quiet news day.

| Token | Discovered from |
|---|---|
| `moduleVersion` | `GET moduleservices/moduleinfo` → `.manifest.versionToken` |
| `apiVersion` | `RowanAnnouncer.MainFlow.Home.mvc.js`, regex on the `callServerAction("GetHomeData", …)` declaration |
| CSRF token | `OutSystems.js`, regex on `AnonymousCSRFToken = "…"` |

Both JS assets are resolved **through `manifest.urlVersions`**, so the collector
reads exactly the bundle revision the live app reads rather than whatever an
unversioned URL happens to serve.

### Stale-version handling

1. Every response is checked for `versionInfo.hasApiVersionChanged`.
2. If true, tokens are rediscovered and the collection is retried **once**.
3. If it is still true, the run fails with `VersionChangedError` (exit **7**).
4. That condition is **never** reported as an empty announcement day.

`hasModuleVersionChanged` is tolerated — Phase 0 showed Rowan still returns full
data — but it is recorded as a warning.

### Token confidentiality in logs

The CSRF token is a public OutSystems platform constant, not a credential, but
tokens are still never printed or persisted in full. The artifact and the CLI
carry a 12-hex-character SHA-256 **fingerprint** per token, plus a
`tokens_changed_since_prior_run` flag computed by comparing against the most
recent previous artifact. That comparison reuses the collection output; it
introduces no separate persistence.

---

## 3. TLS handling

`apps.rowan.edu` presents its leaf certificate and **omits the
`InCommon RSA Server CA 2` intermediate**, so a stock client cannot build a chain
and fails with "unable to get local issuer certificate" (OpenSSL code 21).

The fix is additive, not permissive:

```
certifi public roots  +  vendored InCommon RSA Server CA 2  =  trust store
```

The intermediate is vendored at `src/dailymail/certs/incommon-rsa-server-ca-2.pem`
with its provenance and SHA-256 fingerprint recorded in the adjacent `README.md`.
Its issuer (`USERTrust RSA Certification Authority`) is an ordinary public root,
so trust still terminates normally.

* `check_hostname` is `True` and `verify_mode` is `CERT_REQUIRED`.
* There is **no** code path that disables verification. A test greps the package
  for `verify=False`, `CERT_NONE`, `check_hostname=False`,
  `_create_unverified_context` and `ignoreHTTPSErrors`, and fails if any appears.
* TLS failure raises `TlsError` (exit **3**), deliberately distinct from
  `TransportError` (exit **5**), so a certificate problem is never mistaken for
  an outage.

---

## 4. Request shape

```
POST https://apps.rowan.edu/RowanAnnouncer/screenservices/RowanAnnouncer/MainFlow/Home/ActionGetHomeData
Content-Type: application/json; charset=UTF-8
Accept: application/json
X-CSRFToken: <discovered>
User-Agent: DailyMail/0.1 (Rowan Announcer digest; +contact: sedlock@rowan.edu) python-httpx
```

with `Filters.EndDate` pinned to the **single-day sentinel `1900-01-01`**, an
empty `SelectedCategories` (so nothing is filtered out), `StartIndex` walking in
`MaxRecords` steps, and `MaxRecords` defaulting to 100.

### Pagination

Rowan honours a very large `MaxRecords`, but the collector pages for real so no
day can silently exceed a hardcoded window:

1. Request `StartIndex=0`.
2. Read `TotalCount` from the first page and treat it as authoritative.
3. Keep requesting until the count of **unique** `SubmissionId`s equals
   `TotalCount`.
4. Fail loudly if a page returns no rows while short of the total, if a page adds
   no new records (non-advancing pagination), if `TotalCount` changes
   mid-pagination, or if `MAX_PAGES` is exceeded.

Most days need one request per audience.

---

## 5. Validation gates

The governing requirement: **never confuse "Rowan published nothing" with "the
collector broke"**. Those are indistinguishable by count and trivially
distinguishable by structure.

| Scenario | HTTP | `hasApiVersionChanged` | `data` | `TotalCount` | `Categories` |
|---|---|---|---|---|---|
| Populated day | 200 | false | present | `"13"` | 33 |
| **Genuine empty day** | 200 | false | present | `"0"` | **33** |
| **Stale apiVersion** | 200 | **true** | `{}` | absent | **0** |

A real quiet day still returns the complete category registry. That is the tell.

### V1 — structural gate (fatal, every response)

HTTP 200 · JSON content type · no `exception` · `hasApiVersionChanged == false` ·
`Categories` present and non-empty · `TotalCount` present and a non-negative
integer · `Announcements` present. Only after all seven hold may
`TotalCount == 0` be reported as a genuine zero.

`TotalCount` is accepted as a string (what Rowan sends) or an int, but rejected
if it is a bool, a float, empty, non-numeric or negative.

### V2 — count reconciliation (fatal)

* unique `SubmissionId`s collected == `TotalCount`
* `sum(category.Count)` == `TotalCount` — an independent, server-computed
  cross-foot that held in every Phase 0 observation

### V3 — truncation suspicion (fatal)

A result exactly one page long against a larger `TotalCount` is treated as
incomplete pagination rather than a coincidence.

### V4 — audience validation (fatal)

The request enum is closed to `Employees` and `Students`. An unrecognised value
is refused **before it reaches the wire**, because Rowan answers it with only the
`Both` subset instead of an error. Every returned record's `Audience` must be the
requested value or `Both`.

### V5 — date validation (fatal)

The date is parsed and canonicalised locally first: Rowan silently substitutes
its own current date for malformed input, so `2026-8-20` or `today` would
otherwise produce a confidently wrong collection. Then every returned record must
actually list the requested date in its `DistributionDates`.

### V6 — identity integrity (fatal)

`SubmissionId`s unique within the day, each a positive integer.

### V7 — required fields (fatal per record)

`Id`, non-empty trimmed `Title`, `Audience` in enum, `FullBody` non-empty,
non-empty `DistributionDates`, `SubmittedStatus == "Approved"`, and a
`Category` that resolves against the registry the backend returned in the *same*
response.

### V8 — soft checks (warn)

Missing contact email; an event with no name or a sentinel event date. Recorded,
never fatal.

### V9 — Employee/Student parity (fatal)

The `Audience` field and set membership are computed independently and must agree
exactly:

* `{id : Audience == "Both"}` == `employee_ids ∩ student_ids`
* Employee-only records appear only in the Employee view, and vice versa
* every record marked for an audience appears in that audience's view

Phase 0 measured zero disagreements across 5,985 announcements, so any
disagreement is a genuine anomaly.

### V12 / V13 — transport and token drift

TLS verified against the pinned bundle; token fingerprints logged and compared
against the previous run.

### Exit codes

| Code | Meaning |
|---|---|
| 0 | Success (including a validated genuine zero-announcement day) |
| 2 | Usage error (bad `--date`, bad `--page-size`, missing artifact) |
| 3 | TLS verification/handshake failure |
| 4 | Token discovery failure |
| 5 | HTTP/transport failure |
| 6 | Validation gate failure |
| 7 | `apiVersion` still stale after rediscovery and one retry |

---

## 6. New versus Standing semantics

For target date **D**:

```
New       when min(DistributionDates) == D
Standing  otherwise
```

Each announcement carries the explicit list of every date it runs, so this is
computed **from the source data with no history**. Phase 0 validated the rule
against Rowan's own Employee Daily Mail for 2026-08-20: 13/13 correct, including
all five subjects Rowan listed as new, with no false positives or negatives.

`first_distribution_date` is retained alongside `status`.

This is deliberately **not** "first observed by this program". When SQLite arrives
it will track first-observed independently, as a cross-check and change detector —
not as the source-facing classifier. That also means there is no cold-start
problem: a first-ever run classifies correctly.

Known caveat: if Rowan were to edit an announcement's distribution dates after
publication, the derived status could shift between runs. The artifact records
`first_distribution_date`, so a later phase can detect that.

---

## 7. Audience semantics

`Submission.Audience` is authoritative, and Phase 0 verified it predicts view
membership exactly (0 mismatches / 5,985 records). The server filter is
effectively `Audience == <requested> OR Audience == 'Both'`.

| Source value | Normalized label |
|---|---|
| `Employees` | `Employee` |
| `Students` | `Student` |
| `Both` | `Everyone` |

Both memberships are retained per record in `source_query_membership` (which of
the two queries actually returned it) so the field and the set membership can be
cross-validated by V9 rather than one being trusted blindly.

---

## 8. Dynamic category behaviour

Categories are backend-driven and must never be hardcoded.

* The full registry — including zero-count and inactive categories — is returned
  in every `ActionGetHomeData` response and captured verbatim into
  `category_registry` (`id`, `title`, `rank`, `color`, and `is_active` where
  present).
* An unknown or brand-new category ID is **valid data** and never fails a
  collection. Only a category that cannot be resolved against the registry
  returned in the same response is an error (V7).
* `Count` is audience- and date-scoped, so it is recorded separately in
  `category_counts_by_audience` rather than baked into the registry.
* Newly observed categories are reported by diffing against the most recent prior
  artifact, surfacing in `new_categories_since_prior_run` and as a warning. This
  reuses existing output; no persistence was added for it.

Phase 0 saw 33 categories, identical across audiences and dates. The collector
does not assume that number.

---

## 9. Sensitive-field policy

The Rowan API over-exposes personal data. Every record embeds a `User` object
containing a username, a 9-digit Banner ID (`External_Id`), a `Last_Login` and a
`Password` key, and each submission carries `SubmittedByExternalId`.

**The data model is an allowlist, not a denylist.** Only fields named in
`normalize.SUBMISSION_ALLOWLIST` / `WRAPPER_ALLOWLIST` leave the ingestion layer,
so anything Rowan adds in future is dropped by default.

### Retained

`submission_id` · `title` (outer whitespace trimmed only) · `category_id` and the
full `category` object · `source_audience` and `audience_label` ·
`distribution_dates` · `first_distribution_date` · `status` ·
`source_query_membership` · `full_body` (verbatim) · `short_body` · `body_text`
(derived) · `body_diagnostics` · `submitted_status` · `submitted_date` ·
`approved_date` · `updated_date` · `updated_by_name` ·
`submitted_by_{name,department,job_title,email,phone}` ·
`approved_by_{name,department,job_title,email,phone}` ·
`contact_{name,department,job_title,email,phone}` ·
`is_event` · `event_{name,date,start_time,end_time,location,no_end}` ·
`extra_edition`.

The names, departments, titles, emails and phone numbers **are** retained: they
are part of the announcement as Rowan publishes it and will appear in the curated
email. `extra_edition` is carried purely as a watch flag for Phase 0's open
question U3 (it was false for all 5,125 archive records).

### Dropped, never persisted or logged

`User` (whole object) · `External_Id` / `SubmittedByExternalId` /
`ApproverExternalId` (Banner IDs) · `SubmittedById` / `ApprovedById` /
`UpdatedById` · `Password` · `Last_Login` · `Username` · `rolesInfo` ·
`versionInfo` · `QuestionForApprover` · `ReadyForSubmission` · `RejectComment` ·
`RejectReasonId` · `IsDeleted` · `SelectedCategory` · `ExtraEditionDateSent` ·
`OldSubmissionId` · `SubmissionBody`.

`SubmissionBody` is dropped because Phase 0 proved it is byte-identical to
`FullBody` in 1,756/1,756 records — it is pure duplication.

### Enforcement

* `normalize.assert_no_forbidden_fields()` walks the finished artifact **at
  runtime**, before it is written. The guarantee is enforced, not merely tested.
* Raw API responses are never written to disk, never logged, and never embedded
  in exception messages. Server exceptions surface only the platform's own short
  `message` string.
* Exactly **one** artifact is written per run. There is no raw/unredacted copy.
* Tests assert the policy against a deliberately hostile synthetic record
  containing every dropped field with a realistic value — the Phase 0 fixtures
  already have them stripped, so testing against fixtures alone would prove
  nothing.

---

## 10. Body handling

`FullBody` is the authoritative source HTML and is preserved **verbatim**. Phase 1
does not sanitize it, rewrite links, or touch images.

Deliberately deferred to the renderer in a later phase:

* HTML sanitization
* malformed and scheme-less link correction (Phase 0 found 5 such hrefs)
* stripping unsafe `file://` links
* Outlook-compatible styling
* image handling

`body_text` is derived separately for inspection and later Claude curation: tags
dropped, entities decoded, `script`/`style` content discarded, block boundaries
turned into newlines and table cells space-separated so words do not weld
together.

`body_diagnostics` records `body_bytes`, `image_count`, `data_uri_count`,
`data_uri_bytes` and `link_count`. Measurement only — Phase 1 does not resize,
extract, CID-attach, strip or transform inline images. Phase 0 found single
`data:` URI images up to 6.1 MB, which is a Phase 2 email-design problem.

---

## 11. Sentinel normalization

| Sentinel | Meaning | Applied to |
|---|---|---|
| `1900-01-01`, `1900-01-01T00:00:00`, `…Z` | not set | `event_date`, `updated_date`, `submitted_date`, `approved_date` |
| `00:00:00` | not set | `event_start_time`, `event_end_time` |
| `""` / whitespace-only | not set | all retained text fields |

Numeric zero is **not** blanket-converted. Phase 0 established zero-as-unset only
for `OldSubmissionId` and `RejectReasonId`, both of which are dropped, so no
retained numeric field needs it. Real booleans stay boolean: `is_event: false`,
`event_no_end: false` and `extra_edition: false` are values, not absences.

Known ambiguity: because Rowan uses `00:00:00` for "unset", a genuine midnight
event time is indistinguishable from an absent one. Phase 0 observed no midnight
events; this is recorded rather than guessed at.

---

## 12. Output

One file per successful run, written atomically (temp file + `os.replace`):

```
$XDG_STATE_HOME/dailymail/collections/YYYY-MM-DD.json
```

defaulting to `~/.local/state/dailymail/collections/`. Production output is
outside the repository and is never committed.

### Schema (`schema_version: 1`)

```
schema_version                     int
target_date                        "YYYY-MM-DD"
generated_at_utc                   ISO-8601
generated_at_rowan_local           ISO-8601 (America/New_York)
collector
  name, method, endpoint, page_size
  detail_pages_fetched             always 0
  browser_used                     always false
runtime
  token_fingerprints               {module_version, api_version, csrf_token}  (12 hex chars each)
  token_rediscovery_performed      bool
  tokens_changed_since_prior_run   bool | null
  prior_run_compared               "YYYY-MM-DD" | null
  pages_fetched                    {Employees: int, Students: int}
  duration_seconds                 float
source_counts
  employees_total_count            server TotalCount for the Employee query
  students_total_count             server TotalCount for the Student query
counts
  unique, new, standing
  everyone, employee_only, student_only
  categories
category_registry                  [{id, title, rank, color, is_active?}]  (complete, dynamic)
category_counts_by_audience        {audience: {category_id: count}}
new_categories_since_prior_run     [{id, title}] | null
announcements                      [ normalized record ]   (see §9)
validation
  passed                           [ "V2: …", … ]
  warnings                         [ … ]
  warning_count                    int
```

`unique` is the union of both audience views, so it can legitimately exceed
either `source_counts` value (e.g. 2026-08-21: 13 Employee + 8 Student = 16
unique).

---

## 13. CLI

```sh
uv run dailymail collect                      # today in America/New_York
uv run dailymail collect --date 2026-08-20    # explicit date
uv run dailymail collect --page-size 250      # override pagination window
uv run dailymail inspect --date 2026-08-20    # summarize a written artifact
```

Success prints one concise line plus a short provenance block:

```
COLLECT OK date=2026-08-20 employees=13 students=3 unique=13 new=5 standing=8 categories=33
  tokens=52d9c656b73b rediscovered=False changed_since_prior=None pages={'Employees': 1, 'Students': 1} 1.237s
  audience: everyone=3 employee_only=10 student_only=0
  wrote /home/…/.local/state/dailymail/collections/2026-08-20.json
```

Announcement bodies are never printed by either command; `inspect` lists ids,
status, audience, category, date count, body size and title only. Warnings go to
stderr. Every failure exits non-zero with the code from §5.

### Tests

```sh
uv run pytest
```

157 tests, no network access (verified with sockets blocked) and no dependency on
the live Rowan service. They run against the sanitized Phase 0 fixtures and a
mock transport, and include the permanent 2026-08-20 regression: the five known
subjects must be present and classified `New`, and the remaining eight `Standing`,
matching Phase 0 exactly.

---

## 14. Deliberately not built in Phase 1

SQLite and migrations · cross-run change detection · Gmail/SMTP and App Password
handling · HTML or plain-text email · Claude ranking · systemd units, timers or
any scheduling · alert emails · scheduled Playwright cross-checks.

The Phase 0 Playwright probes remain under `tools/recon/` for manual diagnostics
and are not part of the collector.
