"""The boundary itself, and the specific defect that went through it.

`test_a_calendar_failure_never_costs_the_digest` proved something worth
proving -- that a calendar-enrichment failure costs the reader a button and
nothing else -- and proved it by calling `monkeypatch.undo()`. A `monkeypatch`
instance belongs to the test's scope, not to the line that installed a patch,
so undoing "the calendar stub" also undid the `pipeline` fixture's stubbed
collector and stubbed SMTP send, `conftest`'s temporary XDG directories, and
the parking and route network guards. The very next line ran the full daily
pipeline against the operator's real state directory, the real Rowan endpoint
and the real Gmail App Password.

This module is the guard rail for that class of mistake, in two layers:

  * a structural check that no test module calls `.undo()` at all, which fails
    immediately if the pattern is reintroduced anywhere in the suite;
  * a behavioural check that the calendar-failure invariant is still provable
    with a *bounded* patch scope, and that every hermetic protection is
    verifiably still standing on the far side of the simulated failure.

Then it probes the boundary itself, once per thing that must never happen, to
show the refusals are live rather than aspirational. Every probe is refused
before the operation occurs, so running this module makes no real connection,
speaks no SMTP, and reads no credential. Nothing here records or asserts on a
secret value; a refusal carries a path or a host, never the bytes behind it.
"""

from __future__ import annotations

import ast
import os
import socket
import sqlite3
import subprocess
from pathlib import Path

import pytest

import hermetic_boundary
from conftest import (
    KNOWN_NEW_SUBJECTS_2026_08_20,
    TARGET_DATE,
    artifact_from_fixtures,
)
from dailymail import (
    calendar_enrich,
    collect as collector,
    config as collector_config,
    credentials,
    daily,
    db,
    mailer,
    parking_sources,
    settings as settings_module,
    travel,
)

TESTS_DIR = Path(__file__).resolve().parent

# Taken from the boundary rather than rebuilt here, so the paths this module
# probes are by construction the same ones the boundary watches.
REAL_HOME = hermetic_boundary.REAL_HOME
REAL_CONFIG_DIR = hermetic_boundary.PRODUCTION_CONFIG_DIR
REAL_CREDENTIALS = hermetic_boundary.PRODUCTION_CREDENTIALS
REAL_DATABASE = hermetic_boundary.PRODUCTION_DATABASE
REAL_STATE_DIR = hermetic_boundary.PRODUCTION_STATE_DIR
RELEASE_ROOT = Path(hermetic_boundary.PRODUCTION_RELEASE_ROOT)

# Captured at import, before any fixture has patched a module attribute. These
# are the genuine production callables, and comparing against them is how a
# test can tell "the stub is still installed" from "the stub is gone".
REAL_RUN_COLLECTION = collector.run_collection
REAL_SEND = mailer.send
REAL_SENDER_ADDRESS = mailer.sender_address
REAL_PARKING_FETCH = parking_sources.fetch
REAL_ROUTE_LOOKUP = travel.osrm_duration_seconds

# A routable address that is deliberately not resolved from a name, so the
# socket-level guard is exercised separately from the DNS guard. Refused before
# `connect` is reached, so no packet is ever emitted.
UNREACHED_EXTERNAL_ADDRESS = ("198.51.100.7", 80)


# --- layer one: the pattern must not come back --------------------------------


def _undo_call_lines(path: Path) -> list[int]:
    """Line numbers of every `<something>.undo()` call in one module.

    Parsed rather than grepped on purpose: the modules that explain why this is
    forbidden name `monkeypatch.undo()` in their prose, and a comment is not a
    call. Any receiver counts -- a `MonkeyPatch` handed out by
    `monkeypatch.context()` has the same `undo` and the same blast radius.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return sorted(
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "undo"
    )


def test_no_test_module_calls_undo():
    """The structural regression: this fails the moment the pattern returns.

    `monkeypatch.undo()` has no safe use inside a test body. It reverts the
    whole scope's patch stack, which includes every autouse fixture's, so it
    can only ever remove more than the caller installed. A test that wants one
    patch to stop applying wants `monkeypatch.context()`.
    """
    offenders = {}
    for path in sorted(TESTS_DIR.glob("*.py")):
        lines = _undo_call_lines(path)
        if lines:
            offenders[path.name] = lines
    assert offenders == {}, (
        f"{offenders}: use `monkeypatch.context()` for a bounded patch scope, or "
        "save and restore the one attribute. `undo()` also reverts the autouse "
        "fixtures that make the suite hermetic."
    )


def test_the_boundary_is_installed_and_has_no_uninstall():
    """It is an audit hook, which is why no test can drop it."""
    assert hermetic_boundary.is_installed()
    assert not hasattr(hermetic_boundary, "uninstall")
    assert not hasattr(hermetic_boundary, "remove")


# --- layer two: the invariant, with the boundary intact -----------------------


@pytest.fixture
def pipeline(monkeypatch, settings_obj, employee_fixture, student_fixture):
    """The same wiring `test_daily_calendar_repeats.pipeline` installs.

    Duplicated deliberately rather than imported: this module's whole subject is
    what happens to these patches, so it needs to own them.
    """
    sent: list = []
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)

    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: dict(artifact, target_date=target_date),
    )
    monkeypatch.setattr(daily.collector, "write_artifact", lambda a: Path("/dev/null"))
    monkeypatch.setattr(daily.mailer, "sender_address", lambda: "digest-test@gmail.com")
    monkeypatch.setattr(mailer, "sender_address", lambda: "digest-test@gmail.com")
    monkeypatch.setattr(
        daily.mailer, "send", lambda prepared, settings: sent.append(prepared) or "ok"
    )
    monkeypatch.setattr(
        daily.curate, "curate",
        lambda rows, target_date, settings, **kwargs: daily.curate.CurationOutcome(
            method="fallback", model=None,
            entries=daily.curate.fallback_rank(rows, target_date, settings),
            error="stubbed",
        ),
    )
    return sent


def _protections_still_standing(tmp_path: Path) -> dict[str, bool]:
    """One dictionary describing every protection, so a failure names itself."""
    return {
        "XDG_CONFIG_HOME redirected": os.environ.get("XDG_CONFIG_HOME", "").startswith(
            str(tmp_path)
        ),
        "XDG_DATA_HOME redirected": os.environ.get("XDG_DATA_HOME", "").startswith(
            str(tmp_path)
        ),
        "XDG_STATE_HOME redirected": os.environ.get("XDG_STATE_HOME", "").startswith(
            str(tmp_path)
        ),
        "config dir is temporary": Path(tmp_path) in settings_module.config_dir().parents,
        "credentials path is temporary": (
            settings_module.credentials_path() != REAL_CREDENTIALS
        ),
        "database is temporary": db.database_path() != REAL_DATABASE,
        "state dir is temporary": collector_config.state_dir() != REAL_STATE_DIR,
        "collector still stubbed": daily.collector.run_collection is not REAL_RUN_COLLECTION,
        "SMTP send still stubbed": daily.mailer.send is not REAL_SEND,
        "sender address still stubbed": (
            daily.mailer.sender_address is not REAL_SENDER_ADDRESS
        ),
        "parking fetch still blocked": parking_sources.fetch is not REAL_PARKING_FETCH,
        "route lookup still blocked": (
            travel.osrm_duration_seconds is not REAL_ROUTE_LOOKUP
        ),
        "boundary still installed": hermetic_boundary.is_installed(),
    }


def test_a_calendar_failure_cannot_dismantle_the_hermetic_boundary(
    pipeline, settings_obj, monkeypatch, tmp_path
):
    """The repaired shape of the defect, asserted end to end.

    Phase one replaces the enrichment entry point and requires `run_daily` to
    propagate -- a programming error is not something the orchestration should
    swallow. Phase two needs the real entry point back so a failure *inside* it
    can be shown to be contained. The point of this test is the assertion
    between the two phases: getting the entry point back must restore exactly
    that, and leave all eleven other protections untouched.
    """
    before = _protections_still_standing(tmp_path)
    assert all(before.values()), [name for name, ok in before.items() if not ok]

    real_enrich_digest = calendar_enrich.enrich_digest

    with monkeypatch.context() as broken_entry_point:
        broken_entry_point.setattr(
            daily.calendar_enrich, "enrich_digest",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("calendar exploded")),
        )
        assert calendar_enrich.enrich_digest is not real_enrich_digest
        with pytest.raises(RuntimeError, match="calendar exploded"):
            daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    # (8) The restoration was exactly one attribute wide.
    assert calendar_enrich.enrich_digest is real_enrich_digest
    after = _protections_still_standing(tmp_path)
    assert after == before, [name for name, ok in after.items() if not before[name] or not ok]

    # (1) Force the failure at the layer the containment actually sits on:
    # `build_action_url` is called from `_build_one`, inside the per-candidate
    # `try` that turns one bad event into one missing button. Curation has to
    # want the button first, or relevance withholds the candidate and the
    # failing code is never reached.
    def curation_that_wants_the_calendar(rows, target_date, settings, **kwargs):
        entries = daily.curate.fallback_rank(rows, target_date, settings)
        for entry in entries:
            entry["calendar"] = {
                "offer": True, "confidence": 0.95, "reason": "relevant to the reader",
            }
        return daily.curate.CurationOutcome(
            method="claude", model="sonnet", entries=entries,
        )

    monkeypatch.setattr(daily.curate, "curate", curation_that_wants_the_calendar)
    monkeypatch.setattr(
        calendar_enrich.calendar_action, "build_action_url",
        lambda *a, **k: (_ for _ in ()).throw(ValueError("action url exploded")),
    )

    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    # (2) The digest still succeeds, exactly as the original invariant says.
    assert result.status == "success"
    assert result.email_status == "sent"
    assert result.error is None

    # (1, continued) and the failure genuinely happened.
    assert result.calendar["candidates"] > 0
    assert result.calendar["build_failures"] > 0
    assert result.calendar["offered"] == 0
    assert result.calendar["session_actions"] == 0

    # (3) No announcement was lost, and the digest that shipped contains them.
    assert result.counts["unique"] == 13
    assert result.counts["new"] + result.counts["standing"] == 13
    prepared = pipeline[-1]
    assert prepared.calendar_count == 0
    text = next(
        part.get_content()
        for part in prepared.message.walk()
        if part.get_content_type() == "text/plain"
    )
    for subject in KNOWN_NEW_SUBJECTS_2026_08_20:
        assert subject in text

    # (4, 5) Nothing reached the network or SMTP: the run went through the
    # stubs, and any attempt to go around them would be a recorded violation.
    assert daily.collector.run_collection is not REAL_RUN_COLLECTION
    assert daily.mailer.send is not REAL_SEND
    assert result.calendar["route_lookups"] == 0
    assert hermetic_boundary.VIOLATIONS == []

    # (6) The real credential file was never in play; the fixture one was.
    assert settings_module.credentials_path() != REAL_CREDENTIALS
    assert Path(tmp_path) in settings_module.credentials_path().parents

    # (7) Nor was any production directory: everything resolved into tmp_path.
    for resolved in (
        settings_module.config_dir(),
        settings_module.data_dir(),
        collector_config.state_dir(),
        db.database_path(),
    ):
        assert Path(tmp_path) in resolved.parents, resolved
        assert not str(resolved).startswith(str(REAL_CONFIG_DIR))

    # (8, continued) Still standing on the far side of the whole thing.
    assert all(_protections_still_standing(tmp_path).values())


def test_losing_the_xdg_redirection_still_cannot_reach_production(
    pipeline, settings_obj, monkeypatch
):
    """Defence in depth: what saves the suite if a patch does go missing again.

    `undo()` is forbidden outright now, but the call was never the hazard --
    dropping the XDG redirection was. Every path in DailyMail is resolved from
    the environment at the moment of use, so losing those three variables
    silently repoints the run lock, the database and the credential file at the
    operator's real state, with no error anywhere to notice.

    Reproduced here by deleting exactly those three variables inside a bounded
    scope. All three paths do become the production ones, and all three are
    refused at the point of use rather than at the point of resolution -- which
    is the property that makes the boundary worth having: it does not depend on
    any test remembering anything.
    """
    with monkeypatch.context() as redirection_lost:
        for variable in ("XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME"):
            redirection_lost.delenv(variable)

        # Every path is now the operator's real one.
        assert collector_config.state_dir() == REAL_STATE_DIR
        assert db.database_path() == REAL_DATABASE
        assert settings_module.credentials_path() == REAL_CREDENTIALS
        assert settings_module.config_dir() == REAL_CONFIG_DIR

        # The run lock is the first thing `run_daily` touches, and the first
        # thing the original defect reached.
        with hermetic_boundary.expect_blocked("production-path") as probes:
            with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
                daily.RunLock()
        assert str(REAL_STATE_DIR) in probes[-1]["detail"]

        # The production database, whose schema the run would have migrated.
        with hermetic_boundary.expect_blocked("production-sqlite", "production-path"):
            with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
                db.connect()

        # The real Gmail App Password, which delivery would have loaded.
        with hermetic_boundary.expect_blocked("credential"):
            with pytest.raises(
                (hermetic_boundary.HermeticBoundaryViolation,
                 credentials.CredentialError)
            ):
                credentials.load()

        # And the collector's very first act, resolving Rowan's host.
        with hermetic_boundary.expect_blocked("dns"):
            with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
                REAL_RUN_COLLECTION(target_date=TARGET_DATE, page_size=5)

    # The scope was bounded, so the redirection is back.
    assert db.database_path() != REAL_DATABASE
    assert hermetic_boundary.VIOLATIONS == []


def test_the_repaired_calendar_test_no_longer_reverts_the_suite(settings_obj):
    """The offending module is repaired at the source level too.

    A behavioural test can only observe its own scope. This reads the module
    that carried the defect and requires that the fix is the bounded scope,
    not a differently-spelled revert.
    """
    source = (TESTS_DIR / "test_daily_calendar_repeats.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    offending = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "test_a_calendar_failure_never_costs_the_digest"
    )
    calls = [
        node.func.attr
        for node in ast.walk(offending)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    ]
    assert "undo" not in calls
    assert "context" in calls


# --- the tripwires, one per thing that must never happen ----------------------


def test_the_boundary_refuses_to_resolve_the_rowan_endpoint():
    """`apps.rowan.edu` is the collector's host; resolving it is the first step."""
    with hermetic_boundary.expect_blocked("dns") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            socket.getaddrinfo("apps.rowan.edu", 443)
    assert probes[-1]["kind"] == "dns"
    assert "apps.rowan.edu" in probes[-1]["detail"]


def test_the_boundary_refuses_to_resolve_the_gmail_smtp_host():
    with hermetic_boundary.expect_blocked("dns") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            socket.getaddrinfo("smtp.gmail.com", 587)
    assert "smtp.gmail.com" in probes[-1]["detail"]


def test_the_boundary_refuses_an_external_socket_connection():
    """Closed at the socket as well as at DNS, so an IP literal is no bypass."""
    with hermetic_boundary.expect_blocked("socket") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(0.1)
                sock.connect(UNREACHED_EXTERNAL_ADDRESS)
    assert probes[-1]["kind"] == "socket"


def test_the_boundary_permits_loopback():
    """The guard must be about egress, not about sockets existing at all.

    `AI_NUMERICHOST` so this asserts the boundary's own decision and not the
    presence of a name-service configuration, which a sandboxed validation run
    does not have.
    """
    assert socket.getaddrinfo(
        "127.0.0.1", 0, socket.AF_INET, socket.SOCK_STREAM, 0, socket.AI_NUMERICHOST
    )
    assert hermetic_boundary.VIOLATIONS == []


def test_the_boundary_refuses_an_smtp_conversation():
    import smtplib

    with hermetic_boundary.expect_blocked("smtp", "dns") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            smtplib.SMTP("smtp.gmail.com", 587, timeout=1)
    assert probes[-1]["kind"] in ("smtp", "dns")


def test_the_boundary_refuses_to_read_the_real_gmail_credentials():
    """Refused before the open, so the file's bytes are never in the process.

    Deliberately probed by path: the audit event fires ahead of the filesystem
    lookup, so this holds on a machine where the operator file does not exist,
    and it never depends on -- or reveals -- the contents.
    """
    with hermetic_boundary.expect_blocked("credential") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            open(REAL_CREDENTIALS, encoding="utf-8").close()
    assert probes[-1]["kind"] == "credential"
    assert probes[-1]["detail"].endswith("credentials.env")


def test_the_boundary_refuses_the_production_credential_loader():
    """The same refusal through the production code path, not just `open`."""
    with hermetic_boundary.expect_blocked("credential"):
        with pytest.raises(
            (hermetic_boundary.HermeticBoundaryViolation, credentials.CredentialError)
        ):
            credentials.load(REAL_CREDENTIALS)


def test_the_fixture_credentials_still_load_normally(settings_obj):
    """The boundary must not have broken the thing tests legitimately need."""
    loaded = credentials.load()
    assert loaded.username == "digest-test@gmail.com"
    assert settings_module.credentials_path() != REAL_CREDENTIALS
    # Never assert on the value itself; that it is redacted in `repr` is the
    # assertion that matters here.
    assert "password=<redacted>" in repr(loaded)


def test_the_boundary_refuses_the_production_state_directory():
    with hermetic_boundary.expect_blocked("production-path") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            open(REAL_STATE_DIR / "dailymail.lock", "w").close()
    assert probes[-1]["kind"] == "production-path"


def test_the_boundary_refuses_the_production_database():
    with hermetic_boundary.expect_blocked("production-sqlite", "production-path") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            sqlite3.connect(str(REAL_DATABASE)).close()
    assert probes[-1]["kind"] in ("production-sqlite", "production-path")


def test_the_boundary_refuses_production_systemd():
    with hermetic_boundary.expect_blocked("systemd") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            subprocess.run(
                ["systemctl", "--user", "start", "dailymail.service"],
                capture_output=True,
            )
    assert probes[-1]["kind"] == "systemd"


def test_the_boundary_refuses_mutating_the_immutable_release():
    with hermetic_boundary.expect_blocked("release-mutation") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            os.symlink(RELEASE_ROOT / "probe", RELEASE_ROOT / "current")
    assert probes[-1]["kind"] == "release-mutation"


def test_the_boundary_refuses_the_production_entrypoint():
    with hermetic_boundary.expect_blocked("production-subprocess", "systemd") as probes:
        with pytest.raises(hermetic_boundary.HermeticBoundaryViolation):
            subprocess.run(
                [str(RELEASE_ROOT / "current" / ".venv" / "bin" / "dailymail"),
                 "run-daily"],
                capture_output=True,
            )
    assert probes[-1]["kind"] in ("production-subprocess", "systemd")


def test_every_refusal_so_far_was_one_this_module_asked_for():
    """Probing the guard must not look like breaching it."""
    assert hermetic_boundary.VIOLATIONS == []
    assert hermetic_boundary.PROBES, "the probes above should have been recorded"
    assert all(entry["kind"] for entry in hermetic_boundary.PROBES)
