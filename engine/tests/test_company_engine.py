import unittest
from io import BytesIO
from unittest.mock import patch

import pandas as pd
from openpyxl import load_workbook

from company_engine import (
    build_result_workbook,
    load_approved_master,
    load_promoted_salt_rules,
    prepare_company_inventory,
    read_inventory,
    screen_company_inventory,
)
from regulatory_api import compare_with_master
from cross_regime import add_regulatory_category_columns, screen_cross_regimes
from scope_engine import classify_scope_text, match_extended_scope
from local_llm import _extract_json, _validate_one, analyze_cases, build_llm_review_cases, merge_llm_audit


class CompanyEngineTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.master, cls.notice = load_approved_master()
        cls.salt_rules = load_promoted_salt_rules(cls.master)

    def run_screen(self, rows, structure_verified=False):
        inventory, audit = prepare_company_inventory(pd.DataFrame(rows), fetch_pubchem=False)
        if structure_verified:
            # Stand in for a live PubChem lookup that confirmed the exact CAS
            # synonym; network access is deliberately not used inside tests.
            inventory["structure_smiles_source"] = "PUBCHEM_PUG_REST"
            inventory["structure_status"] = "PUBCHEM_EXACT_CAS_VERIFIED"
        summary, evidence, review = screen_company_inventory(
            inventory, self.master, self.salt_rules, self.notice
        )
        return inventory, audit, summary, evidence, review

    def test_latest_master_and_promoted_salt_rules(self):
        self.assertEqual(self.notice, "2026-5")
        self.assertEqual(len(self.master), 1451)
        self.assertEqual(len(self.salt_rules), 5)

    def test_direct_cas_acute_decision(self):
        _, _, summary, evidence, _ = self.run_screen(
            [
                {
                    "material_id": "D1",
                    "chemical_name": "Propiononitrile",
                    "cas": "107-12-0",
                    "smiles": "CCC#N",
                    "concentration_pct": 2.0,
                }
            ]
        )
        self.assertEqual(summary.loc[0, "final_decision"], "규제 해당")
        self.assertEqual(summary.loc[0, "acute_decision"], "해당")
        self.assertEqual(summary.loc[0, "chronic_decision"], "규칙 없음")
        self.assertTrue(evidence["match_type"].eq("직접 CAS").any())

    def test_category_specific_thresholds(self):
        _, _, summary, _, _ = self.run_screen(
            [
                {
                    "material_id": "D2",
                    "chemical_name": "Aniline",
                    "cas": "62-53-3",
                    "smiles": "Nc1ccccc1",
                    "concentration_pct": 15.0,
                }
            ]
        )
        self.assertEqual(summary.loc[0, "acute_decision"], "기준 미만")
        self.assertEqual(summary.loc[0, "chronic_decision"], "해당")
        self.assertEqual(summary.loc[0, "eco_decision"], "기준 미만")
        self.assertEqual(summary.loc[0, "final_decision"], "규제 해당")

    SALT_CASE = {
        "material_id": "S1",
        "chemical_name": "Aniline hydrochloride",
        "cas": "142-04-1",
        "smiles": "C1=CC(=CC=C1)N.Cl",
        "concentration_pct": 1.0,
    }

    def test_validated_salt_structure_match_with_verified_structure(self):
        _, _, summary, evidence, _ = self.run_screen(
            [dict(self.SALT_CASE)], structure_verified=True
        )
        self.assertEqual(summary.loc[0, "final_decision"], "기준 미만")
        self.assertIn("검증된 염류 구조", summary.loc[0, "match_type"])
        self.assertEqual(evidence.loc[0, "designation_id"], "97-1-156")
        self.assertEqual(evidence.loc[0, "evidence_status"], "CONFIRMED_RULE_MATCH")

    def test_unverified_user_smiles_cannot_confirm_structural_scope(self):
        # The salt rule still matches, but the structure it relies on is only a
        # user-typed SMILES.  That must produce a hold, never an automatic match
        # and never a non-applicable result.
        _, _, summary, evidence, review = self.run_screen([dict(self.SALT_CASE)])
        self.assertEqual(summary.loc[0, "final_decision"], "전문가 검토 필요")
        self.assertEqual(summary.loc[0, "structure_evidence_status"], "USER_SMILES_UNVERIFIED")
        self.assertEqual(evidence.loc[0, "evidence_status"], "STRUCTURE_UNVERIFIED_HOLD")
        self.assertEqual(summary.loc[0, "hh_structure_hits"], 0)
        self.assertEqual(summary.loc[0, "hh_structure_unverified_hits"], 1)
        self.assertEqual(len(review), 1)

    def test_missing_concentration_is_not_nonregulated(self):
        _, _, summary, _, review = self.run_screen(
            [
                {
                    "material_id": "M1",
                    "chemical_name": "Warfarin sodium",
                    "cas": "129-06-6",
                    "smiles": "CC(=O)CC(c1ccccc1)c1c(O)c2ccccc2oc1=O.[Na+]",
                    "concentration_pct": "",
                }
            ],
            structure_verified=True,
        )
        self.assertEqual(summary.loc[0, "final_decision"], "정보 부족")
        self.assertEqual(len(review), 1)

    def test_invalid_cas_is_identity_failure(self):
        _, _, summary, _, _ = self.run_screen(
            [{"material_id": "X1", "chemical_name": "Unknown", "cas": "000-00-0"}]
        )
        self.assertEqual(summary.loc[0, "final_decision"], "물질정보 확인 실패")

    def test_complex_anchor_is_routed_to_review_not_auto_regulated(self):
        _, _, summary, evidence, review = self.run_screen(
            [
                {
                    "material_id": "R1",
                    "chemical_name": "Triethyl arsenate",
                    "cas": "15606-95-8",
                    "concentration_pct": 30,
                }
            ]
        )
        self.assertEqual(summary.loc[0, "final_decision"], "전문가 검토 필요")
        self.assertTrue(evidence.empty)
        self.assertEqual(len(review), 1)

    def test_workbook_contains_company_sheets(self):
        inventory, audit, summary, evidence, review = self.run_screen(
            [
                {
                    "material_id": "D3",
                    "chemical_name": "Nicotine",
                    "cas": "54-11-5",
                    "smiles": "CN1CCC[C@H]1C2=CN=CC=C2",
                    "concentration_pct": 2.0,
                }
            ]
        )
        api = compare_with_master(
            {
                "status": "FOUND",
                "status_ko": "공식 최신 고시 확인 완료",
                "official_notice": self.notice,
            },
            self.notice,
        )
        workbook = build_result_workbook(
            summary, evidence, inventory, audit, review, api, self.notice, len(self.master)
        )
        wb = load_workbook(BytesIO(workbook), read_only=True)
        self.assertEqual(
            wb.sheetnames,
            ["판정결과", "결과읽는법", "자동판정원칙", "판정근거", "검토필요", "법규버전"],
        )

        detailed = build_result_workbook(
            summary,
            evidence,
            inventory,
            audit,
            review,
            api,
            self.notice,
            len(self.master),
            detailed=True,
        )
        detailed_wb = load_workbook(BytesIO(detailed), read_only=True)
        self.assertEqual(detailed_wb.sheetnames[0], "판정결과")
        for sheet in ["상세판정요약", "신뢰게이트", "카테고리검색기록", "법률근거", "규제근거"]:
            self.assertIn(sheet, detailed_wb.sheetnames)

    def test_scope_classifier_covers_non_salt_ranges(self):
        self.assertEqual(classify_scope_text("해당 물질의 유도체"), "DERIVATIVE")
        self.assertEqual(classify_scope_text("해당 물질과 그 수화물"), "HYDRATE_SOLVATE")
        self.assertEqual(classify_scope_text("반응생성물"), "REACTION_PRODUCT")
        self.assertEqual(classify_scope_text("중합체 및 UVCB"), "POLYMER_UVCB")

    def test_approved_smarts_rule_can_match(self):
        rules = pd.DataFrame(
            [
                {
                    "rule_id": "T1",
                    "scope_type": "STRUCTURAL_RANGE",
                    "required_smarts": "[NX3]",
                    "excluded_smarts": "[N+](=O)[O-]",
                    "allowed_elements": "C;H;N",
                    "required_elements": "N",
                }
            ]
        )
        hits = match_extended_scope("Nc1ccccc1", "62-53-3", rules)
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["scope_match_status"], "APPROVED_DETERMINISTIC_MATCH")

    def test_cross_regime_candidate_is_not_final_legal_decision(self):
        inventory = pd.DataFrame(
            [
                {
                    "material_id": "C1",
                    "chemical_name": "Candidate",
                    "cas": "62-53-3",
                    "concentration_pct": 1.0,
                    "use_description": "제품 제조",
                    "annual_quantity_kg": 10,
                    "as_of_date": "2026-09-01",
                    "authorization_status": "확인 중",
                }
            ]
        )
        candidates = pd.DataFrame(
            [
                {
                    "regime": "RESTRICTED",
                    "rule_id": "API-X",
                    "direct_cas": "62-53-3",
                    "scope_type": "DIRECT_CAS",
                    "source_text": "공식 원문 후보",
                }
            ]
        )
        summary, detail = screen_cross_regimes(inventory, pd.DataFrame(), candidates)
        self.assertEqual(summary.loc[0, "RESTRICTED_decision"], "해당 후보·검토 필요")
        self.assertEqual(detail.loc[0, "rule_source_status"], "API_CANDIDATE")

    def test_regulatory_categories_keep_confirmed_and_review_separate(self):
        summary = pd.DataFrame(
            [
                {
                    "material_id": "C1",
                    "HUMAN_HAZARD_decision": "규제 해당",
                    "ACCIDENT_PREPAREDNESS_decision": "규제 해당",
                    "RESTRICTED_decision": "해당 후보·검토 필요",
                    "PROHIBITED_decision": "법규 데이터 확인 필요",
                    "AUTHORIZED_decision": "기준 미만·조건 비충족",
                }
            ]
        )
        categorized = add_regulatory_category_columns(summary)
        self.assertEqual(
            categorized.loc[0, "applicable_regulatory_categories"],
            "인체등유해성물질 / 사고대비물질",
        )
        self.assertEqual(
            categorized.loc[0, "review_regulatory_categories"],
            "제한물질",
        )

    @patch("local_llm._post_chat")
    def test_local_llm_output_is_grounded_and_does_not_replace_decision(self, mocked_chat):
        mocked_chat.return_value = {
            "model": "qwen3.5:4b",
            "results": [
                {
                    "case_id": "LLM-00001",
                    "assessment": "POSSIBLE_MATCH",
                    "scope_type": "DERIVATIVE",
                    "extracted_cas": ["62-53-3", "999-99-9"],
                    "concentration_condition": "0.1% 이상",
                    "use_condition": "",
                    "quantity_condition": "",
                    "temporal_condition": "",
                    "authorization_condition": "",
                    "missing_information": ["용도"],
                    "evidence_quotes": ["62-53-3 및 그 유도체", "원문에 없는 문장"],
                    "explanation_ko": "유도체 범위 검토가 필요함",
                    "confidence": 0.9,
                    "needs_human_review": True,
                }
            ],
        }
        cases = [
            {
                "case_id": "LLM-00001",
                "material_id": "A1",
                "cas": "62-53-3",
                "regulatory_text": "62-53-3 및 그 유도체는 0.1% 이상인 경우",
            }
        ]
        audit = analyze_cases(cases)
        self.assertEqual(audit.loc[0, "llm_extracted_cas"], "62-53-3")
        self.assertEqual(audit.loc[0, "llm_evidence_quotes"], "62-53-3 및 그 유도체")
        self.assertIn("원문에 없는 인용 제거", audit.loc[0, "llm_gate_issues"])
        summary = pd.DataFrame([{"material_id": "A1", "overall_decision": "추가 확인 필요"}])
        merged = merge_llm_audit(summary, audit)
        self.assertEqual(merged.loc[0, "overall_decision"], "추가 확인 필요")

    def test_llm_review_cases_require_regulatory_source_text(self):
        summary = pd.DataFrame(
            [
                {
                    "material_id": "A1",
                    "overall_decision": "추가 확인 필요",
                    "review_regulatory_text": "",
                },
                {
                    "material_id": "A2",
                    "overall_decision": "추가 확인 필요",
                    "review_regulatory_text": "해당 물질과 그 염류",
                },
            ]
        )
        cases = build_llm_review_cases(summary, pd.DataFrame(), pd.DataFrame())
        self.assertEqual([case["material_id"] for case in cases], ["A2"])

    def test_llm_review_cases_only_include_interpretive_holds(self):
        summary = pd.DataFrame(
            [
                {
                    "material_id": "CONFIRMED",
                    "gate_decision": "규제 해당(자동 확정)",
                    "final_decision": "규제 해당",
                    "review_regulatory_text": "공식 규제 원문",
                },
                {
                    "material_id": "IDENTITY_FAIL",
                    "gate_decision": "보류(물질식별 실패)",
                    "final_decision": "물질정보 확인 실패",
                    "review_regulatory_text": "공식 규제 원문",
                },
                {
                    "material_id": "STRUCTURE_REVIEW",
                    "gate_decision": "보류(인체유해성 판정 검토 필요)",
                    "final_decision": "전문가 검토 필요",
                    "review_regulatory_text": "해당 물질과 그 염류",
                },
                {
                    "material_id": "BELOW_BUT_SCOPE_HELD",
                    "gate_decision": "보류(비해당 단정 불가)",
                    "gate_hold_reason": "사용자 SMILES 미검증. 염류·구조범위 확인 필요",
                    "final_decision": "기준 미만",
                    "smiles": "CN.Cl",
                    "review_regulatory_text": "해당 물질과 그 염류",
                },
            ]
        )
        cases = build_llm_review_cases(summary, pd.DataFrame(), pd.DataFrame())
        self.assertEqual(
            [case["material_id"] for case in cases],
            ["STRUCTURE_REVIEW", "BELOW_BUT_SCOPE_HELD"],
        )

    def test_llm_merge_keeps_complete_schema_when_optional_output_is_missing(self):
        summary = pd.DataFrame(
            [{"material_id": "A1", "overall_decision": "추가 확인 필요"}]
        )
        # Error and missing-case rows can legitimately omit evidence/explanation
        # fields.  The UI summary must still receive every expected column.
        audit = pd.DataFrame(
            [
                {
                    "material_id": "A1",
                    "llm_status": "LLM_ERROR",
                    "llm_assessment": "INSUFFICIENT_INFORMATION",
                    "llm_scope_type": "MANUAL_REVIEW",
                }
            ]
        )
        merged = merge_llm_audit(summary, audit)
        for column in [
            "llm_explanation_ko",
            "llm_confidence",
            "llm_evidence_quotes",
            "llm_missing_information",
            "llm_gate_issues",
            "llm_model",
        ]:
            self.assertIn(column, merged.columns)
            self.assertEqual(merged.loc[0, column], "")

    def test_workbook_exports_separate_local_ai_audit(self):
        inventory, audit, summary, evidence, review = self.run_screen(
            [{"material_id": "L1", "chemical_name": "Aniline", "cas": "62-53-3", "concentration_pct": 1}]
        )
        llm_audit = pd.DataFrame(
            [{"material_id": "L1", "llm_assessment": "CONDITION_REVIEW", "llm_model": "qwen3.5:4b"}]
        )
        workbook = build_result_workbook(
            summary,
            evidence,
            inventory,
            audit,
            review,
            {},
            self.notice,
            len(self.master),
            llm_audit=llm_audit,
            llm_status={"status_ko": "로컬 AI 사용 가능", "model": "qwen3.5:4b"},
            detailed=True,
        )
        wb = load_workbook(BytesIO(workbook), read_only=True)
        self.assertIn("로컬AI검토기록", wb.sheetnames)

    def test_llm_json_recovery_accepts_fenced_or_prefixed_object(self):
        self.assertEqual(_extract_json('```json\n{"case_id":"C1"}\n```')["case_id"], "C1")
        self.assertEqual(_extract_json('결과입니다: {"case_id":"C2"}')["case_id"], "C2")

    @patch("local_llm._post_chat")
    def test_llm_retries_one_case_with_simple_json(self, mocked_post):
        mocked_post.side_effect = [
            ValueError("schema response malformed"),
            {
                "model": "test-model",
                "results": [{
                    "case_id": "C1", "assessment": "CONDITION_REVIEW",
                    "scope_type": "DIRECT_CAS", "candidate_designation_ids": ["97-1-1"],
                    "extracted_cas": [], "concentration_condition": "", "use_condition": "",
                    "quantity_condition": "", "temporal_condition": "",
                    "authorization_condition": "", "missing_information": [],
                    "evidence_quotes": ["그 염류"], "explanation_ko": "염류 범위 검토",
                    "confidence": 0.8, "needs_human_review": True,
                }],
            },
        ]
        audit = analyze_cases([{
            "case_id": "C1", "material_id": "M1", "cas": "1-11-1",
            "candidate_designation_ids": "97-1-1",
            "candidate_regulatory_names": "시험물질과 그 염류",
            "regulatory_text": "97-1-1 | 시험물질과 그 염류",
        }], max_cases=1)
        self.assertEqual(mocked_post.call_count, 2)
        self.assertEqual(audit.loc[0, "llm_status"], "VALIDATED_OUTPUT")
        self.assertEqual(audit.loc[0, "llm_attempts"], 2)
        self.assertEqual(audit.loc[0, "llm_scope_type"], "PARENT_SALT")

    def test_successful_retry_replaces_prior_error_in_summary(self):
        summary = pd.DataFrame([{"material_id": "M1"}])
        audit = pd.DataFrame([
            {"material_id": "M1", "llm_status": "LLM_ERROR", "llm_explanation_ko": "old"},
            {"material_id": "M1", "llm_status": "VALIDATED_OUTPUT", "llm_explanation_ko": "new"},
        ])
        merged = merge_llm_audit(summary, audit)
        self.assertEqual(merged.loc[0, "llm_status"], "VALIDATED_OUTPUT")
        self.assertEqual(merged.loc[0, "llm_explanation_ko"], "new")

    def test_exception_relation_and_scope_are_deterministically_preserved(self):
        context = ('[{"designation_id":"97-1-140","regulatory_name":"수은 화합물",'
                   '"scope_type":"NAME_OR_UVCB","match_type":"EXPLICIT_EXCEPTION_CAS",'
                   '"explicit_exception":true}]')
        out = _validate_one(
            {
                "assessment": "INSUFFICIENT_INFORMATION", "scope_type": "HYDRATE_SOLVATE",
                "rule_relation": "INSUFFICIENT_INFORMATION", "candidate_designation_ids": [],
                "extracted_cas": [], "evidence_quotes": [], "missing_information": ["용도", "연간 수량"],
                "confidence": 0.7,
            },
            {
                "case_id": "C2", "material_id": "M2", "cas": "10124-48-8",
                "candidate_designation_ids": "97-1-140", "candidate_context": context,
                "regulatory_text": "수은 화합물(아미노염화제이수은은 제외)",
            },
            "test",
        )
        self.assertEqual(out["llm_rule_relation"], "EXPLICITLY_EXCLUDED")
        self.assertEqual(out["llm_scope_type"], "COMPOUND_GROUP")
        self.assertEqual(out["llm_candidate_designation_ids"], "97-1-140")
        self.assertEqual(out["llm_model_selected_designation_ids"], "")
        self.assertEqual(out["llm_missing_information"], "")

    def test_component_cas_can_never_become_whole_mixture_identity(self):
        context = ('[{"designation_id":"2010-1-603","scope_type":"MIXTURE",'
                   '"match_type":"COMPONENT_CAS_CANDIDATE","explicit_exception":false}]')
        out = _validate_one(
            {
                "assessment": "POSSIBLE_MATCH", "scope_type": "DIRECT_CAS",
                "rule_relation": "INCLUDED_CANDIDATE",
                "candidate_designation_ids": ["2010-1-603"], "extracted_cas": [],
                "evidence_quotes": ["혼합물"], "missing_information": [], "confidence": 0.9,
            },
            {
                "case_id": "C3", "material_id": "M3", "candidate_designation_ids": "2010-1-603",
                "candidate_context": context, "regulatory_text": "두 물질의 혼합물",
            },
            "test",
        )
        self.assertEqual(out["llm_rule_relation"], "COMPONENT_ONLY")
        self.assertEqual(out["llm_scope_type"], "COMPOUND_GROUP")


if __name__ == "__main__":
    unittest.main()


class CsvEncodingFallbackTest(unittest.TestCase):
    """Excel on Korean Windows commonly resaves a CSV as CP949/EUC-KR even
    when the original file was UTF-8; read_inventory must not crash on this."""

    def _roundtrip(self, encoding):
        buf = BytesIO()
        pd.DataFrame(
            [{"material_id": "A1", "chemical_name": "아닐린염산염", "cas": "142-04-1"}]
        ).to_csv(buf, index=False, encoding=encoding)
        buf.seek(0)
        buf.name = "inventory.csv"
        return read_inventory(buf)

    def test_reads_cp949_encoded_csv(self):
        frame = self._roundtrip("cp949")
        self.assertEqual(frame.loc[0, "chemical_name"], "아닐린염산염")

    def test_reads_euc_kr_encoded_csv(self):
        frame = self._roundtrip("euc-kr")
        self.assertEqual(frame.loc[0, "chemical_name"], "아닐린염산염")

    def test_reads_utf8_sig_encoded_csv(self):
        frame = self._roundtrip("utf-8-sig")
        self.assertEqual(frame.loc[0, "chemical_name"], "아닐린염산염")

    def test_unreadable_encoding_raises_a_clear_korean_message(self):
        buf = BytesIO("한글깨짐테스트".encode("utf-16"))
        buf.name = "inventory.csv"
        with self.assertRaises(ValueError) as ctx:
            read_inventory(buf)
        self.assertIn("CSV UTF-8", str(ctx.exception))


class DraftHintsSheetTest(unittest.TestCase):
    """The structural-draft hints must reach the downloaded workbook, not
    only the live Streamlit screen -- otherwise the information disappears
    the moment someone saves the file and closes the app."""

    @classmethod
    def setUpClass(cls):
        cls.master, cls.notice = load_approved_master()
        cls.salt_rules = load_promoted_salt_rules(cls.master)

    def _screen_one(self):
        raw = pd.DataFrame(
            [{"material_id": "A1", "chemical_name": "Test", "cas": "64-17-5", "concentration_pct": 10}]
        )
        enriched, audit = prepare_company_inventory(raw, fetch_pubchem=False)
        summary, evidence, review = screen_company_inventory(
            enriched, self.master, self.salt_rules, self.notice
        )
        return summary, evidence, enriched, audit, review

    def test_hint_sheet_appears_when_hints_exist(self):
        summary, evidence, enriched, audit, review = self._screen_one()
        hints = pd.DataFrame(
            [
                {
                    "cas": "639-58-7",
                    "chemical_name": "테스트물질",
                    "designation_id": "06-5-99",
                    "regime": "RESTRICTED",
                    "explanation_ko": "테스트 설명",
                    "confidence": 0.5,
                    "source_text": "테스트 원문",
                }
            ]
        )
        wb_bytes = build_result_workbook(
            summary, evidence, enriched, audit, review, {}, self.notice, len(self.master),
            draft_hints=hints,
        )
        names = load_workbook(BytesIO(wb_bytes), read_only=True).sheetnames
        self.assertIn("구조초안힌트(미검증)", names)

    def test_hint_sheet_is_absent_when_there_are_no_hints(self):
        summary, evidence, enriched, audit, review = self._screen_one()
        wb_bytes = build_result_workbook(
            summary, evidence, enriched, audit, review, {}, self.notice, len(self.master),
            draft_hints=pd.DataFrame(),
        )
        names = load_workbook(BytesIO(wb_bytes), read_only=True).sheetnames
        self.assertNotIn("구조초안힌트(미검증)", names)
