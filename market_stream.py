"""One owner, one real-time subscription, many independently synchronized books."""
import asyncio
import json
import random
import time
from dataclasses import dataclass, field

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake
from orderbook import Book, BookError


class Reconnect(Exception):
    """Session cannot safely recover in place."""


class FatalStreamError(Exception):
    """Configuration, authorization, or unsupported client command."""


@dataclass
class MarketState:
    book: Book = field(default_factory=Book)
    status: str = "DISCONNECTED"
    last_event_at: float | None = None
    last_snapshot_at: float | None = None
    reason: str = ""


class MarketStream:
    def __init__(self, tickers, *, clock=time.monotonic, timeout=10, stable_after=30,
                 sleep=asyncio.sleep, jitter=random.uniform):
        self.markets = {t: MarketState() for t in tickers}
        if not self.markets:
            raise ValueError("At least one market is required")
        if timeout <= 0:
            raise ValueError("Timeout must be positive")
        self.clock, self.timeout = clock, timeout
        self.sleep, self.jitter, self.stable_after = sleep, jitter, stable_after
        self.next_id = 0
        self.generation = 0
        self.sid = None
        self.last_seq = None
        self.phase = "DISCONNECTED"
        self.pending = None  # (id, operation, deadline); mutations are serialized
        self.awaiting = set()
        self.snapshot_deadline = None
        self.live_since = None
        self.connection_status = "DISCONNECTED"
        self.last_received_at = None
        self.last_error = ""
        self.snapshot_id = None
        self.active_subscribe_id = None
        self.rebuilt = False

    def invalidate(self, tickers, reason):
        for ticker in tickers:
            state = self.markets[ticker]
            state.book.invalidate()
            state.status, state.reason = "RECOVERING", reason
        self.live_since = None

    async def command(self, ws, operation, params):
        self.next_id += 1
        self.pending = (self.next_id, operation, self.clock() + self.timeout)
        await asyncio.wait_for(
            ws.send(json.dumps({"id": self.next_id, "cmd": operation, "params": params})),
            timeout=self.timeout,
        )

    async def subscribe(self, ws):
        self.active_subscribe_id = None
        self.sid, self.last_seq = None, None
        self.phase = "SUBSCRIBING"
        self.awaiting = set(self.markets)
        self.snapshot_deadline = None
        await self.command(ws, "subscribe", {"channels": ["orderbook_delta"],
                                             "market_tickers": list(self.markets)})

    async def start(self, ws):
        self.rebuilt = False
        self.snapshot_id = None
        self.generation += 1
        self.connection_status = "CONNECTED"
        self.invalidate(self.markets, "New connection")
        await self.subscribe(ws)

    async def rebuild(self, ws, reason):
        self.invalidate(self.markets, reason)
        if self.rebuilt:
            raise Reconnect("Subscription rebuild failed")
        self.rebuilt = True
        self.snapshot_id = None
        self.awaiting.clear()
        self.snapshot_deadline = None
        if self.sid is None or self.phase == "UNSUBSCRIBING":
            raise Reconnect(reason)
        self.phase = "UNSUBSCRIBING"
        await self.command(ws, "unsubscribe", {"sids": [self.sid]})

    async def recover(self, ws, tickers, reason):
        self.invalidate(tickers, reason)
        if self.awaiting or self.pending:
            # No ambiguous overlapping snapshot requests on one subscription.
            await self.rebuild(ws, reason)
            return
        self.awaiting = set(tickers)
        self.snapshot_deadline = self.clock() + self.timeout
        await self.command(ws, "update_subscription", {"sid": self.sid,
                           "action": "get_snapshot", "market_tickers": sorted(tickers)})
        # Completion is snapshots, not an undocumented get_snapshot ACK.
        self.snapshot_id = self.pending[0]
        self.pending = None

    async def check_timeouts(self, ws):
        now = self.clock()
        if self.pending and now >= self.pending[2]:
            if self.phase == "UNSUBSCRIBING":
                raise Reconnect("Unsubscribe confirmation timeout")
            if self.sid is None:
                raise Reconnect("Subscribe confirmation timeout")
            await self.rebuild(ws, "Command timeout")
        elif self.snapshot_deadline is not None and now >= self.snapshot_deadline:
            await self.rebuild(ws, "Snapshot timeout")

    async def handle(self, ws, event):
        if not isinstance(event, dict):
            raise Reconnect("Malformed envelope")
        self.last_received_at = self.clock()
        kind, payload = event.get("type"), event.get("msg", {})
        if not isinstance(payload, dict):
            raise Reconnect("Malformed payload")
        if kind == "subscribed":
            if not self.pending or self.pending[1] != "subscribe" or event.get("id") != self.pending[0]:
                return
            if payload.get("channel") != "orderbook_delta" or type(payload.get("sid")) is not int:
                raise Reconnect("Invalid subscription acknowledgment")
            self.sid, self.last_seq = payload["sid"], None
            self.active_subscribe_id = self.pending[0]
            self.pending = None
            self.phase = "ACTIVE"
            self.snapshot_deadline = self.clock() + self.timeout
            return
        if kind == "error":
            request_id = event.get("id")
            # Subscription failures can refer to the original subscribe command
            # long after its acknowledgment. Pending commands alone are insufficient.
            identities = (self.active_subscribe_id, self.snapshot_id,
                          self.pending[0] if self.pending else None)
            if request_id is not None and request_id not in identities:
                return
            code = payload.get("code")
            if code in (1, 2, 3, 4, 5, 8, 9, 11, 12, 14, 15):
                raise FatalStreamError(f"Kalshi command rejected (code {code})")
            await self.rebuild(ws, f"Kalshi subscription error (code {code})")
            return
        if kind in ("orderbook_snapshot", "orderbook_delta") and type(event.get("sid")) is not int:
            raise Reconnect("Missing subscription identity")
        if event.get("sid") != self.sid or self.sid is None:
            return  # Old subscription, or a response from another channel.
        # Includes sequence-bearing ok/unsubscribed responses, before ticker routing.
        seq = event.get("seq")
        if type(seq) is not int:
            raise Reconnect("Missing subscription sequence")
        if self.last_seq is not None and seq != self.last_seq + 1:
            self.last_seq = seq
            if self.phase == "UNSUBSCRIBING":
                raise Reconnect("Sequence failure while rebuilding")
            await self.recover(ws, set(self.markets), "Subscription sequence failure")
            return
        self.last_seq = seq
        if kind == "unsubscribed":
            if self.phase != "UNSUBSCRIBING" or not self.pending or event.get("id") != self.pending[0]:
                raise Reconnect("Unexpected unsubscribe")
            self.pending = None
            await self.subscribe(ws)
            return
        if self.phase != "ACTIVE":
            return
        if kind == "ok":
            return
        if kind not in ("orderbook_snapshot", "orderbook_delta"):
            raise Reconnect("Unknown sequenced message")
        ticker = payload.get("market_ticker")
        if not isinstance(ticker, str) or ticker not in self.markets:
            await self.recover(ws, set(self.markets), "Unknown market in subscription")
            return
        state = self.markets[ticker]
        if kind == "orderbook_delta" and not state.book.valid:
            return
        try:
            if kind == "orderbook_snapshot":
                state.book.replace_snapshot(payload)
                state.last_snapshot_at = self.clock()
                self.awaiting.discard(ticker)
            else:
                state.book.apply_delta(payload)
        except BookError:
            await self.recover(ws, {ticker}, "Corrupt book data")
            return
        state.last_event_at = self.clock()
        state.status, state.reason = "LIVE", ""
        if not self.awaiting:
            self.snapshot_deadline = None
            self.snapshot_id = None
        if all(m.book.valid for m in self.markets.values()) and self.live_since is None:
            self.live_since = self.clock()

    def view(self):
        now = self.clock()
        return tuple((ticker, state.status, state.book.top() if state.book.valid else None,
                      None if state.last_event_at is None else now - state.last_event_at,
                      state.reason) for ticker, state in self.markets.items())

    async def session(self, ws):
        await self.start(ws)
        while True:
            await self.check_timeouts(ws)
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(0.5, self.timeout))
            except asyncio.TimeoutError:
                continue
            try:
                event = json.loads(raw)
            except (ValueError, TypeError):
                raise Reconnect("Invalid JSON") from None
            await self.handle(ws, event)

    async def run(self, connector):
        delay = 1
        while True:
            stable = False
            try:
                async with connector() as ws:
                    try:
                        await self.session(ws)
                    finally:
                        # No await before invalidation: __aexit__ may block while
                        # completing the closing handshake, including on cancellation.
                        stable = (self.live_since is not None and
                                  self.clock() - self.live_since >= self.stable_after)
                        self.invalidate(self.markets, "Connection closing")
                        self.connection_status = self.phase = "CLOSING"
            except InvalidHandshake as exc:
                status = getattr(getattr(exc, "response", None), "status_code", None)
                if status in (401, 403):
                    raise FatalStreamError("WebSocket authentication rejected") from None
                self.last_error = type(exc).__name__
            except (Reconnect, ConnectionClosed, OSError, TimeoutError) as exc:
                self.last_error = str(exc) if isinstance(exc, Reconnect) else type(exc).__name__
            finally:
                self.invalidate(self.markets, "Connection closed")
                self.connection_status = self.phase = "DISCONNECTED"
                self.pending = None
                self.awaiting.clear()
                self.snapshot_deadline = None
                self.sid, self.last_seq = None, None
                self.active_subscribe_id = None
                self.snapshot_id = None
            if stable:
                delay = 1
            await self.sleep(self.jitter(delay * 0.8, delay))
            delay = min(delay * 2, 30)
