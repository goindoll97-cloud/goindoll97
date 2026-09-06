"""Reader-facing regulatory screening report."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import numpy as np
import pandas as pd

from broad_salt_identity import valid_cas_checksum
from scope_engine import clean
from trust_gate import STRUCTURE_STATUS_KO


HAZARD_LABELS = {
    "acute": "인체급성유해성물질",
    "chronic": "인체만성유해성물질",
    "eco": "생태유해성물질",
}

REGIME_LABELS = {
    "ACCIDENT_PREPAREDNESS": "사고대비물질",
    "RESTRICTED": "제한물질",
    "PROHIBITED": "금지물질",
}

PLAIN_COLUMNS = [
    "물질명",
    "CAS번호",
    "함량(%)",
    "판정결과",
    "규제 카테고리",
    "판단근거",
    "법적 근거",
]

READER_GUIDE_ROWS = [
    ("규제 해당", "공식 규제목록의 CAS번호와 직접 일치하거나, 사전 승인된 '~와 그 염류' 규칙에서 검증된 염류 구조로 확인되고 적용 함량·조건도 충족한 경우입니다."),
    ("보류", "규제 비해당을 의미하지 않습니다. 혼합물·반응생성물·화합물군·UVCB·일반 구조범위·기준물질 CAS 연결·이름 유사성·명시적 제외조건·자료 부족 등 사람이 추가로 확인해야 할 사항이 있는 경우입니다."),
    ("비해당", "현재 앱이 확인하는 규제범위와 필요한 조건을 모두 검토한 결과 적용되는 규제항목이 확인되지 않은 경우입니다. 국내 모든 법률에서 비규제라는 의미는 아닙니다."),
    ("직접 CAS 일치", "회사 물질의 CAS번호가 공식 규제목록에 직접 등재된 CAS번호와 일치하는 경우입니다. 자동확정이 가능한 핵심 경로입니다."),
    ("검증된 염류 구조", "회사 물질이 '~와 그 염류' 규정의 염류인지 승인된 염류 규칙과 PubChem으로 검증된 구조를 이용해 확인한 경우입니다. 자동확정이 가능한 핵심 경로입니다."),
    ("기준물질", "포괄 규제범위를 연결하기 위한 기준 물질 또는 기준 CAS입니다. 기준물질과의 연결만으로 규제 해당을 자동확정하지 않습니다."),
    ("포괄 규제범위 후보", "혼합물·반응생성물·화합물군·UVCB·일반 구조범위처럼 직접 CAS 또는 검증된 염류 구조만으로 확정하기 어려운 규제항목의 검토 후보입니다. 후보는 분류·검토 보조용이며 자동확정하지 않습니다."),
    ("명시적 제외", "법규 원문에서 특정 물질 또는 조건을 해당 규제범위에서 제외한다고 명시한 경우입니다. 명시적 제외조건은 이름·구조 유사성에 따른 포함 후보보다 항상 우선합니다. 다만 해당 규제항목에서 제외됐다는 사실만으로 다른 규제까지 모두 비해당으로 확정하지 않으므로 최종 결과는 보류될 수 있습니다."),
    ("법적 근거", "판정 근거가 된 규제 카테고리와 고유번호를 표시합니다. 예: 「인체등유해성물질」 고유번호 97-1-102."),
    ("AI 보조검토", "AI는 포괄 규제범위 후보의 분류와 추가 확인사항 정리에만 사용합니다. AI 판단은 규제 해당·비해당 자동확정 근거로 사용하지 않습니다."),
]

DECISION_POLICY_ROWS = [
    ("규제 해당", "직접 CAS 일치 또는 검증된 염류 구조 일치이며 함량·조건도 충족", "규제 카테고리별 기준과 실제 함량을 함께 표시"),
    ("보류", "직접 CAS·검증된 염류 구조 이외의 포괄 규제범위, 정보 부족, 적용조건 확인 또는 명시적 제외 후 다른 규제 적용 여부를 추가 확인해야 하는 경우", "가장 관련성 높은 규제명 하나와 검토 필요 사유만 표시"),
    ("비해당", "확인 가능한 규제범위와 필요한 조건을 모두 검토했으나 적용 항목이 확인되지 않은 경우", "확인한 범위와 미해당 근거를 표시"),
]


def build_reader_guide() -> pd.DataFrame:
    return pd.DataFrame(READER_GUIDE_ROWS, columns=["용어", "쉬운 설명"])


def build_decision_policy() -> pd.DataFrame:
    return pd.DataFrame(DECISION_POLICY_ROWS, columns=["판정결과", "언제 표시되는가", "자세한 내용"])


def _number(value: Any) -> float:
    return pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]


def _fmt(value: Any) -> str:
    number = _number(value)
    return f"{number:g}" if np.isfinite(number) else ""


def _plain_result(gate_decision: str, final_decision: str) -> str:
    gate = clean(gate_decision)
    if gate == "규제 해당(자동 확정)":
        return "규제 해당"
    if gate == "확인 범위 내 비해당":
        return "비해당"
    return "보류"


def _normalize_categories(value: Any, separator: str = " / ") -> str:
    parts = [p.strip() for p in re.split(r"[;/]", clean(value)) if p.strip()]
    return separator.join(dict.fromkeys(parts))


def _match_short(row: pd.Series) -> str:
    match_type = clean(row.get("match_type"))
    parent = clean(row.get("reference_parent_name")) or clean(row.get("regulatory_substance_name"))
    parent_cas = clean(row.get("reference_parent_cas"))
    if match_type == "직접 CAS":
        return "CAS번호가 직접 일치"
    if match_type == "검증된 염류 구조":
        ref = f"{parent}(CAS {parent_cas})" if parent_cas else parent
        return f"{ref}의 염류로 구조 확인"
    return "검토 후보와 연결"


def _entry_short(row: pd.Series) -> str:
    name = clean(row.get("regulatory_substance_name")) or clean(row.get("chemical_name"))
    designation = clean(row.get("designation_id"))
    base = f"「인체등유해성물질」 '{name}'"
    return f"{base}(고유번호 {designation})" if designation else base


def _hazard_sentences(rows: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    categories: list[str] = []
    sentences: list[str] = []
    citations: list[str] = []
    for _, row in rows.iterrows():
        concentration = _number(row.get("concentration_pct"))
        positive = []
        below = []
        pending = []
        for short, label in HAZARD_LABELS.items():
            decision = clean(row.get(f"{short}_decision"))
            threshold = _number(row.get(f"{short}_threshold_pct"))
            if decision == "규칙 없음" or not np.isfinite(threshold):
                continue
            if decision == "해당":
                categories.append(label)
                positive.append(f"{label} {_fmt(threshold)}% 이상")
            elif decision == "기준 미만":
                below.append(f"{label} {_fmt(threshold)}% 기준 미만")
            elif decision == "함량 확인 필요":
                pending.append(f"{label} 함량 확인 필요")
        head = f"{_entry_short(row)}와 {_match_short(row)}했습니다."
        if positive:
            conc = f"함량 {_fmt(concentration)}%" if np.isfinite(concentration) else "입력 함량"
            sentences.append(f"{head} {'·'.join(positive)} 기준에 {conc}로 규제 해당입니다.")
        elif below:
            sentences.append(f"{head} {'·'.join(below)}입니다.")
        elif pending:
            sentences.append(f"{head} {'·'.join(pending)}입니다.")
        else:
            sentences.append(f"{head} 적용 기준은 원문 확인이 필요합니다.")
        designation = clean(row.get("designation_id"))
        if designation:
            citations.append(f"「인체등유해성물질」 고유번호 {designation}")
    return categories, sentences, citations


def _cross_threshold(row: pd.Series) -> tuple[float, str]:
    threshold = _number(row.get("concentration_threshold_pct"))
    operator = clean(row.get("concentration_operator"))
    if np.isfinite(threshold):
        return threshold, operator or "이상"
    text = clean(row.get("source_text"))
    match = re.search(r"(\d+(?:\.\d+)?)\s*%\s*(이상|초과|이하|미만)?", text)
    if match:
        return float(match.group(1)), clean(match.group(2)) or "이상"
    return np.nan, ""


def _cross_sentences(rows: pd.DataFrame, concentration: float, confirmed_only: bool = False) -> tuple[list[str], list[str], list[str]]:
    categories: list[str] = []
    sentences: list[str] = []
    citations: list[str] = []
    if rows is None or rows.empty:
        return categories, sentences, citations
    for _, row in rows.iterrows():
        label = REGIME_LABELS.get(clean(row.get("regime")), clean(row.get("regime_label")))
        decision = clean(row.get("decision"))
        name = clean(row.get("substance_name")) or label
        designation = clean(row.get("designation_id"))
        if confirmed_only and decision != "규제 해당":
            continue
        threshold, operator = _cross_threshold(row)
        if decision == "규제 해당":
            categories.append(label)
            if np.isfinite(threshold) and np.isfinite(concentration):
                sentences.append(
                    f"「{label}」 목록에 CAS번호로 직접 등재되고 {_fmt(threshold)}% {operator} 기준에 함량 {_fmt(concentration)}%로 규제 해당입니다."
                )
            else:
                sentences.append(f"「{label}」 목록에 CAS번호로 직접 등재되고 적용 조건을 충족하여 규제 해당입니다.")
            if designation:
                citations.append(f"「{label}」 고유번호 {designation}")
        elif decision == "해당 후보·검토 필요":
            sentences.append(f"「{label}」 목록에서 같은 CAS번호가 후보로 확인되어 전문가 검토가 필요합니다.")
            if designation:
                citations.append(f"「{label}」 고유번호 {designation} (검토 후보)")
        elif decision == "기준 미만·조건 비충족":
            if np.isfinite(threshold) and np.isfinite(concentration):
                sentences.append(f"「{label}」 목록에는 등재되어 있으나 {_fmt(threshold)}% {operator} 기준에 함량 {_fmt(concentration)}%로 기준을 충족하지 않습니다.")
            else:
                sentences.append(f"「{label}」 목록에는 등재되어 있으나 적용 조건을 충족하지 않습니다.")
            if designation:
                citations.append(f"「{label}」 고유번호 {designation}")
        elif decision in {"추가정보 필요", "조건 검토 필요"}:
            missing = clean(row.get("missing_enterprise_fields")).replace(";", ", ")
            detail = f" 필요한 정보: {missing}." if missing else ""
            sentences.append(f"「{label}」 목록의 '{name}'과 일치하지만 적용 조건 확인이 필요합니다.{detail}")
            if designation:
                citations.append(f"「{label}」 고유번호 {designation} (검토 필요)")
    return categories, sentences, citations


def _simplify_hold(reason: str) -> str:
    text = clean(reason)
    if not text:
        return ""
    parts: list[str] = []
    for chunk in text.split(" / "):
        chunk = chunk.strip()
        if chunk.startswith("자료 커버리지 미완"):
            labels = []
            for item in chunk.split(":", 1)[1].split(";"):
                label = item.split("(")[0].strip()
                if label:
                    labels.append(label)
            if labels:
                parts.append(f"{'·'.join(dict.fromkeys(labels))} 확인 미완")
        elif chunk.startswith("CAS 형식"):
            parts.append("CAS번호 형식 확인 필요")
        elif any(chunk.startswith(label) for label in STRUCTURE_STATUS_KO.values()):
            parts.append("화학구조 확인 필요")
        elif chunk.startswith("적용에 필요한 입력값 누락"):
            parts.append(chunk)
        elif chunk.startswith("법규") or "고시" in chunk:
            parts.append("적용 고시 최신성 확인 필요")
        else:
            parts.append(chunk.rstrip("."))
    return "확인 필요: " + "; ".join(dict.fromkeys(parts)) + "." if parts else ""


def _candidate_context(item: pd.Series) -> list[dict]:
    try:
        raw = json.loads(clean(item.get("no_cas_scope_candidate_context")) or "[]")
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return [x for x in raw if isinstance(x, dict)]


def _candidate_priority(entry: dict) -> tuple:
    explicit = bool(entry.get("explicit_exception")) or clean(entry.get("match_type")) == "EXPLICIT_EXCEPTION_CAS"
    relation = clean(entry.get("rule_relation"))
    score = pd.to_numeric(pd.Series([entry.get("candidate_score")]), errors="coerce").iloc[0]
    score = float(score) if np.isfinite(score) else 0.0
    rank = 0 if explicit or relation == "EXPLICITLY_EXCLUDED" else 1
    return (rank, -score, clean(entry.get("designation_id")), clean(entry.get("regulatory_name")))


def _primary_candidate(item: pd.Series) -> dict:
    primary_id = clean(item.get("no_cas_scope_primary_designation_id"))
    primary_name = clean(item.get("no_cas_scope_primary_regulatory_name"))
    primary_category = _normalize_categories(item.get("no_cas_scope_primary_category"), "·")
    if primary_id or primary_name:
        return {
            "designation_id": primary_id,
            "regulatory_name": primary_name,
            "regulatory_categories": primary_category,
            "explicit_exception": bool(item.get("no_cas_scope_primary_explicit_exception", False)),
            "match_type": clean(item.get("no_cas_scope_primary_match_type")),
        }
    context = _candidate_context(item)
    if context:
        return sorted(context, key=_candidate_priority)[0]
    ids = [x.strip() for x in clean(item.get("no_cas_scope_designation_ids")).split(";") if x.strip()]
    names = [x.strip() for x in clean(item.get("no_cas_scope_regulatory_names")).split(";") if x.strip()]
    cats = _normalize_categories(item.get("no_cas_scope_regulatory_categories"), "·")
    return {
        "designation_id": ids[0] if ids else "",
        "regulatory_name": names[0] if names else "",
        "regulatory_categories": cats,
        "explicit_exception": False,
        "match_type": "",
    }


def _cap_reason(sentences: list[str], limit: int = 500) -> str:
    text = " ".join(dict.fromkeys(clean(s) for s in sentences if clean(s)))
    if len(text) <= limit:
        return text
    clipped = text[: limit - 3].rstrip(" ,;/·")
    return clipped + "..."


def build_plain_summary(
    summary: pd.DataFrame,
    evidence: Optional[pd.DataFrame],
    cross_detail: Optional[pd.DataFrame] = None,
    notice: str = "",
) -> pd.DataFrame:
    if summary is None or summary.empty:
        return pd.DataFrame(columns=PLAIN_COLUMNS)
    evidence = evidence if evidence is not None else pd.DataFrame()
    cross_detail = cross_detail if cross_detail is not None else pd.DataFrame()

    rows: list[dict] = []
    for _, item in summary.iterrows():
        material_id = clean(item.get("material_id"))
        result = _plain_result(item.get("gate_decision"), item.get("final_decision"))
        concentration = _number(item.get("concentration_pct"))

        confirmed = pd.DataFrame()
        held = pd.DataFrame()
        if not evidence.empty:
            mine = evidence[evidence["material_id"].map(clean).eq(material_id)]
            status = mine.get("evidence_status", pd.Series("", index=mine.index)).map(clean)
            confirmed = mine[status.ne("STRUCTURE_UNVERIFIED_HOLD")]
            held = mine[status.eq("STRUCTURE_UNVERIFIED_HOLD")]
        cross_rows = (
            cross_detail[cross_detail["material_id"].map(clean).eq(material_id)]
            if not cross_detail.empty else pd.DataFrame()
        )

        categories: list[str] = []
        sentences: list[str] = []
        citations: list[str] = []

        hazard_categories, hazard_sentences, hazard_citations = _hazard_sentences(confirmed)
        categories += hazard_categories
        citations += hazard_citations

        cross_categories, cross_sentences, cross_citations = _cross_sentences(
            cross_rows, concentration, confirmed_only=(result == "규제 해당")
        )
        categories += cross_categories
        citations += cross_citations

        primary = _primary_candidate(item)
        candidate_id = clean(primary.get("designation_id"))
        candidate_name = clean(primary.get("regulatory_name")) or "복합 규제범위"
        candidate_category = _normalize_categories(primary.get("regulatory_categories"), "·")
        explicit_exclusion = bool(primary.get("explicit_exception")) or clean(primary.get("match_type")) == "EXPLICIT_EXCEPTION_CAS"
        if clean(item.get("llm_rule_relation")) == "EXPLICITLY_EXCLUDED":
            explicit_exclusion = True

        if result == "규제 해당":
            sentences = hazard_sentences + cross_sentences
            if not sentences:
                sentences = ["직접 CAS 또는 검증된 염류 구조와 적용 조건이 확인되어 규제 해당으로 확정되었습니다."]

        elif result == "비해당":
            if explicit_exclusion and candidate_id:
                target = f"'{candidate_name}'(고유번호 {candidate_id})"
                sentences = [f"본 물질은 「인체등유해성물질」 {target}의 명시적 제외조건에 해당합니다."]
                citations.append(f"「인체등유해성물질」 고유번호 {candidate_id} (명시적 제외)")
            else:
                cas = clean(item.get("input_cas"))
                searchable = bool(cas and valid_cas_checksum(cas) and cas.replace("-", "").strip("0"))
                sentences = ["확인한 규제 카테고리에서 이 CAS번호에 적용되는 자동확정 대상 규제 항목이 확인되지 않았습니다."] if searchable else ["확인 범위에서 적용되는 자동확정 대상 규제 항목이 확인되지 않았습니다."]
            categories = []

        else:
            if explicit_exclusion and candidate_id:
                target = f"'{candidate_name}'(고유번호 {candidate_id})"
                sentences = [f"본 물질은 「인체등유해성물질」 {target}의 명시적 제외조건과 일치합니다. 다른 규제 적용 여부는 전문가 검토가 필요합니다."]
                citations.append(f"「인체등유해성물질」 고유번호 {candidate_id} (명시적 제외 후보)")
            elif candidate_id or candidate_name != "복합 규제범위":
                target = f"'{candidate_name}'" + (f"(고유번호 {candidate_id})" if candidate_id else "")
                prefix = f"{candidate_category} " if candidate_category else ""
                sentences = [f"본 물질은 {prefix}{target}과 관련된 포괄 규제범위 후보입니다. 자동확정하지 않고 전문가 검토가 필요합니다."]
                if candidate_id:
                    citations.append(f"「인체등유해성물질」 고유번호 {candidate_id} (검토 후보)")
            elif not held.empty:
                name = clean(held.iloc[0].get("regulatory_substance_name"))
                sentences = [f"'{name}'의 포괄 규제범위 후보이나 구조 확인이 부족해 전문가 검토가 필요합니다."]
            else:
                hold = _simplify_hold(item.get("gate_hold_reason"))
                sentences = [hold] if hold else cross_sentences[:1]
                if not sentences:
                    sentences = ["판정에 필요한 정보가 부족하여 전문가 검토가 필요합니다."]
            categories = []
            if candidate_category:
                categories = [x.strip() for x in candidate_category.split("·") if x.strip()]
            elif cross_categories:
                categories = cross_categories

        category_text = " / ".join(dict.fromkeys(x.strip() for x in categories if x and x.strip())) or "해당 없음"
        reason = _cap_reason(sentences, 500)

        rows.append({
            "물질명": clean(item.get("resolved_name")) or clean(item.get("input_chemical_name")),
            "CAS번호": clean(item.get("input_cas")),
            "함량(%)": concentration,
            "판정결과": result,
            "규제 카테고리": category_text,
            "판단근거": reason,
            "법적 근거": "; ".join(dict.fromkeys(citations)) or "-",
        })
    return pd.DataFrame(rows, columns=PLAIN_COLUMNS)


PLAIN_EVIDENCE_COLUMNS = {
    "material_id": "인벤토리 ID",
    "chemical_name": "물질명",
    "cas": "CAS번호",
    "regulatory_substance_name": "규제 물질명",
    "designation_id": "고유번호",
    "match_type": "매칭 방식",
    "acute_threshold_pct": "인체급성 기준(%)",
    "chronic_threshold_pct": "인체만성 기준(%)",
    "eco_threshold_pct": "생태 기준(%)",
    "notice": "규제체계",
    "source_text": "규제 원문",
}


def build_plain_evidence(evidence: Optional[pd.DataFrame]) -> pd.DataFrame:
    if evidence is None or evidence.empty:
        return pd.DataFrame(columns=list(PLAIN_EVIDENCE_COLUMNS.values()))
    columns = [col for col in PLAIN_EVIDENCE_COLUMNS if col in evidence.columns]
    out = evidence[columns].rename(columns=PLAIN_EVIDENCE_COLUMNS).copy()
    if "규제체계" in out.columns:
        out["규제체계"] = "인체등유해성물질"
    return out
