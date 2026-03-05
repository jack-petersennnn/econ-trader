#!/usr/bin/env python3
"""
Contract price threshold sweep for CPI and Fed models.
Runs the backtest once with no filter, then post-hoc filters at each threshold.
"""
import json
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import backtester

THRESHOLDS = [0.80, 0.77, 0.74, 0.71, 0.68, 0.65, 0.62, 0.59, 0.56, 0.53, 0.50, 0.47, 0.44, 0.41, 0.38, 0.35]

def sweep_model(model_name: str):
    """Run backtest with no filter, then compute stats at each threshold."""
    # Temporarily set MAX_CONTRACT_PRICE to 1.0 to get ALL trades
    orig = backtester.MAX_CONTRACT_PRICE
    backtester.MAX_CONTRACT_PRICE = 1.0

    if model_name == "cpi":
        result = backtester.backtest_cpi()
    elif model_name == "fed":
        result = backtester.backtest_fed()
    else:
        raise ValueError(f"Unknown model: {model_name}")

    backtester.MAX_CONTRACT_PRICE = orig

    all_trades = result.get("trades", [])
    # Remove skipped trades (shouldn't be any with MAX=1.0, but just in case)
    all_trades = [t for t in all_trades if not t.get("skipped", False)]

    sweep_results = []
    for thresh in THRESHOLDS:
        filtered = [t for t in all_trades if t["contract_price"] <= thresh]
        n = len(filtered)
        if n == 0:
            sweep_results.append({
                "threshold": thresh,
                "trades": 0, "wins": 0, "losses": 0,
                "win_rate": 0, "total_pnl": 0, "avg_pnl": 0
            })
            continue
        wins = sum(1 for t in filtered if t["correct"])
        losses = n - wins
        total_pnl = sum(t["pnl"] for t in filtered)
        sweep_results.append({
            "threshold": thresh,
            "trades": n,
            "wins": wins,
            "losses": losses,
            "win_rate": round(wins / n, 4),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / n, 2),
        })

    return sweep_results, all_trades


def write_results(model_name, sweep_results, output_json, output_md):
    with open(output_json, "w") as f:
        json.dump({"model": model_name, "thresholds": THRESHOLDS, "results": sweep_results}, f, indent=2)

    # Find best
    active = [r for r in sweep_results if r["trades"] > 0]
    best_pnl = max(active, key=lambda r: r["total_pnl"])
    best_per = max(active, key=lambda r: r["avg_pnl"])
    # Risk-adjusted: avg_pnl * win_rate (simple proxy)
    best_risk = max(active, key=lambda r: r["avg_pnl"] * r["win_rate"])

    with open(output_md, "w") as f:
        f.write(f"# {model_name.upper()} Contract Price Threshold Sweep\n\n")
        f.write(f"Sweep across {len(THRESHOLDS)} max contract price thresholds.\n\n")
        f.write("| Max Price | Trades | Wins | Losses | Win Rate | Total P&L | Avg P&L/Trade |\n")
        f.write("|-----------|--------|------|--------|----------|-----------|---------------|\n")
        for r in sweep_results:
            f.write(f"| {r['threshold']:.0%} | {r['trades']} | {r['wins']} | {r['losses']} | "
                    f"{r['win_rate']:.1%} | ${r['total_pnl']:+.2f} | ${r['avg_pnl']:+.2f} |\n")

        f.write(f"\n## Optimal Thresholds\n\n")
        f.write(f"- **Best Total P&L:** {best_pnl['threshold']:.0%} → ${best_pnl['total_pnl']:+.2f} ({best_pnl['trades']} trades, {best_pnl['win_rate']:.1%} WR)\n")
        f.write(f"- **Best Avg P&L/Trade:** {best_per['threshold']:.0%} → ${best_per['avg_pnl']:+.2f}/trade ({best_per['trades']} trades, {best_per['win_rate']:.1%} WR)\n")
        f.write(f"- **Best Risk-Adjusted (avg×WR):** {best_risk['threshold']:.0%} → ${best_risk['avg_pnl']:+.2f}/trade × {best_risk['win_rate']:.1%} WR\n")

        f.write(f"\n## Recommendation\n\n")
        # Recommend best risk-adjusted if it has reasonable trade count, else best total
        rec = best_risk if best_risk['trades'] >= 5 else best_pnl
        f.write(f"**Recommended max contract price: {rec['threshold']:.0%}**\n\n")
        f.write(f"This threshold yields {rec['trades']} trades with {rec['win_rate']:.1%} win rate, "
                f"${rec['total_pnl']:+.2f} total P&L, ${rec['avg_pnl']:+.2f} avg P&L/trade.\n")

    print(f"  Saved: {output_json}")
    print(f"  Saved: {output_md}")


if __name__ == "__main__":
    base = os.path.dirname(os.path.abspath(__file__))

    print("=" * 60)
    print("  CONTRACT PRICE THRESHOLD SWEEP")
    print("=" * 60)

    print("\n  Running CPI sweep...")
    cpi_results, _ = sweep_model("cpi")
    write_results("CPI", cpi_results,
                  os.path.join(base, "cpi-price-sweep.json"),
                  os.path.join(base, "cpi-price-sweep.md"))

    print("\n  Running Fed sweep...")
    fed_results, _ = sweep_model("fed")
    write_results("Fed", fed_results,
                  os.path.join(base, "fed-price-sweep.json"),
                  os.path.join(base, "fed-price-sweep.md"))

    print("\n  Done!")
