"""
Base model class for all economic prediction models.

Provides common infrastructure: config loading, Kalshi client,
signal generation, and trade recommendation formatting.
"""

import json
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional

import pytz

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EST = pytz.timezone("US/Eastern")

logger = logging.getLogger(__name__)


def load_config() -> dict:
    with open(os.path.join(BASE_DIR, "config.json")) as f:
        return json.load(f)


@dataclass
class Signal:
    """A trading signal from a model."""
    model: str
    ticker: str
    market_title: str
    direction: str  # "yes" or "no"
    model_prob: float  # our predicted probability
    market_prob: float  # current market implied probability
    edge: float  # model_prob - market_prob (for yes), or inverse
    confidence: float  # overall confidence 0-1
    kelly_fraction: float  # Kelly criterion suggested fraction
    recommended_size: float  # dollar amount to risk
    reasoning: str
    timestamp: str = field(default_factory=lambda: datetime.now(EST).isoformat())
    data_sources: dict = field(default_factory=dict)
    data_quality_report: dict = field(default_factory=dict)
    sizing_notes: str = field(default="")
    _force_disabled: bool = field(default=False, repr=False)

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def is_actionable(self) -> bool:
        if self._force_disabled:
            return False
        cfg = load_config()
        return (
            self.confidence >= cfg["min_confidence"]
            and self.edge >= cfg["min_edge"]
            and self.recommended_size > 0
        )

    @property
    def is_watchlist(self) -> bool:
        """Signal is interesting but not yet actionable."""
        cfg = load_config()
        watchlist_edge = cfg.get("watchlist_min_edge", 0.01)
        return (
            not self.is_actionable
            and self.edge >= watchlist_edge
        )

    @property
    def status(self) -> str:
        if self.is_actionable:
            return "TRIGGERED"
        elif self.is_watchlist:
            return "WATCHLIST"
        else:
            return "NO_EDGE"


class BaseModel(ABC):
    """Base class for all economic prediction models."""

    NAME = "base"

    def __init__(self):
        self.config = load_config()
        self.logger = logging.getLogger(f"econ-trader.{self.NAME}")

    def kalshi_fee(self, price: float) -> float:
        """Calculate Kalshi fee for a given contract price (0-1)."""
        return self.config["fee_rate"] * price * (1 - price)

    def kelly_criterion(self, prob: float, odds_price: float) -> float:
        """
        Kelly criterion for binary markets with fractional Kelly safety cap.
        prob: our estimated probability of YES
        odds_price: current YES price (0-1)

        Returns fraction of bankroll to bet (0 if no edge).
        """
        if odds_price <= 0 or odds_price >= 1 or prob <= 0 or prob >= 1:
            return 0.0

        fee = self.kalshi_fee(odds_price)
        net_price = odds_price + fee  # effective cost

        if net_price >= 1:
            return 0.0

        f = (prob - net_price) / (1 - net_price)
        # Half-Kelly base
        f = max(0, f) * 0.5
        # Apply fractional Kelly multiplier (default 0.1 = 10% Kelly)
        kelly_multiplier = self.config.get("kelly_fraction_multiplier", 0.1)
        f = f * kelly_multiplier
        # Cap at max position
        max_pct = self.config["max_position_pct"]
        return min(f, max_pct)

    def recommended_bet_size(self, kelly_frac: float) -> float:
        """Convert Kelly fraction to dollar amount with hard caps."""
        bankroll = self.config["bankroll"]
        raw_size = kelly_frac * bankroll

        max_trade_size = self.config.get("max_trade_size", 25.0)
        sizing_note = ""

        if raw_size > max_trade_size:
            sizing_note = f"SIZING: Kelly suggested ${raw_size:.2f}, capped to ${max_trade_size:.2f} (max_trade_size)"
            self.logger.info(sizing_note)
            raw_size = max_trade_size

        # Store sizing note for signal
        self._last_sizing_note = sizing_note
        return round(raw_size, 2)

    def check_portfolio_exposure(self, proposed_size: float) -> tuple[float, str]:
        """Check total portfolio exposure cap. Returns (allowed_size, note)."""
        try:
            with open(os.path.join(BASE_DIR, "portfolio.json")) as f:
                portfolio = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return proposed_size, ""

        bankroll = self.config["bankroll"]
        max_exposure_pct = self.config.get("max_portfolio_exposure", 0.30)
        max_exposure = bankroll * max_exposure_pct

        current_invested = sum(p.get("total_cost", 0) for p in portfolio.get("positions", []))
        remaining_capacity = max(0, max_exposure - current_invested)

        if proposed_size > remaining_capacity:
            note = (f"SIZING: Proposed ${proposed_size:.2f} exceeds portfolio exposure cap "
                    f"(invested=${current_invested:.2f}, cap={max_exposure_pct:.0%} of ${bankroll:.0f}=${max_exposure:.0f}), "
                    f"reduced to ${remaining_capacity:.2f}")
            self.logger.info(note)
            return round(remaining_capacity, 2), note
        return proposed_size, ""

    def make_signal(self, ticker: str, title: str, direction: str,
                    model_prob: float, market_prob: float,
                    reasoning: str, data_sources: dict = None,
                    data_quality_report: dict = None) -> Signal:
        """Create a trading signal with sizing caps applied."""
        self._last_sizing_note = ""

        # model_prob / market_prob are the probability of the selected side
        # (YES if direction=yes, NO if direction=no).
        # Keep edge + Kelly on that same side to avoid sign inversions for NO.
        edge = model_prob - market_prob
        kelly = self.kelly_criterion(model_prob, market_prob)

        size = self.recommended_bet_size(kelly)
        sizing_note = getattr(self, "_last_sizing_note", "")

        # Check portfolio exposure cap
        size, exposure_note = self.check_portfolio_exposure(size)
        if exposure_note:
            sizing_note = (sizing_note + " | " + exposure_note) if sizing_note else exposure_note

        return Signal(
            model=self.NAME,
            ticker=ticker,
            market_title=title,
            direction=direction,
            model_prob=model_prob,
            market_prob=market_prob,
            edge=edge,
            confidence=min(abs(edge) / 0.15 + 0.5, 0.99),  # scale edge to confidence
            kelly_fraction=kelly,
            recommended_size=size,
            reasoning=reasoning,
            data_sources=data_sources or {},
            data_quality_report=data_quality_report or {},
            sizing_notes=sizing_note,
        )

    def save_snapshot(self, model_name: str, signals: list, raw_features: dict,
                      intermediate_values: dict = None):
        """Save a feature snapshot for backtest verification."""
        snapshots_dir = os.path.join(BASE_DIR, "snapshots")
        os.makedirs(snapshots_dir, exist_ok=True)

        now = datetime.now(EST)
        filename = f"{now.strftime('%Y-%m-%d')}_{model_name}.json"
        filepath = os.path.join(snapshots_dir, filename)

        snapshot = {
            "timestamp": now.isoformat(),
            "model": model_name,
            "raw_features": raw_features,
            "intermediate_values": intermediate_values or {},
            "signals": [s.to_dict() for s in signals] if signals else [],
            "signal_count": len(signals) if signals else 0,
            "actionable_count": sum(1 for s in signals if s.is_actionable) if signals else 0,
        }

        # Append if file exists (multiple runs per day)
        existing = []
        if os.path.exists(filepath):
            try:
                with open(filepath) as f:
                    existing = json.load(f)
                if not isinstance(existing, list):
                    existing = [existing]
            except (json.JSONDecodeError, TypeError):
                existing = []

        existing.append(snapshot)
        with open(filepath, "w") as f:
            json.dump(existing, f, indent=2, default=str)

        self.logger.info(f"Snapshot saved to {filepath}")

    @abstractmethod
    def analyze(self, markets: list[dict]) -> list[Signal]:
        """
        Run model analysis on relevant markets.
        Returns list of trading signals.
        """
        pass

    @abstractmethod
    def get_relevant_markets(self, all_markets: list[dict]) -> list[dict]:
        """Filter markets relevant to this model."""
        pass

    def run(self, markets: list[dict]) -> list[Signal]:
        """Full pipeline: filter markets, analyze, return signals."""
        relevant = self.get_relevant_markets(markets)
        if not relevant:
            self.logger.info(f"No relevant {self.NAME} markets found")
            return []
        self.logger.info(f"Found {len(relevant)} {self.NAME} markets to analyze")
        return self.analyze(relevant)

    def print_signals(self, signals: list[Signal]):
        """Pretty-print signals to stdout."""
        if not signals:
            print(f"  No {self.NAME.upper()} signals generated.\n")
            return

        for s in signals:
            actionable = "✅ ACTIONABLE" if s.is_actionable else "⏸️  Below threshold"
            print(f"  {'═' * 60}")
            print(f"  {s.model.upper()} Signal — {actionable}")
            print(f"  Market:     {s.ticker}")
            print(f"  Question:   {s.market_title}")
            print(f"  Direction:  {s.direction.upper()}")
            print(f"  Model Prob: {s.model_prob:.1%}")
            print(f"  Market:     {s.market_prob:.1%}")
            print(f"  Edge:       {s.edge:+.1%}")
            print(f"  Confidence: {s.confidence:.1%}")
            print(f"  Kelly:      {s.kelly_fraction:.2%} → ${s.recommended_size:.2f}")
            print(f"  Reasoning:  {s.reasoning}")
            if s.data_sources:
                print(f"  Data:       {json.dumps(s.data_sources, default=str)}")
            print()
