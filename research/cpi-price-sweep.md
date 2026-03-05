# CPI Contract Price Threshold Sweep

Sweep across 16 max contract price thresholds.

| Max Price | Trades | Wins | Losses | Win Rate | Total P&L | Avg P&L/Trade |
|-----------|--------|------|--------|----------|-----------|---------------|
| 80% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 77% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 74% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 71% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 68% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 65% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 62% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 59% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 56% | 24 | 14 | 10 | 58.3% | $+32.81 | $+1.37 |
| 53% | 12 | 6 | 6 | 50.0% | $+22.59 | $+1.88 |
| 50% | 4 | 2 | 2 | 50.0% | $-4.62 | $-1.15 |
| 47% | 1 | 0 | 1 | 0.0% | $-50.00 | $-50.00 |
| 44% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 41% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 38% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 35% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |

## Optimal Thresholds

- **Best Total P&L:** 80% → $+32.81 (24 trades, 58.3% WR)
- **Best Avg P&L/Trade:** 53% → $+1.88/trade (12 trades, 50.0% WR)
- **Best Risk-Adjusted (avg×WR):** 53% → $+1.88/trade × 50.0% WR

## Recommendation

**Recommended max contract price: 65¢ (no filter needed)**

The CPI model's contract pricing formula already caps contracts at 65¢ (`max(0.30, min(0.65, 0.55 - pred_diff * 0.5))`), so any threshold ≥65¢ captures all 24 trades identically. Lowering below 56¢ starts cutting trades but destroys win rate (50% at 53¢ = no edge, and only 4 trades at 50¢).

Unlike the Fed model where expensive contracts (>80¢) represent obvious consensus plays with poor risk/reward, the CPI model already self-limits to cheap contracts. **No additional price cap is needed for CPI.**

The edge comes from taking all the trades — the 58.3% win rate across 24 trades is the sweet spot.
