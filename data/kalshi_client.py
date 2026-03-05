"""
Kalshi API Client — read-only market data access.

Uses the public Kalshi API v2 to fetch market listings and odds.
No authentication needed for public market data.
"""

import json
import logging
import os
import time
from datetime import datetime
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Known economics event ticker prefixes on Kalshi
# These are exact prefix matches against the event_ticker
ECONOMICS_PREFIXES = [
    "KXFEDDECISION-", "KXFED-", "KXFEDMEET", "KXFEDEND",
    "KXLCPI", "KXCPI",
    "KXGDP", "KXGDPYEAR", "KXGDPUSMAX", "CHINAUSGDP",
    "KXU3MAX", "KXUNEMPLOYMENT",
    "KXNFP", "KXPPI", "KXPCE",
    "KXRECSSNBER", "KXIRSCOLLECT", "KXTARIFFREVENUE",
    "KXFEDEMPLOYEES", "KXBRAZILGDP", "KXGDPSHAREMANU",
    "KXINEQUALITY",
]

# Title keywords — only match specific economic terms, not broad words
ECONOMICS_TITLE_KEYWORDS = [
    "fomc", "interest rate", "federal funds rate", "rate decision",
    "consumer price index", "inflation rate",
    "gdp growth", "gross domestic product",
    "unemployment rate", "nonfarm payroll", "non-farm payroll",
    "producer price index",
    "recession", "federal reserve rate",
]


def _load_config():
    with open(os.path.join(BASE_DIR, "config.json")) as f:
        return json.load(f)


class KalshiClient:
    """Read-only client for Kalshi market data."""

    def __init__(self):
        cfg = _load_config()
        self.base_url = cfg.get("kalshi_base_url", "https://api.elections.kalshi.com/trade-api/v2")
        self.keywords = cfg.get("market_keywords", {})

    def _get(self, endpoint: str, params: Optional[dict] = None) -> dict:
        url = f"{self.base_url}/{endpoint}"
        if params:
            url += f"?{urlencode(params)}"
        logger.debug(f"Kalshi request: {url}")
        try:
            req = Request(url, headers={
                "User-Agent": "econ-trader/1.0",
                "Accept": "application/json",
            })
            with urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode())
        except (HTTPError, URLError) as e:
            logger.error(f"Kalshi request failed: {e}")
            raise

    # ── Legacy market-based methods (kept for compatibility) ──

    def get_markets(self, cursor: Optional[str] = None, limit: int = 100,
                    status: str = "open") -> dict:
        params = {"limit": limit, "status": status}
        if cursor:
            params["cursor"] = cursor
        return self._get("markets", params)

    def get_market(self, ticker: str) -> dict:
        data = self._get(f"markets/{ticker}")
        return data.get("market", data)

    def get_event(self, event_ticker: str) -> dict:
        return self._get(f"events/{event_ticker}")

    # ── New events-based discovery ──

    def search_economics_events(self, max_pages: int = 10) -> list[dict]:
        """
        Scan /events endpoint and filter for economics-related events.
        Returns list of event dicts, each with nested 'markets' list.
        """
        econ_events = []
        cursor = None

        for page in range(max_pages):
            params = {"limit": 100, "with_nested_markets": "true", "status": "open"}
            if cursor:
                params["cursor"] = cursor

            try:
                data = self._get("events", params)
            except Exception as e:
                logger.error(f"Failed to fetch events page {page}: {e}")
                break

            events = data.get("events", [])
            if not events:
                break

            for ev in events:
                if self._is_economics_event(ev):
                    econ_events.append(ev)

            cursor = data.get("cursor")
            if not cursor:
                break

            time.sleep(1)  # Rate limit

        return econ_events

    def get_event_markets(self, event_ticker: str) -> list[dict]:
        """Fetch markets nested under a specific event."""
        try:
            data = self._get(f"events/{event_ticker}", {"with_nested_markets": "true"})
            event = data.get("event", data)
            return event.get("markets", [])
        except Exception as e:
            logger.error(f"Failed to fetch event markets for {event_ticker}: {e}")
            return []

    def _is_economics_event(self, event: dict) -> bool:
        """Check if an event is economics-related by ticker prefix or title keywords."""
        ticker = event.get("event_ticker", "")
        for prefix in ECONOMICS_PREFIXES:
            if ticker.startswith(prefix):
                return True

        # Only match very specific economic indicator phrases in titles
        title = (event.get("title", "") + " " + event.get("sub_title", "")).lower()
        for kw in ECONOMICS_TITLE_KEYWORDS:
            if kw in title:
                return True

        return False

    # ── Flatten events into normalized markets (for model compatibility) ──

    def search_economics_markets(self, max_pages: int = 10) -> list[dict]:
        """
        Search for economics markets via the events endpoint.
        Returns flat list of normalized market dicts (compatible with models).
        """
        events = self.search_economics_events(max_pages=max_pages)
        all_markets = []

        for ev in events:
            markets = ev.get("markets", [])
            if not markets:
                # Try fetching individually if no nested markets came back
                time.sleep(1)
                markets = self.get_event_markets(ev.get("event_ticker", ""))

            for m in markets:
                if m.get("status", "").lower() not in ("open", "active", ""):
                    continue
                nm = self._normalize_market(m)
                nm["event_title"] = ev.get("title", "")
                all_markets.append(nm)

        return all_markets

    def _normalize_market(self, m: dict) -> dict:
        """Normalize a raw Kalshi market into a clean dict.
        
        Preserves raw bid/ask for bracket_selector while maintaining
        backward-compatible yes_price/no_price/yes_prob fields.
        """
        yes_bid = m.get("yes_bid", m.get("last_price", 0)) or 0
        yes_ask = m.get("yes_ask", 0) or 0
        no_bid = m.get("no_bid", 0) or 0
        no_ask = m.get("no_ask", 0) or 0
        
        # Normalize to 0-1 range
        if yes_bid > 1: yes_bid /= 100.0
        if yes_ask > 1: yes_ask /= 100.0
        if no_bid > 1: no_bid /= 100.0
        if no_ask > 1: no_ask /= 100.0

        # Backward-compatible price fields
        yes_price = yes_bid  # legacy: yes_price = best bid
        no_price = no_bid

        # Determine category
        cat = "other"
        title_lower = (m.get("title", "") + " " + m.get("subtitle", "")).lower()
        ticker_lower = m.get("ticker", "").lower()
        event_ticker = m.get("event_ticker", "").lower()

        cat_map = {
            "fed": ["fed", "fomc", "rate decision", "interest rate", "kxfed"],
            "cpi": ["cpi", "inflation", "consumer price", "kxlcpi", "kxcpi"],
            "gdp": ["gdp", "gross domestic product", "kxgdp"],
            "nfp": ["nonfarm", "non-farm", "payroll", "jobs report", "kxnfp"],
            "unemployment": ["unemployment", "jobless", "kxu3"],
            "ppi": ["ppi", "producer price", "kxppi"],
        }
        combined = f"{title_lower} {ticker_lower} {event_ticker}"
        for cat_name, kws in cat_map.items():
            for kw in kws:
                if kw in combined:
                    cat = cat_name
                    break
            if cat != "other":
                break

        return {
            "ticker": m.get("ticker", ""),
            "event_ticker": m.get("event_ticker", ""),
            "title": m.get("title", ""),
            "subtitle": m.get("subtitle", ""),
            "category": cat,
            # Raw bid/ask for bracket_selector
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            # Backward-compatible fields
            "yes_price": yes_price,
            "no_price": no_price,
            "yes_prob": yes_price,
            "volume": m.get("volume", 0),
            "open_interest": m.get("open_interest", 0),
            "close_time": m.get("close_time", ""),
            "status": m.get("status", ""),
            "result": m.get("result", ""),
        }

    def classify_market(self, market: dict) -> str:
        return market.get("category", "other")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = KalshiClient()
    print("=== Kalshi Economics Events ===\n")
    try:
        events = client.search_economics_events()
        if not events:
            print("No economics events found.")
        for ev in events:
            markets = ev.get("markets", [])
            print(f"  📌 {ev.get('event_ticker', '?')} — {ev.get('title', '?')} ({len(markets)} markets)")
            for m in markets[:5]:
                yes = m.get("yes_bid", m.get("last_price", 0)) or 0
                if yes > 1:
                    yes /= 100.0
                print(f"      {m.get('ticker', '?'):35s}  Yes: {yes:.0%}  {m.get('title', '')}")
            if len(markets) > 5:
                print(f"      ... and {len(markets) - 5} more")
            print()
    except Exception as e:
        print(f"Error: {e}")
