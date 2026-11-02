"""Real local WebSocket framing and reconnects; no credentials or external service."""
import asyncio
import json
import pytest
from websockets.asyncio.server import serve
from websockets.asyncio.client import connect
from market_stream import MarketStream
from test_market_stream import snap, delta


@pytest.mark.parametrize('abrupt', [False, True])
def test_local_recovery_and_reconnect(abrupt):
    async def scenario():
        finished = asyncio.Event()
        sessions = []
        sleeps = []
        stream = MarketStream(['A', 'B'])

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
                # Confirm processing before closing; this waits on local state only.
                while not all(m.book.valid for m in stream.markets.values()):
                    await asyncio.sleep(0)
                if abrupt:
                    ws.transport.abort()
                else:
                    await ws.close()
            else:
                while not all(m.book.valid for m in stream.markets.values()):
                    await asyncio.sleep(0)
                finished.set()
                await ws.wait_closed()

        async def sleep(delay):
            sleeps.append(delay)
            assert all(not m.book.valid for m in stream.markets.values())
            await asyncio.sleep(0)

        stream.sleep = sleep
        stream.jitter = lambda low, high: high
        async with serve(server, '127.0.0.1', 0) as listener:
            port = listener.sockets[0].getsockname()[1]
            task = asyncio.create_task(stream.run(lambda: connect(f'ws://127.0.0.1:{port}')))
            try:
                await asyncio.wait_for(finished.wait(), 5)
                assert stream.generation == 2
                assert sleeps == [1]
                assert all(row[2] is not None for row in stream.view())
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                assert all(row[2] is None for row in stream.view())
    asyncio.run(scenario())
