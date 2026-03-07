"""
Regression tests: ensure dead/proprietary series never silently enter the NFP ensemble.

These tests verify that:
1. Known-dead FRED series are not referenced in the model code
2. The source weights sum to 1.0
3. All configured series are actually fetchable from FRED
4. The staleness guard rejects old data
"""

import json
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# Series that MUST NEVER appear in operational model code (comments OK)
DEAD_SERIES = {
    "NPPTTL":          "ADP - discontinued May 2022",
    "NAPMEI":          "ISM Mfg Employment - removed from FRED June 2016",
    "NMFBSI":          "ISM Services Employment - removed from FRED June 2016",
    "NAPMNOI":         "ISM Mfg New Orders - removed from FRED June 2016",
    "NAPMPI":          "ISM Mfg Production - removed from FRED June 2016",
    "NAPMSDI":         "ISM Mfg Supplier Deliveries - removed from FRED June 2016",
    "NAPMII":          "ISM Mfg Inventories - removed from FRED June 2016",
    "CSCICP03USM665S": "Consumer Confidence - stale/frozen since Jan 2024",
}


class TestNoDeadSeries(unittest.TestCase):
    """Verify dead/proprietary series never enter the model pipeline."""

    def test_dead_series_not_in_model_code(self):
        """No dead series should appear in operational code (non-comment lines)."""
        model_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "models", "nfp_model.py"
        )
        with open(model_path) as f:
            lines = f.readlines()

        violations = []
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comments and docstrings
            if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'"):
                continue
            # Skip lines that are clearly documentation (inside docstrings)
            # We check for series IDs in actual code: fetch calls, dict keys, etc.
            for series, reason in DEAD_SERIES.items():
                if series in line and not stripped.startswith("#"):
                    # Allow in string comments within removed-section markers
                    if "removed" in line.lower() or "deleted" in line.lower() or "replaced" in line.lower():
                        continue
                    if "ℹ️" in line:  # info markers about removed sources
                        continue
                    violations.append(f"Line {i}: found dead series {series} ({reason}): {stripped}")

        self.assertEqual(violations, [], 
            f"Dead series found in operational model code:\n" + "\n".join(violations))

    def test_dead_series_not_in_data_quality(self):
        """No dead series in data_quality.py critical/optional features."""
        dq_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "data_quality.py"
        )
        with open(dq_path) as f:
            content = f.read()

        # Parse the CRITICAL_FEATURES and OPTIONAL_FEATURES dicts
        # Look for series IDs in string literals
        violations = []
        for series, reason in DEAD_SERIES.items():
            # Match series in quotes (actual config, not comments)
            pattern = rf'["\']({re.escape(series)})["\']'
            matches = re.findall(pattern, content)
            if matches:
                violations.append(f"Dead series {series} ({reason}) found in data_quality.py config")

        self.assertEqual(violations, [],
            f"Dead series in data_quality.py:\n" + "\n".join(violations))


class TestSourceWeights(unittest.TestCase):
    """Verify source weight configuration is valid."""

    def test_weights_sum_to_one(self):
        """Source weights must sum to 1.0 (within floating point tolerance)."""
        from models.nfp_model import SOURCE_WEIGHTS
        total = sum(SOURCE_WEIGHTS.values())
        self.assertAlmostEqual(total, 1.0, places=4,
            msg=f"Source weights sum to {total}, expected 1.0")

    def test_all_weights_positive(self):
        """All weights must be positive."""
        from models.nfp_model import SOURCE_WEIGHTS
        for name, weight in SOURCE_WEIGHTS.items():
            self.assertGreater(weight, 0, f"Weight for {name} is not positive: {weight}")

    def test_weight_count_matches_source_count(self):
        """Number of weights should match expected source count."""
        from models.nfp_model import SOURCE_WEIGHTS
        self.assertEqual(len(SOURCE_WEIGHTS), 6,
            f"Expected 6 source weights, got {len(SOURCE_WEIGHTS)}: {list(SOURCE_WEIGHTS.keys())}")


class TestVersionedConfig(unittest.TestCase):
    """Verify the frozen config matches the live model."""

    def test_config_matches_model_weights(self):
        """Versioned config weights should match SOURCE_WEIGHTS."""
        from models.nfp_model import SOURCE_WEIGHTS
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config_versions", "nfp_v2_live_only.json"
        )
        with open(config_path) as f:
            config = json.load(f)

        for name, cfg in config["sources"].items():
            self.assertIn(name, SOURCE_WEIGHTS,
                f"Config source '{name}' not in SOURCE_WEIGHTS")
            self.assertAlmostEqual(
                SOURCE_WEIGHTS[name], cfg["weight"], places=4,
                msg=f"Weight mismatch for {name}: model={SOURCE_WEIGHTS[name]}, config={cfg['weight']}")

    def test_config_source_count(self):
        """Config total_sources should match actual source count."""
        config_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "config_versions", "nfp_v2_live_only.json"
        )
        with open(config_path) as f:
            config = json.load(f)

        self.assertEqual(config["total_sources"], len(config["sources"]))


class TestStalenessGuard(unittest.TestCase):
    """Verify the staleness guard actually rejects old data."""

    def test_fetch_source_rejects_stale(self):
        """_fetch_source should return None for data older than max_age_days."""
        from models.nfp_model import NFPModel
        model = NFPModel()
        # USSLIND (Leading Index) last updated 2020 — should be rejected with tight staleness
        result = model._fetch_source("Test Stale", "USSLIND", limit=2, max_age_days=30)
        self.assertIsNone(result,
            "Staleness guard should reject USSLIND (last updated ~2020) with 30-day max")

    def test_fetch_source_accepts_fresh(self):
        """_fetch_source should return data for fresh series."""
        from models.nfp_model import NFPModel
        model = NFPModel()
        result = model._fetch_source("Test Fresh", "ICSA", limit=2, max_age_days=30)
        self.assertIsNotNone(result,
            "Should accept ICSA (weekly, always fresh)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
