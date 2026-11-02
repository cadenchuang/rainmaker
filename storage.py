"""SQLite schema and writer-thread implementation; no connection crosses threads."""
import json
import sqlite3
import time

SCHEMA_VERSION = 1


class IdempotencyError(ValueError):
    pass


class JournalStore:
    def __init__(self, path):
        self.db = sqlite3.connect(path, timeout=0.1)
        try:
            version = self.db.execute('PRAGMA user_version').fetchone()[0]
            if version not in (0, SCHEMA_VERSION):
                raise ValueError('Unsupported recording schema version')
            if self.db.execute('PRAGMA journal_mode=WAL').fetchone()[0].lower() != 'wal':
                raise ValueError('Recording requires a local WAL-capable database')
            self.db.execute('PRAGMA synchronous=FULL')
            self.db.execute('PRAGMA foreign_keys=ON')
            with self.db:
                self.db.execute('''CREATE TABLE IF NOT EXISTS recording_runs (
                    run_id TEXT PRIMARY KEY, started_utc_ns INTEGER NOT NULL,
                    started_monotonic_ns INTEGER NOT NULL, ended_utc_ns INTEGER,
                    coverage TEXT NOT NULL, clean_shutdown INTEGER NOT NULL DEFAULT 0,
                    last_issued_ordinal INTEGER NOT NULL DEFAULT 0,
                    committed_ordinal INTEGER NOT NULL DEFAULT 0,
                    lost_count INTEGER NOT NULL DEFAULT 0, first_lost INTEGER, last_lost INTEGER,
                    error_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS journal (
                    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
                    ordinal INTEGER NOT NULL, kind TEXT NOT NULL,
                    utc_ns INTEGER NOT NULL, monotonic_ns INTEGER NOT NULL,
                    generation INTEGER NOT NULL, subscription_epoch INTEGER NOT NULL,
                    sid TEXT, command_id TEXT, seq TEXT, ticker TEXT, event_type TEXT,
                    parent_ordinal INTEGER, raw BLOB, details_json TEXT NOT NULL,
                    PRIMARY KEY (run_id, ordinal))''')
                self.db.execute('''CREATE TABLE IF NOT EXISTS discontinuities (
                    run_id TEXT NOT NULL REFERENCES recording_runs(run_id),
                    first_ordinal INTEGER NOT NULL, last_ordinal INTEGER NOT NULL,
                    reason TEXT NOT NULL, PRIMARY KEY (run_id, first_ordinal))''')
                self.db.execute('CREATE INDEX IF NOT EXISTS journal_market ON journal(ticker, run_id, ordinal)')
                self.db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def values(record):
        sid, command_id, seq = record.sid, record.command_id, record.seq
        ticker, event_type = record.ticker, record.event_type
        if record.kind == 'ws_received':
            # Extract searchable fields off the live path; raw always remains untouched.
            try:
                event = json.loads(record.raw)
            except (ValueError, TypeError, RecursionError):
                event = None
            if isinstance(event, dict):
                sid = event.get('sid') if type(event.get('sid')) is int else sid
                command_id = event.get('id') if type(event.get('id')) is int else command_id
                seq = event.get('seq') if type(event.get('seq')) is int else seq
                event_type = event.get('type') if isinstance(event.get('type'), str) else None
                payload = event.get('msg')
                if event_type == 'subscribed' and isinstance(payload, dict) and type(payload.get('sid')) is int:
                    sid = payload['sid']
                if isinstance(payload, dict) and isinstance(payload.get('market_ticker'), str):
                    ticker = payload['market_ticker']
        # Malformed JSON string values may contain escaped unpaired surrogates.
        # Do not let searchable metadata prevent durable preservation of the raw message.
        def safe_text(value):
            if value is None:
                return None
            try:
                value.encode('utf-8')
                return value
            except UnicodeEncodeError:
                return None
        return (record.run_id, record.ordinal, record.kind, record.utc_ns,
                record.monotonic_ns, record.generation, record.subscription_epoch,
                str(sid) if sid is not None else None,
                str(command_id) if command_id is not None else None,
                str(seq) if seq is not None else None,
                safe_text(ticker), safe_text(event_type), record.parent_ordinal, record.raw,
                json.dumps(dict(record.details), sort_keys=True))

    def persist(self, records, recorder, *, finish=False):
        health = recorder.health()
        committed = max([health.committed_ordinal] + [r.ordinal for r in records])
        # Even the last data transaction is provisional. It MUST NOT publish
        # COMPLETE using state read before its commit (which may block).
        coverage = 'INCOMPLETE' if health.coverage == 'INCOMPLETE' else 'OPEN'
        with self.db:
            self.db.execute('''INSERT OR IGNORE INTO recording_runs
                (run_id, started_utc_ns, started_monotonic_ns, coverage) VALUES (?, ?, ?, ?)''',
                (recorder.run_id, recorder.started_utc_ns, recorder.started_monotonic_ns, 'OPEN'))
            for record in records:
                values = self.values(record)
                previous = self.db.execute('SELECT * FROM journal WHERE run_id=? AND ordinal=?',
                                           (record.run_id, record.ordinal)).fetchone()
                if previous is not None:
                    if previous != values:
                        raise IdempotencyError('Conflicting content for journal identity')
                else:
                    self.db.execute('INSERT INTO journal VALUES (' + ','.join(['?'] * 15) + ')', values)
                if record.lost_before:
                    self._loss(record.run_id, record.lost_before)
            if finish and recorder._pending_loss:
                self._loss(recorder.run_id, recorder._pending_loss)
            self.db.execute('''UPDATE recording_runs SET ended_utc_ns=?, coverage=?, clean_shutdown=?,
                last_issued_ordinal=MAX(last_issued_ordinal,?), committed_ordinal=MAX(committed_ordinal,?), lost_count=?, first_lost=?, last_lost=?,
                error_count=?, last_error=? WHERE run_id=?''',
                (None, coverage, 0, health.issued_ordinal,
                 committed, health.lost_count, health.first_lost, health.last_lost,
                 health.error_count, health.last_error, recorder.run_id))

    def publish_final(self, recorder):
        # The decision was taken after the final data commit, under the same lock
        # used by close(). No lock is held during this SQLite transaction.
        coverage, clean = recorder._publication()
        health = recorder.health()
        with self.db:
            self.db.execute('''UPDATE recording_runs SET ended_utc_ns=?,
                coverage=CASE WHEN coverage='INCOMPLETE' THEN 'INCOMPLETE' ELSE ? END,
                clean_shutdown=?, error_count=?, last_error=? WHERE run_id=?''',
                (time.time_ns(), coverage, int(clean), health.error_count,
                 health.last_error, recorder.run_id))

    def _loss(self, run_id, loss):
        previous = self.db.execute(
            'SELECT last_ordinal, reason FROM discontinuities WHERE run_id=? AND first_ordinal=?',
            (run_id, loss.first)).fetchone()
        if previous is not None and previous != (loss.last, 'queue_overflow'):
            raise IdempotencyError('Conflicting discontinuity identity')
        self.db.execute('INSERT OR IGNORE INTO discontinuities VALUES (?, ?, ?, ?)',
                        (run_id, loss.first, loss.last, 'queue_overflow'))

    def close(self):
        self.db.close()


def writer_main(recorder, path):
    store = None
    batch = []
    try:
        while True:
            # Access only short-lived memory locks; never hold one over SQLite I/O.
            with recorder._condition:
                expired = recorder._expire_locked()
                finishing = recorder._closing and not recorder._queue
            try:
                if store is None:
                    store = JournalStore(path)
                if not batch and not expired:
                    batch = recorder._take_batch()
                with recorder._condition:
                    finishing = recorder._closing and not recorder._queue
                store.persist([r for r, _ in batch], recorder, finish=finishing or expired)
                recorder._ack(batch)
                batch = []
                if finishing or expired:
                    recorder._decide()
                    store.publish_final(recorder)
                    recorder._published()
                    return
            except Exception as exc:
                recorder._failure(exc)
                if store is not None:
                    store.close()
                    store = None
                with recorder._condition:
                    if recorder._closing and time.monotonic() >= recorder._deadline:
                        return  # Failed publication leaves provisional OPEN/INCOMPLETE metadata.
                    recorder._condition.wait(0.1)
    finally:
        if store is not None:
            store.close()
