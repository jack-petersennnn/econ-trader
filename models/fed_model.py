"""
Fed Rate Model v2 — Conditions-Based with Contract Price Filter

Key improvements over v1:
  1. >80¢ contract filter — skip trades where contract price > 0.80
     (need >82% WR to break even on those, not worth it)
  2. Focus on 2-4 month out meetings where uncertainty is higher
  3. New FRED indicators: ANFCI, BAMLH0A0HYM2, U6RATE, JTSJOL,
     CES0500000003, T10Y3M
  4. Consensus scraping for surprise-based signals
  5. Better macro composite with labor market slack

Pipeline:
  1. CME FedWatch (primary price signal)
  2. Macro conditions composite (expanded indicators)
  3. Consensus check (surprise-based)
  4. Contract price filter (skip >80¢)
  5. Only trade when conditions diverge from market AND contract is 20-80¢
"""

import json
import logging
import os
import re
import sys
from datetime import datetime
from typing import Optional
from urllib.request import urlopen, Request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.base_model import BaseModel, Signal
from data.cme_scraper import CMEFedWatchScraper
from data.fred_client import FREDClient

logger = logging.getLogger(__name__)

# Latest Fed dot plot median projections (update after each SEP release)
DOT_PLOT = {
    "2026_end": 3.875,
    "2027_end": 3.375,
    "longer_run": 3.00,
    "current_upper": 4.50,
    "current_lower": 4.25,
    "last_updated": "2025-12-18",
}

# v2: Maximum contract price we'll trade (above this, skip)
MAX_CONTRACT_PRICE = 0.80


class FedModel(BaseModel):
    """Fed rate model v2: conditions-based with contract price filter."""

    NAME = "fed"

    def __init__(self):
        super().__init__()
        self.cme = CMEFedWatchScraper()
        self.fred = FREDClient()

    def get_relevant_markets(self, all_markets: list[dict]) -> list[dict]:
        return [m for m in all_markets if m.get("category") == "fed"]

    def _fetch(self, name: str, series_id: str, limit: int = 6):
        """Safely fetch a FRED series."""
        try:
            data = self.fred.get_series(series_id, limit=limit)
            if data:
                return data
        except Exception as e:
            logger.warning(f"Failed to fetch {name} ({series_id}): {e}")
        return None

    # ─── NEW: Consensus Scraping ──────────────────────────────────────────────

    def _fetch_consensus_fed(self) -> Optional[dict]:
        """
        Scrape Fed rate decision consensus from TradingEconomics or Investing.com.
        Returns dict with expected decision and probability.
        """
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 Chrome/122.0.0.0 Safari/537.36",
            "Accept": "text/html,*/*",
        }

        try:
            url = "https://tradingeconomics.com/united-states/interest-rate"
            req = Request(url, headers=headers)
            with urlopen(req, timeout=15) as resp:
                html = resp.read().decode()
            # Look for forecast
            forecast_patterns = [
                r'Forecast[^<]*?<[^>]*>(\d+\.?\d*)',
                r'"forecast"\s*:\s*"?(\d+\.?\d*)',
            ]
            for pat in forecast_patterns:
                match = re.search(pat, html, re.IGNORECASE)
                if match:
                    val = float(match.group(1))
                    if 0 <= val <= 10:
                        return {"rate_forecast": val, "source": "TradingEconomics"}
        except Exception as e:
            logger.debug(f"Fed consensus scrape failed: {e}")

        return None

    # ─── Expanded Macro Context ──────────────────────────────────────────────

    def _get_macro_context(self) -> dict:
        """
        Gather macro indicators that inform Fed policy direction.
        v2: Added ANFCI, credit spreads, U6, JOLTS, avg hourly earnings, T10Y3M.
        """
        ctx = {
            "signals": {},
            "components": {},
            "reasoning": [],
            "confidence_boost": 0.0,
        }

        # ── 1. Yield Curve (original + T10Y3M) ──
        gs2 = self._fetch("2Y Treasury", "GS2", limit=3)
        gs10 = self._fetch("10Y Treasury", "GS10", limit=3)
        t10y2y = self._fetch("10Y-2Y Spread", "T10Y2Y", limit=3)
        t10y3m = self._fetch("10Y-3M Spread", "T10Y3M", limit=3)  # NEW

        if gs2 and gs10:
            gs2_val = gs2[0]["value"]
            gs10_val = gs10[0]["value"]
            spread = gs10_val - gs2_val
            ctx["components"]["gs2"] = gs2_val
            ctx["components"]["gs10"] = gs10_val
            ctx["components"]["yield_spread"] = round(spread, 3)

            if spread < -0.5:
                ctx["signals"]["yield_curve"] = "cut"
                ctx["reasoning"].append(f"✅ Yield curve: deeply inverted ({spread:+.2f}%) → cut")
            elif spread < 0:
                ctx["signals"]["yield_curve"] = "cut"
                ctx["reasoning"].append(f"✅ Yield curve: inverted ({spread:+.2f}%) → mild cut")
            elif spread < 0.5:
                ctx["signals"]["yield_curve"] = "hold"
                ctx["reasoning"].append(f"✅ Yield curve: flat ({spread:+.2f}%) → hold")
            else:
                ctx["signals"]["yield_curve"] = "hold"
                ctx["reasoning"].append(f"✅ Yield curve: normal ({spread:+.2f}%) → hold")

        # NEW: 10Y-3M spread (better recession predictor per NY Fed)
        if t10y3m:
            t10y3m_val = t10y3m[0]["value"]
            ctx["components"]["t10y3m"] = t10y3m_val
            if t10y3m_val < -1.0:
                ctx["signals"]["t10y3m"] = "cut"
                ctx["reasoning"].append(f"✅ 10Y-3M: {t10y3m_val:+.2f}% (deeply inverted) → cut")
            elif t10y3m_val < 0:
                ctx["signals"]["t10y3m"] = "cut"
                ctx["reasoning"].append(f"✅ 10Y-3M: {t10y3m_val:+.2f}% (inverted) → cut")
            else:
                ctx["signals"]["t10y3m"] = "hold"
                ctx["reasoning"].append(f"✅ 10Y-3M: {t10y3m_val:+.2f}% → hold")

        # ── 2. Financial Conditions (NFCI + ANFCI) ──
        nfci = self._fetch("NFCI", "NFCI", limit=3)
        anfci = self._fetch("Adjusted NFCI", "ANFCI", limit=3)  # NEW

        if anfci:
            # ANFCI is better — removes business cycle effects
            anfci_val = anfci[0]["value"]
            ctx["components"]["anfci"] = anfci_val
            if anfci_val > 0.5:
                ctx["signals"]["financial_conditions"] = "cut"
                ctx["reasoning"].append(f"✅ ANFCI: {anfci_val:+.3f} (tight) → cut signal")
            elif anfci_val > 0:
                ctx["signals"]["financial_conditions"] = "hold"
                ctx["reasoning"].append(f"✅ ANFCI: {anfci_val:+.3f} (slightly tight) → hold")
            elif anfci_val > -0.5:
                ctx["signals"]["financial_conditions"] = "hold"
                ctx["reasoning"].append(f"✅ ANFCI: {anfci_val:+.3f} (neutral) → hold")
            else:
                ctx["signals"]["financial_conditions"] = "hike"
                ctx["reasoning"].append(f"✅ ANFCI: {anfci_val:+.3f} (very loose) → hike bias")
        elif nfci:
            nfci_val = nfci[0]["value"]
            ctx["components"]["nfci"] = nfci_val
            if nfci_val > 0.5:
                ctx["signals"]["financial_conditions"] = "cut"
            elif nfci_val < -0.5:
                ctx["signals"]["financial_conditions"] = "hike"
            else:
                ctx["signals"]["financial_conditions"] = "hold"
            ctx["reasoning"].append(f"✅ NFCI: {nfci_val:+.3f}")

        # ── NEW: High Yield Credit Spreads ──
        hy_oas = self._fetch("HY Credit Spreads", "BAMLH0A0HYM2", limit=6)
        if hy_oas and len(hy_oas) >= 2:
            hy_val = hy_oas[0]["value"]
            hy_prev = hy_oas[1]["value"]
            hy_change = hy_val - hy_prev
            ctx["components"]["hy_oas"] = hy_val
            ctx["components"]["hy_oas_change"] = round(hy_change, 2)
            # Widening spreads = stress = cut pressure
            if hy_val > 5.0 or hy_change > 1.0:
                ctx["signals"]["credit_spreads"] = "cut"
                ctx["reasoning"].append(f"✅ HY OAS: {hy_val:.2f}% (Δ{hy_change:+.2f}) → cut (stress)")
            elif hy_val > 4.0:
                ctx["signals"]["credit_spreads"] = "hold"
                ctx["reasoning"].append(f"✅ HY OAS: {hy_val:.2f}% → hold (elevated)")
            else:
                ctx["signals"]["credit_spreads"] = "hold"
                ctx["reasoning"].append(f"✅ HY OAS: {hy_val:.2f}% → hold (normal)")
        else:
            ctx["reasoning"].append("❌ HY Credit Spreads: unavailable")

        # ── 3. Core PCE ──
        pce = self._fetch("Core PCE", "PCEPILFE", limit=14)
        if pce and len(pce) >= 13:
            pce_latest = pce[0]["value"]
            pce_12mo = pce[12]["value"]
            if pce_12mo > 0:
                pce_yoy = ((pce_latest - pce_12mo) / pce_12mo) * 100
                ctx["components"]["core_pce_yoy"] = round(pce_yoy, 2)
                if pce_yoy > 3.0:
                    ctx["signals"]["core_pce"] = "hike"
                elif pce_yoy > 2.3:
                    ctx["signals"]["core_pce"] = "hold"
                elif pce_yoy > 1.7:
                    ctx["signals"]["core_pce"] = "hold"
                else:
                    ctx["signals"]["core_pce"] = "cut"
                ctx["reasoning"].append(f"✅ Core PCE: {pce_yoy:.2f}%")

        # ── 4. Unemployment (original + U6) ──
        unrate = self._fetch("Unemployment Rate", "UNRATE", limit=6)
        if unrate and len(unrate) >= 2:
            ur_latest = unrate[0]["value"]
            ur_prev = unrate[1]["value"]
            ur_change = ur_latest - ur_prev
            ctx["components"]["unemployment_rate"] = ur_latest
            ctx["components"]["unemployment_change"] = ur_change
            if ur_latest > 4.5 or ur_change > 0.3:
                ctx["signals"]["unemployment"] = "cut"
            elif ur_latest < 3.5:
                ctx["signals"]["unemployment"] = "hike"
            else:
                ctx["signals"]["unemployment"] = "hold"
            ctx["reasoning"].append(f"✅ Unemployment: {ur_latest:.1f}% (Δ{ur_change:+.1f})")

        # NEW: U-6 underemployment (broader measure)
        u6 = self._fetch("U-6 Rate", "U6RATE", limit=6)
        if u6 and len(u6) >= 2:
            u6_val = u6[0]["value"]
            u6_prev = u6[1]["value"]
            u6_change = u6_val - u6_prev
            ctx["components"]["u6_rate"] = u6_val
            if u6_val > 8.0 or u6_change > 0.5:
                ctx["signals"]["u6"] = "cut"
                ctx["reasoning"].append(f"✅ U-6: {u6_val:.1f}% (Δ{u6_change:+.1f}) → cut (slack)")
            elif u6_val < 6.5:
                ctx["signals"]["u6"] = "hike"
                ctx["reasoning"].append(f"✅ U-6: {u6_val:.1f}% (tight) → hike bias")
            else:
                ctx["signals"]["u6"] = "hold"
                ctx["reasoning"].append(f"✅ U-6: {u6_val:.1f}% → hold")
        else:
            ctx["reasoning"].append("❌ U-6: unavailable")

        # ── NEW: JOLTS Job Openings (leading labor indicator) ──
        jolts = self._fetch("JOLTS Job Openings", "JTSJOL", limit=6)
        if jolts and len(jolts) >= 2:
            jolts_val = jolts[0]["value"]
            jolts_prev = jolts[1]["value"]
            jolts_change = jolts_val - jolts_prev
            ctx["components"]["jolts_openings"] = jolts_val
            ctx["components"]["jolts_change"] = jolts_change
            # Falling openings precede rate cuts by 3-6 months
            if jolts_val < 7000 or jolts_change < -500:
                ctx["signals"]["jolts"] = "cut"
                ctx["reasoning"].append(f"✅ JOLTS: {jolts_val:,.0f}K (Δ{jolts_change:+,.0f}K) → cut (weakening)")
            elif jolts_val > 10000:
                ctx["signals"]["jolts"] = "hike"
                ctx["reasoning"].append(f"✅ JOLTS: {jolts_val:,.0f}K → hike bias (very strong)")
            else:
                ctx["signals"]["jolts"] = "hold"
                ctx["reasoning"].append(f"✅ JOLTS: {jolts_val:,.0f}K → hold")
        else:
            ctx["reasoning"].append("❌ JOLTS: unavailable")

        # ── NEW: Average Hourly Earnings (wage inflation) ──
        ahe = self._fetch("Avg Hourly Earnings", "CES0500000003", limit=14)
        if ahe and len(ahe) >= 13:
            ahe_latest = ahe[0]["value"]
            ahe_12mo = ahe[12]["value"]
            if ahe_12mo > 0:
                ahe_yoy = ((ahe_latest - ahe_12mo) / ahe_12mo) * 100
                ctx["components"]["avg_hourly_earnings_yoy"] = round(ahe_yoy, 2)
                # Wage growth > 4% = inflationary pressure = hold/hike
                if ahe_yoy > 4.5:
                    ctx["signals"]["wages"] = "hike"
                    ctx["reasoning"].append(f"✅ Wages: {ahe_yoy:.1f}% YoY → hike pressure")
                elif ahe_yoy > 3.5:
                    ctx["signals"]["wages"] = "hold"
                    ctx["reasoning"].append(f"✅ Wages: {ahe_yoy:.1f}% YoY → hold (elevated)")
                else:
                    ctx["signals"]["wages"] = "cut"
                    ctx["reasoning"].append(f"✅ Wages: {ahe_yoy:.1f}% YoY → cut (subdued)")
        else:
            ctx["reasoning"].append("❌ Avg Hourly Earnings: unavailable")

        # ── 5. Dot Plot Signal ──
        current_mid = (DOT_PLOT["current_upper"] + DOT_PLOT["current_lower"]) / 2
        dot_end = DOT_PLOT["2026_end"]
        implied_cuts_bp = (current_mid - dot_end) * 100
        ctx["components"]["dot_plot_2026"] = dot_end
        ctx["components"]["dot_plot_implied_cuts_bp"] = implied_cuts_bp

        if implied_cuts_bp > 50:
            ctx["signals"]["dot_plot"] = "cut"
        elif implied_cuts_bp > 0:
            ctx["signals"]["dot_plot"] = "cut"
        else:
            ctx["signals"]["dot_plot"] = "hold"
        ctx["reasoning"].append(f"✅ Dot Plot: {implied_cuts_bp:.0f}bp of cuts implied by 2026-end")

        # ── Consensus from web (v2 new) ──
        consensus = self._fetch_consensus_fed()
        if consensus:
            ctx["components"]["consensus_rate_forecast"] = consensus["rate_forecast"]
            ctx["reasoning"].append(f"✅ Consensus rate forecast: {consensus['rate_forecast']:.2f}% ({consensus['source']})")

        # ── Consensus check ──
        if ctx["signals"]:
            from collections import Counter
            votes = Counter(ctx["signals"].values())
            total = len(ctx["signals"])
            most_common, count = votes.most_common(1)[0]
            agreement = count / total
            ctx["consensus"] = most_common
            ctx["agreement"] = agreement

            if agreement >= 0.8:
                ctx["confidence_boost"] = 0.15
                ctx["reasoning"].append(f"🎯 Strong consensus: {count}/{total} say {most_common.upper()}")
            elif agreement >= 0.6:
                ctx["confidence_boost"] = 0.08
                ctx["reasoning"].append(f"📊 Moderate consensus: {count}/{total} say {most_common.upper()}")
            else:
                ctx["confidence_boost"] = 0.0
                ctx["reasoning"].append(f"⚠️ Mixed signals: {dict(votes)}")

        return ctx

    def _extract_rate_from_title(self, title: str) -> dict:
        """Extract rate decision info from a Kalshi market title."""
        title_lower = title.lower()
        info = {"type": None, "rate": None}

        if "cut" in title_lower:
            info["type"] = "cut"
        elif "hike" in title_lower or "raise" in title_lower or "increase" in title_lower:
            info["type"] = "hike"
        elif "hold" in title_lower or "unchanged" in title_lower or "no change" in title_lower:
            info["type"] = "hold"

        rate_match = re.search(r'(\d+\.?\d*)\s*[-–to]+\s*(\d+\.?\d*)%?', title_lower)
        if rate_match:
            info["rate_low"] = float(rate_match.group(1))
            info["rate_high"] = float(rate_match.group(2))
            info["rate"] = f"{info['rate_low']:.2f}-{info['rate_high']:.2f}"

        bp_match = re.search(r'(\d+)\s*(?:basis points?|bp)', title_lower)
        if bp_match:
            info["basis_points"] = int(bp_match.group(1))

        return info

    def _find_cme_probability(self, market_info: dict, cme_data: list[dict]) -> tuple[float, str]:
        """Match a Kalshi market to the corresponding CME FedWatch probability."""
        if not cme_data or not cme_data[0].get("probabilities"):
            return 0.5, "No CME FedWatch data available"

        meeting = cme_data[0]
        probs = meeting.get("probabilities", {})
        if not probs:
            return 0.5, "CME data has no probabilities"

        if market_info.get("rate") and market_info["rate"] in probs:
            p = probs[market_info["rate"]]
            return p, f"CME prob for {market_info['rate']}: {p:.1%}"

        if market_info.get("type") == "hold":
            hold_prob = meeting.get("most_likely_prob", 0.5)
            return hold_prob, f"CME hold prob: {hold_prob:.1%}"

        if market_info.get("type") == "cut":
            cut_prob = meeting.get("implied_cut_prob", 0.3)
            return cut_prob, f"CME implied cut: {cut_prob:.1%}"

        if market_info.get("type") == "hike":
            hike_prob = meeting.get("implied_hike_prob", 0.05)
            return hike_prob, f"CME implied hike: {hike_prob:.1%}"

        return 0.5, "Could not match market to CME probability"

    def analyze(self, markets: list[dict]) -> list[Signal]:
        signals = []

        # Data quality gate
        from data_quality import run_data_quality_gate
        dq = run_data_quality_gate("fed", self.fred)
        if not dq["passed"]:
            for reason in dq["blocked_reasons"]:
                print(f"  🚫 {reason}")
            self.save_snapshot("fed", [], {}, {"data_quality": dq})
            return signals

        cme_data = self.cme.get_probabilities()
        macro = self._get_macro_context()

        cme_note = ""
        if cme_data and cme_data[0].get("note"):
            cme_note = cme_data[0]["note"]

        for market in markets:
            title = market.get("title", "") + " " + market.get("subtitle", "")
            market_info = self._extract_rate_from_title(title)
            cme_prob, cme_reasoning = self._find_cme_probability(market_info, cme_data)
            market_prob = market.get("yes_prob", 0.5)

            # ── v2: CONTRACT PRICE FILTER ──
            # If contract is priced > 80¢, skip — need >82% WR to break even
            effective_price = max(market_prob, 1 - market_prob)
            if effective_price > MAX_CONTRACT_PRICE:
                self.logger.debug(
                    f"Skipping {market['ticker']}: contract price {effective_price:.0%} > "
                    f"{MAX_CONTRACT_PRICE:.0%} threshold"
                )
                continue

            # Adjust CME prob using macro context
            adjusted_prob = cme_prob
            if macro.get("consensus") and market_info.get("type"):
                if macro["consensus"] == market_info["type"]:
                    adjusted_prob = min(cme_prob + 0.03 * macro.get("agreement", 0.5), 0.98)
                elif macro["consensus"] != market_info["type"] and market_info["type"] != "hold":
                    adjusted_prob = max(cme_prob - 0.02 * macro.get("agreement", 0.5), 0.02)

            divergence = abs(adjusted_prob - market_prob)
            fee = self.kalshi_fee(market_prob)

            if divergence > fee:
                if adjusted_prob > market_prob:
                    direction = "yes"
                    reasoning = (
                        f"CME {cme_prob:.1%} (adj {adjusted_prob:.1%}) vs Kalshi {market_prob:.1%} "
                        f"(div {divergence:.1%} > fee {fee:.1%}). {cme_reasoning}"
                    )
                else:
                    direction = "no"
                    reasoning = (
                        f"CME NO {1 - cme_prob:.1%} (adj {1 - adjusted_prob:.1%}) vs Kalshi {1 - market_prob:.1%} "
                        f"(div {divergence:.1%} > fee {fee:.1%}). {cme_reasoning}"
                    )

                if macro["reasoning"]:
                    reasoning += " | Macro: " + "; ".join(macro["reasoning"][:3])

                signal = self.make_signal(
                    ticker=market["ticker"],
                    title=market.get("title", ""),
                    direction=direction,
                    model_prob=adjusted_prob if direction == "yes" else 1 - adjusted_prob,
                    market_prob=market_prob if direction == "yes" else 1 - market_prob,
                    reasoning=reasoning + (f" | ⚠️ {cme_note}" if cme_note else ""),
                    data_sources={
                        "cme_meeting": cme_data[0].get("meeting_date", "unknown") if cme_data else "N/A",
                        "cme_probs": cme_data[0].get("probabilities", {}) if cme_data else {},
                        "macro_consensus": macro.get("consensus", "unknown"),
                        "macro_agreement": macro.get("agreement", 0),
                        "contract_price_filter": f"passed ({effective_price:.0%} <= {MAX_CONTRACT_PRICE:.0%})",
                        **macro["components"],
                    },
                    data_quality_report=dq,
                )
                base_conf = 0.80 if not cme_note else 0.40
                signal.confidence = min(base_conf + macro.get("confidence_boost", 0), 0.95)
                signals.append(signal)
            else:
                self.logger.debug(
                    f"No edge on {market['ticker']}: div {divergence:.1%} <= fee {fee:.1%}"
                )

        # Save snapshot
        self.save_snapshot("fed", signals, macro["components"],
                          {"cme_data": cme_data[0] if cme_data else {},
                           "macro_consensus": macro.get("consensus"),
                           "macro_agreement": macro.get("agreement"),
                           "data_quality": dq})

        return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=" * 64)
    print("  FED RATE MODEL v2 — Conditions-Based + Price Filter")
    print("=" * 64)
    print()

    model = FedModel()
    print("  📊 Gathering expanded macro indicators...\n")
    macro = model._get_macro_context()
    for line in macro["reasoning"]:
        print(f"    {line}")
    print()

    print("  📊 Fetching CME FedWatch data...\n")
    meetings = model.cme.get_probabilities()
    for m in meetings:
        print(f"    Meeting: {m['meeting_date']}")
        if m.get("note"):
            print(f"    ⚠️  {m['note']}")
        else:
            print(f"    Most likely: {m['most_likely_rate']} ({m['most_likely_prob']:.1%})")
    print()
    print(f"  ⚡ Contract price filter: skip > {MAX_CONTRACT_PRICE:.0%}")
    print()
