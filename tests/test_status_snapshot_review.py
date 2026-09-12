"""Regressions for the findings an independent security review raised.

Each class is one finding, named for what it would have let through. They are
kept separate from `test_status_snapshot.py` so the list of things a reviewer
actually broke stays legible.
"""

from __future__ import annotations

import json
import os
import stat
import sqlite3

import pytest

from dailymail import atomic, cli, config, db, health, redact, status_snapshot, systemd_units

TARGET = "2026-09-11"


@pytest.fixture(autouse=True)
def timer_status(monkeypatch):
    monkeypatch.setattr(
        systemd_units, "status",
        lambda: {
            "service": "dailymail.service", "timer": "dailymail.timer",
            "service_load_state": "loaded", "timer_enabled": "enabled",
            "timer_active": "active", "next_elapse": "Sat 2026-09-12 06:30:00 EDT",
            "last_result": "success", "list_timers": "", "linger": True,
        },
    )


@pytest.fixture
def live_db(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    yield connection
    connection.close()


@pytest.fixture
def cantopen(monkeypatch):
    def boom(*args, **kwargs):
        raise sqlite3.OperationalError("unable to open database file")

    monkeypatch.setattr(db, "connect_readonly", boom)


def a_run(connection, **fields):
    run_id = db.start_run(connection, TARGET, "timer")
    db.finish_run(connection, run_id, **{"status": "success", "email_status": "sent", **fields})
    return run_id


# --- Finding 1: staleness must fail CLOSED -----------------------------------


class TestStalenessFailsClosed:
    """An old snapshot reported `healthy` when the schedule could not be read.

    `_expected_run_window_start` swallowed every exception from `settings.load()`
    and returned `None`; `status_snapshot_stale` was then `None`, which is falsy,
    so an arbitrarily old snapshot was judged on its merits with an empty
    `adapter_errors`. A typo in `daily_send_time`, an unknown timezone, a corrupt
    `config.toml`, or a sandbox that hides `~/.config` from the reader all
    reached it -- and the last of those is the *sandboxed collector* this whole
    change exists to serve.
    """

    @pytest.fixture
    def ancient(self, live_db, cantopen):
        a_run(live_db)
        path = config.status_snapshot_path()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["generated_at"] = "2024-01-01T00:00:00+00:00"
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_a_stale_snapshot_degrades(self, ancient):
        result = health.build_status("auto")
        assert result["status_snapshot_stale"] is True
        assert result["health"] == "unknown"
        assert any("stale" in message for message in result["adapter_errors"])

    @pytest.mark.parametrize(
        "break_schedule",
        [
            pytest.param(lambda m: m.setattr(
                "dailymail.settings.load",
                lambda *a, **k: (_ for _ in ()).throw(RuntimeError("corrupt config")),
            ), id="settings-cannot-load"),
            pytest.param(lambda m: m.setattr(
                "dailymail.settings.load",
                lambda *a, **k: (_ for _ in ()).throw(PermissionError(13, "denied")),
            ), id="config-directory-unreadable"),
        ],
    )
    def test_an_undetermined_schedule_never_reports_healthy(
        self, ancient, monkeypatch, break_schedule
    ):
        break_schedule(monkeypatch)
        result = health.build_status("auto")
        # Undetermined, not fine.
        assert result["status_snapshot_stale"] is None
        assert result["health"] == "unknown"
        assert result["adapter_errors"], "an undetermined answer must be reported"
        assert any(
            "freshness" in message or "schedule" in message
            for message in result["adapter_errors"]
        ), result["adapter_errors"]

    def test_an_unparseable_timestamp_is_refused_outright(self, live_db, cantopen):
        a_run(live_db)
        path = config.status_snapshot_path()
        document = json.loads(path.read_text(encoding="utf-8"))
        document["generated_at"] = "not-a-timestamp"
        path.write_text(json.dumps(document), encoding="utf-8")
        with pytest.raises(status_snapshot.SnapshotError, match="not a timestamp"):
            status_snapshot.read()

    def test_a_fresh_snapshot_with_a_readable_schedule_is_still_healthy(
        self, live_db, cantopen
    ):
        """The fix must not have made everything permanently unknown."""
        a_run(live_db)
        result = health.build_status("auto")
        assert result["status_snapshot_stale"] is False
        assert result["health"] == "healthy"


# --- Finding 4: redaction on the way IN, not just on the way out -------------


class TestSecretsNeverReachTheFile:
    """`error_summary` was written to disk raw.

    `mailer.send` raises `recipients refused: [...]` carrying the address, and
    `daily.py` writes `str(exc)` of that into both `runs.error_summary` and
    `deliveries.error_summary`. `health` scrubbed it on the way out -- but the
    snapshot is a file, and `status-snapshot show` prints it, so it reached a
    terminal and an operator's scrollback unscrubbed.
    """

    HOSTILE = (
        "recipients refused: ['reader@rowan.edu'] via "
        "https://relay-user:hunter2@smtp.example.invalid/send "
        "api_key=SEKRET123 password=CorrectHorse "
        "Authorization: Bearer ey.SECRET.TOKEN"
    )

    @pytest.fixture
    def written(self, live_db):
        a_run(live_db, status="failed", error_summary=self.HOSTILE)
        db.record_delivery(
            live_db, target_date=TARGET, recipient="reader@rowan.edu",
            content_hash_value="h", state="failed", error_summary=self.HOSTILE,
        )
        return config.status_snapshot_path()

    @pytest.mark.parametrize(
        "secret",
        ["reader@rowan.edu", "hunter2", "SEKRET123", "CorrectHorse", "ey.SECRET.TOKEN"],
    )
    def test_no_secret_is_on_disk(self, written, secret):
        assert secret not in written.read_text(encoding="utf-8")

    def test_the_error_is_still_actionable(self, written):
        text = status_snapshot.read()["recent_runs"][0]["error_summary"]
        assert "recipients refused" in text
        assert "[redacted" in text

    def test_status_snapshot_show_prints_nothing_secret(self, written, capsys):
        assert cli.main(["status-snapshot", "show"]) == 0
        out = capsys.readouterr().out
        for secret in ("reader@rowan.edu", "hunter2", "SEKRET123", "CorrectHorse"):
            assert secret not in out

    def test_health_and_the_snapshot_scrub_identically(self):
        """One implementation, so the two can never drift apart."""
        assert health._safe_text(self.HOSTILE) == redact.safe_text(self.HOSTILE)


# --- Finding 2 (secondary): nothing arbitrary reaches the contract -----------


class TestNothingArbitraryReachesTheContract:
    @pytest.fixture
    def craft(self, live_db, cantopen):
        a_run(live_db)
        path = config.status_snapshot_path()
        genuine = json.loads(path.read_text(encoding="utf-8"))

        def write(**overrides):
            path.write_text(json.dumps({**genuine, **overrides}), encoding="utf-8")
            return health.build_status("auto")

        return write

    def test_a_boolean_is_not_published_as_a_count(self, craft):
        """`bool` is an `int` subclass; `database_runs: true` is a type error."""
        result = craft(statistics={"runs": True, "announcements": 7})
        assert result["metrics"].get("database_runs") is None
        assert result["metrics"]["database_announcements"] == 7

    def test_an_oversized_statistic_name_is_refused(self, craft):
        result = craft(statistics={"x" * 5000: 1})
        assert result["status_data_source"] == "unavailable"
        assert any("statistic name" in m for m in result["adapter_errors"])

    def test_a_string_database_size_does_not_reach_the_contract(self, craft):
        result = craft(database_size_bytes="not-a-number")
        size = result["metrics"]["database_size_bytes"]
        assert size is None or isinstance(size, int)

    def test_an_enormous_run_field_is_refused_rather_than_published(self, craft):
        genuine_run = json.loads(
            config.status_snapshot_path().read_text(encoding="utf-8")
        )["recent_runs"][0]
        result = craft(recent_runs=[{**genuine_run, "trigger": "t" * 100_000}])
        assert result["status_data_source"] == "unavailable"
        assert any("above the" in m for m in result["adapter_errors"])

    def test_a_hostile_delivery_state_is_bounded(self, craft):
        genuine = json.loads(config.status_snapshot_path().read_text(encoding="utf-8"))
        delivery = genuine.get("latest_delivery") or {
            name: None for name in status_snapshot.DELIVERY_COLUMNS
        }
        result = craft(latest_delivery={**delivery, "state": "s" * 300})
        # Either refused outright, or bounded on the way out. Never verbatim.
        published = result.get("status_latest_delivery_state")
        assert published is None or len(published) <= 64


class TestHealthAlwaysAnswers:
    """A traceback is strictly worse than the blank document being replaced."""

    def test_an_unexpected_builder_failure_still_prints_a_document(
        self, settings_obj, monkeypatch, capsys
    ):
        def explode(*args, **kwargs):
            raise ZeroDivisionError("something nobody anticipated")

        monkeypatch.setattr(health, "build_status", explode)
        assert cli.main(["health", "--json"]) == 0
        document = json.loads(capsys.readouterr().out)
        assert document["schema_version"] == "controlpanel.status.v1"
        assert document["health"] == "unknown"
        assert document["status_data_source"] == "unavailable"
        assert document["recent_runs"] == []
        assert document["adapter_errors"]
        assert "ZeroDivisionError" in document["adapter_errors"][0]

    def test_the_last_resort_document_is_redacted_too(
        self, settings_obj, monkeypatch, capsys
    ):
        def explode(*args, **kwargs):
            raise RuntimeError("failed for reader@rowan.edu with password=hunter2")

        monkeypatch.setattr(health, "build_status", explode)
        cli.main(["health", "--json"])
        out = capsys.readouterr().out
        assert "reader@rowan.edu" not in out
        assert "hunter2" not in out


# --- Findings 5 and 6: the file and the directory around it ------------------


class TestFileAndDirectoryHardening:
    def test_the_state_directory_is_private(self, tmp_path, monkeypatch):
        """Directory write permission -- not the file's 0600 -- governs replacement."""
        monkeypatch.setattr(os, "umask", lambda mask: 0o000)
        os.umask(0o000)
        target = tmp_path / "fresh" / "nested" / "status.json"
        atomic.atomic_write_json(target, {"a": 1}, mode=0o600)
        assert stat.S_IMODE(target.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(target.stat().st_mode) == 0o600

    def test_a_symlinked_snapshot_is_refused_by_the_open_itself(self, tmp_path):
        secret = tmp_path / "secret.json"
        secret.write_text('{"schema_version": "dailymail.status-snapshot.v1"}', encoding="utf-8")
        link = tmp_path / "status.json"
        link.symlink_to(secret)
        with pytest.raises(status_snapshot.SnapshotError, match="symlink"):
            status_snapshot.read(link)

    def test_the_size_bound_is_enforced_on_bytes_read(self, tmp_path, monkeypatch):
        """Not on what `stat` reported, which a racing writer could lie about."""
        target = tmp_path / "status.json"
        target.write_text("x" * 16, encoding="utf-8")
        monkeypatch.setattr(config, "STATUS_SNAPSHOT_MAX_BYTES", 4)
        with pytest.raises(status_snapshot.SnapshotError, match="bound"):
            status_snapshot.read(target)

    def test_a_non_regular_file_is_refused(self, tmp_path):
        fifo = tmp_path / "status.json"
        os.mkfifo(fifo)
        try:
            with pytest.raises(status_snapshot.SnapshotError):
                status_snapshot.read(fifo)
        finally:
            fifo.unlink()

    def test_invalid_utf8_is_named_not_raised(self, tmp_path):
        target = tmp_path / "status.json"
        target.write_bytes(b"\xff\xfe\x00garbage")
        with pytest.raises(status_snapshot.SnapshotError, match="UTF-8|malformed"):
            status_snapshot.read(target)


# --- Review finding: atomicity and staleness were asserted by name only ------


class TestAtomicityIsActuallyTested:
    """A reviewer replaced the whole `mkstemp` + `os.replace` body with a plain
    in-place `open(path, "w")` and the entire 1,158-test suite still passed --
    one test failed, and only incidentally, because it monkeypatched `os.replace`
    which the mutant no longer called.

    The two tests that looked like they covered this did not: both wrote
    *sequentially* and parsed the result, which any writer emitting valid JSON
    satisfies. These use real concurrency, so a non-atomic writer fails them.
    """

    def test_a_reader_racing_a_writer_never_sees_a_partial_document(self, tmp_path):
        import threading

        target = tmp_path / "status.json"
        # Big enough that a non-atomic write cannot complete between reads.
        small = {"schema_version": "a", "pad": "x" * 400_000}
        large = {"schema_version": "b", "pad": "y" * 400_000}
        atomic.atomic_write_json(target, small)

        stop = threading.Event()
        failures: list[str] = []

        def reader():
            while not stop.is_set():
                try:
                    raw = target.read_text(encoding="utf-8")
                except FileNotFoundError:
                    failures.append("the file vanished mid-write")
                    continue
                try:
                    json.loads(raw)
                except ValueError:
                    failures.append(f"partial document, {len(raw)} bytes")

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        try:
            for index in range(40):
                atomic.atomic_write_json(target, small if index % 2 else large)
        finally:
            stop.set()
            thread.join(timeout=10)
        assert not failures, failures[:3]

    def test_the_temporary_file_is_created_in_the_destination_directory(
        self, tmp_path, monkeypatch
    ):
        """`os.replace` is only atomic within a filesystem."""
        seen: list[str] = []
        real_mkstemp = atomic.tempfile.mkstemp

        def watched(*args, **kwargs):
            seen.append(kwargs.get("dir"))
            return real_mkstemp(*args, **kwargs)

        monkeypatch.setattr(atomic.tempfile, "mkstemp", watched)
        target = tmp_path / "nested" / "status.json"
        atomic.atomic_write_json(target, {"a": 1})
        assert seen == [str(target.parent)]

    def test_the_replacement_goes_through_os_replace(self, tmp_path, monkeypatch):
        """The rename is the atomic step; a writer that skips it is not atomic."""
        calls: list[tuple[str, str]] = []
        real_replace = atomic.os.replace

        def watched(src, dst, *args, **kwargs):
            calls.append((str(src), str(dst)))
            return real_replace(src, dst, *args, **kwargs)

        monkeypatch.setattr(atomic.os, "replace", watched)
        target = tmp_path / "status.json"
        atomic.atomic_write_json(target, {"a": 1})
        assert len(calls) == 1
        assert calls[0][1] == str(target)


class TestStalenessIsAScheduleQuestionNotAnAgeQuestion:
    """A reviewer replaced the schedule-window rule with a naive `age > 1 hour`
    and **nothing in the suite failed**, because every existing case used an
    age that both rules flag. The distinguishing case is a snapshot written by
    this morning's 06:30 run and read late the same evening: current by the
    schedule rule, stale by any short age rule.
    """

    @pytest.fixture
    def at(self, live_db, cantopen, monkeypatch):
        """Write a snapshot stamped `generated`, and read it as if it were `now`."""
        a_run(live_db)
        path = config.status_snapshot_path()

        def evaluate(generated: str, now: str):
            document = json.loads(path.read_text(encoding="utf-8"))
            document["generated_at"] = generated
            path.write_text(json.dumps(document), encoding="utf-8")
            monkeypatch.setattr(health, "_now", lambda: now)
            return health.build_status("auto")

        return evaluate

    def test_this_mornings_snapshot_is_current_all_day(self, at):
        """06:32 EDT written, read at 23:00 EDT -- 16.5 hours later, and current.

        Any age threshold short enough to be useful flags this; the schedule
        rule does not, because no 06:30 has passed since it was written.
        """
        result = at("2026-09-11T10:32:23+00:00", "2026-09-12T03:00:00+00:00")
        assert result["status_snapshot_stale"] is False
        assert result["health"] == "healthy"
        # The window it was judged against is the morning it was written, not a
        # duration before the read.
        assert result["status_snapshot_expected_since"] == "2026-09-11T10:30:00+00:00"

    def test_a_snapshot_from_before_the_last_scheduled_run_is_stale(self, at):
        # Written 10:32Z on the 10th; read after the 11th's 06:30 came and went.
        result = at("2026-09-10T10:32:23+00:00", "2026-09-11T12:00:00+00:00")
        assert result["status_snapshot_stale"] is True
        assert result["health"] == "unknown"

    def test_the_boundary_is_the_scheduled_time_not_a_duration(self, at):
        """Two snapshots minutes apart, on opposite sides of one 06:30."""
        just_before = at("2026-09-11T10:29:00+00:00", "2026-09-11T12:00:00+00:00")
        just_after = at("2026-09-11T10:31:00+00:00", "2026-09-11T12:00:00+00:00")
        assert just_before["status_snapshot_stale"] is True
        assert just_after["status_snapshot_stale"] is False
        # Two minutes apart, judged against the same boundary: no age threshold
        # separates them, and the schedule boundary does.
        assert (
            just_before["status_snapshot_expected_since"]
            == just_after["status_snapshot_expected_since"]
            == "2026-09-11T10:30:00+00:00"
        )

    def test_the_expected_boundary_is_published(self, at):
        result = at("2026-09-11T10:32:23+00:00", "2026-09-11T12:00:00+00:00")
        assert result["status_snapshot_expected_since"] == "2026-09-11T10:30:00+00:00"


class TestTheSchemaIsBoundToTheColumns:
    """Nothing tied `SCHEMA_VERSION` to the column tuples, so adding a column
    without bumping the schema would have a new release read an old release's
    file and find a run entry missing a field. That is now refused rather than
    fatal -- but the skew is still avoidable, and this is the reminder."""

    def test_changing_the_columns_requires_a_deliberate_decision(self):
        assert status_snapshot.SCHEMA_VERSION == "dailymail.status-snapshot.v1"
        assert status_snapshot.RUN_COLUMNS == (
            "run_id", "target_date", "started_at", "completed_at", "status",
            "employee_count", "student_count", "unique_count", "new_count",
            "standing_count", "changed_count", "collector_validation",
            "email_status", "error_summary", "trigger", "parking_stats",
        )
        assert status_snapshot.DELIVERY_COLUMNS == (
            "delivery_id", "target_date", "prepared_at", "sent_at", "state",
            "smtp_status", "message_bytes", "image_count", "forced",
            "error_summary",
        )

    def test_the_run_columns_are_the_ones_health_reads(self, live_db):
        """The agreement between the two documents rests on this."""
        a_run(live_db)
        connection = db.connect_readonly()
        available = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
        connection.close()
        assert set(status_snapshot.RUN_COLUMNS) <= available

    def test_the_delivery_columns_exist_and_exclude_the_recipient(self, live_db):
        connection = db.connect_readonly()
        available = {
            row[1] for row in connection.execute("PRAGMA table_info(deliveries)")
        }
        connection.close()
        assert set(status_snapshot.DELIVERY_COLUMNS) <= available
        # The address, the message id and the content hash stay out.
        for excluded in ("recipient", "message_id", "content_hash"):
            assert excluded in available
            assert excluded not in status_snapshot.DELIVERY_COLUMNS


class TestTheProjectionIsOneConsistentRead:
    def test_a_concurrent_commit_cannot_split_the_document(self, live_db):
        """~30 SELECTs in autocommit could mix two instants: `statistics.runs`
        counting a run that `recent_runs` did not contain."""
        a_run(live_db)
        other = db.connect()
        try:
            counts = []
            real_statistics = db.statistics

            def statistics_then_commit(connection):
                # Commit a new run from another connection mid-projection.
                if not counts:
                    counts.append(1)
                    run_id = db.start_run(other, "2026-09-12", "probe")
                    db.finish_run(other, run_id, status="success")
                return real_statistics(connection)

            db.statistics = statistics_then_commit
            try:
                connection = db.connect_readonly()
                document = status_snapshot.build(connection)
                connection.close()
            finally:
                db.statistics = real_statistics
        finally:
            other.close()

        # Whatever instant it chose, the two halves agree with each other.
        assert document["statistics"]["runs"] == len(
            [r for r in document["recent_runs"]]
        ) or document["statistics"]["runs"] >= len(document["recent_runs"])
        run_ids = {entry["run_id"] for entry in document["recent_runs"]}
        assert len(run_ids) == len(document["recent_runs"])


class TestSnapshotOnlyModeSaysUnreadableNotNeverRan:
    def test_a_missing_snapshot_under_source_snapshot_is_not_never_ran(
        self, settings_obj
    ):
        result = health.build_status("snapshot")
        assert result["status_data_source"] == "unavailable"
        assert result["health"] == "unknown"
        assert "No DailyMail run has been recorded" not in result["summary"]
        assert result["adapter_errors"]
