"""Durable logical-announcement families: remembering that two IDs are one thing.

The 1 September 2026 regression is the reason this file exists. The Cayuse
announcement had already been recognized as a repost family -- 6768 was matched
to 6665 on 31 August -- and the next repost, 6769, still shipped labelled NEW.
Two separate defects had to line up for that:

  1. the comparison ran from scratch every morning against a corpus query that
     needed a multi-megabyte SQLite temp file, and that morning the filesystem
     holding `TMPDIR` was mounted read-only, so the whole stage raised
     `unable to open database file` and was skipped; and
  2. nothing carried the finding forward, so a single lost day lost the
     classification outright, and an original that ages out of the delivered
     corpus can never anchor a future repost at all.

So the tests here are about *memory* and *reach*: once DailyMail knows two
SubmissionIds are the same logical announcement, a later repost must resolve
against the family however long ago the original was delivered -- while every
veto that keeps a genuinely new occurrence NEW still applies to every single
comparison.
"""

from __future__ import annotations

import json

import pytest

from conftest import RENDER_REGRESSION
from dailymail import daily, db, repeats

TARGET = "2026-09-01"

# Rowan announcement 6665/6667/6668/6768/6769: five SubmissionIds, one
# announcement, carrying byte-identical bodies on five different days.
CAYUSE_TITLE = (
    "Coming soon: A modernized Cayuse platform to better support our research "
    "enterprise"
)
CAYUSE_HTML = (
    "<p>The Division of Research is preparing to move to a modernized Cayuse "
    "platform. Cayuse SP and Cayuse 424 will be replaced by Cayuse Sponsored "
    "Projects and Cayuse Proposals.</p>"
    '<p><a href="https://research.rowan.edu/cayuse">Cayuse at Rowan</a></p>'
)
CAYUSE_TEXT = (
    "The Division of Research is preparing to move to a modernized Cayuse "
    "platform. Cayuse SP and Cayuse 424 will be replaced by Cayuse Sponsored "
    "Projects and Cayuse Proposals.\nCayuse at Rowan."
)


def cayuse(submission_id: int, **overrides) -> dict:
    record = {
        "submission_id": submission_id,
        "title": CAYUSE_TITLE,
        "full_body": CAYUSE_HTML,
        "body_text": CAYUSE_TEXT,
        "category_id": 5,
        "source_audience": "Employees",
        "is_event": 0,
        "event_date": None,
        "event_start_time": None,
        "event_location": None,
        "contact_email": "lezotte@rowan.edu",
        "submitted_by_email": "milone@rowan.edu",
        "content_hash": f"hash-{submission_id}",
        "status": "New",
        "source_status": "New",
    }
    record.update(overrides)
    return record


def delivered(record: dict, when: str) -> dict:
    """The same announcement as it appears in the delivered index."""
    return dict(record, last_delivered_date=when, first_delivered_date=when)


@pytest.fixture
def families(settings_obj):
    connection = db.connect()
    db.initialize(connection)
    yield connection
    connection.close()


def seed(connection, record: dict, *, on: str, delivered_on: str | None = None):
    """Persist one announcement, optionally as something the reader was sent.

    Goes through the real `db` writers rather than raw SQL so the fixtures obey
    the same foreign keys, category rows and version history production does.
    """
    with db.transaction(connection):
        db.upsert_category(
            connection, category_id=record["category_id"],
            title=f"Category {record['category_id']}", rowan_rank=1, color=None,
            is_active=True, manual_priority=record["category_id"],
        )
        version_id, _ = db.record_announcement(
            connection,
            dict(record, distribution_dates=[on], first_distribution_date=on),
            observed_at=db.now_utc(),
        )
        db.record_daily(
            connection, target_date=on, submission_id=record["submission_id"],
            version_id=version_id, status="New", changed=False,
            observed_at=db.now_utc(),
        )
    if delivered_on:
        # `record_delivery` opens its own transaction, so it stays outside.
        db.record_delivery(
            connection, target_date=delivered_on,
            recipient="reader@example.invalid", content_hash_value="h",
            state="sent", sent_at=db.now_utc(),
        )
    return version_id


# --- the reported Cayuse regression ------------------------------------------


def test_the_september_first_cayuse_repost_is_shown_as_standing():
    """The exact production case: 6769 repeats 6665, delivered six days earlier."""
    match = repeats.compare(cayuse(6769), delivered(cayuse(6665), "2026-08-26"))
    assert match is not None
    assert match.matched_submission_id == 6665
    assert match.method == repeats.METHOD_EXACT
    assert match.display_status == "Standing"
    assert match.body_similarity == 1.0
    assert not match.materially_changed


def test_the_cayuse_family_reaches_past_the_delivered_corpus(families):
    """The durable half of the fix.

    Here the delivered index handed to the matcher is *empty* -- as it would be
    once the original has aged out of whatever window a future implementation
    uses -- and the match is found anyway, because 6665 is a known member of the
    family this repost belongs to.
    """
    seed(families, cayuse(6665), on="2026-08-26", delivered_on="2026-08-26")
    key = repeats.family_key(CAYUSE_TITLE, 5, "Employees")
    with db.transaction(families):
        family_id = db.upsert_family(
            families, family_key=key, canonical_submission_id=6665,
            normalized_title=repeats.normalize_title(CAYUSE_TITLE),
            category_id=5, source_audience="Employees",
        )
        db.add_family_member(families, family_id=family_id, submission_id=6665)

    matches = repeats.find_repeats(
        [cayuse(6769)],
        [],  # nothing at all in the delivered index
        family_provider=lambda k: db.family_member_index(families, k),
        load_bodies=lambda ids: db.announcement_bodies(families, ids),
    )
    assert "6769" in matches, "the family should have supplied the candidate"
    assert matches["6769"].matched_submission_id == 6665
    assert matches["6769"].evidence["candidate_source"] == "family"
    assert matches["6769"].via_family


def test_a_family_member_the_reader_never_received_cannot_demote_anything(families):
    """Membership widens the search; it never lowers the bar.

    A family whose members were collected but never *delivered* justifies
    nothing: "the reader has already seen this" is the only claim that earns a
    demotion, and it is still checked per candidate.
    """
    seed(families, cayuse(6665), on="2026-08-26")  # collected, never sent
    key = repeats.family_key(CAYUSE_TITLE, 5, "Employees")
    with db.transaction(families):
        family_id = db.upsert_family(
            families, family_key=key, canonical_submission_id=6665,
            normalized_title=repeats.normalize_title(CAYUSE_TITLE),
            category_id=5, source_audience="Employees",
        )
        db.add_family_member(families, family_id=family_id, submission_id=6665)

    matches = repeats.find_repeats(
        [cayuse(6769)], [],
        family_provider=lambda k: db.family_member_index(families, k),
        load_bodies=lambda ids: db.announcement_bodies(families, ids),
    )
    assert matches == {}


# --- the chain ---------------------------------------------------------------


def test_a_family_chain_stays_anchored_on_its_original(families):
    """A -> B -> C -> D all record the *first* member, not the previous one.

    Every repost is byte-identical, so each one scores 1.0 against every other
    member. Anchoring on the earliest SubmissionId is what makes the audit trail
    stable: re-running a day cannot make the recorded match wander.
    """
    history = [
        delivered(cayuse(6665), "2026-08-26"),
        delivered(cayuse(6668), "2026-08-28"),
        delivered(cayuse(6768), "2026-08-31"),
    ]
    matches = repeats.find_repeats([cayuse(6769)], history)
    assert matches["6769"].matched_submission_id == 6665


def test_persisting_a_match_grows_the_family_for_the_next_repost(families):
    """One run's finding is the next run's candidate."""
    seed(families, cayuse(6665), on="2026-08-26", delivered_on="2026-08-26")
    seed(families, cayuse(6769), on=TARGET)
    matches = repeats.find_repeats(
        [cayuse(6769)], [delivered(cayuse(6665), "2026-08-26")]
    )
    daily.persist_logical_repeats(families, matches, TARGET)

    family = db.family_for_submission(families, 6769)
    assert family is not None
    assert family["canonical_submission_id"] == 6665
    members = db.family_members(families, family["family_id"])
    assert {row["submission_id"] for row in members} == {6665, 6769}
    assert [row["submission_id"] for row in members if row["is_canonical"]] == [6665]
    assert db.repeat_match(families, 6769)["family_id"] == family["family_id"]
    assert db.daily_record(families, TARGET, 6769)["display_status"] == "Standing"
    assert db.daily_record(families, TARGET, 6769)["status"] == "New"


def test_persisting_the_same_match_twice_changes_nothing(families):
    """Idempotent: a re-run must not grow the family or duplicate a member."""
    seed(families, cayuse(6665), on="2026-08-26", delivered_on="2026-08-26")
    seed(families, cayuse(6769), on=TARGET)
    matches = repeats.find_repeats(
        [cayuse(6769)], [delivered(cayuse(6665), "2026-08-26")]
    )
    daily.persist_logical_repeats(families, matches, TARGET)
    first = db.family_for_submission(families, 6769)
    daily.persist_logical_repeats(families, matches, TARGET)
    second = db.family_for_submission(families, 6769)
    assert first["family_id"] == second["family_id"]
    assert second["member_count"] == 2
    assert families.execute(
        "SELECT COUNT(*) FROM logical_announcement_families"
    ).fetchone()[0] == 1
    assert families.execute(
        "SELECT COUNT(*) FROM logical_announcement_members"
    ).fetchone()[0] == 2


def test_the_accessibility_family_resolves_every_repost():
    """The Phase 4 case that already worked must go on working.

    6686 -> 6687 -> 6688 -> 6689: four SubmissionIds two minutes apart, all
    carrying the identical body.
    """
    title = "Digital Accessibility at Rowan: What Faculty Need to Know"
    html = (
        "<h2>Digital Accessibility at Rowan: What Faculty Need to Know</h2>"
        "<p>Rowan University is committed to making its websites and course "
        "materials accessible to all users.</p>"
        '<p><a href="https://go.rowan.edu/accessibilityguide">Guide</a></p>'
    )
    text = (
        "Digital Accessibility at Rowan: What Faculty Need to Know\n"
        "Rowan University is committed to making its websites and course "
        "materials accessible to all users.\nGuide."
    )

    def member(submission_id, **overrides):
        return cayuse(
            submission_id, title=title, full_body=html, body_text=text,
            category_id=4, contact_email="facultycenter@rowan.edu",
            submitted_by_email="perry@rowan.edu", **overrides,
        )

    history = [
        delivered(member(6686), "2026-08-27"),
        delivered(member(6687), "2026-08-28"),
        delivered(member(6688), "2026-08-30"),
    ]
    matches = repeats.find_repeats([member(6689)], history)
    assert matches["6689"].matched_submission_id == 6686
    assert matches["6689"].method == repeats.METHOD_EXACT


# --- Coffee Hours ------------------------------------------------------------


def coffee(submission_id: int, **overrides) -> dict:
    entry = RENDER_REGRESSION["6702"]
    record = {
        "submission_id": submission_id,
        "title": entry["title"],
        "full_body": entry["full_body"],
        "body_text": entry["body_text"],
        "category_id": entry["category_id"],
        "source_audience": entry["source_audience"],
        "is_event": 0,
        "event_date": None,
        "event_start_time": None,
        "event_location": None,
        "contact_email": "redacted@example.invalid",
        "submitted_by_email": "redacted@example.invalid",
        "content_hash": f"hash-{submission_id}",
        "status": "New",
        "source_status": "New",
    }
    record.update(overrides)
    return record


def test_the_september_first_coffee_hours_repost_is_shown_as_standing():
    """The second reported case: 6702 repeats 6701, delivered on 31 August.

    Rowan created a fresh SubmissionId for the *same* Focus on Research session
    set -- same two sittings, same times, same registration form -- so the reader
    has already been sent this.
    """
    match = repeats.compare(coffee(6702), delivered(coffee(6701), "2026-08-31"))
    assert match is not None
    assert match.matched_submission_id == 6701
    assert match.method == repeats.METHOD_EXACT
    assert match.display_status == "Standing"
    assert match.evidence["prior_delivered"] == "2026-08-31"


def test_a_different_coffee_hours_topic_is_a_new_announcement():
    """A different focus area is a different announcement, not a repost.

    The titles differ, which is a hard veto: no amount of shared boilerplate
    makes `Focus on Teaching` the announcement the reader already read about
    research.
    """
    other = coffee(
        6900,
        title="Provost’s Coffee Hours (Focus on Teaching)",
        body_text=RENDER_REGRESSION["6702"]["body_text"].replace(
            "focused on research", "focused on teaching"
        ),
    )
    assert repeats.compare(other, delivered(coffee(6701), "2026-08-31")) is None


def test_a_materially_new_coffee_hours_session_set_is_a_new_announcement():
    """Same title, same boilerplate, entirely new sittings -> still NEW.

    This is the veto that matters for a series: the substance of a Coffee Hours
    announcement *is* its dates, and everything around them is template.
    """
    body = RENDER_REGRESSION["6702"]["body_text"]
    october = coffee(
        6901,
        body_text=body.replace(
            "Thursday, September 10 from 11:00-12:30", "Thursday, October 8 from 11:00-12:30"
        ).replace(
            "Monday, September 21 from 2:30-4:00", "Monday, October 19 from 2:30-4:00"
        ),
    )
    match = repeats.compare(october, delivered(coffee(6701), "2026-08-31"))
    assert match is None


def test_a_recurring_event_on_a_genuinely_new_date_stays_new():
    """The central protection, restated for a body-only date.

    Rowan's structured `Event` flag is false for most real events, so the veto
    has to work from the dates in the prose too.
    """
    body = (
        "The Faculty Senate meets in Bunce Hall. All faculty are welcome.\n"
        "Wednesday, September 16 from 3:00-5:00\n"
    )
    prior = cayuse(
        7000, title="Faculty Senate Meeting", body_text=body,
        full_body=f"<p>{body}</p>",
    )
    current = cayuse(
        7001, title="Faculty Senate Meeting",
        body_text=body.replace("September 16", "October 21"),
        full_body=f"<p>{body.replace('September 16', 'October 21')}</p>",
    )
    assert repeats.compare(current, delivered(prior, "2026-09-01")) is None


def test_a_room_change_is_standing_and_updated():
    """Rowan announcement 6618: same screening, same time, a different building.

    Standing, because the reader was sent it. UPDATED, because if they act on
    what they remember they walk to the wrong building.
    """
    # Padded to a realistic length, because the band the change falls into is
    # the whole point: the real 6618/6617 pair scored 0.9874, and a two-line
    # synthetic body would make the same edit look far more drastic than it is.
    body = (
        "The Rowan Center for Holocaust and Genocide Studies invites the "
        "University community to a screening of Eldorado: Everything the Nazis "
        "Hate, a documentary about the destruction of Berlin's queer culture "
        "under National Socialism.\n"
        "Tuesday September 15 5:00-7:00 in Robinson 102\n"
        "A discussion concerning the importance of this film led by Professor "
        "Manning will follow. Please join us for this screening and "
        "conversation. Refreshments will be served and no registration is "
        "required to attend."
    )
    prior = cayuse(
        6617, title="Eldorado Film Screening", body_text=body,
        full_body=f"<p>{body}</p>", category_id=27,
    )
    moved = body.replace("Robinson 102", "Business 208")
    current = cayuse(
        6618, title="Eldorado Film Screening", body_text=moved,
        full_body=f"<p>{moved}</p>", category_id=27,
    )
    match = repeats.compare(current, delivered(prior, "2026-08-25"))
    assert match is not None
    assert match.method == repeats.METHOD_NEAR
    assert match.materially_changed, "a venue change must carry the UPDATED badge"
    assert match.evidence["near_identical_change"] == "substantive"


def test_a_spelling_correction_is_not_an_update():
    """The other side of the same rule: a typo fix is not news."""
    body = "Rowan University is committed to accessible course materials."
    prior = cayuse(6800, title="Accessibility", body_text=body, full_body=f"<p>{body}</p>")
    current = cayuse(
        6801, title="Accessibility",
        body_text=body.replace("committed", "commited"),
        full_body=f"<p>{body.replace('committed', 'commited')}</p>",
    )
    match = repeats.compare(current, delivered(prior, "2026-08-25"))
    assert match is not None
    assert not match.materially_changed
    assert match.evidence["near_identical_change"] == "spelling_only"


def test_the_planetarium_next_run_is_not_a_revision_of_the_last_one():
    """The false positive the date veto exists for.

    Rowan announcement 6749 reposts the same planetarium show with an entirely
    new run of dates. Everything except the dates is boilerplate, so it scored
    0.96 similar and was demoted to Standing on 31 August -- hiding a season the
    reader had never been told about.
    """
    prior_body = (
        "Planetarium Show - James Webb Space Telescope: The Story Unfolds\n"
        "Edelman Planetarium, Science Hall.\n"
        "Saturdays at 4 p.m. from July 11 through August 29\n"
        "Tickets are available at the door."
    )
    current_body = prior_body.replace(
        "Saturdays at 4 p.m. from July 11 through August 29",
        "Saturdays at 4 p.m. from September 5 through November 21",
    )
    prior = cayuse(
        6544, title="Planetarium Show - James Webb Space Telescope",
        body_text=prior_body, full_body=f"<p>{prior_body}</p>", category_id=7,
    )
    current = cayuse(
        6749, title="Planetarium Show - James Webb Space Telescope",
        body_text=current_body, full_body=f"<p>{current_body}</p>", category_id=7,
    )
    match = repeats.compare(current, delivered(prior, "2026-08-25"))
    assert match is None, "a new run of dates is not a revision of the old one"


def test_an_identical_body_is_exempt_from_the_date_veto():
    """Two identical bodies cannot disagree about a date, so the veto is skipped.

    Without the exemption the veto would be dead weight on the commonest case --
    and worse, an artefact of how the date parser guesses omitted years.
    """
    body = "Applications open. Sessions run Thursday, September 10 from 11:00-12:30."
    prior = cayuse(6810, title="Sessions", body_text=body, full_body=f"<p>{body}</p>")
    current = cayuse(6811, title="Sessions", body_text=body, full_body=f"<p>{body}</p>")
    match = repeats.compare(current, delivered(prior, "2026-08-25"))
    assert match is not None
    assert match.evidence["body_dates"] == "identical_body"


# --- schema ------------------------------------------------------------------


def test_the_v4_migration_is_additive_and_idempotent(families):
    """Running initialize twice must not change anything it already built."""
    before = sorted(
        row["name"] for row in families.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    )
    assert "logical_announcement_families" in before
    assert "logical_announcement_members" in before
    assert "display_status_corrections" in before
    assert db.schema_version(families) == db.SCHEMA_VERSION == 4

    db.initialize(families)
    after = sorted(
        row["name"] for row in families.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    )
    assert before == after
    assert families.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert families.execute("PRAGMA foreign_key_check").fetchall() == []


def test_pairwise_findings_are_backfilled_into_families(families):
    """The knowledge earned before the schema existed is not thrown away."""
    seed(families, cayuse(6665), on="2026-08-26", delivered_on="2026-08-26")
    seed(families, cayuse(6768), on="2026-08-31")
    with db.transaction(families):
        db.record_repeat_match(families, {
            "submission_id": 6768,
            "matched_submission_id": 6665,
            "family_key": repeats.family_key(CAYUSE_TITLE, 5, "Employees"),
            "method": repeats.METHOD_EXACT,
            "confidence": 0.99,
            "body_similarity": 1.0,
            "materially_changed": False,
            "evidence": json.dumps({}),
        })
        families.execute("DELETE FROM logical_announcement_families")
        families.execute(
            "UPDATE repeat_matches SET family_id = NULL WHERE submission_id = 6768"
        )
    with db.transaction(families):
        touched = db.backfill_families_from_repeat_matches(families)
    assert touched == 1
    family = db.family_for_submission(families, 6768)
    assert family["canonical_submission_id"] == 6665
    assert {
        row["submission_id"]
        for row in db.family_members(families, family["family_id"])
    } == {6665, 6768}
    assert db.repeat_match(families, 6768)["family_id"] == family["family_id"]

    # Idempotent: running it again touches the same family and adds nobody.
    with db.transaction(families):
        db.backfill_families_from_repeat_matches(families)
    assert db.family_for_submission(families, 6768)["member_count"] == 2


def test_a_correction_records_why_without_touching_rowan_status(families):
    """The audit trail for a retrospective change to what the digest shows."""
    seed(families, cayuse(6769), on=TARGET)
    with db.transaction(families):
        db.set_display_status(
            families, target_date=TARGET, submission_id=6769,
            display_status="Standing",
        )
        db.record_display_status_correction(
            families, target_date=TARGET, submission_id=6769,
            previous_display_status=None, new_display_status="Standing",
            reason="logical-repeat stage skipped; re-resolved",
        )
    record = db.daily_record(families, TARGET, 6769)
    assert record["status"] == "New", "Rowan's own classification is preserved"
    assert record["display_status"] == "Standing"
    corrections = db.display_status_corrections(families, TARGET)
    assert len(corrections) == 1
    assert corrections[0]["previous_display_status"] is None
    assert corrections[0]["new_display_status"] == "Standing"
    assert "re-resolved" in corrections[0]["reason"]


# --- robustness --------------------------------------------------------------


def test_the_delivered_index_carries_no_bodies(families):
    """The query that failed on 1 September no longer reads the whole corpus.

    `delivered_history` is the index pass; bodies are fetched for the shortlist
    that survived title bucketing. Returning `full_body` for every delivered
    announcement is what forced SQLite to materialize the entire delivered
    corpus into a temp file in an unrelated -- and that morning unwritable --
    temp directory.
    """
    seed(families, cayuse(6665), on="2026-08-26", delivered_on="2026-08-26")
    rows = db.delivered_history(families, TARGET)
    assert rows, "the delivered index should have found 6665"
    columns = set(rows[0].keys())
    assert "full_body" not in columns
    assert "body_text" not in columns
    assert {"submission_id", "title", "category_id", "last_delivered_date"} <= columns

    bodies = db.announcement_bodies(families, [6665])
    assert set(bodies) == {6665}
    assert bodies[6665]["full_body"] == CAYUSE_HTML
    assert bodies[6665]["body_text"] == CAYUSE_TEXT


def test_temp_storage_is_pinned_to_memory(families):
    """2 == MEMORY. A sort must never depend on a writable TMPDIR again."""
    assert families.execute("PRAGMA temp_store").fetchone()[0] == 2


def test_announcement_bodies_handles_an_empty_shortlist(families):
    assert db.announcement_bodies(families, []) == {}
