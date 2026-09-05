import unittest

import pandas as pd

from plain_report import (
    PLAIN_COLUMNS,
    build_decision_policy,
    build_plain_evidence,
    build_plain_summary,
    build_reader_guide,
)


def evidence_row(**overrides):
    row = {
        "material_id": "A1",
        "evidence_status": "CONFIRMED_RULE_MATCH",
        "chemical_name": "Aniline",
        "cas": "62-53-3",
        "concentration_pct": 15.0,
        "match_type": "직접 CAS",
        "scope_type": "DIRECT_CAS",
        "designation_id": "97-1-156",
        "regulatory_substance_name": "아닐린과 그 염류",
        "reference_parent_name": "",
        "reference_parent_cas": "",
        "notice": "2026-5",
        "source_text": "아닐린과 그 염류",
        "acute_threshold_pct": 25.0,
        "acute_decision": "기준 미만",
        "chronic_threshold_pct": 10.0,
        "chronic_decision": "해당",
        "eco_threshold_pct": float("nan"),
        "eco_decision": "규칙 없음",
    }
    row.update(overrides)
    return row


def summary_row(**overrides):
    row = {
        "material_id": "A1",
        "input_chemical_name": "Aniline",
        "resolved_name": "아닐린",
        "input_cas": "62-53-3",
        "concentration_pct": 15.0,
        "final_decision": "규제 해당",
        "gate_decision": "규제 해당(자동 확정)",
        "gate_hold_reason": "",
        "decision_reason": "",
    }
    row.update(overrides)
    return row


class PlainSummaryTest(unittest.TestCase):
    def test_reader_guide_explains_hold_and_ai_in_plain_language(self):
        guide = build_reader_guide().set_index("용어")["쉬운 설명"]
        self.assertIn("비규제라는 뜻이 아닙니다", guide["보류"])
        self.assertIn("확정하지 않습니다", guide["AI 보조검토"])
        policy = build_decision_policy()
        self.assertEqual(policy["판정결과"].tolist(), ["규제 해당", "보류", "비해당"])
        hold_text = policy.loc[policy["판정결과"].eq("보류"), "언제 표시되는가"].iloc[0]
        self.assertIn("복합 범위", hold_text)

    def test_salt_candidate_is_short_and_names_its_category(self):
        summary = pd.DataFrame([
            summary_row(
                final_decision="전문가 검토 필요",
                gate_decision="보류(CAS 없는 규제범위 후보)",
                no_cas_scope_designation_ids="97-1-299",
                no_cas_scope_regulatory_names="3,5-톨루엔디아민과 그 염류",
                no_cas_scope_regulatory_categories="인체급성유해성물질",
                no_cas_scope_candidate_context=(
                    '[{"designation_id":"97-1-299","scope_type":"SALT_FAMILY"}]'
                ),
                llm_status="VALIDATED_OUTPUT",
                llm_rule_relation="INSUFFICIENT_INFORMATION",
                llm_explanation_ko="이 문장은 일반 결과표에 그대로 노출되면 안 됩니다.",
            )
        ])
        row = build_plain_summary(summary, pd.DataFrame(), None, "2026-5").iloc[0]
        self.assertEqual(row["판정결과"], "보류")
        self.assertEqual(row["규제 카테고리"], "인체급성유해성물질")
        self.assertIn("해당될 가능성이 높습니다", row["판단근거"])
        self.assertIn("3,5-톨루엔디아민과 그 염류", row["판단근거"])
        self.assertNotIn("일반 결과표에 그대로", row["판단근거"])

    def test_columns_are_limited_to_the_reader_facing_set(self):
        plain = build_plain_summary(
            pd.DataFrame([summary_row()]), pd.DataFrame([evidence_row()]), None, "2026-5"
        )
        self.assertEqual(list(plain.columns), PLAIN_COLUMNS)
        self.assertEqual(len(plain), 1)

    def test_regulated_row_names_category_threshold_and_concentration(self):
        plain = build_plain_summary(
            pd.DataFrame([summary_row()]), pd.DataFrame([evidence_row()]), None, "2026-5"
        )
        row = plain.iloc[0]
        self.assertEqual(row["판정결과"], "규제 해당")
        self.assertEqual(row["규제 카테고리"], "인체만성유해성물질")
        self.assertIn("지정번호 97-1-156", row["판단근거"])
        self.assertIn("기준 10% 이상", row["판단근거"])
        self.assertIn("실제 함량 15%", row["판단근거"])
        self.assertIn("인체급성유해성물질: 기준 25% 이상", row["판단근거"])
        self.assertEqual(row["법적 근거"], "2026-5 지정번호 97-1-156")

    def test_salt_match_is_explained_with_the_reference_substance(self):
        evidence = pd.DataFrame(
            [
                evidence_row(
                    cas="142-04-1",
                    match_type="검증된 염류 구조",
                    reference_parent_name="Aniline",
                    reference_parent_cas="62-53-3",
                    concentration_pct=30.0,
                    acute_decision="해당",
                    chronic_decision="해당",
                )
            ]
        )
        summary = pd.DataFrame([summary_row(input_cas="142-04-1", concentration_pct=30.0)])
        row = build_plain_summary(summary, evidence, None, "2026-5").iloc[0]
        self.assertIn("염류로 구조가 확인되었습니다", row["판단근거"])
        self.assertIn("CAS 62-53-3", row["판단근거"])

    def test_below_threshold_is_reported_as_such_not_as_a_hold(self):
        evidence = pd.DataFrame(
            [
                evidence_row(
                    concentration_pct=0.5, acute_decision="기준 미만", chronic_decision="기준 미만"
                )
            ]
        )
        summary = pd.DataFrame(
            [
                summary_row(
                    concentration_pct=0.5,
                    final_decision="기준 미만",
                    gate_decision="보류(비해당 단정 불가)",
                    gate_hold_reason="자료 커버리지 미완: 제한물질(해당 카테고리 규칙 데이터 없음)",
                )
            ]
        )
        row = build_plain_summary(summary, evidence, None, "2026-5").iloc[0]
        self.assertEqual(row["판정결과"], "보류")
        self.assertIn("확인하지 못한 사항", row["판단근거"])
        self.assertIn("제한물질", row["판단근거"])

    def test_no_match_states_that_the_list_was_searched(self):
        summary = pd.DataFrame(
            [
                summary_row(
                    final_decision="확인 범위 내 비해당",
                    gate_decision="확인 범위 내 비해당",
                )
            ]
        )
        row = build_plain_summary(summary, pd.DataFrame(), None, "2026-5").iloc[0]
        self.assertEqual(row["판정결과"], "비해당")
        self.assertIn("일치하는 항목은 없었습니다", row["판단근거"])
        self.assertEqual(row["규제 카테고리"], "해당 없음")

    def test_unverified_structure_match_is_explained_not_asserted(self):
        evidence = pd.DataFrame(
            [
                evidence_row(
                    evidence_status="STRUCTURE_UNVERIFIED_HOLD",
                    match_type="검증된 염류 구조",
                    chronic_decision="해당",
                )
            ]
        )
        summary = pd.DataFrame(
            [
                summary_row(
                    final_decision="전문가 검토 필요",
                    gate_decision="보류(비해당 단정 불가)",
                    gate_hold_reason="사용자 SMILES 미검증. 구조범위 규정 해당 여부를 자동 확정할 수 없습니다.",
                )
            ]
        )
        row = build_plain_summary(summary, evidence, None, "2026-5").iloc[0]
        self.assertEqual(row["판정결과"], "보류")
        self.assertEqual(row["규제 카테고리"], "해당 없음")
        self.assertIn("PubChem", row["판단근거"])

    def test_cross_regime_match_adds_its_own_category(self):
        cross = pd.DataFrame(
            [
                {
                    "material_id": "A1",
                    "regime": "RESTRICTED",
                    "regime_label": "제한물질",
                    "decision": "규제 해당",
                    "substance_name": "아닐린",
                    "source_name": "제한물질·금지물질의 지정",
                    "source_reference": "2026-3",
                    "missing_enterprise_fields": "",
                }
            ]
        )
        row = build_plain_summary(
            pd.DataFrame([summary_row()]), pd.DataFrame([evidence_row()]), cross, "2026-5"
        ).iloc[0]
        self.assertIn("제한물질", row["규제 카테고리"])
        self.assertIn("제한물질 2026-3", row["법적 근거"])

    def test_plain_evidence_is_a_short_citation_table(self):
        table = build_plain_evidence(pd.DataFrame([evidence_row()]))
        self.assertIn("규제 원문", table.columns)
        self.assertNotIn("evidence_status", table.columns)
        self.assertLessEqual(len(table.columns), 11)


if __name__ == "__main__":
    unittest.main()
