"""Extra Editions: the confirmed coverage gap, and what DailyMail does anyway.

On 31 August 2026 Rowan sent employees an Extra Edition:

    *** EXTRA EDITION - Mon Aug 31, 2026 ***
    A New Chapter for University Advancement

from President Ali A. Houshmand, announcing John Zabinski's 1 October retirement
and Brittany Petrella as interim vice president for University Advancement.
DailyMail's 31 August digest did not contain it.

That is not a collection bug. Re-running the Phase 0 reconnaissance read-only
against `ActionGetHomeData` on 11 September 2026 established, in this order:

  * single-day mode for 2026-08-31 returns 24 employee and 14 student records
    and none of them is it -- and the run's own V1 count gate passed, because
    the source's `TotalCount` agrees;
  * range mode for the same day returns the same set;
  * the whole archive, `2020-01-01` to `2030-12-31`, is 5,372 employee and 4,266
    student records, and `ExtraEdition` is false for every one of them while
    `ExtraEditionDateSent` is set on none;
  * the announcement does not exist anywhere in that archive under any wording;
  * the one read path that knows about Extra Editions,
    `MainFlow/EmailAdmin/DataActionGetDailyMailAnnouncements`, answers an
    anonymous caller with `NotRegisteredException: SuperAdmin2 role required`.

So there is no deterministic public read source for an Extra Edition, and the
only other endpoints that mention one -- `ActionTest_DistributeExtraEditionByDate`
and `ActionSendDailyMail` -- are Rowan's own senders, which DailyMail must never
call. `docs/extra-editions.md` records the gap and what closing it would take.

What *is* built, and is asserted here, is everything that does not require a new
ingestion channel: the source's own `ExtraEdition` flag is collected, stored,
counted in the status document, given substantial curation priority, and shown as
a compact badge. All of it keys on the flag and nothing else, so it is dormant
today and correct on the morning Rowan sets it. Inferring an Extra Edition from
the words "EXTRA EDITION" in a subject would let any submitter mint the badge,
so nothing here does that.
"""

from __future__ import annotations

import json

import pytest

from dailymail import curate, db, normalize, render

TARGET = "2026-08-31"

# The exact announcement, so the regression is about this and not about a
# category of thing. If it ever becomes collectible these values are what a
# future test asserts against.
EXTRA_EDITION_TITLE = "A New Chapter for University Advancement"
EXTRA_EDITION_SUBJECT = "*** EXTRA EDITION - Mon Aug 31, 2026 ***"


def _submission(**overrides) -> dict:
    """One `ActionGetHomeData` record in the shape the collector receives."""
    submission = {
        "Id": 9401,
        "Title": EXTRA_EDITION_TITLE,
        "Body": "<p>Message from the President.</p>",
        "ShortBody": "Message from the President.",
        "Audience": "Employees",
        "SubmittedStatus": "Approved",
        "ContactName": "Office of the President",
        "ContactDepartment": "Office of the President",
        "ContactRowanEmail": "president@rowan.edu",
        "ContactPhone": "856-256-4000",
        "Event": False,
        "EventName": "",
        "EventDate": "1900-01-01",
        "EventStartTime": "00:00:00",
        "EventEndTime": "00:00:00",
        "EventLocation": "",
        "EventNoEnd": False,
        "ExtraEdition": True,
        "ExtraEditionDateSent": "2026-08-31T13:00:00Z",
        "SubmittedDate": "2026-08-31T12:00:00Z",
        "ApprovedDate": "2026-08-31T12:30:00Z",
    }
    submission.update(overrides)
    return submission


# --- the source flag is collected, and only the source flag ------------------


def test_the_source_extra_edition_flag_is_carried_through_normalization():
    assert normalize.SUBMISSION_ALLOWLIST["ExtraEdition"] == "extra_edition"


def test_the_extra_edition_send_timestamp_is_never_persisted():
    """It is workflow metadata, and Phase 1 forbids it."""
    assert "ExtraEditionDateSent" in normalize.FORBIDDEN_KEYS


def test_an_extra_edition_is_stored_with_its_flag(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    try:
        version_id = _store(connection, extra_edition=True)
        stored = connection.execute(
            "SELECT extra_edition FROM announcement_versions WHERE version_id = ?",
            (version_id,),
        ).fetchone()
        assert stored["extra_edition"] == 1
    finally:
        connection.close()


def _store(connection, *, extra_edition, submission_id=9401, status="New",
           title=EXTRA_EDITION_TITLE):
    stamp = db.now_utc()
    with db.transaction(connection):
        db.upsert_category(
            connection, category_id=1, title="Official", rowan_rank=1,
            color=None, is_active=True, manual_priority=1,
        )
        record = {
            "submission_id": submission_id,
            "title": title,
            "full_body": "<p>Message from the President.</p>",
            "body_text": "Message from the President.",
            "source_audience": "Employees",
            "category_id": 1,
            "distribution_dates": [TARGET],
            "first_distribution_date": TARGET,
            "status": status,
            "is_event": 0,
            "extra_edition": 1 if extra_edition else 0,
        }
        version_id, _ = db.record_announcement(
            connection, record, observed_at=stamp
        )
        db.record_daily(
            connection, target_date=TARGET, submission_id=submission_id,
            version_id=version_id, status=status, changed=False, observed_at=stamp,
        )
    return version_id


# --- the confirmed gap, recorded so it cannot be quietly forgotten ------------


def test_no_extra_edition_has_ever_been_observed_in_the_committed_fixtures():
    """The Phase 0 finding, as a check rather than as a sentence in a document.

    If a fixture ever arrives with the flag set, this test failing is the
    notification that the open question has been answered.
    """
    from conftest import RENDER_REGRESSION

    assert not any(
        entry.get("extra_edition") for entry in RENDER_REGRESSION.values()
    )


def test_the_status_document_counts_extra_editions(settings_obj):
    """The operator can see the answer without writing a query."""
    connection = db.connect()
    db.initialize(connection)
    try:
        assert db.statistics(connection)["extra_editions"] == 0
        _store(connection, extra_edition=True)
        assert db.statistics(connection)["extra_editions"] == 1
    finally:
        connection.close()


def test_the_aug_31_extra_edition_is_not_in_any_collected_artifact():
    """The regression for the reported miss.

    Every committed collection fixture is searched for the exact announcement.
    This is the assertion that would start failing the day a collection change
    -- any collection change -- began picking it up, which is precisely when
    somebody needs to be told.
    """
    from conftest import REPO_ROOT

    haystacks = []
    for path in sorted((REPO_ROOT / "artifacts").rglob("*.json")):
        haystacks.append(path.read_text(encoding="utf-8", errors="replace"))
    blob = "\n".join(haystacks)
    assert blob, "the reconnaissance fixtures must be present"
    for phrase in (EXTRA_EDITION_TITLE, "Zabinski", "Petrella"):
        assert phrase not in blob, (
            f"{phrase!r} is now present in the committed artifacts. If Rowan has "
            "started publishing Extra Editions through ActionGetHomeData, "
            "docs/extra-editions.md needs updating and this test needs to become "
            "a positive assertion."
        )


def test_the_write_endpoints_behind_extra_editions_are_named_and_forbidden():
    """`DistributeExtraEditionByDate` and `SendDailyMail` are Rowan's senders.

    DailyMail can build exactly one request shape, so these are unreachable by
    construction -- this asserts the construction.
    """
    from dailymail import config

    import pathlib
    import re

    assert config.GET_HOME_DATA_PATH.endswith("Home/ActionGetHomeData")

    # Every screen-service path the package can put on the wire is a quoted
    # string literal. Prose about an endpoint in a docstring is documentation;
    # a literal is a reachable request, so only literals are checked.
    package = pathlib.Path(config.__file__).parent
    literals = set()
    for path in sorted(package.rglob("*.py")):
        literals.update(
            re.findall(r'["\'](screenservices/[^"\']*)["\']', path.read_text(encoding="utf-8"))
        )
    assert literals == {config.GET_HOME_DATA_PATH}, literals
    for endpoint in (
        "DistributeExtraEdition",
        "ActionSendDailyMail",
        "ActionSaveSubmission",
        "ActionUpdateSubmissionStatus",
        "ActionSaveVisitorClicks",
    ):
        assert not any(endpoint in literal for literal in literals), endpoint


# --- presentation: driven by the flag, never by wording ----------------------


def _render(connection, settings_obj, ids):
    rows = db.digest_rows(connection, TARGET)
    return render.render_digest(
        rows,
        target_date=TARGET,
        counts=db.counts_for_date(connection, TARGET),
        ordering={str(i): {"model_rank": n + 1} for n, i in enumerate(ids)},
        curation_method="fallback",
        settings=settings_obj,
    )


def test_an_extra_edition_gets_its_badge(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    try:
        _store(connection, extra_edition=True)
        digest = _render(connection, settings_obj, [9401])
        assert ">EXTRA EDITION</td>" in digest.html
        assert "EXTRA EDITION" in digest.text
    finally:
        connection.close()


def test_an_ordinary_announcement_never_gets_the_badge(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    try:
        _store(connection, extra_edition=False)
        digest = _render(connection, settings_obj, [9401])
        assert "EXTRA EDITION" not in digest.html
        assert "EXTRA EDITION" not in digest.text
    finally:
        connection.close()


def test_the_badge_is_never_inferred_from_the_subject_line(settings_obj):
    """An announcement that merely *says* it is an Extra Edition is not one.

    Titles are author-supplied, so a badge inferred from wording would be a
    badge any submitter could mint.
    """
    connection = db.connect()
    db.initialize(connection)
    try:
        _store(
            connection, extra_edition=False,
            title=f"{EXTRA_EDITION_SUBJECT} {EXTRA_EDITION_TITLE}",
        )
        digest = _render(connection, settings_obj, [9401])
        assert ">EXTRA EDITION</td>" not in digest.html
        # The title itself still renders verbatim -- nothing is censored.
        assert EXTRA_EDITION_TITLE in digest.html
    finally:
        connection.close()


def test_an_extra_edition_keeps_its_own_new_or_standing_status(settings_obj):
    """It is not a third universe: it still belongs to a section."""
    connection = db.connect()
    db.initialize(connection)
    try:
        _store(connection, extra_edition=True, status="Standing")
        digest = _render(connection, settings_obj, [9401])
        assert ">STANDING</td>" in digest.html
        assert ">EXTRA EDITION</td>" in digest.html
        assert "card-standing" in digest.html
    finally:
        connection.close()


# --- curation priority -------------------------------------------------------


def test_the_curation_payload_carries_the_flag(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    try:
        _store(connection, extra_edition=True)
        rows = db.digest_rows(connection, TARGET)
        payload = curate.build_payload(rows, TARGET, settings_obj)
        assert payload["new_items"][0]["extra_edition"] is True
        curate.assert_payload_is_clean(payload)
    finally:
        connection.close()


def test_the_prompt_tells_the_model_to_rank_an_extra_edition_highly():
    assert '"extra_edition": true' in curate.SYSTEM_PROMPT
    assert "could not wait" in curate.SYSTEM_PROMPT


def test_the_deterministic_fallback_puts_an_extra_edition_first_in_its_group(
    settings_obj,
):
    connection = db.connect()
    db.initialize(connection)
    try:
        _store(connection, extra_edition=False, submission_id=9402,
               title="An ordinary announcement")
        _store(connection, extra_edition=True, submission_id=9401)
        rows = db.digest_rows(connection, TARGET)
        entries = curate.fallback_rank(rows, TARGET, settings_obj)
        new = [e for e in entries if e["section"] == "New"]
        first = min(new, key=lambda e: e["model_rank"])
        assert first["submission_id"] == "9401"
        assert "EXTRA EDITION" in first["rationale"]
    finally:
        connection.close()


def test_the_deterministic_fallback_lifts_a_standing_extra_edition_too(
    settings_obj,
):
    connection = db.connect()
    db.initialize(connection)
    try:
        _store(connection, extra_edition=False, submission_id=9402,
               title="An ordinary standing announcement", status="Standing")
        _store(connection, extra_edition=True, submission_id=9401,
               status="Standing")
        rows = db.digest_rows(connection, TARGET)
        entries = curate.fallback_rank(rows, TARGET, settings_obj)
        standing = sorted(
            (e for e in entries if e["section"] == "Standing"),
            key=lambda e: e["model_rank"],
        )
        assert standing[0]["submission_id"] == "9401"
    finally:
        connection.close()


def test_ordinary_fallback_ordering_is_unchanged_without_an_extra_edition(
    settings_obj,
):
    """The boost must be inert on every ordinary day, which is all of them."""
    connection = db.connect()
    db.initialize(connection)
    try:
        _store(connection, extra_edition=False, submission_id=9402, title="A")
        _store(connection, extra_edition=False, submission_id=9403, title="B")
        rows = db.digest_rows(connection, TARGET)
        entries = curate.fallback_rank(rows, TARGET, settings_obj)
        new = sorted(
            (e for e in entries if e["section"] == "New"),
            key=lambda e: e["model_rank"],
        )
        # Highest submission id first, as before.
        assert [e["submission_id"] for e in new] == ["9403", "9402"]
        assert all("EXTRA EDITION" not in e["rationale"] for e in new)
    finally:
        connection.close()


def test_an_extra_edition_still_participates_in_logical_family_behaviour(
    settings_obj,
):
    """Exceptional in priority, ordinary in identity."""
    from dailymail import repeats

    connection = db.connect()
    db.initialize(connection)
    try:
        _store(connection, extra_edition=True)
        rows = db.digest_rows(connection, TARGET)
        row = rows[0]
        # The repeat machinery reads the same fields for it as for anything
        # else; nothing about the flag exempts it.
        assert row["extra_edition"] == 1
        assert repeats.normalize_title(row["title"]) == repeats.normalize_title(
            EXTRA_EDITION_TITLE
        )
    finally:
        connection.close()
