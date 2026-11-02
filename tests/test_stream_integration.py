"""Real local WebSocket framing and reconnects; no credentials or external service."""
import asyncio
import json
from contextlib import asynccontextmanager
import pytest
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from recording import Recorder
import sqlite3
from market_stream import MarketStream
from test_market_stream import snap, delta


@asynccontextmanager
async def synthetic_server(handler, *, handler_timeout=2):
    """Bound handlers, propagate their errors, and cancel them before server teardown."""
    failure = asyncio.get_running_loop().create_future()
    handlers = set()

    async def supervised(ws):
        task = asyncio.current_task()
        handlers.add(task)
        try:
            await asyncio.wait_for(handler(ws), handler_timeout)
        except Exception as exc:
            if not failure.done():
                failure.set_result(exc)
        finally:
            handlers.discard(task)

    listener = await serve(supervised, '127.0.0.1', 0, close_timeout=0.2)
    try:
        port = listener.sockets[0].getsockname()[1]
        yield f'ws://127.0.0.1:{port}', failure
        if failure.done():
            raise failure.result()
    finally:
        listener.close()
        tasks = tuple(handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.wait_for(listener.wait_closed(), 2)


async def wait_for_completion(finished, failure, client):
    """A failed handler/client must win over a generic scenario timeout."""
    completed = asyncio.create_task(finished.wait())
    try:
        done, _ = await asyncio.wait({completed, failure, client}, timeout=5,
                                     return_when=asyncio.FIRST_COMPLETED)
        if failure in done:
            raise failure.result()
        if client in done:
            client.result()
            raise AssertionError('Client exited before scenario completion')
        if completed not in done:
            raise TimeoutError('Integration scenario did not finish')
    finally:
        completed.cancel()
        await asyncio.gather(completed, return_exceptions=True)


async def wait_for_sync(stream, ws, *, timeout=1):
    async def poll():
        while not all(m.book.valid for m in stream.markets.values()):
            if ws.close_code is not None:
                raise ConnectionError('Connection closed before synchronization')
            await asyncio.sleep(0)
    await asyncio.wait_for(poll(), timeout)


@pytest.mark.parametrize('recording', [False, True])
@pytest.mark.parametrize('abrupt', [False, True])
def test_local_recovery_and_reconnect(abrupt, recording, tmp_path):
    async def scenario():
        finished = asyncio.Event()
        sessions = []
        sleeps = []
        recorder = Recorder(tmp_path / 'events.db') if recording else None
        stream = MarketStream(['A', 'B'], recorder=recorder)

        async def send(ws, event):
            await ws.send(json.dumps(event))

        async def server(ws):
            sessions.append(ws)
            command = json.loads(await ws.recv())
            assert command['params']['market_tickers'] == ['A', 'B']
            await send(ws, {'id': command['id'], 'type': 'subscribed',
                            'msg': {'channel': 'orderbook_delta', 'sid': 7}})
            await send(ws, snap(1))
            await send(ws, snap(2, 'B'))
            if len(sessions) == 1:
                await send(ws, delta(3, amount='-100'))
                command = json.loads(await ws.recv())
                assert command['params']['action'] == 'get_snapshot'
                assert command['params']['market_tickers'] == ['A']
                assert stream.markets['B'].book.valid
                assert not stream.markets['A'].book.valid
                await send(ws, {'type': 'ok', 'sid': 7, 'seq': 4, 'id': command['id']})
                await send(ws, delta(5, 'B'))
                await send(ws, snap(6))
                await send(ws, delta(8, 'B'))  # shared gap
                command = json.loads(await ws.recv())
                assert set(command['params']['market_tickers']) == {'A', 'B'}
                assert all(not m.book.valid for m in stream.markets.values())
                await send(ws, snap(9))
                await send(ws, snap(10, 'B'))
                await wait_for_sync(stream, ws)
                if abrupt:
                    ws.transport.abort()
                else:
                    await ws.close()
            else:
                await wait_for_sync(stream, ws)
                finished.set()
                await ws.wait_closed()

        async def sleep(delay):
            sleeps.append(delay)
            assert all(not m.book.valid for m in stream.markets.values())
            await asyncio.sleep(0)

        stream.sleep = sleep
        stream.jitter = lambda low, high: high
        async with synthetic_server(server) as (url, failure):
            task = asyncio.create_task(stream.run(lambda: connect(url)))
            try:
                await wait_for_completion(finished, failure, task)
                assert stream.generation == 2
                assert sleeps == [1]
                assert all(row[2] is not None for row in stream.view())
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert all(row[2] is None for row in stream.view())
                if recorder is not None:
                    health = await recorder.close(3)
                    assert health.clean_shutdown and health.coverage == 'INCOMPLETE'
                    with sqlite3.connect(tmp_path / 'events.db') as db:
                        generations = db.execute("SELECT DISTINCT generation FROM journal WHERE kind='ws_received' ORDER BY generation").fetchall()
                        assert generations == [(1,), (2,)]
                        assert db.execute("SELECT count(*) FROM journal WHERE kind='connection_closed'").fetchone()[0] == 2
    asyncio.run(scenario())


@pytest.mark.parametrize('mode', ['assertion', 'missing_snapshot', 'handler_timeout'])
def test_server_failures_propagate_and_shutdown(mode):
    async def scenario():
        handler_entered = asyncio.Event()
        handler_stopped = asyncio.Event()

        async def server(ws):
            handler_entered.set()
            try:
                if mode == 'assertion':
                    raise AssertionError('Synthetic handler failure')
                if mode == 'missing_snapshot':
                    # A permanently invalid book used to leave this server spinning.
                    await wait_for_sync(MarketStream(['A']), ws, timeout=0.01)
                else:
                    await asyncio.Event().wait()
            finally:
                handler_stopped.set()

        expected = AssertionError if mode == 'assertion' else TimeoutError
        with pytest.raises(expected):
            async with synthetic_server(server, handler_timeout=0.1) as (url, failure):
                async with connect(url):
                    await asyncio.wait_for(handler_entered.wait(), 1)
                    idle_client = asyncio.create_task(asyncio.Event().wait())
                    try:
                        await wait_for_completion(asyncio.Event(), failure, idle_client)
                    finally:
                        idle_client.cancel()
                        await asyncio.gather(idle_client, return_exceptions=True)
        assert handler_stopped.is_set()
    asyncio.run(scenario())


def test_server_shutdown_cancels_blocked_handler():
    async def scenario():
        entered, stopped = asyncio.Event(), asyncio.Event()

        async def server(ws):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        async with synthetic_server(server) as (url, failure):
            async with connect(url):
                await asyncio.wait_for(entered.wait(), 1)
        assert stopped.is_set()
        assert not failure.done()
    asyncio.run(scenario())
