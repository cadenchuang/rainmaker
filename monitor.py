"""Read-only live order book. Invalidates state and reconnects on gaps/errors."""
import argparse
import asyncio
import base64
import json
import os
import time
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from dotenv import load_dotenv
from rich.console import Console

WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
CONSOLE = Console()


def auth_headers() -> dict[str, str]:
    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        raise RuntimeError("Set KALSHI_KEY_ID and KALSHI_PRIVATE_KEY_PATH in .env")
    private = serialization.load_pem_private_key(Path(key_path).expanduser().read_bytes(), password=None)
    timestamp = str(int(time.time() * 1000))
    signed_text = (timestamp + "GET" + "/trade-api/ws/v2").encode()
    if isinstance(private, ed25519.Ed25519PrivateKey):
        signature = private.sign(signed_text)
    elif isinstance(private, rsa.RSAPrivateKey):
        signature = private.sign(signed_text, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                          salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        raise RuntimeError("Unsupported private key type")
    return {"KALSHI-ACCESS-KEY": key_id,
            "KALSHI-ACCESS-TIMESTAMP": timestamp,
            "KALSHI-ACCESS-SIGNATURE": base64.b64encode(signature).decode()}


@dataclass
class Book:
    yes: dict[Decimal, Decimal] = field(default_factory=dict)
    no: dict[Decimal, Decimal] = field(default_factory=dict)
    valid: bool = False
    last_seq: int | None = None
    last_update: float | None = None

    def snapshot(self, msg: dict) -> None:
        payload = msg["msg"]
        self.yes = {Decimal(p): Decimal(q) for p, q in payload["yes_dollars_fp"]}
        self.no = {Decimal(p): Decimal(q) for p, q in payload["no_dollars_fp"]}
        self.last_seq = msg["seq"]
        self.last_update = time.monotonic()
        self.valid = True

    def delta(self, msg: dict) -> None:
        if not self.valid or self.last_seq is None or msg["seq"] != self.last_seq + 1:
            self.valid = False
            raise ValueError("Orderbook sequence gap or no snapshot")
        payload = msg["msg"]
        levels = self.yes if payload["side"] == "yes" else self.no
        price = Decimal(payload["price_dollars"])
        quantity = levels.get(price, Decimal(0)) + Decimal(payload["delta_fp"])
        if quantity < 0:
            self.valid = False
            raise ValueError("Negative orderbook quantity")
        if quantity == 0:
            levels.pop(price, None)
        else:
            levels[price] = quantity
        self.last_seq = msg["seq"]
        self.last_update = time.monotonic()

    def top(self) -> tuple[str, str, str, str]:
        # Kalshi broadcasts bid levels for YES and NO; opposite bids imply asks.
        yes_bid = max(self.yes, default=None)
        no_bid = max(self.no, default=None)
        yes_ask = 1 - no_bid if no_bid is not None else None
        no_ask = 1 - yes_bid if yes_bid is not None else None
        return tuple(str(x) if x is not None else "—" for x in (yes_bid, yes_ask, no_bid, no_ask))


async def run(ticker: str) -> None:
    load_dotenv()
    delay = 1
    while True:
        book = Book()  # Never trust the old book after disconnect.
        try:
            async with websockets.connect(WS, additional_headers=auth_headers(), ping_interval=20, ping_timeout=20) as ws:
                await ws.send(json.dumps({"id": 1, "cmd": "subscribe", "params": {"channels": ["orderbook_delta"], "market_ticker": ticker}}))
                CONSOLE.print(f"[green]CONNECTED[/green] — {ticker}; waiting for full snapshot")
                delay = 1
                async for raw in ws:
                    event = json.loads(raw)
                    kind = event.get("type")
                    if kind == "error":
                        raise RuntimeError(f"Kalshi subscription error: {event.get('msg')}")
                    if kind not in ("orderbook_snapshot", "orderbook_delta"):
                        continue
                    if event.get("msg", {}).get("market_ticker") != ticker:
                        continue
                    if kind == "orderbook_snapshot":
                        book.snapshot(event)
                    else:
                        book.delta(event)
                    yb, ya, nb, na = book.top()
                    CONSOLE.print(f"YES bid/ask {yb}/{ya} | NO bid/ask {nb}/{na} | seq={book.last_seq} | [green]VALID[/green]")
        except (Exception, KeyboardInterrupt) as exc:
            if isinstance(exc, KeyboardInterrupt):
                return
            CONSOLE.print(f"[red]STALE / DISCONNECTED[/red] ({type(exc).__name__}: {exc}). Reconnecting in {delay}s; no trading allowed.")
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker", help="Exact market ticker returned by discover.py")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.ticker))
    except KeyboardInterrupt:
        CONSOLE.print("Stopped")
