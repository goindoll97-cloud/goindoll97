"""Plain-language screening report.

The audit sheets record *how* a decision was reached.  This module records
*what* the decision means, in one sentence per material that a non-specialist
reader can check against the notice: which category, which listed substance,
how the material was matched to it, and how the concentration compares with the
threshold.
"""

from __future__ import annotations

import json
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
    ("규제 해당", "공식 법규의 물질과 일치하고 함량·조건도 충족한 상태입니다."),
    ("보류", "비규제라는 뜻이 아닙니다. 자료가 부족하거나 해석이 필요해 사람이 확인해야 합니다."),
    ("비해당", "이 앱이 확인한 자료 범위에서는 일치 항목이 없었습니다. 국내 모든 법률에서 비규제라는 뜻은 아닙니다."),
    ("직접 CAS 일치", "회사 CAS번호가 공식 목록의 CAS번호와 정확히 같은 경우입니다."),
    ("검증된 염류 구조", "기준물질과 염의 관계가 승인된 규칙과 확인된 구조로 입증된 경우입니다."),
    ("승인된 구조범위 규칙", "전문가 검증을 통과하여 자동판정에 사용하도록 승인된 구조 규칙입니다."),
    ("CAS 없는 규제범위 후보", "법규가 염류·화합물군·혼합물처럼 범위로 지정한 항목과 관련될 가능성입니다. 자동확정하지 않습니다."),
    ("염류", "산이나 염기가 다른 이온과 결합한 물질군입니다. 이름이 비슷하다는 이유만으로 같은 염류로 확정하지 않습니다."),
    ("화합물군", "특정 원소나 공통 구조를 포함하는 여러 물질을 묶어 지정한 범위입니다."),
    ("혼합물", "두 가지 이상 물질이 들어 있는 제품입니다. 구성성분 하나가 맞아도 혼합물 전체가 같다고 볼 수 없습니다."),
    ("반응생성물", "여러 원료의 반응으로 만들어져 조성이 달라질 수 있습니다. 원료 CAS만으로 동일성을 확정하지 않습니다."),
    ("UVCB", "조성이 일정하지 않거나 성분을 모두 특정하기 어려운 복합물질입니다. 조성·제조정보 확인이 필요합니다."),
    ("구성성분만 일치", "CAS가 규제 혼합물·반응생성물의 한 성분과만 같습니다. 규제물질 자체로 확정되지 않습니다."),
    ("명시적 제외", "해당 지정규정에서 제외한다고 적힌 경우입니다. 다른 규제 항목은 계속 확인해야 합니다."),
    ("법적 근거", "판정에 사용한 고시명·지정번호·규제 원문 등 확인 가능한 출처입니다."),
    ("AI 보조검토", "AI가 후보·조건·누락정보를 정리한 참고자료입니다. AI 설명만으로 해당·비해당을 확정하지 않습니다."),
]

DECISION_POLICY_ROWS = [
    ("규제 해당", "직접 CAS 또는 승인된 구조규칙으로 일치하고 최신 법규·함량·조건·근거가 모두 확인됨", "판단근거 열에 일치 방식과 기준을 표시"),
    ("보류", "염류 가능성, 복합 범위, 정보 누락·충돌 등 사람이 확인할 사항이 하나라도 있음", "판단근거 열에 구조·제외조건·부족한 정보를 표시"),
    ("비해당", "최신 법규와 필요한 규제범위를 모두 확인했지만 일치 항목이 없음", "판단근거 열에 확인한 범위와 함량 비교를 표시"),
]


def build_reader_guide() -> pd.DataFrame:
    return pd.DataFrame(READER_GUIDE_ROWS, columns=["용어", "쉬운 설명"])


def build_decision_policy() -> pd.DataFrame:
    return pd.DataFrame(
        DECISION_POLICY_ROWS,
        columns=["판정결과", "언제 표시되는가", "자세한 내용"],
    )


def _number(value: Any) -> float:
    return pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]


def _fmt(value: Any) -> str:
    number = _number(value)
    return f"{number:g}" if np.isfinite(number) else ""


def _join_semi(raw: str, sep: str) -> str:
    """Join a '; '-separated field with `sep`, without leaving the stray
    space that a naive `.replace(';', sep)` leaves behind (e.g. turning
    'A; B' into 'A /  B' instead of 'A / B')."""
    parts = [p.strip() for p in clean(raw).split(";") if p.strip()]
    return sep.join(dict.fromkeys(parts))


def _match_phrase(row: pd.Series) -> str:
    """Describe, in plain Korean, why this material was tied to the listed entry."""
    match_type = clean(row.get("match_type"))
    parent = clean(row.get("reference_parent_name")) or clean(row.get("regulatory_substance_name"))
    parent_cas = clean(row.get("reference_parent_cas"))
    if match_type == "직접 CAS" or (parent_cas and parent_cas == clean(row.get("cas"))):
        return "등재된 물질과 CAS번호가 직접 일치합니다"
    reference = f"'{parent}'(CAS {parent_cas})" if parent_cas else f"'{parent}'"
    if match_type == "검증된 염류 구조":
        return f"고시 기준물질 {reference}의 염류로 구조가 확인되었습니다"
    if match_type == "검증된 구조범위 규칙":
        scope = clean(row.get("scope_type"))
        if scope == "HYDRATE_SOLVATE":
            return f"고시 기준물질 {reference}의 수화물·용매화물로 확인되었습니다"
        if scope == "STEREOISOMER":
            return f"고시 기준물질 {reference}의 이성질체로 확인되었습니다"
        return f"고시 기준물질 {reference}에 대한 승인된 구조범위 규칙에 일치했습니다"
    return "등재된 물질과 CAS번호가 직접 일치합니다"


def _entry_phrase(row: pd.Series, notice: str) -> str:
    name = clean(row.get("regulatory_substance_name")) or clean(row.get("chemical_name"))
    designation = clean(row.get("designation_id"))
    label = f"「{clean(row.get('notice')) or clean(notice)}」 고시의 '{name}'"
    return f"{label}(지정번호 {designation})" if designation else label


def _hazard_sentences(rows: pd.DataFrame, notice: str) -> tuple[list[str], list[str], list[str]]:
    """One sentence per listed entry, followed by a clause per hazard category."""
    categories: list[str] = []
    sentences: list[str] = []
    citations: list[str] = []
    for _, row in rows.iterrows():
        concentration = _number(row.get("concentration_pct"))
        clauses: list[tuple[int, str]] = []
        for short, label in HAZARD_LABELS.items():
            decision = clean(row.get(f"{short}_decision"))
            threshold = _number(row.get(f"{short}_threshold_pct"))
            if decision == "규칙 없음" or not np.isfinite(threshold):
                continue
            if decision == "해당":
                categories.append(label)
                clauses.append(
                    (
                        0,
                        f"{label}: 기준 {_fmt(threshold)}% 이상, 실제 함량 "
                        f"{_fmt(concentration)}% → 규제 해당",
                    )
                )
            elif decision == "기준 미만":
                clauses.append(
                    (
                        1,
                        f"{label}: 기준 {_fmt(threshold)}% 이상, 실제 함량 "
                        f"{_fmt(concentration)}% → 기준 미만",
                    )
                )
            elif decision == "함량 확인 필요":
                clauses.append(
                    (0, f"{label}: 기준 {_fmt(threshold)}% 이상, 함량 미입력 → 확인 필요")
                )
        head = f"이 물질은 {_entry_phrase(row, notice)}에 대해 {_match_phrase(row)}."
        if clauses:
            body = " / ".join(text for _, text in sorted(clauses, key=lambda item: item[0]))
            sentences.append(f"{head} {body}.")
        else:
            sentences.append(f"{head} 이 지정번호에는 함량 기준이 연결되어 있지 않아 원문 확인이 필요합니다.")
        designation = clean(row.get("designation_id"))
        if designation:
            citations.append(
                f"{clean(row.get('notice')) or clean(notice)} 지정번호 {designation}"
            )
    return categories, sentences, citations


def _cross_sentences(rows: pd.DataFrame) -> tuple[list[str], list[str], list[str]]:
    categories: list[str] = []
    sentences: list[str] = []
    citations: list[str] = []
    if rows is None or rows.empty:
        return categories, sentences, citations
    for _, row in rows.iterrows():
        label = REGIME_LABELS.get(clean(row.get("regime")), clean(row.get("regime_label")))
        decision = clean(row.get("decision"))
        name = clean(row.get("substance_name")) or label
        source = clean(row.get("source_reference")) or clean(row.get("source_name"))
        if decision == "규제 해당":
            categories.append(label)
            sentences.append(
                f"이 물질은 {label} 목록에 CAS번호로 직접 등재되어 있으며 수치 조건도 충족하여 규제 대상입니다."
            )
            if source:
                citations.append(f"{label} {source}")
        elif decision == "해당 후보·검토 필요":
            sentences.append(
                f"공식 법령 API 원문의 {label} 목록에서 같은 CAS번호가 확인되었으나, "
                "자동 추출된 규칙이라 검증 전이므로 확정하지 않았습니다."
            )
        elif decision == "기준 미만·조건 비충족":
            detail = f" ({clean(row.get('reason'))})" if clean(row.get("reason")) else ""
            sentences.append(
                f"이 물질은 {label} 목록에 CAS번호로 등재되어 있으나 함량 조건을 "
                f"충족하지 않습니다.{detail}"
            )
            if source:
                citations.append(f"{label} {source}")
        elif decision in {"추가정보 필요", "조건 검토 필요"}:
            missing = clean(row.get("missing_enterprise_fields")).replace(";", ", ")
            detail = f" 필요한 정보: {missing}." if missing else ""
            sentences.append(
                f"이 물질은 {label} 목록의 '{name}'과 CAS번호가 일치하지만, "
                f"용도·수량 등 적용 조건을 확인해야 판정할 수 있습니다.{detail}"
            )
    return categories, sentences, citations


def _simplify_hold(reason: str) -> str:
    """Compress the gate's diagnostic text into one reader-facing sentence."""
    text = clean(reason)
    if not text:
        return ""
    parts: list[str] = []
    for chunk in text.split(" / "):
        chunk = chunk.strip()
        if chunk.startswith("자료 커버리지 미완"):
            missing: list[str] = []
            candidate: list[str] = []
            for item in chunk.split(":", 1)[1].split(";"):
                item = item.strip()
                if not item:
                    continue
                label = item.split("(")[0].strip()
                (candidate if "API 후보" in item else missing).append(label)
            if missing:
                parts.append(
                    f"{'·'.join(missing)} 목록 데이터가 아직 없어 해당 카테고리는 확인하지 못했습니다"
                )
            if candidate:
                parts.append(
                    f"{'·'.join(candidate)}은 공식 API 원문에서만 확인되어 검증 전 상태입니다"
                )
        elif chunk.startswith("CAS 형식"):
            parts.append("입력된 CAS번호가 유효하지 않아 목록 대조가 불가능합니다")
        elif any(chunk.startswith(label) for label in STRUCTURE_STATUS_KO.values()):
            # Genuine structure-verification holds always start with one of
            # these exact labels (see trust_gate.structure_gate). A naive
            # "구조" substring check would also match unrelated text like
            # "승인된 구조범위 규칙" -- i.e. "there is no approved structural-
            # range RULE for this compound group" -- which is a completely
            # different problem from "we could not confirm your structure."
            parts.append("화학구조가 확인되지 않아 염류·이성질체 범위를 판단하지 못했습니다")
        elif chunk.startswith("적용에 필요한 입력값 누락"):
            parts.append(chunk)
        elif chunk.startswith("법규") or "고시" in chunk:
            parts.append("적용 고시가 현행인지 확인되지 않았습니다")
        else:
            parts.append(chunk.rstrip("."))
    return "확인하지 못한 사항: " + "; ".join(dict.fromkeys(parts)) + "."


def _plain_result(gate_decision: str, final_decision: str) -> str:
    """Collapse all internal states into three reader-facing outcomes."""
    gate = clean(gate_decision)
    if gate == "규제 해당(자동 확정)":
        return "규제 해당"
    if gate == "확인 범위 내 비해당":
        return "비해당"
    return "보류"


def build_plain_summary(
    summary: pd.DataFrame,
    evidence: Optional[pd.DataFrame],
    cross_detail: Optional[pd.DataFrame] = None,
    notice: str = "",
) -> pd.DataFrame:
    """One row per material: name, CAS, concentration, result, category, reason."""
    if summary is None or summary.empty:
        return pd.DataFrame(columns=PLAIN_COLUMNS)
    evidence = evidence if evidence is not None else pd.DataFrame()
    cross_detail = cross_detail if cross_detail is not None else pd.DataFrame()

    rows: list[dict] = []
    for _, item in summary.iterrows():
        material_id = clean(item.get("material_id"))
        confirmed = pd.DataFrame()
        held = pd.DataFrame()
        if not evidence.empty:
            mine = evidence[evidence["material_id"].map(clean).eq(material_id)]
            status = mine.get("evidence_status", pd.Series("", index=mine.index)).map(clean)
            confirmed = mine[status.ne("STRUCTURE_UNVERIFIED_HOLD")]
            held = mine[status.eq("STRUCTURE_UNVERIFIED_HOLD")]
        cross_rows = (
            cross_detail[cross_detail["material_id"].map(clean).eq(material_id)]
            if not cross_detail.empty
            else pd.DataFrame()
        )

        categories, sentences, citations = _hazard_sentences(confirmed, notice)
        cross_categories, cross_sentences, cross_citations = _cross_sentences(cross_rows)
        categories += cross_categories
        sentences += cross_sentences
        citations += cross_citations

        result = _plain_result(item.get("gate_decision"), item.get("final_decision"))

        cas = clean(item.get("input_cas"))
        searchable_cas = bool(cas and valid_cas_checksum(cas) and cas.replace("-", "").strip("0"))
        final_decision = clean(item.get("final_decision"))
        scope_candidates = clean(item.get("no_cas_scope_designation_ids"))
        ai_status = clean(item.get("llm_status"))
        ai_candidates = clean(item.get("llm_candidate_designation_ids"))
        rule_relation = clean(item.get("llm_rule_relation"))
        ai_explanation = clean(item.get("llm_explanation_ko"))
        ai_quotes = clean(item.get("llm_evidence_quotes"))
        excluded_id = clean(item.get("no_cas_scope_excluded_designation_id"))
        excluded_name = clean(item.get("no_cas_scope_excluded_regulatory_name"))
        primary_id = clean(item.get("no_cas_scope_primary_designation_id"))
        primary_name = clean(item.get("no_cas_scope_primary_regulatory_name"))
        primary_category = clean(item.get("no_cas_scope_primary_category"))
        candidate_names = clean(item.get("no_cas_scope_regulatory_names"))
        candidate_categories = clean(item.get("no_cas_scope_regulatory_categories"))
        try:
            candidate_context = json.loads(clean(item.get("no_cas_scope_candidate_context")) or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            candidate_context = []
        candidate_scope_types = {
            clean(entry.get("scope_type"))
            for entry in candidate_context
            if isinstance(entry, dict)
        }
        relation_text = {
            "EXPLICITLY_EXCLUDED": "해당 지정규정의 명시적 제외 관계",
            "COMPONENT_ONLY": "혼합물·반응생성물의 구성성분만 일치",
            "INCLUDED_CANDIDATE": "총칭·구조범위 포함 후보",
            "INSUFFICIENT_INFORMATION": "관계 판단정보 부족",
        }.get(rule_relation, rule_relation)
        # An explicit exclusion condition (an isomer, salt, or CAS the notice
        # itself names as excluded) settles the question, whether it was
        # caught by the deterministic scope scan or by the AI's own reading
        # of the notice text -- it always outranks a "may apply" reading of
        # the same designation.
        context_exclusion = next(
            (e for e in candidate_context if isinstance(e, dict) and e.get("explicit_exception")),
            None,
        )
        explicitly_excluded = (
            bool(excluded_id) or rule_relation == "EXPLICITLY_EXCLUDED" or context_exclusion is not None
        )

        if result != "규제 해당":
            # A confirmed result states only its confirmed basis (built
            # above); everything below is candidate/hold reasoning that only
            # applies while the material is still open.
            if confirmed.empty and searchable_cas:
                if final_decision == "전문가 검토 필요" and held.empty:
                    # A complex-scope (anchor/contextual) hit that could not be
                    # auto-confirmed is NOT the same as a clean miss -- saying
                    # "no match found" here would misreport an open review as a
                    # settled negative.
                    if not scope_candidates and not explicitly_excluded:
                        designation = clean(item.get("review_reason")) or clean(item.get("decision_reason"))
                        sentences.append(
                            f"「{clean(notice)}」 목록의 복합 규제범위와 관련되어 검토가 필요합니다. "
                            f"({designation})" if designation
                            else f"「{clean(notice)}」 목록의 복합 규제범위와 관련되어 검토가 필요합니다."
                        )
                elif final_decision != "전문가 검토 필요" and not explicitly_excluded:
                    sentences.append(
                        f"「{clean(notice)}」 인체급성·인체만성·생태유해성물질 목록에서 이 CAS번호와 "
                        "일치하는 항목은 없었습니다."
                    )

            if not held.empty:
                names = _join_semi(
                    "; ".join(clean(row.get("regulatory_substance_name")) for _, row in held.iterrows()),
                    "', '",
                )
                sentences.append(
                    f"구조 비교로는 '{names}'의 규제범위에 들어갈 수 있으나, 사용한 화학구조가 "
                    "PubChem의 정확한 CAS 동의어로 확인되지 않아 확정하지 않았습니다."
                )

            hold_reason = _simplify_hold(item.get("gate_hold_reason"))
            if hold_reason and not scope_candidates and not explicitly_excluded:
                sentences.append(hold_reason)
            coverage = clean(item.get("coverage_summary"))
            if not hold_reason and coverage.startswith("자료 커버리지 미완"):
                sentences.append(_simplify_hold(coverage))

            if explicitly_excluded:
                # The exclusion condition is the whole story here -- it
                # replaces any "may apply"/candidate wording rather than
                # sitting alongside it.
                excl_id = excluded_id or clean((context_exclusion or {}).get("designation_id"))
                excl_name = excluded_name or clean((context_exclusion or {}).get("regulatory_name"))
                label = f"'{excl_name}'(지정번호 {excl_id})" if excl_id else f"'{excl_name}'"
                sentences.append(
                    f"본 물질은 {label}에서 명시적으로 제외한다고 적힌 대상에 해당하여 "
                    "이 지정규정의 규제범위에 포함되지 않습니다."
                )
                if excl_id:
                    citations.append(f"{clean(notice)} 지정번호 {excl_id} (명시적 제외)")
            else:
                # Show only the single most relevant candidate name -- not
                # every name that matched somewhere in the same designation
                # group, which is how an unrelated isomer's name ends up
                # riding along with the one actually in question.
                candidate_ids = primary_id or ai_candidates or scope_candidates
                if candidate_ids:
                    category_text = (
                        _join_semi(primary_category, "·") or _join_semi(candidate_categories, "·") or "규제물질"
                    )
                    name_text = (
                        primary_name
                        or (candidate_names.split(";")[0].strip() if candidate_names else "")
                        or "복합 규제범위"
                    )
                    target = f"{category_text}의 '{name_text}'(지정번호 {candidate_ids})"
                    if rule_relation == "COMPONENT_ONLY":
                        sentences.append(
                            f"본 물질은 {target}의 구성성분과 일치하지만, 혼합물·반응생성물 자체인지는 "
                            "확인되지 않았습니다. 전문가 검토가 필요합니다."
                        )
                    elif rule_relation == "INCLUDED_CANDIDATE" or "SALT_FAMILY" in candidate_scope_types:
                        sentences.append(
                            f"본 물질은 {target}에 해당될 가능성이 높습니다. 전문가 검토가 필요합니다."
                        )
                    elif ai_status and ai_status != "VALIDATED_OUTPUT":
                        sentences.append(
                            f"본 물질은 {target}과 관련될 가능성이 있습니다. AI 검토가 완료되지 않아 전문가 확인이 필요합니다."
                        )
                    else:
                        sentences.append(
                            f"본 물질은 {target}과 관련될 가능성이 있습니다. 전문가 검토가 필요합니다."
                        )
                    citations.append(f"{clean(notice)} 지정번호 {candidate_ids} (검토 후보)")

        if not sentences:
            sentences.append(clean(item.get("decision_reason")) or "판정 근거가 기록되지 않았습니다.")

        # Keep the reason short and single-purpose: a confirmed result states
        # its basis in at most two sentences; a hold or non-match states the
        # single most relevant reason rather than every clause collected
        # along the way.
        sentences = list(dict.fromkeys(sentences))
        sentences = sentences[:2] if result == "규제 해당" else sentences[:1]

        rows.append(
            {
                "물질명": clean(item.get("resolved_name"))
                or clean(item.get("input_chemical_name")),
                "CAS번호": clean(item.get("input_cas")),
                "함량(%)": _number(item.get("concentration_pct")),
                "판정결과": result,
                "규제 카테고리": (
                    " / ".join(dict.fromkeys(categories))
                    or _join_semi(primary_category, " / ")
                    or _join_semi(candidate_categories, " / ")
                    or "해당 없음"
                ),
                "판단근거": " ".join(sentences),
                "법적 근거": "; ".join(dict.fromkeys(citations)) or "-",
                "구조범위 후보 지정번호": scope_candidates,
                "AI 검토 상태": ai_status,
                "AI 검토 후보 지정번호": ai_candidates,
                "규칙 관계 분류": relation_text,
                "AI 검토 설명": ai_explanation,
                "AI 원문 근거": ai_quotes,
            }
        )
    return pd.DataFrame(rows, columns=PLAIN_COLUMNS)


PLAIN_EVIDENCE_COLUMNS = {
    "material_id": "인벤토리 ID",
    "chemical_name": "물질명",
    "cas": "CAS번호",
    "regulatory_substance_name": "규제 물질명",
    "designation_id": "지정번호",
    "match_type": "매칭 방식",
    "acute_threshold_pct": "인체급성 기준(%)",
    "chronic_threshold_pct": "인체만성 기준(%)",
    "eco_threshold_pct": "생태 기준(%)",
    "notice": "적용 고시",
    "source_text": "규제 원문",
}


def build_plain_evidence(evidence: Optional[pd.DataFrame]) -> pd.DataFrame:
    """A short citation table: the listed entry and the notice text behind it."""
    if evidence is None or evidence.empty:
        return pd.DataFrame(columns=list(PLAIN_EVIDENCE_COLUMNS.values()))
    columns = [col for col in PLAIN_EVIDENCE_COLUMNS if col in evidence.columns]
    return evidence[columns].rename(columns=PLAIN_EVIDENCE_COLUMNS)
