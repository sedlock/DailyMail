#!/usr/bin/env python3
"""DISPOSABLE RECON PROBE - not production code.
Calls the RowanAnnouncer OutSystems screen-service API directly.
Harvests moduleVersion / apiVersion / CSRF token dynamically.
Read-only: only ever POSTs data-fetch screen actions.
"""
import json, re, sys, urllib.request, ssl, os

BASE = "https://apps.rowan.edu/RowanAnnouncer"
CA = os.environ.get("ROWAN_CA_BUNDLE",
    "/tmp/claude-1000/-mnt-bench-src-DailyMail/28a8267a-3562-445f-a6c4-d20591bf5541/scratchpad/rowan-ca-bundle.pem")
CTX = ssl.create_default_context(cafile=CA)

def http(url, data=None, headers=None):
    req = urllib.request.Request(url, data=data, headers=headers or {},
                                 method="POST" if data else "GET")
    with urllib.request.urlopen(req, context=CTX, timeout=60) as r:
        return r.status, r.read()

_cache = {}
def tokens():
    """Discover moduleVersion, apiVersion, csrf token from live app assets."""
    if _cache: return _cache
    # 1. index HTML -> manifest loader token
    _, idx = http(BASE + "/")
    idx = idx.decode("utf8", "replace")
    mv = re.search(r'OSManifestLoader\.indexVersionToken\s*=\s*"([^"]+)"', idx)
    # 2. moduleinfo -> authoritative versionToken
    _, mi = http(BASE + "/moduleservices/moduleinfo")
    mij = json.loads(mi)
    module_version = mij["manifest"]["versionToken"]
    # 3. OutSystems.js -> anonymous CSRF token
    _, osjs = http(BASE + "/scripts/OutSystems.js")
    csrf = re.search(rb'AnonymousCSRFToken\s*=\s*"([^"]+)"', osjs).group(1).decode()
    # 4. appDefinition -> apiVersion candidates
    _, appdef = http(BASE + "/scripts/RowanAnnouncer.appDefinition.js")
    _cache.update(moduleVersion=module_version, csrf=csrf,
                  indexVersionToken=mv.group(1) if mv else None,
                  appdef=appdef.decode("utf8","replace"))
    return _cache

def call(action, view, input_params, api_version="C1CrfYuM0JxcZElpjPJRPw",
         module_version=None, csrf=None):
    t = tokens()
    body = json.dumps({
        "versionInfo": {"moduleVersion": module_version or t["moduleVersion"],
                        "apiVersion": api_version},
        "viewName": view,
        "inputParameters": input_params,
    }).encode()
    hdrs = {"Content-Type": "application/json; charset=UTF-8",
            "Accept": "application/json",
            "X-CSRFToken": csrf or t["csrf"]}
    st, raw = http(f"{BASE}/screenservices/RowanAnnouncer/{action}", body, hdrs)
    return st, json.loads(raw)

def home(audience="Employees", start_date="2026-08-21", end_date="1900-01-01",
         start_index=0, max_records=20, category_ids=None, keywords="",
         is_category_update=False, **kw):
    filters = {"Audience": audience, "EndDate": end_date,
               "CategoryIds": {"List": category_ids or [], "EmptyListItem": {}},
               "StartDate": start_date, "keywords": keywords,
               "SelectedCategories": {"List": [], "EmptyListItem": 0}}
    return call("MainFlow/Home/ActionGetHomeData", "MainFlow.Home",
                {"Filters": filters, "StartIndex": start_index,
                 "MaxRecords": max_records,
                 "IsCategoryUpdate": is_category_update}, **kw)

if __name__ == "__main__":
    t = tokens()
    print("moduleVersion    :", t["moduleVersion"])
    print("indexVersionToken:", t["indexVersionToken"])
    print("csrf             :", t["csrf"])
