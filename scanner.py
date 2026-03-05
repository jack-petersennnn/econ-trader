#!/usr/bin/env python3
"""
Kalshi Economics Market Scanner

Scans Kalshi for active economics prediction markets via the /events endpoint.
Shows each event with its nested markets and current odds.

Categories: CPI, Fed Rate, GDP, NFP, Unemployment, PPI

Usage:
    python3 scanner.py           # Show all active econ markets
    python3 scanner.py --json    # Output as JSON
"""

import json
import logging
import sys
import os
from datetime import datetime

import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from data.kalshi_client import KalshiClient

EST = pytz.timezone("US/Eastern")
logger = logging.getLogger(__name__)


def scan_events(verbose: bool = True) -> list[dict]:
    """Scan Kalshi for economics events with nested markets."""
    client = KalshiClient()
    events = client.search_economics_events()

    if verbose and not events:
        print("  ⚠️  No economics events found.")
        print("  This could mean:")
        print("    • No economics events are currently active")
        print("    • API rate limit hit — try again in a minute")

    return events


def scan_markets(verbose: bool = True) -> list[dict]:
    """Scan Kalshi for economics markets (flat list for model compatibility)."""
    client = KalshiClient()
    return client.search_economics_markets()


def print_events(events: list[dict]):
    """Pretty-print event scan results with nested markets."""
    now = datetime.now(EST)
    print(f"\n{'═' * 72}")
    print(f"  KALSHI ECONOMICS MARKET SCANNER")
    print(f"  {now.strftime('%A, %B %d %Y — %I:%M %p ET')}")
    print(f"{'═' * 72}\n")

    if not events:
        print("  No economics events found.\n")
        return

    total_markets = 0
    for ev in events:
        ticker = ev.get("event_ticker", "?")
        title = ev.get("title", "?")
        markets = ev.get("markets", [])
        total_markets += len(markets)

        print(f"  ┌─ {ticker}")
        print(f"  │  {title}")
        print(f"  │  ({len(markets)} market{'s' if len(markets) != 1 else ''})")
        print(f"  │")

        # Sort markets by yes_bid descending
        sorted_markets = sorted(
            markets,
            key=lambda m: m.get("yes_bid", m.get("last_price", 0)) or 0,
            reverse=True,
        )

        for m in sorted_markets:
            mticker = m.get("ticker", "?")
            mtitle = m.get("title", "")
            subtitle = m.get("subtitle", "")
            yes = m.get("yes_bid", m.get("last_price", 0)) or 0
            vol = m.get("volume", 0) or 0
            close = (m.get("close_time") or "")[:10] or "N/A"

            if yes > 1:
                yes /= 100.0

            prob_bar = "█" * int(yes * 20) + "░" * (20 - int(yes * 20))
            label = mtitle
            if subtitle:
                label += f" — {subtitle}"

            print(f"  │  {mticker}")
            print(f"  │    {label}")
            print(f"  │    Yes: {yes:.0%} {prob_bar}  Vol: {vol:>6,}  Close: {close}")
            print(f"  │")

        print(f"  └{'─' * 70}\n")

    print(f"  Total: {len(events)} events, {total_markets} markets\n")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    as_json = "--json" in sys.argv

    events = scan_events(verbose=not as_json)

    if as_json:
        # For JSON mode, output flat market list for compatibility
        markets = scan_markets(verbose=False)
        print(json.dumps(markets, indent=2, default=str))
    else:
        print_events(events)


if __name__ == "__main__":
    main()
