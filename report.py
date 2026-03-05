#!/usr/bin/env python3
"""
Report Generator — Daily/Weekly portfolio and model performance reports.

Usage:
    python3 report.py           # Daily summary
    python3 report.py weekly    # Weekly summary with model breakdown
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta

import pytz

EST = pytz.timezone("US/Eastern")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def load_portfolio() -> dict:
    try:
        with open(os.path.join(BASE_DIR, "portfolio.json")) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"bankroll": 500, "cash": 500, "positions": [], "closed_trades": [],
                "total_pnl": 0, "total_trades": 0, "win_rate": 0}


def load_config() -> dict:
    with open(os.path.join(BASE_DIR, "config.json")) as f:
        return json.load(f)


def daily_report():
    """Generate daily portfolio summary."""
    portfolio = load_portfolio()
    config = load_config()
    now = datetime.now(EST)

    print(f"\n{'═' * 64}")
    print(f"  📊 DAILY REPORT — {now.strftime('%A, %B %d %Y')}")
    print(f"{'═' * 64}\n")

    bankroll = config["bankroll"]
    cash = portfolio["cash"]
    invested = sum(p["total_cost"] for p in portfolio.get("positions", []))
    total_value = cash + invested
    total_return = ((total_value - bankroll) / bankroll) * 100

    print(f"  Portfolio Value:  ${total_value:,.2f}")
    print(f"  Starting Capital: ${bankroll:,.2f}")
    print(f"  Total Return:     {total_return:+.2f}%")
    print(f"  Realized P&L:     ${portfolio.get('total_pnl', 0):+.2f}")
    print(f"  Cash:             ${cash:,.2f}")
    print(f"  Invested:         ${invested:,.2f}")
    print()

    # Open positions
    positions = portfolio.get("positions", [])
    print(f"  Open Positions: {len(positions)}")
    for p in positions:
        print(f"    • {p['ticker']} — {p['direction'].upper()} "
              f"{p['num_contracts']}x @ ${p['entry_price']:.2f} "
              f"(${p['total_cost']:.2f})")
    print()

    # Today's trades
    closed = portfolio.get("closed_trades", [])
    today_str = now.strftime("%Y-%m-%d")
    today_trades = [t for t in closed
                    if t.get("settle_time", "").startswith(today_str)]

    if today_trades:
        today_pnl = sum(t.get("pnl", 0) for t in today_trades)
        print(f"  Today's Settled: {len(today_trades)} trades, P&L: ${today_pnl:+.2f}")
        for t in today_trades:
            emoji = "✅" if t.get("pnl", 0) > 0 else "❌"
            print(f"    {emoji} {t['ticker']} — ${t.get('pnl', 0):+.2f}")
    else:
        print("  No trades settled today.")
    print()


def weekly_report():
    """Generate weekly summary with model-level breakdown."""
    portfolio = load_portfolio()
    config = load_config()
    now = datetime.now(EST)
    week_ago = now - timedelta(days=7)

    print(f"\n{'═' * 64}")
    print(f"  📊 WEEKLY REPORT — Week of {week_ago.strftime('%b %d')} to {now.strftime('%b %d, %Y')}")
    print(f"{'═' * 64}\n")

    closed = portfolio.get("closed_trades", [])

    # Filter to this week
    week_trades = []
    for t in closed:
        settle = t.get("settle_time", "")
        if settle:
            try:
                dt = datetime.fromisoformat(settle)
                if dt.replace(tzinfo=None) >= week_ago.replace(tzinfo=None):
                    week_trades.append(t)
            except ValueError:
                pass

    # Overall stats
    total_pnl = sum(t.get("pnl", 0) for t in week_trades)
    wins = sum(1 for t in week_trades if t.get("pnl", 0) > 0)
    losses = len(week_trades) - wins

    print(f"  Trades This Week: {len(week_trades)}")
    print(f"  Week P&L:         ${total_pnl:+.2f}")
    print(f"  Wins/Losses:      {wins}W / {losses}L")
    print(f"  Win Rate:         {wins / len(week_trades):.0%}" if week_trades else "  Win Rate:         N/A")
    print()

    # By model
    by_model = defaultdict(list)
    for t in week_trades:
        by_model[t.get("model", "unknown")].append(t)

    if by_model:
        print("  Model Breakdown:")
        for model, trades in sorted(by_model.items()):
            model_pnl = sum(t.get("pnl", 0) for t in trades)
            model_wins = sum(1 for t in trades if t.get("pnl", 0) > 0)
            print(f"    {model.upper():10s}  {len(trades)} trades  "
                  f"P&L: ${model_pnl:+.2f}  "
                  f"Win: {model_wins}/{len(trades)}")
    print()

    # Average edge realized
    if week_trades:
        avg_edge = sum(t.get("edge", 0) for t in week_trades) / len(week_trades)
        avg_conf = sum(t.get("confidence", 0) for t in week_trades) / len(week_trades)
        print(f"  Avg Edge:         {avg_edge:+.1%}")
        print(f"  Avg Confidence:   {avg_conf:.0%}")
    print()

    # Cumulative
    bankroll = config["bankroll"]
    total_value = portfolio["cash"] + sum(p["total_cost"] for p in portfolio.get("positions", []))
    print(f"  All-Time P&L:     ${portfolio.get('total_pnl', 0):+.2f}")
    print(f"  Portfolio Value:  ${total_value:,.2f}")
    print(f"  Total Return:     {((total_value - bankroll) / bankroll) * 100:+.2f}%")
    print()


def main():
    if len(sys.argv) > 1 and sys.argv[1].lower() == "weekly":
        weekly_report()
    else:
        daily_report()


if __name__ == "__main__":
    main()
