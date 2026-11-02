# Rainmaker — Market Data Milestone 1

Read-only Python starter: discover contracts via REST, monitor one contract via authenticated WebSocket, maintain a local order book in RAM, invalidate and reconnect on lost connection or sequence gap.

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

The monitor starts with no valid order book, requires a snapshot, applies price-level deltas, checks subscription sequence numbers, and reconnects from a new snapshot when a gap or disconnection is detected. Prices remain read-only. It does not place orders or make forecast/trading decisions.

Run tests:

```bash
pytest -q
```

## Known limitations / next steps

- This is a terminal monitor, not yet a React/web UI.
- No persistent storage or message replay yet.
- Only one contract per stream; expand after verifying reliability.
- No stale-time timeout while socket remains connected but book has no changes; add separate connection heartbeat/freshness tracking before trading.
- Monitor terminal output is event driven, not a measured end-to-end latency dashboard.
- Add bounded log queues, periodic reference snapshots, and detailed sequence/recovery tests before serious operation.
- Live order-book data requires authenticated WebSockets; REST market discovery is public.
