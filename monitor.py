"""Read-only live order book. Invalidates state and reconnects on gaps/errors."""
import argparse
import asyncio
import base64
import os
import time
from pathlib import Path

import websockets
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from dotenv import load_dotenv
from rich.console import Console
from market_stream import MarketStream, FatalStreamError

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


async def render(stream):
    while True:
        rows = stream.view()  # Immutable copy, rendered outside the event loop.
        text = "\n".join(
            f"{ticker} | {status} | {quote or 'unavailable'} | "
            f"book age={age:.1f}s | {reason}" if age is not None else
            f"{ticker} | {status} | unavailable | {reason}"
            for ticker, status, quote, age, reason in rows
        )
        header = f"{stream.connection_status} | seq={stream.last_seq} | {stream.last_error}"
        await asyncio.to_thread(CONSOLE.print, header + "\n" + text, markup=False)
        await asyncio.sleep(1)


async def run(ticker: str | list[str]) -> None:
    load_dotenv()
    stream = MarketStream([ticker] if isinstance(ticker, str) else ticker)

    def connect():
        try:
            headers = auth_headers()
        except (OSError, ValueError, RuntimeError, TypeError):
            raise FatalStreamError("Cannot load signing configuration") from None
        return websockets.connect(WS, additional_headers=headers,
                                  ping_interval=20, ping_timeout=20, open_timeout=10)

    reporter = asyncio.create_task(render(stream))
    try:
        await stream.run(connect)
    finally:
        reporter.cancel()
        try:
            await reporter
        except asyncio.CancelledError:
            pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker", nargs="+", help="Exact market ticker returned by discover.py")
    args = parser.parse_args()
    try:
        asyncio.run(run(args.ticker))
    except KeyboardInterrupt:
        CONSOLE.print("Stopped")
    except (FatalStreamError, RuntimeError, OSError, ValueError):
        CONSOLE.print("Monitor stopped: check configuration and authorization.")
        raise SystemExit(1)
