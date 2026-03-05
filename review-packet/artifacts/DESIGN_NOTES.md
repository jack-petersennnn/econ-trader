# NFP Dynamic Bracket Selector — Design Notes

## What It Does
Given a set of Kalshi NFP contracts + our model's predictive distribution, picks the best tradable bracket(s) by expected value per dollar risked.

## Inputs & Outputs

**Inputs:**
- `contracts[]` — raw Kalshi market dicts (from events API, with nested markets)
- `model_mu` — our ensemble NFP point estimate (raw number, e.g. 195000)
- `model_sigma` — uncertainty (75000, historical NFP miss distribution)
- `consensus_mu` — optional consensus estimate for hybrid blending
- `config` — trading config with bracket_selector overrides

**Output:** `List[Candidate]` — ranked by score (EV/risk), each containing:
- bracket info (ticker, threshold, direction, prices)
- side (yes/no), model_prob, market_prob, edge, EV in cents, score

## Parsing Strategy (two layers)

**Layer A (preferred): Structured fields**
- Kalshi sometimes provides `floor_strike`, `cap_strike`, `strike_type`
- Used directly when present — no regex needed

**Layer B (fallback): Title regex**
- Handles: "above 150K", "> 200,000", "200K or more", "at least 175k"
- Handles: "below 100K", "under 50,000", "less than 100K"
- Handles: "between 100K and 150K", "150K–200K", "100,000 to 150,000"
- Number normalization: commas, K/k suffix, auto-scale if < 1000

**Unparseable contracts:** logged with warning, excluded from scoring. Never silently skipped.

## Consensus Hybrid

```
hybrid_mu = consensus_mu * consensus_weight + model_mu * (1 - consensus_weight)
```

Default `consensus_weight = 0.50` (equal blend). If consensus unavailable, falls back to model-only. Both μ_model and μ_hybrid are logged in selection output.

**Rationale (from Model 2):** Our ensemble uses the same ADP/claims/ISM inputs every macro desk has. Edge comes from finding where Kalshi retail misprices relative to the adjusted distribution, not from beating the market's NFP estimate in absolute terms.

## Probability Computation

Normal CDF: `P(NFP > T) = Φ((μ - T) / σ)`

- GT brackets: P(X > threshold)
- LT brackets: P(X < threshold)  
- BETWEEN brackets: P(lower < X < upper)
- Clamped to [0.02, 0.95] — prevents insane Kelly on tails

## Scoring & Gates

For each bracket, both YES and NO sides are evaluated:

```
entry_price = ask + slippage (1¢ conservative)
fee = fee_rate * p * (1-p) * 100 (in cents)
EV = P(win) * (100 - entry) - P(lose) * entry - fee
score = EV / entry (EV per dollar risked)
```

**Gates (must pass ALL):**
| Gate | Default | Purpose |
|------|---------|---------|
| max_spread_cents | 8¢ | No trading in illiquid books |
| min_ev_cents | 5¢ | Minimum expected value per contract |
| min_edge | 3% | Minimum probability edge |
| price_band_min | 15¢ | No extreme tail speculation |
| price_band_max | 85¢ | No overpaying for high-probability outcomes |
| min_volume | 0 | Volume floor (0 for new markets, raise for live) |

**Idle proof:** When nothing passes gates, logs top 3 near-misses with exact fail reasons.

## Stale Market Mapping Guard

- `compute_snapshot_hash()` — SHA256 of sorted (ticker, title, subtitle) tuples, truncated to 16 chars
- Stored per-event in `market_snapshots.json`
- On each scan: compare current hash to stored hash
- If changed → log WARNING, force full re-parse
- Deterministic: same contracts in different order → same hash

**Prevents:** The class of bug where cached contract IDs point to old/removed brackets (the "always 150K" problem).

## Integration into nfp_model.py

1. NFP model computes ensemble estimate (μ, σ)
2. Tries bracket selector on NFP-categorized markets
3. Runs stale guard check
4. If candidates found → creates Signals from top picks
5. **Fallback:** If bracket_selector not available, import fails, or no NFP contracts → falls back to legacy `_match_to_bracket()` with WARNING log

**Fallback is loud on purpose** — legacy path uses the old fixed-threshold logic we're trying to replace.

## What's NOT Validated Yet

- Real Kalshi NFP contract format (market not listed for Mar 7 yet)
- Live price data / spread behavior
- Consensus scraping integration (placeholder, returns None)
- Volume filter (set to 0, needs calibration against real NFP market depth)

## Files

| File | Purpose |
|------|---------|
| `bracket_selector.py` | Core module (pure, no API calls) |
| `models/nfp_model.py` | Integration + legacy fallback |
| `models/base_model.py` | Signal class, Kelly sizing, portfolio exposure |
| `trader.py` | Trade execution with dedup + event cap |
| `tests/test_bracket_selector.py` | 44 unit tests |
| `tests/fixtures/*.json` | Sample + varied-format contract fixtures |
