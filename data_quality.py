"""
Data Quality Gates

Validates feature availability and freshness before model execution.
If critical features are missing or stale, blocks the model from trading.
"""

import logging
from datetime import datetime, timedelta
from typing import Optional

logger = logging.getLogger(__name__)

# Staleness thresholds
MAX_STALE_MONTHLY = 50  # days - monthly indicators can be 30-45 days old legitimately
MAX_STALE_WEEKLY = 21   # days - weekly series with ~2 week publication lag (e.g. CCSA)
MAX_STALE_DAILY = 3     # days - for daily indicators

# Per-series overrides (some OECD series have longer publication lags)
STALENESS_OVERRIDES = {
    "USACSCICP02STSAM": 90,  # OECD Consumer Confidence — publishes with 60-90 day lag
}

# Critical features per model - if ANY critical feature fails, NO TRADE
CRITICAL_FEATURES = {
    "nfp": {
        "adp": {"series": "ADPMNUSNERSA", "frequency": "monthly", "label": "ADP Employment"},
        "initial_claims": {"series": "ICSA", "frequency": "weekly", "label": "Initial Jobless Claims"},
    },
    "cpi": {
        "shelter": {"series": "CUSR0000SAH1", "frequency": "monthly", "label": "Shelter CPI"},
        "core_cpi": {"series": "CPILFESL", "frequency": "monthly", "label": "Core CPI"},
        "cleveland_nowcast": {"series": None, "frequency": "weekly", "label": "Cleveland Fed Nowcast"},  # special handling
    },
    "fed": {
        "yield_curve": {"series": "T10Y2Y", "frequency": "daily", "label": "10Y-2Y Yield Spread"},
        "unemployment": {"series": "UNRATE", "frequency": "monthly", "label": "Unemployment Rate"},
    },
}

# Optional features per model - logged but don't block
OPTIONAL_FEATURES = {
    "nfp": {
        "continued_claims": {"series": "CCSA", "frequency": "weekly", "label": "Continued Claims"},
        "regional_fed_empire": {"series": "GACDISA066MSFRBNY", "frequency": "monthly", "label": "Empire State Mfg"},
        "regional_fed_philly": {"series": "GACDFSA066MSFRBPHI", "frequency": "monthly", "label": "Philly Fed Mfg"},
        "temp_help": {"series": "TEMPHELPS", "frequency": "monthly", "label": "Temp Help Services"},
        "consumer_confidence": {"series": "USACSCICP02STSAM", "frequency": "monthly", "label": "Consumer Confidence (OECD)"},
    },
    "cpi": {
        "food": {"series": "CPIUFDSL", "frequency": "monthly", "label": "Food CPI"},
        "gasoline": {"series": "GASREGW", "frequency": "weekly", "label": "Gasoline Price"},
        "ppi": {"series": "PPIACO", "frequency": "monthly", "label": "PPI"},
        "breakeven_5y": {"series": "T5YIE", "frequency": "daily", "label": "5Y Breakeven"},
    },
    "fed": {
        "core_pce": {"series": "PCEPILFE", "frequency": "monthly", "label": "Core PCE"},
        "anfci": {"series": "ANFCI", "frequency": "weekly", "label": "Adjusted NFCI"},
        "hy_oas": {"series": "BAMLH0A0HYM2", "frequency": "daily", "label": "HY Credit Spreads"},
        "u6": {"series": "U6RATE", "frequency": "monthly", "label": "U-6 Rate"},
        "jolts": {"series": "JTSJOL", "frequency": "monthly", "label": "JOLTS Openings"},
        "wages": {"series": "CES0500000003", "frequency": "monthly", "label": "Avg Hourly Earnings"},
    },
}


def _max_age_days(frequency: str, series_id: str = None) -> int:
    """Get max staleness in days, with per-series overrides."""
    if series_id and series_id in STALENESS_OVERRIDES:
        return STALENESS_OVERRIDES[series_id]
    if frequency == "daily":
        return MAX_STALE_DAILY
    elif frequency == "weekly":
        return MAX_STALE_WEEKLY
    else:
        return MAX_STALE_MONTHLY


def check_feature(fred_client, feature_name: str, feature_config: dict) -> dict:
    """
    Check a single feature's availability and freshness.
    Returns a status dict.
    """
    series_id = feature_config.get("series")
    label = feature_config.get("label", feature_name)
    frequency = feature_config.get("frequency", "monthly")
    max_age = _max_age_days(frequency, series_id)

    result = {
        "name": feature_name,
        "label": label,
        "series": series_id,
        "status": "failed",
        "value": None,
        "date": None,
        "age_days": None,
        "max_age_days": max_age,
        "message": "",
    }

    if series_id is None:
        # Special feature (e.g. Cleveland nowcast) - can't check via FRED
        result["status"] = "skip"
        result["message"] = f"{label}: special source, not FRED-checkable"
        return result

    try:
        data = fred_client.get_series(series_id, limit=1)
        if not data:
            result["status"] = "failed"
            result["message"] = f"{label}: no data returned"
            return result

        obs = data[0]
        result["value"] = obs["value"]
        result["date"] = obs["date"]

        obs_date = datetime.strptime(obs["date"], "%Y-%m-%d")
        age = (datetime.utcnow() - obs_date).days
        result["age_days"] = age

        if age > max_age:
            result["status"] = "stale"
            result["message"] = f"{label} stale ({age} days old, max {max_age})"
        else:
            result["status"] = "ok"
            result["message"] = f"{label}: OK (value={obs['value']}, {age}d old)"

    except Exception as e:
        result["status"] = "failed"
        result["message"] = f"{label}: fetch error — {e}"

    return result


def run_data_quality_gate(model_name: str, fred_client) -> dict:
    """
    Run data quality checks for a model.
    
    Returns:
        dict with:
            - passed (bool): whether the model is allowed to trade
            - report (list[dict]): per-feature status
            - blocked_reasons (list[str]): why it was blocked (if any)
            - summary (str): human-readable summary
    """
    critical = CRITICAL_FEATURES.get(model_name, {})
    optional = OPTIONAL_FEATURES.get(model_name, {})

    report = []
    blocked_reasons = []
    passed = True

    # Check critical features
    for fname, fconfig in critical.items():
        result = check_feature(fred_client, fname, fconfig)
        result["critical"] = True
        report.append(result)

        if result["status"] in ("failed", "stale"):
            passed = False
            blocked_reasons.append(f"DATA GATE: {model_name.upper()} model blocked — {result['message']}")
            logger.warning(blocked_reasons[-1])
        elif result["status"] == "ok":
            logger.info(f"DATA GATE: {model_name.upper()} — {result['message']}")

    # Check optional features (log only, don't block)
    for fname, fconfig in optional.items():
        result = check_feature(fred_client, fname, fconfig)
        result["critical"] = False
        report.append(result)

        if result["status"] in ("failed", "stale"):
            logger.info(f"DATA GATE: {model_name.upper()} — optional {result['message']}")
        elif result["status"] == "ok":
            logger.debug(f"DATA GATE: {model_name.upper()} — {result['message']}")

    ok_count = sum(1 for r in report if r["status"] == "ok")
    total = len(report)
    summary = (
        f"DATA GATE: {model_name.upper()} — {ok_count}/{total} features OK"
        + (f" — BLOCKED: {'; '.join(blocked_reasons)}" if not passed else " — PASSED")
    )

    return {
        "passed": passed,
        "report": report,
        "blocked_reasons": blocked_reasons,
        "summary": summary,
    }
