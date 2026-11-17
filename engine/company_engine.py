from __future__ import annotations

import re
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill

import broad_salt_identity as salt
from dashboard_engine import calculate_framework, read_uploaded_table
from regulatory_api import notice_key
from scope_engine import classify_scope_text, match_extended_scope
from plain_report import (
    build_decision_policy,
    build_plain_evidence,
    build_plain_summary,
    build_reader_guide,
)
from trust_gate import (
    CATEGORY_LOG_KO,
    GATE_LOG_KO,
    LEGAL_BASIS_KO,
    STRUCTURE_AUTOMATABLE,
    STRUCTURE_STATUS_KO,
    structure_evidence_status,
)


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_MASTER = DATA_DIR / "regulatory_rules_master_APPROVED.csv"
DEFAULT_RULES = DATA_DIR / "broad_salt_rules.csv"
DEFAULT_CASES = DATA_DIR / "broad_salt_validation_cases.csv"
DEFAULT_CACHE = ROOT / "outputs" / "pubchem_structure_cache.csv"


INVENTORY_ALIASES = {
    "물질id": "material_id",
    "물질_id": "material_id",
    "인벤토리id": "material_id",
    "인벤토리_id": "material_id",
    "제품명": "product_name",
    "물질명": "chemical_name",
    "화학물질명": "chemical_name",
    "cas": "cas",
    "cas번호": "cas",
    "cas_no": "cas",
    "cas no": "cas",
    "cas number": "cas",
    "smiles": "smiles",
    "함량": "concentration_pct",
    "함량(%)": "concentration_pct",
    "농도": "concentration_pct",
    "농도(%)": "concentration_pct",
    "부서": "department",
    "용도": "use",
    "사용용도": "use_description",
    "용도설명": "use_description",
    "연간취급량": "annual_quantity_kg",
    "연간취급량(kg)": "annual_quantity_kg",
    "연간사용량(kg)": "annual_quantity_kg",
    "판정기준일": "as_of_date",
    "기준일": "as_of_date",
    "허가상태": "authorization_status",
    "허가·신고상태": "authorization_status",
    "취급행위": "operation_type",
}

CATEGORY_COLUMNS = {
    "acute": ("acute_threshold_raw", "인체급성"),
    "chronic": ("chronic_threshold_raw", "인체만성"),
    "eco": ("eco_threshold_raw", "생태유해성"),
}

STATUS_PRIORITY = {
    "해당": 4,
    "함량 확인 필요": 3,
    "기준 미만": 2,
    "규칙 없음": 1,
}


def _clean(value: Any) -> str:
    return salt.clean(value)


def _first_nonempty(*values: Any) -> str:
    for value in values:
        text = _clean(value)
        if text:
            return text
    return ""


def _normalize_columns(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.columns = [str(col).strip().replace("\ufeff", "") for col in out.columns]
    aliases = {str(key).strip().lower(): value for key, value in INVENTORY_ALIASES.items()}
    return out.rename(columns={col: aliases.get(str(col).strip().lower(), col) for col in out.columns})


CSV_ENCODING_CANDIDATES = ["utf-8-sig", "utf-8", "cp949", "euc-kr"]


def _read_csv_any_encoding(source) -> pd.DataFrame:
    """Read a CSV trying several encodings in order."""
    last_error: Optional[Exception] = None
    for encoding in CSV_ENCODING_CANDIDATES:
        try:
            if hasattr(source, "seek"):
                source.seek(0)
            return pd.read_csv(source, encoding=encoding)
        except (UnicodeDecodeError, UnicodeError) as exc:
            last_error = exc
            continue
    raise ValueError(
        "CSV 파일의 인코딩을 인식하지 못했습니다(UTF-8/CP949/EUC-KR 모두 실패). "
        "엑셀에서 '다른 이름으로 저장' → 'CSV UTF-8(쉼표로 분리)'로 다시 저장한 뒤 "
        "업로드해 주십시오."
    ) from last_error


def read_inventory(uploaded, default_path: Optional[Path] = None) -> pd.DataFrame:
    if uploaded is None:
        if default_path is None:
            return pd.DataFrame()
        if default_path.suffix.lower() in {".xlsx", ".xls"}:
            frame = pd.read_excel(default_path)
        else:
            frame = _read_csv_any_encoding(default_path)
    else:
        name = str(getattr(uploaded, "name", "inventory.csv")).lower()
        frame = pd.read_excel(uploaded) if name.endswith((".xlsx", ".xls")) else _read_csv_any_encoding(uploaded)
    return _normalize_columns(frame)


def prepare_company_inventory(raw: pd.DataFrame, fetch_pubchem: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    out = _normalize_columns(raw)
    for col in [
        "material_id", "product_name", "chemical_name", "cas", "smiles", "concentration_pct",
        "department", "use", "use_description", "annual_quantity_kg", "as_of_date",
        "authorization_status", "operation_type",
    ]:
        if col not in out.columns:
            out[col] = ""
    out = out.reset_index(drop=True)
    missing_id = out["material_id"].map(_clean).eq("")
    generated = [f"MAT-{i:04d}" for i in range(1, len(out) + 1)]
    out.loc[missing_id, "material_id"] = pd.Series(generated, index=out.index)[missing_id]
    out["cas"] = out["cas"].map(salt.normalize_cas)
    out["concentration_pct"] = pd.to_numeric(out["concentration_pct"], errors="coerce")
    out["annual_quantity_kg"] = pd.to_numeric(out["annual_quantity_kg"], errors="coerce")
    use_blank = out["use_description"].map(_clean).eq("")
    out.loc[use_blank, "use_description"] = out.loc[use_blank, "use"].map(_clean)
    if fetch_pubchem:
        return salt.enrich_inventory_with_pubchem(out, cache_file=DEFAULT_CACHE, auto_fetch=True)
    out["input_smiles"] = out["smiles"].map(_clean)
    out["structure_smiles_source"] = np.where(out["input_smiles"].ne(""), "USER_SUPPLIED", "UNRESOLVED")
    out["structure_status"] = np.where(out["input_smiles"].ne(""), "USER_SMILES_PRESENT", "PUBCHEM_NOT_RUN")
    for col in [
        "pubchem_cid", "pubchem_title", "pubchem_isomeric_smiles", "pubchem_connectivity_smiles",
        "pubchem_inchikey", "pubchem_cas_verified", "pubchem_retrieval_date", "pubchem_record_url",
        "pubchem_error",
    ]:
        out[col] = ""
    return out, pd.DataFrame()


def latest_notice(master: pd.DataFrame) -> str:
    if master is None or master.empty or "notice" not in master.columns:
        return ""
    labels = [_clean(value) for value in master["notice"].dropna().unique() if _clean(value)]
    return sorted(labels, key=notice_key)[-1] if labels else ""


def load_approved_master(path: Path = DEFAULT_MASTER) -> tuple[pd.DataFrame, str]:
    master = pd.read_csv(path, low_memory=False)
    master.columns = [str(col).strip().replace("\ufeff", "") for col in master.columns]
    label = latest_notice(master)
    current = master[master["notice"].astype(str).eq(label)].copy()
    if current.empty:
        raise ValueError("승인 규제 마스터에서 최신 고시 행을 찾지 못했습니다.")
    current = _inherit_designation_fields(current)
    return current.reset_index(drop=True), label


def _first_meaningful(series: pd.Series) -> Any:
    for value in series:
        text = _clean(value)
        if text and text.lower() != "nan":
            return value
    return np.nan


def _inherit_designation_fields(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    fields = [
        "acute_threshold_raw", "chronic_threshold_raw", "eco_threshold_raw", "category",
        "main_rule_text", "authoritative_source", "source_file", "source_sheet",
    ]
    if "designation_id" not in out.columns:
        return out
    for col in fields:
        if col not in out.columns:
            out[col] = np.nan
        inherited = out.groupby("designation_id", dropna=False)[col].transform(_first_meaningful)
        blank = out[col].map(_clean).isin(["", "nan", "None"])
        out.loc[blank, col] = inherited[blank]
    return out


def _split_cas(value: Any) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    return list(dict.fromkeys(re.findall(r"\b\d{2,7}-\d{2}-\d\b", text)))


def _parse_threshold(value: Any) -> float:
    text = _clean(value)
    if not text or text in {"-", "–", "—", "nan"}:
        return np.nan
    match = re.search(r"(?<!\d)(\d+(?:\.\d+)?)(?!\d)", text.replace(",", ""))
    return float(match.group(1)) if match else np.nan


def _category_status(threshold: float, concentration: float) -> str:
    if not np.isfinite(threshold):
        return "규칙 없음"
    if not np.isfinite(concentration):
        return "함량 확인 필요"
    return "해당" if float(concentration) >= float(threshold) else "기준 미만"


def _build_master_cas_index(master: pd.DataFrame) -> dict[str, dict[str, list[dict]]]:
    index = {"direct": {}, "excluded": {}, "contextual": {}}
    for _, row in master.iterrows():
        item = row.to_dict()
        exceptions = _split_cas(row.get("exception_cas"))
        direct = _split_cas(row.get("direct_cas"))
        # anchor_cas is retained as an internal/source-schema field for compatibility.
        # It is a 기준물질 CAS, never an automatic inclusion match.
        reference_cas = _split_cas(row.get("anchor_cas"))
        all_in_row = _split_cas(row.get("all_cas_in_row"))
        for cas in exceptions:
            index["excluded"].setdefault(cas, []).append(item)
        for cas in direct:
            direct_item = dict(item)
            direct_item["match_type"] = "직접 CAS"
            index["direct"].setdefault(cas, []).append(direct_item)
        for cas in set([*reference_cas, *all_in_row]) - set(direct) - set(exceptions):
            context_item = dict(item)
            context_item["match_type"] = "기준물질·복합범위 CAS(보류)"
            index["contextual"].setdefault(cas, []).append(context_item)
    return index


def _master_matches(cas: str, index: dict[str, dict[str, list[dict]]]) -> tuple[list[dict], list[dict], list[dict]]:
    if not cas:
        return [], [], []
    return (
        list(index["direct"].get(cas, [])),
        list(index["excluded"].get(cas, [])),
        list(index["contextual"].get(cas, [])),
    )


def load_promoted_salt_rules(master: pd.DataFrame) -> pd.DataFrame:
    framework = calculate_framework(read_uploaded_table(None, DEFAULT_RULES), read_uploaded_table(None, DEFAULT_CASES))
    promoted = framework["promotion"]
    promoted = promoted[promoted["promotion_eligible"].astype(bool)][["rule_id", "designation_id", "promotion_status"]]
    rules = framework["rules"].merge(promoted, on=["rule_id", "designation_id"], how="inner")
    meta_rows = []
    for designation, group in master.groupby("designation_id", dropna=False):
        meta_rows.append({
            "designation_id": _clean(designation),
            "regulatory_substance_name": _clean(_first_meaningful(group["substance_name_ko"])),
            "acute_threshold_raw_master": _first_meaningful(group["acute_threshold_raw"]),
            "chronic_threshold_raw_master": _first_meaningful(group["chronic_threshold_raw"]),
            "eco_threshold_raw_master": _first_meaningful(group["eco_threshold_raw"]),
            "category_master": _clean(_first_meaningful(group["category"])),
            "source_text_master": _clean(_first_meaningful(group["source_text"])),
            "authoritative_source_master": _clean(_first_meaningful(group["authoritative_source"])),
        })
    return rules.merge(pd.DataFrame(meta_rows), on="designation_id", how="left")


def _salt_matches(inv: pd.Series, rules: pd.DataFrame) -> list[dict]:
    matches: list[dict] = []
    smiles = _clean(inv.get("smiles"))
    if not smiles:
        return matches
    inv_key = salt.parent_key(smiles)
    inv_conn = salt.parent_connectivity_key(smiles)
    if not inv_key or not inv_conn:
        return matches
    for _, rule in rules.iterrows():
        scope = salt.normalize_isomer_scope(rule.get("isomer_scope"))
        ref_key = _clean(rule.get("reference_parent_key")) or salt.parent_key(rule.get("reference_smiles"))
        ref_conn = _clean(rule.get("reference_parent_connectivity_key")) or salt.parent_connectivity_key(rule.get("reference_smiles"))
        if not ref_key or not ref_conn:
            membership, comparison = None, "REVIEW_INVALID_REFERENCE_SMILES"
        elif inv_key == ref_key:
            membership, comparison = True, "PARENT_MATCH_EXACT_STEREO"
        elif inv_conn != ref_conn:
            membership, comparison = False, "PARENT_NO_MATCH_CONNECTIVITY"
        elif scope == "ALL_STEREOISOMERS":
            membership, comparison = True, "PARENT_MATCH_ALL_STEREOISOMERS_RULE"
        elif scope == "EXACT_STEREO_ONLY":
            membership, comparison = False, "PARENT_NO_MATCH_EXACT_STEREO_RULE"
        else:
            membership, comparison = None, "REVIEW_STEREOISOMER_SCOPE_UNSPECIFIED"
        if membership is True:
            matches.append({
                **rule.to_dict(),
                "match_type": "검증된 염류 구조",
                "parent_comparison_status": comparison,
                "inventory_parent_key": inv_key,
                "reference_parent_key": ref_key,
                "inventory_parent_connectivity_key": inv_conn,
                "reference_parent_connectivity_key": ref_conn,
            })
    return matches


def _row_threshold(match: dict, short_name: str) -> tuple[Any, float]:
    raw_col = CATEGORY_COLUMNS[short_name][0]
    raw = match.get(f"{raw_col}_master", match.get(raw_col))
    return raw, _parse_threshold(raw)


def _evidence_row(inv: pd.Series, match: dict, concentration: float, notice: str, evidence_status: str = "CONFIRMED_RULE_MATCH") -> dict:
    row = {
        "material_id": _clean(inv.get("material_id")),
        "evidence_status": evidence_status,
        "product_name": _clean(inv.get("product_name")),
        "chemical_name": _clean(inv.get("chemical_name")),
        "cas": _clean(inv.get("cas")),
        "concentration_pct": concentration,
        "match_type": _clean(match.get("match_type")),
        "scope_type": _first_nonempty(
            match.get("scope_type"),
            classify_scope_text(
                _first_nonempty(match.get("source_text_master"), match.get("source_text")),
                has_direct_cas=_clean(match.get("match_type")) == "직접 CAS",
            ),
        ),
        "designation_id": _clean(match.get("designation_id")),
        "regulatory_substance_name": _first_nonempty(match.get("regulatory_substance_name"), match.get("substance_name_ko")),
        "reference_parent_name": _clean(match.get("reference_parent_name")),
        "reference_parent_cas": _clean(match.get("reference_parent_cas")),
        "category": _first_nonempty(match.get("category_master"), match.get("category")),
        "notice": notice,
        "source_text": _first_nonempty(match.get("source_text_master"), match.get("source_text")),
        "regulatory_source": _clean(match.get("regulatory_source")),
        "authoritative_source": _first_nonempty(match.get("authoritative_source_master"), match.get("authoritative_source")),
    }
    for short, (_, label) in CATEGORY_COLUMNS.items():
        raw, threshold = _row_threshold(match, short)
        row[f"{short}_threshold_raw"] = _clean(raw)
        row[f"{short}_threshold_pct"] = threshold
        row[f"{short}_decision"] = _category_status(threshold, concentration)
        row[f"{short}_reason"] = (
            f"실제 함량 {concentration:g}% ≥ 기준 {threshold:g}%" if np.isfinite(concentration) and np.isfinite(threshold) and concentration >= threshold
            else f"실제 함량 {concentration:g}% < 기준 {threshold:g}%" if np.isfinite(concentration) and np.isfinite(threshold)
            else f"{label} 기준 {threshold:g}%, 입력 함량 없음" if np.isfinite(threshold)
            else f"이 규칙에는 {label} 기준이 없음"
        )
    return row


def _aggregate_status(values: list[str]) -> str:
    if not values:
        return "규칙 없음"
    return max(values, key=lambda value: STATUS_PRIORITY.get(value, 0))


def screen_company_inventory(
    inventory: pd.DataFrame,
    master: pd.DataFrame,
    salt_rules: pd.DataFrame,
    notice: str,
    extended_rules: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    summary_rows, evidence_rows, review_rows = [], [], []
    screened_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    master_cas_index = _build_master_cas_index(master)

    for _, inv in inventory.iterrows():
        cas = salt.normalize_cas(inv.get("cas"))
        concentration = pd.to_numeric(pd.Series([inv.get("concentration_pct")]), errors="coerce").iloc[0]
        direct, excluded, master_review_candidates = _master_matches(cas, master_cas_index)
        structural = [] if direct else _salt_matches(inv, salt_rules)
        extended = [] if direct or structural else match_extended_scope(
            inv.get("smiles"), cas, extended_rules if extended_rules is not None else pd.DataFrame()
        )

        # Core trust policy:
        # - Direct CAS never needs structure inference.
        # - Salt-family matches require a structure traceable to the exact CAS.
        # - Generic structural ranges may be collected as review evidence but never gain
        #   automatic-confirmation rights in trust_gate.py.
        structure_status = structure_evidence_status(inv)
        structure_trusted = structure_status in STRUCTURE_AUTOMATABLE
        scope_matches = [*structural, *extended]
        if structure_trusted:
            confirmed_scope, held_structural = scope_matches, []
        else:
            confirmed_scope, held_structural = [], scope_matches

        combined, seen = [], set()
        for match in [*direct, *confirmed_scope]:
            key = (_clean(match.get("designation_id")), _clean(match.get("match_type")))
            if key not in seen:
                combined.append(match)
                seen.add(key)
        evidence = [_evidence_row(inv, match, concentration, notice) for match in combined]
        evidence_rows.extend(evidence)

        held_evidence, held_seen = [], set()
        for match in held_structural:
            key = (_clean(match.get("designation_id")), _clean(match.get("match_type")))
            if key in held_seen:
                continue
            held_seen.add(key)
            held_evidence.append(_evidence_row(inv, match, concentration, notice, "STRUCTURE_UNVERIFIED_HOLD"))
        evidence_rows.extend(held_evidence)

        category_results = {
            short: _aggregate_status([row[f"{short}_decision"] for row in evidence])
            for short in CATEGORY_COLUMNS
        }
        if any(value == "해당" for value in category_results.values()):
            final = "규제 해당"
            reason = "규제 물질과 일치하며 하나 이상의 유해성 기준농도 이상입니다."
        elif evidence and any(value == "함량 확인 필요" for value in category_results.values()):
            final = "정보 부족"
            reason = "규제 물질과 일치하지만 함량 정보가 없어 기준 적용이 필요합니다."
        elif evidence:
            final = "기준 미만"
            reason = "규제 물질 범위에는 포함되지만 입력 함량이 연결된 기준보다 낮습니다."
        else:
            cas_ok = bool(cas and salt.valid_cas_checksum(cas) and cas.replace("-", "").strip("0"))
            if not cas_ok:
                final = "물질정보 확인 실패"
                reason = "유효한 CAS 번호가 없어 공식 규제목록과 정확히 비교할 수 없습니다."
            elif held_evidence:
                final = "전문가 검토 필요"
                held_ids = "; ".join(dict.fromkeys(row["designation_id"] for row in held_evidence if row["designation_id"]))
                final_label = STRUCTURE_STATUS_KO.get(structure_status, structure_status)
                reason = (
                    f"포괄 규제범위 후보가 있으나 사용한 구조의 근거가 {final_label} 상태입니다. "
                    f"염류는 PubChem 정확 CAS로 구조가 검증되어야만 자동 확정할 수 있습니다. 관련 지정번호: {held_ids}"
                )
            elif master_review_candidates:
                final = "전문가 검토 필요"
                ids = "; ".join(dict.fromkeys(_clean(row.get("designation_id")) for row in master_review_candidates if _clean(row.get("designation_id"))))
                reason = (
                    "CAS가 기준물질 또는 포괄 규제범위 원문에 존재하지만 직접 CAS가 아니므로 자동 확정하지 않습니다. "
                    f"관련 지정번호: {ids}"
                )
            elif not structure_trusted:
                final = "전문가 검토 필요"
                reason = (
                    "직접 CAS는 일치하지 않았고, 구조 근거가 "
                    f"{STRUCTURE_STATUS_KO.get(structure_status, structure_status)} 상태여서 염류 해당 여부를 확정할 수 없습니다."
                )
            else:
                final = "확인 범위 내 비해당"
                reason = "직접 CAS와 검증된 염류 규칙을 대조했으며 자동확정 가능한 일치 항목이 없습니다."
            if excluded:
                reason += " 규제 규칙의 명시적 예외 CAS로 확인된 행이 있습니다."

        matched_ids = "; ".join(dict.fromkeys(row["designation_id"] for row in evidence if row["designation_id"]))
        matched_names = "; ".join(dict.fromkeys(row["regulatory_substance_name"] for row in evidence if row["regulatory_substance_name"]))
        match_types = "; ".join(dict.fromkeys(row["match_type"] for row in evidence if row["match_type"]))
        scope_types = "; ".join(dict.fromkeys(row["scope_type"] for row in evidence if row["scope_type"]))
        if not scope_types and master_review_candidates:
            scope_types = "; ".join(dict.fromkeys(classify_scope_text(row.get("source_text") or row.get("main_rule_text"), False) for row in master_review_candidates))
        review_regulatory_text = "\n---\n".join(dict.fromkeys(
            _clean(row.get("source_text") or row.get("main_rule_text"))
            for row in master_review_candidates if _clean(row.get("source_text") or row.get("main_rule_text"))
        ))
        review_reason = ""
        if final in {"정보 부족", "전문가 검토 필요", "물질정보 확인 실패"}:
            review_reason = reason
            review_rows.append({
                "material_id": _clean(inv.get("material_id")), "product_name": _clean(inv.get("product_name")),
                "chemical_name": _clean(inv.get("chemical_name")), "cas": cas, "review_status": final,
                "review_reason": reason,
                "recommended_action": "제품 내 함량을 입력" if final == "정보 부족" else "CAS·SDS·구조정보 및 법적 범위를 확인",
            })

        summary_rows.append({
            "material_id": _clean(inv.get("material_id")), "product_name": _clean(inv.get("product_name")),
            "department": _clean(inv.get("department")), "use": _clean(inv.get("use")),
            "use_description": _clean(inv.get("use_description")), "annual_quantity_kg": inv.get("annual_quantity_kg"),
            "as_of_date": _clean(inv.get("as_of_date")), "authorization_status": _clean(inv.get("authorization_status")),
            "operation_type": _clean(inv.get("operation_type")), "input_chemical_name": _clean(inv.get("chemical_name")),
            "input_cas": cas, "concentration_pct": concentration,
            "resolved_name": _clean(inv.get("pubchem_title")) or _clean(inv.get("chemical_name")),
            "pubchem_cid": _clean(inv.get("pubchem_cid")), "smiles": _clean(inv.get("smiles")),
            "identity_status": _clean(inv.get("structure_status")), "identity_source": _clean(inv.get("structure_smiles_source")),
            "pubchem_connectivity_smiles": _clean(inv.get("pubchem_connectivity_smiles")),
            "pubchem_isomeric_smiles": _clean(inv.get("pubchem_isomeric_smiles")),
            "structure_evidence_status": structure_status,
            "structure_evidence_status_ko": STRUCTURE_STATUS_KO.get(structure_status, structure_status),
            "hh_direct_hits": len(direct), "hh_structure_hits": len(confirmed_scope),
            "hh_structure_unverified_hits": len(held_evidence), "hh_contextual_hits": len(master_review_candidates),
            "hh_excluded_hits": len(excluded), "pubchem_record_url": _clean(inv.get("pubchem_record_url")),
            "match_type": match_types, "scope_type": scope_types, "designation_id": matched_ids,
            "matched_regulatory_name": matched_names, "final_decision": final,
            "acute_decision": category_results["acute"], "chronic_decision": category_results["chronic"],
            "eco_decision": category_results["eco"], "decision_reason": reason, "review_reason": review_reason,
            "review_regulatory_text": review_regulatory_text, "applied_notice": notice, "screened_at_utc": screened_at,
        })

    return pd.DataFrame(summary_rows), pd.DataFrame(evidence_rows), pd.DataFrame(review_rows)


SUMMARY_KO = {
    "material_id": "인벤토리 ID", "product_name": "제품명", "department": "부서", "use": "용도",
    "use_description": "용도 설명", "annual_quantity_kg": "연간취급량(kg)", "as_of_date": "판정기준일",
    "authorization_status": "허가·신고상태", "operation_type": "취급행위", "input_chemical_name": "입력 물질명",
    "input_cas": "입력 CAS", "concentration_pct": "함량(%)", "resolved_name": "확인 물질명",
    "pubchem_cid": "PubChem CID", "smiles": "SMILES", "identity_status": "물질식별 상태",
    "identity_source": "구조정보 출처", "structure_evidence_status_ko": "구조근거 검증",
    "hh_direct_hits": "인체유해성 직접CAS 일치", "hh_structure_hits": "인체유해성 검증 구조 일치",
    "hh_structure_unverified_hits": "구조근거 미검증 후보", "hh_contextual_hits": "기준물질·포괄범위 원문 검색",
    "hh_excluded_hits": "예외 CAS 검색", "gate_status": "신뢰게이트 결과", "gate_decision": "게이트 판정",
    "rule_based_decision": "승인 규칙 기반 판정",
    "gate_hold_reason": "보류 사유", "legal_basis_summary": "법률근거 검증", "coverage_summary": "카테고리 커버리지",
    "law_version_status": "법규 버전 상태", "pubchem_record_url": "PubChem 근거", "match_type": "규제 매칭 방식",
    "scope_type": "규제범위 유형", "designation_id": "지정번호", "matched_regulatory_name": "규제 물질명",
    "final_decision": "최종 판정", "acute_decision": "인체급성 판정", "chronic_decision": "인체만성 판정",
    "eco_decision": "생태유해성 판정", "decision_reason": "판정 사유", "review_reason": "검토 필요 사유",
    "review_regulatory_text": "검토대상 규제 원문", "applied_notice": "적용 고시", "screened_at_utc": "판정시각(UTC)",
    "HUMAN_HAZARD_decision": "인체등유해성 판정", "ACCIDENT_PREPAREDNESS_decision": "사고대비물질 판정",
    "RESTRICTED_decision": "제한물질 판정", "PROHIBITED_decision": "금지물질 판정", "overall_decision": "통합 판정",
    "applicable_regulatory_categories": "해당 규제 카테고리", "review_regulatory_categories": "검토 필요 카테고리",
    "llm_status": "로컬 AI 상태", "llm_assessment": "로컬 AI 평가", "llm_scope_type": "AI 해석 범위유형",
    "llm_explanation_ko": "AI 검토 설명", "llm_confidence": "AI 신뢰도(참고)", "llm_evidence_quotes": "AI 원문 근거",
    "llm_missing_information": "AI 확인 누락정보", "llm_gate_issues": "AI 출력 검증조치", "llm_model": "로컬 AI 모델",
    "llm_candidate_designation_ids": "AI 후보 지정번호", "llm_model_selected_designation_ids": "AI 모델 선택 지정번호(감사용)",
    "llm_rule_relation": "규칙 관계 분류", "no_cas_scope_candidate_context": "포괄 규제범위 후보 메타데이터",
    "no_cas_scope_designation_ids": "포괄 규제범위 후보 지정번호", "no_cas_scope_regulatory_names": "포괄 규제범위 후보 규제명",
}

EVIDENCE_KO = {
    "material_id": "인벤토리 ID", "evidence_status": "근거 상태", "product_name": "제품명",
    "chemical_name": "입력 물질명", "cas": "입력 CAS", "concentration_pct": "함량(%)", "match_type": "매칭 방식",
    "scope_type": "규제범위 유형", "designation_id": "지정번호", "regulatory_substance_name": "규제 물질명",
    "reference_parent_name": "기준물질명", "reference_parent_cas": "기준물질 CAS", "category": "고시 분류",
    "acute_threshold_raw": "인체급성 기준 원문값", "acute_threshold_pct": "인체급성 기준(%)", "acute_decision": "인체급성 판정",
    "acute_reason": "인체급성 판정근거", "chronic_threshold_raw": "인체만성 기준 원문값", "chronic_threshold_pct": "인체만성 기준(%)",
    "chronic_decision": "인체만성 판정", "chronic_reason": "인체만성 판정근거", "eco_threshold_raw": "생태 기준 원문값",
    "eco_threshold_pct": "생태 기준(%)", "eco_decision": "생태유해성 판정", "eco_reason": "생태유해성 판정근거",
    "notice": "적용 고시", "source_text": "규제 원문", "regulatory_source": "규제 출처 URL", "authoritative_source": "공식 출처 확인",
}

REVIEW_KO = {
    "material_id": "인벤토리 ID", "product_name": "제품명", "chemical_name": "물질명", "cas": "CAS",
    "review_status": "검토 상태", "review_reason": "검토 사유", "recommended_action": "권장 조치",
}


def korean_summary(summary: pd.DataFrame) -> pd.DataFrame:
    return summary.rename(columns=SUMMARY_KO)


def build_result_workbook(
    summary: pd.DataFrame, evidence: pd.DataFrame, enriched_inventory: pd.DataFrame,
    pubchem_audit: pd.DataFrame, review: pd.DataFrame, api_status: dict, master_notice: str,
    master_rows: int, cross_summary: Optional[pd.DataFrame] = None, cross_detail: Optional[pd.DataFrame] = None,
    cross_api_status: Optional[pd.DataFrame] = None, llm_audit: Optional[pd.DataFrame] = None,
    llm_status: Optional[dict] = None, gate_log: Optional[pd.DataFrame] = None,
    category_log: Optional[pd.DataFrame] = None, legal_basis: Optional[pd.DataFrame] = None,
    draft_hints: Optional[pd.DataFrame] = None, no_cas_candidates: Optional[pd.DataFrame] = None,
    detailed: bool = False,
) -> bytes:
    llm_status = llm_status or {}
    llm_frame = llm_audit if llm_audit is not None else pd.DataFrame()
    llm_completed = int(len(llm_frame))
    llm_errors = int(llm_frame.get("llm_status", pd.Series(dtype=str)).astype(str).isin(["LLM_ERROR", "MISSING_CASE_OUTPUT"]).sum())
    system = pd.DataFrame([
        {"항목": "승인 규제 마스터 고시", "값": master_notice},
        {"항목": "승인 규칙 행 수", "값": master_rows},
        {"항목": "법규 API 상태", "값": api_status.get("status_ko", "확인하지 않음")},
        {"항목": "법규 동기화 상태", "값": api_status.get("sync_status_ko", "확인하지 않음")},
        {"항목": "버전 비교 기준일(KST)", "값": api_status.get("reference_date", "")},
        {"항목": "법규 버전 해석", "값": (
            "후속 고시가 시행 전이며 현행 승인본의 법적 유효성은 별도로 확인해야 합니다."
            if api_status.get("sync_status") == "FUTURE_NOTICE"
            else "법규 버전 결과는 승인 규칙 기반 판정과 별도로 검토해야 합니다."
        )},
        {"항목": "공식 최신 고시", "값": api_status.get("official_notice", "")},
        {"항목": "공식 시행일", "값": api_status.get("effective_date", "")},
        {"항목": "API 확인시각(UTC)", "값": api_status.get("checked_at_utc", "")},
        {"항목": "핵심 자동판정 범위", "값": "직접 CAS 일치 및 승인·검증된 '~와 그 염류' 구조 일치"},
        {"항목": "자동 확정 조건", "값": "직접 CAS 또는 검증된 염류 구조이며 최신 법규·함량조건·지정번호·규제원문·공식출처가 모두 확인된 경우에만 자동 확정합니다. 승인 규칙상 해당은 최종 법적 확정과 구분합니다."},
        {"항목": "포괄 범위 안전원칙", "값": "혼합물·반응생성물·화합물군·UVCB·일반 구조범위·기준물질 CAS 연결·이름 유사성은 분류와 검토 보조에만 사용하고 자동 확정하지 않습니다."},
        {"항목": "염류 구조 요건", "값": "염류 자동 확정은 PubChem 정확 CAS 동의어 구조 또는 그 구조와 연결성이 일치한 사용자 SMILES에만 허용합니다."},
        {"항목": "주의", "값": "확인 범위 내 비해당은 모든 국내 화학규제의 절대적 비규제를 의미하지 않습니다."},
        {"항목": "로컬 AI 상태", "값": llm_status.get("status_ko", "사용하지 않음")},
        {"항목": "로컬 AI 모델", "값": llm_status.get("model", "")},
        {"항목": "로컬 AI 검토기록 수", "값": llm_completed},
        {"항목": "로컬 AI 오류·응답누락 수", "값": llm_errors},
        {"항목": "로컬 AI 역할", "값": "포괄 규제범위 후보의 분류·확인사항 정리 보조; 규제 해당·비해당 자동확정 권한 없음"},
    ])
    identity_cols = [col for col in [
        "material_id", "product_name", "chemical_name", "cas", "concentration_pct", "input_smiles", "smiles",
        "structure_smiles_source", "structure_status", "pubchem_cid", "pubchem_title", "pubchem_isomeric_smiles",
        "pubchem_connectivity_smiles", "pubchem_inchikey", "pubchem_cas_verified", "pubchem_retrieval_date",
        "pubchem_record_url", "pubchem_error",
    ] if col in enriched_inventory.columns]
    sheets = {
        "판정결과": build_plain_summary(summary, evidence, cross_detail, master_notice),
        "결과읽는법": build_reader_guide(), "자동판정원칙": build_decision_policy(),
        "판정근거": build_plain_evidence(evidence), "검토필요": review.rename(columns=REVIEW_KO), "법규버전": system,
    }
    if llm_audit is not None:
        if llm_frame.empty:
            llm_frame = pd.DataFrame(columns=[
                "case_id", "material_id", "llm_status", "llm_assessment", "llm_scope_type",
                "llm_candidate_designation_ids", "llm_model_selected_designation_ids", "llm_rule_relation",
                "llm_explanation_ko", "llm_confidence", "llm_evidence_quotes", "llm_missing_information",
                "llm_gate_issues", "llm_model",
            ])
        sheets["로컬AI검토기록"] = llm_frame.copy()
    no_cas_frame = no_cas_candidates if no_cas_candidates is not None else pd.DataFrame()
    if not no_cas_frame.empty:
        display_scope = no_cas_frame.copy()
        if "candidate_match_type" in display_scope.columns:
            display_scope["candidate_match_type"] = display_scope["candidate_match_type"].replace({
                "EXPLICIT_EXCEPTION_CAS": "명시적 제외 CAS",
                "COMPONENT_CAS_CANDIDATE": "구성성분 CAS 기반 후보",
                "EXACT_SCOPE_CAS_CANDIDATE": "기준물질·원문 CAS 기반 후보",
                "NAME_SCOPE_CANDIDATE": "물질명 기반 후보",
                "SCOPE_CANDIDATE": "포괄범위 후보",
            })
        if "scope_type" in display_scope.columns:
            display_scope["scope_type"] = display_scope["scope_type"].replace({
                "REACTION_PRODUCT": "반응생성물", "MIXTURE": "혼합물", "SALT_FAMILY": "염류 범위",
                "NAME_OR_UVCB": "총칭·UVCB",
            })
        sheets["포괄규제범위후보"] = display_scope.rename(columns={
            "material_id": "인벤토리 ID", "chemical_name": "물질명", "cas": "CAS번호",
            "designation_id": "후보 지정번호", "notice": "적용 고시", "regulatory_name": "후보 규제명",
            "scope_type": "범위유형", "regulatory_categories": "후보 규제 카테고리",
            "candidate_match_type": "후보 연결방식", "candidate_reason": "후보 사유",
            "source_text": "규제 원문", "candidate_status": "검토 상태",
        })
    draft_hints_frame = draft_hints if draft_hints is not None else pd.DataFrame()
    if not draft_hints_frame.empty:
        sheets["구조초안힌트(미검증)"] = draft_hints_frame.rename(columns={
            "cas": "CAS번호", "chemical_name": "물질명", "designation_id": "지정번호", "regime": "규제 카테고리",
            "explanation_ko": "AI 설명(미검증)", "confidence": "확신도", "source_text": "규제 원문",
        })
    if detailed:
        sheets.update({
            "상세판정요약": summary.rename(columns=SUMMARY_KO),
            "신뢰게이트": (gate_log if gate_log is not None else pd.DataFrame()).rename(columns=GATE_LOG_KO),
            "카테고리검색기록": (category_log if category_log is not None else pd.DataFrame()).rename(columns=CATEGORY_LOG_KO),
            "법률근거": (legal_basis if legal_basis is not None else pd.DataFrame()).rename(columns=LEGAL_BASIS_KO),
            "규제근거": evidence.rename(columns=EVIDENCE_KO), "물질식별": enriched_inventory[identity_cols].copy(),
            "PubChem조회기록": pubchem_audit.copy(),
        })
        if cross_summary is not None:
            sheets["규제체계별판정"] = cross_summary.copy()
        if cross_detail is not None:
            sheets["타법규근거"] = cross_detail.copy()
        if cross_api_status is not None:
            sheets["법규API상태"] = cross_api_status.copy()
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        for sheet_name, frame in sheets.items():
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
            ws = writer.book[sheet_name]
            ws.freeze_panes = "A2"
            ws.auto_filter.ref = ws.dimensions
            for cell in ws[1]:
                cell.font = Font(color="FFFFFF", bold=True)
                cell.fill = PatternFill("solid", fgColor="1E5B77")
                cell.alignment = Alignment(horizontal="center", vertical="center")
            for column in ws.columns:
                letter = column[0].column_letter
                max_len = min(60, max([len(str(cell.value or "")) for cell in column] + [8]) + 2)
                ws.column_dimensions[letter].width = max_len
                for cell in column[1:]:
                    cell.alignment = Alignment(vertical="top", wrap_text=True)
            if sheet_name in {"판정결과", "상세판정요약"}:
                fills = {
                    "규제 해당": "F4CCCC", "규제 해당(자동 확정)": "F4CCCC",
                    "규제 해당 보류(법률근거 미확인)": "FCE5CD", "보류(규제 해당 후보·전문가 확인 필요)": "FCE5CD",
                    "보류(추가 확인 필요)": "FFF2CC", "보류": "FFF2CC", "보류(공식 근거 검증 전 후보)": "FCE5CD",
                    "보류(비해당 단정 불가)": "FFF2CC", "보류(물질식별 실패)": "D9D2E9", "정보 부족": "FFF2CC",
                    "전문가 검토 필요": "FCE5CD", "물질정보 확인 실패": "D9D2E9", "기준 미만": "D9EAD3",
                    "비해당": "D9EAF7", "확인 범위 내 비해당": "D9EAF7",
                }
                for label in ["판정결과", "게이트 판정", "최종 판정"]:
                    if label not in frame.columns:
                        continue
                    decision_col = list(frame.columns).index(label) + 1
                    for row_no in range(2, ws.max_row + 1):
                        cell = ws.cell(row_no, decision_col)
                        color = fills.get(_clean(cell.value))
                        if color:
                            cell.fill = PatternFill("solid", fgColor=color)
                            cell.font = Font(bold=True)
    return output.getvalue()
