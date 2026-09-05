from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pandas as pd
from rdkit import Chem


ROOT = Path(__file__).resolve().parent
DEFAULT_EXTENDED_RULES = ROOT / "data" / "extended_scope_rules_APPROVED.csv"

SCOPE_TYPES = [
    "DIRECT_CAS",
    "PARENT_SALT",
    "SPECIFIED_SALT",
    "HYDRATE_SOLVATE",
    "STEREOISOMER",
    "STRUCTURAL_RANGE",
    "DERIVATIVE",
    "COMPOUND_GROUP",
    "REACTION_PRODUCT",
    "POLYMER_UVCB",
    "MANUAL_REVIEW",
]

RULE_COLUMNS = [
    "rule_id",
    "regime",
    "designation_id",
    "substance_name",
    "scope_type",
    "direct_cas",
    "reference_parent_cas",
    "reference_smiles",
    "required_smarts",
    "excluded_smarts",
    "allowed_elements",
    "required_elements",
    "excluded_cas",
    "isomer_scope",
    "salt_scope",
    "composition_condition",
    "concentration_threshold_pct",
    "use_condition",
    "quantity_threshold",
    "quantity_unit",
    "temporal_condition",
    "authorization_condition",
    "automation_status",
    "source_verified",
    "regulatory_source",
    "rule_version",
    "notes",
]


def clean(value: Any) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def truthy(value: Any) -> bool:
    return clean(value).lower() in {"1", "true", "yes", "y", "예", "확인", "verified"}


def split_values(value: Any) -> list[str]:
    return [part.strip() for part in re.split(r"[;,|]", clean(value)) if part.strip()]


def classify_scope_text(text: Any, has_direct_cas: bool = False) -> str:
    """Classify the legal identity expression; classification is not membership proof."""
    value = re.sub(r"\s+", "", clean(text))
    if not value:
        return "DIRECT_CAS" if has_direct_cas else "MANUAL_REVIEW"
    if "반응생성물" in value or "반응물" in value:
        return "REACTION_PRODUCT"
    if any(token in value for token in ["중합체", "고분자", "UVCB", "조성이불명"]):
        return "POLYMER_UVCB"
    if any(token in value for token in ["수화물", "용매화물"]):
        return "HYDRATE_SOLVATE"
    if any(token in value for token in ["이성질체", "광학이성질체", "입체이성질체"]):
        return "STEREOISOMER"
    if "유도체" in value:
        return "DERIVATIVE"
    if any(token in value for token in ["구조범위", "알킬기", "치환기", "탄소수", "일반식"]):
        return "STRUCTURAL_RANGE"
    if any(token in value for token in ["화합물", "화합물군", "혼합물", "및그화합물"]):
        return "COMPOUND_GROUP"
    if any(token in value for token in ["염류", "염을포함", "그염"]):
        return "PARENT_SALT"
    return "DIRECT_CAS" if has_direct_cas else "MANUAL_REVIEW"


def load_extended_scope_rules(path: Path = DEFAULT_EXTENDED_RULES) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=RULE_COLUMNS)
    frame = pd.read_csv(path, dtype=str).fillna("")
    for column in RULE_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    frame["scope_type"] = frame["scope_type"].map(lambda x: clean(x).upper())
    approved = frame["automation_status"].map(lambda x: clean(x).upper()).eq("APPROVED_AUTO")
    verified = frame["source_verified"].map(truthy)
    return frame[approved & verified].copy().reset_index(drop=True)


def validate_extended_scope_rules(frame: pd.DataFrame) -> pd.DataFrame:
    issues: list[dict] = []
    for idx, row in frame.iterrows():
        rule_id = clean(row.get("rule_id")) or f"row_{idx + 2}"
        scope = clean(row.get("scope_type")).upper()
        if scope not in SCOPE_TYPES:
            issues.append({"rule_id": rule_id, "issue": "UNKNOWN_SCOPE_TYPE"})
        if clean(row.get("automation_status")).upper() == "APPROVED_AUTO" and not truthy(
            row.get("source_verified")
        ):
            issues.append({"rule_id": rule_id, "issue": "SOURCE_NOT_VERIFIED"})
        for smarts in split_values(row.get("required_smarts")) + split_values(row.get("excluded_smarts")):
            if Chem.MolFromSmarts(smarts) is None:
                issues.append({"rule_id": rule_id, "issue": f"INVALID_SMARTS:{smarts}"})
    return pd.DataFrame(issues)


def _elements(mol: Chem.Mol) -> set[str]:
    return {atom.GetSymbol() for atom in mol.GetAtoms()}


def match_extended_scope(smiles: Any, cas: Any, rules: pd.DataFrame) -> list[dict]:
    """Apply only pre-approved deterministic SMARTS rules.

    Reaction products and polymer/UVCB rules are intentionally never inferred
    from a single SMILES because composition/process evidence is required.
    """
    value = clean(smiles)
    if not value or rules is None or rules.empty:
        return []
    mol = Chem.MolFromSmiles(value)
    if mol is None:
        return []
    cas_value = clean(cas)
    found: list[dict] = []
    for _, row in rules.iterrows():
        scope = clean(row.get("scope_type")).upper()
        if scope in {"REACTION_PRODUCT", "POLYMER_UVCB", "MANUAL_REVIEW", "DIRECT_CAS", "PARENT_SALT"}:
            continue
        if cas_value and cas_value in split_values(row.get("excluded_cas")):
            continue
        reference_smiles = clean(row.get("reference_smiles"))
        if scope in {"SPECIFIED_SALT", "HYDRATE_SOLVATE"}:
            reference = Chem.MolFromSmiles(reference_smiles) if reference_smiles else None
            if reference is None:
                continue
            if Chem.MolToSmiles(mol, isomericSmiles=True) != Chem.MolToSmiles(reference, isomericSmiles=True):
                continue
        if scope == "STEREOISOMER":
            reference = Chem.MolFromSmiles(reference_smiles) if reference_smiles else None
            isomer_scope = clean(row.get("isomer_scope")).upper()
            if reference is None or isomer_scope not in {"EXACT_STEREO_ONLY", "ALL_STEREOISOMERS"}:
                continue
            candidate_key = Chem.MolToSmiles(mol, isomericSmiles=isomer_scope == "EXACT_STEREO_ONLY")
            reference_key = Chem.MolToSmiles(reference, isomericSmiles=isomer_scope == "EXACT_STEREO_ONLY")
            if candidate_key != reference_key:
                continue
        required = [Chem.MolFromSmarts(x) for x in split_values(row.get("required_smarts"))]
        excluded = [Chem.MolFromSmarts(x) for x in split_values(row.get("excluded_smarts"))]
        if scope not in {"SPECIFIED_SALT", "HYDRATE_SOLVATE", "STEREOISOMER"} and not required:
            continue
        if any(pattern is None for pattern in required + excluded):
            continue
        if not all(mol.HasSubstructMatch(pattern) for pattern in required):
            continue
        if any(mol.HasSubstructMatch(pattern) for pattern in excluded):
            continue
        elements = _elements(mol)
        allowed = set(split_values(row.get("allowed_elements")))
        required_elements = set(split_values(row.get("required_elements")))
        if allowed and not elements.issubset(allowed):
            continue
        if required_elements and not required_elements.issubset(elements):
            continue
        found.append(
            {
                **row.to_dict(),
                "match_type": "검증된 구조범위 규칙",
                "scope_type": scope,
                "scope_match_status": "APPROVED_DETERMINISTIC_MATCH",
            }
        )
    return found
