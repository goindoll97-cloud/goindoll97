"""Desktop-only output regression: same engine, four visible labels, three Excel tabs."""
from __future__ import annotations

from io import BytesIO

import pandas as pd
from openpyxl import Workbook, load_workbook

from desktop_report import (
    COLUMNS, adapt_desktop_workbook, build_desktop_plain_summary,
)
from plain_report import _plain_result


def sample_summary():
    return pd.DataFrame([
        {
            "material_id": "P1", "input_chemical_name": "Aniline",
            "resolved_name": "Aniline", "input_cas": "62-53-3",
            "concentration_pct": 15, "final_decision": "규제 해당",
            "overall_decision": "규제 해당",
            "gate_decision": "보류(규제 해당 후보·전문가 확인 필요)",
            "rule_based_decision": "승인 규칙상 해당",
            "gate_hold_reason": "후속 고시 2026-16호 시행 예정",
            "law_version_status": "후속 고시 2026-16호 시행 예정",
        },
        {
            "material_id": "B1", "input_chemical_name": "Below",
            "resolved_name": "Below", "input_cas": "50-00-0",
            "concentration_pct": 2, "final_decision": "기준 미만",
            "overall_decision": "확인 범위 내 비해당",
            "gate_decision": "보류(비해당 단정 불가)",
            "gate_hold_reason": "법규 최신성 확인 필요",
        },
        {
            "material_id": "N1", "input_chemical_name": "Not matched",
            "resolved_name": "Not matched", "input_cas": "58-08-2",
            "concentration_pct": 1, "final_decision": "확인 범위 내 비해당",
            "overall_decision": "확인 범위 내 비해당",
            "gate_decision": "보류(비해당 단정 불가)",
            "gate_hold_reason": "법규 최신성 확인 필요",
        },
        {
            "material_id": "H1", "input_chemical_name": "Scope ambiguity",
            "resolved_name": "Scope ambiguity", "input_cas": "71-43-2",
            "concentration_pct": 10, "final_decision": "전문가 검토 필요",
            "overall_decision": "추가 확인 필요",
            "gate_decision": "보류(CAS 없는 규제범위 후보)",
            "gate_hold_reason": "범위지정 검토 필요",
            "no_cas_scope_primary_designation_id": "97-1-200",
            "no_cas_scope_primary_regulatory_name": "포괄 지정 후보",
            "no_cas_scope_primary_category": "인체급성유해성물질",
        },
    ])


def sample_evidence():
    template = {
        "evidence_status": "CONFIRMED_RULE_MATCH",
        "match_type": "직접 CAS", "scope_type": "DIRECT_CAS",
        "designation_id": "97-1-156",
        "regulatory_substance_name": "아닐린과 그 염류",
        "acute_threshold_pct": 25, "acute_decision": "기준 미만",
        "chronic_threshold_pct": 10, "chronic_decision": "해당",
        "eco_threshold_pct": 5, "eco_decision": "해당",
    }
    return pd.DataFrame([
        {**template, "material_id": "P1", "chemical_name": "Aniline",
         "cas": "62-53-3", "concentration_pct": 15},
        {**template, "material_id": "B1", "chemical_name": "Below",
         "cas": "50-00-0", "concentration_pct": 2,
         "acute_threshold_pct": 10, "acute_decision": "기준 미만",
         "chronic_decision": "규칙 없음", "chronic_threshold_pct": None,
         "eco_decision": "규칙 없음", "eco_threshold_pct": None},
    ])


def main():
    summary = sample_summary()
    evidence = sample_evidence()
    hints = pd.DataFrame([{"material_id": "H1", "cas": "71-43-2"}])
    desktop = build_desktop_plain_summary(
        summary, evidence, pd.DataFrame(), "2026-5", draft_hints=hints
    )
    assert list(desktop.columns) == COLUMNS
    assert len(desktop) == 4
    rows = desktop.set_index("인벤토리 ID")
    assert list(desktop["인벤토리 ID"]) == ["P1", "B1", "N1", "H1"]
    assert desktop["판정결과"].tolist() == [
        "규제 해당", "기준 미만", "비해당", "보류(추가 확인 필요)"
    ], desktop[["인벤토리 ID", "판정결과"]].to_dict("records")
    assert "2026-16" in rows.loc["P1", "추가 확인사항"]
    assert rows.loc["P1", "판정결과"] == "규제 해당"
    assert rows.loc["P1", "규제 카테고리"] == (
        "인체만성유해성물질 / 생태유해성물질"
    ), rows.loc["P1", "규제 카테고리"]
    assert rows.loc["B1", "규제 카테고리"] == "해당 없음"
    assert rows.loc["N1", "규제 카테고리"] == "해당 없음"
    assert rows.loc["H1", "구조초안 힌트"] == "발생함(검토 필요)"
    assert rows.loc["P1", "구조초안 힌트"] == "없음"
    assert "인체급성유해성물질: 기준 25% 이상, 실제 함량 15% — 기준 미만" in rows.loc["P1", "판단근거"]
    assert "인체만성유해성물질: 기준 10% 이상, 실제 함량 15% — 규제 해당" in rows.loc["P1", "판단근거"]
    assert "생태유해성물질: 기준 5% 이상, 실제 함량 15% — 규제 해당" in rows.loc["P1", "판단근거"]
    assert rows.loc["B1", "인체급성 기준(%)"] == 10
    assert rows.loc["B1", "인체급성 판정"] == "기준 미만"
    assert rows.loc["H1", "추가 확인사항"].startswith("확인하지 못한 사항:")

    # A pending cross-regime candidate still requires review even if
    # the human-hazard comparison is below its threshold.
    candidate = summary.loc[[1]].copy()
    candidate["overall_decision"] = "규제 해당 후보"
    cross = pd.DataFrame([{"material_id": "B1", "regime": "RESTRICTED",
                           "regime_label": "제한물질", "decision": "해당 후보·검토 필요"}])
    held = build_desktop_plain_summary(candidate, evidence, cross, "2026-5")
    assert held.iloc[0]["판정결과"] == "보류(추가 확인 필요)"

    # An unverified structural match is not accepted as confirmed evidence.
    tentative = summary.loc[[3]].copy()
    provisional = pd.DataFrame([{
        "material_id": "H1", "evidence_status": "STRUCTURE_UNVERIFIED_HOLD",
        "acute_decision": "해당", "acute_threshold_pct": 0.5,
    }])
    assert build_desktop_plain_summary(
        tentative, provisional, pd.DataFrame(), "2026-5"
    ).iloc[0]["판정결과"] == "보류(추가 확인 필요)"

    book = Workbook()
    book.active.title = "판정결과"
    book.create_sheet("결과읽는법")
    version = book.create_sheet("법규버전")
    version.append(["항목", "값"])
    version.append(["승인 규제 마스터 고시", "2026-5"])
    book.create_sheet("판정근거")
    book.create_sheet("상세판정요약")
    intermediate = BytesIO()
    book.save(intermediate)
    data = adapt_desktop_workbook(intermediate.getvalue(), desktop)
    output = load_workbook(BytesIO(data))
    assert output.sheetnames == ["판정결과", "결과읽는법", "법규버전"], output.sheetnames
    assert output["판정결과"]["A1"].value == "인벤토리 ID"
    assert output["판정결과"]["A2"].value == "P1"
    assert output["판정결과"]["E2"].value == "규제 해당"
    assert output["판정결과"]["E5"].value == "보류(추가 확인 필요)"
    assert output["법규버전"]["B2"].value == "2026-5"
    guide = output["결과읽는법"]
    assert any(c.value == "기준 미만" for c in guide["A"])
    assert any(c.value == "보류(추가 확인 필요)" for c in guide["A"])
    # Shared report logic (used by Streamlit) was not modified.
    assert _plain_result(
        "보류(규제 해당 후보·전문가 확인 필요)", "규제 해당",
        "승인 규칙상 해당"
    ) == "승인 규칙상 해당"
    print("PASS: desktop-only 4 labels, detailed 3 hazards, ID/hint, warning, 3 sheets")


if __name__ == "__main__":
    main()
