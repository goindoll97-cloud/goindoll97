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
    def test_reader_guide_explains_new_final_policy(self):
        guide = build_reader_guide().set_index("용어")["쉬운 설명"]
        self.assertIn("가장 관련성이 높은", guide["보류"])
        self.assertIn("항상 우선", guide["명시적 제외"])
        policy = build_decision_policy()
        self.assertEqual(policy["판정결과"].tolist(), ["규제 해당", "보류", "비해당"])
        self.assertIn("1~2문장", policy.loc[policy["판정결과"].eq("규제 해당"), "자세한 내용"].iloc[0])

    def test_hold_uses_only_primary_candidate(self):
        summary = pd.DataFrame([
            summary_row(
                final_decision="전문가 검토 필요",
                gate_decision="보류(CAS 없는 규제범위 후보)",
                no_cas_scope_designation_ids="97-1-299",
                no_cas_scope_regulatory_names="3,5-톨루엔디아민과 그 염류; 2,5-톨루엔디아민과 그 염류",
                no_cas_scope_regulatory_categories="인체급성유해성물질; 인체만성유해성물질",
                no_cas_scope_primary_designation_id="97-1-299",
                no_cas_scope_primary_regulatory_name="3,5-톨루엔디아민과 그 염류",
                no_cas_scope_primary_category="인체급성유해성물질; 인체만성유해성물질",
                no_cas_scope_primary_match_type="NAME_SCOPE_CANDIDATE",
                no_cas_scope_primary_explicit_exception=False,
            )
        ])
        row = build_plain_summary(summary, pd.DataFrame(), None, "2026-5").iloc[0]
        self.assertEqual(row["판정결과"], "보류")
        self.assertIn("3,5-톨루엔디아민과 그 염류", row["판단근거"])
        self.assertNotIn("2,5-톨루엔디아민과 그 염류", row["판단근거"])
        self.assertIn("전문가 검토", row["판단근거"])

    def test_explicit_exclusion_beats_similarity_candidate(self):
        summary = pd.DataFrame([
            summary_row(
                input_chemical_name="2,6-Diaminotoluene",
                resolved_name="2,6-Diaminotoluene",
                input_cas="823-40-5",
                final_decision="전문가 검토 필요",
                gate_decision="보류(CAS 없는 규제범위 후보)",
                no_cas_scope_primary_designation_id="97-1-299",
                no_cas_scope_primary_regulatory_name="톨루엔디아민류(2,6-톨루엔디아민 제외)와 그 염류",
                no_cas_scope_primary_category="인체급성유해성물질; 인체만성유해성물질; 생태유해성물질",
                no_cas_scope_primary_match_type="EXPLICIT_EXCEPTION_CAS",
                no_cas_scope_primary_explicit_exception=True,
                llm_rule_relation="INCLUDED_CANDIDATE",
            )
        ])
        row = build_plain_summary(summary, pd.DataFrame(), None, "2026-5").iloc[0]
        self.assertEqual(row["판정결과"], "보류")
        self.assertIn("명시적 제외조건", row["판단근거"])
        self.assertNotIn("해당될 가능성이 높", row["판단근거"])

    def test_confirmed_result_suppresses_candidate_and_expert_review_text(self):
        evidence = pd.DataFrame([
            evidence_row(
                chemical_name="Strychnine hydrochloride",
                cas="1421-86-9",
                concentration_pct=100.0,
                match_type="검증된 염류 구조",
                regulatory_substance_name="스트리시닌과 그 염류",
                reference_parent_name="Strychnine",
                reference_parent_cas="57-24-9",
                designation_id="97-1-144",
                acute_threshold_pct=1.0,
                acute_decision="해당",
                chronic_threshold_pct=float("nan"),
                chronic_decision="규칙 없음",
                eco_threshold_pct=25.0,
                eco_decision="해당",
            )
        ])
        summary = pd.DataFrame([
            summary_row(
                input_chemical_name="Strychnine hydrochloride",
                resolved_name="Strychnine hydrochloride",
                input_cas="1421-86-9",
                concentration_pct=100.0,
                no_cas_scope_primary_designation_id="97-1-144",
                no_cas_scope_primary_regulatory_name="스트리시닌과 그 염류",
                no_cas_scope_primary_category="인체급성유해성물질; 생태유해성물질",
                llm_rule_relation="INCLUDED_CANDIDATE",
            )
        ])
        cross = pd.DataFrame([{
            "material_id": "A1",
            "regime": "PROHIBITED",
            "regime_label": "금지물질",
            "decision": "규제 해당",
            "substance_name": "Strychnine hydrochloride",
            "source_name": "금지물질의 지정",
            "source_reference": "현행 고시",
        }])
        row = build_plain_summary(summary, evidence, cross, "2026-5").iloc[0]
        self.assertEqual(row["판정결과"], "규제 해당")
        self.assertNotIn("전문가 검토", row["판단근거"])
        self.assertNotIn("가능성이", row["판단근거"])
        self.assertIn("금지물질", row["규제 카테고리"])
        self.assertLessEqual(len(row["판단근거"]), 200)

    def test_category_whitespace_is_normalized(self):
        summary = pd.DataFrame([
            summary_row(
                final_decision="전문가 검토 필요",
                gate_decision="보류(CAS 없는 규제범위 후보)",
                no_cas_scope_primary_designation_id="97-1-299",
                no_cas_scope_primary_regulatory_name="3,5-톨루엔디아민과 그 염류",
                no_cas_scope_primary_category=" 인체급성유해성물질 ;  인체만성유해성물질 ; 생태유해성물질 ",
            )
        ])
        row = build_plain_summary(summary, pd.DataFrame(), None, "2026-5").iloc[0]
        self.assertEqual(
            row["규제 카테고리"],
            "인체급성유해성물질 / 인체만성유해성물질 / 생태유해성물질",
        )
        self.assertNotIn(" /  ", row["규제 카테고리"])

    def test_columns_are_limited_to_reader_facing_set(self):
        plain = build_plain_summary(
            pd.DataFrame([summary_row()]), pd.DataFrame([evidence_row()]), None, "2026-5"
        )
        self.assertEqual(list(plain.columns), PLAIN_COLUMNS)
        self.assertEqual(len(plain), 1)

    def test_regulated_row_names_threshold_and_concentration_compactly(self):
        row = build_plain_summary(
            pd.DataFrame([summary_row()]), pd.DataFrame([evidence_row()]), None, "2026-5"
        ).iloc[0]
        self.assertEqual(row["판정결과"], "규제 해당")
        self.assertEqual(row["규제 카테고리"], "인체만성유해성물질")
        self.assertIn("지정번호 97-1-156", row["판단근거"])
        self.assertIn("10% 이상", row["판단근거"])
        self.assertIn("함량 15%", row["판단근거"])
        self.assertLessEqual(len(row["판단근거"]), 200)

    def test_no_match_states_checked_scope(self):
        summary = pd.DataFrame([
            summary_row(
                final_decision="확인 범위 내 비해당",
                gate_decision="확인 범위 내 비해당",
            )
        ])
        row = build_plain_summary(summary, pd.DataFrame(), None, "2026-5").iloc[0]
        self.assertEqual(row["판정결과"], "비해당")
        self.assertIn("확인되지 않았습니다", row["판단근거"])
        self.assertEqual(row["규제 카테고리"], "해당 없음")

    def test_plain_evidence_is_short_citation_table(self):
        table = build_plain_evidence(pd.DataFrame([evidence_row()]))
        self.assertIn("규제 원문", table.columns)
        self.assertNotIn("evidence_status", table.columns)
        self.assertLessEqual(len(table.columns), 11)


if __name__ == "__main__":
    unittest.main()
