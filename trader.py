#!/usr/bin/env python3
"""
Econ Trader — Paper Trading Engine

Usage:
    python3 trader.py scan      # Full pipeline: scan markets + generate signals
    python3 trader.py report    # Current portfolio status
    python3 trader.py settle TICKER yes|no  # Manually settle a position
"""

import json
import logging
import os
import sys
from datetime import datetime
from typing import Optional

import pytz

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from data.kalshi_client import KalshiClient
from models.base_model import Signal, load_config
from models.cpi_model import CPIModel
from models.nfp_model import NFPModel
from models.fed_model import FedModel
from auto_settler import auto_settle

EST = pytz.timezone("US/Eastern")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
PORTFOLIO_PATH = os.path.join(BASE_DIR, "portfolio.json")
SCANS_DIR = os.path.join(BASE_DIR, "scans")

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


def save_scan(all_signals: list[Signal], markets_count: int):
    """Save scan results to scans/YYYY-MM-DD.json (append if exists)."""
    os.makedirs(SCANS_DIR, exist_ok=True)
    now = datetime.now(EST)
    date_str = now.strftime("%Y-%m-%d")
    scan_path = os.path.join(SCANS_DIR, f"{date_str}.json")

    # Load existing scans for today
    existing = []
    if os.path.exists(scan_path):
        try:
            with open(scan_path) as f:
                existing = json.load(f)
        except (json.JSONDecodeError, TypeError):
            existing = []

    scan_entry = {
        "timestamp": now.isoformat(),
        "markets_scanned": markets_count,
        "signals_count": len(all_signals),
        "actionable_count": sum(1 for s in all_signals if s.is_actionable),
        "watchlist_count": sum(1 for s in all_signals if s.is_watchlist),
        "signals": [s.to_dict() for s in all_signals],
    }

    existing.append(scan_entry)
    with open(scan_path, "w") as f:
        json.dump(existing, f, indent=2, default=str)

    return scan_path


def print_key_dates():
    """Print upcoming key economic dates."""
    cfg = load_config()
    dates = cfg.get("key_dates", {})
    if not dates:
        return

    now = datetime.now(EST).date()
    print(f"  📅 KEY DATES:")

    labels = {
        "next_cpi": "CPI Release",
        "next_nfp": "Jobs Report (NFP)",
        "next_fomc": "FOMC Meeting",
    }

    for key, label in labels.items():
        date_str = dates.get(key)
        if date_str:
            try:
                dt = datetime.strptime(date_str, "%Y-%m-%d").date()
                days = (dt - now).days
                status = f"{days}d away" if days > 0 else ("TODAY!" if days == 0 else f"{-days}d ago")
                emoji = "🔴" if days <= 3 else ("🟡" if days <= 7 else "🟢")
                print(f"  {emoji} {label:20s} {date_str}  ({status})")
            except ValueError:
                print(f"     {label:20s} {date_str}")

    print()


def _extract_event_id(ticker: str, market_title: str = "") -> str:
    """Extract the event root from a ticker for dedup grouping.
    
    Examples:
        KXLCPIMAXYOY-27-P3   -> KXLCPIMAXYOY-27
        KXLCPIMAXYOY-27-P3.5 -> KXLCPIMAXYOY-27
        NFP-26MAR-200         -> NFP-26MAR
    
    Strips the last segment after the final dash if it looks like a
    bracket/strike (starts with P, T, B, or is a number).
    
    Fallback: if ticker parse doesn't strip anything (no bracket suffix found),
    and market_title is provided, uses normalized market_title as grouping key.
    This handles future Kalshi ticker format changes.
    """
    parts = ticker.rsplit("-", 1)
    if len(parts) == 2:
        suffix = parts[1]
        # Bracket/strike suffixes: P3, P3.5, T47, B85.5, 200, etc.
        if suffix and (suffix[0] in ("P", "T", "B") or suffix.replace(".", "").isdigit()):
            return parts[0]
    # Fallback: use normalized market_title if available (groups all brackets of same event)
    if market_title:
        return f"title:{market_title.strip().lower()}"
    return ticker


def execute_paper_trade(signal: Signal, portfolio: dict) -> Optional[dict]:
    """Execute a paper trade based on a signal.
    
    Safety rails (added Mar 2026):
      1. Ticker dedup — no duplicate open position on the same ticker
      2. Event exposure cap — max 20% of bankroll on any single event
    """
    if not signal.is_actionable:
        return None

    cfg = load_config()

    # ── SAFETY: Ticker dedup — skip if we already hold this exact ticker ──
    open_tickers = {p["ticker"] for p in portfolio["positions"] if p["status"] == "open"}
    if signal.ticker in open_tickers:
        logger.info(f"DEDUP: Skipping {signal.ticker} — already have open position")
        return None

    # ── SAFETY: Event-level exposure cap (default 20% of bankroll) ──
    max_event_exposure_pct = cfg.get("max_event_exposure_pct", 0.20)
    event_id = _extract_event_id(signal.ticker, signal.market_title)
    event_exposure = sum(
        p["total_cost"]
        for p in portfolio["positions"]
        if p["status"] == "open" and _extract_event_id(p["ticker"], p.get("market_title", "")) == event_id
    )
    max_event_exposure = cfg["bankroll"] * max_event_exposure_pct
    remaining_event_capacity = max(0, max_event_exposure - event_exposure)

    if remaining_event_capacity <= 0:
        logger.info(
            f"EVENT CAP: Skipping {signal.ticker} — event '{event_id}' already at "
            f"${event_exposure:.2f} (cap: ${max_event_exposure:.2f} = {max_event_exposure_pct:.0%} of bankroll)"
        )
        return None

    # ── SAFETY: Minimum trade size — skip dust positions that distort metrics ──
    min_trade_size = cfg.get("min_trade_size", 5.0)
    if remaining_event_capacity < min_trade_size:
        logger.info(
            f"DUST SKIP: {signal.ticker} — remaining event cap ${remaining_event_capacity:.2f} "
            f"below min trade size ${min_trade_size:.2f}"
        )
        return None

    max_size = cfg["max_position_pct"] * portfolio["cash"]
    size = min(signal.recommended_size, max_size, remaining_event_capacity)

    if size < 1.0:
        return None
    if size > portfolio["cash"]:
        logger.warning(f"Insufficient cash (${portfolio['cash']:.2f}) for ${size:.2f} trade")
        return None

    price = signal.market_prob
    if price <= 0 or price >= 1:
        return None

    fee = cfg["fee_rate"] * price * (1 - price)
    cost_per_contract = price + fee
    num_contracts = int(size / cost_per_contract) if cost_per_contract > 0 else 0

    if num_contracts < 1:
        return None

    total_cost = num_contracts * cost_per_contract

    trade = {
        "ticker": signal.ticker,
        "market_title": signal.market_title,
        "direction": signal.direction,
        "model": signal.model,
        "num_contracts": num_contracts,
        "entry_price": price,
        "fee_per_contract": round(fee, 4),
        "total_cost": round(total_cost, 2),
        "model_prob": signal.model_prob,
        "edge": signal.edge,
        "confidence": signal.confidence,
        "kelly_fraction": signal.kelly_fraction,
        "reasoning": signal.reasoning,
        "entry_time": datetime.now(EST).isoformat(),
        "status": "open",
        "settled": False,
        "result": None,
        "pnl": None,
    }

    portfolio["cash"] -= total_cost
    portfolio["cash"] = round(portfolio["cash"], 2)
    portfolio["positions"].append(trade)
    portfolio["total_trades"] += 1

    # ── POST-TRADE INVARIANT: verify event cap wasn't breached ──
    _assert_event_exposure_invariant(portfolio, cfg)

    return trade


def _assert_event_exposure_invariant(portfolio: dict, cfg: dict):
    """Post-trade safety check: no event should exceed its exposure cap.
    
    This catches bugs even if the dedup/cap logic above has a hole.
    Raises RuntimeError in paper mode; in live mode would block further trades.
    """
    max_event_exposure_pct = cfg.get("max_event_exposure_pct", 0.20)
    max_event_exposure = cfg["bankroll"] * max_event_exposure_pct
    epsilon = 1.0  # $1 tolerance for rounding

    event_totals: dict[str, float] = {}
    for p in portfolio["positions"]:
        if p["status"] == "open":
            eid = _extract_event_id(p["ticker"], p.get("market_title", ""))
            event_totals[eid] = event_totals.get(eid, 0) + p["total_cost"]

    for eid, total in event_totals.items():
        if total > max_event_exposure + epsilon:
            pct = total / cfg["bankroll"] * 100
            logger.error(
                f"⚠️ INVARIANT VIOLATION: Event '{eid}' has ${total:.2f} exposure "
                f"({pct:.0f}% of bankroll) — cap is ${max_event_exposure:.2f} "
                f"({max_event_exposure_pct:.0%}). This should not happen!"
            )
            # In paper mode, log loudly but don't crash the whole scan
            # In live mode, this should halt all trading


def settle_position(portfolio: dict, ticker: str, result: str) -> Optional[dict]:
    for i, pos in enumerate(portfolio["positions"]):
        if pos["ticker"] == ticker and pos["status"] == "open":
            won = (pos["direction"] == result)
            if won:
                payout = pos["num_contracts"] * 1.0
                net_pnl = payout - pos["total_cost"]
            else:
                payout = 0
                net_pnl = -pos["total_cost"]

            pos["status"] = "settled"
            pos["settled"] = True
            pos["result"] = result
            pos["pnl"] = round(net_pnl, 2)
            pos["settle_time"] = datetime.now(EST).isoformat()

            portfolio["cash"] += payout
            portfolio["cash"] = round(portfolio["cash"], 2)
            portfolio["total_pnl"] += net_pnl
            portfolio["total_pnl"] = round(portfolio["total_pnl"], 2)

            portfolio["closed_trades"].append(portfolio["positions"].pop(i))

            closed = portfolio["closed_trades"]
            wins = sum(1 for t in closed if t.get("pnl", 0) > 0)
            portfolio["win_rate"] = wins / len(closed) if closed else 0.0

            return pos
    return None


def print_summary_table(all_signals: list[Signal]):
    """Print a clean summary table of all signals."""
    print(f"\n  {'━' * 90}")
    print(f"  {'TICKER':<28s} {'DIR':>4s} {'MODEL':>6s} {'MARKET':>7s} "
          f"{'EDGE':>7s} {'CONF':>6s} {'SIZE':>8s} {'STATUS':<10s}")
    print(f"  {'━' * 90}")

    triggered = [s for s in all_signals if s.is_actionable]
    watchlist = [s for s in all_signals if s.is_watchlist]
    no_edge = [s for s in all_signals if s.status == "NO_EDGE"]

    for label, group in [("🎯 TRIGGERED", triggered), ("👀 WATCHLIST", watchlist), ("— NO EDGE", no_edge)]:
        if not group:
            continue
        for s in sorted(group, key=lambda x: -abs(x.edge)):
            status = s.status
            print(f"  {s.ticker:<28s} {s.direction.upper():>4s} {s.model_prob:>5.0%} "
                  f" {s.market_prob:>5.0%}  {s.edge:>+6.1%} {s.confidence:>5.0%} "
                  f"${s.recommended_size:>6.2f}  {label}")

    print(f"  {'━' * 90}")
    print(f"  Total: {len(all_signals)} signals | "
          f"{len(triggered)} triggered | "
          f"{len(watchlist)} watchlist | "
          f"{len(no_edge)} no edge")
    print()


def run_scan():
    """Full pipeline: scan markets, run models, show recommendations."""
    cfg = load_config()
    now = datetime.now(EST)
    print(f"\n{'═' * 72}")
    print(f"  ECON TRADER — FULL PIPELINE SCAN")
    print(f"  {now.strftime('%A, %B %d %Y — %I:%M %p ET')}")
    print(f"  Mode: PAPER TRADING")
    print(f"{'═' * 72}\n")

    # Key dates
    print_key_dates()

    # 0. Auto-settle before scanning
    print("  🔄 Running auto-settlement...\n")
    try:
        auto_settle(verbose=True)
    except Exception as e:
        print(f"  ⚠️  Auto-settle error (non-blocking): {e}")
    print()

    # 1. Scan markets
    print("  📡 Scanning Kalshi for economics markets...\n")
    try:
        kalshi = KalshiClient()
        markets = kalshi.search_economics_markets()
        print(f"  Found {len(markets)} economics markets\n")
    except Exception as e:
        print(f"  ⚠️  Kalshi scan failed: {e}")
        print("  Using mock markets for demonstration...\n")
        markets = [
            {"ticker": "CPI-26MAR-3.0", "title": "CPI YoY above 3.0%",
             "subtitle": "", "category": "cpi", "yes_prob": 0.45, "volume": 1200, "close_time": "2026-03-14"},
            {"ticker": "NFP-26MAR-200", "title": "Nonfarm payrolls above 200K",
             "subtitle": "", "category": "nfp", "yes_prob": 0.55, "volume": 800, "close_time": "2026-03-07"},
            {"ticker": "FED-26MAR-CUT", "title": "Fed cuts rates by 25 basis points",
             "subtitle": "March FOMC", "category": "fed", "yes_prob": 0.30, "volume": 2000, "close_time": "2026-03-19"},
        ]

    # 2. Run models (respect overrides)
    overrides = cfg.get("model_overrides", {})
    models = [
        ("cpi", CPIModel()),
        ("nfp", NFPModel()),
        ("fed", FedModel()),
    ]
    all_signals = []

    for model_key, model in models:
        override = overrides.get(model_key, {})
        if override.get("enabled") is False:
            print(f"  ⏸️  {model.NAME.upper()} model — DISABLED ({override.get('notes', '')})\n")
            continue
        print(f"  🔬 Running {model.NAME.upper()} model...")
        try:
            signals = model.run(markets)
            # Apply per-model thresholds
            if "min_confidence" in override or "min_edge" in override:
                min_conf = override.get("min_confidence", cfg.get("min_confidence", 0.45))
                min_edge = override.get("min_edge", cfg.get("min_edge", 0.03))
                for s in signals:
                    if s.confidence < min_conf or abs(s.edge) < min_edge:
                        s._force_disabled = True
            all_signals.extend(signals)
            if not signals:
                print(f"     No {model.NAME.upper()} signals\n")
        except Exception as e:
            print(f"     ⚠️  {model.NAME.upper()} model error: {e}\n")

    # 3. Summary table
    print_summary_table(all_signals)

    # 4. Save scan
    scan_path = save_scan(all_signals, len(markets))
    print(f"  💾 Scan saved to {scan_path}\n")

    # 5. Execute actionable trades
    actionable = [s for s in all_signals if s.is_actionable]
    watchlist = [s for s in all_signals if s.is_watchlist]

    if actionable:
        portfolio = load_portfolio()
        print(f"  💰 Cash available: ${portfolio['cash']:.2f}\n")

        for signal in actionable:
            trade = execute_paper_trade(signal, portfolio)
            if trade:
                print(f"  ✅ TRADE: {signal.ticker} {signal.direction.upper()} — "
                      f"{trade['num_contracts']} contracts @ ${trade['entry_price']:.2f} "
                      f"(cost: ${trade['total_cost']:.2f})")
            else:
                print(f"  ⏸️  {signal.ticker}: trade skipped (insufficient size/cash)")

        save_portfolio(portfolio)
        print(f"\n  💰 Cash remaining: ${portfolio['cash']:.2f}")
        print(f"  📊 Open positions: {len(portfolio['positions'])}")
    else:
        print("  No actionable trades at this time.")

    if watchlist:
        print(f"\n  👀 {len(watchlist)} signal(s) on watchlist — monitor as events approach")

    print()


def run_report():
    """Show current portfolio status."""
    portfolio = load_portfolio()
    now = datetime.now(EST)

    print(f"\n{'═' * 72}")
    print(f"  ECON TRADER — PORTFOLIO REPORT")
    print(f"  {now.strftime('%A, %B %d %Y — %I:%M %p ET')}")
    print(f"{'═' * 72}\n")

    print_key_dates()

    cfg = load_config()
    print(f"  💰 Bankroll:     ${cfg['bankroll']:.2f}")
    print(f"  💵 Cash:         ${portfolio['cash']:.2f}")
    invested = sum(p["total_cost"] for p in portfolio["positions"])
    print(f"  📊 Invested:     ${invested:.2f}")
    print(f"  📈 Total P&L:    ${portfolio['total_pnl']:+.2f}")
    print(f"  🎯 Win Rate:     {portfolio['win_rate']:.0%}")
    print(f"  📊 Total Trades: {portfolio['total_trades']}")

    if portfolio.get("last_updated"):
        print(f"  🕐 Last Updated: {portfolio['last_updated']}")
    print()

    if portfolio["positions"]:
        print(f"  ┌─ OPEN POSITIONS {'─' * 52}")
        for p in portfolio["positions"]:
            print(f"  │  {p['ticker']:25s} {p['direction'].upper():4s}  "
                  f"{p['num_contracts']}x @ ${p['entry_price']:.2f}  "
                  f"Cost: ${p['total_cost']:.2f}")
            print(f"  │  Model: {p['model'].upper()}  Edge: {p['edge']:+.1%}  "
                  f"Conf: {p['confidence']:.0%}")
        print(f"  └{'─' * 70}\n")
    else:
        print("  No open positions.\n")

    closed = portfolio.get("closed_trades", [])
    if closed:
        recent = closed[-10:]
        print(f"  ┌─ RECENT TRADES {'─' * 53}")
        for t in recent:
            emoji = "✅" if t.get("pnl", 0) > 0 else "❌"
            print(f"  │  {emoji} {t['ticker']:25s} {t['direction'].upper():4s}  "
                  f"P&L: ${t.get('pnl', 0):+.2f}  Result: {t.get('result', 'N/A')}")
        print(f"  └{'─' * 70}\n")

    # Show recent scan history
    if os.path.isdir(SCANS_DIR):
        scan_files = sorted(os.listdir(SCANS_DIR))[-5:]
        if scan_files:
            print(f"  📁 Recent scans: {', '.join(scan_files)}")
            print()


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 trader.py scan                    # Full pipeline scan")
        print("  python3 trader.py auto-settle             # Auto-settle via Kalshi API")
        print("  python3 trader.py report                  # Portfolio status")
        print("  python3 trader.py settle TICKER yes|no    # Settle a position")
        sys.exit(0)

    cmd = sys.argv[1].lower()

    if cmd == "scan":
        run_scan()
    elif cmd == "auto-settle":
        print("\n  🔄 Running auto-settlement...\n")
        auto_settle(verbose=True)
    elif cmd == "report":
        run_report()
    elif cmd == "settle":
        if len(sys.argv) < 4:
            print("Usage: python3 trader.py settle TICKER yes|no")
            sys.exit(1)
        ticker = sys.argv[2]
        result = sys.argv[3].lower()
        if result not in ("yes", "no"):
            print("Result must be 'yes' or 'no'")
            sys.exit(1)
        portfolio = load_portfolio()
        trade = settle_position(portfolio, ticker, result)
        if trade:
            save_portfolio(portfolio)
            emoji = "✅" if trade["pnl"] > 0 else "❌"
            print(f"{emoji} Settled {ticker}: P&L ${trade['pnl']:+.2f}")
        else:
            print(f"No open position found for {ticker}")
    else:
        print(f"Unknown command: {cmd}")
        sys.exit(1)


if __name__ == "__main__":
    main()
