"""Desktop-only, three-worksheet, reader-facing regulatory report.

The Streamlit app and shared regulatory/trust-gate decision engine are not
modified. Display labels are derived from the engine's verified outcomes,
not from a future-law warning or an unverified AI suggestion.
"""
from __future__ import annotations

from copy import copy
from io import BytesIO
import math
from typing import Any

import pandas as pd
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill

from plain_report import build_plain_summary
from scope_engine import clean


HAZARD = (
    ("acute", "인체급성유해성물질"),
    ("chronic", "인체만성유해성물질"),
    ("eco", "생태유해성물질"),
)
COLUMNS = [
    "인벤토리 ID", "물질명", "CAS번호", "함량(%)", "판정결과",
    "규제 카테고리", "법규 인체급성 기준(%)", "인체급성 판정",
    "법규 인체만성 기준(%)", "인체만성 판정", "법규 생태 기준(%)",
    "생태 판정", "구조초안 힌트", "검증 상태", "판단근거",
    "추가 확인사항", "법적 근거",
]
STATUS_RANK = {"해당": 4, "함량 확인 필요": 3, "기준 미만": 2}
RESULT_COLORS = {
    "규제 해당": "F4CCCC",
    "기준 미만": "D9EAD3",
    "비해당": "D9EAF7",
    "보류(추가 확인 필요)": "FFF2CC",
}


def _value(value: Any) -> str:
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return ""
    return clean(value)


def _num(value: Any) -> float:
    result = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return float(result) if pd.notna(result) else math.nan


def _format_num(value: Any) -> str:
    num = _num(value)
    return f"{num:g}" if math.isfinite(num) else "확인 필요"


def _matches(data: pd.DataFrame | None, material_id: str) -> pd.DataFrame:
    if data is None or data.empty or "material_id" not in data.columns:
        return pd.DataFrame()
    return data[data["material_id"].map(_value).eq(material_id)]


def _confirmed_evidence(evidence: pd.DataFrame | None, material_id: str) -> pd.DataFrame:
    mine = _matches(evidence, material_id)
    if mine.empty:
        return mine
    if "evidence_status" not in mine:
        return mine
    return mine[mine["evidence_status"].map(_value).ne("STRUCTURE_UNVERIFIED_HOLD")]


def _category_details(rows: pd.DataFrame, concentration: Any):
    lines = []
    details = {}
    for key, label in HAZARD:
        candidates = []
        for _, row in rows.iterrows():
            decision = _value(row.get(f"{key}_decision"))
            threshold = _num(row.get(f"{key}_threshold_pct"))
            if decision in STATUS_RANK and math.isfinite(threshold):
                candidates.append((STATUS_RANK[decision], threshold, decision))
        if not candidates:
            details[key] = ("", "규칙 없음")
            lines.append(f"{label}: 확인된 적용 기준 없음")
            continue
        _, threshold, decision = max(candidates, key=lambda x: x[0])
        details[key] = (threshold, decision)
        lines.append(
            f"{label}: 기준 {_format_num(threshold)}% 이상, "
            f"실제 함량 {_format_num(concentration)}% — "
            f"{'규제 해당' if decision == '해당' else decision}"
        )
    return details, lines


def _cross_positive(rows: pd.DataFrame) -> bool:
    return bool(not rows.empty and "decision" in rows.columns
                and rows["decision"].map(_value).eq("규제 해당").any())


def _label(row: pd.Series, cross_rows: pd.DataFrame) -> str:
    final = _value(row.get("final_decision"))
    overall = _value(row.get("overall_decision"))
    if final == "규제 해당" or overall == "규제 해당" or _cross_positive(cross_rows):
        return "규제 해당"
    # An unresolved, separately applicable regime cannot be overwritten by
    # a "below threshold" or a "no match" finding in another regime.
    if overall in {"규제 해당 후보", "추가 확인 필요"}:
        return "보류(추가 확인 필요)"
    if final == "기준 미만":
        return "기준 미만"
    if final == "확인 범위 내 비해당":
        return "비해당"
    return "보류(추가 확인 필요)"


def _has_hint(draft_hints: pd.DataFrame | None, material_id: str, cas: str) -> bool:
    if draft_hints is None or draft_hints.empty:
        return False
    for column in ("material_id", "인벤토리 ID"):
        if column in draft_hints.columns:
            return bool(draft_hints[column].map(_value).eq(material_id).any())
    for column in ("cas", "CAS번호"):
        if column in draft_hints.columns and cas:
            return bool(draft_hints[column].map(_value).eq(cas).any())
    # Without a reliable row identifier, never attach a global draft to a
    # specific inventory item.
    return False


def build_desktop_plain_summary(
    summary: pd.DataFrame, evidence: pd.DataFrame | None,
    cross_detail: pd.DataFrame | None, notice: str,
    draft_hints: pd.DataFrame | None = None,
) -> pd.DataFrame:
    if summary is None or summary.empty:
        return pd.DataFrame(columns=COLUMNS)

    base = build_plain_summary(summary, evidence, cross_detail, notice).reset_index(drop=True)
    rows = []
    for i, (_, item) in enumerate(summary.iterrows()):
        original = base.iloc[i]
        material_id = _value(item.get("material_id"))
        cas = _value(item.get("input_cas"))
        concentration = _num(item.get("concentration_pct"))
        confirmed = _confirmed_evidence(evidence, material_id)
        cross_rows = _matches(cross_detail, material_id)
        decision = _label(item, cross_rows)
        details, lines = _category_details(confirmed, concentration)

        extra = _value(item.get("gate_hold_reason"))
        law_status = _value(item.get("law_version_status"))
        # Statutory freshness belongs in this field, not in the four-way
        # output label. Keep the full original warning for human review.
        if law_status and law_status not in extra and law_status != "승인 규칙 최신":
            extra = "; ".join(x for x in (extra, law_status) if x)
        if extra:
            extra = "확인하지 못한 사항: " + extra
        elif decision.startswith("보류"):
            extra = "확인하지 못한 사항: 추가 법령·물질정보 검토 필요"
        else:
            extra = "-"

        if decision == "비해당":
            explanation = (
                "확인한 규제범위에서 적용되는 항목이 확인되지 않았습니다. "
                "다른 화학물질 규제까지 절대적으로 비해당임을 의미하지 않습니다."
            )
        else:
            explanation = "\n".join(lines)
            if decision == "규제 해당" and not confirmed.empty:
                ids = [
                    _value(x) for x in confirmed.get("designation_id", pd.Series(dtype=str))
                    if _value(x)
                ]
                if ids:
                    explanation += "\n확인된 고유번호: " + ", ".join(dict.fromkeys(ids))
            if decision.startswith("보류"):
                original_reason = _value(original.get("판단근거"))
                if original_reason:
                    explanation += "\n검토 사유: " + original_reason
            if decision == "규제 해당" and _cross_positive(cross_rows):
                cross_labels = [
                    _value(r.get("regime_label")) or _value(r.get("regime"))
                    for _, r in cross_rows.iterrows()
                    if _value(r.get("decision")) == "규제 해당"
                ]
                explanation += "\n타 규제체계: " + " / ".join(dict.fromkeys(cross_labels))

        categories = (
            _value(original.get("규제 카테고리"))
            if decision == "규제 해당" else
            "해당 없음" if decision in {"비해당", "기준 미만"}
            else "확인된 해당 항목 없음(검토 중)"
        )
        row = {
            "인벤토리 ID": material_id,
            "물질명": _value(original.get("물질명")),
            "CAS번호": cas,
            "함량(%)": concentration if math.isfinite(concentration) else None,
            "판정결과": decision,
            "규제 카테고리": categories,
            "구조초안 힌트": (
                "발생함(검토 필요)" if _has_hint(draft_hints, material_id, cas) else "없음"
            ),
            "검증 상태": _value(original.get("검증 상태")) or "-",
            "판단근거": explanation,
            "추가 확인사항": extra,
            "법적 근거": _value(original.get("법적 근거")) or "-",
        }
        for key, title in (("acute", "인체급성"), ("chronic", "인체만성"), ("eco", "생태")):
            threshold, outcome = details[key]
            row[f"법규 {title} 기준(%)"] = threshold
            row[f"{title} 판정"] = outcome
        rows.append(row)
    return pd.DataFrame(rows, columns=COLUMNS)


def desktop_guide() -> pd.DataFrame:
    rows = [
        ("규제 해당", "승인 규칙에서 직접 CAS 또는 검증된 구조와 적용 기준을 충족하는 근거가 확인되었습니다. 법규 최신성·공식 적용 여부는 검증 상태와 법규버전을 반드시 확인하세요."),
        ("기준 미만", "규제 범위 및 기준값에 연결되지만 입력 함량이 적용 기준에 미달합니다. 다른 규제 후보가 미해결이면 보류로 표시됩니다."),
        ("비해당", "확인한 규제범위에서 해당 항목을 찾지 못했다는 뜻입니다. 국내 모든 법규에서 절대적 비규제임을 뜻하지 않습니다."),
        ("보류(추가 확인 필요)", "포괄 지정, 혼합물, 명시적 제외, 정보 부족, 확인되지 않은 규제 후보 등은 사람의 검토가 필요합니다."),
        ("인벤토리 ID", "입력 자료의 ID를 그대로 표시하여 원본 행과 판정 결과를 대조합니다."),
        ("세 가지 유해성 판단", "인체급성·인체만성·생태유해성 기준과 실제 함량을 각각 표시합니다. '규칙 없음'은 이 앱에서 검증된 적용 기준을 찾지 못한 것으로, 법적 비규제를 보장하지 않습니다."),
        ("규제 카테고리", "확인된 규제 해당 카테고리만 표시합니다. 기준 미만 및 확인 범위 내 비해당은 '해당 없음'으로 표시합니다."),
        ("구조초안 힌트", "없음 또는 발생함(검토 필요). 미검증 AI 초안은 법적 확정 근거가 아니며, 실제로 생성/연결된 경우에만 발생함으로 표시됩니다."),
        ("검증 상태 및 추가 확인사항", "법규 최신성·정확한 구조 및 다른 조건의 검증 필요 여부는 여기에서 확인하며, 판정결과를 일괄 보류로 바꾸지 않습니다."),
        ("법적 근거", "적용 고시의 고유번호나 관련 지정 근거를 표시합니다. 상세 적용은 공식 원문과 대조해야 합니다."),
    ]
    return pd.DataFrame(rows, columns=["용어", "쉬운 설명"])


def adapt_desktop_workbook(base_workbook: bytes, plain: pd.DataFrame) -> bytes:
    """Keep original version metadata and guide styling; export only 3 tabs."""
    book = load_workbook(BytesIO(base_workbook))
    required = {"판정결과", "결과읽는법", "법규버전"}
    if not required.issubset(book.sheetnames):
        raise ValueError(f"필요한 시트가 누락되었습니다: {required - set(book.sheetnames)}")
    for name in list(book.sheetnames):
        if name not in required:
            del book[name]

    original_sheet = book["판정결과"]
    at = book.sheetnames.index("판정결과")
    del book["판정결과"]
    ws = book.create_sheet("판정결과", at)
    guide = book["결과읽는법"]
    guide.delete_rows(1, guide.max_row)
    for row in [list(desktop_guide().columns), *desktop_guide().itertuples(index=False, name=None)]:
        guide.append(list(row))

    ws.append(list(plain.columns))
    for row in plain.itertuples(index=False, name=None):
        ws.append([None if isinstance(v, float) and math.isnan(v) else v for v in row])

    ws.freeze_panes = "E2"
    ws.auto_filter.ref = ws.dimensions
    ws.sheet_view.showGridLines = False
    ws.row_dimensions[1].height = 33
    width = {
        "인벤토리 ID": 17, "물질명": 28, "CAS번호": 19, "함량(%)": 13,
        "판정결과": 24, "규제 카테고리": 34,
        "법규 인체급성 기준(%)": 20, "인체급성 판정": 18,
        "법규 인체만성 기준(%)": 20, "인체만성 판정": 18,
        "법규 생태 기준(%)": 17, "생태 판정": 18, "구조초안 힌트": 22,
        "검증 상태": 34, "판단근거": 85, "추가 확인사항": 65,
        "법적 근거": 48,
    }
    for c in ws[1]:
        c.font = Font(name="맑은 고딕", bold=True, color="FFFFFF", size=11)
        c.fill = PatternFill(fill_type="solid", fgColor="1E5B77")
        c.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        ws.column_dimensions[c.column_letter].width = width.get(str(c.value), 20)

    status_col = list(plain.columns).index("판정결과") + 1
    id_col = list(plain.columns).index("인벤토리 ID") + 1
    cas_col = list(plain.columns).index("CAS번호") + 1
    for cells in list(ws.rows)[1:]:
        row_num = cells[0].row
        ws.row_dimensions[row_num].height = 92
        for c in cells:
            c.font = Font(name="맑은 고딕", size=10, color="213A4A")
            c.alignment = Alignment(vertical="top", wrap_text=True)
        color = RESULT_COLORS.get(_value(ws.cell(row_num, status_col).value))
        if color:
            ws.cell(row_num, status_col).fill = PatternFill(fill_type="solid", fgColor=color)
            ws.cell(row_num, status_col).font = Font(name="맑은 고딕", bold=True, color="183B55")
        for col in (cas_col, id_col):
            ws.cell(row_num, col).number_format = "@"
        ws.cell(row_num, 4).number_format = "0.###"
        for col in (7, 9, 11):
            ws.cell(row_num, col).number_format = "0.###"

    guide.freeze_panes = "A2"
    guide.auto_filter.ref = guide.dimensions
    guide.column_dimensions["A"].width = 29
    guide.column_dimensions["B"].width = 105
    guide.row_dimensions[1].height = 30
    for cell in guide[1]:
        cell.font = Font(name="맑은 고딕", bold=True, color="FFFFFF")
        cell.fill = PatternFill(fill_type="solid", fgColor="1E5B77")
        cell.alignment = Alignment(vertical="center")
    for row in list(guide.rows)[1:]:
        guide.row_dimensions[row[0].row].height = 43
        for cell in row:
            cell.alignment = Alignment(vertical="center", wrap_text=True)
    book.active = 0
    output = BytesIO()
    book.save(output)
    return output.getvalue()
