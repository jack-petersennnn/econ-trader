# Changes Since Initial Review Packet

## Critical Bug Fixes (from Model 1 review)

1. **Challenger source REMOVED** — `ICNSA` is insured unemployment claims, NOT Challenger job cuts data. Was 8% weight producing garbage under a misleading label. No standard FRED series exists for actual Challenger data.

2. **Michigan Sentiment REMOVED** — too weak a predictor of NFP to justify weight. Barely adds information.

3. **Dead code deleted** — line 117 in `nfp_model.py` computed a claims formula that was immediately overwritten on line 120. Removed the dead computation.

4. **ADP 1.1x multiplier dropped** — the claim that "ADP understates NFP by ~10%" varies wildly month to month (sometimes ADP overshoots by 100K+). Fixed multiplier was adding noise at 25% weight. TODO: rolling 6-month bias correction.

## Weight Rebalance (7 sources, down from 9)

| Source | Old Weight | New Weight | Notes |
|--------|-----------|------------|-------|
| ADP | 22% | 25% | Best single predictor |
| Initial Claims | 18% | 22% | Strong inverse correlation |
| ISM Services | 12% | 18% | Services = 80% of jobs, deserved more |
| Continued Claims | 10% | 12% | Lagging but informative |
| Temp Help | 10% | 10% | Unchanged |
| ISM Manufacturing | 8% | 7% | Proportional to 10% of employment |
| Consumer Confidence | 7% | 6% | Weak predictor, reduced |
| Michigan Sentiment | 5% | REMOVED | Too weak |
| Challenger Cuts | 8% | REMOVED | Wrong FRED series |

## Dynamic Sigma with Freshness Check

- σ = 65K when **fresh** ADP available (ADP date within 5 days of NFP release)
- σ = 90K otherwise (stale ADP or no ADP)
- **Floor: 60K** (prevents overconfidence even with ADP)
- **Ceiling: 120K** (prevents absurd uncertainty)
- Freshness check uses `adp_date` component vs `config.key_dates.next_nfp`

## Kelly Sizing

- `kelly_fraction_multiplier`: 0.10 → **0.20**
- Existing hard caps still in place:
  - `max_trade_size`: $25
  - `max_position_pct`: 10% of bankroll ($50)
  - `max_event_exposure_pct`: 20% of bankroll ($100)
  - `max_portfolio_exposure`: 30% of bankroll ($150)
  - `min_trade_size`: $5 (dust skip)

## Legacy Fallback Blocked

- `allow_legacy_fallback: false` in config
- If bracket_selector fails/unavailable → NO TRADE (logs warning, saves snapshot)
- Prevents stale-bracket trades that caused the "always 150K" bug class

## Manual Consensus Field

- `next_nfp_consensus: null` added to config.json
- Update before each NFP release with Bloomberg/consensus estimate
- Bracket selector uses hybrid_mu when consensus available, model-only when null

## Bracket Selector Config

```json
"bracket_selector": {
    "min_volume": 150,
    "max_spread_cents": 8,
    "min_ev_cents": 5,
    "min_edge": 0.03,
    "max_candidates": 1
}
```

## Review Questions for Models

1. Does dropping ADP multiplier + removing two sources improve robustness?
2. Is ADP at 25% too heavy? Double-counting risk with any other source?
3. Is σ=65K (with floor 60K) overly confident historically?
4. Is Kelly 0.20 safe given the caps above and uncalibrated state?
5. Any remaining proxy/double-counting risk in the 7 sources?
