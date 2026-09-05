"""Six-stage trust gate for company chemical regulatory screening.

The gate runs after the deterministic engines and never changes a rule result.
It decides only whether a result may be *auto-confirmed* or must be *held*:

    S1 CAS 검증        -> is the identifier itself trustworthy?
    S2 카테고리 검색    -> were all five regulatory categories actually searched?
    S3 구조범위 보완    -> is the structure evidence traceable to a verified CAS?
    S4 함량·조건 적용   -> were concentration/use/quantity conditions applicable?
    S5 법률근거 검증    -> does every positive hit carry an official citation?
    S6 자동확정/보류    -> confirm, or hold with the exact missing item named.

Design rule: a negative result ("비해당") is a claim that requires as much
evidence as a positive one.  When any stage is incomplete the material is held,
never silently reported as unregulated.
"""

from __future__ import annotations

from typing import Any, Optional

import numpy as np
import pandas as pd

import broad_salt_identity as salt
from scope_engine import clean


REGIME_ORDER = [
    "HUMAN_HAZARD",
    "ACCIDENT_PREPAREDNESS",
    "RESTRICTED",
    "PROHIBITED",
]

REGIME_LABELS = {
    "HUMAN_HAZARD": "인체등유해성물질",
    "ACCIDENT_PREPAREDNESS": "사고대비물질",
    "RESTRICTED": "제한물질",
    "PROHIBITED": "금지물질",
}

STAGES = [
    ("S1", "CAS 검증"),
    ("S2", "전 규제 카테고리 검색"),
    ("S3", "구조범위 보완"),
    ("S4", "함량·조건 적용"),
    ("S5", "법률근거 검증"),
    ("S6", "자동확정/보류"),
]

GATE_CONFIRMED = "자동 확정"
GATE_HOLD = "보류"

PASS = "통과"
HOLD = "보류"
FAIL = "미충족"

POSITIVE_DECISIONS = {"규제 해당"}
CANDIDATE_DECISIONS = {"해당 후보·검토 필요", "규제 해당 후보"}
# company_engine.py's own human-hazard final_decision uses a different
# vocabulary than the cross-regime CANDIDATE_DECISIONS above (e.g. an
# anchor/contextual CAS reference that needs a person to confirm scope, a
# missing concentration, or an invalid CAS). Without checking for these too,
# the gate never sees them as unresolved and can wrongly confirm "확인 범위
# 내 비해당" for a material company_engine itself flagged as needing review.
HUMAN_HAZARD_REVIEW_DECISIONS = {"전문가 검토 필요", "정보 부족", "물질정보 확인 실패"}

# Only these evidence paths may support an automatic positive decision.  The
# LLM is deliberately absent: its output is review assistance, never proof.
AUTO_CONFIRM_MATCH_TYPES = {
    "직접 CAS",
    "검증된 염류 구조",
    "검증된 구조범위 규칙",
}
MANUAL_ONLY_SCOPE_TYPES = {
    "DERIVATIVE",
    "COMPOUND_GROUP",
    "REACTION_PRODUCT",
    "POLYMER_UVCB",
    "MANUAL_REVIEW",
    "MIXTURE",
    "NAME_OR_UVCB",
}


# ---------------------------------------------------------------------------
# S1  CAS verification
# ---------------------------------------------------------------------------
def _identity_status(row: Any) -> str:
    """Return the PubChem status without the cache prefix."""
    value = clean(row.get("identity_status")) or clean(row.get("structure_status"))
    return value[len("CACHED_") :] if value.startswith("CACHED_") else value


def cas_gate(row: Any) -> dict:
    """Stage 1.  A CAS may be compared to the official lists only when it is
    syntactically and checksum valid.  PubChem agreement is recorded here but is
    enforced in stage 3, because a direct-CAS hit on the approved master is
    itself authoritative identity evidence."""
    cas = clean(row.get("input_cas")) or clean(row.get("cas"))
    status = _identity_status(row)
    if not cas:
        return {
            "stage_status": FAIL,
            "code": "CAS_MISSING",
            "detail": "CAS가 입력되지 않아 공식 목록과 정확히 대조할 수 없습니다.",
        }
    if not salt.valid_cas_checksum(cas) or not cas.replace("-", "").strip("0"):
        return {
            "stage_status": FAIL,
            "code": "CAS_INVALID_CHECKSUM",
            "detail": f"CAS 형식·체크섬 검증에 실패했습니다({cas}).",
        }
    if status in {"PUBCHEM_CAS_SYNONYM_MISMATCH", "PUBCHEM_MULTIPLE_CANDIDATES_REVIEW"}:
        return {
            "stage_status": HOLD,
            "code": "CAS_IDENTITY_CONFLICT",
            "detail": "CAS 형식은 유효하지만 PubChem 동의어 대조에서 물질 식별이 일치하지 않았습니다.",
        }
    if status == "PUBCHEM_EXACT_CAS_VERIFIED":
        return {
            "stage_status": PASS,
            "code": "CAS_PUBCHEM_EXACT_VERIFIED",
            "detail": "체크섬이 유효하고 PubChem 정확 CAS 동의어로 물질이 확인되었습니다.",
        }
    return {
        "stage_status": PASS,
        "code": "CAS_FORMAT_VALID",
        "detail": "체크섬이 유효합니다. 외부 구조 데이터베이스 확인은 수행되지 않았습니다.",
    }


# ---------------------------------------------------------------------------
# S3  Structure evidence
# ---------------------------------------------------------------------------
def structure_evidence_status(row: Any) -> str:
    """Classify how far the structure used for scope matching can be traced.

    ``USER_SUPPLIED`` SMILES are accepted for automatic scope matching only when
    they reproduce the connectivity of the PubChem record that carries the exact
    CAS synonym.  An unchecked user string is never sufficient on its own.
    """
    smiles = clean(row.get("smiles"))
    if not smiles:
        return "NO_STRUCTURE"
    source = clean(row.get("structure_smiles_source")) or clean(row.get("identity_source"))
    status = _identity_status(row)
    if source == "PUBCHEM_PUG_REST" and status == "PUBCHEM_EXACT_CAS_VERIFIED":
        return "PUBCHEM_CAS_VERIFIED"
    if source == "USER_SUPPLIED":
        reference = clean(row.get("pubchem_connectivity_smiles")) or clean(
            row.get("pubchem_isomeric_smiles")
        )
        if reference and status == "PUBCHEM_EXACT_CAS_VERIFIED":
            user_key = salt.parent_connectivity_key(smiles)
            ref_key = salt.parent_connectivity_key(reference)
            if user_key and ref_key and user_key == ref_key:
                return "USER_SMILES_PUBCHEM_CONFIRMED"
            return "USER_SMILES_PUBCHEM_CONFLICT"
        return "USER_SMILES_UNVERIFIED"
    return "STRUCTURE_SOURCE_UNKNOWN"


STRUCTURE_AUTOMATABLE = {"PUBCHEM_CAS_VERIFIED", "USER_SMILES_PUBCHEM_CONFIRMED"}

STRUCTURE_STATUS_KO = {
    "PUBCHEM_CAS_VERIFIED": "PubChem 정확 CAS 검증 구조",
    "USER_SMILES_PUBCHEM_CONFIRMED": "사용자 SMILES·PubChem 연결성 일치",
    "USER_SMILES_PUBCHEM_CONFLICT": "사용자 SMILES와 PubChem 구조 불일치",
    "USER_SMILES_UNVERIFIED": "사용자 SMILES 미검증",
    "STRUCTURE_SOURCE_UNKNOWN": "구조 출처 불명",
    "NO_STRUCTURE": "구조정보 없음",
}


def structure_gate(row: Any) -> dict:
    status = clean(row.get("structure_evidence_status")) or structure_evidence_status(row)
    label = STRUCTURE_STATUS_KO.get(status, status)
    if status in STRUCTURE_AUTOMATABLE:
        return {"stage_status": PASS, "code": status, "detail": f"{label}로 구조범위 규칙 적용이 가능합니다."}
    if status == "USER_SMILES_PUBCHEM_CONFLICT":
        return {"stage_status": FAIL, "code": status, "detail": f"{label}. 구조 기반 판정을 적용하지 않았습니다."}
    return {
        "stage_status": HOLD,
        "code": status,
        "detail": f"{label}. 염류·이성질체 등 구조범위 규정 해당 여부를 자동 확정할 수 없습니다.",
    }


# ---------------------------------------------------------------------------
# S2  Category coverage
# ---------------------------------------------------------------------------
def _int(value: Any) -> int:
    number = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    return int(number) if np.isfinite(number) else 0


def cross_rule_availability(
    approved_rules: Optional[pd.DataFrame],
    candidate_rules: Optional[pd.DataFrame],
    api_status: Optional[pd.DataFrame] = None,
) -> dict[str, dict]:
    """Count the rules actually loaded for each non-human-hazard regime."""
    availability: dict[str, dict] = {}
    api_lookup: dict[str, str] = {}
    if api_status is not None and not api_status.empty and "regime" in api_status.columns:
        for _, row in api_status.iterrows():
            api_lookup[clean(row.get("regime"))] = clean(row.get("status_ko"))
    for regime in REGIME_ORDER[1:]:
        approved_n = 0
        candidate_n = 0
        if approved_rules is not None and not approved_rules.empty:
            approved_n = int(approved_rules["regime"].map(clean).eq(regime).sum())
        if candidate_rules is not None and not candidate_rules.empty:
            candidate_n = int(candidate_rules["regime"].map(clean).eq(regime).sum())
            if regime in {"RESTRICTED", "PROHIBITED"}:
                # The restricted/prohibited notice is published as one document.
                # Rows the parser could not split still cover both regimes.
                candidate_n += int(
                    candidate_rules["regime"]
                    .map(clean)
                    .str.startswith("RESTRICTED_PROHIBITED")
                    .sum()
                )
        availability[regime] = {
            "approved_rows": approved_n,
            "candidate_rows": candidate_n,
            "api_status_ko": api_lookup.get(regime, api_lookup.get("RESTRICTED_PROHIBITED", "")),
        }
    return availability


def build_category_search_log(
    summary: pd.DataFrame,
    cross_detail: Optional[pd.DataFrame],
    availability: dict[str, dict],
    master_notice: str,
    master_rows: int,
) -> pd.DataFrame:
    """One row per material per regulatory category.

    This is the primary-search record the reviewer needs: it shows that every
    substance was looked up in all five categories, and which of those lookups
    rested on an approved rule set rather than on missing data.
    """
    rows: list[dict] = []
    detail = cross_detail if cross_detail is not None else pd.DataFrame()

    # Build the cross-regime hit counts once.  The previous implementation
    # rescanned the complete detail table for every material and every regime,
    # which became very slow for company inventories with 1,000+ rows.
    cross_hit_counts: dict[tuple[str, str], int] = {}
    if not detail.empty and {"material_id", "regime"}.issubset(detail.columns):
        material_ids = detail["material_id"].map(clean)
        regimes = detail["regime"].map(clean)
        for material_id_value, regime_value in zip(material_ids, regimes):
            resolved_regimes = [regime_value]
            if regime_value.startswith("RESTRICTED_PROHIBITED"):
                resolved_regimes = ["RESTRICTED", "PROHIBITED"]
            for resolved_regime in resolved_regimes:
                key = (material_id_value, resolved_regime)
                cross_hit_counts[key] = cross_hit_counts.get(key, 0) + 1
    for _, item in summary.iterrows():
        material_id = clean(item.get("material_id"))
        cas = clean(item.get("input_cas")) or clean(item.get("cas"))
        cas_ok = bool(cas) and salt.valid_cas_checksum(cas)
        for regime in REGIME_ORDER:
            if regime == "HUMAN_HAZARD":
                direct = _int(item.get("hh_direct_hits"))
                contextual = _int(item.get("hh_contextual_hits"))
                excluded = _int(item.get("hh_excluded_hits"))
                structural = _int(item.get("hh_structure_hits"))
                unverified = _int(item.get("hh_structure_unverified_hits"))
                hits = direct + structural
                rule_source = f"승인 규제 마스터 {master_notice}"
                rule_rows = master_rows
                coverage = "COVERAGE_OK" if rule_rows else "COVERAGE_NO_RULE_DATA"
                found = (
                    f"직접 CAS {direct}건 / 구조범위 {structural}건 / "
                    f"복합범위 원문 {contextual}건 / 예외 CAS {excluded}건"
                )
                if unverified:
                    found += f" / 구조근거 미검증 {unverified}건"
            else:
                info = availability.get(regime, {})
                approved_n = int(info.get("approved_rows", 0))
                candidate_n = int(info.get("candidate_rows", 0))
                rule_rows = approved_n + candidate_n
                if approved_n and candidate_n:
                    rule_source = f"승인 규칙 {approved_n}행 + API 후보 {candidate_n}행"
                elif approved_n:
                    rule_source = f"승인 규칙 {approved_n}행"
                elif candidate_n:
                    rule_source = f"API 후보 {candidate_n}행(검증 전)"
                else:
                    rule_source = "규칙 데이터 없음"
                if not rule_rows:
                    coverage = "COVERAGE_NO_RULE_DATA"
                elif not approved_n:
                    coverage = "COVERAGE_CANDIDATE_ONLY"
                else:
                    coverage = "COVERAGE_OK"
                hits = cross_hit_counts.get((material_id, regime), 0)
                found = f"직접 CAS 일치 {hits}건"
            if not cas_ok:
                coverage = "COVERAGE_CAS_NOT_VERIFIED"
            rows.append(
                {
                    "material_id": material_id,
                    "chemical_name": clean(item.get("input_chemical_name"))
                    or clean(item.get("chemical_name")),
                    "cas": cas,
                    "regime": regime,
                    "regime_label": REGIME_LABELS[regime],
                    "searched": "예" if cas_ok and rule_rows else "아니오",
                    "rule_source": rule_source,
                    "rule_rows": rule_rows,
                    "hit_count": hits,
                    "search_result": found,
                    "coverage_status": coverage,
                    "coverage_status_ko": COVERAGE_KO[coverage],
                }
            )
    return pd.DataFrame(rows)


COVERAGE_KO = {
    "COVERAGE_OK": "승인 규칙으로 검색 완료",
    "COVERAGE_CANDIDATE_ONLY": "검증 전 API 후보만 존재",
    "COVERAGE_NO_RULE_DATA": "해당 카테고리 규칙 데이터 없음",
    "COVERAGE_CAS_NOT_VERIFIED": "CAS 미검증으로 검색 불가",
}


def coverage_gate(material_id: str, category_log: pd.DataFrame) -> dict:
    if category_log is None or category_log.empty:
        return {"stage_status": HOLD, "code": "NO_SEARCH_LOG", "detail": "카테고리 검색기록이 없습니다."}
    rows = category_log[category_log["material_id"].map(clean).eq(clean(material_id))]
    if rows.empty:
        return {"stage_status": HOLD, "code": "NO_SEARCH_LOG", "detail": "카테고리 검색기록이 없습니다."}
    gaps = rows[rows["coverage_status"].ne("COVERAGE_OK")]
    if gaps.empty:
        return {
            "stage_status": PASS,
            "code": "ALL_CATEGORIES_SEARCHED",
            "detail": f"{len(rows)}개 규제 카테고리를 모두 승인 규칙으로 검색했습니다.",
        }
    labels = "; ".join(
        f"{row['regime_label']}({row['coverage_status_ko']})" for _, row in gaps.iterrows()
    )
    return {
        "stage_status": HOLD,
        "code": "CATEGORY_COVERAGE_INCOMPLETE",
        "detail": f"자료 커버리지 미완: {labels}",
    }


# ---------------------------------------------------------------------------
# S4  Concentration and enterprise conditions
# ---------------------------------------------------------------------------
def condition_gate(row: Any, cross_rows: pd.DataFrame) -> dict:
    missing: list[str] = []
    concentration = pd.to_numeric(
        pd.Series([row.get("concentration_pct")]), errors="coerce"
    ).iloc[0]
    category_values = [
        clean(row.get("acute_decision")),
        clean(row.get("chronic_decision")),
        clean(row.get("eco_decision")),
    ]
    if "함량 확인 필요" in category_values:
        missing.append("함량(%)")
    if cross_rows is not None and not cross_rows.empty:
        for value in cross_rows.get("missing_enterprise_fields", pd.Series(dtype=str)):
            for field in str(value).split(";"):
                if clean(field) and clean(field) not in missing:
                    missing.append(clean(field))
        if cross_rows["decision"].map(clean).eq("조건 검토 필요").any():
            missing.append("용도·시행일·허가조건 원문 대조")
    if missing:
        return {
            "stage_status": HOLD,
            "code": "ENTERPRISE_INPUT_MISSING",
            "detail": "적용에 필요한 입력값 누락: " + ", ".join(dict.fromkeys(missing)),
        }
    if not np.isfinite(concentration):
        return {
            "stage_status": PASS,
            "code": "NO_THRESHOLD_APPLICABLE",
            "detail": "일치한 규칙에 함량 기준이 없어 함량 조건은 적용 대상이 아닙니다.",
        }
    return {
        "stage_status": PASS,
        "code": "CONDITIONS_APPLIED",
        "detail": f"입력 함량 {concentration:g}%와 규정 조건을 모두 적용했습니다.",
    }


# ---------------------------------------------------------------------------
# S5  Legal basis
# ---------------------------------------------------------------------------
def _truthy(value: Any) -> bool:
    return clean(value).lower() in {"1", "true", "yes", "y", "예", "확인", "verified"}


def build_legal_basis(
    evidence: pd.DataFrame,
    cross_detail: Optional[pd.DataFrame],
    master_notice: str,
) -> pd.DataFrame:
    """Per-material citation table used by both the screen and the workbook."""
    rows: list[dict] = []
    if evidence is not None and not evidence.empty:
        for _, item in evidence.iterrows():
            confirmed = clean(item.get("evidence_status")) != "STRUCTURE_UNVERIFIED_HOLD"
            complete = bool(
                clean(item.get("designation_id"))
                and clean(item.get("source_text"))
                and clean(item.get("notice"))
                and _truthy(item.get("authoritative_source"))
            )
            rows.append(
                {
                    "material_id": clean(item.get("material_id")),
                    "chemical_name": clean(item.get("chemical_name")),
                    "cas": clean(item.get("cas")),
                    "regime": "HUMAN_HAZARD",
                    "regime_label": REGIME_LABELS["HUMAN_HAZARD"],
                    "designation_id": clean(item.get("designation_id")),
                    "regulatory_substance_name": clean(item.get("regulatory_substance_name")),
                    "match_type": clean(item.get("match_type")),
                    "scope_type": clean(item.get("scope_type")),
                    "notice": clean(item.get("notice")) or clean(master_notice),
                    "effective_date": "",
                    "source_text": clean(item.get("source_text")),
                    "source_reference": clean(item.get("regulatory_source")),
                    "official_source_verified": "예" if _truthy(item.get("authoritative_source")) else "아니오",
                    "evidence_status": clean(item.get("evidence_status")) or "CONFIRMED_RULE_MATCH",
                    "basis_status": (
                        "OFFICIAL_BASIS_VERIFIED"
                        if complete and confirmed
                        else "BASIS_HOLD_STRUCTURE_UNVERIFIED"
                        if not confirmed
                        else "BASIS_INCOMPLETE"
                    ),
                }
            )
    if cross_detail is not None and not cross_detail.empty:
        for _, item in cross_detail.iterrows():
            approved = clean(item.get("rule_source_status")) == "APPROVED"
            complete = bool(
                approved
                and clean(item.get("source_text"))
                and (clean(item.get("source_reference")) or clean(item.get("source_name")))
            )
            rows.append(
                {
                    "material_id": clean(item.get("material_id")),
                    "chemical_name": clean(item.get("chemical_name")),
                    "cas": clean(item.get("cas")),
                    "regime": clean(item.get("regime")),
                    "regime_label": clean(item.get("regime_label")),
                    "designation_id": clean(item.get("designation_id")),
                    "regulatory_substance_name": clean(item.get("substance_name")),
                    "match_type": "직접 CAS",
                    "scope_type": clean(item.get("scope_type")),
                    "notice": clean(item.get("source_reference")),
                    "effective_date": clean(item.get("official_effective_date")),
                    "source_text": clean(item.get("source_text")),
                    "source_reference": clean(item.get("source_name")),
                    "official_source_verified": "예" if approved else "아니오",
                    "evidence_status": clean(item.get("rule_source_status")),
                    "basis_status": (
                        "OFFICIAL_BASIS_VERIFIED"
                        if complete
                        else "BASIS_CANDIDATE_UNVERIFIED"
                        if not approved
                        else "BASIS_INCOMPLETE"
                    ),
                }
            )
    return pd.DataFrame(rows)


def legal_basis_gate(material_id: str, legal_basis: pd.DataFrame, is_positive: bool) -> dict:
    if not is_positive:
        return {
            "stage_status": PASS,
            "code": "NO_POSITIVE_MATCH",
            "detail": "확정 대상 양성 판정이 없어 법률근거 검증 대상이 아닙니다.",
        }
    if legal_basis is None or legal_basis.empty:
        return {"stage_status": HOLD, "code": "NO_BASIS_RECORD", "detail": "법률근거 기록이 없습니다."}
    rows = legal_basis[legal_basis["material_id"].map(clean).eq(clean(material_id))]
    verified = rows[rows["basis_status"].eq("OFFICIAL_BASIS_VERIFIED")]
    if verified.empty:
        return {
            "stage_status": HOLD,
            "code": "BASIS_NOT_VERIFIED",
            "detail": "양성 판정에 연결된 공식 근거(지정번호·고시·원문·출처)가 확인되지 않았습니다.",
        }
    ids = "; ".join(dict.fromkeys(clean(v) for v in verified["designation_id"] if clean(v)))
    notices = "; ".join(dict.fromkeys(clean(v) for v in verified["notice"] if clean(v)))
    return {
        "stage_status": PASS,
        "code": "OFFICIAL_BASIS_VERIFIED",
        "detail": f"공식 근거 확인 지정번호 {ids or '기재 없음'} / 고시 {notices or '기재 없음'}",
    }


def automation_basis_gate(evidence_rows: pd.DataFrame, cross_rows: pd.DataFrame) -> dict:
    """Allow auto-confirmation only through a narrow, deterministic evidence path."""
    human_positive = pd.DataFrame()
    if evidence_rows is not None and not evidence_rows.empty:
        decision_columns = [
            column for column in ("acute_decision", "chronic_decision", "eco_decision")
            if column in evidence_rows.columns
        ]
        # Older audit fixtures do not carry category decision columns.  In that
        # case the caller's already-established positive result and the
        # confirmed evidence row are sufficient to evaluate the evidence path.
        positive_mask = pd.Series(not decision_columns, index=evidence_rows.index)
        for column in decision_columns:
            positive_mask |= evidence_rows[column].map(clean).eq("해당")
        status = evidence_rows.get(
            "evidence_status", pd.Series("CONFIRMED_RULE_MATCH", index=evidence_rows.index)
        ).map(clean)
        human_positive = evidence_rows[positive_mask & status.ne("STRUCTURE_UNVERIFIED_HOLD")]

    cross_positive = pd.DataFrame()
    if cross_rows is not None and not cross_rows.empty and "decision" in cross_rows.columns:
        cross_positive = cross_rows[cross_rows["decision"].map(clean).eq("규제 해당")]

    if human_positive.empty and cross_positive.empty:
        return {"stage_status": HOLD, "code": "NO_AUTOMATABLE_POSITIVE", "detail": "자동확정 가능한 양성 근거가 없습니다."}

    # Other regulatory regimes currently enter the approved engine only by
    # direct CAS.  Their approved positive is therefore deterministic.
    if not cross_positive.empty:
        return {"stage_status": PASS, "code": "APPROVED_DIRECT_CAS_PATH", "detail": "승인 규칙의 직접 CAS 근거입니다."}

    for _, row in human_positive.iterrows():
        match_type = clean(row.get("match_type"))
        scope_type = clean(row.get("scope_type"))
        if match_type == "직접 CAS":
            return {"stage_status": PASS, "code": "DIRECT_CAS_PATH", "detail": "공식 목록의 직접 CAS 근거입니다."}
        if match_type in AUTO_CONFIRM_MATCH_TYPES and scope_type not in MANUAL_ONLY_SCOPE_TYPES:
            return {"stage_status": PASS, "code": "APPROVED_STRUCTURE_PATH", "detail": "승인·검증된 구조범위 근거입니다."}

    return {
        "stage_status": HOLD,
        "code": "COMPLEX_SCOPE_MANUAL_ONLY",
        "detail": "포괄적 염류·유도체·화합물군·혼합물·반응생성물·UVCB는 승인된 안전 경로가 없어 사람이 확인해야 합니다.",
    }


# ---------------------------------------------------------------------------
# S6  Confirm or hold
# ---------------------------------------------------------------------------
def law_version_gate(api_status: dict) -> dict:
    sync = clean((api_status or {}).get("sync_status"))
    if sync == "CURRENT":
        return {
            "stage_status": PASS,
            "code": "LAW_VERSION_CURRENT",
            "detail": f"공식 API 확인 고시와 승인 마스터가 일치합니다({clean(api_status.get('official_notice'))}).",
        }
    if sync == "UPDATE_PENDING":
        return {
            "stage_status": HOLD,
            "code": "LAW_VERSION_UPDATE_PENDING",
            "detail": "공식 API에서 더 최신 고시가 확인되었으나 아직 검증·승인되지 않았습니다.",
        }
    if sync == "LOCAL_NEWER":
        return {
            "stage_status": HOLD,
            "code": "LAW_VERSION_LOCAL_NEWER",
            "detail": "로컬 승인본이 공식 확인본보다 최신으로 표시되어 버전 대조가 필요합니다.",
        }
    return {
        "stage_status": HOLD,
        "code": "LAW_VERSION_UNVERIFIED",
        "detail": "법규 API 확인이 수행되지 않아 적용 고시가 현행인지 확정할 수 없습니다.",
    }


def _is_positive(row: Any) -> bool:
    values = [clean(row.get("final_decision"))]
    for regime in REGIME_ORDER[1:]:
        values.append(clean(row.get(f"{regime}_decision")))
    return any(value in POSITIVE_DECISIONS for value in values)


def _is_candidate(row: Any) -> bool:
    values = [clean(row.get(f"{regime}_decision")) for regime in REGIME_ORDER[1:]]
    values.append(clean(row.get("overall_decision")))
    values.append(clean(row.get("final_decision")))
    if any(value in CANDIDATE_DECISIONS for value in values):
        return True
    return clean(row.get("final_decision")) in HUMAN_HAZARD_REVIEW_DECISIONS


def apply_trust_gate(
    summary: pd.DataFrame,
    evidence: pd.DataFrame,
    cross_detail: Optional[pd.DataFrame],
    category_log: pd.DataFrame,
    legal_basis: pd.DataFrame,
    api_status: Optional[dict] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return the summary with gate columns plus a per-stage audit log."""
    out = summary.copy()
    law_gate = law_version_gate(api_status or {})
    gate_rows: list[dict] = []
    decisions: list[str] = []
    statuses: list[str] = []
    hold_reasons: list[str] = []
    basis_summary: list[str] = []
    coverage_summary: list[str] = []

    detail = cross_detail if cross_detail is not None else pd.DataFrame()

    # Index the three audit tables once instead of filtering each complete
    # DataFrame again for every inventory row.  The gate decisions themselves
    # are unchanged; only the lookup path is different.
    def grouped_by_material(frame: Optional[pd.DataFrame]) -> dict[str, pd.DataFrame]:
        if frame is None or frame.empty or "material_id" not in frame.columns:
            return {}
        normalized = frame.assign(_gate_material_id=frame["material_id"].map(clean))
        return {
            str(material_id): group.drop(columns="_gate_material_id")
            for material_id, group in normalized.groupby("_gate_material_id", sort=False)
        }

    detail_by_material = grouped_by_material(detail)
    category_by_material = grouped_by_material(category_log)
    basis_by_material = grouped_by_material(legal_basis)
    evidence_by_material = grouped_by_material(evidence)

    for _, item in out.iterrows():
        material_id = clean(item.get("material_id"))
        cross_rows = detail_by_material.get(material_id, pd.DataFrame())
        category_rows = category_by_material.get(material_id, pd.DataFrame())
        basis_rows = basis_by_material.get(material_id, pd.DataFrame())
        evidence_rows = evidence_by_material.get(material_id, pd.DataFrame())
        positive = _is_positive(item)
        candidate = _is_candidate(item)
        human_hazard_final = clean(item.get("final_decision"))
        human_hazard_review = human_hazard_final in HUMAN_HAZARD_REVIEW_DECISIONS

        results = {
            "S1": cas_gate(item),
            "S2": coverage_gate(material_id, category_rows),
            "S3": structure_gate(item),
            "S4": condition_gate(item, cross_rows),
            "S5": legal_basis_gate(material_id, basis_rows, positive),
        }
        auto_gate = automation_basis_gate(evidence_rows, cross_rows)

        blocking = [code for code, res in results.items() if res["stage_status"] != PASS]

        if (
            positive
            and results["S5"]["stage_status"] == PASS
            and law_gate["stage_status"] == PASS
            and auto_gate["stage_status"] == PASS
        ):
            gate_status = GATE_CONFIRMED
            decision = "규제 해당(자동 확정)"
            hold_reason = ""
        elif positive:
            gate_status = GATE_HOLD
            decision = "보류(규제 해당 후보·전문가 확인 필요)"
            reasons = []
            if results["S5"]["stage_status"] != PASS:
                reasons.append(results["S5"]["detail"])
            if law_gate["stage_status"] != PASS:
                reasons.append(law_gate["detail"])
            if auto_gate["stage_status"] != PASS:
                reasons.append(auto_gate["detail"])
            hold_reason = " / ".join(dict.fromkeys(reasons)) or "자동확정 요건을 모두 충족하지 못했습니다."
        elif human_hazard_review:
            # company_engine already determined the human-hazard side cannot
            # be confirmed one way or the other (an anchor/contextual CAS
            # reference, a missing concentration, or an invalid CAS) -- carry
            # its own specific reason through rather than the generic
            # cross-regime "unverified API candidate" message below, and
            # never let this fall through to a confirmed "비해당".
            gate_status = GATE_HOLD
            decision = {
                "물질정보 확인 실패": "보류(물질식별 실패)",
                "정보 부족": "보류(정보 부족)",
            }.get(human_hazard_final, "보류(인체유해성 판정 검토 필요)")
            hold_reason = (
                clean(item.get("review_reason"))
                or clean(item.get("decision_reason"))
                or "인체유해성 판정에 추가 확인이 필요합니다."
            )
        elif candidate:
            gate_status = GATE_HOLD
            decision = "보류(공식 근거 검증 전 후보)"
            hold_reason = "검증 전 API 후보 규칙에서만 일치했습니다. 승인 절차 완료 후 재판정이 필요합니다."
        else:
            gaps = [results[code]["detail"] for code in blocking]
            if law_gate["stage_status"] != PASS:
                gaps.append(law_gate["detail"])
            if results["S1"]["stage_status"] == FAIL:
                gate_status = GATE_HOLD
                decision = "보류(물질식별 실패)"
                hold_reason = " / ".join(gaps)
            elif gaps:
                gate_status = GATE_HOLD
                decision = "보류(비해당 단정 불가)"
                hold_reason = " / ".join(gaps)
            else:
                gate_status = GATE_CONFIRMED
                decision = "확인 범위 내 비해당"
                hold_reason = ""

        statuses.append(gate_status)
        decisions.append(decision)
        hold_reasons.append(hold_reason)
        basis_summary.append(results["S5"]["detail"])
        coverage_summary.append(results["S2"]["detail"])

        ordered = [
            ("S1", results["S1"]),
            ("S2", results["S2"]),
            ("S3", results["S3"]),
            ("S4", results["S4"]),
            ("S5", results["S5"]),
        ]
        for code, label in STAGES:
            if code == "S6":
                result = {
                    "stage_status": PASS if gate_status == GATE_CONFIRMED else HOLD,
                    "code": "AUTO_CONFIRMED" if gate_status == GATE_CONFIRMED else "HELD_FOR_REVIEW",
                    "detail": hold_reason or decision,
                }
            else:
                result = dict(ordered)[code]
            gate_rows.append(
                {
                    "material_id": material_id,
                    "chemical_name": clean(item.get("input_chemical_name")),
                    "cas": clean(item.get("input_cas")),
                    "stage": code,
                    "stage_label": label,
                    "stage_status": result["stage_status"],
                    "stage_code": result["code"],
                    "stage_detail": result["detail"],
                    "gate_status": gate_status,
                    "gate_decision": decision,
                    "law_version_status": law_gate["code"],
                }
            )

    out["gate_status"] = statuses
    out["gate_decision"] = decisions
    out["gate_hold_reason"] = hold_reasons
    out["legal_basis_summary"] = basis_summary
    out["coverage_summary"] = coverage_summary
    out["law_version_status"] = law_gate["detail"]
    return out, pd.DataFrame(gate_rows)


GATE_LOG_KO = {
    "material_id": "인벤토리 ID",
    "chemical_name": "물질명",
    "cas": "CAS",
    "stage": "단계",
    "stage_label": "단계명",
    "stage_status": "단계 결과",
    "stage_code": "단계 코드",
    "stage_detail": "단계 근거",
    "gate_status": "게이트 결과",
    "gate_decision": "게이트 판정",
    "law_version_status": "법규 버전 상태",
}

CATEGORY_LOG_KO = {
    "material_id": "인벤토리 ID",
    "chemical_name": "물질명",
    "cas": "CAS",
    "regime": "규제 코드",
    "regime_label": "규제 카테고리",
    "searched": "검색 수행",
    "rule_source": "검색 대상 규칙원천",
    "rule_rows": "검색 규칙 행 수",
    "hit_count": "일치 건수",
    "search_result": "검색 결과",
    "coverage_status": "커버리지 코드",
    "coverage_status_ko": "커버리지 상태",
}

LEGAL_BASIS_KO = {
    "material_id": "인벤토리 ID",
    "chemical_name": "물질명",
    "cas": "CAS",
    "regime": "규제 코드",
    "regime_label": "규제 카테고리",
    "designation_id": "지정번호",
    "regulatory_substance_name": "규제 물질명",
    "match_type": "매칭 방식",
    "scope_type": "규제범위 유형",
    "notice": "적용 고시",
    "effective_date": "시행일",
    "source_text": "규제 원문",
    "source_reference": "출처",
    "official_source_verified": "공식 출처 확인",
    "evidence_status": "근거 상태",
    "basis_status": "법률근거 검증 결과",
}
