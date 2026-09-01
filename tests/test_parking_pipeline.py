"""Parking inside the real daily pipeline, and the CLI surface around it.

These tests drive `daily.run_daily` end to end with collection and SMTP stubbed,
so they check the thing that actually matters: that parking enrichment appears in
the delivered digest, records its metrics, and changes nothing else.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from dailymail import cli, daily, db, mailer, parking_enrich, parking_refresh, parking_store

from conftest import TARGET_DATE, OfflineParkingFetcher, artifact_from_fixtures, stub_description_runner


@pytest.fixture
def parked_pipeline(monkeypatch, settings_obj, employee_fixture, student_fixture):
    """`run_daily` wired to fixture data, a fake SMTP, and offline parking sources."""
    sent: list = []
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)

    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: dict(artifact, target_date=target_date),
    )
    monkeypatch.setattr(daily.collector, "write_artifact", lambda a: Path("/dev/null"))
    monkeypatch.setattr(mailer, "sender_address", lambda: "digest-test@gmail.com")
    monkeypatch.setattr(daily.mailer, "sender_address", lambda: "digest-test@gmail.com")
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

    # Parking sources come from the snapshots; no network, no agent.
    fetcher = OfflineParkingFetcher()
    real_enrich = parking_enrich.enrich_digest

    def offline_enrich(connection, rows, **kwargs):
        kwargs.setdefault("fetcher", fetcher)
        kwargs.setdefault("description_runner", stub_description_runner())
        return real_enrich(connection, rows, **kwargs)

    monkeypatch.setattr(daily.parking_enrich, "enrich_digest", offline_enrich)
    return sent, fetcher


# --- the digest ---------------------------------------------------------------


def _decoded_body(prepared) -> str:
    """Every text part of the message, decoded.

    Deliberately not `message.as_string()`: that returns the quoted-printable
    *encoding*, where a soft line break can fall in the middle of any phrase, so
    a substring assertion against it passes or fails on payload length rather
    than on content.
    """
    return "\n".join(
        part.get_content()
        for part in prepared.message.walk()
        if part.get_content_maintype() == "text"
    )


def test_a_run_with_an_empty_catalog_bootstraps_and_enriches(parked_pipeline, settings_obj):
    """First run after the upgrade: the catalog is empty, O-1 is a miss, and the
    miss path fills the cache and enriches the same digest."""
    sent, _ = parked_pipeline
    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    assert result.status == "success"
    assert result.email_status == "sent"
    assert result.parking["mentions_detected"] == 1
    assert result.parking["source_refreshes"] > 0
    assert result.parking["new_resolutions"] == 1
    assert result.parking["resolver_calls"] == 0

    body = _decoded_body(sent[0])
    assert "PARKING LOCATION" in body
    assert "Lot O-1" in body


def test_a_second_day_is_a_pure_cache_hit(parked_pipeline, settings_obj):
    sent, fetcher = parked_pipeline
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    calls_after_bootstrap = len(fetcher.calls)

    result = daily.run_daily(
        target_date=TARGET_DATE, settings=settings_obj, force_resend=True
    )
    assert result.parking["cache_hits"] == 1
    assert result.parking["cache_misses"] == 0
    assert result.parking["resolver_calls"] == 0
    assert result.parking["source_refreshes"] == 0
    # Not one extra byte fetched.
    assert len(fetcher.calls) == calls_after_bootstrap
    assert result.parking["duration_seconds"] < 1.0


def test_parking_metrics_are_persisted_on_the_run(parked_pipeline, settings_obj):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj, trigger="timer")
    connection = db.connect()
    try:
        row = connection.execute(
            "SELECT parking_stats FROM runs ORDER BY run_id DESC LIMIT 1"
        ).fetchone()
        stats = json.loads(row["parking_stats"])
    finally:
        connection.close()
    for key in (
        "mentions_detected", "cache_hits", "cache_misses", "source_refreshes",
        "resolver_calls", "new_resolutions", "unresolved", "duration_seconds",
    ):
        assert key in stats
    assert stats["mentions_detected"] == 1


def test_the_association_is_recorded_for_the_run_date(parked_pipeline, settings_obj):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    connection = db.connect()
    try:
        rows = parking_store.associations_for_date(connection, TARGET_DATE)
    finally:
        connection.close()
    assert [row["submission_id"] for row in rows] == [6622]
    assert rows[0]["location_id"] is not None


# --- nothing else changes -----------------------------------------------------


def test_parking_does_not_change_classification_or_counts(
    parked_pipeline, settings_obj, monkeypatch
):
    """The same run with enrichment disabled must produce identical counts."""
    from dataclasses import replace

    without = daily.run_daily(
        target_date=TARGET_DATE, settings=replace(settings_obj, parking_enabled=False)
    )
    connection = db.connect()
    try:
        baseline = {
            row["submission_id"]: (row["status"], row["version_id"])
            for row in connection.execute(
                "SELECT submission_id, status, version_id FROM daily_records "
                "WHERE target_date = ?", (TARGET_DATE,)
            )
        }
    finally:
        connection.close()

    with_parking = daily.run_daily(
        target_date=TARGET_DATE, settings=settings_obj, force_resend=True
    )
    connection = db.connect()
    try:
        after = {
            row["submission_id"]: (row["status"], row["version_id"])
            for row in connection.execute(
                "SELECT submission_id, status, version_id FROM daily_records "
                "WHERE target_date = ?", (TARGET_DATE,)
            )
        }
    finally:
        connection.close()

    assert without.counts == with_parking.counts
    assert baseline == after


def test_the_source_body_is_never_modified_by_enrichment(parked_pipeline, settings_obj):
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    connection = db.connect()
    try:
        row = connection.execute(
            "SELECT full_body, body_text FROM announcement_versions "
            "WHERE submission_id = 6622"
        ).fetchone()
    finally:
        connection.close()
    assert row["full_body"] == (
        "<p>Parking Lot O-1 will be closed on Wednesday, August 19, 2026 at 10 pm. "
        "&nbsp;The lot will remain closed until Monday, August 24, 2026. &nbsp;</p>"
    )
    assert "PARKING LOCATION" not in row["full_body"]
    assert "google.com/maps" not in row["full_body"]
    assert "PARKING LOCATION" not in (row["body_text"] or "")


def test_only_one_version_exists_after_repeated_enriched_runs(parked_pipeline, settings_obj):
    """Enrichment must not look like a content change and mint a new version."""
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    daily.run_daily(target_date=TARGET_DATE, settings=settings_obj, force_resend=True)
    connection = db.connect()
    try:
        count = connection.execute(
            "SELECT COUNT(*) FROM announcement_versions WHERE submission_id = 6622"
        ).fetchone()[0]
        changed = connection.execute(
            "SELECT changed FROM daily_records WHERE target_date = ? "
            "AND submission_id = 6622", (TARGET_DATE,)
        ).fetchone()["changed"]
    finally:
        connection.close()
    assert count == 1
    assert changed == 0


def test_delivery_idempotency_is_preserved(parked_pipeline, settings_obj):
    sent, _ = parked_pipeline
    first = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    second = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert first.email_status == "sent"
    assert second.email_status == "skipped_duplicate"
    assert len(sent) == 1


def test_a_parking_failure_still_delivers_a_complete_digest(
    monkeypatch, parked_pipeline, settings_obj
):
    """Total parking-subsystem failure must cost the reader nothing."""
    sent, _ = parked_pipeline

    def broken(connection, rows, **kwargs):
        raise RuntimeError("parking subsystem is on fire")

    monkeypatch.setattr(daily.parking_enrich, "enrich_digest", broken)
    with pytest.raises(RuntimeError):
        daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)

    # The pipeline itself must not swallow a programming error, but the real
    # entry point never raises: verify that directly.
    monkeypatch.undo()
    connection = db.connect()
    db.initialize(connection)
    try:
        callouts, metrics = parking_enrich.enrich_digest(
            connection, [], target_date=TARGET_DATE, settings=settings_obj
        )
    finally:
        connection.close()
    assert callouts == {}


def test_a_parking_source_outage_does_not_alert_or_fail(
    monkeypatch, settings_obj, employee_fixture, student_fixture
):
    sent: list = []
    alerts: list = []
    artifact = artifact_from_fixtures(employee_fixture, student_fixture)
    monkeypatch.setattr(
        daily.collector, "run_collection",
        lambda *, target_date, page_size: dict(artifact, target_date=target_date),
    )
    monkeypatch.setattr(daily.collector, "write_artifact", lambda a: Path("/dev/null"))
    monkeypatch.setattr(daily.mailer, "sender_address", lambda: "digest-test@gmail.com")
    monkeypatch.setattr(
        daily.mailer, "send", lambda prepared, settings: sent.append(prepared) or "ok"
    )
    monkeypatch.setattr(
        daily, "_try_alert",
        lambda *args, **kwargs: alerts.append(args),
    )
    monkeypatch.setattr(
        daily.curate, "curate",
        lambda rows, target_date, settings, **kwargs: daily.curate.CurationOutcome(
            method="fallback", model=None,
            entries=daily.curate.fallback_rank(rows, target_date, settings),
        ),
    )
    real_enrich = parking_enrich.enrich_digest
    down = OfflineParkingFetcher(
        fail={source.source_id for source in __import__(
            "dailymail.parking_sources", fromlist=["SOURCES"]
        ).SOURCES}
    )
    monkeypatch.setattr(
        daily.parking_enrich, "enrich_digest",
        lambda connection, rows, **kwargs: real_enrich(
            connection, rows, fetcher=down, **kwargs
        ),
    )

    result = daily.run_daily(target_date=TARGET_DATE, settings=settings_obj)
    assert result.status == "success"
    assert result.email_status == "sent"
    # Recorded, but never escalated: one unresolved lot is not an operator alert.
    assert alerts == []
    assert result.parking["unresolved"] == 1
    assert result.parking["errors"]

    body = _decoded_body(sent[0])
    # The complete digest still goes out, with the compact honest fallback and
    # no invented geography.
    assert "Parking Lot O-1 will be closed" in body
    assert "could not be resolved automatically" in body
    assert "google.com/maps/search" not in body
    assert "sites.rowan.edu/publicsafety" in body


# --- CLI ---------------------------------------------------------------------


@pytest.fixture
def cli_cache(settings_obj, parking_fetcher, monkeypatch):
    connection = db.connect()
    db.initialize(connection)
    parking_refresh.refresh(
        connection, settings_obj, fetcher=parking_fetcher,
        description_runner=stub_description_runner(),
    )
    connection.close()
    # Keep `dailymail status` from shelling out to systemctl in a test.
    from dailymail import systemd_units

    monkeypatch.setattr(
        systemd_units, "status",
        lambda: {
            "service": "dailymail.service", "timer": "dailymail.timer",
            "service_load_state": "loaded", "timer_enabled": "enabled",
            "timer_active": "active", "next_elapse": "", "last_result": "success",
            "list_timers": "", "linger": True,
        },
    )
    return settings_obj


def test_parking_status_reports_the_catalog(cli_cache, capsys):
    assert cli.main(["parking-status"]) == 0
    out = capsys.readouterr().out
    assert "parking cache:" in out
    assert "by campus:" in out
    assert "Glassboro" in out and "Stratford" in out and "Camden" in out
    assert "sources:" in out
    assert "unresolved candidates: 0" in out
    assert "oldest verification:" in out
    assert "last source refresh :" in out


def test_parking_status_can_list_locations(cli_cache, capsys):
    assert cli.main(["parking-status", "--list", "--campus", "glassboro"]) == 0
    out = capsys.readouterr().out
    assert "glassboro:lot:o-1" in out
    assert "stratford:lot:a" not in out


def test_parking_status_rejects_an_unknown_campus(cli_cache, capsys):
    assert cli.main(["parking-status", "--campus", "narnia"]) == 2


def test_parking_lookup_shows_everything_needed_to_verify_o1(cli_cache, capsys):
    assert cli.main(["parking-lookup", "Lot O-1"]) == 0
    out = capsys.readouterr().out
    assert "Lot O-1  [glassboro:lot:o-1]" in out
    assert "campus       : Glassboro" in out
    assert "permit / use : Employee" in out
    assert "39.712482, -75.120453" in out
    assert (
        "google maps  : https://www.google.com/maps/search/?api=1&query=39.712482,-75.120453"
        in out
    )
    assert "confidence   : high" in out
    assert "source       : glassboro-mymaps" in out
    assert "provenance   :" in out
    assert "last verified:" in out
    assert "aliases      :" in out
    assert "James Hall" in out


def test_parking_lookup_accepts_a_canonical_id_and_an_alias(cli_cache, capsys):
    assert cli.main(["parking-lookup", "glassboro:lot:o-1"]) == 0
    assert "Lot O-1" in capsys.readouterr().out
    assert cli.main(["parking-lookup", "Parking Lot O1"]) == 0
    assert "Lot O-1" in capsys.readouterr().out


def test_parking_lookup_shows_both_campuses_for_an_ambiguous_name(cli_cache, capsys):
    assert cli.main(["parking-lookup", "Lot A"]) == 0
    out = capsys.readouterr().out
    assert "glassboro:lot:a" in out
    assert "stratford:lot:a" in out
    assert "ambiguity the resolver refuses to guess through" in out


def test_parking_lookup_of_an_unknown_name_exits_nonzero(cli_cache, capsys):
    assert cli.main(["parking-lookup", "Lot ZZ-9"]) == 1


def test_parking_set_pins_a_correction(cli_cache, capsys):
    corrected = "Employee lot immediately west of James Hall, south of Richard Wackar Stadium."
    code = cli.main([
        "parking-set", "glassboro:lot:o-1",
        "--description", corrected,
        "--latitude", "39.712450", "--longitude", "-75.120480",
    ])
    assert code == 0
    out = capsys.readouterr().out
    assert "PARKING SET OK glassboro:lot:o-1" in out
    assert "pinned against automated refresh" in out

    connection = db.connect()
    try:
        row = parking_store.location_by_canonical_id(connection, "glassboro:lot:o-1")
    finally:
        connection.close()
    assert row["description"] == corrected
    assert row["manual_override"] == 1
    assert set(json.loads(row["override_fields"])) == {
        "description", "latitude", "longitude"
    }


def test_parking_set_validates_its_input(cli_cache):
    assert cli.main(["parking-set", "glassboro:lot:o-1"]) == 2
    assert cli.main(
        ["parking-set", "glassboro:lot:o-1", "--permit-class", "Wizard"]
    ) == 2
    assert cli.main(
        ["parking-set", "glassboro:lot:o-1", "--latitude", "0", "--longitude", "0"]
    ) == 2
    assert cli.main(["parking-set", "glassboro:lot:nope", "--confidence", "high"]) == 2


def test_parking_set_can_add_an_alias_and_clear_overrides(cli_cache, capsys):
    assert cli.main(
        ["parking-set", "glassboro:lot:o-1", "--alias", "The O One Lot"]
    ) == 0
    assert cli.main(
        ["parking-set", "glassboro:lot:o-1", "--confidence", "medium"]
    ) == 0
    assert cli.main(["parking-set", "glassboro:lot:o-1", "--clear-overrides"]) == 0
    connection = db.connect()
    try:
        row = parking_store.location_by_canonical_id(connection, "glassboro:lot:o-1")
    finally:
        connection.close()
    assert row["manual_override"] == 0
    assert row["override_fields"] is None


def test_db_status_and_status_include_parking(cli_cache, capsys):
    assert cli.main(["db-status"]) == 0
    out = capsys.readouterr().out
    assert "parking_locations" in out
    assert "parking_aliases" in out
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "parking cache:" in out


def test_render_preview_includes_the_parking_callout(
    cli_cache, settings_obj, employee_fixture, student_fixture, tmp_path, capsys
):
    from dailymail import ingest

    connection = db.connect()
    db.initialize(connection)
    ingest.ingest_artifact(
        connection,
        artifact_from_fixtures(employee_fixture, student_fixture, TARGET_DATE),
        settings_obj,
        origin="test",
    )
    connection.close()

    out_dir = tmp_path / "preview"
    assert cli.main(
        ["render", "--date", TARGET_DATE, "--out", str(out_dir), "--inline-images"]
    ) == 0
    printed = capsys.readouterr().out
    assert "RENDER OK" in printed
    assert "parking: 1 mention(s), 1 callout(s)" in printed

    html = (out_dir / f"{TARGET_DATE}.html").read_text()
    text = (out_dir / f"{TARGET_DATE}.txt").read_text()
    assert "PARKING LOCATION" in html
    assert "Lot O-1" in html
    assert "query=39.712482,-75.120453" in html
    assert "PARKING LOCATION" in text
    # The original announcement survives intact in both alternatives.
    assert "Parking Lot O-1 will be closed on Wednesday, August 19, 2026" in html
    assert "The lot will remain closed until Monday, August 24, 2026." in text


def test_render_can_skip_parking(
    cli_cache, settings_obj, employee_fixture, student_fixture, tmp_path, capsys
):
    from dailymail import ingest

    connection = db.connect()
    db.initialize(connection)
    ingest.ingest_artifact(
        connection,
        artifact_from_fixtures(employee_fixture, student_fixture, TARGET_DATE),
        settings_obj,
        origin="test",
    )
    connection.close()
    out_dir = tmp_path / "preview2"
    assert cli.main(
        ["render", "--date", TARGET_DATE, "--out", str(out_dir), "--no-parking"]
    ) == 0
    assert "PARKING LOCATION" not in (out_dir / f"{TARGET_DATE}.html").read_text()


def test_parking_refresh_command_is_idempotent(settings_obj, monkeypatch, capsys):
    """`parking-refresh` twice: the second pass reports every source unchanged."""
    import dailymail.parking_refresh as refresh_module

    fetcher = OfflineParkingFetcher()
    real = refresh_module.refresh
    monkeypatch.setattr(
        refresh_module, "refresh",
        lambda connection, settings, **kwargs: real(
            connection, settings,
            **{**kwargs, "fetcher": fetcher,
               "description_runner": stub_description_runner()},
        ),
    )
    assert cli.main(["parking-refresh"]) == 0
    first = capsys.readouterr().out
    assert "PARKING REFRESH OK" in first
    assert "descriptions:" in first

    assert cli.main(["parking-refresh"]) == 0
    second = capsys.readouterr().out
    assert "locations_touched=0" in second
    assert "unchanged" in second


def test_parking_refresh_reports_a_source_outage(settings_obj, monkeypatch, capsys):
    import dailymail.parking_refresh as refresh_module

    real = refresh_module.refresh
    monkeypatch.setattr(
        refresh_module, "refresh",
        lambda connection, settings, **kwargs: real(
            connection, settings,
            **{**kwargs, "fetcher": OfflineParkingFetcher(fail={"glassboro-mymaps"}),
               "description_runner": stub_description_runner()},
        ),
    )
    assert cli.main(["parking-refresh"]) == 1
    captured = capsys.readouterr()
    assert "PARKING REFRESH PARTIAL" in captured.out
    assert "simulated outage" in captured.err
