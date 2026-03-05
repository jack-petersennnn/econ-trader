# FED Contract Price Threshold Sweep

Sweep across 16 max contract price thresholds.

| Max Price | Trades | Wins | Losses | Win Rate | Total P&L | Avg P&L/Trade |
|-----------|--------|------|--------|----------|-----------|---------------|
| 80% | 3 | 3 | 0 | 100.0% | $+3.71 | $+1.24 |
| 77% | 1 | 1 | 0 | 100.0% | $+1.39 | $+1.39 |
| 74% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 71% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 68% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 65% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 62% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 59% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 56% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 53% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 50% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 47% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 44% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 41% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 38% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |
| 35% | 0 | 0 | 0 | 0.0% | $+0.00 | $+0.00 |

## Optimal Thresholds

- **Best Total P&L:** 80% → $+3.71 (3 trades, 100.0% WR)
- **Best Avg P&L/Trade:** 77% → $+1.39/trade (1 trades, 100.0% WR)
- **Best Risk-Adjusted (avg×WR):** 77% → $+1.39/trade × 100.0% WR

## Recommendation

**Recommended max contract price: 80¢ (keep current)**

The existing 80¢ cap is already optimal. Without the filter, the Fed model takes 16 trades at 100% WR but earns only $11.52 total — most trades are on expensive (>80¢) hold contracts where the payout per correct prediction is tiny. With the 80¢ filter, only 3 trades pass but they're the ones with meaningful payouts ($1.24/trade avg). Going below 77¢ eliminates nearly all trades.

**Keep the 80¢ cap.** The filter correctly skips obvious consensus holds and only trades when there's real price dislocation.
