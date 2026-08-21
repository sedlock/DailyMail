"""Runtime token discovery and apiVersion change detection."""

from __future__ import annotations

import httpx
import pytest

from dailymail.discovery import RuntimeVersions, discover, fingerprint
from dailymail.errors import DiscoveryError

from conftest import MOCK_API_VERSION, MOCK_CSRF, MOCK_MODULE_VERSION, MockAnnouncer


def test_discovers_all_three_tokens(two_audience_mock):
    with two_audience_mock.client() as client:
        versions = discover(client)
    assert versions.module_version == MOCK_MODULE_VERSION
    assert versions.api_version == MOCK_API_VERSION
    assert versions.csrf_token == MOCK_CSRF


def test_nothing_is_hardcoded_from_phase_0(two_audience_mock):
    """Discovery must read the live deployment, not Phase 0's observed values."""
    with two_audience_mock.client() as client:
        versions = discover(client)
    assert versions.api_version != "C1CrfYuM0JxcZElpjPJRPw"
    assert versions.csrf_token != "T6C+9iB49TLra4jEsMeSckDMNhQ="
    assert versions.module_version != "EEtZtRyG_45VdflpWRQexA"


def test_discovery_resolves_assets_through_the_manifest(two_audience_mock):
    """The versioned asset URLs from moduleinfo are what get fetched."""
    seen: list[str] = []
    inner = two_audience_mock.transport()

    def recording(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return inner.handler(request)

    with httpx.Client(transport=httpx.MockTransport(recording)) as client:
        discover(client)

    assert any("Home.mvc.js?v1" in url for url in seen), seen
    assert any("OutSystems.js?v2" in url for url in seen), seen


def test_fingerprint_is_short_and_not_the_token():
    token = "T6C+9iB49TLra4jEsMeSckDMNhQ="
    printed = fingerprint(token)
    assert len(printed) == 12
    assert token not in printed


def test_fingerprints_never_expose_tokens():
    versions = RuntimeVersions(
        module_version="module-secret",
        api_version="api-secret",
        csrf_token="csrf-secret",
    )
    rendered = str(versions.fingerprints())
    for secret in ("module-secret", "api-secret", "csrf-secret"):
        assert secret not in rendered


def test_missing_api_version_is_a_loud_discovery_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("moduleinfo"):
            return httpx.Response(
                200, json={"manifest": {"versionToken": "v", "urlVersions": {}}}
            )
        # A bundle with no callServerAction declaration at all.
        return httpx.Response(200, text="/* refactored client bundle */")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DiscoveryError, match="apiVersion"):
            discover(client)


def test_missing_csrf_token_is_a_loud_discovery_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("moduleinfo"):
            return httpx.Response(
                200, json={"manifest": {"versionToken": "v", "urlVersions": {}}}
            )
        if path.endswith("Home.mvc.js"):
            return httpx.Response(
                200,
                text=(
                    'callServerAction("GetHomeData", '
                    '"screenservices/RowanAnnouncer/MainFlow/Home/ActionGetHomeData", '
                    '"apiv")'
                ),
            )
        return httpx.Response(200, text="no token here")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DiscoveryError, match="AnonymousCSRFToken"):
            discover(client)


def test_empty_module_version_rejected():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"manifest": {"versionToken": ""}})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DiscoveryError, match="empty versionToken"):
            discover(client)


def test_moduleinfo_http_error_is_discovery_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="unavailable")

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(DiscoveryError, match="HTTP 503"):
            discover(client)


def test_stale_api_version_triggers_rediscovery_and_one_retry(two_audience_mock):
    """Phase 0's silent-failure mode must self-heal exactly once."""
    from dailymail.collect import run_collection

    two_audience_mock.fail_api_version_until_attempt = 1
    with two_audience_mock.client() as client:
        artifact = run_collection(target_date="2026-08-20", http=client)

    assert artifact["runtime"]["token_rediscovery_performed"] is True
    assert two_audience_mock.discovery_calls == 2
    assert artifact["counts"]["unique"] == 13
    assert any("V13" in w for w in artifact["validation"]["warnings"])


def test_persistent_version_change_fails_loudly_and_is_not_an_empty_day(
    two_audience_mock,
):
    from dailymail.collect import run_collection
    from dailymail.errors import VersionChangedError

    # Every attempt reports a stale apiVersion.
    two_audience_mock.fail_api_version_until_attempt = 99
    with two_audience_mock.client() as client:
        with pytest.raises(VersionChangedError) as caught:
            run_collection(target_date="2026-08-20", http=client)

    assert caught.value.exit_code == 7
    assert "NOT an empty announcement day" in str(caught.value)
    # Rediscovery was attempted exactly once before giving up.
    assert two_audience_mock.discovery_calls == 2
