from __future__ import annotations

from io import BytesIO
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

import broad_salt_identity as salt


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DEFAULT_RULES = DATA_DIR / "broad_salt_rules.csv"
DEFAULT_CASES = DATA_DIR / "broad_salt_validation_cases.csv"
DEFAULT_RESULTS = DATA_DIR / "FIRST_PAPER_RESULTS_SUMMARY.xlsx"
DEFAULT_CACHE = ROOT / "outputs" / "pubchem_structure_cache.csv"


RULE_EXTRA_COLUMNS = [
    "threshold_pct",
    "threshold_source",
    "threshold_version",
]

INVENTORY_ALIASES = {
    "물질id": "material_id",
    "물질_id": "material_id",
    "물질명": "chemical_name",
    "화학물질명": "chemical_name",
    "cas번호": "cas",
    "cas_no": "cas",
    "cas number": "cas",
    "함량": "concentration_pct",
    "함량(%)": "concentration_pct",
    "농도": "concentration_pct",
    "농도(%)": "concentration_pct",
}


def _clean_columns(df: pd.DataFrame) -> pd.DataFrame:
    x = df.copy()
    x.columns = [str(c).strip().replace("\ufeff", "") for c in x.columns]
    aliases = {str(k).strip().lower(): v for k, v in INVENTORY_ALIASES.items()}
    x = x.rename(columns={c: aliases.get(str(c).strip().lower(), c) for c in x.columns})
    return x


def read_uploaded_table(uploaded, default_path: Optional[Path] = None) -> pd.DataFrame:
    if uploaded is None:
        if default_path is None:
            return pd.DataFrame()
        return _clean_columns(_read_path(default_path))
    name = str(getattr(uploaded, "name", "uploaded.csv")).lower()
    if name.endswith((".xlsx", ".xls")):
        return _clean_columns(pd.read_excel(uploaded))
    return _clean_columns(pd.read_csv(uploaded))


def _read_path(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.read_csv(path)


def prepare_rules(raw: pd.DataFrame, fetch_missing_smiles: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    x = _clean_columns(raw)
    for c in salt.RULE_COLUMNS + RULE_EXTRA_COLUMNS:
        if c not in x.columns:
            x[c] = ""
    x = x[salt.RULE_COLUMNS + RULE_EXTRA_COLUMNS].copy()

    identity_input = pd.DataFrame({
        "chemical_name": x["reference_parent_name"],
        "cas": x["reference_parent_cas"],
        "smiles": x["reference_smiles"],
    })
    if fetch_missing_smiles:
        identity, audit = salt.enrich_inventory_with_pubchem(
            identity_input,
            cache_file=ROOT / "outputs" / "pubchem_rule_structure_cache.csv",
        )
        x["reference_smiles"] = identity["smiles"].values
        x["reference_smiles_source"] = identity["structure_smiles_source"].values
        x["reference_structure_status"] = identity["structure_status"].values
        x["reference_pubchem_cid"] = identity["pubchem_cid"].values
        x["reference_pubchem_record_url"] = identity["pubchem_record_url"].values
    else:
        audit = pd.DataFrame()
        x["reference_smiles_source"] = np.where(
            x["reference_smiles"].map(salt.clean).ne(""), "INPUT_FILE", "MISSING"
        )
        x["reference_structure_status"] = np.where(
            x["reference_smiles"].map(salt.clean).ne(""), "INPUT_SMILES", "MISSING_SMILES"
        )
        x["reference_pubchem_cid"] = ""
        x["reference_pubchem_record_url"] = ""

    x["source_verified_bool"] = x["source_verified"].map(salt.as_bool)
    x["reference_rule_frozen_bool"] = x["reference_rule_frozen"].map(salt.as_bool)
    x["automation_allowed_bool"] = x["automation_status"].map(salt.automation_allowed)
    x["salt_scope_supported_bool"] = x["salt_scope"].map(salt.salt_scope_supported)
    x["reference_parent_key"] = x["reference_smiles"].map(salt.parent_key)
    x["reference_parent_connectivity_key"] = x["reference_smiles"].map(
        salt.parent_connectivity_key
    )
    x["isomer_scope_normalized"] = x["isomer_scope"].map(salt.normalize_isomer_scope)
    x["machine_executable"] = x["reference_parent_key"].astype(str).str.len().gt(0)
    x["source_evidence_complete"] = (
        x["source_verified_bool"].astype(bool)
        & x["regulatory_source"].map(salt.clean).ne("")
        & x["chemical_identity_source"].map(salt.clean).ne("")
    )
    x["threshold_pct"] = pd.to_numeric(x["threshold_pct"], errors="coerce")
    return x, audit


def prepare_cases(raw: pd.DataFrame, fetch_missing_smiles: bool = False) -> tuple[pd.DataFrame, pd.DataFrame]:
    x = _clean_columns(raw)
    for c in salt.VALIDATION_COLUMNS:
        if c not in x.columns:
            x[c] = ""
    x = x[salt.VALIDATION_COLUMNS].copy()

    identity_input = pd.DataFrame({
        "chemical_name": x["chemical_name"],
        "cas": x["cas"],
        "smiles": x["smiles"],
    })
    if fetch_missing_smiles:
        identity, audit = salt.enrich_inventory_with_pubchem(
            identity_input,
            cache_file=ROOT / "outputs" / "pubchem_validation_structure_cache.csv",
        )
        x["smiles"] = identity["smiles"].values
        x["smiles_source"] = identity["structure_smiles_source"].values
        x["structure_status"] = identity["structure_status"].values
        x["pubchem_cid"] = identity["pubchem_cid"].values
        x["pubchem_record_url"] = identity["pubchem_record_url"].values
    else:
        audit = pd.DataFrame()
        x["smiles_source"] = np.where(x["smiles"].map(salt.clean).ne(""), "INPUT_FILE", "MISSING")
        x["structure_status"] = np.where(x["smiles"].map(salt.clean).ne(""), "INPUT_SMILES", "MISSING_SMILES")
        x["pubchem_cid"] = ""
        x["pubchem_record_url"] = ""

    x["reference_membership_bool"] = x["reference_membership"].map(salt.parse_membership)
    x["reference_source_present"] = x["reference_source"].map(salt.clean).ne("")
    x["reference_reviewed_bool"] = x["reference_review_status"].map(salt.reference_reviewed)
    x["reference_supported"] = (
        x["reference_membership_bool"].notna()
        & x["reference_source_present"]
        & x["reference_reviewed_bool"]
    )
    return x, audit


def calculate_framework(
    rules_raw: pd.DataFrame,
    cases_raw: pd.DataFrame,
    min_cases: int = 5,
    min_positive: int = 2,
    min_negative: int = 2,
    min_accuracy: float = 0.95,
    require_zero_false_safe: bool = True,
) -> dict[str, pd.DataFrame]:
    rules, _ = prepare_rules(rules_raw)
    cases, _ = prepare_cases(cases_raw)
    detail = salt.evaluate_validation_cases(cases, rules)
    by_rule = salt.summarize_validation_by_rule(detail, rules)
    promotion = salt.build_rule_promotion_with_policy(
        by_rule,
        min_cases=min_cases,
        min_positive=min_positive,
        min_negative=min_negative,
        min_accuracy=min_accuracy,
        require_zero_false_safe=require_zero_false_safe,
    )
    sensitivity_summary, sensitivity_detail = salt.build_promotion_sensitivity(by_rule)
    coverage = salt.build_rule_coverage_audit(rules, cases, by_rule)
    coverage_summary = salt.summarize_expansion_coverage(coverage)
    return {
        "rules": rules,
        "cases": cases,
        "detail": detail,
        "by_rule": by_rule,
        "promotion": promotion,
        "sensitivity_summary": sensitivity_summary,
        "sensitivity_detail": sensitivity_detail,
        "coverage": coverage,
        "coverage_summary": coverage_summary,
    }


def apply_threshold_table(rules: pd.DataFrame, thresholds: pd.DataFrame) -> pd.DataFrame:
    if thresholds is None or thresholds.empty:
        return rules.copy()
    t = _clean_columns(thresholds)
    if "threshold_pct" not in t.columns:
        return rules.copy()
    keys = [c for c in ["rule_id", "designation_id"] if c in t.columns and c in rules.columns]
    if not keys:
        return rules.copy()
    keep = keys + [c for c in ["threshold_pct", "threshold_source", "threshold_version"] if c in t.columns]
    t = t[keep].drop_duplicates(keys)
    out = rules.drop(columns=[c for c in ["threshold_pct", "threshold_source", "threshold_version"] if c in rules.columns])
    out = out.merge(t, on=keys, how="left")
    out["threshold_pct"] = pd.to_numeric(out.get("threshold_pct"), errors="coerce")
    return out


def prepare_inventory(raw: pd.DataFrame, fetch_pubchem: bool) -> tuple[pd.DataFrame, pd.DataFrame]:
    x = _clean_columns(raw)
    for c in ["material_id", "chemical_name", "cas", "smiles", "concentration_pct"]:
        if c not in x.columns:
            x[c] = ""
    if x["material_id"].map(salt.clean).eq("").all():
        x["material_id"] = [f"MAT-{i:04d}" for i in range(1, len(x) + 1)]
    x["concentration_pct"] = pd.to_numeric(x["concentration_pct"], errors="coerce")

    if fetch_pubchem:
        enriched, audit = salt.enrich_inventory_with_pubchem(x, cache_file=DEFAULT_CACHE)
        return enriched, audit

    x["structure_smiles_source"] = np.where(x["smiles"].map(salt.clean).ne(""), "INPUT_FILE", "MISSING")
    x["structure_status"] = np.where(x["smiles"].map(salt.clean).ne(""), "INPUT_SMILES", "PUBCHEM_NOT_RUN")
    x["pubchem_cid"] = ""
    x["pubchem_record_url"] = ""
    x["pubchem_error"] = ""
    return x, pd.DataFrame()


def screen_inventory(
    inventory: pd.DataFrame,
    rules: pd.DataFrame,
    promotion: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    if inventory is None or inventory.empty:
        return pd.DataFrame(), pd.DataFrame()
    promoted = promotion[promotion["promotion_eligible"].astype(bool)][
        ["rule_id", "designation_id", "promotion_status"]
    ].copy()
    active = promoted.merge(rules, on=["rule_id", "designation_id"], how="left")
    if active.empty:
        return pd.DataFrame(), _aggregate_inventory(inventory, pd.DataFrame())

    rows = []
    for _, inv in inventory.iterrows():
        for _, rr in active.iterrows():
            scope = salt.normalize_isomer_scope(rr.get("isomer_scope"))
            membership, comparison, inv_key, ref_key, inv_conn, ref_conn = salt._compare_parent_identity(
                inv.get("smiles"), rr.get("reference_smiles"), scope
            )
            conc = pd.to_numeric(pd.Series([inv.get("concentration_pct")]), errors="coerce").iloc[0]
            threshold = pd.to_numeric(pd.Series([rr.get("threshold_pct")]), errors="coerce").iloc[0]
            triggered = None
            if membership is True:
                if not np.isfinite(threshold):
                    status = "STRUCTURAL_MATCH_THRESHOLD_NOT_LINKED"
                elif not np.isfinite(conc):
                    status = "REVIEW_MISSING_CONCENTRATION"
                elif float(conc) >= float(threshold):
                    status = "REGULATORY_THRESHOLD_TRIGGER"
                    triggered = True
                else:
                    status = "STRUCTURAL_MATCH_BELOW_THRESHOLD"
                    triggered = False
            elif membership is False:
                status = "NO_MATCH_TO_THIS_PROMOTED_RULE"
                triggered = False
            else:
                status = comparison

            rows.append({
                **inv.to_dict(),
                "rule_id": salt.clean(rr.get("rule_id")),
                "designation_id": salt.clean(rr.get("designation_id")),
                "reference_parent_name": salt.clean(rr.get("reference_parent_name")),
                "reference_parent_cas": salt.clean(rr.get("reference_parent_cas")),
                "inventory_parent_key": inv_key,
                "inventory_parent_connectivity_key": inv_conn,
                "reference_parent_key": ref_key,
                "reference_parent_connectivity_key": ref_conn,
                "isomer_scope": scope,
                "salt_membership": membership,
                "parent_comparison_status": comparison,
                "threshold_pct": threshold,
                "threshold_source": salt.clean(rr.get("threshold_source")),
                "screening_triggered": triggered,
                "screening_status": status,
                "promotion_status": salt.clean(rr.get("promotion_status")),
            })
    detail = pd.DataFrame(rows)
    return detail, _aggregate_inventory(inventory, detail)


def _aggregate_inventory(inventory: pd.DataFrame, detail: pd.DataFrame) -> pd.DataFrame:
    rows = []
    for _, inv in inventory.iterrows():
        mid = salt.clean(inv.get("material_id"))
        g = detail[detail["material_id"].astype(str).eq(mid)] if not detail.empty else pd.DataFrame()
        if g.empty:
            decision = "REVIEW_NO_PROMOTED_RULE"
            matched = ""
            reason = "자동판정 가능한 승격 규칙이 없습니다."
        elif g["screening_triggered"].eq(True).any():
            decision = "REGULATORY_CANDIDATE"
            matched = "; ".join(g.loc[g["screening_triggered"].eq(True), "reference_parent_name"].astype(str).unique())
            reason = "승격 규칙의 parent 구조와 일치하고 연결된 농도기준 이상입니다."
        elif g["salt_membership"].eq(True).any():
            m = g[g["salt_membership"].eq(True)]
            matched = "; ".join(m["reference_parent_name"].astype(str).unique())
            if m["screening_status"].eq("STRUCTURAL_MATCH_BELOW_THRESHOLD").all():
                decision = "MATCH_BELOW_THRESHOLD"
                reason = "구조 범위에는 포함되지만 연결된 농도기준 미만입니다."
            else:
                decision = "REVIEW_REQUIRED"
                reason = "구조 범위에는 포함되지만 농도기준 또는 입력 농도 확인이 필요합니다."
        elif g["salt_membership"].isna().any():
            decision = "REVIEW_REQUIRED"
            matched = ""
            reason = "구조 누락·오류 또는 이성질체 적용범위 불확실성이 있습니다."
        else:
            decision = "NO_MATCH_WITHIN_PROMOTED_RULES"
            matched = ""
            reason = "현재 승격된 broad-salt 규칙 내에서 parent가 일치하지 않습니다. 전체 법령상 비규제를 의미하지 않습니다."
        rows.append({
            "material_id": mid,
            "chemical_name": salt.clean(inv.get("chemical_name")),
            "cas": salt.clean(inv.get("cas")),
            "concentration_pct": inv.get("concentration_pct"),
            "structure_status": salt.clean(inv.get("structure_status")),
            "matched_parent": matched,
            "final_decision": decision,
            "decision_reason": reason,
        })
    return pd.DataFrame(rows)


def dataframe_to_excel(sheets: dict[str, pd.DataFrame]) -> bytes:
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        for name, frame in sheets.items():
            safe = str(name)[:31]
            (frame if frame is not None else pd.DataFrame()).to_excel(writer, sheet_name=safe, index=False)
    return buffer.getvalue()


def molecular_image(smiles: str, size: tuple[int, int] = (520, 320)):
    try:
        from rdkit import Chem
        from rdkit.Chem import Draw

        mol = Chem.MolFromSmiles(salt.clean(smiles))
        return Draw.MolToImage(mol, size=size) if mol is not None else None
    except Exception:
        return None
