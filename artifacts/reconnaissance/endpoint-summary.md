# Endpoint & Token Summary (reconnaissance, 2026-08-21)

Companion to `docs/site-reconnaissance.md`. Values captured on 2026-08-21;
the four tokens rotate on Rowan redeploy and must be discovered at runtime.

## The only endpoint DailyMail needs

```
POST https://apps.rowan.edu/RowanAnnouncer/screenservices/RowanAnnouncer/MainFlow/Home/ActionGetHomeData
Content-Type: application/json; charset=UTF-8
Accept: application/json
X-CSRFToken: T6C+9iB49TLra4jEsMeSckDMNhQ=
```
No cookies. No authentication. Callable without a browser.

Returns for one audience + one date: full announcement list with complete HTML
bodies, all contact/submitter/approver metadata, event fields, per-record
`DistributionDates`, the 33-entry category list with counts, and `TotalCount`.

## Runtime token discovery

| Token | Where to read it |
|---|---|
| `moduleVersion` | `GET /RowanAnnouncer/moduleservices/moduleinfo` → `.manifest.versionToken` |
| `apiVersion` | `GET /RowanAnnouncer/scripts/RowanAnnouncer.MainFlow.Home.mvc.js`, regex `callServerAction\("GetHomeData",\s*"([^"]+)",\s*"([^"]+)"` |
| CSRF token | `GET /RowanAnnouncer/scripts/OutSystems.js`, regex `AnonymousCSRFToken\s*=\s*"([^"]+)"` |

`tools/recon/02-api-probe.py` is a working reference implementation.

## Token failure modes (measured)

| Manipulation | Result |
|---|---|
| wrong/empty `apiVersion` | **HTTP 200, `data: {}`, `hasApiVersionChanged: true`** — silent failure |
| wrong `moduleVersion` | HTTP 200, full data, `hasModuleVersionChanged: true` (tolerated) |
| `X-CSRFToken` header missing | HTTP 403 `{"exception":{"message":"Invalid Login"}}` |
| `X-CSRFToken` present, bogus value | HTTP 200, full data (only presence is checked) |
| `MaxRecords <= 0` | `{"exception":{"message":"Error executing query."}}` |
| `Audience` invalid | **HTTP 200, only the `Both` subset** — silent partial failure |
| `CurrentDate` unparseable | **silently falls back to today** |

## TLS

`apps.rowan.edu` omits its intermediate certificate. The chain is genuine:

```
leaf         CN=apps.rowan.edu, O=Rowan University
intermediate CN=InCommon RSA Server CA 2, O=Internet2   <-- NOT SENT
root         CN=USERTrust RSA Certification Authority   <-- in system store
```

Fetch the intermediate from the leaf's AIA URI
`http://crt.sectigo.com/InCommonRSAServerCA2.crt`, append it to the system CA
bundle, and keep verification **enabled**:

```
openssl x509 -inform DER -in InCommonRSAServerCA2.crt -out incommon.pem
cat /etc/ssl/certs/ca-certificates.crt incommon.pem > rowan-ca-bundle.pem
curl --cacert rowan-ca-bundle.pem https://apps.rowan.edu/RowanAnnouncer/Home   # 200, verify=0
```

## Endpoints that must never be called

`ActionSaveSubmission`, `ActionUpdateSubmissionStatus`,
`ActionCreateApproversForSubmission`, `ActionGetAIRewriteResult`,
`ActionSaveVisitorClicks`, `ActionSendDailyMail`,
`ActionTest_DistributeExtraEditionByDate`, `ActionImportLegacyAnnouncements`,
`ActionDownloadData`, `ActionExportSubmissionsToExcel`,
`Test_CreateSubmissionForEachCategory`, `ActionSaveCategory`,
`ActionSaveAlertMessage`, `ActionSaveOrgCategory*`, `ActionDeleteOrgCategory*`,
`ActionCreateCalendarInvite`, `ActionDoLogin`, `ActionDoLogout`.

Note: loading `/Announcement?SubmissionId=<id>` **in a browser** causes the app's
own front end to POST `ActionSaveVisitorClicks`. The API-only collector avoids
this; detail pages are not needed because `ActionGetHomeData` already returns
full bodies.
