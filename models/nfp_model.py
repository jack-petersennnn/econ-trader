"""
NFP (Non-Farm Payrolls) Prediction Model — 6-Source Ensemble

Estimates the upcoming jobs report using 6 leading indicators:
1. ADP Employment Report (30%) — released 2 days before NFP (FRED: ADPMNUSNERSA)
2. Weekly Initial Jobless Claims (26%) — strong inverse correlation (FRED: ICSA)
3. Continued Claims (15%) — lagging but informative (FRED: CCSA)
4. Regional Fed Mfg Composite (10%) — avg of Empire State + Philly Fed diffusion
   indexes (FRED: GACDISA066MSFRBNY, GACDFSA066MSFRBPHI). Released before NFP.
5. Temporary Help Services (12%) — leading indicator (FRED: TEMPHELPS)
6. Consumer Confidence (7%) — OECD composite (FRED: USACSCICP02STSAM)

Removed (Mar 2026):
  - ISM Manufacturing Employment: NAPMEI deleted from FRED in 2016 (ISM pulled all data)
  - ISM Services Employment: NMFBSI also deleted from FRED in 2016
  - Challenger Job Cuts: was pulling ICNSA (wrong series), no FRED source exists
  - Michigan Sentiment: too weak a predictor to justify weight

ISM note: ISM asked FRED to remove all 22 ISM series on June 24, 2016. Data is
proprietary and not available via any free API. Regional Fed surveys are the best
free alternative for manufacturing sentiment. No equivalent free services PMI exists,
so we redistribute ISM Svc weight across remaining sources rather than use a bad proxy.

Dynamic bracket selection via bracket_selector.py (consensus hybrid approach).
"""

import logging
import math
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from models.base_model import BaseModel, Signal
from data.fred_client import FREDClient

logger = logging.getLogger(__name__)


# Historical reliability weights (inverse MAE-based, normalized)
# Higher = more reliable predictor of NFP
#
# Rebalanced Mar 2026 (v3):
#   - ISM Mfg/Svc removed: FRED deleted all ISM data in 2016 (proprietary)
#   - ISM Mfg replaced with Regional Fed composite (Empire State + Philly Fed avg)
#   - ISM Svc weight redistributed — no free services PMI proxy exists
#   - Challenger removed (wrong FRED series)
#   - Michigan Sentiment removed (too weak)
SOURCE_WEIGHTS = {
    "adp":                 0.30,  # Best single predictor, R²~0.6
    "initial_claims":      0.26,  # Strong inverse correlation
    "continued_claims":    0.15,  # Lagging but informative
    "regional_fed_mfg":    0.10,  # Empire State + Philly Fed avg (diffusion, pre-NFP)
    "temp_help":           0.12,  # Leading indicator of hiring/firing
    "consumer_confidence":  0.07,  # Indirect but correlated
}


class NFPModel(BaseModel):
    """NFP prediction model using employment leading indicators ensemble."""

    NAME = "nfp"

    def __init__(self):
        super().__init__()
        self.fred = FREDClient()

    def get_relevant_markets(self, all_markets: list[dict]) -> list[dict]:
        return [m for m in all_markets if m.get("category") == "nfp"]

    def _fetch_source(self, name: str, series_id: str, limit: int = 6,
                       max_age_days: int = 60) -> Optional[list]:
        """Safely fetch a FRED series with staleness guard.
        
        Returns None (and logs warning) if:
        - API call fails
        - Series returns no data
        - Most recent observation is older than max_age_days
        """
        try:
            data = self.fred.get_series(series_id, limit=limit)
            if not data:
                logger.warning(f"{name} ({series_id}): no data returned")
                return None
            
            # Staleness guard: reject data older than max_age_days
            latest_date_str = data[0].get("date", "")
            if latest_date_str:
                try:
                    from datetime import datetime, date
                    latest_date = datetime.strptime(latest_date_str, "%Y-%m-%d").date()
                    age_days = (date.today() - latest_date).days
                    if age_days > max_age_days:
                        logger.warning(
                            f"{name} ({series_id}): STALE DATA — latest obs {latest_date_str} "
                            f"is {age_days} days old (max {max_age_days}). "
                            f"Series may be discontinued. Rejecting."
                        )
                        return None
                except (ValueError, TypeError):
                    pass  # Can't parse date — let it through with a warning
                    logger.warning(f"{name} ({series_id}): could not parse date '{latest_date_str}'")
            
            return data
        except Exception as e:
            logger.warning(f"Failed to fetch {name} ({series_id}): {e}")
        return None

    def _compute_nfp_estimate(self) -> dict:
        """
        Build NFP estimate from multiple leading indicators.
        Each source produces an independent NFP estimate in thousands.
        Sources are then ensemble-weighted by historical reliability.
        """
        estimate = {
            "components": {},
            "source_estimates": {},  # name -> {"estimate_k": float, "weight": float, "detail": str}
            "nfp_estimate_k": None,
            "confidence": 0.35,
            "reasoning": [],
            "sources_used": 0,
            "sources_failed": 0,
        }

        # ── 1. ADP Employment ──
        # ADPMNUSNERSA = Total Nonfarm Private Payroll Employment (level, thousands)
        # Previously used NPPTTL which was DISCONTINUED May 2022 — caused phantom data bug.
        # We compute month-over-month change from the level series.
        adp_data = self._fetch_source("ADP Employment", "ADPMNUSNERSA", limit=3)
        if adp_data and len(adp_data) >= 2:
            adp_latest = adp_data[0]["value"]
            adp_prev = adp_data[1]["value"]
            # ADPMNUSNERSA is in raw persons (e.g., 132333000 = 132.333M employees)
            # MoM change needs /1000 to convert to "thousands" (K) for the ensemble
            # e.g., 63000 raw change → 63K jobs added
            adp_change = (adp_latest - adp_prev) / 1000
            # Use ADP change directly — fixed 1.1x multiplier was adding noise.
            # ADP-vs-NFP bias varies wildly month to month (+100K to -100K).
            # TODO: rolling 6-month bias correction when we have enough data.
            adp_est = adp_change
            estimate["components"]["adp_level"] = adp_latest
            estimate["components"]["adp_change"] = adp_change
            estimate["components"]["adp_date"] = adp_data[0]["date"]
            estimate["source_estimates"]["adp"] = {
                "estimate_k": adp_est,
                "weight": SOURCE_WEIGHTS["adp"],
                "detail": f"ADP change {adp_change:+,.0f}K → adjusted {adp_est:+,.0f}K",
            }
            estimate["reasoning"].append(f"✅ ADP: {adp_change:+,.0f}K (adj: {adp_est:+,.0f}K)")
        else:
            estimate["sources_failed"] += 1
            estimate["reasoning"].append("❌ ADP: unavailable")

        # ── 2. Initial Jobless Claims (inverse) ──
        claims_data = self._fetch_source("Initial Claims", "ICSA", limit=8)
        if claims_data and len(claims_data) >= 4:
            recent_4 = [c["value"] for c in claims_data[:4]]
            avg_claims = sum(recent_4) / len(recent_4)
            estimate["components"]["claims_4wk_avg"] = round(avg_claims)
            estimate["components"]["claims_latest"] = claims_data[0]["value"]
            # Regression: NFP_K ≈ 500 - 1.5 * claims_K
            claims_k = avg_claims / 1000
            claims_est = 500 - 1.5 * claims_k
            estimate["source_estimates"]["initial_claims"] = {
                "estimate_k": claims_est,
                "weight": SOURCE_WEIGHTS["initial_claims"],
                "detail": f"4wk avg {avg_claims:,.0f} → est {claims_est:+,.0f}K",
            }
            estimate["reasoning"].append(f"✅ Initial Claims: 4wk avg {avg_claims:,.0f} → {claims_est:+,.0f}K")
        else:
            estimate["sources_failed"] += 1
            estimate["reasoning"].append("❌ Initial Claims: unavailable")

        # ── 3. Continued Claims (people staying unemployed) ──
        cc_data = self._fetch_source("Continued Claims", "CCSA", limit=6)
        if cc_data and len(cc_data) >= 2:
            cc_latest = cc_data[0]["value"]
            cc_prev = cc_data[1]["value"]
            cc_change = cc_latest - cc_prev
            estimate["components"]["continued_claims"] = cc_latest
            estimate["components"]["continued_claims_change"] = cc_change
            # Rising continued claims = fewer jobs being created
            # Typical level ~1.7M-1.9M; each 100K above 1.8M reduces NFP by ~30K
            cc_est = 180 - (cc_latest / 1000 - 1800) * 0.3
            # Also factor in the direction
            if cc_change > 50000:
                cc_est -= 20  # rising = bad
            elif cc_change < -50000:
                cc_est += 20  # falling = good
            estimate["source_estimates"]["continued_claims"] = {
                "estimate_k": cc_est,
                "weight": SOURCE_WEIGHTS["continued_claims"],
                "detail": f"Level {cc_latest:,.0f} (Δ{cc_change:+,.0f}) → est {cc_est:+,.0f}K",
            }
            estimate["reasoning"].append(f"✅ Continued Claims: {cc_latest:,.0f} (Δ{cc_change:+,.0f}) → {cc_est:+,.0f}K")
        else:
            estimate["sources_failed"] += 1
            estimate["reasoning"].append("❌ Continued Claims: unavailable")

        # ── 4. Regional Fed Manufacturing Composite ──
        # Replaces ISM Mfg (NAPMEI deleted from FRED 2016) + absorbs ISM Svc weight
        # Empire State (NY Fed) + Philly Fed: diffusion indexes centered at 0
        # Both released mid-month, before NFP Friday
        empire = self._fetch_source("Empire State Mfg", "GACDISA066MSFRBNY", limit=3)
        philly = self._fetch_source("Philly Fed Mfg", "GACDFSA066MSFRBPHI", limit=3)
        fed_vals = []
        if empire:
            fed_vals.append(empire[0]["value"])
        if philly:
            fed_vals.append(philly[0]["value"])
        if fed_vals:
            fed_avg = sum(fed_vals) / len(fed_vals)
            estimate["components"]["regional_fed_mfg"] = fed_avg
            # These center at 0 (not 50 like ISM). Positive = expansion.
            # Empirically: each point above 0 ≈ 5K total NFP impact
            # Baseline 150K + directional signal
            fed_est = 150 + fed_avg * 5
            fed_detail = f"Regional Fed avg {fed_avg:.1f}"
            if len(fed_vals) == 2:
                fed_detail += f" (Empire {fed_vals[0]:.1f}, Philly {fed_vals[1]:.1f})"
            elif empire:
                fed_detail += " (Empire only)"
            else:
                fed_detail += " (Philly only)"
            estimate["source_estimates"]["regional_fed_mfg"] = {
                "estimate_k": fed_est,
                "weight": SOURCE_WEIGHTS["regional_fed_mfg"],
                "detail": f"{fed_detail} → est {fed_est:+,.0f}K",
            }
            estimate["reasoning"].append(f"✅ Regional Fed Mfg: {fed_detail} → {fed_est:+,.0f}K")
        else:
            estimate["sources_failed"] += 1
            estimate["reasoning"].append("❌ Regional Fed Mfg: unavailable (both Empire State & Philly Fed failed)")

        # ── 6. Temporary Help Services (leading indicator) ──
        temp_data = self._fetch_source("Temp Help Services", "TEMPHELPS", limit=4)
        if temp_data and len(temp_data) >= 2:
            temp_latest = temp_data[0]["value"]
            temp_prev = temp_data[1]["value"]
            temp_change = temp_latest - temp_prev
            estimate["components"]["temp_help"] = temp_latest
            estimate["components"]["temp_help_change"] = temp_change
            # Temp help leads NFP by 1-2 months. Change of ±10K temp → ±30K total NFP
            temp_est = 150 + temp_change * 3.0
            estimate["source_estimates"]["temp_help"] = {
                "estimate_k": temp_est,
                "weight": SOURCE_WEIGHTS["temp_help"],
                "detail": f"Temp help Δ{temp_change:+,.1f}K → est {temp_est:+,.0f}K",
            }
            estimate["reasoning"].append(f"✅ Temp Help: Δ{temp_change:+,.1f}K → {temp_est:+,.0f}K")
        else:
            estimate["sources_failed"] += 1
            estimate["reasoning"].append("❌ Temp Help Services: unavailable")

        # ── 7. Consumer Confidence (OECD Composite) ──
        # Replaced CSCICP03USM665S (stale/frozen since Jan 2024) with USACSCICP02STSAM
        # OECD publishes with ~2 month lag, so allow 90 days
        cc_conf = self._fetch_source("Consumer Confidence", "USACSCICP02STSAM", limit=3, max_age_days=90)
        if cc_conf and len(cc_conf) >= 2:
            conf_latest = cc_conf[0]["value"]
            conf_prev = cc_conf[1]["value"]
            conf_change = conf_latest - conf_prev
            estimate["components"]["consumer_confidence"] = conf_latest
            # Confidence > 100 = optimistic. Each point above 100 ≈ 1.5K NFP boost
            conf_est = 150 + (conf_latest - 100) * 1.5 + conf_change * 2
            estimate["source_estimates"]["consumer_confidence"] = {
                "estimate_k": conf_est,
                "weight": SOURCE_WEIGHTS["consumer_confidence"],
                "detail": f"Conf {conf_latest:.1f} (Δ{conf_change:+.1f}) → est {conf_est:+,.0f}K",
            }
            estimate["reasoning"].append(f"✅ Consumer Confidence: {conf_latest:.1f} → {conf_est:+,.0f}K")
        else:
            estimate["sources_failed"] += 1
            estimate["reasoning"].append("❌ Consumer Confidence: unavailable")

        # ── 8. University of Michigan Sentiment — REMOVED (Mar 2026) ──
        # Too weak a predictor of NFP to justify an API call + ensemble weight.
        estimate["reasoning"].append("ℹ️ UMich Sentiment: removed (too weak)")

        # ── 9. Challenger Job Cuts — REMOVED (Mar 2026) ──
        # Was pulling ICNSA (insured unemployment claims) and mislabeling as Challenger data.
        # Actual Challenger, Gray & Christmas data has no standard FRED series.
        # Weight redistributed to remaining sources.
        estimate["reasoning"].append("ℹ️ Challenger Cuts: removed (wrong FRED series)")

        # ── Ensemble ──
        # ── Minimum sources gate ──
        MIN_SOURCES = 4  # Require at least 4 of 6 sources with fresh data
        sources = estimate["source_estimates"]
        estimate["sources_used"] = len(sources)

        if len(sources) < MIN_SOURCES:
            estimate["reasoning"].append(
                f"🚫 DEGRADED: only {len(sources)}/{len(SOURCE_WEIGHTS)} sources available "
                f"(minimum {MIN_SOURCES}). Model should NOT trade — insufficient signal diversity."
            )
            estimate["degraded"] = True

        if sources:
            # Normalize weights for available sources
            total_weight = sum(s["weight"] for s in sources.values())
            nfp_est = sum(
                s["estimate_k"] * s["weight"] / total_weight
                for s in sources.values()
            )

            # Log normalized weights (shows redistribution when sources drop)
            if len(sources) < len(SOURCE_WEIGHTS):
                missing = set(SOURCE_WEIGHTS.keys()) - set(sources.keys())
                redistrib_pct = sum(SOURCE_WEIGHTS[m] for m in missing) * 100
                estimate["reasoning"].append(
                    f"⚠️ Weight redistribution: {redistrib_pct:.0f}% from missing "
                    f"source(s) [{', '.join(missing)}] spread across {len(sources)} active sources"
                )
            estimate["normalized_weights"] = {
                name: round(src["weight"] / total_weight, 4)
                for name, src in sources.items()
            }
            estimate["nfp_estimate_k"] = round(nfp_est)

            # Confidence: more sources = higher confidence, max ~0.85
            base_conf = 0.35
            per_source = 0.06
            estimate["confidence"] = min(base_conf + len(sources) * per_source, 0.88)

            # Boost confidence if sources agree (low dispersion)
            individual_ests = [s["estimate_k"] for s in sources.values()]
            if len(individual_ests) >= 3:
                mean_est = sum(individual_ests) / len(individual_ests)
                variance = sum((e - mean_est) ** 2 for e in individual_ests) / len(individual_ests)
                std = variance ** 0.5
                if std < 40:  # sources agree closely
                    estimate["confidence"] = min(estimate["confidence"] + 0.08, 0.92)
                    estimate["reasoning"].append(f"📊 Source agreement: std={std:.0f}K (high agreement bonus)")
                elif std > 80:  # sources disagree
                    estimate["confidence"] = max(estimate["confidence"] - 0.08, 0.35)
                    estimate["reasoning"].append(f"📊 Source disagreement: std={std:.0f}K (confidence penalty)")

            estimate["reasoning"].append(
                f"📈 Ensemble NFP: {nfp_est:+,.0f}K "
                f"({len(sources)}/{len(SOURCE_WEIGHTS)} sources, conf: {estimate['confidence']:.0%})"
            )

            # Print individual source contributions
            estimate["reasoning"].append("── Source Breakdown ──")
            for name, src in sorted(sources.items(), key=lambda x: -x[1]["weight"]):
                w_pct = src["weight"] / total_weight * 100
                estimate["reasoning"].append(
                    f"  {name:25s} est: {src['estimate_k']:+6.0f}K  wt: {w_pct:4.1f}%"
                )

        return estimate

    def _match_to_bracket(self, nfp_est_k: float, market: dict) -> tuple[float, str]:
        """Match NFP estimate to a market bracket using normal distribution."""
        import re
        title = (market.get("title", "") + " " + market.get("subtitle", "")).lower()

        # NFP uncertainty: std dev ~75K (historical miss distribution)
        std_dev = 75.0

        def norm_cdf(x):
            return 0.5 * (1 + math.erf(x / math.sqrt(2)))

        def prob_above(threshold):
            z = (nfp_est_k - threshold) / std_dev
            return norm_cdf(z)

        # Extract numbers from title
        numbers = re.findall(r'(\d{1,3}(?:,\d{3})*)\s*(?:k|thousand)?', title)
        numbers = [float(n.replace(",", "")) for n in numbers]

        if numbers and all(n >= 1000 for n in numbers):
            numbers = [n / 1000 for n in numbers]

        if "above" in title or "over" in title or "higher" in title or "at least" in title:
            if numbers:
                threshold = max(numbers)
                p = prob_above(threshold)
                return p, f"P(NFP > {threshold:.0f}K) = {p:.1%}"
        elif "below" in title or "under" in title or "lower" in title or "less" in title:
            if numbers:
                threshold = min(numbers)
                p = 1 - prob_above(threshold)
                return p, f"P(NFP < {threshold:.0f}K) = {p:.1%}"
        elif len(numbers) >= 2:
            lo, hi = min(numbers), max(numbers)
            p = prob_above(lo) - prob_above(hi)
            return p, f"P({lo:.0f}K < NFP < {hi:.0f}K) = {p:.1%}"

        return 0.5, f"Could not parse NFP bracket from: {title}"

    def analyze(self, markets: list[dict]) -> list[Signal]:
        """Run NFP model: compute estimate → dynamic bracket selection → signals.
        
        Uses bracket_selector for smart contract picking instead of hardcoded thresholds.
        Falls back to legacy _match_to_bracket() if bracket_selector finds no NFP markets
        (e.g., markets list is already filtered/normalized without raw bracket data).
        """
        signals = []

        # Data quality gate
        from data_quality import run_data_quality_gate
        from data.fred_client import FREDClient
        dq = run_data_quality_gate("nfp", FREDClient())
        if not dq["passed"]:
            for reason in dq["blocked_reasons"]:
                print(f"  🚫 {reason}")
            self.save_snapshot("nfp", [], {}, {"data_quality": dq})
            return signals

        estimate = self._compute_nfp_estimate()

        if estimate["nfp_estimate_k"] is None:
            self.logger.warning("Could not compute NFP estimate")
            return signals

        nfp_k = estimate["nfp_estimate_k"]

        # Dynamic sigma: tighter when FRESH ADP is available (within 5 days of NFP release)
        # Floor at 60K even with ADP (prevents overconfidence if ADP is noisy)
        # Ceiling at 120K without ADP
        SIGMA_FLOOR = 60.0
        SIGMA_CEILING = 120.0
        SIGMA_WITH_ADP = 65.0   # conservative — was 55, floored to 60 per Model 2
        SIGMA_WITHOUT_ADP = 90.0

        has_adp = "adp" in estimate.get("source_estimates", {})
        adp_is_fresh = False
        if has_adp:
            adp_date_str = estimate.get("components", {}).get("adp_date")
            if adp_date_str:
                try:
                    from datetime import datetime
                    adp_date = datetime.strptime(adp_date_str, "%Y-%m-%d").date()
                    nfp_date_str = self.config.get("key_dates", {}).get("next_nfp")
                    if nfp_date_str:
                        nfp_date = datetime.strptime(nfp_date_str, "%Y-%m-%d").date()
                        days_gap = (nfp_date - adp_date).days
                        adp_is_fresh = 0 <= days_gap <= 5
                        if not adp_is_fresh:
                            self.logger.info(
                                f"ADP data is stale ({adp_date_str}, {days_gap}d before NFP) — using wide σ"
                            )
                except (ValueError, TypeError):
                    pass

        raw_sigma = SIGMA_WITH_ADP if adp_is_fresh else SIGMA_WITHOUT_ADP
        nfp_sigma = max(SIGMA_FLOOR, min(SIGMA_CEILING, raw_sigma))
        self.logger.info(
            f"NFP σ = {nfp_sigma:.0f}K "
            f"({'fresh ADP' if adp_is_fresh else 'stale ADP' if has_adp else 'no ADP'}, "
            f"floor={SIGMA_FLOOR}, ceil={SIGMA_CEILING})"
        )

        # ── Try dynamic bracket selection first ──
        try:
            from bracket_selector import (
                select_best_trades, check_and_update_snapshot,
            )
            
            # Filter to NFP markets only (raw contract dicts)
            nfp_contracts = [m for m in markets if m.get("category") == "nfp"]
            
            if nfp_contracts:
                # Stale market guard: check if contract listing changed
                event_ticker = nfp_contracts[0].get("event_ticker", "KXNFP")
                snapshot_info = check_and_update_snapshot(event_ticker, nfp_contracts)
                if snapshot_info["changed"]:
                    self.logger.warning(
                        f"NFP market listing changed! Hash: {snapshot_info['prev_hash']} → "
                        f"{snapshot_info['hash']}. Re-parsing all contracts."
                    )
                
                # Convert nfp_k (in thousands) to raw number for bracket selector
                model_mu = nfp_k * 1000  # e.g., 195K → 195000
                model_sigma = nfp_sigma * 1000  # 75K → 75000
                
                # Consensus: manual entry in config (reliable) or scraping (TODO)
                # Expected in raw units (e.g., 185000). Auto-fix if entered in thousands.
                consensus_mu = self.config.get("next_nfp_consensus")
                if consensus_mu:
                    consensus_mu = float(consensus_mu)
                    if consensus_mu < 10_000:
                        self.logger.warning(
                            f"Consensus looks like thousands ({consensus_mu:,.0f}) — "
                            f"multiplying by 1000 to {consensus_mu * 1000:,.0f}"
                        )
                        consensus_mu *= 1000
                    self.logger.info(f"NFP consensus from config: {consensus_mu:,.0f}")
                else:
                    self.logger.warning("No NFP consensus available — running model-only (no hybrid)")
                
                candidates = select_best_trades(
                    contracts=nfp_contracts,
                    model_mu=model_mu,
                    model_sigma=model_sigma,
                    config=self.config,
                    consensus_mu=consensus_mu,
                    event_ticker=event_ticker,
                )
                
                if candidates:
                    self.logger.info(
                        f"Dynamic bracket selector: {len(candidates)} candidate(s) "
                        f"(mu={nfp_k:.0f}K, σ={nfp_sigma:.0f}K)"
                    )
                    
                    for cand in candidates:
                        summary = "; ".join(estimate["reasoning"][:3])
                        b = cand.bracket
                        full_reasoning = (
                            f"{summary} | "
                            f"Dynamic bracket: {b.contract_id} "
                            f"thresh={b.threshold/1000:.0f}K {b.direction} | "
                            f"P({cand.side})={cand.model_prob:.1%} vs mkt {cand.market_prob:.1%} "
                            f"edge={cand.edge:+.1%} EV={cand.ev_cents:+.1f}¢ "
                            f"spread={cand.spread_cents}¢"
                        )
                        
                        signal = self.make_signal(
                            ticker=b.contract_id,
                            title=b.title,
                            direction=cand.side,
                            model_prob=cand.model_prob,
                            market_prob=cand.market_prob,
                            reasoning=full_reasoning,
                            data_sources=estimate["components"],
                            data_quality_report=dq,
                        )
                        signal.confidence = estimate["confidence"]
                        signals.append(signal)
                    
                    # Save snapshot with bracket selector data
                    self.save_snapshot("nfp", signals, estimate["components"], {
                        "source_estimates": estimate["source_estimates"],
                        "nfp_estimate_k": nfp_k,
                        "nfp_sigma_k": nfp_sigma,
                        "confidence": estimate["confidence"],
                        "data_quality": dq,
                        "bracket_selector": True,
                        "snapshot_hash": snapshot_info["hash"],
                        "candidates": [c.to_dict() for c in candidates],
                    })
                    
                    return signals
                else:
                    self.logger.info("Dynamic bracket selector: no candidates passed filters")
                
        except ImportError:
            self.logger.warning("bracket_selector not available — using legacy _match_to_bracket")
        except Exception as e:
            self.logger.warning(f"bracket_selector error: {e} — falling back to legacy")

        # ── Fallback: legacy per-market matching ──
        # Blocked by default to prevent stale-bracket trades.
        # Enable with config.allow_legacy_fallback = true
        if not self.config.get("allow_legacy_fallback", False):
            self.logger.warning(
                "⚠️ LEGACY FALLBACK BLOCKED: bracket_selector unavailable and "
                "allow_legacy_fallback=false. No NFP trades will be placed. "
                "This is intentional — legacy uses fixed thresholds with unreliable edge."
            )
            self.save_snapshot("nfp", [], estimate["components"],
                              {"nfp_estimate_k": nfp_k, "confidence": estimate["confidence"],
                               "data_quality": dq, "bracket_selector": False,
                               "fallback_blocked": True})
            return signals

        self.logger.warning(
            "⚠️ FALLBACK: Using legacy _match_to_bracket (no dynamic bracket data). "
            "This uses fixed thresholds — edge may be unreliable."
        )
        for market in markets:
            prob, reasoning = self._match_to_bracket(nfp_k, market)
            market_prob = market.get("yes_prob", 0.5)

            if prob > market_prob:
                direction = "yes"
            else:
                direction = "no"

            full_reasoning = "; ".join(estimate["reasoning"][:3]) + f" | {reasoning}"

            signal = self.make_signal(
                ticker=market["ticker"],
                title=market.get("title", ""),
                direction=direction,
                model_prob=prob if direction == "yes" else 1 - prob,
                market_prob=market_prob if direction == "yes" else 1 - market_prob,
                reasoning=full_reasoning,
                data_sources=estimate["components"],
                data_quality_report=dq,
            )
            signal.confidence = estimate["confidence"]
            signals.append(signal)

        # Save snapshot
        self.save_snapshot("nfp", signals, estimate["components"],
                          {"source_estimates": estimate["source_estimates"],
                           "nfp_estimate_k": estimate["nfp_estimate_k"],
                           "confidence": estimate["confidence"],
                           "data_quality": dq,
                           "bracket_selector": False})

        return signals


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("=" * 64)
    print("  NFP (NON-FARM PAYROLLS) MODEL — Multi-Source Ensemble")
    print("=" * 64)
    print()

    model = NFPModel()
    print("  📊 Computing NFP estimate from 9 sources...\n")
    estimate = model._compute_nfp_estimate()
    for line in estimate["reasoning"]:
        print(f"    {line}")
    print()

    if estimate["nfp_estimate_k"]:
        print(f"  📈 NFP Estimate: {estimate['nfp_estimate_k']:+,}K")
        print(f"  🎯 Confidence: {estimate['confidence']:.0%}")
        print(f"  📡 Sources: {estimate['sources_used']}/{len(SOURCE_WEIGHTS)} succeeded")
    else:
        print("  ⚠️  Could not compute estimate (check FRED API key)")
    print()

    print("  🔍 Scanning Kalshi for NFP markets...\n")
    try:
        from data.kalshi_client import KalshiClient
        kalshi = KalshiClient()
        all_markets = kalshi.search_economics_markets()
        signals = model.run(all_markets)
        model.print_signals(signals)
    except Exception as e:
        print(f"  ⚠️  Kalshi scan failed: {e}")
        mock = [{"ticker": "NFP-26MAR-200", "title": "Nonfarm payrolls above 200K",
                 "subtitle": "", "category": "nfp", "yes_prob": 0.55}]
        signals = model.run(mock)
        model.print_signals(signals)
