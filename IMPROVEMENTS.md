# Econ Trader Improvements — 2026-03-02

## 1. Auto-Settlement System (`auto_settler.py`)

**Problem:** Positions required manual settlement via `trader.py settle TICKER yes|no`.

**Solution:**
- New `auto_settler.py` module checks all open positions against Kalshi API (`GET /markets/{ticker}`) for settlement status
- Looks for `status` = "settled"/"closed"/"finalized" and `result` = "yes"/"no"
- Automatically updates portfolio.json: marks positions won/lost, updates cash, P&L, win rate
- Logs all settlements with timestamps and `auto_settled: true` flag
- New command: `python3 trader.py auto-settle`
- Integrated into scan pipeline: auto-settle runs BEFORE scanning for new trades
- API errors are logged as warnings but don't block the pipeline

## 2. Data Quality Gates (`data_quality.py`)

**Problem:** Models could trade on stale or missing data, the #1 P&L risk identified by reviewers.

**Solution:**
- New `data_quality.py` module with per-model critical/optional feature definitions
- **NFP critical:** ADP, initial_claims, ISM services employment
- **CPI critical:** shelter CPI, core CPI, Cleveland Fed nowcast
- **Fed critical:** yield curve (10Y-2Y), unemployment rate
- Staleness thresholds: monthly data >7 days = stale, weekly >2 days, daily >1 day
- If ANY critical feature is missing or stale → model returns NO signals
- Clear log messages: `"DATA GATE: NFP model blocked — ADP data stale (14 days old, max 7)"`
- `data_quality_report` dict added to each Signal for audit trail
- Each model's `analyze()` calls the gate before running

## 3. Feature Snapshot Logging (`snapshots/`)

**Problem:** No way to verify what data the model saw at decision time for backtesting.

**Solution:**
- `BaseModel.save_snapshot()` method saves complete snapshots to `snapshots/YYYY-MM-DD_MODEL.json`
- Each snapshot includes: all raw feature values, intermediate computed values, model output, market prices, timestamps, data quality report
- Appends if file exists (multiple runs per day stored as JSON array)
- Called at end of every model's `analyze()` — for both actionable and non-actionable signals
- Enables post-hoc verification: "did the model use the right data at the right time?"

## 4. Fractional Kelly Safety Cap

**Problem:** Full Kelly sizing on uncalibrated probabilities is dangerous — both reviewers flagged this.

**Solution — three layers of protection:**

1. **Fractional Kelly multiplier** (`kelly_fraction_multiplier` in config.json, default: 0.1 = 10% Kelly)
   - Applied inside `kelly_criterion()` after the half-Kelly base
   - Net effect: 5% of full Kelly (0.5 × 0.1)

2. **Hard dollar cap per trade** (`max_trade_size` in config.json, default: $25)
   - Regardless of Kelly output, no single trade exceeds this
   - Logged: `"SIZING: Kelly suggested $85, capped to $25 (max_trade_size)"`

3. **Portfolio exposure cap** (`max_portfolio_exposure` in config.json, default: 30%)
   - Total invested across all positions capped at 30% of bankroll ($150 on $500)
   - `check_portfolio_exposure()` reduces proposed trade size if cap would be breached

Config additions:
```json
"kelly_fraction_multiplier": 0.1,
"max_trade_size": 25.0,
"max_portfolio_exposure": 0.30
```
