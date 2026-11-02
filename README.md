# Rainmaker — Streaming and durable raw recording

Read-only Python starter: discover contracts via REST, monitor multiple contracts over one authenticated WebSocket subscription, and maintain validated local books in RAM.

## Install (Mac / VS Code)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python discover.py --limit 20
```

For election-only discovery, find the correct election series ticker on Kalshi and use `python discover.py --series YOUR_SERIES_TICKER`. Do not assume that the default unfiltered output contains elections.

## Live monitoring (read-only)

Generate an API key in Kalshi's account settings. Prefer a read-only key if offered. Store the private key outside the repository. Copy `.env.example` to `.env` and set `KALSHI_KEY_ID` and `KALSHI_PRIVATE_KEY_PATH` (absolute path to PEM file). Do not paste the private key into chat or commit it.

```bash
cp .env.example .env
# edit .env locally
python monitor.py EXACT_MARKET_TICKER
```

Multiple contracts use the same command:

```bash
python monitor.py FIRST_MARKET_TICKER SECOND_MARKET_TICKER
```

`orderbook.py` validates snapshots atomically and applies Decimal price-level deltas.
Quote access raises an error until a valid snapshot exists. `market_stream.py` owns
one real-time subscription and checks its shared sequence before market routing,
including sequence-bearing control responses. Per-market sequences are not consecutive.

A corrupt individual book is invalidated and requests `get_snapshot`; healthy peers
continue. Deltas for the recovering market are discarded until its replacement
snapshot. Any shared sequence anomaly invalidates every market and requests all
snapshots. Overlapping recovery failures rebuild the subscription: unsubscribe,
wait for confirmation, then subscribe. Failed rebuilds reconnect the socket.
Repeated subscribe alone does not refresh existing markets.

The controller uses a 10-second command/snapshot deadline (constructor configurable),
a 10-second connection deadline, and reconnect backoff capped at 30 seconds with
jitter. Backoff resets only after 30 synchronized seconds. Unsupported snapshot
recovery falls back to rebuilding. Permanent command/authentication errors stop.
Errors are reported without server payloads or authentication headers.

Terminal output is sampled once a second and rendered in a worker thread, keeping
console I/O out of the receive loop. Book-event age describes silence, not proof of
staleness. Transport keepalive is separate; quiet markets do not reconnect just
because no price changed. Invalid/recovering books display unavailable quotes.

Protocol references: [orderbook updates](https://docs.kalshi.com/websockets/orderbook-updates)
and [connection commands](https://docs.kalshi.com/websockets/websocket-connection).
Snapshot completion is determined by received replacement books, not an assumed
ordering of snapshot responses and command acknowledgments.

Run tests:

```bash
pytest -q
```

## Known limitations

- Read-only terminal monitor; no orders, dashboard, or forecasts.
- Fixed market membership for a running CLI session; restart to change tickers.
- One shared subscription means sequence failures affect all its markets.
- Real-time mode only; no conflation, redundant streams, or delta replay.
- No end-to-end latency guarantee or automatic age-based refresh of quiet books.
- One serialized recovery batch; a second corruption during recovery escalates to
  subscription rebuild rather than overlapping requests.
- One rebuild attempt per connection, then connection fallback. Persistent channel
  errors/overflow retry with capped backoff; reduce market count if throughput is insufficient.
- Synthetic tests exercise protocol behavior, not live Kalshi service guarantees.
- Tests use fake clocks/transports and a localhost WebSocket server; they require
  no environment loading, credentials, or external network access.


## Optional raw recording (Milestone B, Part A)

Recording is opt-in; commands without `--record-db` keep the existing behavior.
Use a local filesystem path with an existing parent directory:

```bash
python monitor.py KXDSENATESEATS-27-52 KXDSENATESEATS-27-51 --record-db rainmaker-events.sqlite
```

One background thread owns the SQLite connection, schema, JSON serialization, and
batched transactions. The database uses WAL and `synchronous=FULL`. The live receive
loop performs bounded memory admission only; it never waits for database writes.
Recording adds allocation/lock overhead and background CPU work; it is not a
zero-overhead or unlimited-throughput guarantee. No additional packages are needed.

The journal retains every received application message before JSON parsing,
including whitespace, malformed JSON, binary messages, duplicate sequences, ignored
messages, acknowledgments, and errors. Text stays text and bytes stay bytes. It
also records subscription send attempts/results, subscription epochs, connection
generations, invalidations, recovery, integrity failures, and processing results.
Parent ordinals link processing/recovery to received messages and send results to
command attempts. Authentication headers, signatures, key paths, and environment
contents are never sent to the recorder. This is application-message recording,
not packet capture; WebSocket ping/pong control frames are not journaled.

### Ordering and completeness

Every offered record gets a run-local increasing ordinal, including records rejected
by admission. A new process invocation gets a new UUID run ID. Order by `(run_id,
ordinal)` within a run, never by UTC timestamp or exchange sequence alone. Each
record carries UTC epoch nanoseconds and monotonic nanoseconds; monotonic clocks
are meaningful only within a process run. Exchange timestamps remain in the raw
payload. Shared subscription sequences and identifiers are indexed as decimal text
to preserve even unusually large received integer values without SQLite overflow.

The display distinguishes `issued`, `committed`, `buffered` (including in-flight
batches), `lost`, and writer `errors`. `committed` is the highest durably journaled
ordinal, not a promise that every preceding ordinal exists. Consult discontinuities.

- `OPEN`: current run or an uncleanly stopped run; full coverage is unverified.
- `INCOMPLETE`: sticky indication of overflow, a writer failure, a detected stream
  integrity failure/disconnection, or an unconfirmed shutdown. Live monitoring continues.
- `FINALIZING` (live health only): all data committed and the success decision won
  before the drain deadline, but the metadata publication commit is unconfirmed.
  This is not `COMPLETE`; SQLite still shows provisional `OPEN` until publication.
- `COMPLETE`: recording stopped and drained with no detected recording/stream failure.
  This describes observed application traffic, not proof that the exchange delivered
  every event or that an initial snapshot existed for every desired interval.
- `clean_shutdown=1`: admitted records drained and run finalization committed. A run
  can be cleanly drained yet `INCOMPLETE`, for example after a sequence gap.

Queue bounds default to 10,000 uncommitted records and an estimated 64 MiB, including
in-flight batches; size accounting conservatively budgets text and envelope overhead,
not exact Python heap usage. Transactions contain up to 500 records. Idle writer
wakeups occur every 100 ms. SQLite commit latency is additional. These bounds are
constructor-configurable in `Recorder`; the CLI intentionally exposes only the path.

When admission fails, the recorder counts lost ordinals immediately and attaches the
contiguous lost range to the next admitted record, or persists it during finalization
if it is a tail loss. Database failures retry the same batch with stable identities;
a retry with conflicting content is rejected. Failed writes do not advance the
committed watermark. There is no unbounded spill queue or synchronous fallback.

Press Control+C for a bounded five-second drain attempt. Finalization has one
memory-locked decision state machine; no memory lock spans SQLite I/O:

1. `OPEN` accepts records. Closing stops admission and enters `DRAINING`.
2. The final **data transaction is provisional**: it persists journal records,
   loss ranges and watermarks, but never publishes `COMPLETE` or clean shutdown.
3. Only after that transaction commits does the writer choose `DECIDED_COMPLETE`
   or `DECIDED_INCOMPLETE` under the same lock used for deadline decisions.
4. A separate metadata transaction publishes the decision. Only confirmed
   publication produces terminal health `COMPLETE`/`INCOMPLETE` and a clean flag.

If the drain deadline wins before step 3, the decision is irreversibly incomplete.
A data transaction that commits after that deadline may improve the committed
watermark, but can only be followed by `INCOMPLETE` metadata. It cannot promote the
run to `COMPLETE`. If the process exits before publication, provisional `OPEN`
metadata and `clean_shutdown=0` remain unverified, never verified complete.

If the writer wins step 3 before the deadline but the **publication** commit is
slow, bounded shutdown returns `FINALIZING`, not `INCOMPLETE` and not `COMPLETE`.
That fixed success decision may publish after the deadline. This distinction avoids
claiming a timeout failure while an already-authorized success commit is in flight.
A failed publication is rolled back and changes the decision to incomplete; it is
never retried as `COMPLETE`. Database lock/I/O errors may prevent any final metadata
update, leaving provisional `OPEN` or `INCOMPLETE` and no clean-shutdown claim.

Disk system calls cannot be forcibly canceled. The writer is a daemon, so bounded
shutdown does not wait indefinitely for them. Do not assume queued records survived
an unconfirmed shutdown. Restarting creates a separate run and never promotes an
earlier `OPEN` run or restores live books from disk. A permanently unavailable
database cannot durably log its own failure: consult live health and treat
unfinalized runs as unverified.

Unexpected receiver, parser, handler or session-cleanup failures record a sanitized
exception class, mark coverage incomplete, and propagate visibly. Captured raw data
is retained, with parser-failure processing outcomes linked to its ordinal.
Intentional cancellation while waiting for a new message can still drain cleanly;
cancellation while processing captured input remains incomplete.

Generated `.sqlite`, `.sqlite3`, `.db` files and their WAL/SHM/journal sidecars are
Git-ignored, as is the designated root `recordings/` data directory. Source files
and SQL migration files outside that reserved data directory remain visible.

### Inspect the database

Use a separate read-only connection with short queries:

```bash
sqlite3 -readonly rainmaker-events.sqlite "SELECT run_id, coverage, clean_shutdown, last_issued_ordinal, committed_ordinal, lost_count, error_count FROM recording_runs;"
sqlite3 -readonly rainmaker-events.sqlite "SELECT run_id, ordinal, kind, generation, subscription_epoch, sid, seq, parent_ordinal FROM journal ORDER BY run_id, ordinal LIMIT 30;"
sqlite3 -readonly rainmaker-events.sqlite "SELECT * FROM discontinuities ORDER BY run_id, first_ordinal;"
sqlite3 -readonly rainmaker-events.sqlite "SELECT run_id, ordinal, details_json FROM journal WHERE kind IN ('integrity','connection_error','subscription_error') ORDER BY run_id, ordinal;"
```

`discontinuities` contains exact queue-overflow ranges. For an unclean shutdown,
ordinals beyond the last committed watermark are an unverified tail, even if no
loss marker survived. Source sequence failures are separate `integrity` journal
records, not fabricated counts of missing exchange messages. Long reader transactions
can delay WAL checkpoints. Back up a closed database or use SQLite's backup facility;
do not copy only the main file while a live WAL is outstanding.

Schema versioning uses `PRAGMA user_version`; version 0 initializes idempotently,
version 1 reopens, and unknown versions are refused without overwriting their schema.
This part does not materialize quotes, implement replay, or collect market metadata.
Historical simulations must reject incomplete intervals and establish valid snapshot
baselines; recording raw messages alone does not establish executable fills.

Run storage/failure tests and the bounded 5,000-message stress test locally:

```bash
python -m pytest -q
python -m pytest -q tests/test_recording.py -k stress
```

All automated recording tests use temporary local databases, synthetic messages, or
a localhost WebSocket server. They do not load credentials or contact Kalshi.
