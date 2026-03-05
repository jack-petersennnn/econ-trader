# Econ Trader — Economics Prediction Trading Bot

Paper trading bot for Kalshi economics prediction markets. Uses nowcasting models to find edge on CPI, NFP, and Fed rate markets.

## Quick Start

```bash
# Scan for active economics markets
python3 scanner.py

# Run full pipeline (scan + models + paper trades)
python3 trader.py scan

# Check portfolio
python3 trader.py report

# Run individual models
python3 models/cpi_model.py
python3 models/nfp_model.py
python3 models/fed_model.py

# Daily/weekly reports
python3 report.py
python3 report.py weekly

# Settle a position manually
python3 trader.py settle TICKER yes
```

## Setup

### 1. FRED API Key (Required)
Get a free key at https://fred.stlouisfed.org/docs/api/api_key.html

Then either:
```bash
export FRED_API_KEY=your_key_here
```
Or update `config.json`:
```json
"fred_api_key": "your_key_here"
```

### 2. Dependencies
Only uses Python standard library + `pytz`:
```bash
pip install pytz
```

## Architecture

### Models

| Model | Data Sources | What It Does |
|-------|-------------|--------------|
| **CPI** | Cleveland Fed Nowcast, FRED (PPI, Shelter) | Estimates next CPI print, compares to Kalshi brackets |
| **NFP** | FRED (ADP, Jobless Claims, ISM) | Estimates next jobs report, compares to Kalshi brackets |
| **Fed** | CME FedWatch, Kalshi | Finds divergence between CME futures-implied and Kalshi odds |

### Trading Rules
- **Bankroll:** $500 (paper)
- **Max position:** 10% of bankroll per trade
- **Min edge:** 5% (model probability - market probability)
- **Min confidence:** 60%
- **Position sizing:** Half-Kelly criterion
- **Fees:** 7% × price × (1 - price)

### Data Flow
```
FRED API ─────┐
Cleveland Fed ─┤──→ Models ──→ Signals ──→ Trader ──→ Portfolio
CME FedWatch ──┤                              ↓
Kalshi API ────┘                          portfolio.json
```

## Files

| File | Purpose |
|------|---------|
| `scanner.py` | Scans Kalshi for economics markets |
| `trader.py` | Paper trading engine + CLI |
| `report.py` | Portfolio reports |
| `config.json` | All settings |
| `portfolio.json` | Positions and P&L |
| `models/base_model.py` | Base class, Kelly criterion, signal generation |
| `models/cpi_model.py` | CPI nowcasting |
| `models/nfp_model.py` | NFP prediction |
| `models/fed_model.py` | Fed rate arbitrage |
| `data/fred_client.py` | FRED API client |
| `data/kalshi_client.py` | Kalshi market reader |
| `data/cme_scraper.py` | CME FedWatch scraper |

## Notes

- **Paper trading only** — no real money at risk
- CME FedWatch scraping may be blocked; falls back to placeholder data
- Kalshi API may require authentication for full market data
- All times in US/Eastern
- Models use normal distribution approximations for bracket probability estimation
