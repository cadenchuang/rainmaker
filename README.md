# Rainmaker — Market Data Milestone 1

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

- Read-only terminal monitor; no orders, database, dashboard, or forecasts.
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
