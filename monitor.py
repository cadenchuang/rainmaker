"""Read-only live order book. Invalidates state and reconnects on gaps/errors."""
import argparse
import asyncio
import base64
import os
import time
from pathlib import Path

import websockets
from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa
from dotenv import load_dotenv
from rich.console import Console
from market_stream import MarketStream, FatalStreamError
from recording import Recorder

WS = "wss://external-api-ws.kalshi.com/trade-api/ws/v2"
CONSOLE = Console()


def auth_headers() -> dict[str, str]:
    key_id = os.environ.get("KALSHI_KEY_ID")
    key_path = os.environ.get("KALSHI_PRIVATE_KEY_PATH")
    if not key_id or not key_path:
        raise FatalStreamError("Signing configuration is missing")
    try:
        private = serialization.load_pem_private_key(Path(key_path).expanduser().read_bytes(), password=None)
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm):
        raise FatalStreamError("Cannot load signing configuration") from None
    timestamp = str(int(time.time() * 1000))
    signed_text = (timestamp + "GET" + "/trade-api/ws/v2").encode()
    if isinstance(private, ed25519.Ed25519PrivateKey):
        signature = private.sign(signed_text)
    elif isinstance(private, rsa.RSAPrivateKey):
        signature = private.sign(signed_text, padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                                                          salt_length=padding.PSS.DIGEST_LENGTH), hashes.SHA256())
    else:
        raise FatalStreamError("Unsupported signing key type")
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
        if stream.recorder is not None:
            health = stream.recorder.health()
            header += (f" | recording={health.coverage} issued={health.issued_ordinal}"
                       f" committed={health.committed_ordinal} buffered={health.buffered_records}"
                       f" lost={health.lost_count} errors={health.error_count} {health.last_error}")
        await asyncio.to_thread(CONSOLE.print, header + "\n" + text, markup=False)
        await asyncio.sleep(1)


async def run(ticker: str | list[str], record_db: str | None = None) -> None:
    load_dotenv()
    recorder = Recorder(record_db) if record_db is not None else None
    stream = MarketStream([ticker] if isinstance(ticker, str) else ticker, recorder=recorder)

    def connect():
        headers = auth_headers()
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
        finally:
            if recorder is not None:
                health = await recorder.close(timeout=5)
                await asyncio.to_thread(CONSOLE.print,
                    f"Recording stopped: {health.coverage}; committed={health.committed_ordinal}"
                    f" issued={health.issued_ordinal}; clean_shutdown={health.clean_shutdown}",
                    markup=False)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("ticker", nargs="+", help="Exact market ticker returned by discover.py")
    parser.add_argument("--record-db", metavar="PATH", help="Record raw events to local SQLite")
    args = parser.parse_args(argv)
    try:
        asyncio.run(run(args.ticker, record_db=args.record_db))
    except KeyboardInterrupt:
        CONSOLE.print("Stopped")
    except FatalStreamError:
        CONSOLE.print("Monitor stopped: check configuration and authorization.", markup=False)
        raise SystemExit(1) from None
    except Exception as exc:
        # Exception text, chains, locals and tracebacks may contain credentials.
        # Preserve the failure class without printing any of those payloads.
        CONSOLE.print(f"Monitor stopped: unexpected {type(exc).__name__}.", markup=False)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
