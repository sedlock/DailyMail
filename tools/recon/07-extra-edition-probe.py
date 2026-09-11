#!/usr/bin/env python3
"""READ-ONLY reconnaissance probe: where do Rowan's Extra Editions live?

DIAGNOSTIC ONLY. Not production code, not imported by the package, never on the
daily path. It reuses the package's own discovery and TLS so it reads exactly
what the collector reads.

This is the probe that answered Phase 0's open question U3. It re-runs on demand
so the answer in `docs/extra-editions.md` can be re-verified rather than
believed.

    uv run python tools/recon/07-extra-edition-probe.py
    uv run python tools/recon/07-extra-edition-probe.py --date 2026-08-31

READ-ONLY BY CONSTRUCTION. Every request below is a `ActionGetHomeData` fetch or
a `DataAction*` read. `ActionTest_DistributeExtraEditionByDate` and
`ActionSendDailyMail` are Rowan's own senders; their names appear here only in
this sentence, and nothing in this file can call them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))

from dailymail import client, config, discovery, tls  # noqa: E402

# The read-only EmailAdmin data action and its apiVersion, harvested from the
# live client bundle. Probed to establish that it is role-gated, nothing more.
EMAIL_ADMIN_READ = "MainFlow/EmailAdmin/DataActionGetDailyMailAnnouncements"
EMAIL_ADMIN_API_VERSION = "IcRIx27_Q4PaO3IgszpGbw"

NEEDLES = ("Zabinski", "Petrella", "A New Chapter", "EXTRA EDITION")


def home(http, versions, *, audience, start_date, end_date, max_records=100000):
    body = {
        "versionInfo": {
            "moduleVersion": versions.module_version,
            "apiVersion": versions.api_version,
        },
        "viewName": config.VIEW_NAME,
        "inputParameters": {
            "Filters": {
                "Audience": audience,
                "StartDate": start_date,
                "EndDate": end_date,
                "CategoryIds": {"List": [], "EmptyListItem": {}},
                "SelectedCategories": {"List": [], "EmptyListItem": 0},
                "keywords": "",
            },
            "StartIndex": 0,
            "MaxRecords": max_records,
            "IsCategoryUpdate": False,
        },
    }
    response = http.post(
        f"{config.BASE_URL}/{config.GET_HOME_DATA_PATH}",
        json=body,
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "X-CSRFToken": versions.csrf_token,
        },
    )
    data = response.json().get("data", {})
    return data.get("TotalCount"), (data.get("Announcements", {}).get("List") or [])


def scan(records):
    flagged, matched = [], []
    for entry in records:
        submission = entry.get("Submission", entry)
        if submission.get("ExtraEdition"):
            flagged.append((submission.get("Id"), submission.get("Title")))
        sent = submission.get("ExtraEditionDateSent")
        if sent and not str(sent).startswith("1900-01-01"):
            flagged.append((submission.get("Id"), f"DateSent={sent}"))
        blob = json.dumps(submission, ensure_ascii=False)
        if any(re.search(re.escape(needle), blob, re.I) for needle in NEEDLES):
            matched.append((submission.get("Id"), submission.get("Title")))
    return flagged, matched


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", default="2026-08-31", help="the day in question")
    args = parser.parse_args(argv)

    http = client.build_http_client(tls.build_ssl_context())
    versions = discovery.discover(http)
    print(f"tokens: {versions.fingerprints()}")

    for audience in config.REQUEST_AUDIENCES:
        for label, start, end in (
            ("single-day", args.date, config.SINGLE_DAY_END_DATE_SENTINEL),
            ("range      ", args.date, args.date),
            ("archive    ", "2020-01-01", "2030-12-31"),
        ):
            total, records = home(
                http, versions, audience=audience, start_date=start, end_date=end
            )
            flagged, matched = scan(records)
            print(
                f"{audience:<10} {label}  TotalCount={total:<6} "
                f"returned={len(records):<6} ExtraEdition=true:{len(flagged)} "
                f"text-matches:{len(matched)}"
            )
            for entry in flagged[:10]:
                print(f"    FLAGGED {entry}")
            for entry in matched[:10]:
                print(f"    matched {entry}")

    # The one read path that knows about the daily-mail distribution.
    response = http.post(
        f"{config.BASE_URL}/screenservices/RowanAnnouncer/{EMAIL_ADMIN_READ}",
        json={
            "versionInfo": {
                "moduleVersion": versions.module_version,
                "apiVersion": EMAIL_ADMIN_API_VERSION,
            },
            "viewName": "MainFlow.EmailAdmin",
            "inputParameters": {"SendDate": args.date, "Audience": "Employees"},
        },
        headers={
            "Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "X-CSRFToken": versions.csrf_token,
        },
    )
    payload = response.json()
    exception = payload.get("exception") or {}
    print(
        f"\n{EMAIL_ADMIN_READ}\n  HTTP {response.status_code} "
        f"{exception.get('specificType') or 'no exception'}: "
        f"{exception.get('message') or json.dumps(payload.get('data'))[:200]}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover - diagnostic entry point
    raise SystemExit(main())
