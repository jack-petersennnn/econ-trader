"""
FRED API Client — Federal Reserve Economic Data
https://api.stlouisfed.org/fred/

Free API, but requires an API key.
Get yours at: https://fred.stlouisfed.org/docs/api/api_key.html
"""

import json
import logging
import os
import sys
from datetime import datetime, timedelta
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError
from urllib.parse import urlencode

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_config():
    with open(os.path.join(BASE_DIR, "config.json")) as f:
        return json.load(f)


class FREDClient:
    """Client for the FRED (Federal Reserve Economic Data) API."""

    BASE_URL = "https://api.stlouisfed.org/fred"

    def __init__(self, api_key: Optional[str] = None):
        cfg = _load_config()
        self.api_key = api_key or os.environ.get("FRED_API_KEY") or cfg.get("fred_api_key", "")
        if not self.api_key or self.api_key == "YOUR_FRED_API_KEY_HERE":
            logger.warning(
                "No FRED API key configured. Set FRED_API_KEY env var or update config.json. "
                "Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html"
            )
        self.series_map = cfg.get("fred_series", {})

    def _get(self, endpoint: str, params: dict) -> dict:
        params["api_key"] = self.api_key
        params["file_type"] = "json"
        url = f"{self.BASE_URL}/{endpoint}?{urlencode(params)}"
        logger.debug(f"FRED request: {url}")
        try:
            req = Request(url, headers={"User-Agent": "econ-trader/1.0"})
            with urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except HTTPError as e:
            if e.code == 400:
                logger.error("FRED API key invalid or missing. Get one at https://fred.stlouisfed.org/docs/api/api_key.html")
            raise
        except URLError as e:
            logger.error(f"FRED request failed: {e}")
            raise

    def get_series(self, series_id: str, limit: int = 12, sort_order: str = "desc") -> list[dict]:
        """Fetch recent observations for a FRED series."""
        data = self._get("series/observations", {
            "series_id": series_id,
            "sort_order": sort_order,
            "limit": limit,
        })
        observations = []
        for obs in data.get("observations", []):
            if obs["value"] != ".":
                observations.append({
                    "date": obs["date"],
                    "value": float(obs["value"]),
                })
        return observations

    def get_latest(self, series_id: str) -> Optional[dict]:
        """Get the most recent observation for a series."""
        obs = self.get_series(series_id, limit=1)
        return obs[0] if obs else None

    def get_series_info(self, series_id: str) -> dict:
        """Get metadata about a series."""
        data = self._get("series", {"series_id": series_id})
        serieses = data.get("seriess", [])
        return serieses[0] if serieses else {}

    def get_ppi(self, limit: int = 12) -> list[dict]:
        return self.get_series(self.series_map.get("ppi", "PPIACO"), limit)

    def get_shelter(self, limit: int = 12) -> list[dict]:
        return self.get_series(self.series_map.get("shelter", "CUSR0000SAH1"), limit)

    def get_jobless_claims(self, limit: int = 12) -> list[dict]:
        return self.get_series(self.series_map.get("jobless_claims", "ICSA"), limit)

    def get_cpi(self, limit: int = 12) -> list[dict]:
        return self.get_series(self.series_map.get("cpi_all", "CPIAUCSL"), limit)

    def get_fed_funds(self, limit: int = 12) -> list[dict]:
        return self.get_series(self.series_map.get("fed_funds", "FEDFUNDS"), limit)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    client = FREDClient()
    if client.api_key == "YOUR_FRED_API_KEY_HERE":
        print("⚠️  FRED API key not set. Get a free key at:")
        print("   https://fred.stlouisfed.org/docs/api/api_key.html")
        print("   Then set FRED_API_KEY env var or update config.json")
        sys.exit(1)

    print("=== FRED Client Test ===\n")
    for name, sid in client.series_map.items():
        try:
            latest = client.get_latest(sid)
            if latest:
                print(f"  {name:20s} ({sid:15s}): {latest['value']:>12,.2f}  ({latest['date']})")
            else:
                print(f"  {name:20s} ({sid:15s}): No data")
        except Exception as e:
            print(f"  {name:20s} ({sid:15s}): ERROR - {e}")
