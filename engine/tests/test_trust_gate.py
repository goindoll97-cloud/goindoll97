import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from company_engine import (
    load_approved_master,
    load_promoted_salt_rules,
    prepare_company_inventory,
    screen_company_inventory,
)
from cross_regime import add_regulatory_category_columns, screen_cross_regimes
from result_store import list_saved_results, read_saved_result, save_result_workbook
from trust_gate import (
    REGIME_ORDER,
    apply_trust_gate,
    build_category_search_log,
    build_legal_basis,
    cas_gate,
    cross_rule_availability,
    structure_evidence_status,
)


APPROVED_EVIDENCE = {
    "material_id": "A1",
    "chemical_name": "Aniline",
    "cas": "62-53-3",
    "designation_id": "97-1-156",
    "regulatory_substance_name": "아닐린",
    "match_type": "직접 CAS",
    "scope_type": "DIRECT_CAS",
    "notice": "2026-5",
    "source_text": "아닐린 및 그 염류",
    "regulatory_source": "",
    "authoritative_source": "True",
    "evidence_status": "CONFIRMED_RULE_MATCH",
}


def summary_row(**overrides):
    row = {
        "material_id": "A1",
        "input_chemical_name": "Aniline",
        "input_cas": "62-53-3",
        "concentration_pct": 15.0,
        "identity_status": "PUBCHEM_EXACT_CAS_VERIFIED",
        "identity_source": "PUBCHEM_PUG_REST",
        "smiles": "Nc1ccccc1",
        "structure_evidence_status": "PUBCHEM_CAS_VERIFIED",
        "final_decision": "규제 해당",
        "acute_decision": "기준 미만",
        "chronic_decision": "해당",
        "eco_decision": "기준 미만",
        "overall_decision": "규제 해당",
        "hh_direct_hits": 1,
        "hh_structure_hits": 0,
        "hh_structure_unverified_hits": 0,
        "hh_contextual_hits": 0,
        "hh_excluded_hits": 0,
    }
    for regime in REGIME_ORDER[1:]:
        row[f"{regime}_decision"] = "법규 데이터 확인 필요"
    row.update(overrides)
    return row


CURRENT_API = {"sync_status": "CURRENT", "official_notice": "2026-5"}


def run_gate(summary, evidence, cross_detail=None, api_status=None, availability=None):
    availability = availability if availability is not None else cross_rule_availability(
        pd.DataFrame(), pd.DataFrame(), pd.DataFrame()
    )
    category_log = build_category_search_log(
        summary, cross_detail, availability, "2026-5", 1451
    )
    legal_basis = build_legal_basis(evidence, cross_detail, "2026-5")
    gated, gate_log = apply_trust_gate(
        summary, evidence, cross_detail, category_log, legal_basis, api_status or CURRENT_API
    )
    return gated, gate_log, category_log, legal_basis


class CasAndStructureGateTest(unittest.TestCase):
    def test_invalid_cas_fails_stage_one(self):
        self.assertEqual(cas_gate({"input_cas": "000-00-0"})["code"], "CAS_INVALID_CHECKSUM")
        self.assertEqual(cas_gate({"input_cas": ""})["code"], "CAS_MISSING")

    def test_pubchem_synonym_mismatch_is_held(self):
        result = cas_gate(
            {"input_cas": "62-53-3", "identity_status": "PUBCHEM_CAS_SYNONYM_MISMATCH"}
        )
        self.assertEqual(result["stage_status"], "보류")

    def test_user_smiles_needs_pubchem_confirmation(self):
        unverified = {
            "smiles": "Nc1ccccc1",
            "structure_smiles_source": "USER_SUPPLIED",
            "structure_status": "USER_SMILES_PRESENT",
        }
        self.assertEqual(structure_evidence_status(unverified), "USER_SMILES_UNVERIFIED")

        confirmed = {
            "smiles": "Nc1ccccc1",
            "structure_smiles_source": "USER_SUPPLIED",
            "structure_status": "PUBCHEM_EXACT_CAS_VERIFIED",
            "pubchem_connectivity_smiles": "C1=CC=C(C=C1)N",
        }
        self.assertEqual(structure_evidence_status(confirmed), "USER_SMILES_PUBCHEM_CONFIRMED")

        conflicting = dict(confirmed, pubchem_connectivity_smiles="CCO")
        self.assertEqual(structure_evidence_status(conflicting), "USER_SMILES_PUBCHEM_CONFLICT")


class CategorySearchLogTest(unittest.TestCase):
    def test_every_material_is_searched_in_all_four_categories(self):
        summary = pd.DataFrame([summary_row(), summary_row(material_id="A2")])
        _, _, category_log, _ = run_gate(summary, pd.DataFrame([APPROVED_EVIDENCE]))
        self.assertEqual(len(category_log), 8)
        for material_id in ["A1", "A2"]:
            rows = category_log[category_log["material_id"].eq(material_id)]
            self.assertEqual(rows["regime"].tolist(), REGIME_ORDER)

    def test_missing_cross_regime_rules_are_reported_as_coverage_gaps(self):
        summary = pd.DataFrame([summary_row()])
        _, _, category_log, _ = run_gate(summary, pd.DataFrame([APPROVED_EVIDENCE]))
        human = category_log[category_log["regime"].eq("HUMAN_HAZARD")].iloc[0]
        self.assertEqual(human["coverage_status"], "COVERAGE_OK")
        others = category_log[category_log["regime"].ne("HUMAN_HAZARD")]
        self.assertTrue(others["coverage_status"].eq("COVERAGE_NO_RULE_DATA").all())

    def test_candidate_only_rules_do_not_count_as_full_coverage(self):
        candidates = pd.DataFrame(
            [{"regime": "RESTRICTED", "rule_id": "API-1", "direct_cas": "62-53-3"}]
        )
        availability = cross_rule_availability(pd.DataFrame(), candidates, pd.DataFrame())
        self.assertEqual(availability["RESTRICTED"]["candidate_rows"], 1)
        summary = pd.DataFrame([summary_row()])
        _, _, category_log, _ = run_gate(
            summary, pd.DataFrame([APPROVED_EVIDENCE]), availability=availability
        )
        restricted = category_log[category_log["regime"].eq("RESTRICTED")].iloc[0]
        self.assertEqual(restricted["coverage_status"], "COVERAGE_CANDIDATE_ONLY")


class DecisionGateTest(unittest.TestCase):
    def test_human_hazard_review_needed_is_never_confirmed_as_nonregulated(self):
        # company_engine can leave the human-hazard side as "전문가 검토 필요"
        # (e.g. an anchor/contextual CAS reference inside a parent designation
        # that isn't a direct match) even when no cross-regime category has
        # anything to say. The gate must hold, not quietly confirm "비해당".
        summary = pd.DataFrame(
            [
                summary_row(
                    final_decision="전문가 검토 필요",
                    overall_decision="전문가 검토 필요",
                    acute_decision="규칙 없음",
                    chronic_decision="규칙 없음",
                    eco_decision="규칙 없음",
                    hh_direct_hits=0,
                    review_reason="CAS가 복합 규제범위 원문에 존재하지만 자동 확정되지 않았습니다.",
                )
            ]
        )
        gated, _, _, _ = run_gate(summary, pd.DataFrame())
        self.assertEqual(gated.loc[0, "gate_status"], "보류")
        self.assertNotEqual(gated.loc[0, "gate_decision"], "확인 범위 내 비해당")
        self.assertIn("복합 규제범위", gated.loc[0, "gate_hold_reason"])

    def test_positive_with_official_basis_is_auto_confirmed(self):
        summary = pd.DataFrame([summary_row()])
        gated, gate_log, _, legal_basis = run_gate(summary, pd.DataFrame([APPROVED_EVIDENCE]))
        self.assertEqual(gated.loc[0, "gate_status"], "자동 확정")
        self.assertEqual(gated.loc[0, "gate_decision"], "규제 해당(자동 확정)")
        self.assertEqual(legal_basis.loc[0, "basis_status"], "OFFICIAL_BASIS_VERIFIED")
        self.assertEqual(len(gate_log), 6)

    def test_positive_without_official_basis_is_held(self):
        evidence = pd.DataFrame([dict(APPROVED_EVIDENCE, source_text="", authoritative_source="")])
        gated, _, _, _ = run_gate(pd.DataFrame([summary_row()]), evidence)
        self.assertEqual(gated.loc[0, "gate_status"], "보류")
        self.assertEqual(
            gated.loc[0, "gate_decision"],
            "보류(규제 해당 후보·전문가 확인 필요)",
        )

    def test_positive_is_held_when_law_version_is_not_current(self):
        gated, _, _, _ = run_gate(
            pd.DataFrame([summary_row()]),
            pd.DataFrame([APPROVED_EVIDENCE]),
            api_status={"sync_status": "NOT_CHECKED"},
        )
        self.assertEqual(gated.loc[0, "gate_status"], "보류")
        self.assertIn("현행인지", gated.loc[0, "gate_hold_reason"])

    def test_complex_group_evidence_cannot_auto_confirm(self):
        evidence = pd.DataFrame([
            dict(
                APPROVED_EVIDENCE,
                match_type="검증된 구조범위 규칙",
                scope_type="COMPOUND_GROUP",
                acute_decision="해당",
            )
        ])
        gated, _, _, _ = run_gate(pd.DataFrame([summary_row()]), evidence)
        self.assertEqual(gated.loc[0, "gate_status"], "보류")
        self.assertIn("화합물군", gated.loc[0, "gate_hold_reason"])

    def test_structure_only_hit_without_verification_is_never_confirmed(self):
        evidence = pd.DataFrame(
            [
                dict(
                    APPROVED_EVIDENCE,
                    match_type="검증된 염류 구조",
                    evidence_status="STRUCTURE_UNVERIFIED_HOLD",
                )
            ]
        )
        summary = pd.DataFrame(
            [
                summary_row(
                    final_decision="전문가 검토 필요",
                    overall_decision="추가 확인 필요",
                    structure_evidence_status="USER_SMILES_UNVERIFIED",
                    chronic_decision="규칙 없음",
                    acute_decision="규칙 없음",
                    eco_decision="규칙 없음",
                )
            ]
        )
        gated, _, _, legal_basis = run_gate(summary, evidence)
        self.assertEqual(
            legal_basis.loc[0, "basis_status"], "BASIS_HOLD_STRUCTURE_UNVERIFIED"
        )
        self.assertEqual(gated.loc[0, "gate_status"], "보류")
        self.assertIn("보류", gated.loc[0, "gate_decision"])

    def test_negative_with_coverage_gap_is_held_not_declared_nonregulated(self):
        summary = pd.DataFrame(
            [
                summary_row(
                    final_decision="확인 범위 내 비해당",
                    overall_decision="확인 범위 내 비해당",
                    acute_decision="규칙 없음",
                    chronic_decision="규칙 없음",
                    eco_decision="규칙 없음",
                    hh_direct_hits=0,
                )
            ]
        )
        gated, _, _, _ = run_gate(summary, pd.DataFrame())
        self.assertEqual(gated.loc[0, "gate_decision"], "보류(비해당 단정 불가)")
        self.assertIn("커버리지", gated.loc[0, "gate_hold_reason"])

    def test_unverified_law_version_blocks_a_negative_conclusion(self):
        approved = pd.DataFrame(
            [
                {
                    "regime": regime,
                    "rule_id": f"R-{regime}",
                    "direct_cas": "1-1-1",
                    "approval_status": "APPROVED",
                }
                for regime in REGIME_ORDER[1:]
            ]
        )
        availability = cross_rule_availability(approved, pd.DataFrame(), pd.DataFrame())
        summary = pd.DataFrame(
            [
                summary_row(
                    final_decision="확인 범위 내 비해당",
                    overall_decision="확인 범위 내 비해당",
                    acute_decision="규칙 없음",
                    chronic_decision="규칙 없음",
                    eco_decision="규칙 없음",
                    hh_direct_hits=0,
                )
            ]
        )
        confirmed, _, _, _ = run_gate(
            summary, pd.DataFrame(), availability=availability, api_status=CURRENT_API
        )
        self.assertEqual(confirmed.loc[0, "gate_decision"], "확인 범위 내 비해당")

        held, _, _, _ = run_gate(
            summary,
            pd.DataFrame(),
            availability=availability,
            api_status={"sync_status": "UNVERIFIED"},
        )
        self.assertEqual(held.loc[0, "gate_decision"], "보류(비해당 단정 불가)")

    def test_identity_failure_is_named_distinctly(self):
        summary = pd.DataFrame(
            [
                summary_row(
                    input_cas="000-00-0",
                    final_decision="물질정보 확인 실패",
                    overall_decision="추가 확인 필요",
                    acute_decision="규칙 없음",
                    chronic_decision="규칙 없음",
                    eco_decision="규칙 없음",
                    hh_direct_hits=0,
                )
            ]
        )
        gated, _, _, _ = run_gate(summary, pd.DataFrame())
        self.assertEqual(gated.loc[0, "gate_decision"], "보류(물질식별 실패)")

    def test_api_candidate_match_is_held_as_unverified_candidate(self):
        cross_detail = pd.DataFrame(
            [
                {
                    "material_id": "A1",
                    "chemical_name": "Aniline",
                    "cas": "62-53-3",
                    "regime": "RESTRICTED",
                    "regime_label": "제한물질",
                    "decision": "해당 후보·검토 필요",
                    "rule_id": "API-1",
                    "designation_id": "",
                    "substance_name": "",
                    "scope_type": "DIRECT_CAS",
                    "missing_enterprise_fields": "",
                    "reason": "검증 전 후보",
                    "source_text": "공식 원문 후보",
                    "source_name": "",
                    "source_reference": "",
                    "official_effective_date": "",
                    "rule_source_status": "API_CANDIDATE",
                }
            ]
        )
        summary = pd.DataFrame(
            [
                summary_row(
                    final_decision="확인 범위 내 비해당",
                    acute_decision="규칙 없음",
                    chronic_decision="규칙 없음",
                    eco_decision="규칙 없음",
                    hh_direct_hits=0,
                    RESTRICTED_decision="해당 후보·검토 필요",
                    overall_decision="규제 해당 후보",
                )
            ]
        )
        gated, _, _, legal_basis = run_gate(summary, pd.DataFrame(), cross_detail=cross_detail)
        self.assertEqual(gated.loc[0, "gate_decision"], "보류(공식 근거 검증 전 후보)")
        self.assertEqual(legal_basis.loc[0, "basis_status"], "BASIS_CANDIDATE_UNVERIFIED")


class EndToEndGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master, cls.notice = load_approved_master()
        cls.salt_rules = load_promoted_salt_rules(cls.master)

    def test_full_flow_produces_gate_and_basis_records(self):
        inventory, _ = prepare_company_inventory(
            pd.DataFrame(
                [
                    {
                        "material_id": "E1",
                        "chemical_name": "Aniline",
                        "cas": "62-53-3",
                        "smiles": "Nc1ccccc1",
                        "concentration_pct": 15.0,
                    }
                ]
            ),
            fetch_pubchem=False,
        )
        inventory["structure_smiles_source"] = "PUBCHEM_PUG_REST"
        inventory["structure_status"] = "PUBCHEM_EXACT_CAS_VERIFIED"
        summary, evidence, _ = screen_company_inventory(
            inventory, self.master, self.salt_rules, self.notice
        )
        cross_summary, cross_detail = screen_cross_regimes(
            inventory, pd.DataFrame(), pd.DataFrame()
        )
        summary = summary.merge(cross_summary, on="material_id", how="left")
        summary["HUMAN_HAZARD_decision"] = summary["final_decision"]
        summary["overall_decision"] = summary["final_decision"]
        summary = add_regulatory_category_columns(summary)
        gated, gate_log, category_log, legal_basis = run_gate(
            summary, evidence, cross_detail=cross_detail
        )
        self.assertEqual(gated.loc[0, "gate_decision"], "규제 해당(자동 확정)")
        self.assertEqual(len(category_log), 4)
        self.assertEqual(gate_log["stage"].tolist(), ["S1", "S2", "S3", "S4", "S5", "S6"])
        self.assertTrue(
            legal_basis["basis_status"].eq("OFFICIAL_BASIS_VERIFIED").any()
        )


class ResultPreservationTest(unittest.TestCase):
    def test_saved_results_are_never_overwritten(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            first = save_result_workbook(b"one", "2026-5", 3, directory=directory)
            second = save_result_workbook(b"two", "2026-5", 3, directory=directory)
            self.assertNotEqual(first["path"], second["path"])
            self.assertEqual(Path(first["path"]).read_bytes(), b"one")
            self.assertEqual(Path(second["path"]).read_bytes(), b"two")
            self.assertEqual(len(list_saved_results(directory)), 2)

    def test_manifest_records_the_applied_legal_version(self):
        import result_store

        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            original = result_store.MANIFEST
            result_store.MANIFEST = directory / "run_manifest.jsonl"
            try:
                save_result_workbook(
                    b"data",
                    "2026-5",
                    2,
                    api_status={"sync_status": "CURRENT", "official_notice": "2026-5"},
                    gate_counts={"규제 해당(자동 확정)": 1, "보류(비해당 단정 불가)": 1},
                    directory=directory,
                )
                record = json.loads(result_store.MANIFEST.read_text(encoding="utf-8").strip())
            finally:
                result_store.MANIFEST = original
        self.assertEqual(record["master_notice"], "2026-5")
        self.assertEqual(record["law_api_status"], "CURRENT")
        self.assertEqual(record["gate_counts"]["규제 해당(자동 확정)"], 1)

    def test_paths_outside_the_preservation_folder_are_refused(self):
        with self.assertRaises(ValueError):
            read_saved_result("/etc/hostname")


if __name__ == "__main__":
    unittest.main()
