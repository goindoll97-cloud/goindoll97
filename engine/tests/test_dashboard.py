import unittest

import pandas as pd

from dashboard_engine import (
    DEFAULT_CASES,
    DEFAULT_RULES,
    apply_threshold_table,
    calculate_framework,
    prepare_inventory,
    read_uploaded_table,
    screen_inventory,
)


class DashboardEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules_raw = read_uploaded_table(None, DEFAULT_RULES)
        cls.cases_raw = read_uploaded_table(None, DEFAULT_CASES)
        cls.fw = calculate_framework(cls.rules_raw, cls.cases_raw)

    def test_default_framework_counts(self):
        self.assertEqual(len(self.fw["rules"]), 5)
        self.assertEqual(len(self.fw["cases"]), 60)
        self.assertEqual(int(self.fw["detail"]["decided"].sum()), 58)
        self.assertEqual(int(self.fw["promotion"]["promotion_eligible"].sum()), 5)

    def test_structural_match_routes_to_review_without_threshold(self):
        raw = pd.DataFrame([{
            "material_id": "T1",
            "chemical_name": "Aniline hydrochloride",
            "cas": "142-04-1",
            "smiles": "C1=CC=C(C=C1)N.Cl",
            "concentration_pct": 1.0,
        }])
        inventory, _ = prepare_inventory(raw, fetch_pubchem=False)
        detail, summary = screen_inventory(inventory, self.fw["rules"], self.fw["promotion"])
        self.assertTrue(detail["salt_membership"].eq(True).any())
        self.assertEqual(summary.loc[0, "final_decision"], "REVIEW_REQUIRED")

    def test_threshold_is_applied_after_structural_match(self):
        raw = pd.DataFrame([{
            "material_id": "T2",
            "chemical_name": "Aniline hydrochloride",
            "cas": "142-04-1",
            "smiles": "C1=CC=C(C=C1)N.Cl",
            "concentration_pct": 1.0,
        }])
        thresholds = pd.DataFrame([{
            "designation_id": "97-1-156",
            "threshold_pct": 0.5,
            "threshold_source": "TEST_ONLY",
        }])
        inventory, _ = prepare_inventory(raw, fetch_pubchem=False)
        rules = apply_threshold_table(self.fw["rules"], thresholds)
        _, summary = screen_inventory(inventory, rules, self.fw["promotion"])
        self.assertEqual(summary.loc[0, "final_decision"], "REGULATORY_CANDIDATE")


if __name__ == "__main__":
    unittest.main()
