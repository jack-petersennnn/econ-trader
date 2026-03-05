"""
Auto-Settlement System

Checks all open positions in portfolio.json against Kalshi API for settlement status.
Automatically marks positions as won/lost and updates cash/P&L.
"""

import json
import logging
import os
from datetime import datetime
from typing import Optional

import pytz

from data.kalshi_client import KalshiClient

EST = pytz.timezone("US/Eastern")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_PATH = os.path.join(BASE_DIR, "portfolio.json")

logger = logging.getLogger(__name__)


def load_portfolio() -> dict:
    try:
        with open(PORTFOLIO_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {
            "bankroll": 500.0, "cash": 500.0,
            "positions": [], "closed_trades": [],
            "total_pnl": 0.0, "total_trades": 0, "win_rate": 0.0,
            "last_updated": None,
        }


def save_portfolio(portfolio: dict):
    portfolio["last_updated"] = datetime.now(EST).isoformat()
    with open(PORTFOLIO_PATH, "w") as f:
        json.dump(portfolio, f, indent=2, default=str)


def check_market_settlement(client: KalshiClient, ticker: str) -> Optional[dict]:
    """
    Check if a market has settled via the Kalshi API.
    Returns dict with 'settled' (bool), 'result' ('yes'/'no'/None) or None on error.
    """
    try:
        market = client.get_market(ticker)
        status = (market.get("status") or "").lower()
        result = (market.get("result") or "").lower()

        if status in ("settled", "closed", "finalized"):
            if result in ("yes", "no"):
                return {"settled": True, "result": result, "status": status}
            # Settled but no clear result — check yes/no sub-result
            # Some markets use 'yes_price' = 100 or 0 to indicate result
            yes_price = market.get("yes_price", market.get("last_price", None))
            if yes_price is not None:
                if yes_price >= 99 or yes_price >= 0.99:
                    return {"settled": True, "result": "yes", "status": status}
                elif yes_price <= 1 or yes_price <= 0.01:
                    return {"settled": True, "result": "no", "status": status}
            return {"settled": True, "result": None, "status": status}

        return {"settled": False, "result": None, "status": status}
    except Exception as e:
        logger.warning(f"Failed to check settlement for {ticker}: {e}")
        return None


def auto_settle(verbose: bool = True) -> dict:
    """
    Check all open positions and settle any that have resolved.
    
    Returns summary dict with counts.
    """
    portfolio = load_portfolio()
    positions = portfolio.get("positions", [])

    if not positions:
        if verbose:
            print("  No open positions to settle.")
        return {"checked": 0, "settled": 0, "errors": 0, "skipped": 0}

    client = KalshiClient()
    now = datetime.now(EST)

    settled_count = 0
    error_count = 0
    skipped_count = 0

    # Iterate in reverse so we can remove settled positions
    i = len(positions) - 1
    while i >= 0:
        pos = positions[i]
        ticker = pos.get("ticker", "")

        if pos.get("status") != "open":
            i -= 1
            continue

        result = check_market_settlement(client, ticker)

        if result is None:
            # API error
            error_count += 1
            logger.warning(f"AUTO-SETTLE: Could not check {ticker} (API error)")
            if verbose:
                print(f"  ⚠️  {ticker}: API error, skipping")
            i -= 1
            continue

        if not result["settled"]:
            skipped_count += 1
            if verbose:
                print(f"  ⏳ {ticker}: still open (status: {result['status']})")
            i -= 1
            continue

        if result["result"] is None:
            # Settled but can't determine yes/no
            error_count += 1
            logger.warning(f"AUTO-SETTLE: {ticker} settled but result unclear (status: {result['status']})")
            if verbose:
                print(f"  ⚠️  {ticker}: settled but result unclear, manual review needed")
            i -= 1
            continue

        # We have a clear settlement result
        market_result = result["result"]  # "yes" or "no"
        won = (pos["direction"] == market_result)

        if won:
            payout = pos["num_contracts"] * 1.0
            net_pnl = payout - pos["total_cost"]
        else:
            payout = 0
            net_pnl = -pos["total_cost"]

        pos["status"] = "settled"
        pos["settled"] = True
        pos["result"] = market_result
        pos["pnl"] = round(net_pnl, 2)
        pos["settle_time"] = now.isoformat()
        pos["auto_settled"] = True

        portfolio["cash"] += payout
        portfolio["cash"] = round(portfolio["cash"], 2)
        portfolio["total_pnl"] += net_pnl
        portfolio["total_pnl"] = round(portfolio["total_pnl"], 2)

        portfolio["closed_trades"].append(positions.pop(i))

        # Update win rate
        closed = portfolio["closed_trades"]
        wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
        portfolio["win_rate"] = wins / len(closed) if closed else 0.0

        settled_count += 1
        emoji = "✅" if won else "❌"
        logger.info(f"AUTO-SETTLE: {emoji} {ticker} → {market_result} | P&L: ${net_pnl:+.2f}")
        if verbose:
            print(f"  {emoji} {ticker}: {market_result} | P&L: ${net_pnl:+.2f} (auto-settled)")

        i -= 1

    summary = {
        "checked": len(portfolio.get("positions", [])) + settled_count,
        "settled": settled_count,
        "errors": error_count,
        "skipped": skipped_count,
        "timestamp": now.isoformat(),
    }

    if settled_count > 0:
        save_portfolio(portfolio)
        if verbose:
            print(f"\n  💾 Portfolio updated: {settled_count} position(s) settled")

    if verbose:
        print(f"\n  📊 Auto-settle summary: checked={summary['checked']}, "
              f"settled={summary['settled']}, errors={summary['errors']}, "
              f"still_open={summary['skipped']}")

    return summary


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    print("\n  🔄 Running auto-settlement...\n")
    auto_settle(verbose=True)
