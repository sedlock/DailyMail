# The status snapshot — why `health` stopped reading the database

**Phase 7, 12 September 2026.** Raised by ControlPanel V2.2 as
`docs/work-orders/DAILYMAIL_STATUS_PROBE.md`. This is DailyMail's half.

---

## 1. What was wrong

`dailymail health --json` opened the live database read-only:

```python
sqlite3.connect(f"file:{target}?mode=ro", uri=True)
```

The database is in WAL mode, set unconditionally on every writer open. **A WAL
database cannot be read — not even read-only — without its `-shm` shared-memory
index**, and when `-shm` is absent SQLite must *create* it in the database's own
directory.

SQLite unlinks `-wal` and `-shm` when the last connection closes cleanly, so
`-shm` is absent except during the roughly two minutes `dailymail.service`
actually runs. ControlPanel's collector runs with `ProtectHome=read-only` and
`~/.local/share/dailymail` is not in its `ReadWritePaths`, so the creation fails
and the first query raises `SQLITE_CANTOPEN`.

The command still **exited 0 with a well-formed `controlpanel.status.v1`
document**. It was simply blank: `recent_runs: []`, every database-derived field
`None`, and the SQLite message in `adapter_errors`.

Measured from ControlPanel's own retained snapshots, 2026-09-06 → 2026-09-11:

> **6,786 of 6,801 collections (99.78%)** returned the blank document, and
> **395 consecutive** at the time the work order was written. Every daily run was
> first observed roughly **24 hours late** — at the instant the *next* day's run
> happened to open the database and leave a `-shm` behind.

The discriminator is one file and nothing else:

| `-wal` | `-shm` | directory | result |
|---|---|---|---|
| yes | yes | read-only | **OK** |
| yes | **no** | read-only | **FAIL** `unable to open database file` |
| no | no | read-only | **FAIL** `attempt to write a readonly database` |
| yes | **no** | writable | OK — **and it creates the `-shm`** |

Reproduced independently against live production on 2026-09-12, under the
collector's exact sandbox, before any code was changed.

## 2. What was ruled out, and why

* **`immutable=1`** — *formally disproven, not merely unproven*. Against a copy
  with genuinely uncheckpointed WAL content it **silently ignores the WAL and
  returns stale data with no error**: 51 runs where `mode=ro` read 52. A status
  probe that quietly reports yesterday's digest as the latest is worse than one
  that says "unavailable". It opens cleanly with no `-shm`, which is exactly
  what makes it dangerous. `TestTheWalFailure` keeps that disproof as a test.
* **`nolock=1`** — still `SQLITE_CANTOPEN`.
* **Reverting to DELETE journal mode** — works, but surrenders WAL's
  reader/writer concurrency and crash behaviour for the whole application to
  satisfy a status probe.
* **Granting ControlPanel `ReadWritePaths`** — measured: the probe then *creates*
  `-shm` and `-wal` inside DailyMail's state. A "read-only" probe with a writable
  directory is a writer.

## 3. What DailyMail does instead

**It publishes what it knows, instead of asking an observer to take a lock on
its database to find out.**

After every committed run or delivery transition, DailyMail writes a bounded,
redacted projection of its own state to a file in the **state** directory —
distinct from the database directory. The collector can read that and cannot
write it, which is the whole point.

```
~/.local/state/dailymail/status.json      mode 0600, ≤ 256 KiB
    schema  dailymail.status-snapshot.v1
```

### Contents

Exactly the values `health.build_status` would otherwise read from SQLite, so
the two documents agree *structurally* rather than approximately:

| Field | |
|---|---|
| `schema_version`, `generated_at`, `application_version` | identity and freshness |
| `database_schema_version`, `database_size_bytes` | |
| `statistics` | the full `db.statistics()` count set |
| `recent_runs` | ≤ 10 run rows, exactly the columns `health` selects |
| `last_retrieval_success`, `last_delivery_success` | |
| `latest_delivery`, `last_confirmed_delivery` | state, timestamps, SMTP status, bytes |
| `parking_sources` | verification timestamps and failure count |
| `source` | provenance |

Nothing else. No announcement body, no source HTML, **no recipient address**, no
credential, no header, no environment, no prompt. Free text is clipped at 300
characters on the way in; the run list is capped; the whole document is refused
on read above 256 KiB.

### Ordering

Written at the end of `db.start_run`, `db.finish_run` and `db.record_delivery`,
in each case **outside and after** that function's own `transaction()` block.
Putting it there rather than at the five `daily.py` call sites means the snapshot
cannot drift from the `runs` table, and a rolled-back transaction never reaches
the writer at all. `test_the_snapshot_is_written_after_the_commit_not_during`
proves it by observing the database from a second connection at the moment the
snapshot is built.

### Failure policy

A snapshot write that fails is **logged and swallowed**. It never rolls back
committed state, never suppresses or duplicates a digest, never touches delivery
idempotency, never restarts anything and never sends an alert down the channel
that just failed. Observability is not permitted to change business behaviour;
the next `health` call reports the snapshot as missing or stale instead.

### Atomicity

One writer, `atomic.atomic_write_json` — the `mkstemp` + `os.replace` body that
has written the daily collection artifact since Phase 1, extracted so the
artifact and the snapshot share it rather than drifting. Same-filesystem
temporary file, `fsync` before rename for the snapshot, `0600`, symlink and
non-regular-file destinations refused, and no debris left when a write is
interrupted.

## 4. `health --source`

```sh
dailymail health --json                      # auto (what production uses)
dailymail health --json --source auto
dailymail health --json --source database
dailymail health --json --source snapshot
```

* **auto** — read the database; on failure fall back to the snapshot. The
  fallback is never silent: `adapter_errors` keeps the live database error,
  `status_data_source` says `snapshot`, and the snapshot's schema, path,
  `generated_at`, `age_seconds` and `stale` flag all travel with it.
* **database** — database only, never falls back. Diagnosis, and the test that
  proves the failure being routed around is real.
* **snapshot** — snapshot only, never opens SQLite. This is what proves the
  collector boundary works with no `-shm` present.

### Honesty

Falling back means *"DailyMail's last committed state is available"*. It does
**not** mean *"the live database was successfully probed"*, and the document
never implies it did.

The two are reported separately, because conflating them is how a probe lies:

| situation | `status_data_source` | component health | `adapter_errors` |
|---|---|---|---|
| database read worked | `database` | from the data | empty |
| database failed, snapshot fresh | `snapshot` | **from the data** | the database error |
| database failed, snapshot stale | `snapshot` | `unknown` | database error **+** staleness |
| database failed, no/bad snapshot | `unavailable` | `unknown` | database error **+** the snapshot's own reason |

Reporting `unknown` while holding a fresh snapshot that says this morning's run
delivered would under-report exactly as badly as reporting `healthy` from a
stale one would over-report.

### Staleness is a schedule question

Not an age question. A snapshot written at the end of the 06:30 run is current
all day; the same snapshot is stale the moment a 06:30 comes and goes without a
newer one. `status_snapshot_expected_since` exposes the boundary so ControlPanel
can present it however it likes.

### Never a zero

Missing, malformed, unknown-schema, oversized, unsafe-path, unreadable,
structurally wrong and stale are **eight distinct reported conditions**. None of
them is turned into `recent_runs: []` with a healthy-looking document, because a
document reporting no runs is indistinguishable from a DailyMail that has never
run.

## 5. Seeding it

```sh
dailymail status-snapshot refresh    # rebuild from committed state
dailymail status-snapshot show       # print it, rewrite nothing
```

`refresh` exists so a freshly activated release publishes a correct status
immediately instead of waiting for the next morning. It opens the database
**read-only**, projects committed state and writes one file. It performs no
Rowan request, no SMTP operation, no model call, no render, creates no delivery,
starts no run and writes nothing to the application database — and it exits
non-zero rather than inventing a snapshot it cannot build. It will not create a
database that does not exist.

## 6. Cost

Measured against the real 24 MB production database: **0.2 ms** to build,
**1.2 ms** including the fsynced atomic write, producing a **~9.6 KB** document.
A full `run-daily` publishes four times — about **5 ms** against a ~95 s run.

## 7. ControlPanel

No ControlPanel change is required to consume this, and no new grant to
ControlPanel is involved. ControlPanel's presentation safeguard — retaining what
it last observed and reporting a current probe failure separately — remains
correct and remains deployed; it now has a working probe underneath it.
