"""
Dynamic Bracket Selector — picks the best tradable contract from a set of
Kalshi brackets given a model's predictive distribution.

Design principles:
  - Pure functions, no API calls — testable with fixtures
  - Consensus-hybrid: blends model estimate with consensus for edge calculation
  - Exhaustive probability distribution (sums to 1, no double-counting)
  - Stale market mapping guard via snapshot hashing
  - Conservative: min EV, spread gates, liquidity floors

Usage:
    from bracket_selector import select_best_trades

    candidates = select_best_trades(
        contracts=kalshi_contracts,    # raw Kalshi market dicts
        model_mu=195_000,              # model point estimate (jobs)
        model_sigma=75_000,            # model uncertainty
        consensus_mu=190_000,          # consensus estimate (optional)
        config=config,                 # trading config dict
    )
"""

import hashlib
import json
import logging
import math
import os
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT_HASH_PATH = os.path.join(BASE_DIR, "market_snapshots.json")

# ─── Default config values (overridden by config.json) ───────────────────────

DEFAULT_SELECTOR_CONFIG = {
    "max_spread_cents": 8,          # max bid-ask spread to consider
    "min_volume": 0,                # min contract volume (0 = no filter for new markets)
    "min_ev_cents": 5,              # minimum expected value per contract in cents
    "min_edge": 0.03,               # minimum probability edge
    "price_band_min_cents": 15,     # don't buy YES below 15¢ (too speculative)
    "price_band_max_cents": 85,     # don't buy YES above 85¢ (too expensive)
    "fee_rate": 0.07,               # Kalshi fee rate
    "slippage_cents": 1,            # conservative slippage assumption
    "max_candidates": 1,            # how many trades per event (start conservative)
    "consensus_weight": 0.50,       # weight on consensus in hybrid (0 = model only)
    "prob_floor": 0.02,             # minimum probability (prevent 0%)
    "prob_cap": 0.95,               # maximum probability
}


# ─── Data classes ─────────────────────────────────────────────────────────────

@dataclass
class Bracket:
    """A parsed bracket from a Kalshi contract."""
    contract_id: str        # Kalshi contract ticker
    event_ticker: str       # parent event
    title: str              # raw title text
    threshold: float        # the number (e.g., 150000 for ">150K")
    direction: str          # "GT" (greater than), "LT" (less than), "BETWEEN"
    upper_bound: Optional[float] = None  # for BETWEEN brackets
    yes_bid: int = 0        # in cents
    yes_ask: int = 0
    no_bid: int = 0
    no_ask: int = 0
    volume: int = 0
    open_interest: int = 0
    parseable: bool = True
    parse_note: str = ""


@dataclass
class Candidate:
    """A scored trade candidate."""
    bracket: Bracket
    side: str               # "yes" or "no"
    model_prob: float       # our probability of this outcome
    market_prob: float      # implied from price
    edge: float             # model_prob - market_prob
    ev_cents: float         # expected value per contract in cents
    entry_price_cents: int  # what we'd pay
    spread_cents: int       # bid-ask spread
    score: float            # ranking score (EV per dollar risked)
    reasoning: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["bracket"] = asdict(self.bracket)
        return d


# ─── Parsing ──────────────────────────────────────────────────────────────────

def _normalize_number(text: str) -> Optional[float]:
    """Parse a number from text, handling K/k suffix and commas."""
    text = text.strip().replace(",", "")
    # Handle "150K", "150k", "150,000"
    m = re.match(r'^(-?\d+\.?\d*)\s*[kK]?$', text)
    if m:
        val = float(m.group(1))
        if 'k' in text.lower() or val < 1000:
            # If it says "K" or the number is small, it's in thousands
            return val * 1000
        return val
    return None


def parse_contracts_to_brackets(contracts: list[dict], event_ticker: str = "") -> list[Bracket]:
    """
    Parse raw Kalshi contract dicts into Bracket objects.
    
    Tries structured fields first (floor_strike, cap_strike),
    then falls back to title regex parsing.
    """
    brackets = []
    
    for c in contracts:
        ticker = c.get("ticker", "")
        title = c.get("title", "") + " " + c.get("subtitle", "")
        
        # Normalize prices to cents
        yes_bid = _to_cents(c.get("yes_bid", 0))
        yes_ask = _to_cents(c.get("yes_ask", 0))
        no_bid = _to_cents(c.get("no_bid", 0))
        no_ask = _to_cents(c.get("no_ask", 0))
        
        bracket = Bracket(
            contract_id=ticker,
            event_ticker=c.get("event_ticker", event_ticker),
            title=title.strip(),
            threshold=0,
            direction="GT",
            yes_bid=yes_bid,
            yes_ask=yes_ask,
            no_bid=no_bid,
            no_ask=no_ask,
            volume=c.get("volume", 0) or 0,
            open_interest=c.get("open_interest", 0) or 0,
        )
        
        # Layer A: Structured fields (Kalshi sometimes provides these)
        floor_strike = c.get("floor_strike")
        cap_strike = c.get("cap_strike")
        strike_type = c.get("strike_type", "").lower()
        
        if floor_strike is not None and cap_strike is not None:
            bracket.threshold = float(floor_strike)
            bracket.upper_bound = float(cap_strike)
            bracket.direction = "BETWEEN"
        elif floor_strike is not None:
            bracket.threshold = float(floor_strike)
            bracket.direction = "GT" if strike_type != "less" else "LT"
        elif cap_strike is not None:
            bracket.threshold = float(cap_strike)
            bracket.direction = "LT" if strike_type != "greater" else "GT"
        else:
            # Layer B: Title regex parsing
            parsed = _parse_threshold_from_title(title)
            if parsed:
                bracket.threshold, bracket.direction, bracket.upper_bound = parsed
            else:
                bracket.parseable = False
                bracket.parse_note = f"Could not parse threshold from: {title}"
                logger.warning(f"Unparseable contract: {ticker} — {title}")
        
        brackets.append(bracket)
    
    return brackets


def _parse_threshold_from_title(title: str) -> Optional[tuple[float, str, Optional[float]]]:
    """
    Extract threshold and direction from contract title.
    Returns (threshold, direction, upper_bound_or_None).
    """
    t = title.lower().strip()
    
    # Range: "between 100K and 150K", "100K to 150K", "100K–150K", "100,000 - 150,000"
    range_patterns = [
        r'between\s+([\d,]+\.?\d*)\s*k?\s*(?:and|to|[-–—])\s*([\d,]+\.?\d*)\s*k?',
        r'([\d,]+\.?\d*)\s*k?\s*(?:to|[-–—])\s*([\d,]+\.?\d*)\s*k?',
    ]
    for pat in range_patterns:
        m = re.search(pat, t)
        if m:
            lo = _normalize_number(m.group(1))
            hi = _normalize_number(m.group(2))
            if lo is not None and hi is not None:
                return (lo, "BETWEEN", hi)
    
    # Greater than: "above 150K", "> 150K", "150K+", "150K or more", "at least 150K"
    gt_patterns = [
        r'(?:above|over|more than|greater than|higher than|at least|≥|>=?)\s*([\d,]+\.?\d*)\s*k?',
        r'([\d,]+\.?\d*)\s*k?\s*(?:\+|or more|or higher|or above)',
    ]
    for pat in gt_patterns:
        m = re.search(pat, t)
        if m:
            val = _normalize_number(m.group(1))
            if val is not None:
                return (val, "GT", None)
    
    # Less than: "below 150K", "< 150K", "under 150K", "less than 150K"
    lt_patterns = [
        r'(?:below|under|less than|lower than|fewer than|≤|<=?)\s*([\d,]+\.?\d*)\s*k?',
        r'([\d,]+\.?\d*)\s*k?\s*(?:or less|or lower|or below|or fewer)',
    ]
    for pat in lt_patterns:
        m = re.search(pat, t)
        if m:
            val = _normalize_number(m.group(1))
            if val is not None:
                return (val, "LT", None)
    
    return None


def _to_cents(price) -> int:
    """Normalize a price to cents. Handles 0-1 float or 0-100 int."""
    if price is None:
        return 0
    price = float(price)
    if 0 < price <= 1:
        return round(price * 100)
    return round(price)


# ─── Probability computation ─────────────────────────────────────────────────

def _norm_cdf(x: float) -> float:
    """Standard normal CDF."""
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def compute_bracket_probabilities(
    brackets: list[Bracket],
    mu: float,
    sigma: float,
    prob_floor: float = 0.02,
    prob_cap: float = 0.95,
) -> dict[str, float]:
    """
    Compute P(YES) for each bracket given a normal distribution N(mu, sigma).
    
    Returns dict of {contract_id: p_yes}.
    Probabilities are clamped to [prob_floor, prob_cap].
    """
    probs = {}
    
    for b in brackets:
        if not b.parseable:
            continue
        
        if b.direction == "GT":
            # P(X > threshold)
            z = (mu - b.threshold) / sigma
            p = _norm_cdf(z)
        elif b.direction == "LT":
            # P(X < threshold)
            z = (b.threshold - mu) / sigma
            p = _norm_cdf(z)
        elif b.direction == "BETWEEN" and b.upper_bound is not None:
            # P(lower < X < upper)
            z_lo = (mu - b.threshold) / sigma
            z_hi = (mu - b.upper_bound) / sigma
            p = _norm_cdf(z_lo) - _norm_cdf(z_hi)
        else:
            p = 0.5  # fallback
        
        p = max(prob_floor, min(prob_cap, p))
        probs[b.contract_id] = p
    
    return probs


# ─── Consensus hybrid ────────────────────────────────────────────────────────

def hybrid_mu(
    model_mu: float,
    consensus_mu: Optional[float],
    consensus_weight: float = 0.50,
) -> float:
    """
    Blend model estimate with consensus.
    
    If consensus unavailable, returns model_mu.
    consensus_weight=0.5 means equal blend.
    """
    if consensus_mu is None:
        return model_mu
    return consensus_mu * consensus_weight + model_mu * (1 - consensus_weight)


# ─── Trade scoring ────────────────────────────────────────────────────────────

def score_candidates(
    brackets: list[Bracket],
    probs: dict[str, float],
    config: dict,
) -> list[Candidate]:
    """
    Score each bracket for both YES and NO sides.
    Returns sorted list of candidates (best first).
    """
    cfg = {**DEFAULT_SELECTOR_CONFIG, **config.get("bracket_selector", {})}
    
    fee_rate = cfg["fee_rate"]
    slippage = cfg["slippage_cents"]
    min_ev = cfg["min_ev_cents"]
    min_edge = cfg["min_edge"]
    max_spread = cfg["max_spread_cents"]
    price_min = cfg["price_band_min_cents"]
    price_max = cfg["price_band_max_cents"]
    
    candidates = []
    
    for b in brackets:
        if not b.parseable:
            continue
        if b.contract_id not in probs:
            continue
        
        p_yes = probs[b.contract_id]
        p_no = 1 - p_yes
        
        # ── Evaluate YES side ──
        if b.yes_ask > 0:
            yes_entry = b.yes_ask + slippage
            yes_spread = b.yes_ask - b.yes_bid if b.yes_bid > 0 else 99
            yes_fee = fee_rate * (yes_entry / 100) * (1 - yes_entry / 100) * 100  # in cents
            
            # EV = P(win) * payout - cost
            # Buying YES at X cents: win = 100 - X - fee, lose = X + fee
            yes_ev = p_yes * (100 - yes_entry) - p_no * yes_entry - yes_fee
            yes_market_p = yes_entry / 100
            yes_edge = p_yes - yes_market_p
            
            if (yes_spread <= max_spread
                and price_min <= yes_entry <= price_max
                and yes_edge >= min_edge
                and yes_ev >= min_ev):
                
                # Score: EV per dollar risked
                risk = yes_entry  # cents at risk
                score = yes_ev / risk if risk > 0 else 0
                
                candidates.append(Candidate(
                    bracket=b,
                    side="yes",
                    model_prob=round(p_yes, 4),
                    market_prob=round(yes_market_p, 4),
                    edge=round(yes_edge, 4),
                    ev_cents=round(yes_ev, 2),
                    entry_price_cents=yes_entry,
                    spread_cents=yes_spread,
                    score=round(score, 4),
                    reasoning=f"BUY YES {b.contract_id} @ {yes_entry}¢ | "
                              f"P(yes)={p_yes:.1%} vs mkt {yes_market_p:.1%} | "
                              f"EV={yes_ev:+.1f}¢ | spread={yes_spread}¢",
                ))
        
        # ── Evaluate NO side ──
        if b.no_ask > 0:
            no_entry = b.no_ask + slippage
            no_spread = b.no_ask - b.no_bid if b.no_bid > 0 else 99
            no_fee = fee_rate * (no_entry / 100) * (1 - no_entry / 100) * 100
            
            no_ev = p_no * (100 - no_entry) - p_yes * no_entry - no_fee
            no_market_p = no_entry / 100
            no_edge = p_no - no_market_p
            
            if (no_spread <= max_spread
                and price_min <= no_entry <= price_max
                and no_edge >= min_edge
                and no_ev >= min_ev):
                
                risk = no_entry
                score = no_ev / risk if risk > 0 else 0
                
                candidates.append(Candidate(
                    bracket=b,
                    side="no",
                    model_prob=round(p_no, 4),
                    market_prob=round(no_market_p, 4),
                    edge=round(no_edge, 4),
                    ev_cents=round(no_ev, 2),
                    entry_price_cents=no_entry,
                    spread_cents=no_spread,
                    score=round(score, 4),
                    reasoning=f"BUY NO {b.contract_id} @ {no_entry}¢ | "
                              f"P(no)={p_no:.1%} vs mkt {no_market_p:.1%} | "
                              f"EV={no_ev:+.1f}¢ | spread={no_spread}¢",
                ))
    
    # Sort by score (EV per dollar risked), descending
    candidates.sort(key=lambda c: c.score, reverse=True)
    return candidates


def select_best_trades(
    contracts: list[dict],
    model_mu: float,
    model_sigma: float,
    config: dict,
    consensus_mu: Optional[float] = None,
    event_ticker: str = "",
) -> list[Candidate]:
    """
    Full pipeline: parse → compute probs → score → select best.
    
    Returns top N candidates (default 1) that pass all gates.
    """
    cfg = {**DEFAULT_SELECTOR_CONFIG, **config.get("bracket_selector", {})}
    max_candidates = cfg["max_candidates"]
    consensus_w = cfg["consensus_weight"]
    
    # 1. Parse contracts
    brackets = parse_contracts_to_brackets(contracts, event_ticker)
    parseable = [b for b in brackets if b.parseable]
    unparseable = [b for b in brackets if not b.parseable]
    
    if unparseable:
        logger.warning(f"Unparseable contracts: {[b.contract_id for b in unparseable]}")
    
    if not parseable:
        logger.warning("No parseable brackets found")
        return []
    
    # 2. Compute hybrid mu
    mu = hybrid_mu(model_mu, consensus_mu, consensus_w)
    
    logger.info(
        f"Bracket selector: model_mu={model_mu:,.0f}, consensus_mu={consensus_mu or 'N/A'}, "
        f"hybrid_mu={mu:,.0f}, sigma={model_sigma:,.0f}, brackets={len(parseable)}"
    )
    
    # 3. Compute probabilities
    probs = compute_bracket_probabilities(
        parseable, mu, model_sigma,
        prob_floor=cfg["prob_floor"],
        prob_cap=cfg["prob_cap"],
    )
    
    # 4. Score and rank
    candidates = score_candidates(parseable, probs, config)
    
    # 5. Select top N
    selected = candidates[:max_candidates]
    
    for i, c in enumerate(selected):
        logger.info(f"Selected #{i+1}: {c.reasoning}")
    
    if not selected:
        # Log why nothing was selected (idle proof)
        logger.info("No candidates passed all gates. Top 3 closest:")
        # Re-score without gates for diagnostics
        all_scored = _score_all_ungated(parseable, probs, config)
        for c in all_scored[:3]:
            logger.info(f"  Near-miss: {c.reasoning}")
    
    return selected


def _score_all_ungated(brackets, probs, config):
    """Score all brackets without gates — for diagnostics only."""
    cfg = {**DEFAULT_SELECTOR_CONFIG, **config.get("bracket_selector", {})}
    slippage = cfg["slippage_cents"]
    fee_rate = cfg["fee_rate"]
    results = []
    
    for b in brackets:
        if b.contract_id not in probs:
            continue
        p_yes = probs[b.contract_id]
        p_no = 1 - p_yes
        
        for side, p_win, ask in [("yes", p_yes, b.yes_ask), ("no", p_no, b.no_ask)]:
            if ask <= 0:
                continue
            entry = ask + slippage
            fee = fee_rate * (entry/100) * (1 - entry/100) * 100
            ev = p_win * (100 - entry) - (1 - p_win) * entry - fee
            spread = (ask - (b.yes_bid if side == "yes" else b.no_bid))
            edge = p_win - entry/100
            
            fail_reasons = []
            if spread > cfg["max_spread_cents"]: fail_reasons.append(f"spread={spread}>{cfg['max_spread_cents']}")
            if entry < cfg["price_band_min_cents"]: fail_reasons.append(f"price={entry}<{cfg['price_band_min_cents']}")
            if entry > cfg["price_band_max_cents"]: fail_reasons.append(f"price={entry}>{cfg['price_band_max_cents']}")
            if edge < cfg["min_edge"]: fail_reasons.append(f"edge={edge:.3f}<{cfg['min_edge']}")
            if ev < cfg["min_ev_cents"]: fail_reasons.append(f"ev={ev:.1f}<{cfg['min_ev_cents']}")
            
            results.append(Candidate(
                bracket=b, side=side, model_prob=round(p_win,4),
                market_prob=round(entry/100,4), edge=round(edge,4),
                ev_cents=round(ev,2), entry_price_cents=entry,
                spread_cents=spread, score=round(ev/(entry or 1),4),
                reasoning=f"{side.upper()} {b.contract_id} @ {entry}¢ | "
                          f"p={p_win:.1%} edge={edge:+.3f} ev={ev:+.1f}¢ | "
                          f"BLOCKED: {', '.join(fail_reasons) or 'none'}",
            ))
    
    results.sort(key=lambda c: c.ev_cents, reverse=True)
    return results


# ─── Stale market mapping guard ──────────────────────────────────────────────

def compute_snapshot_hash(contracts: list[dict]) -> str:
    """
    Deterministic hash of the current market contract listing.
    If this changes, cached mappings must be invalidated.
    """
    # Sort by ticker for determinism
    key_data = sorted([
        (c.get("ticker", ""), c.get("title", ""), c.get("subtitle", ""))
        for c in contracts
    ])
    raw = json.dumps(key_data, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def check_and_update_snapshot(event_ticker: str, contracts: list[dict]) -> dict:
    """
    Check if market listing has changed since last scan.
    
    Returns:
        {"changed": bool, "hash": str, "prev_hash": str|None, "contracts_count": int}
    """
    current_hash = compute_snapshot_hash(contracts)
    
    # Load previous snapshots
    snapshots = {}
    try:
        with open(SNAPSHOT_HASH_PATH) as f:
            snapshots = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    
    prev = snapshots.get(event_ticker, {})
    prev_hash = prev.get("hash")
    changed = prev_hash is not None and prev_hash != current_hash
    
    if changed:
        logger.warning(
            f"Market listing changed for {event_ticker}: "
            f"{prev_hash} → {current_hash} ({len(contracts)} contracts). "
            f"Cached mappings invalidated."
        )
    
    # Update
    snapshots[event_ticker] = {
        "hash": current_hash,
        "contracts_count": len(contracts),
        "last_updated": datetime.utcnow().isoformat(),
    }
    
    try:
        with open(SNAPSHOT_HASH_PATH, "w") as f:
            json.dump(snapshots, f, indent=2)
    except Exception as e:
        logger.error(f"Failed to save snapshot hash: {e}")
    
    return {
        "changed": changed,
        "hash": current_hash,
        "prev_hash": prev_hash,
        "contracts_count": len(contracts),
    }


# ─── CLI for testing ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    
    print("=" * 64)
    print("  BRACKET SELECTOR — Test Mode")
    print("=" * 64)
    
    # Load fixtures if available, otherwise use built-in test data
    fixture_path = os.path.join(BASE_DIR, "tests", "fixtures", "nfp_contracts_sample.json")
    if os.path.exists(fixture_path):
        with open(fixture_path) as f:
            contracts = json.load(f)
        print(f"\n  Loaded {len(contracts)} contracts from fixture\n")
    else:
        print("\n  Using built-in test contracts\n")
        contracts = _get_test_contracts()
    
    config = {}
    config_path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(config_path):
        with open(config_path) as f:
            config = json.load(f)
    
    mu = float(sys.argv[1]) if len(sys.argv) > 1 else 195_000
    sigma = float(sys.argv[2]) if len(sys.argv) > 2 else 75_000
    consensus = float(sys.argv[3]) if len(sys.argv) > 3 else None
    
    print(f"  Model μ: {mu:,.0f}")
    print(f"  Model σ: {sigma:,.0f}")
    print(f"  Consensus: {consensus or 'N/A'}")
    print()
    
    candidates = select_best_trades(
        contracts=contracts,
        model_mu=mu,
        model_sigma=sigma,
        config=config,
        consensus_mu=consensus,
        event_ticker="KXNFP-TEST",
    )
    
    if candidates:
        for c in candidates:
            print(f"  ✅ {c.reasoning}")
    else:
        print("  No candidates selected.")
    print()


def _get_test_contracts():
    """Built-in test contracts mimicking Kalshi NFP format."""
    return [
        {"ticker": "KXNFP-26MAR07-T50", "event_ticker": "KXNFP-26MAR07",
         "title": "Nonfarm payrolls above 50K", "subtitle": "",
         "yes_bid": 95, "yes_ask": 97, "no_bid": 3, "no_ask": 5, "volume": 500},
        {"ticker": "KXNFP-26MAR07-T100", "event_ticker": "KXNFP-26MAR07",
         "title": "Nonfarm payrolls above 100K", "subtitle": "",
         "yes_bid": 85, "yes_ask": 87, "no_bid": 13, "no_ask": 15, "volume": 800},
        {"ticker": "KXNFP-26MAR07-T150", "event_ticker": "KXNFP-26MAR07",
         "title": "Nonfarm payrolls above 150K", "subtitle": "",
         "yes_bid": 68, "yes_ask": 70, "no_bid": 30, "no_ask": 32, "volume": 1200},
        {"ticker": "KXNFP-26MAR07-T175", "event_ticker": "KXNFP-26MAR07",
         "title": "Nonfarm payrolls above 175K", "subtitle": "",
         "yes_bid": 52, "yes_ask": 55, "no_bid": 45, "no_ask": 48, "volume": 1500},
        {"ticker": "KXNFP-26MAR07-T200", "event_ticker": "KXNFP-26MAR07",
         "title": "Nonfarm payrolls above 200K", "subtitle": "",
         "yes_bid": 38, "yes_ask": 41, "no_bid": 59, "no_ask": 62, "volume": 1800},
        {"ticker": "KXNFP-26MAR07-T225", "event_ticker": "KXNFP-26MAR07",
         "title": "Nonfarm payrolls above 225K", "subtitle": "",
         "yes_bid": 22, "yes_ask": 25, "no_bid": 75, "no_ask": 78, "volume": 900},
        {"ticker": "KXNFP-26MAR07-T250", "event_ticker": "KXNFP-26MAR07",
         "title": "Nonfarm payrolls above 250K", "subtitle": "",
         "yes_bid": 12, "yes_ask": 15, "no_bid": 85, "no_ask": 88, "volume": 600},
        {"ticker": "KXNFP-26MAR07-T300", "event_ticker": "KXNFP-26MAR07",
         "title": "Nonfarm payrolls above 300K", "subtitle": "",
         "yes_bid": 5, "yes_ask": 8, "no_bid": 92, "no_ask": 95, "volume": 300},
    ]
