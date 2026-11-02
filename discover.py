"""Read-only market discovery. Use a SERIES_TICKER to scope to election contracts."""
import argparse
import requests
from rich.console import Console
from rich.table import Table

BASE = "https://external-api.kalshi.com/trade-api/v2"


def fetch_markets(series: str | None, limit: int = 20) -> list[dict]:
    params = {"limit": limit, "status": "open"}
    if series:
        params["series_ticker"] = series
    response = requests.get(f"{BASE}/markets", params=params, timeout=15)
    response.raise_for_status()
    return response.json().get("markets", [])


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--series", help="Series ticker for the election family")
    parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args()
    markets = fetch_markets(args.series, args.limit)
    table = Table(title="Kalshi open contracts (read-only)")
    for col in ("Ticker", "Title", "YES bid", "YES ask", "Volume"):
        table.add_column(col)
    for m in markets:
        table.add_row(m.get("ticker", ""), (m.get("title") or "")[:65],
                      str(m.get("yes_bid_dollars", "?")),
                      str(m.get("yes_ask_dollars", "?")),
                      str(m.get("volume_fp", "?")))
    Console().print(table)
    Console().print("Select a ticker and run: python monitor.py YOUR_TICKER")
    if not args.series:
        Console().print("[yellow]These are all open categories, not election-filtered. Pass --series to scope discovery.[/yellow]")

if __name__ == "__main__":
    main()
