"""Runtime discovery of the OutSystems version tokens.

Nothing deployment-specific is hardcoded. Rowan rotates these on every
republish, and a stale `apiVersion` produces the Phase 0 silent failure
(HTTP 200, `data: {}`, `hasApiVersionChanged: true`), so the collector rediscovers
rather than shipping constants.

Sources (Phase 0 §2.2):
  moduleVersion  GET moduleservices/moduleinfo -> .manifest.versionToken
  apiVersion     RowanAnnouncer.MainFlow.Home.mvc.js -> callServerAction(...)
  csrf token     OutSystems.js -> AnonymousCSRFToken = "..."

The CSRF token is an OutSystems platform-wide anonymous constant, not a secret
and not per-session. We still avoid logging it in full (see `fingerprint`).
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass

import httpx

from . import config
from .errors import DiscoveryError

_API_VERSION_RE = re.compile(
    r'callServerAction\(\s*"GetHomeData"\s*,\s*'
    r'"screenservices/RowanAnnouncer/MainFlow/Home/ActionGetHomeData"\s*,\s*'
    r'"([^"]+)"'
)
_CSRF_RE = re.compile(r'AnonymousCSRFToken\s*=\s*"([^"]+)"')


def fingerprint(value: str) -> str:
    """Short, non-reversible stand-in so tokens never appear in logs verbatim."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class RuntimeVersions:
    module_version: str
    api_version: str
    csrf_token: str

    def fingerprints(self) -> dict[str, str]:
        """Safe-to-log identity of this token set."""
        return {
            "module_version": fingerprint(self.module_version),
            "api_version": fingerprint(self.api_version),
            "csrf_token": fingerprint(self.csrf_token),
        }


def _get_text(client: httpx.Client, url: str, what: str) -> str:
    try:
        response = client.get(url)
    except httpx.HTTPError as exc:
        raise DiscoveryError(f"could not fetch {what}: {type(exc).__name__}") from exc
    if response.status_code != 200:
        raise DiscoveryError(f"{what} returned HTTP {response.status_code}")
    return response.text


def _asset_url(manifest_urls: dict[str, str], filename: str) -> str:
    """Resolve an asset through the manifest so we fetch the deployed revision.

    The manifest maps e.g. "/RowanAnnouncer/scripts/OutSystems.js" to a cache-
    busting query suffix. Using it means we read exactly the bundle the live app
    reads, rather than whatever an unversioned URL happens to serve.
    """
    suffix = f"/{filename}"
    for path, version in manifest_urls.items():
        if path.endswith(suffix):
            return f"https://apps.rowan.edu{path}{version or ''}"
    # Fall back to the conventional location if the manifest shape ever changes.
    return f"{config.BASE_URL}/scripts/{filename}"


def discover(client: httpx.Client) -> RuntimeVersions:
    """Discover the current token set. Raises DiscoveryError on any gap."""
    raw = _get_text(client, f"{config.BASE_URL}/{config.MODULE_INFO_PATH}", "moduleinfo")
    try:
        manifest = json.loads(raw)["manifest"]
        module_version = manifest["versionToken"]
        url_versions = manifest.get("urlVersions") or {}
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise DiscoveryError(
            "moduleinfo did not contain manifest.versionToken"
        ) from exc
    if not module_version:
        raise DiscoveryError("moduleinfo returned an empty versionToken")

    home_js = _get_text(
        client, _asset_url(url_versions, config.HOME_MVC_ASSET), config.HOME_MVC_ASSET
    )
    match = _API_VERSION_RE.search(home_js)
    if not match:
        raise DiscoveryError(
            f"could not locate the GetHomeData apiVersion in {config.HOME_MVC_ASSET}; "
            "the app's client bundle layout may have changed"
        )
    api_version = match.group(1)

    os_js = _get_text(
        client,
        _asset_url(url_versions, config.OUTSYSTEMS_JS_ASSET),
        config.OUTSYSTEMS_JS_ASSET,
    )
    csrf_match = _CSRF_RE.search(os_js)
    if not csrf_match:
        raise DiscoveryError(
            f"could not locate AnonymousCSRFToken in {config.OUTSYSTEMS_JS_ASSET}"
        )

    return RuntimeVersions(
        module_version=module_version,
        api_version=api_version,
        csrf_token=csrf_match.group(1),
    )
