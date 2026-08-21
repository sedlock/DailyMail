"""Shared test harness.

Tests run entirely against the sanitized Phase 0 fixtures and a mock HTTP
transport. Nothing here touches the live Rowan service.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "artifacts" / "reconnaissance" / "fixtures"

TARGET_DATE = "2026-08-20"

# The five subjects Rowan's own Employee Daily Mail listed as new on 2026-08-20.
KNOWN_NEW_SUBJECTS_2026_08_20 = [
    "Parking Lot O-1 Closure",
    "Academic Integrity: Resources and Reminders for Faculty",
    "Internal Limited Submission: Brain Research Foundation 2027 Seed Grant Program",
    "Profs for a Purpose: Mini-Grant Proposals Open",
    "You Belong Series & Certificate Launch Summit 2026",
]

# Per docs/site-reconnaissance.md §6.1.
EXPECTED_NEW_IDS_2026_08_20 = {"6572", "6602", "6622", "6623", "6625"}
EXPECTED_STANDING_IDS_2026_08_20 = {
    "6476", "6492", "6496", "6529", "6538", "6578", "6591", "6606",
}

# Discovered token values used by the mock transport. Deliberately different
# from the real deployment's values so a test can never pass by accident.
MOCK_MODULE_VERSION = "MOCKmoduleVersion00000"
MOCK_API_VERSION = "MOCKapiVersion0000000"
MOCK_CSRF = "MOCKcsrfToken00000000="


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    """Redirect every XDG location into a temp dir for every test.

    Without this, tests would read the developer's real database, collection
    history and credentials, and results would depend on whatever live runs
    happened to have occurred on the machine.
    """
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path / "state"


@pytest.fixture
def fake_credentials(tmp_path, monkeypatch):
    """A throwaway credential file with correct permissions.

    The password is deliberately distinctive so tests can assert it never
    reaches a message, a log line, or the curation payload.
    """
    from dailymail import settings as settings_module

    directory = settings_module.config_dir()
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    path = directory / "credentials.env"
    path.write_text(
        "GMAIL_SMTP_USER=digest-test@gmail.com\n"
        "GMAIL_APP_PASSWORD=abcd efgh ijkl mnop\n",
        encoding="utf-8",
    )
    path.chmod(0o600)
    return path


@pytest.fixture
def settings_obj(fake_credentials):
    from dailymail import settings as settings_module

    settings_module.ensure_config()
    return settings_module.load()


@pytest.fixture
def populated_db(settings_obj, employee_fixture, student_fixture):
    """Database seeded from the sanitized Phase 0 fixtures for both dates."""
    from dailymail import db, ingest

    connection = db.connect()
    db.initialize(connection)
    artifact = _artifact_from_fixtures(employee_fixture, student_fixture, TARGET_DATE)
    ingest.ingest_artifact(connection, artifact, settings_obj, origin="test")
    yield connection
    connection.close()


def _artifact_from_fixtures(employee_fixture, student_fixture, target_date):
    """Build a Phase 1 shaped collection artifact from the two view fixtures."""
    membership: dict[str, list[str]] = {}
    records: dict[str, dict] = {}
    for view, fixture in (("Employees", employee_fixture), ("Students", student_fixture)):
        for raw in fixture["Announcements"]:
            submission_id = str(raw["Submission"]["Id"])
            membership.setdefault(submission_id, []).append(view)
            records.setdefault(submission_id, raw)

    from dailymail.normalize import normalize_category_registry, normalize_record

    announcements = [
        normalize_record(
            raw, target_date=target_date,
            source_query_membership=membership[submission_id],
        )
        for submission_id, raw in sorted(records.items(), key=lambda kv: int(kv[0]))
    ]
    registry = normalize_category_registry(employee_fixture["Categories"])
    return {
        "schema_version": 1,
        "target_date": target_date,
        "generated_at_utc": "2026-08-21T12:00:00+00:00",
        "counts": {
            "unique": len(announcements),
            "new": sum(1 for a in announcements if a["status"] == "New"),
            "standing": sum(1 for a in announcements if a["status"] == "Standing"),
            "categories": len(registry),
        },
        "source_counts": {
            "employees_total_count": len(employee_fixture["Announcements"]),
            "students_total_count": len(student_fixture["Announcements"]),
        },
        "category_registry": registry,
        "announcements": announcements,
        "validation": {"passed": ["test"], "warnings": [], "warning_count": 0},
    }


def artifact_from_fixtures(employee_fixture, student_fixture, target_date=TARGET_DATE):
    return _artifact_from_fixtures(employee_fixture, student_fixture, target_date)


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.fixture
def employee_fixture() -> dict:
    return load_fixture("homedata-employees-2026-08-20.sanitized.json")


@pytest.fixture
def student_fixture() -> dict:
    return load_fixture("homedata-students-2026-08-20.sanitized.json")


@pytest.fixture
def empty_day_fixture() -> dict:
    return load_fixture("homedata-empty-day.sanitized.json")


@pytest.fixture
def broken_apiversion_fixture() -> dict:
    return load_fixture("homedata-broken-apiversion.sanitized.json")


@pytest.fixture
def event_record() -> dict:
    return load_fixture("announcement-event.sanitized.json")["record"]


@pytest.fixture
def student_only_record() -> dict:
    return load_fixture("announcement-student-only.sanitized.json")["record"]


def as_api_response(
    *,
    announcements: list[dict],
    categories: list[dict],
    total_count: int | str,
    api_version_changed: bool = False,
    module_version_changed: bool = False,
) -> dict:
    """Wrap fixture data in the real `ActionGetHomeData` envelope.

    The Phase 0 fixtures store a flattened form; the live API nests lists under
    a `List` key and returns counts as strings. Rebuilding the true envelope
    means the tests exercise the same parsing path production uses.
    """
    return {
        "versionInfo": {
            "hasModuleVersionChanged": module_version_changed,
            "hasApiVersionChanged": api_version_changed,
        },
        "data": {
            "Categories": {"List": categories},
            "Announcements": {"List": announcements},
            "TotalCount": str(total_count),
        },
        "rolesInfo": ",",
    }


def recategorize(categories: list[dict], announcements: list[dict]) -> list[dict]:
    """Recompute per-category `Count` so the cross-foot matches a custom subset."""
    tally: dict[object, int] = {}
    for record in announcements:
        tally[record["Submission"]["Category"]] = (
            tally.get(record["Submission"]["Category"], 0) + 1
        )
    out = []
    for category in categories:
        clone = dict(category)
        clone["Count"] = str(tally.get(category["Id"], 0))
        out.append(clone)
    return out


class MockAnnouncer:
    """Mock transport serving discovery assets and paged ActionGetHomeData.

    `pages_by_audience` maps an audience to a list of response envelopes served
    in StartIndex order, letting a test drive multi-page pagination.
    """

    def __init__(
        self,
        *,
        pages_by_audience: dict[str, list[dict]],
        api_version: str = MOCK_API_VERSION,
        module_version: str = MOCK_MODULE_VERSION,
        csrf: str = MOCK_CSRF,
        fail_api_version_until_attempt: int = 0,
    ) -> None:
        self.pages_by_audience = pages_by_audience
        self.api_version = api_version
        self.module_version = module_version
        self.csrf = csrf
        self.fail_api_version_until_attempt = fail_api_version_until_attempt
        self.requests: list[dict] = []
        self.discovery_calls = 0
        self._data_attempts = 0

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def client(self) -> httpx.Client:
        return httpx.Client(transport=self.transport(), base_url="https://apps.rowan.edu")

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path

        if path.endswith("moduleservices/moduleinfo"):
            self.discovery_calls += 1
            return httpx.Response(
                200,
                json={
                    "manifest": {
                        "versionToken": self.module_version,
                        "urlVersions": {
                            "/RowanAnnouncer/scripts/RowanAnnouncer.MainFlow.Home.mvc.js": "?v1",
                            "/RowanAnnouncer/scripts/OutSystems.js": "?v2",
                        },
                    }
                },
            )

        if path.endswith("RowanAnnouncer.MainFlow.Home.mvc.js"):
            body = (
                'controller.callServerAction("GetHomeData", '
                '"screenservices/RowanAnnouncer/MainFlow/Home/ActionGetHomeData", '
                f'"{self.api_version}", inputs, controller);'
            )
            return httpx.Response(200, text=body)

        if path.endswith("OutSystems.js"):
            body = (
                'e.CSRFHeader="X-CSRFToken",'
                f'e.AnonymousCSRFToken="{self.csrf}",e.getCSRFToken=n'
            )
            return httpx.Response(200, text=body)

        if path.endswith("ActionGetHomeData"):
            payload = json.loads(request.content)
            params = payload["inputParameters"]
            audience = params["Filters"]["Audience"]
            start_index = params["StartIndex"]
            max_records = params["MaxRecords"]
            self.requests.append(
                {
                    "audience": audience,
                    "start_date": params["Filters"]["StartDate"],
                    "end_date": params["Filters"]["EndDate"],
                    "start_index": start_index,
                    "max_records": max_records,
                    "api_version": payload["versionInfo"]["apiVersion"],
                    "module_version": payload["versionInfo"]["moduleVersion"],
                    "csrf": request.headers.get("x-csrftoken"),
                }
            )

            self._data_attempts += 1
            if self._data_attempts <= self.fail_api_version_until_attempt:
                # Reproduce Phase 0's silent failure: HTTP 200, empty data.
                return httpx.Response(
                    200,
                    json={
                        "versionInfo": {
                            "hasModuleVersionChanged": False,
                            "hasApiVersionChanged": True,
                        },
                        "data": {},
                        "rolesInfo": ",",
                    },
                )

            pages = self.pages_by_audience.get(audience, [])
            index = start_index // max_records if max_records else 0
            if index < len(pages):
                return httpx.Response(200, json=pages[index])
            return httpx.Response(
                200,
                json=as_api_response(
                    announcements=[],
                    categories=pages[0]["data"]["Categories"]["List"] if pages else [],
                    total_count=pages[0]["data"]["TotalCount"] if pages else 0,
                ),
            )

        return httpx.Response(404, text="unexpected path in test")


@pytest.fixture
def two_audience_mock(employee_fixture, student_fixture) -> MockAnnouncer:
    """Mock serving the real 2026-08-20 Employee and Student fixture data."""
    return MockAnnouncer(
        pages_by_audience={
            "Employees": [
                as_api_response(
                    announcements=employee_fixture["Announcements"],
                    categories=employee_fixture["Categories"],
                    total_count=employee_fixture["TotalCount"],
                )
            ],
            "Students": [
                as_api_response(
                    announcements=student_fixture["Announcements"],
                    categories=student_fixture["Categories"],
                    total_count=student_fixture["TotalCount"],
                )
            ],
        }
    )
