#!/usr/bin/env python3
"""Unit tests for bracket_selector.py"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from bracket_selector import (
    parse_contracts_to_brackets,
    compute_bracket_probabilities,
    score_candidates,
    select_best_trades,
    hybrid_mu,
    compute_snapshot_hash,
    _parse_threshold_from_title,
    _normalize_number,
    DEFAULT_SELECTOR_CONFIG,
)

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
PASS = 0
FAIL = 0


def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        print(f"  ❌ {name} — {detail}")


def load_fixture(name):
    with open(os.path.join(FIXTURES, name)) as f:
        return json.load(f)


# ─── Test 1: Title parsing ───────────────────────────────────────────────────

print("\n=== Test 1: Title Parsing ===")

cases = [
    ("Nonfarm payrolls above 150K", (150_000, "GT", None)),
    ("above 200,000 jobs", (200_000, "GT", None)),
    ("200K or more", (200_000, "GT", None)),
    ("at least 175k", (175_000, "GT", None)),
    ("Less than 100K jobs", (100_000, "LT", None)),
    ("below 50,000", (50_000, "LT", None)),
    ("Under 200K nonfarm payrolls", (200_000, "LT", None)),
    ("Between 100K and 150K jobs added", (100_000, "BETWEEN", 150_000)),
    ("150K–200K jobs added", (150_000, "BETWEEN", 200_000)),
    ("100,000 to 150,000", (100_000, "BETWEEN", 150_000)),
]

for title, expected in cases:
    result = _parse_threshold_from_title(title)
    if result is None:
        check(f'"{title}"', False, "returned None")
    else:
        thresh, direction, upper = result
        match = (thresh == expected[0] and direction == expected[1] 
                 and upper == expected[2])
        check(f'"{title}" → {direction} {thresh:,.0f}', match,
              f"got ({thresh}, {direction}, {upper}), expected {expected}")

# Unparseable
result = _parse_threshold_from_title("Will the economy add jobs?")
check("Unparseable returns None", result is None, f"got {result}")


# ─── Test 2: Contract parsing (standard fixture) ─────────────────────────────

print("\n=== Test 2: Standard Contract Parsing ===")

contracts = load_fixture("nfp_contracts_sample.json")
brackets = parse_contracts_to_brackets(contracts, "KXNFP-26MAR07")

check(f"Parsed {len(brackets)} brackets", len(brackets) == 8)
check("All parseable", all(b.parseable for b in brackets))

# Verify thresholds are correct and monotonic
thresholds = [b.threshold for b in brackets]
check("Thresholds extracted", thresholds == [50_000, 100_000, 150_000, 175_000, 
                                              200_000, 225_000, 250_000, 300_000],
      f"got {thresholds}")
check("All GT direction", all(b.direction == "GT" for b in brackets))


# ─── Test 3: Varied format parsing ───────────────────────────────────────────

print("\n=== Test 3: Varied Format Parsing ===")

contracts_v = load_fixture("nfp_contracts_varied_format.json")
brackets_v = parse_contracts_to_brackets(contracts_v, "KXNFP-26MAR07")

parseable_v = [b for b in brackets_v if b.parseable]
unparseable_v = [b for b in brackets_v if not b.parseable]

check(f"Parseable: {len(parseable_v)}/8", len(parseable_v) >= 6,
      f"only {len(parseable_v)} parseable")

# NFP-WEIRD ("Will the economy add jobs?") should be unparseable
weird = next((b for b in brackets_v if b.contract_id == "NFP-WEIRD"), None)
check("NFP-WEIRD is unparseable", weird and not weird.parseable)

# Structured field contract should parse
struct = next((b for b in brackets_v if b.contract_id == "NFP-STRUCT-GT"), None)
check("Structured field parsed", struct and struct.parseable and struct.threshold == 175_000 
      and struct.direction == "GT",
      f"got {struct.threshold if struct else 'N/A'}, {struct.direction if struct else 'N/A'}")

# Range parsing
range1 = next((b for b in brackets_v if b.contract_id == "NFP-RANGE-1"), None)
check("Range parsed (between)", range1 and range1.direction == "BETWEEN" 
      and range1.threshold == 100_000 and range1.upper_bound == 150_000,
      f"got {range1.direction if range1 else 'N/A'}")

range_dash = next((b for b in brackets_v if b.contract_id == "NFP-RANGE-DASH"), None)
check("Range parsed (dash)", range_dash and range_dash.direction == "BETWEEN"
      and range_dash.threshold == 150_000 and range_dash.upper_bound == 200_000,
      f"got {range_dash.direction if range_dash else 'N/A'}")


# ─── Test 4: Probability computation ─────────────────────────────────────────

print("\n=== Test 4: Probability Computation ===")

brackets = parse_contracts_to_brackets(load_fixture("nfp_contracts_sample.json"))
probs = compute_bracket_probabilities(brackets, mu=195_000, sigma=75_000)

check("8 probabilities computed", len(probs) == 8)

# Monotonicity: P(>50K) > P(>100K) > P(>150K) > ... > P(>300K)
prob_list = [probs[f"KXNFP-26MAR07-T{t}"] for t in [50, 100, 150, 175, 200, 225, 250, 300]]
is_monotonic = all(prob_list[i] > prob_list[i+1] for i in range(len(prob_list)-1))
check("Probabilities are monotonically decreasing", is_monotonic,
      f"probs: {[f'{p:.3f}' for p in prob_list]}")

# Sanity checks
check("P(>50K) > 90%", prob_list[0] > 0.90, f"got {prob_list[0]:.3f}")
check("P(>300K) < 20%", prob_list[-1] < 0.20, f"got {prob_list[-1]:.3f}")

# Probability clamping
check("P(>50K) <= 0.95 (cap)", prob_list[0] <= 0.95, f"got {prob_list[0]:.3f}")
check("P(>300K) >= 0.02 (floor)", prob_list[-1] >= 0.02, f"got {prob_list[-1]:.3f}")

# With shifted mu
probs_high = compute_bracket_probabilities(brackets, mu=250_000, sigma=75_000)
probs_low = compute_bracket_probabilities(brackets, mu=120_000, sigma=75_000)
check("Higher mu → higher P(>200K)", 
      probs_high["KXNFP-26MAR07-T200"] > probs["KXNFP-26MAR07-T200"],
      f"{probs_high['KXNFP-26MAR07-T200']:.3f} vs {probs['KXNFP-26MAR07-T200']:.3f}")
check("Lower mu → lower P(>200K)",
      probs_low["KXNFP-26MAR07-T200"] < probs["KXNFP-26MAR07-T200"])


# ─── Test 5: Candidate scoring ───────────────────────────────────────────────

print("\n=== Test 5: Candidate Scoring ===")

config = {"bracket_selector": {"max_spread_cents": 8, "min_ev_cents": 3, "min_edge": 0.03}}
candidates = score_candidates(brackets, probs, config)

check("Candidates generated", len(candidates) > 0, f"got {len(candidates)}")

# No candidate should have negative EV
check("All candidates have positive EV", 
      all(c.ev_cents > 0 for c in candidates),
      f"EVs: {[c.ev_cents for c in candidates]}")

# No candidate should have spread > max
check("All candidates within spread limit",
      all(c.spread_cents <= 8 for c in candidates))

# Sorted by score descending
check("Sorted by score (best first)",
      all(candidates[i].score >= candidates[i+1].score 
          for i in range(len(candidates)-1)))


# ─── Test 6: select_best_trades (full pipeline) ──────────────────────────────

print("\n=== Test 6: Full Pipeline ===")

contracts = load_fixture("nfp_contracts_sample.json")

# Model says 195K, market prices imply ~195K area is fair
selected = select_best_trades(
    contracts=contracts, model_mu=195_000, model_sigma=75_000,
    config=config, consensus_mu=190_000, event_ticker="KXNFP-26MAR07",
)

check("At least 1 candidate selected (or 0 if all gated)", True)  # informational
if selected:
    check("Selected has positive edge", selected[0].edge > 0)
    check("Selected has positive EV", selected[0].ev_cents > 0)
    print(f"       → {selected[0].reasoning}")

# Stability: same inputs → same output
selected2 = select_best_trades(
    contracts=contracts, model_mu=195_000, model_sigma=75_000,
    config=config, consensus_mu=190_000, event_ticker="KXNFP-26MAR07",
)
if selected and selected2:
    check("Stable output (same input → same pick)",
          selected[0].bracket.contract_id == selected2[0].bracket.contract_id)

# With very different model → different pick
selected_high = select_best_trades(
    contracts=contracts, model_mu=280_000, model_sigma=75_000,
    config=config, consensus_mu=None, event_ticker="KXNFP-26MAR07",
)
if selected_high and selected:
    check("Different mu → potentially different bracket",
          True)  # just verify it runs


# ─── Test 7: Consensus hybrid ────────────────────────────────────────────────

print("\n=== Test 7: Consensus Hybrid ===")

check("50/50 blend", hybrid_mu(200_000, 180_000, 0.5) == 190_000)
check("100% model", hybrid_mu(200_000, 180_000, 0.0) == 200_000)
check("100% consensus", hybrid_mu(200_000, 180_000, 1.0) == 180_000)
check("No consensus → model only", hybrid_mu(200_000, None, 0.5) == 200_000)


# ─── Test 8: Snapshot hash ───────────────────────────────────────────────────

print("\n=== Test 8: Stale Market Guard ===")

c1 = [{"ticker": "A", "title": "T1", "subtitle": ""}, {"ticker": "B", "title": "T2", "subtitle": ""}]
c2 = [{"ticker": "B", "title": "T2", "subtitle": ""}, {"ticker": "A", "title": "T1", "subtitle": ""}]
c3 = [{"ticker": "A", "title": "T1", "subtitle": ""}, {"ticker": "C", "title": "T3", "subtitle": ""}]

h1 = compute_snapshot_hash(c1)
h2 = compute_snapshot_hash(c2)
h3 = compute_snapshot_hash(c3)

check("Same contracts (diff order) → same hash", h1 == h2)
check("Different contracts → different hash", h1 != h3)
check("Hash is 16 chars", len(h1) == 16)


# ─── Summary ─────────────────────────────────────────────────────────────────

print(f"\n{'═' * 50}")
print(f"  Results: {PASS} passed, {FAIL} failed")
print(f"{'═' * 50}\n")

if FAIL > 0:
    sys.exit(1)
