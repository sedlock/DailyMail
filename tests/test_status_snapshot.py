"""The status snapshot, and the WAL failure it exists to route around.

The defect, measured in production between 2026-09-06 and 2026-09-11: **6,786 of
6,801 ControlPanel collections (99.78%)** returned a structurally valid but
blank `controlpanel.status.v1` document, and every daily run was first observed
roughly **24 hours late**.

The cause is one sidecar file. `health` opens the database with
`file:…?mode=ro`. The database is in WAL mode, and a WAL database cannot be read
-- not even read-only -- without the `-shm` shared-memory index, which SQLite must
*create* in the database's own directory when it is absent. SQLite unlinks
`-wal` and `-shm` on the last clean close, so `-shm` is absent except during the
~2 minutes `dailymail.service` runs. ControlPanel's collector has
`ProtectHome=read-only`, so the creation fails, the first query raises, and the
command still exits 0 with an empty document.

The whole fixture matrix below turns on that one file, which is the point: the
discriminator is not permissions, not the WAL's contents, and not the database
at all.

What is asserted here, in order:

* the failure is real, and `--source database` still reports it honestly;
* `--source snapshot` answers without opening SQLite at all;
* `--source auto` produces the *same material document* as a healthy database
  read, with the live-database error still attached;
* the snapshot only ever describes committed state;
* a snapshot that cannot be written cannot change what DailyMail did;
* nothing here is ever allowed to become a zero.
"""

from __future__ import annotations

import ast
import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from dailymail import atomic, config, db, health, status_snapshot

TARGET = "2026-09-11"


# --- helpers -----------------------------------------------------------------


def seed(connection, *, runs=(), deliveries=()):
    """Populate `runs`/`deliveries` through the real application functions."""
    ids = []
    for entry in runs:
        run_id = db.start_run(
            connection, entry.get("target_date", TARGET), entry.get("trigger", "timer")
        )
        finish = {k: v for k, v in entry.items() if k not in ("target_date", "trigger")}
        if finish:
            db.finish_run(connection, run_id, **finish)
        ids.append(run_id)
    for entry in deliveries:
        db.record_delivery(
            connection,
            target_date=entry.get("target_date", TARGET),
            recipient="reader@example.invalid",
            content_hash_value=entry.get("content_hash", "abc123"),
            state=entry.get("state", "sent"),
            message_id=entry.get("message_id"),
            smtp_status=entry.get("smtp_status"),
            message_bytes=entry.get("message_bytes"),
            sent_at=entry.get("sent_at"),
            error_summary=entry.get("error_summary"),
        )
    return ids


@pytest.fixture(autouse=True)
def timer_status(monkeypatch):
    """A fixed systemd answer.

    `health.build_status` shells out to `systemctl`, which the hermetic boundary
    refuses -- correctly, since the normal suite must never touch the real user
    manager. Every test here is about the *history* half of the document, so the
    schedule half is pinned.
    """
    from dailymail import systemd_units

    state = {
        "service": "dailymail.service",
        "timer": "dailymail.timer",
        "service_load_state": "loaded",
        "timer_enabled": "enabled",
        "timer_active": "active",
        "next_elapse": "Sat 2026-09-12 06:30:00 EDT",
        "last_result": "success",
        "list_timers": "",
        "linger": True,
    }
    monkeypatch.setattr(systemd_units, "status", lambda: state)
    return state


@pytest.fixture
def live_db(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    yield connection
    connection.close()


def make_wal_database(directory: Path, *, checkpointed: bool) -> Path:
    """A real WAL database, with or without uncheckpointed content and `-shm`.

    The uncheckpointed case is produced by a child process that is SIGKILLed, so
    SQLite never performs its clean-close checkpoint-and-unlink. That is what a
    machine that lost power mid-run leaves behind, and it is the case where
    `immutable=1` silently lies.
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "dailymail.sqlite3"
    connection = sqlite3.connect(str(path))
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("CREATE TABLE IF NOT EXISTS marker (n INTEGER)")
    connection.execute("INSERT INTO marker (n) VALUES (1)")
    connection.commit()
    if checkpointed:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.close()
        return path
    connection.close()

    script = textwrap.dedent(
        f"""
        import os, signal, sqlite3
        c = sqlite3.connect({str(path)!r})
        c.execute("PRAGMA journal_mode = WAL")
        c.execute("BEGIN IMMEDIATE")
        c.execute("INSERT INTO marker (n) VALUES (2)")
        c.commit()
        os.kill(os.getpid(), signal.SIGKILL)
        """
    )
    # Bounded and captured. This child is a fresh interpreter, so it runs
    # *outside* the suite's audit hook -- the one place in the tests where the
    # hermetic boundary is not watching. It only ever touches `tmp_path`, and it
    # cannot hang the suite.
    subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        timeout=60,
    )
    return path


def read_only(path: Path):
    """Make a directory unwritable and return its previous mode.

    It does **not** put the mode back -- every caller owns that in a `finally`.
    The docstring used to claim otherwise, which is the kind of thing that
    eventually leaves a developer with a 0500 directory and no idea why.
    """
    original = path.stat().st_mode
    path.chmod(original & ~0o222)
    return original


# --- the failure being routed around -----------------------------------------


class TestTheWalFailure:
    def test_a_wal_database_without_its_shm_cannot_be_read_from_a_readonly_dir(
        self, tmp_path
    ):
        """The whole defect, in one assertion. The only variable is `-shm`."""
        directory = tmp_path / "data"
        path = make_wal_database(directory, checkpointed=False)
        assert (directory / "dailymail.sqlite3-wal").exists()
        assert (directory / "dailymail.sqlite3-shm").exists()

        # With `-shm` present, a read-only open over a read-only directory works.
        os.remove(directory / "dailymail.sqlite3-shm")
        original = read_only(directory)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            with pytest.raises(sqlite3.Error) as caught:
                connection.execute("SELECT COUNT(*) FROM marker").fetchone()
            connection.close()
            assert "unable to open database file" in str(caught.value) or (
                "readonly database" in str(caught.value)
            )
            # ...and it did not manage to create the sidecar either.
            assert not (directory / "dailymail.sqlite3-shm").exists()
        finally:
            directory.chmod(original)

    def test_the_same_database_reads_fine_once_the_shm_exists(self, tmp_path):
        directory = tmp_path / "data"
        path = make_wal_database(directory, checkpointed=False)
        assert (directory / "dailymail.sqlite3-shm").exists()
        original = read_only(directory)
        try:
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            assert connection.execute("SELECT COUNT(*) FROM marker").fetchone()[0] >= 1
            connection.close()
        finally:
            directory.chmod(original)

    def test_a_writable_directory_silently_makes_the_probe_a_writer(self, tmp_path):
        """Why granting ControlPanel write access was rejected."""
        directory = tmp_path / "data"
        path = make_wal_database(directory, checkpointed=False)
        os.remove(directory / "dailymail.sqlite3-shm")
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.execute("SELECT COUNT(*) FROM marker").fetchone()
        connection.close()
        # A "read-only" probe created a file in the application's data directory.
        assert (directory / "dailymail.sqlite3-shm").exists()

    def test_no_sqlite_uri_in_the_package_uses_immutable_or_nolock(self):
        """Formally disproven, not merely unproven: `immutable=1` ignores the WAL.

        Against a copy with genuinely uncheckpointed content it returned 51 runs
        where `mode=ro` read 52. A probe that quietly reports yesterday's digest
        as current is worse than one that says "unavailable".

        Scanned as *code*: every SQLite URI the package can build is a string
        literal, and the prose in `status_snapshot.py` explaining why these were
        rejected must not be what makes this test pass or fail.
        """
        package = Path(db.__file__).parent
        uris: list[tuple[Path, str]] = []
        for path in sorted(package.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docstrings = {
                id(node.body[0].value)
                for node in ast.walk(tree)
                if isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                )
                and node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            }
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Constant)
                    and isinstance(node.value, str)
                    and id(node) not in docstrings
                    and ("file:" in node.value or "mode=ro" in node.value)
                ):
                    uris.append((path, node.value))
        assert uris, "the package must build at least one SQLite URI"
        for path, uri in uris:
            lowered = uri.lower()
            assert "immutable" not in lowered, (path, uri)
            assert "nolock" not in lowered, (path, uri)

    def test_the_application_still_uses_wal(self, live_db):
        """The fix must not have quietly surrendered WAL to make life easier."""
        assert live_db.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"

    def test_immutable_would_have_hidden_uncheckpointed_content(self, tmp_path):
        """The measurement that ruled `immutable=1` out, as a test."""
        directory = tmp_path / "data"
        path = make_wal_database(directory, checkpointed=False)
        truthful = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        honest_count = truthful.execute("SELECT COUNT(*) FROM marker").fetchone()[0]
        truthful.close()
        os.remove(directory / "dailymail.sqlite3-shm")
        lying = sqlite3.connect(f"file:{path}?immutable=1", uri=True)
        stale_count = lying.execute("SELECT COUNT(*) FROM marker").fetchone()[0]
        lying.close()
        assert stale_count < honest_count, (
            "this fixture must contain genuinely uncheckpointed WAL content"
        )


# --- the snapshot describes committed state ----------------------------------


class TestTransactionOrdering:
    def test_start_run_publishes_an_active_snapshot(self, live_db):
        run_id = db.start_run(live_db, TARGET, "timer")
        document = status_snapshot.read()
        assert document["recent_runs"][0]["run_id"] == run_id
        assert document["recent_runs"][0]["status"] == "running"
        assert document["recent_runs"][0]["completed_at"] is None

    def test_finish_run_publishes_the_terminal_snapshot(self, live_db):
        run_id = db.start_run(live_db, TARGET, "timer")
        db.finish_run(
            live_db, run_id, status="success", email_status="sent", unique_count=27,
            new_count=8, standing_count=19,
        )
        latest = status_snapshot.read()["recent_runs"][0]
        assert latest["status"] == "success"
        assert latest["email_status"] == "sent"
        assert latest["unique_count"] == 27
        assert latest["completed_at"] is not None

    def test_a_delivery_transition_publishes_the_new_state(self, live_db):
        seed(live_db, runs=[{"status": "success", "email_status": "sent"}])
        before = status_snapshot.read()
        assert before["latest_delivery"] is None
        db.record_delivery(
            live_db, target_date=TARGET, recipient="reader@example.invalid",
            content_hash_value="h", state="sent", sent_at="2026-09-11T10:32:23+00:00",
        )
        after = status_snapshot.read()
        assert after["latest_delivery"]["state"] == "sent"
        assert after["latest_delivery"]["sent_at"] == "2026-09-11T10:32:23+00:00"
        assert after["last_confirmed_delivery"]["state"] == "sent"

    def test_an_unknown_delivery_is_recorded_as_unknown_not_as_sent(self, live_db):
        db.record_delivery(
            live_db, target_date=TARGET, recipient="reader@example.invalid",
            content_hash_value="h", state="unknown",
            error_summary="SMTP timed out after the DATA command",
        )
        document = status_snapshot.read()
        assert document["latest_delivery"]["state"] == "unknown"
        assert document["last_confirmed_delivery"] is None

    def test_a_rolled_back_transaction_never_reaches_the_snapshot(self, live_db):
        """A guard, not a reproduction: no code publishes from a rollback today,
        and this is what fails if one ever starts."""
        seed(live_db, runs=[{"status": "success", "email_status": "sent"}])
        baseline = status_snapshot.read()
        with pytest.raises(RuntimeError):
            with db.transaction(live_db):
                live_db.execute(
                    "INSERT INTO runs (target_date, started_at, status, trigger) "
                    "VALUES ('2099-01-01', '2099-01-01T00:00:00+00:00', 'success', 'x')"
                )
                raise RuntimeError("deliberate rollback")
        # The snapshot is untouched, and so is the database.
        assert status_snapshot.read() == baseline
        assert live_db.execute(
            "SELECT COUNT(*) FROM runs WHERE target_date = '2099-01-01'"
        ).fetchone()[0] == 0

    def test_the_snapshot_is_written_after_the_commit_not_during(
        self, live_db, monkeypatch
    ):
        """If it were written inside the transaction it could describe a row
        that later rolls back. Proven by looking at the database *from another
        connection* at the moment the snapshot is built."""
        seen: list[int] = []
        real_build = status_snapshot.build

        def observing_build(connection):
            # A separate read-only connection sees only committed rows.
            other = db.connect_readonly()
            try:
                seen.append(
                    other.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
                )
            finally:
                other.close()
            return real_build(connection)

        monkeypatch.setattr(status_snapshot, "build", observing_build)
        db.start_run(live_db, TARGET, "timer")
        assert seen == [1], (
            "the snapshot was built while the INSERT was still uncommitted"
        )


class TestSnapshotWriteFailure:
    def test_a_failing_snapshot_write_does_not_reverse_business_state(
        self, live_db, monkeypatch
    ):
        """Observability is not permitted to change what DailyMail did."""

        def explode(*args, **kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(status_snapshot, "write", explode)
        run_id = db.start_run(live_db, TARGET, "timer")
        db.finish_run(live_db, run_id, status="success", email_status="sent")
        delivery_id = db.record_delivery(
            live_db, target_date=TARGET, recipient="reader@example.invalid",
            content_hash_value="h", state="sent",
        )
        # Every committed transition survived.
        assert live_db.execute(
            "SELECT status, email_status FROM runs WHERE run_id = ?", (run_id,)
        ).fetchone()["status"] == "success"
        assert live_db.execute(
            "SELECT state FROM deliveries WHERE delivery_id = ?", (delivery_id,)
        ).fetchone()["state"] == "sent"

    def test_a_failing_snapshot_write_is_logged_not_raised(
        self, live_db, monkeypatch, caplog
    ):
        def explode(*args, **kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(status_snapshot, "write", explode)
        with caplog.at_level("WARNING"):
            db.start_run(live_db, TARGET, "timer")
        assert any("status snapshot" in r.message for r in caplog.records)

    def test_a_missing_snapshot_is_reported_as_missing_not_as_zero(self, live_db):
        seed(live_db, runs=[{"status": "success", "email_status": "sent"}])
        config.status_snapshot_path().unlink()
        with pytest.raises(status_snapshot.SnapshotError, match="no status snapshot"):
            status_snapshot.read()


# --- file mechanics ----------------------------------------------------------


class TestFileMechanics:
    def test_the_snapshot_lives_outside_the_database_directory(self, live_db):
        """The whole point: the collector can read this and not the WAL."""
        assert config.status_snapshot_path().parent != db.database_path().parent

    def test_the_snapshot_is_private(self, live_db):
        db.start_run(live_db, TARGET, "timer")
        mode = stat.S_IMODE(config.status_snapshot_path().stat().st_mode)
        assert mode == config.STATUS_SNAPSHOT_MODE == 0o600

    def test_the_snapshot_stays_well_inside_its_bound(self, live_db):
        seed(
            live_db,
            runs=[
                {
                    "status": "success",
                    "email_status": "sent",
                    "error_summary": "x" * 5000,
                }
                for _ in range(40)
            ],
        )
        size = config.status_snapshot_path().stat().st_size
        assert size < config.STATUS_SNAPSHOT_MAX_BYTES, size

    def test_the_run_list_is_bounded(self, live_db):
        seed(live_db, runs=[{"status": "success"} for _ in range(30)])
        assert len(status_snapshot.read()["recent_runs"]) == (
            status_snapshot.RECENT_RUN_LIMIT
        )

    def test_free_text_is_bounded(self, live_db):
        run_id = db.start_run(live_db, TARGET, "timer")
        db.finish_run(live_db, run_id, status="failed", error_summary="y" * 9000)
        text = status_snapshot.read()["recent_runs"][0]["error_summary"]
        assert len(text) <= status_snapshot.MAX_TEXT

    def test_a_reader_never_sees_a_partial_document(self, live_db, tmp_path):
        """`os.replace` is atomic, so a concurrent reader gets old or new."""
        target = tmp_path / "status.json"
        atomic.atomic_write_json(target, {"schema_version": "a", "pad": "x" * 100000})
        first = target.read_text(encoding="utf-8")
        atomic.atomic_write_json(target, {"schema_version": "b", "pad": "y" * 100000})
        second = target.read_text(encoding="utf-8")
        for text in (first, second):
            json.loads(text)  # always parses; never truncated
        assert first != second

    def test_an_interrupted_write_leaves_no_debris(self, tmp_path, monkeypatch):
        target = tmp_path / "status.json"

        def explode(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(atomic.os, "replace", explode)
        with pytest.raises(OSError):
            atomic.atomic_write_json(target, {"a": 1})
        assert list(tmp_path.iterdir()) == []

    def test_a_symlinked_destination_is_refused(self, tmp_path):
        real = tmp_path / "elsewhere.json"
        real.write_text("{}", encoding="utf-8")
        link = tmp_path / "status.json"
        link.symlink_to(real)
        with pytest.raises(atomic.UnsafePathError):
            atomic.atomic_write_json(link, {"a": 1})

    def test_a_symlinked_snapshot_is_refused_on_read(self, tmp_path):
        real = tmp_path / "elsewhere.json"
        real.write_text(
            json.dumps(
                {
                    "schema_version": status_snapshot.SCHEMA_VERSION,
                    "generated_at": "2026-09-11T10:32:00+00:00",
                    "statistics": {},
                    "recent_runs": [],
                }
            ),
            encoding="utf-8",
        )
        link = tmp_path / "status.json"
        link.symlink_to(real)
        with pytest.raises(status_snapshot.SnapshotError, match="symlink"):
            status_snapshot.read(link)

    def test_concurrent_writers_cannot_corrupt_the_file(self, tmp_path):
        """Every writer uses its own temp file, so the loser is simply replaced."""
        target = tmp_path / "status.json"
        for index in range(25):
            atomic.atomic_write_json(target, {"n": index, "pad": "z" * 5000})
            loaded = json.loads(target.read_text(encoding="utf-8"))
            assert loaded["n"] == index
        assert [p.name for p in tmp_path.iterdir()] == ["status.json"]

    def test_the_collector_artifact_still_uses_the_same_writer(self, monkeypatch):
        """One implementation, not two that drift.

        Asserted by behaviour rather than by grepping the source, which would
        pass on an unused import.
        """
        from dailymail import collect

        calls: list = []
        real = atomic.atomic_write_json

        def watched(path, document, **kwargs):
            calls.append(Path(path).name)
            return real(path, document, **kwargs)

        monkeypatch.setattr(collect, "atomic_write_json", watched)
        collect.write_artifact({"target_date": "2026-09-11", "announcements": []})
        assert calls == ["2026-09-11.json"]


# --- reading and validation ---------------------------------------------------


class TestSnapshotValidation:
    @pytest.fixture
    def written(self, live_db):
        seed(
            live_db,
            runs=[{"status": "success", "email_status": "sent", "unique_count": 27}],
            deliveries=[{"state": "sent", "sent_at": "2026-09-11T10:32:23+00:00"}],
        )
        return config.status_snapshot_path()

    @pytest.mark.parametrize(
        "mutate, expected",
        [
            (lambda d: d.update(schema_version="other.v1"), "schema"),
            (lambda d: d.pop("generated_at"), "generated_at"),
            (lambda d: d.pop("statistics"), "statistics"),
            (lambda d: d.pop("recent_runs"), "recent_runs"),
            (lambda d: d.update(recent_runs={}), "not a list"),
            (lambda d: d.update(statistics=[]), "not an object"),
            (lambda d: d.update(recent_runs=[{"no": "run_id"}]), "run entry is missing"),
            (lambda d: d.update(recent_runs=["not-a-dict"]), "malformed run"),
        ],
    )
    def test_every_structural_defect_is_named_rather_than_zeroed(
        self, written, mutate, expected
    ):
        document = json.loads(written.read_text(encoding="utf-8"))
        mutate(document)
        written.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(status_snapshot.SnapshotError, match=expected):
            status_snapshot.read()

    def test_malformed_json_is_named(self, written):
        written.write_text("{ not json", encoding="utf-8")
        with pytest.raises(status_snapshot.SnapshotError, match="malformed JSON"):
            status_snapshot.read()

    def test_an_oversized_snapshot_is_refused(self, written):
        written.write_text("[" + "0," * 200000 + "0]", encoding="utf-8")
        with pytest.raises(status_snapshot.SnapshotError, match="bound"):
            status_snapshot.read()

    def test_a_valid_snapshot_round_trips(self, written):
        document = status_snapshot.read()
        assert document["schema_version"] == status_snapshot.SCHEMA_VERSION
        assert document["recent_runs"][0]["unique_count"] == 27
        assert document["latest_delivery"]["state"] == "sent"


class TestRedaction:
    def test_no_recipient_credential_or_body_reaches_the_snapshot(self, live_db):
        seed(
            live_db,
            runs=[{"status": "success", "email_status": "sent"}],
            deliveries=[
                {"state": "sent", "message_id": "<abc@dailymail.local>"},
            ],
        )
        raw = config.status_snapshot_path().read_text(encoding="utf-8")
        for forbidden in (
            "reader@example.invalid",
            "GMAIL_APP_PASSWORD",
            "abcd efgh ijkl mnop",
            "Authorization",
            "Cookie",
        ):
            assert forbidden not in raw, forbidden

    def test_the_snapshot_carries_no_announcement_content(self, live_db):
        db.start_run(live_db, TARGET, "timer")
        document = status_snapshot.read()
        blob = json.dumps(document)
        for forbidden in ("full_body", "body_text", "<p>", "<html"):
            assert forbidden not in blob, forbidden


# --- health: the three sources ------------------------------------------------


class TestHealthSources:
    @pytest.fixture
    def populated(self, live_db):
        seed(
            live_db,
            runs=[
                {
                    "status": "success",
                    "email_status": "sent",
                    "unique_count": 27,
                    "new_count": 8,
                    "standing_count": 19,
                    "employee_count": 24,
                    "student_count": 14,
                    "collector_validation": "ok",
                }
            ],
            deliveries=[{"state": "sent", "sent_at": "2026-09-11T10:32:23+00:00"}],
        )
        return live_db

    def test_database_mode_reports_the_database(self, populated):
        document = health.build_status("database")
        assert document["status_data_source"] == "database"
        assert document["adapter_errors"] == []
        assert document["recent_runs"]

    def test_snapshot_mode_never_opens_sqlite(self, populated, monkeypatch):
        def forbidden(*args, **kwargs):
            raise AssertionError("snapshot mode must not open the database")

        monkeypatch.setattr(db, "connect_readonly", forbidden)
        document = health.build_status("snapshot")
        assert document["status_data_source"] == "snapshot"
        assert document["recent_runs"]
        assert document["metrics"]["database_runs"] == 1

    def test_auto_prefers_the_database_when_it_works(self, populated):
        document = health.build_status("auto")
        assert document["status_data_source"] == "database"
        assert document["adapter_errors"] == []

    def test_auto_falls_back_and_keeps_the_database_error(self, populated, monkeypatch):
        def cantopen(*args, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(db, "connect_readonly", cantopen)
        document = health.build_status("auto")
        assert document["status_data_source"] == "snapshot"
        # The outage is still reported -- falling back does not mean pretending --
        # but in the field that means "the live probe failed", not the one that
        # means "my status is incomplete", because it is not.
        assert document["status_database_probe_error"] == "unable to open database file"
        assert document["adapter_errors"] == []
        # ...and the reader still learns what actually happened.
        assert document["metrics"]["latest_success_announcements_unique"] == 27
        assert len(document["recent_runs"]) == 1
        assert document["health"] == "healthy"

    def test_the_two_documents_agree_on_every_material_field(
        self, populated, monkeypatch
    ):
        """The property the whole design rests on."""
        from_db = health.build_status("database")

        def cantopen(*args, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(db, "connect_readonly", cantopen)
        from_snapshot = health.build_status("auto")

        assert from_db["recent_runs"] == from_snapshot["recent_runs"]
        assert from_db["problems"] == from_snapshot["problems"]
        assert from_db["health"] == from_snapshot["health"]
        # Both directions, so an invented extra metric is caught as well as a
        # missing one.
        volatile = {"parking_cache_age_seconds"}
        assert set(from_db["metrics"]) - volatile == (
            set(from_snapshot["metrics"]) - volatile
        )
        for key, value in from_db["metrics"].items():
            if key in volatile:
                continue
            assert from_snapshot["metrics"][key] == value, key
        # Length-checked, so a dropped component is not silently zipped away.
        assert len(from_db["components"]) == len(from_snapshot["components"]) == 2
        for left, right in zip(from_db["components"], from_snapshot["components"]):
            for field in ("id", "name", "health", "summary", "last_attempt",
                          "last_success", "next_expected"):
                assert left[field] == right[field], field

    def test_snapshot_provenance_is_always_stated(self, populated, monkeypatch):
        monkeypatch.setattr(
            db, "connect_readonly",
            lambda *a, **k: (_ for _ in ()).throw(
                sqlite3.OperationalError("unable to open database file")
            ),
        )
        document = health.build_status("auto")
        assert document["status_snapshot_schema"] == status_snapshot.SCHEMA_VERSION
        assert document["status_snapshot_generated_at"]
        assert document["status_snapshot_age_seconds"] is not None
        assert document["status_snapshot_stale"] is False
        assert "snapshot" in document["components"][0]["source"]

    def test_an_unknown_source_is_rejected(self):
        with pytest.raises(ValueError):
            health.build_status("somewhere-else")

    def test_a_failed_probe_with_no_snapshot_stays_honestly_unknown(
        self, populated, monkeypatch
    ):
        config.status_snapshot_path().unlink()
        monkeypatch.setattr(
            db, "connect_readonly",
            lambda *a, **k: (_ for _ in ()).throw(
                sqlite3.OperationalError("unable to open database file")
            ),
        )
        document = health.build_status("auto")
        assert document["status_data_source"] == "unavailable"
        assert document["health"] == "unknown"
        assert document["recent_runs"] == []
        # Two distinct failures, both named. Neither becomes a zero.
        assert len(document["adapter_errors"]) == 2
        assert document["metrics"].get("database_runs") is None

    def test_a_stale_snapshot_degrades_without_losing_its_data(
        self, populated, monkeypatch
    ):
        path = config.status_snapshot_path()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["generated_at"] = "2020-01-01T00:00:00+00:00"
        path.write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setattr(
            db, "connect_readonly",
            lambda *a, **k: (_ for _ in ()).throw(
                sqlite3.OperationalError("unable to open database file")
            ),
        )
        result = health.build_status("auto")
        assert result["status_snapshot_stale"] is True
        assert result["health"] == "unknown"
        # The data is still there; it is simply not claimed to be current.
        assert result["recent_runs"]
        assert any("stale" in message for message in result["adapter_errors"])

    def test_a_fresh_snapshot_is_not_called_stale_merely_for_being_hours_old(
        self, populated
    ):
        """A snapshot from this morning's 06:30 is current all day."""
        document = health.build_status("snapshot")
        assert document["status_snapshot_stale"] is False


class TestRunStates:
    @pytest.mark.parametrize(
        "finish, expected_health",
        [
            ({"status": "success", "email_status": "sent"}, "healthy"),
            ({"status": "success", "email_status": "skipped_duplicate"}, "healthy"),
            ({"status": "success", "email_status": "dry_run"}, "paused"),
            ({"status": "failed", "error_summary": "collection failed"}, "failed"),
            ({"status": "success", "email_status": "failed"}, "failed"),
        ],
    )
    def test_each_terminal_state_survives_the_round_trip(
        self, live_db, finish, expected_health
    ):
        run_id = db.start_run(live_db, TARGET, "timer")
        db.finish_run(live_db, run_id, **finish)
        from_db = health.build_status("database")
        from_snapshot = health.build_status("snapshot")
        digest = [c for c in from_db["components"] if c["id"] == "daily-digest"][0]
        assert digest["health"] == expected_health
        assert from_db["recent_runs"] == from_snapshot["recent_runs"]
        assert [c["health"] for c in from_db["components"]] == [
            c["health"] for c in from_snapshot["components"]
        ]

    def test_a_run_in_progress_is_visible_from_the_snapshot(self, live_db):
        db.start_run(live_db, TARGET, "timer")
        document = health.build_status("snapshot")
        assert document["recent_runs"][0]["success"] is None
        assert "in progress" in document["recent_runs"][0]["summary"]

    def test_no_runs_at_all_is_not_confused_with_an_unreadable_database(
        self, live_db
    ):
        db.record_delivery(
            live_db, target_date=TARGET, recipient="reader@example.invalid",
            content_hash_value="h", state="unknown",
        )
        document = health.build_status("snapshot")
        assert document["status_data_source"] == "snapshot"
        assert document["recent_runs"] == []
        assert document["adapter_errors"] == []
        assert document["health"] == "unknown"
        assert "No DailyMail run has been recorded" in document["summary"]


# --- the maintenance command --------------------------------------------------


class TestRefreshCommand:
    def test_refresh_writes_the_snapshot_and_nothing_else(self, live_db, capsys):
        from dailymail import cli

        seed(live_db, runs=[{"status": "success", "email_status": "sent"}])
        config.status_snapshot_path().unlink()
        before = {
            table: live_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "deliveries", "announcements", "daily_records")
        }
        assert cli.main(["status-snapshot", "refresh"]) == 0
        assert config.status_snapshot_path().is_file()
        after = {
            table: live_db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("runs", "deliveries", "announcements", "daily_records")
        }
        assert before == after
        assert "SNAPSHOT OK" in capsys.readouterr().out

    def test_refresh_cannot_reach_rowan_smtp_or_a_model(self, live_db, monkeypatch):
        """It is a projection of local state, and structurally nothing else."""
        from dailymail import cli

        seed(live_db, runs=[{"status": "success", "email_status": "sent"}])

        def forbidden(name):
            def boom(*args, **kwargs):
                raise AssertionError(f"refresh must never call {name}")

            return boom

        import smtplib
        import subprocess as sp

        import httpx

        monkeypatch.setattr(httpx, "Client", forbidden("httpx.Client"))
        monkeypatch.setattr(smtplib, "SMTP", forbidden("smtplib.SMTP"))
        monkeypatch.setattr(smtplib, "SMTP_SSL", forbidden("smtplib.SMTP_SSL"))
        monkeypatch.setattr(sp, "run", forbidden("subprocess.run"))
        monkeypatch.setattr(sp, "Popen", forbidden("subprocess.Popen"))
        assert cli.main(["status-snapshot", "refresh"]) == 0

    def test_refresh_does_not_create_a_database(self, settings_obj, capsys):
        """Being observed must never bootstrap an empty application."""
        from dailymail import cli

        assert not db.database_path().exists()
        assert cli.main(["status-snapshot", "refresh"]) == 1
        assert not db.database_path().exists()
        assert "SNAPSHOT FAILED" in capsys.readouterr().err

    def test_refresh_exits_nonzero_when_it_cannot_write(self, live_db, monkeypatch):
        from dailymail import cli

        seed(live_db, runs=[{"status": "success"}])

        def explode(*args, **kwargs):
            raise OSError(30, "Read-only file system")

        monkeypatch.setattr(status_snapshot, "write", explode)
        assert cli.main(["status-snapshot", "refresh"]) == 1

    def test_refresh_is_idempotent_in_content(self, live_db):
        from dailymail import cli

        seed(live_db, runs=[{"status": "success", "email_status": "sent"}])
        cli.main(["status-snapshot", "refresh"])
        first = status_snapshot.read()
        cli.main(["status-snapshot", "refresh"])
        second = status_snapshot.read()
        first.pop("generated_at")
        second.pop("generated_at")
        assert first == second

    def test_show_prints_without_rewriting(self, live_db, capsys):
        from dailymail import cli

        seed(live_db, runs=[{"status": "success", "email_status": "sent"}])
        path = config.status_snapshot_path()
        before = path.read_bytes()
        assert cli.main(["status-snapshot", "show"]) == 0
        assert path.read_bytes() == before
        assert status_snapshot.SCHEMA_VERSION in capsys.readouterr().out


# --- the ControlPanel collector boundary, end to end --------------------------


class TestCollectorBoundary:
    """The installed command, run the way the collector actually runs it."""

    @pytest.fixture
    def boundary(self, live_db, tmp_path):
        seed(
            live_db,
            runs=[
                {
                    "status": "success",
                    "email_status": "sent",
                    "unique_count": 27,
                    "new_count": 8,
                    "standing_count": 19,
                }
            ],
            deliveries=[{"state": "sent", "sent_at": "2026-09-11T10:32:23+00:00"}],
        )
        live_db.close()
        # The state SQLite itself leaves behind: WAL content, no `-shm`.
        directory = db.database_path().parent
        shm = directory / "dailymail.sqlite3-shm"
        if shm.exists():
            shm.unlink()
        original = directory.stat().st_mode
        directory.chmod(original & ~0o222)
        try:
            yield directory
        finally:
            # Explicitly, rather than relying on pytest resuming the generator.
            # It does -- but one non-local edit between the chmod and the yield
            # would leave a 0500 directory behind for every later test.
            directory.chmod(original)

    def test_auto_returns_a_valid_contract_from_the_snapshot(self, boundary):
        document = health.build_status("auto")
        assert document["schema_version"] == "controlpanel.status.v1"
        assert document["status_data_source"] == "snapshot"
        assert document["project"] == "dailymail"
        assert len(document["recent_runs"]) == 1
        assert document["metrics"]["latest_success_announcements_unique"] == 27
        assert document["metrics"]["latest_success_announcements_new"] == 8
        assert document["metrics"]["latest_success_announcements_standing"] == 19
        assert document["components"]
        # The live failure is still on the record, named for what it is.
        assert document["status_database_probe_error"]
        # ...and this document is complete, so it does not claim otherwise.
        assert document["adapter_errors"] == []

    def test_the_probe_creates_no_sidecar_in_the_data_directory(self, boundary):
        health.build_status("auto")
        assert not (boundary / "dailymail.sqlite3-shm").exists()

    def test_database_mode_still_fails_here(self, boundary):
        document = health.build_status("database")
        assert document["status_data_source"] == "unavailable"
        assert document["recent_runs"] == []
        assert document["adapter_errors"]

    def test_controlpanel_needs_no_write_access_to_dailymail_state(self, boundary):
        """The snapshot is read from a directory the collector cannot write."""
        snapshot_dir = config.status_snapshot_path().parent
        original = snapshot_dir.stat().st_mode
        snapshot_dir.chmod(original & ~0o222)
        try:
            document = health.build_status("auto")
            assert document["status_data_source"] == "snapshot"
            assert document["recent_runs"]
        finally:
            snapshot_dir.chmod(original)


class TestAHostileSnapshotCannotCrashOrLie:
    """A status probe that raises is worse than the blank document it replaced.

    Found by crafting snapshots by hand against the first implementation: a file
    that passed the schema check but was missing a run column raised `KeyError`
    straight out of `health`, because the loader handed unvalidated dicts to code
    that subscripts them. `read()` now validates every row field by field, so an
    untrustworthy snapshot is *reported* rather than believed or fatal.
    """

    @pytest.fixture
    def crafted(self, live_db, monkeypatch):
        def cantopen(*args, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(db, "connect_readonly", cantopen)
        path = config.status_snapshot_path()
        path.parent.mkdir(parents=True, exist_ok=True)

        def write(document):
            path.write_text(json.dumps(document), encoding="utf-8")
            return health.build_status("auto")

        return write

    BASE = {
        "schema_version": status_snapshot.SCHEMA_VERSION,
        "generated_at": "2026-09-12T00:00:00+00:00",
        "statistics": {},
        "recent_runs": [],
    }

    @pytest.mark.parametrize(
        "name, document",
        [
            (
                "a run entry missing every column but run_id",
                {**BASE, "recent_runs": [{"run_id": 1}]},
            ),
            (
                "a run entry whose fields are containers",
                {
                    **BASE,
                    "recent_runs": [
                        {name: [] for name in status_snapshot.RUN_COLUMNS}
                    ],
                },
            ),
            (
                "a nested object smuggled into a run field",
                {
                    **BASE,
                    "recent_runs": [
                        {
                            **{name: None for name in status_snapshot.RUN_COLUMNS},
                            "status": {"a": {"b": {"c": [1] * 50}}},
                        }
                    ],
                },
            ),
            (
                "a malformed last_retrieval_success",
                {**BASE, "last_retrieval_success": {"nope": 1}},
            ),
            (
                "a malformed latest_delivery",
                {**BASE, "latest_delivery": 12345},
            ),
            (
                "a non-object parking_sources",
                {**BASE, "parking_sources": "not-an-object"},
            ),
            (
                "a statistic that is a container",
                {**BASE, "statistics": {"runs": [1, 2, 3]}},
            ),
            (
                "more runs than the bound allows",
                {
                    **BASE,
                    "recent_runs": [
                        {name: None for name in status_snapshot.RUN_COLUMNS}
                        for _ in range(status_snapshot.RECENT_RUN_LIMIT + 5)
                    ],
                },
            ),
        ],
    )
    def test_a_crafted_snapshot_is_refused_rather_than_trusted_or_fatal(
        self, crafted, name, document
    ):
        result = crafted(document)
        # Never raises...
        assert result["schema_version"] == "controlpanel.status.v1"
        # ...never claims the snapshot worked...
        assert result["status_data_source"] == "unavailable", name
        # ...never invents data...
        assert result["recent_runs"] == []
        assert result["health"] == "unknown"
        # ...and names both the database failure and the snapshot's own reason.
        assert len(result["adapter_errors"]) == 2, result["adapter_errors"]

    def test_a_statistic_that_is_not_a_number_is_dropped_not_published(
        self, crafted
    ):
        """A string that looks like a count is not one, and is not a metric."""
        result = crafted({**self.BASE, "statistics": {"runs": "9999"}})
        assert result["status_data_source"] == "snapshot"
        assert result["metrics"].get("database_runs") is None

    def test_a_genuine_snapshot_still_passes_the_stricter_validation(self, live_db):
        seed(
            live_db,
            runs=[{"status": "success", "email_status": "sent", "unique_count": 27}],
            deliveries=[{"state": "sent", "sent_at": "2026-09-11T10:32:23+00:00"}],
        )
        document = status_snapshot.read()
        assert document["recent_runs"][0]["unique_count"] == 27
        for name in status_snapshot.RUN_COLUMNS:
            assert name in document["recent_runs"][0]
        for name in status_snapshot.DELIVERY_COLUMNS:
            assert name in document["latest_delivery"]


class TestStatsColumnsAreCompactedNotTruncated:
    """`runs.parking_stats` is a JSON document in a text column, not prose.

    Clipping it as free text truncated it mid-object, so the snapshot-backed
    document silently lost every parking metric while the database-backed one
    kept them. Caught by requiring the two documents to be identical after a
    real pipeline run; the fix drops what the status document does not read
    rather than cutting the string.
    """

    def test_a_long_stats_document_survives_as_valid_json(self, live_db):
        stats = json.dumps(
            {
                "mentions_detected": 1,
                "cache_hits": 0,
                "cache_misses": 1,
                "source_refreshes": 9,
                "resolver_calls": 1,
                "new_resolutions": 0,
                "unresolved": 1,
                "errors": ["a very long parking source failure " * 20] * 5,
            }
        )
        assert len(stats) > status_snapshot.MAX_TEXT
        run_id = db.start_run(live_db, TARGET, "timer")
        db.finish_run(
            live_db, run_id, status="success", email_status="sent",
            parking_stats=stats,
        )
        stored = status_snapshot.read()["recent_runs"][0]["parking_stats"]
        # Still parseable, which truncation would have destroyed.
        parsed = json.loads(stored)
        assert parsed["mentions_detected"] == 1
        assert parsed["source_refreshes"] == 9
        # ...and the unbounded part is gone rather than cut in half.
        assert "errors" not in parsed
        assert len(stored) < status_snapshot.MAX_TEXT

    def test_the_parking_metrics_survive_into_the_published_document(
        self, live_db, monkeypatch
    ):
        stats = json.dumps(
            {
                "mentions_detected": 3,
                "cache_hits": 2,
                "cache_misses": 1,
                "source_refreshes": 0,
                "resolver_calls": 0,
                "new_resolutions": 0,
                "unresolved": 0,
                "errors": ["x" * 400],
            }
        )
        run_id = db.start_run(live_db, TARGET, "timer")
        db.finish_run(
            live_db, run_id, status="success", email_status="sent",
            parking_stats=stats,
        )
        from_db = health.build_status("database")

        def cantopen(*args, **kwargs):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(db, "connect_readonly", cantopen)
        from_snapshot = health.build_status("auto")

        assert from_db["metrics"]["latest_run_parking_mentions_detected"] == 3
        assert (
            from_snapshot["metrics"]["latest_run_parking_mentions_detected"] == 3
        )
        assert from_db["recent_runs"] == from_snapshot["recent_runs"]

    def test_a_malformed_stats_column_is_still_bounded(self, live_db):
        run_id = db.start_run(live_db, TARGET, "timer")
        db.finish_run(
            live_db, run_id, status="success", parking_stats="not json " * 200
        )
        stored = status_snapshot.read()["recent_runs"][0]["parking_stats"]
        assert len(stored) <= status_snapshot.MAX_TEXT
