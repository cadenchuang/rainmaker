"""Disk failures and concurrency tested locally, with no credentials or network."""
import asyncio
from dataclasses import FrozenInstanceError, replace
import json
import sqlite3
import threading
import time

import pytest
from recording import Recorder, Envelope, LossRange
from storage import JournalStore, IdempotencyError
from market_stream import MarketStream, Reconnect
from test_market_stream import Wire, snap, delta


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError('Timed out waiting for recorder')
        time.sleep(0.001)


def close(recorder, timeout=3):
    return asyncio.run(recorder.close(timeout))


def rows(path, sql, params=()):
    with sqlite3.connect(path) as db:
        return db.execute(sql, params).fetchall()


def test_order_raw_types_clocks_and_watermark(tmp_path):
    path = tmp_path / 'events.db'
    r = Recorder(path)
    raw = ' {"seq":2,"sid":7,"type":"orderbook_delta","msg":{"market_ticker":"A"}} \n'
    first = r.offer('ws_received', raw=raw, generation=4, subscription_epoch=2)
    r.offer('ws_received', raw=b'\xffnot JSON', parent_ordinal=first)
    h = close(r)
    assert h.coverage == 'COMPLETE' and h.clean_shutdown
    assert h.finalization_state == 'COMPLETE'
    assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs') == [('COMPLETE', 1)]
    assert h.committed_ordinal == h.issued_ordinal
    assert h.buffered_records == h.buffered_bytes == 0
    recorded = rows(path, 'SELECT ordinal, raw, generation, subscription_epoch, sid, seq, ticker, utc_ns, monotonic_ns FROM journal ORDER BY ordinal')
    assert [row[0] for row in recorded] == list(range(1, h.issued_ordinal + 1))
    assert recorded[1][1:7] == (raw, 4, 2, '7', '2', 'A')
    assert recorded[2][1] == b'\xffnot JSON'
    assert all(row[7] > 0 and row[8] > 0 for row in recorded)
    assert rows(path, 'PRAGMA journal_mode')[0][0] == 'wal'
    with pytest.raises(FrozenInstanceError):
        h.coverage = 'other'


def test_idempotent_insert_conflict_rolls_back(tmp_path):
    path = tmp_path / 'events.db'
    r = Recorder(path)
    close(r)
    store = JournalStore(path)
    assert store.db.execute('PRAGMA synchronous').fetchone()[0] == 2
    event = Envelope(r.run_id, 100, 'test', 1, 2, raw='same')
    try:
        store.persist([event], r)
        store.persist([event], r)
        assert store.db.execute('SELECT count(*) FROM journal WHERE ordinal=100').fetchone()[0] == 1
        with pytest.raises(IdempotencyError):
            store.persist([replace(event, ordinal=101), replace(event, raw='conflict')], r)
        assert store.db.execute('SELECT count(*) FROM journal WHERE ordinal=101').fetchone()[0] == 0
    finally:
        store.close()


def test_overflow_accounts_inflight_and_persists_discontinuities(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = JournalStore.persist
    def blocked(store, records, recorder, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(store, records, recorder, **kwargs)
    monkeypatch.setattr(JournalStore, 'persist', blocked)
    path = tmp_path / 'events.db'
    r = Recorder(path, max_records=2)
    try:
        assert entered.wait(2)
        r.offer('accepted')
        a = r.offer('lost_a')
        b = r.offer('lost_b')
        h = r.health()
        assert h.coverage == 'INCOMPLETE' and h.lost_count == 2
        assert h.committed_ordinal == 0 and h.buffered_records == 2
        release.set()
        wait_for(lambda: r.health().buffered_records == 0)
        r.offer('after_gap')
        h = close(r)
        assert h.coverage == 'INCOMPLETE' and h.clean_shutdown
        assert rows(path, 'SELECT first_ordinal,last_ordinal FROM discontinuities') == [(a, b)]
        assert rows(path, 'SELECT lost_count FROM recording_runs') == [(2,)]
    finally:
        release.set()
        close(r)


def test_byte_overflow_at_tail_is_not_silent(tmp_path):
    path = tmp_path / 'events.db'
    r = Recorder(path, max_bytes=1024)
    ordinal = r.offer('too_large', raw='x' * 10000)
    h = close(r)
    assert h.coverage == 'INCOMPLETE' and h.lost_count >= 1
    assert rows(path, 'SELECT first_ordinal FROM discontinuities')[0][0] == ordinal


def test_database_failure_retry_and_live_processing(tmp_path, monkeypatch):
    failed = threading.Event()
    release = threading.Event()
    original = JournalStore.persist
    def failure(store, records, recorder, **kwargs):
        if not release.is_set():
            failed.set()
            raise sqlite3.OperationalError('synthetic disk full')
        return original(store, records, recorder, **kwargs)
    monkeypatch.setattr(JournalStore, 'persist', failure)
    r = Recorder(tmp_path / 'events.db')
    try:
        assert failed.wait(2)
        wait_for(lambda: r.health().error_count > 0)
        async def live():
            s, w = MarketStream(['A'], recorder=r), Wire()
            await s.start(w)
            await s.handle(w, {'type': 'subscribed', 'id': w.sent[-1]['id'], 'msg': {'channel': 'orderbook_delta', 'sid': 7}})
            await s.handle(w, snap(1))
            await s.handle(w, delta(2))
            assert s.markets['A'].book.valid
        asyncio.run(live())
        assert r.health().committed_ordinal == 0
        release.set()
        h = close(r)
        assert h.coverage == 'INCOMPLETE' and h.clean_shutdown
        assert h.committed_ordinal == h.issued_ordinal
    finally:
        release.set()
        close(r)


def test_startup_error_and_bounded_shutdown(tmp_path):
    r = Recorder(tmp_path / 'missing' / 'events.db')
    wait_for(lambda: r.health().error_count > 0)
    h = close(r, timeout=0.05)
    assert h.coverage == 'INCOMPLETE' and not h.clean_shutdown
    assert h.committed_ordinal == 0
    r._thread.join(1)
    assert not r._thread.is_alive()


def test_shutdown_timeout_never_claims_clean_with_pending_tail(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = JournalStore.persist
    def blocked(store, records, recorder, **kwargs):
        entered.set()
        release.wait(3)
        return original(store, records, recorder, **kwargs)
    monkeypatch.setattr(JournalStore, 'persist', blocked)
    path = tmp_path / 'events.db'
    r = Recorder(path)
    try:
        assert entered.wait(2)
        r.offer('pending_tail')
        h = close(r, timeout=0.01)
        assert h.coverage == 'INCOMPLETE' and not h.clean_shutdown
        release.set()
        r._thread.join(2)
        assert not r._thread.is_alive()
        assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs') == [('INCOMPLETE', 0)]
    finally:
        release.set()
        r._thread.join(2)


def test_unclean_restart_preserves_old_run(tmp_path, monkeypatch):
    # An existing unfinalized run models the durable prefix left after process death.
    path = tmp_path / 'events.db'
    store = JournalStore(path)
    with store.db:
        store.db.execute("INSERT INTO recording_runs(run_id,started_utc_ns,started_monotonic_ns,coverage) VALUES ('crashed',1,1,'OPEN')")
    store.close()
    r = Recorder(path)
    h = close(r)
    assert rows(path, "SELECT coverage,clean_shutdown FROM recording_runs WHERE run_id='crashed'") == [('OPEN', 0)]
    assert h.run_id != 'crashed' and h.clean_shutdown


def test_stress_bounded_admission_and_order(tmp_path):
    path = tmp_path / 'stress.db'
    r = Recorder(path, max_records=6000, batch_size=250)
    for i in range(5000):
        r.offer('ws_received', raw=f'{{"seq":{i}}}')
    h = close(r, timeout=10)
    assert h.coverage == 'COMPLETE' and h.lost_count == 0
    assert h.issued_ordinal == h.committed_ordinal == 5002
    assert rows(path, 'SELECT count(*),min(ordinal),max(ordinal) FROM journal') == [(5002, 1, 5002)]


def test_stream_raw_before_parse_duplicate_sequences_and_rebuild(tmp_path):
    path = tmp_path / 'stream.db'
    r = Recorder(path)
    messages = [
        json.dumps({'type': 'subscribed', 'id': 1, 'msg': {'channel': 'orderbook_delta', 'sid': 7}}),
        json.dumps(snap(1)), json.dumps(snap(2, 'B')),
        json.dumps(delta(3)), json.dumps(delta(3)),  # duplicate must not be deduplicated
        json.dumps(snap(4)), json.dumps(snap(5, 'B')),
        json.dumps({'type': 'error', 'id': 1, 'msg': {'code': 10}}),
        json.dumps({'type': 'unsubscribed', 'id': 3, 'sid': 7, 'seq': 6}),
        json.dumps({'type': 'subscribed', 'id': 4, 'msg': {'channel': 'orderbook_delta', 'sid': 8}}),
        json.dumps(snap(1, sid=8)), json.dumps(snap(2, 'B', sid=8)),
        '  {malformed JSON\n',
    ]
    async def scenario():
        class Socket(Wire):
            def __init__(self):
                super().__init__()
                self.messages = iter(messages)
            async def recv(self):
                return next(self.messages)
        s, w = MarketStream(['A', 'B'], recorder=r), Socket()
        with pytest.raises(Reconnect, match='Invalid JSON'):
            await s.session(w)
        assert s.subscription_epoch == 2 and s.generation == 1
        assert all(m.book.valid for m in s.markets.values())
        assert [command['cmd'] for command in w.sent] == ['subscribe', 'update_subscription', 'unsubscribe', 'subscribe']
        return await r.close(3)
    h = asyncio.run(scenario())
    assert h.coverage == 'INCOMPLETE' and h.clean_shutdown
    saved = rows(path, "SELECT ordinal, raw, subscription_epoch FROM journal WHERE kind='ws_received' ORDER BY ordinal")
    assert [row[1] for row in saved] == messages
    assert [row[2] for row in saved[:9]] == [1] * 9
    assert [row[2] for row in saved[9:]] == [2] * 4
    assert rows(path, "SELECT count(*) FROM journal WHERE kind='ws_received' AND seq='3'") == [(2,)]
    for parent, ordinal in rows(path, "SELECT parent_ordinal,ordinal FROM journal WHERE kind='processing_result'"):
        assert parent is not None and parent < ordinal
    assert rows(path, "SELECT count(*) FROM journal WHERE kind='processing_result' AND details_json LIKE '%malformed_json%'") == [(1,)]
    assert rows(path, "SELECT count(*) FROM journal WHERE kind='integrity'")[0][0] >= 1


def test_writer_thread_ownership_and_no_disk_wait_on_admission(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = JournalStore.persist
    owners = []
    def blocked(store, records, recorder, **kwargs):
        owners.append(threading.get_ident())
        entered.set()
        release.wait(3)
        return original(store, records, recorder, **kwargs)
    monkeypatch.setattr(JournalStore, 'persist', blocked)
    r = Recorder(tmp_path / 'events.db')
    try:
        assert entered.wait(2)
        for i in range(100):
            r.offer('event', details=(('i', i),))
        assert not release.is_set() and r.health().buffered_records == 101
        assert owners and all(owner != threading.get_ident() for owner in owners)
        release.set()
        assert close(r).clean_shutdown
    finally:
        release.set()
        close(r)


def test_malformed_metadata_cannot_poison_raw_storage(tmp_path):
    path = tmp_path / 'raw.db'
    r = Recorder(path)
    messages = ['{"type":"\\ud800","msg":{"market_ticker":"\\ud800"}}', '[' * 1500]
    for raw in messages:
        r.offer('ws_received', raw=raw)
    h = close(r)
    assert h.clean_shutdown and h.error_count == 0
    assert [row[0] for row in rows(path, "SELECT raw FROM journal WHERE kind='ws_received' ORDER BY ordinal")] == messages


def test_actual_process_death_leaves_unclean_prefix(tmp_path):
    import subprocess
    import sys
    path = tmp_path / 'crashed.db'
    script = '''
import os, sys, time
from recording import Recorder
r = Recorder(sys.argv[1])
n = r.offer('ws_received', raw='exact crash-prefix message')
stop = time.monotonic() + 3
while r.health().committed_ordinal < n:
    if time.monotonic() > stop:
        raise RuntimeError('Writer failed to commit')
    time.sleep(0.001)
os._exit(0)
'''
    subprocess.run([sys.executable, '-c', script, str(path)], check=True, timeout=5)
    old = rows(path, 'SELECT run_id,coverage,clean_shutdown,committed_ordinal FROM recording_runs')[0]
    assert old[1:] == ('OPEN', 0, 2)
    assert rows(path, "SELECT raw FROM journal WHERE ordinal=2") == [('exact crash-prefix message',)]
    r = Recorder(path)
    h = close(r)
    assert h.clean_shutdown and h.run_id != old[0]
    assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs WHERE run_id=?', (old[0],)) == [('OPEN', 0)]


def test_real_database_lock_is_reported_and_retried(tmp_path):
    path = tmp_path / 'locked.db'
    store = JournalStore(path)
    store.close()
    blocker = sqlite3.connect(path)
    blocker.execute('BEGIN IMMEDIATE')
    r = Recorder(path)
    try:
        wait_for(lambda: r.health().error_count > 0)
        assert r.health().coverage == 'INCOMPLETE'
        assert r.health().committed_ordinal == 0
        blocker.rollback()
        assert close(r).clean_shutdown
    finally:
        blocker.close()
        close(r)


def test_future_schema_is_not_overwritten(tmp_path):
    path = tmp_path / 'future.db'
    with sqlite3.connect(path) as db:
        db.execute('PRAGMA user_version=999')
    r = Recorder(path)
    wait_for(lambda: r.health().error_count > 0)
    h = close(r, 0.05)
    r._thread.join(1)
    assert h.coverage == 'INCOMPLETE' and h.committed_ordinal == 0
    assert rows(path, 'PRAGMA user_version') == [(999,)]


def test_live_books_survive_rejected_recording_admission(tmp_path):
    path = tmp_path / 'overflow.db'
    r = Recorder(path, max_bytes=1)
    async def scenario():
        s, w = MarketStream(['A'], recorder=r), Wire()
        await s.start(w)
        await s.handle(w, {'type': 'subscribed', 'id': 1,
                           'msg': {'channel': 'orderbook_delta', 'sid': 7}})
        await s.handle(w, snap(1))
        await s.handle(w, delta(2))
        assert s.markets['A'].book.valid and s.last_seq == 2
        h = await r.close(3)
        assert h.coverage == 'INCOMPLETE' and h.lost_count == h.issued_ordinal
        assert h.committed_ordinal == 0 and h.clean_shutdown
    asyncio.run(scenario())
    assert rows(path, 'SELECT first_ordinal,last_ordinal FROM discontinuities') == [(1, r.health().issued_ordinal)]


class CommitGate:
    """Pause after SQL statements, immediately before the real transaction commit."""
    def __init__(self, db, entered, release, fail=False):
        self.db, self.entered, self.release, self.fail = db, entered, release, fail

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __enter__(self):
        self.db.__enter__()
        return self

    def __exit__(self, exc_type, exc, traceback):
        if exc_type is None:
            self.entered.set()
            if not self.release.wait(3):
                self.db.rollback()
                raise TimeoutError('Test did not release commit gate')
            if self.fail:
                self.db.rollback()
                raise sqlite3.OperationalError('Synthetic commit failure')
        return self.db.__exit__(exc_type, exc, traceback)


def test_final_data_commit_after_deadline_cannot_publish_complete(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = JournalStore.persist

    def persist(store, records, recorder, **kwargs):
        if kwargs.get('finish') and not isinstance(store.db, CommitGate):
            store.db = CommitGate(store.db, entered, release)
        return original(store, records, recorder, **kwargs)

    monkeypatch.setattr(JournalStore, 'persist', persist)
    path = tmp_path / 'final-commit.db'
    r = Recorder(path)

    async def scenario():
        task = asyncio.create_task(r.close(0.1))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            h = await task  # Deadline expires while the final data commit is held.
            assert h.coverage == 'INCOMPLETE' and not h.clean_shutdown
            assert h.finalization_state == 'DECIDED_INCOMPLETE'
            release.set()
            await asyncio.to_thread(r._thread.join, 2)
            assert not r._thread.is_alive()
            assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs') == [('INCOMPLETE', 0)]
            assert r.health().coverage == 'INCOMPLETE'
        finally:
            release.set()
            await task
            await asyncio.to_thread(r._thread.join, 2)
    asyncio.run(scenario())


def test_slow_success_publication_is_finalizing_not_false_timeout(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = JournalStore.publish_final

    def publish(store, recorder):
        store.db = CommitGate(store.db, entered, release)
        return original(store, recorder)

    monkeypatch.setattr(JournalStore, 'publish_final', publish)
    path = tmp_path / 'publication.db'
    r = Recorder(path)

    async def scenario():
        task = asyncio.create_task(r.close(0.1))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            h = await task
            assert h.coverage == 'FINALIZING' and not h.clean_shutdown
            assert h.finalization_state == 'DECIDED_COMPLETE'
            # Data is durable, but success metadata is still uncommitted.
            assert h.committed_ordinal == h.issued_ordinal
            assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs') == [('OPEN', 0)]
            release.set()
            await asyncio.to_thread(r._thread.join, 2)
            assert r.health().coverage == 'COMPLETE' and r.health().clean_shutdown
            assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs') == [('COMPLETE', 1)]
        finally:
            release.set()
            await task
            await asyncio.to_thread(r._thread.join, 2)
    asyncio.run(scenario())


@pytest.mark.parametrize('stage', ['data', 'publication'])
def test_commit_failure_never_retries_as_complete(tmp_path, monkeypatch, stage):
    entered, release = threading.Event(), threading.Event()
    release.set()
    method = 'persist' if stage == 'data' else 'publish_final'
    original = getattr(JournalStore, method)
    calls = []

    def fail_once(store, *args, **kwargs):
        selected = stage == 'publication' or kwargs.get('finish')
        if selected and not calls:
            calls.append(1)
            store.db = CommitGate(store.db, entered, release, fail=True)
        return original(store, *args, **kwargs)

    monkeypatch.setattr(JournalStore, method, fail_once)
    path = tmp_path / 'failure.db'
    r = Recorder(path)
    h = close(r)
    assert entered.is_set() and h.error_count >= 1
    assert h.clean_shutdown and h.coverage == 'INCOMPLETE'
    assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs') == [('INCOMPLETE', 1)]


@pytest.mark.parametrize('mode', ['nested_json', 'receiver_error'])
def test_abnormal_session_failure_is_incomplete_end_to_end(tmp_path, mode):
    path = tmp_path / 'abnormal.db'
    r = Recorder(path)
    # Well below the default WebSocket size limit, but deep enough for this runtime.
    raw = '[' * 100000

    async def scenario():
        class Socket(Wire):
            async def recv(self):
                if mode == 'receiver_error':
                    raise RuntimeError('Synthetic receiver failure')
                return raw

        class Connection:
            async def __aenter__(self):
                return Socket()
            async def __aexit__(self, *args):
                pass

        expected = RecursionError if mode == 'nested_json' else RuntimeError
        with pytest.raises(expected):
            await asyncio.wait_for(MarketStream(['A'], recorder=r).run(Connection), 2)
        return await r.close(3)

    h = asyncio.run(scenario())
    assert h.clean_shutdown and h.coverage == 'INCOMPLETE'
    assert rows(path, 'SELECT coverage FROM recording_runs') == [('INCOMPLETE',)]
    assert rows(path, "SELECT details_json FROM journal WHERE kind='abnormal_termination'") == [
        (json.dumps({'error': 'RecursionError' if mode == 'nested_json' else 'RuntimeError'}, sort_keys=True),)]
    if mode == 'nested_json':
        assert rows(path, "SELECT raw FROM journal WHERE kind='ws_received'") == [(raw,)]
        assert rows(path, "SELECT count(*) FROM journal WHERE kind='processing_result' AND parent_ordinal IS NOT NULL") == [(1,)]


def test_intentional_receive_cancellation_can_drain_cleanly(tmp_path):
    path = tmp_path / 'cancelled.db'
    r = Recorder(path)

    async def scenario():
        entered = asyncio.Event()
        class Socket(Wire):
            async def recv(self):
                entered.set()
                await asyncio.Event().wait()
        class Connection:
            async def __aenter__(self):
                return Socket()
            async def __aexit__(self, *args):
                pass
        stream = MarketStream(['A'], recorder=r)
        task = asyncio.create_task(stream.run(Connection))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert all(row[2] is None for row in stream.view())
        return await r.close(3)

    h = asyncio.run(scenario())
    assert h.coverage == 'COMPLETE' and h.clean_shutdown
    assert rows(path, "SELECT count(*) FROM journal WHERE kind='session_cancelled'") == [(1,)]


def test_recording_artifacts_are_git_ignored_but_sources_are_not():
    import subprocess
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    artifacts = ['rainmaker-events.sqlite', 'rainmaker-events.sqlite-wal',
                 'rainmaker-events.sqlite-shm', 'data/events.sqlite3',
                 'data/events.sqlite3-wal', 'data/events.sqlite3-shm',
                 'events.db', 'events.db-wal', 'events.db-shm', 'recordings/run/events.bin']
    result = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin'],
                            input='\n'.join(artifacts) + '\n', text=True, capture_output=True,
                            cwd=root, check=True)
    assert set(result.stdout.splitlines()) == set(artifacts)
    sources = ['recording.py', 'storage.py', 'migrations/001_recording.sql', 'schema.sql']
    result = subprocess.run(['git', 'check-ignore', '--no-index', '--stdin'],
                            input='\n'.join(sources) + '\n', text=True, capture_output=True, cwd=root)
    assert result.returncode == 1 and not result.stdout


def test_publication_commit_failure_after_deadline_stays_unverified(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    original = JournalStore.publish_final
    calls = []

    def publish(store, recorder):
        calls.append(1)
        store.db = CommitGate(store.db, entered, release, fail=True)
        return original(store, recorder)

    monkeypatch.setattr(JournalStore, 'publish_final', publish)
    path = tmp_path / 'late-publication-failure.db'
    r = Recorder(path)

    async def scenario():
        task = asyncio.create_task(r.close(0.1))
        try:
            assert await asyncio.to_thread(entered.wait, 1)
            h = await task  # Let the deadline expire before failing the pending commit.
            assert h.coverage == 'FINALIZING' and not h.clean_shutdown
            assert h.committed_ordinal == h.issued_ordinal
            assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs') == [('OPEN', 0)]
            release.set()
            await asyncio.to_thread(r._thread.join, 2)
            assert not r._thread.is_alive()
            h = r.health()
            assert h.coverage == 'INCOMPLETE' and not h.clean_shutdown
            assert h.error_count == 1 and h.last_error == 'OperationalError'
            assert rows(path, 'SELECT coverage,clean_shutdown FROM recording_runs') == [('OPEN', 0)]
            assert len(calls) == 1  # No later COMPLETE retry after the deadline.
        finally:
            release.set()
            await task
            await asyncio.to_thread(r._thread.join, 2)
    asyncio.run(scenario())
