import asyncio
import json
from decimal import Decimal
import pytest
from market_stream import MarketStream, Reconnect, FatalStreamError


class Wire:
    def __init__(self):
        self.sent = []
    async def send(self, raw):
        self.sent.append(json.loads(raw))


def snap(seq, ticker='A', sid=7):
    return {'type': 'orderbook_snapshot', 'sid': sid, 'seq': seq,
            'msg': {'market_ticker': ticker, 'yes_dollars_fp': [['.4', '2']], 'no_dollars_fp': []}}


def delta(seq, ticker='A', amount='1'):
    return {'type': 'orderbook_delta', 'sid': 7, 'seq': seq,
            'msg': {'market_ticker': ticker, 'side': 'yes', 'price_dollars': '.4', 'delta_fp': amount}}


async def ready(clock=lambda: 0):
    s, w = MarketStream(['A', 'B'], clock=clock), Wire()
    await s.start(w)
    await s.handle(w, {'type': 'subscribed', 'id': w.sent[-1]['id'], 'msg': {'channel': 'orderbook_delta', 'sid': 7}})
    await s.handle(w, snap(1))
    await s.handle(w, snap(2, 'B'))
    return s, w


def test_interleaving_control_and_targeted_recovery():
    async def scenario():
        s, w = await ready()
        await s.handle(w, delta(3))
        await s.handle(w, {'type': 'ok', 'sid': 7, 'seq': 4})
        await s.handle(w, delta(5, 'B'))
        await s.handle(w, delta(6, amount='-100'))
        assert not s.markets['A'].book.valid
        assert s.markets['B'].book.valid
        assert w.sent[-1]['params']['market_tickers'] == ['A']
        await s.handle(w, delta(7, amount='100'))
        await s.handle(w, delta(8, 'B'))
        await s.handle(w, snap(9))
        await s.handle(w, delta(10))
        assert s.markets['A'].book.yes[Decimal('.4')] == 3
        assert s.markets['B'].book.yes[Decimal('.4')] == 4
    asyncio.run(scenario())


@pytest.mark.parametrize('seq', [2, 1, 4])
def test_shared_sequence_anomalies(seq):
    async def scenario():
        s, w = await ready()
        await s.handle(w, delta(seq))
        assert all(not m.book.valid for m in s.markets.values())
        assert set(w.sent[-1]['params']['market_tickers']) == {'A', 'B'}
        await s.handle(w, snap(seq + 1))
        await s.handle(w, snap(seq + 2, 'B'))
        assert all(m.book.valid for m in s.markets.values())
    asyncio.run(scenario())


def test_timeout_rebuild_then_connection_fallback():
    async def scenario():
        now = [0]
        s, w = await ready(lambda: now[0])
        await s.handle(w, delta(3, amount='-100'))
        now[0] = 11
        await s.check_timeouts(w)
        assert w.sent[-1]['cmd'] == 'unsubscribe'
        now[0] = 22
        with pytest.raises(Reconnect):
            await s.check_timeouts(w)
    asyncio.run(scenario())


def test_rebuild_ack_and_new_sid():
    async def scenario():
        s, w = await ready()
        await s.rebuild(w, 'test')
        await s.handle(w, {'type': 'unsubscribed', 'id': w.sent[-1]['id'], 'sid': 7, 'seq': 3})
        await s.handle(w, {'type': 'subscribed', 'id': w.sent[-1]['id'], 'msg': {'channel': 'orderbook_delta', 'sid': 8}})
        await s.handle(w, snap(100, sid=7))
        assert not s.markets['A'].book.valid
        await s.handle(w, snap(1, sid=8))
        await s.handle(w, snap(2, 'B', sid=8))
        assert all(m.book.valid for m in s.markets.values())
    asyncio.run(scenario())


@pytest.mark.parametrize('code,exception', [(9, FatalStreamError), (13, None), (10, None), (25, None)])
def test_errors(code, exception):
    async def scenario():
        s, w = await ready()
        await s.handle(w, delta(3, amount='-100'))
        event = {'type': 'error', 'id': w.sent[-1]['id'], 'msg': {'code': code}}
        if exception:
            with pytest.raises(exception):
                await s.handle(w, event)
        else:
            await s.handle(w, event)
            assert s.phase == 'UNSUBSCRIBING'
            assert all(not m.book.valid for m in s.markets.values())
    asyncio.run(scenario())


def test_initial_deadlines_and_failed_rebuild():
    async def scenario():
        now = [0]
        s, w = MarketStream(['A'], clock=lambda: now[0]), Wire()
        await s.start(w)
        now[0] = 11
        with pytest.raises(Reconnect):
            await s.check_timeouts(w)
        s, w = await ready(lambda: now[0])
        await s.rebuild(w, 'test')
        await s.handle(w, {'type': 'unsubscribed', 'id': w.sent[-1]['id'], 'sid': 7, 'seq': 3})
        await s.handle(w, {'type': 'subscribed', 'id': w.sent[-1]['id'], 'msg': {'channel': 'orderbook_delta', 'sid': 8}})
        now[0] = 22
        with pytest.raises(Reconnect):
            await s.check_timeouts(w)
    asyncio.run(scenario())


def test_second_gap_during_snapshot_recovery_rebuilds():
    async def scenario():
        s, w = await ready()
        await s.handle(w, delta(4))
        await s.handle(w, snap(6))
        assert s.phase == 'UNSUBSCRIBING'
        assert all(not m.book.valid for m in s.markets.values())
    asyncio.run(scenario())


def test_unknown_ticker_sequence_consumed_and_full_recovery():
    async def scenario():
        s, w = await ready()
        await s.handle(w, delta(3, 'UNKNOWN'))
        assert s.last_seq == 3
        assert s.awaiting == {'A', 'B'}
    asyncio.run(scenario())


def test_quiet_market_does_not_timeout():
    async def scenario():
        now = [0]
        s, w = await ready(lambda: now[0])
        now[0] = 1000
        await s.check_timeouts(w)
        assert all(row[2] is not None and row[3] == 1000 for row in s.view())
    asyncio.run(scenario())


def test_backoff_and_cancellation_without_network():
    async def scenario():
        attempts, waits = [], []
        s = MarketStream(['A'], jitter=lambda low, high: high)
        class FailedConnection:
            async def __aenter__(self):
                attempts.append(1)
                raise OSError('synthetic')
            async def __aexit__(self, *args):
                pass
        async def sleep(delay):
            waits.append(delay)
            if len(waits) == 7:
                raise asyncio.CancelledError
        s.sleep = sleep
        with pytest.raises(asyncio.CancelledError):
            await s.run(FailedConnection)
        assert waits == [1, 2, 4, 8, 16, 30, 30]
        assert len(attempts) == 7
        assert s.view()[0][2] is None
    asyncio.run(scenario())


def test_missing_identity_and_malformed_market():
    async def scenario():
        s, w = await ready()
        event = delta(3)
        del event['sid']
        with pytest.raises(Reconnect):
            await s.handle(w, event)
        event = delta(3)
        event['msg']['market_ticker'] = []
        await s.handle(w, event)
        assert all(not m.book.valid for m in s.markets.values())
    asyncio.run(scenario())


@pytest.mark.parametrize('code', [10, 25])
@pytest.mark.parametrize('during_recovery', [False, True])
def test_established_subscription_errors_rebuild_all_books(code, during_recovery):
    async def scenario():
        s, w = await ready()
        subscribe_id = w.sent[0]['id']
        if during_recovery:
            await s.handle(w, delta(3, amount='-100'))
            assert s.markets['B'].book.valid
        await s.handle(w, {'type': 'error', 'id': subscribe_id, 'msg': {'code': code}})
        assert all(row[2] is None for row in s.view())
        assert s.phase == 'UNSUBSCRIBING'
        unsubscribe_id = w.sent[-1]['id']
        assert w.sent[-1]['cmd'] == 'unsubscribe'
        seq = 4 if during_recovery else 3
        await s.handle(w, {'type': 'unsubscribed', 'id': unsubscribe_id, 'sid': 7, 'seq': seq})
        new_id = w.sent[-1]['id']
        await s.handle(w, {'type': 'subscribed', 'id': new_id,
                           'msg': {'channel': 'orderbook_delta', 'sid': 8}})
        # Errors associated with the retired subscription must not poison the new one.
        await s.handle(w, {'type': 'error', 'id': subscribe_id, 'msg': {'code': code}})
        await s.handle(w, snap(1, sid=8))
        await s.handle(w, snap(2, 'B', sid=8))
        assert all(row[2] is not None for row in s.view())
    asyncio.run(scenario())


@pytest.mark.parametrize('cancel', [False, True])
def test_quotes_invalid_before_blocked_connection_exit(cancel):
    async def scenario():
        s, w = await ready()
        active = asyncio.Event()
        closing = asyncio.Event()
        release = asyncio.Event()

        class Connection:
            async def __aenter__(self):
                return w
            async def __aexit__(self, *args):
                closing.set()
                await release.wait()

        async def session(ws):
            active.set()
            if cancel:
                await asyncio.Event().wait()
            raise Reconnect('Synthetic session failure')

        s.session = session
        task = asyncio.create_task(s.run(Connection))
        try:
            await asyncio.wait_for(active.wait(), 1)
            if cancel:
                task.cancel()
            await asyncio.wait_for(closing.wait(), 1)
            assert not task.done()
            assert s.connection_status != 'CONNECTED'
            assert all(row[2] is None for row in s.view())
            for market in s.markets.values():
                with pytest.raises(ValueError):
                    market.book.quote()
        finally:
            task.cancel()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
    asyncio.run(scenario())
