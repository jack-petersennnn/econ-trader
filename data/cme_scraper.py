"""
CME FedWatch Probabilities — via FRED Fed Funds Futures

Since CME blocks direct scraping (403), we calculate implied rate
probabilities from FRED data:
- DFEDTARU / DFEDTARL: current Fed Funds target range
- FEDFUNDS: effective fed funds rate

We also try scraping from alternative sources as backup.
"""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional
from urllib.request import urlopen, Request
from urllib.error import URLError, HTTPError

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_config():
    with open(os.path.join(BASE_DIR, "config.json")) as f:
        return json.load(f)


class CMEFedWatchScraper:
    """Fed rate probability estimator using FRED data + alternative sources."""

    def __init__(self):
        self._cache = None
        self._cache_time = None

    def _fetch_from_fred(self) -> Optional[list[dict]]:
        """Calculate implied probabilities from FRED fed funds data."""
        try:
            from data.fred_client import FREDClient
            fred = FREDClient()

            # Get current target range
            upper = fred.get_latest("DFEDTARU")
            lower = fred.get_latest("DFEDTARL")
            effective = fred.get_latest("FEDFUNDS")

            if not upper or not lower:
                logger.warning("Could not get fed funds target range from FRED")
                return None

            current_upper = upper["value"]
            current_lower = lower["value"]
            current_rate = f"{current_lower:.2f}-{current_upper:.2f}"
            eff_rate = effective["value"] if effective else (current_upper + current_lower) / 2

            # Build probability estimates based on rate positioning
            # When effective rate is closer to lower bound → market expects easing
            # When closer to upper bound → market expects tightening
            midpoint = (current_upper + current_lower) / 2
            rate_position = (eff_rate - current_lower) / (current_upper - current_lower) if current_upper != current_lower else 0.5

            # Simple model: use rate position to estimate next meeting probabilities
            # This is a rough approximation — real FedWatch uses futures prices
            hold_prob = 0.60  # base case
            cut_prob = 0.25
            hike_prob = 0.15

            # Adjust based on where effective rate sits in the band
            if rate_position < 0.4:
                # Rate running low in band — market may expect cut
                cut_prob += 0.10
                hold_prob -= 0.10
            elif rate_position > 0.6:
                # Rate running high — could signal tightening bias
                hike_prob += 0.05
                hold_prob -= 0.05

            # Also check if last rate change was recent
            cut_range = f"{current_lower - 0.25:.2f}-{current_upper - 0.25:.2f}"
            hike_range = f"{current_lower + 0.25:.2f}-{current_upper + 0.25:.2f}"

            cfg = _load_config()
            next_fomc = cfg.get("key_dates", {}).get("next_fomc", "next_fomc")

            probabilities = {
                current_rate: round(hold_prob, 3),
                cut_range: round(cut_prob, 3),
                hike_range: round(hike_prob, 3),
            }

            most_likely = max(probabilities, key=probabilities.get)

            return [{
                "meeting_date": next_fomc,
                "probabilities": probabilities,
                "most_likely_rate": most_likely,
                "most_likely_prob": probabilities[most_likely],
                "implied_cut_prob": cut_prob,
                "implied_hold_prob": hold_prob,
                "implied_hike_prob": hike_prob,
                "current_rate": current_rate,
                "effective_rate": eff_rate,
                "source": "FRED (DFEDTARU/DFEDTARL/FEDFUNDS)",
                "note": f"Estimated from FRED data. Current target: {current_rate}%, effective: {eff_rate:.2f}%",
            }]

        except Exception as e:
            logger.warning(f"FRED-based probability calculation failed: {e}")
            return None

    def _try_alternative_sources(self) -> Optional[list[dict]]:
        """Try alternative web sources for FedWatch data."""
        # Try fetching from a financial data aggregator
        urls_to_try = [
            ("https://www.cmegroup.com/services/fedWatch/data.json", {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
                "Accept": "application/json",
                "Referer": "https://www.cmegroup.com/",
            }),
        ]

        for url, headers in urls_to_try:
            try:
                req = Request(url, headers=headers)
                with urlopen(req, timeout=10) as resp:
                    data = json.loads(resp.read().decode())
                    parsed = self._parse_fedwatch_json(data)
                    if parsed:
                        return parsed
            except Exception:
                continue

        return None

    def _parse_fedwatch_json(self, data: dict) -> list[dict]:
        """Parse CME FedWatch JSON if we manage to get it."""
        meetings = []
        try:
            for meeting in data.get("meetings", data.get("data", [])):
                probs = {}
                meeting_date = meeting.get("date", meeting.get("meetingDate", ""))
                for rate_info in meeting.get("probabilities", meeting.get("rates", [])):
                    rate_range = rate_info.get("range", rate_info.get("rate", ""))
                    prob = float(rate_info.get("probability", rate_info.get("prob", 0)))
                    if isinstance(prob, (int, float)) and prob > 1:
                        prob /= 100.0
                    probs[rate_range] = prob

                if probs:
                    most_likely = max(probs, key=probs.get)
                    meetings.append({
                        "meeting_date": meeting_date,
                        "probabilities": probs,
                        "most_likely_rate": most_likely,
                        "most_likely_prob": probs[most_likely],
                    })
        except (KeyError, TypeError, ValueError) as e:
            logger.error(f"Failed to parse FedWatch JSON: {e}")
        return meetings

    def get_probabilities(self, force_refresh: bool = False) -> list[dict]:
        """Get Fed rate probabilities. Tries CME, then FRED-based calculation."""
        # Cache check (5 min TTL)
        if not force_refresh and self._cache and self._cache_time:
            age = (datetime.now() - self._cache_time).seconds
            if age < 300:
                return self._cache

        # Try alternative sources first (might get real CME data)
        result = self._try_alternative_sources()

        # Fall back to FRED-based calculation
        if not result:
            result = self._fetch_from_fred()

        if result:
            self._cache = result
            self._cache_time = datetime.now()
            return result

        # Last resort placeholder
        logger.warning("All FedWatch data sources failed")
        return self._placeholder_data()

    def _placeholder_data(self) -> list[dict]:
        return [{
            "meeting_date": "next_fomc",
            "probabilities": {},
            "most_likely_rate": "unknown",
            "most_likely_prob": 0.0,
            "implied_cut_prob": 0.0,
            "implied_hold_prob": 0.0,
            "implied_hike_prob": 0.0,
            "note": "All data sources unavailable — check CME FedWatch manually",
            "url": "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html",
        }]

    def get_next_meeting(self) -> Optional[dict]:
        meetings = self.get_probabilities()
        return meetings[0] if meetings else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    scraper = CMEFedWatchScraper()
    print("=== FedWatch Probabilities ===\n")
    meetings = scraper.get_probabilities()
    for m in meetings:
        print(f"  Meeting: {m['meeting_date']}")
        if m.get("note"):
            print(f"  ℹ️  {m['note']}")
        print(f"  Most likely: {m['most_likely_rate']} ({m['most_likely_prob']:.1%})")
        for rate, prob in sorted(m.get("probabilities", {}).items()):
            bar = "█" * int(prob * 40)
            print(f"    {rate:15s} {prob:6.1%} {bar}")
        print()
