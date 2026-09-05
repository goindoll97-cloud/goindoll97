# -*- coding: utf-8 -*-
"""Broad-salt identity resolution for the FIRST PAPER only.

Scope of this module
--------------------
This module implements only the first-paper proof of concept:
    explicit broad-salt regulatory scopes such as "A and its salts".

It intentionally DOES NOT implement:
    - generic derivative families
    - structural ranges / SMARTS families
    - mixtures / reaction masses
    - reaction products / UVCB

Those remain REVIEW_REQUIRED / future work.

Scientific design
-----------------
1) A source-verified machine-executable parent rule (reference SMILES) is curated
   from the official regulatory text and traceable public chemical-identity sources.
2) The parent rule is frozen before independent validation outcomes are evaluated.
3) Independent validation cases use source-supported reference labels that are not
   used by the RDKit prediction step.
4) A rule can be promoted to automatic screening only when:
       source evidence is complete
       AND the reference rule was frozen before validation
       AND the rule is machine executable
       AND independent validation passes pre-specified criteria.
5) If a rule is not promoted, both apparent MATCH and apparent NO-MATCH remain
   review-only evidence. This prevents unsupported NO-MATCH results from becoming
   false-safe automatic decisions.
"""
from __future__ import annotations

import os
import re
import json
import time
from itertools import product
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent

RULE_FILE_ENV = "BROAD_SALT_RULES_FILE"
VALIDATION_FILE_ENV = "BROAD_SALT_VALIDATION_FILE"

DEFAULT_RULE_FILES = [
    ROOT / "broad_salt_rules.xlsx",
    ROOT / "broad_salt_rules.csv",
]
DEFAULT_VALIDATION_FILES = [
    ROOT / "broad_salt_validation_cases.xlsx",
    ROOT / "broad_salt_validation_cases.csv",
]

PUBCHEM_API_ROOT = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
PUBCHEM_CACHE_DEFAULT = ROOT / "intermediate" / "pubchem_structure_cache.csv"
PUBCHEM_CACHE_COLUMNS = [
    "cas",
    "structure_status",
    "pubchem_cid",
    "pubchem_title",
    "pubchem_isomeric_smiles",
    "pubchem_connectivity_smiles",
    "pubchem_inchikey",
    "pubchem_cas_verified",
    "pubchem_retrieval_date",
    "pubchem_record_url",
    "pubchem_query_url",
    "pubchem_error",
]

# Pre-specified promotion policy. These values are written to output so that the
# Methods section can state the exact criteria used in the analysis.
MIN_CASES = int(os.getenv("SALT_VALIDATION_MIN_CASES", "5"))
MIN_POSITIVE = int(os.getenv("SALT_VALIDATION_MIN_POSITIVE", "2"))
MIN_NEGATIVE = int(os.getenv("SALT_VALIDATION_MIN_NEGATIVE", "2"))
MIN_ACCURACY = float(os.getenv("SALT_VALIDATION_MIN_ACCURACY", "0.95"))
STUDY_TARGET_RULES = int(os.getenv("SALT_STUDY_TARGET_RULES", "12"))
TARGET_CASES_PER_RULE = int(os.getenv("SALT_TARGET_CASES_PER_RULE", "10"))
TARGET_POSITIVE_PER_RULE = int(os.getenv("SALT_TARGET_POSITIVE_PER_RULE", "2"))
TARGET_NEGATIVE_PER_RULE = int(os.getenv("SALT_TARGET_NEGATIVE_PER_RULE", "2"))
REQUIRE_ZERO_FALSE_SAFE = os.getenv(
    "SALT_VALIDATION_REQUIRE_ZERO_FALSE_SAFE", "1"
).strip().lower() not in {"0", "false", "no", "off"}

# Secondary, counterfactual sensitivity grid. The pre-specified primary policy
# above is never replaced by this grid; it is included as one labelled scenario.
SENSITIVITY_MIN_CASES = (5, 8, 10)
SENSITIVITY_MIN_POSITIVE = (2, 3, 5)
SENSITIVITY_MIN_ACCURACY = (0.90, 0.95, 1.00)
SENSITIVITY_MIN_NEGATIVE = MIN_NEGATIVE

BROAD_SALT_PATTERNS = [
    r"(?:와|과|및)\s*그\s*염류",
    r"(?:와|과|및)\s*그\s*염\b",
    r"\band\s+(?:all\s+)?(?:its|their)\s+salts\b",
]

RULE_COLUMNS = [
    "rule_id",
    "designation_id",
    "reference_parent_name",
    "reference_parent_cas",
    "reference_smiles",
    "salt_scope",
    "automation_status",
    "isomer_scope",
    "study_target",
    "curation_status",
    "source_verified",
    "reference_rule_frozen",
    "regulatory_source",
    "chemical_identity_source",
    "rule_version",
    "frozen_date",
    "notes",
]

VALIDATION_COLUMNS = [
    "case_id",
    "rule_id",
    "designation_id",
    "chemical_name",
    "cas",
    "smiles",
    "reference_membership",
    "reference_source",
    "reference_review_status",
    "notes",
]


def clean(x) -> str:
    if x is None:
        return ""
    try:
        if bool(pd.isna(x)):
            return ""
    except (TypeError, ValueError):
        pass
    s = str(x).strip()
    return "" if s.lower() in {"nan", "none", "null", "<na>"} else s


def as_bool(x) -> bool:
    if isinstance(x, (bool, np.bool_)):
        return bool(x)
    return clean(x).lower() in {"1", "true", "yes", "y", "verified", "frozen", "예"}


def parse_membership(x) -> Optional[bool]:
    s = clean(x).upper()
    if s in {"MATCH", "Y", "YES", "TRUE", "1", "IN", "MEMBER", "POSITIVE"}:
        return True
    if s in {"NO_MATCH", "N", "NO", "FALSE", "0", "OUT", "NON_MEMBER", "NEGATIVE"}:
        return False
    return None


def reference_reviewed(x) -> bool:
    """Treat legacy blank rows as curated; explicit candidate markers require review."""
    s = clean(x).upper()
    if not s:
        return True
    return s in {"APPROVED", "REVIEWED", "SOURCE_VERIFIED", "CONFIRMED", "1", "TRUE", "YES"}


def automation_allowed(x) -> bool:
    return clean(x).lower() in {"auto", "automatic", "approved_auto", "1", "true", "yes"}


def salt_scope_supported(x) -> bool:
    return clean(x).lower() in {"all_counterion_salts", "all_salts", "broad_salt"}


def broad_salt_scope(text: str) -> bool:
    t = clean(text)
    return any(re.search(p, t, flags=re.I) for p in BROAD_SALT_PATTERNS)


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() in {".xlsx", ".xls"}:
        return pd.read_excel(path)
    return pd.read_csv(path)


def _find_input(env_name: str, defaults: list[Path]) -> Optional[Path]:
    explicit = clean(os.getenv(env_name, ""))
    if explicit:
        p = Path(explicit)
        return p if p.exists() else None
    for p in defaults:
        if p.exists():
            return p
    return None


def load_rules() -> tuple[pd.DataFrame, str]:
    p = _find_input(RULE_FILE_ENV, DEFAULT_RULE_FILES)
    if p is None:
        return pd.DataFrame(columns=RULE_COLUMNS), "INPUT_REQUIRED"
    raw = _read_table(p)

    for c in RULE_COLUMNS:
        if c not in raw.columns:
            raw[c] = ""
    x = raw[RULE_COLUMNS].copy()
    rule_identity_input = pd.DataFrame({
        "chemical_name": x["reference_parent_name"],
        "cas": x["reference_parent_cas"],
        "smiles": x["reference_smiles"],
    })
    rule_identity, _ = enrich_inventory_with_pubchem(
        rule_identity_input,
        cache_file=ROOT / "intermediate" / "pubchem_rule_structure_cache.csv",
    )
    x["reference_smiles_input"] = x["reference_smiles"].map(clean)
    x["reference_smiles"] = rule_identity["smiles"].values
    x["reference_smiles_source"] = rule_identity["structure_smiles_source"].values
    x["reference_structure_status"] = rule_identity["structure_status"].values
    x["reference_pubchem_cid"] = rule_identity["pubchem_cid"].values
    x["reference_pubchem_record_url"] = rule_identity["pubchem_record_url"].values
    # An exact-CAS-verified PubChem record is retained as chemical-identity
    # provenance, but source_verified still remains a separate researcher flag.
    missing_identity_source = x["chemical_identity_source"].map(clean).eq("")
    pubchem_resolved = x["reference_smiles_source"].eq("PUBCHEM_PUG_REST")
    x.loc[missing_identity_source & pubchem_resolved, "chemical_identity_source"] = x.loc[
        missing_identity_source & pubchem_resolved, "reference_pubchem_record_url"
    ]
    x["source_verified_bool"] = x["source_verified"].map(as_bool)
    x["reference_rule_frozen_bool"] = x["reference_rule_frozen"].map(as_bool)
    x["automation_allowed_bool"] = x["automation_status"].map(automation_allowed)
    x["salt_scope_supported_bool"] = x["salt_scope"].map(salt_scope_supported)
    x["reference_parent_key"] = x["reference_smiles"].map(parent_key)
    x["reference_parent_connectivity_key"] = x["reference_smiles"].map(parent_connectivity_key)
    x["isomer_scope_normalized"] = x["isomer_scope"].map(normalize_isomer_scope)
    x["machine_executable"] = x["reference_parent_key"].astype(str).str.len().gt(0)
    x["source_evidence_complete"] = (
        x["source_verified_bool"].astype(bool)
        & x["regulatory_source"].fillna("").astype(str).str.strip().ne("")
        & x["chemical_identity_source"].fillna("").astype(str).str.strip().ne("")
    )
    x["input_file"] = str(p)
    return x, f"LOADED:{p.name}"


def load_validation_cases() -> tuple[pd.DataFrame, str]:
    p = _find_input(VALIDATION_FILE_ENV, DEFAULT_VALIDATION_FILES)
    if p is None:
        return pd.DataFrame(columns=VALIDATION_COLUMNS), "INPUT_REQUIRED"
    raw = _read_table(p)

    for c in VALIDATION_COLUMNS:
        if c not in raw.columns:
            raw[c] = ""
    x = raw[VALIDATION_COLUMNS].copy()
    validation_identity_input = pd.DataFrame({
        "chemical_name": x["chemical_name"],
        "cas": x["cas"],
        "smiles": x["smiles"],
    })
    validation_identity, _ = enrich_inventory_with_pubchem(
        validation_identity_input,
        cache_file=ROOT / "intermediate" / "pubchem_validation_structure_cache.csv",
    )
    x["smiles_input"] = x["smiles"].map(clean)
    x["smiles"] = validation_identity["smiles"].values
    x["smiles_source"] = validation_identity["structure_smiles_source"].values
    x["structure_status"] = validation_identity["structure_status"].values
    x["pubchem_cid"] = validation_identity["pubchem_cid"].values
    x["pubchem_record_url"] = validation_identity["pubchem_record_url"].values
    x["reference_membership_bool"] = x["reference_membership"].map(parse_membership)
    x["reference_source_present"] = x["reference_source"].fillna("").astype(str).str.strip().ne("")
    x["reference_reviewed_bool"] = x["reference_review_status"].map(reference_reviewed)
    x["reference_supported"] = (
        x["reference_membership_bool"].notna()
        & x["reference_source_present"]
        & x["reference_reviewed_bool"]
    )
    x["input_file"] = str(p)
    return x, f"LOADED:{p.name}"


def _standardized_parent_smiles(smiles: str, isomeric: bool) -> str:
    """Return a standardized parent SMILES while retaining the requested detail."""
    smi = clean(smiles)
    if not smi:
        return ""
    try:
        from rdkit import Chem
        from rdkit.Chem.MolStandardize import rdMolStandardize

        mol = Chem.MolFromSmiles(smi)
        if mol is None:
            return ""
        parent = rdMolStandardize.FragmentParent(mol)
        try:
            parent = rdMolStandardize.Uncharger().uncharge(parent)
        except Exception:
            pass
        return Chem.MolToSmiles(parent, canonical=True, isomericSmiles=isomeric)
    except Exception:
        return ""


def parent_key(smiles: str) -> str:
    """Return a stereochemistry-aware parent key after salt standardization.

    This is a cheminformatics identity candidate, not a legal identity by itself.
    """
    return _standardized_parent_smiles(smiles, isomeric=True)


def parent_connectivity_key(smiles: str) -> str:
    """Return the same parent key with stereochemical annotations removed."""
    return _standardized_parent_smiles(smiles, isomeric=False)


def normalize_isomer_scope(x) -> str:
    value = clean(x).upper()
    aliases = {
        "ALL": "ALL_STEREOISOMERS",
        "ALL_ISOMERS": "ALL_STEREOISOMERS",
        "ALL_STEREOISOMERS": "ALL_STEREOISOMERS",
        "EXACT": "EXACT_STEREO_ONLY",
        "EXACT_ONLY": "EXACT_STEREO_ONLY",
        "EXACT_STEREO_ONLY": "EXACT_STEREO_ONLY",
    }
    return aliases.get(value, "UNSPECIFIED_REVIEW")


def normalize_cas(x) -> str:
    """Normalize a CAS Registry Number without guessing missing punctuation."""
    return re.sub(r"\s+", "", clean(x))


def valid_cas_checksum(cas: str) -> bool:
    """Return True only for a syntactically and checksum-valid CAS RN."""
    value = normalize_cas(cas)
    if not re.fullmatch(r"\d{2,7}-\d{2}-\d", value):
        return False
    body, check = value.rsplit("-", 1)
    digits = body.replace("-", "")
    checksum = sum((i + 1) * int(d) for i, d in enumerate(reversed(digits))) % 10
    return checksum == int(check)


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return clean(raw).lower() not in {"0", "false", "no", "off"}


def _pubchem_json(url: str, timeout_seconds: float, max_retries: int) -> dict:
    """Read one PubChem PUG REST JSON document with bounded retries."""
    user_agent = clean(os.getenv("PUBCHEM_USER_AGENT", "")) or (
        "first-paper-broad-salt-framework/2.1 (PubChem PUG REST structure lookup)"
    )
    last_error = None
    for attempt in range(max(1, max_retries)):
        try:
            request = Request(url, headers={"User-Agent": user_agent, "Accept": "application/json"})
            with urlopen(request, timeout=timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            if exc.code == 404:
                raise LookupError("PUBCHEM_NOT_FOUND") from exc
            last_error = exc
            if exc.code not in {408, 429, 500, 502, 503, 504}:
                break
        except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
        if attempt + 1 < max(1, max_retries):
            time.sleep(min(2.0, 0.5 * (2**attempt)))
    raise RuntimeError(f"{type(last_error).__name__}: {last_error}") from last_error


def fetch_pubchem_structure_by_cas(
    cas: str,
    timeout_seconds: Optional[float] = None,
    max_retries: Optional[int] = None,
) -> dict:
    """Fetch a structure only when PubChem confirms the exact CAS as a synonym.

    Both stereochemistry-aware and connectivity SMILES are retained.  Legal scope
    is deliberately not inferred here; this function only resolves chemical
    identity evidence for the later broad-salt rule.
    """
    cas_norm = normalize_cas(cas)
    retrieved = datetime.now(timezone.utc).date().isoformat()
    base = {c: "" for c in PUBCHEM_CACHE_COLUMNS}
    base.update({"cas": cas_norm, "pubchem_retrieval_date": retrieved})

    if not valid_cas_checksum(cas_norm):
        base["structure_status"] = "PUBCHEM_INVALID_CAS"
        base["pubchem_error"] = "CAS format or checksum is invalid"
        return base

    timeout = timeout_seconds if timeout_seconds is not None else float(
        os.getenv("PUBCHEM_TIMEOUT_SECONDS", "20")
    )
    retries = max_retries if max_retries is not None else int(
        os.getenv("PUBCHEM_MAX_RETRIES", "3")
    )
    encoded = quote(cas_norm, safe="")
    query_url = (
        f"{PUBCHEM_API_ROOT}/compound/name/{encoded}/property/"
        "Title,CanonicalSMILES,IsomericSMILES,InChIKey/JSON"
    )
    base["pubchem_query_url"] = query_url

    try:
        payload = _pubchem_json(query_url, timeout, retries)
        properties = payload.get("PropertyTable", {}).get("Properties", [])
        if not properties:
            raise LookupError("PUBCHEM_NOT_FOUND")

        verified = []
        for prop in properties:
            cid = clean(prop.get("CID"))
            if not cid:
                continue
            synonym_url = f"{PUBCHEM_API_ROOT}/compound/cid/{quote(cid, safe='')}/synonyms/JSON"
            synonyms_payload = _pubchem_json(synonym_url, timeout, retries)
            info = synonyms_payload.get("InformationList", {}).get("Information", [])
            synonyms = info[0].get("Synonym", []) if info else []
            if cas_norm in {normalize_cas(s) for s in synonyms}:
                verified.append(prop)

        if len(verified) == 0:
            base["structure_status"] = "PUBCHEM_CAS_SYNONYM_MISMATCH"
            base["pubchem_error"] = "Name query resolved, but exact CAS was not found in PubChem synonyms"
            return base
        if len(verified) > 1:
            base["structure_status"] = "PUBCHEM_MULTIPLE_CANDIDATES_REVIEW"
            base["pubchem_error"] = "More than one PubChem CID carries the exact CAS synonym"
            base["pubchem_cid"] = ";".join(clean(p.get("CID")) for p in verified)
            return base

        prop = verified[0]
        cid = clean(prop.get("CID"))
        # PubChem's current JSON may use SMILES/ConnectivitySMILES aliases even
        # when the legacy CanonicalSMILES/IsomericSMILES properties are requested.
        isomeric = clean(prop.get("IsomericSMILES")) or clean(prop.get("SMILES"))
        connectivity = (
            clean(prop.get("CanonicalSMILES"))
            or clean(prop.get("ConnectivitySMILES"))
            or isomeric
        )
        if not isomeric and not connectivity:
            base["structure_status"] = "PUBCHEM_MISSING_SMILES_REVIEW"
            base["pubchem_error"] = "Verified CID did not return a SMILES property"
            base["pubchem_cid"] = cid
            return base

        base.update({
            "structure_status": "PUBCHEM_EXACT_CAS_VERIFIED",
            "pubchem_cid": cid,
            "pubchem_title": clean(prop.get("Title")),
            "pubchem_isomeric_smiles": isomeric or connectivity,
            "pubchem_connectivity_smiles": connectivity or isomeric,
            "pubchem_inchikey": clean(prop.get("InChIKey")),
            "pubchem_cas_verified": True,
            "pubchem_record_url": f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}",
        })
        return base
    except LookupError:
        base["structure_status"] = "PUBCHEM_NOT_FOUND"
        base["pubchem_error"] = "No PubChem compound was found for the CAS query"
        return base
    except Exception as exc:
        base["structure_status"] = "PUBCHEM_REQUEST_ERROR"
        base["pubchem_error"] = f"{type(exc).__name__}: {exc}"
        return base


def _load_pubchem_cache(path: Path) -> dict[str, dict]:
    if not path.exists():
        return {}
    try:
        cached = pd.read_csv(path, dtype=str).fillna("")
    except Exception:
        return {}
    if "cas" not in cached.columns:
        return {}
    return {
        normalize_cas(row.get("cas")): {c: clean(row.get(c)) for c in PUBCHEM_CACHE_COLUMNS}
        for _, row in cached.iterrows()
        if normalize_cas(row.get("cas"))
    }


def _save_pubchem_cache(path: Path, records: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [{c: clean(record.get(c)) for c in PUBCHEM_CACHE_COLUMNS} for record in records.values()]
    pd.DataFrame(rows, columns=PUBCHEM_CACHE_COLUMNS).sort_values("cas").to_csv(
        path, index=False, encoding="utf-8-sig"
    )


def enrich_inventory_with_pubchem(
    inventory: pd.DataFrame,
    cache_file: Optional[str | Path] = None,
    auto_fetch: Optional[bool] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Fill missing inventory SMILES from source-traceable PubChem CAS records.

    A user-supplied SMILES always takes precedence. PubChem results are accepted
    only after exact-CAS synonym verification; every other outcome remains review.
    Returns the enriched inventory and a row-level identity-resolution audit.
    """
    if inventory is None or not len(inventory):
        return pd.DataFrame() if inventory is None else inventory.copy(), pd.DataFrame()

    enabled = _env_bool("PUBCHEM_AUTO_SMILES", True) if auto_fetch is None else bool(auto_fetch)
    refresh = _env_bool("PUBCHEM_REFRESH_CACHE", False)
    cache_path = Path(cache_file or os.getenv("PUBCHEM_CACHE_FILE", PUBCHEM_CACHE_DEFAULT))
    cache = _load_pubchem_cache(cache_path)
    transient = {"PUBCHEM_REQUEST_ERROR"}
    out = inventory.copy()
    if "smiles" not in out.columns:
        out["smiles"] = ""
    out["input_smiles"] = out["smiles"].map(clean)

    audit_rows = []
    resolved_smiles = []
    for idx, row in out.iterrows():
        cas = normalize_cas(row.get("cas"))
        supplied = clean(row.get("smiles"))
        record = {c: "" for c in PUBCHEM_CACHE_COLUMNS}
        record["cas"] = cas

        if supplied:
            record["structure_status"] = "USER_SMILES_PRESENT"
            source = "USER_SUPPLIED"
            chosen = supplied
        elif not enabled:
            record["structure_status"] = "PUBCHEM_AUTO_FETCH_DISABLED"
            source = "UNRESOLVED"
            chosen = ""
        else:
            cached = cache.get(cas)
            if cached and not refresh and clean(cached.get("structure_status")) not in transient:
                record.update(cached)
                record["structure_status"] = "CACHED_" + clean(cached.get("structure_status"))
                original_status = clean(cached.get("structure_status"))
            else:
                record = fetch_pubchem_structure_by_cas(cas)
                original_status = clean(record.get("structure_status"))
                if cas and original_status not in transient:
                    cache[cas] = record.copy()
            if original_status == "PUBCHEM_EXACT_CAS_VERIFIED":
                chosen = clean(record.get("pubchem_isomeric_smiles")) or clean(
                    record.get("pubchem_connectivity_smiles")
                )
                source = "PUBCHEM_PUG_REST"
            else:
                chosen = ""
                source = "UNRESOLVED"

        resolved_smiles.append(chosen)
        audit_rows.append({
            "inventory_row": idx,
            "chemical_name": clean(row.get("chemical_name")) or clean(row.get("name")),
            "cas": cas,
            "input_smiles": supplied,
            "resolved_smiles": chosen,
            "structure_smiles_source": source,
            **record,
        })

    out["smiles"] = resolved_smiles
    audit = pd.DataFrame(audit_rows)
    audit_by_row = audit.set_index("inventory_row")
    for column in [
        "structure_smiles_source", "structure_status", "pubchem_cid", "pubchem_title",
        "pubchem_isomeric_smiles", "pubchem_connectivity_smiles", "pubchem_inchikey",
        "pubchem_cas_verified", "pubchem_retrieval_date", "pubchem_record_url", "pubchem_error",
    ]:
        out[column] = [clean(audit_by_row.at[idx, column]) for idx in out.index]

    if enabled and cache:
        _save_pubchem_cache(cache_path, cache)
    return out, audit


def summarize_pubchem_enrichment(inventory: pd.DataFrame) -> pd.DataFrame:
    if inventory is None or not len(inventory):
        return pd.DataFrame()
    source = inventory.get("structure_smiles_source", pd.Series("", index=inventory.index)).astype(str)
    status = inventory.get("structure_status", pd.Series("", index=inventory.index)).astype(str)
    missing_initial = inventory.get("input_smiles", pd.Series("", index=inventory.index)).map(clean).eq("")
    available = inventory.get("smiles", pd.Series("", index=inventory.index)).map(clean).ne("")
    pubchem_resolved = source.eq("PUBCHEM_PUG_REST")
    return pd.DataFrame([{
        "n_inventory_rows": len(inventory),
        "n_user_smiles": int(source.eq("USER_SUPPLIED").sum()),
        "n_initially_missing_smiles": int(missing_initial.sum()),
        "n_pubchem_resolved": int(pubchem_resolved.sum()),
        "n_structure_available": int(available.sum()),
        "n_structure_review_required": int((~available).sum()),
        "structure_availability_rate": float(available.mean()),
        "pubchem_resolution_rate_among_initially_missing": (
            float(pubchem_resolved.sum() / missing_initial.sum()) if missing_initial.sum() else np.nan
        ),
        "n_pubchem_request_errors": int(status.str.contains("PUBCHEM_REQUEST_ERROR", regex=False).sum()),
    }])


def validation_policy_table() -> pd.DataFrame:
    return pd.DataFrame([
        {"criterion": "minimum_decided_cases_per_rule", "value": MIN_CASES},
        {"criterion": "minimum_positive_cases_per_rule", "value": MIN_POSITIVE},
        {"criterion": "minimum_negative_cases_per_rule", "value": MIN_NEGATIVE},
        {"criterion": "study_target_rule_count", "value": STUDY_TARGET_RULES},
        {"criterion": "target_curated_cases_per_rule", "value": TARGET_CASES_PER_RULE},
        {"criterion": "target_positive_cases_per_rule", "value": TARGET_POSITIVE_PER_RULE},
        {"criterion": "target_negative_cases_per_rule", "value": TARGET_NEGATIVE_PER_RULE},
        {"criterion": "minimum_selective_accuracy", "value": MIN_ACCURACY},
        {"criterion": "require_zero_false_safe", "value": int(REQUIRE_ZERO_FALSE_SAFE)},
        {
            "criterion": "secondary_threshold_sensitivity_grid",
            "value": (
                "decided_cases={5,8,10}; positive_cases={2,3,5}; "
                f"negative_cases={SENSITIVITY_MIN_NEGATIVE}; accuracy={{0.90,0.95,1.00}}; false_safe=0"
            ),
        },
        {
            "criterion": "gate_ablation_interpretation",
            "value": "S1-S3 counterfactual_not_deployable; S4 full_promotion primary_deployable_framework",
        },
        {
            "criterion": "promotion_logic",
            "value": (
                "source_evidence_complete AND reference_rule_frozen AND machine_executable AND "
                "supported_salt_scope AND automation_status=auto AND "
                "independent_source_supported_validation_pass"
            ),
        },
    ])


def _resolve_rule(case: pd.Series, rules: pd.DataFrame) -> Optional[pd.Series]:
    if rules is None or not len(rules):
        return None
    rid = clean(case.get("rule_id"))
    did = clean(case.get("designation_id"))
    if rid:
        g = rules[rules["rule_id"].astype(str).eq(rid)]
        if len(g) == 1:
            return g.iloc[0]
        if len(g) > 1:
            return None
    if did:
        g = rules[rules["designation_id"].astype(str).eq(did)]
        if len(g) == 1:
            return g.iloc[0]
    return None


def _compare_parent_identity(
    candidate_smiles: str,
    reference_smiles: str,
    isomer_scope,
) -> tuple[Optional[bool], str, str, str, str, str]:
    """Compare parent identity without silently deciding an unspecified stereo scope."""
    candidate_key = parent_key(candidate_smiles)
    candidate_connectivity = parent_connectivity_key(candidate_smiles)
    reference_key = parent_key(reference_smiles)
    reference_connectivity = parent_connectivity_key(reference_smiles)
    scope = normalize_isomer_scope(isomer_scope)

    if not reference_key or not reference_connectivity:
        return None, "REVIEW_INVALID_REFERENCE_SMILES", candidate_key, reference_key, candidate_connectivity, reference_connectivity
    if not candidate_key or not candidate_connectivity:
        return None, "REVIEW_MISSING_OR_INVALID_SMILES", candidate_key, reference_key, candidate_connectivity, reference_connectivity
    if candidate_key == reference_key:
        return True, "PARENT_MATCH_EXACT_STEREO", candidate_key, reference_key, candidate_connectivity, reference_connectivity
    if candidate_connectivity != reference_connectivity:
        return False, "PARENT_NO_MATCH_CONNECTIVITY", candidate_key, reference_key, candidate_connectivity, reference_connectivity
    if scope == "ALL_STEREOISOMERS":
        return True, "PARENT_MATCH_ALL_STEREOISOMERS_RULE", candidate_key, reference_key, candidate_connectivity, reference_connectivity
    if scope == "EXACT_STEREO_ONLY":
        return False, "PARENT_NO_MATCH_EXACT_STEREO_RULE", candidate_key, reference_key, candidate_connectivity, reference_connectivity
    return None, "REVIEW_STEREOISOMER_SCOPE_UNSPECIFIED", candidate_key, reference_key, candidate_connectivity, reference_connectivity


def evaluate_validation_cases(cases: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    rows = []
    if cases is None or not len(cases):
        return pd.DataFrame()

    for _, case in cases.iterrows():
        rr = _resolve_rule(case, rules)
        truth = parse_membership(case.get("reference_membership"))
        reference_source = clean(case.get("reference_source"))
        review_status = clean(case.get("reference_review_status"))
        reviewed = reference_reviewed(review_status)
        reference_supported = bool(truth is not None and reference_source and reviewed)
        case_key = parent_key(case.get("smiles"))
        case_connectivity_key = parent_connectivity_key(case.get("smiles"))

        if rr is None:
            status = "REVIEW_RULE_NOT_UNIQUELY_RESOLVED"
            pred = None
            ref_key = ""
            ref_connectivity_key = ""
            isomer_scope = "UNSPECIFIED_REVIEW"
            rid = clean(case.get("rule_id"))
            did = clean(case.get("designation_id"))
        else:
            rid = clean(rr.get("rule_id"))
            did = clean(rr.get("designation_id"))
            isomer_scope = normalize_isomer_scope(rr.get("isomer_scope"))
            pred, comparison_status, case_key, ref_key, case_connectivity_key, ref_connectivity_key = (
                _compare_parent_identity(case.get("smiles"), rr.get("reference_smiles"), isomer_scope)
            )
            if comparison_status == "REVIEW_MISSING_OR_INVALID_SMILES":
                status = "REVIEW_INVALID_CASE_SMILES"
            elif pred is None:
                status = comparison_status
            else:
                status = "DECIDED_MATCH" if pred else "DECIDED_NO_MATCH"

        decided = pred is not None and reference_supported
        correct = bool(pred == truth) if decided else None
        false_safe = bool(truth and (pred is False)) if decided else False
        over_screen = bool((truth is False) and pred) if decided else False

        rows.append({
            "case_id": clean(case.get("case_id")),
            "rule_id": rid,
            "designation_id": did,
            "chemical_name": clean(case.get("chemical_name")),
            "cas": clean(case.get("cas")),
            "smiles": clean(case.get("smiles")),
            "case_parent_key": case_key,
            "case_parent_connectivity_key": case_connectivity_key,
            "reference_parent_key": ref_key,
            "reference_parent_connectivity_key": ref_connectivity_key,
            "isomer_scope": isomer_scope,
            "reference_membership": clean(case.get("reference_membership")),
            "reference_membership_bool": truth,
            "reference_source": reference_source,
            "reference_review_status": review_status or "LEGACY_CURATED",
            "reference_reviewed_bool": reviewed,
            "reference_supported": reference_supported,
            "reference_support_status": (
                "SOURCE_SUPPORTED" if reference_supported else
                ("MISSING_REFERENCE_LABEL" if truth is None else
                 ("MISSING_REFERENCE_SOURCE" if not reference_source else "RESEARCHER_CONFIRM_REQUIRED"))
            ),
            "predicted_membership_bool": pred,
            "decision_status": status,
            "decided": decided,
            "correct": correct,
            "false_safe": false_safe,
            "over_screen": over_screen,
        })

    return pd.DataFrame(rows)


def summarize_validation_by_rule(detail: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    if rules is None or not len(rules):
        return pd.DataFrame()

    rows = []
    for _, rr in rules.iterrows():
        rid = clean(rr.get("rule_id"))
        did = clean(rr.get("designation_id"))
        g = detail[detail["rule_id"].astype(str).eq(rid)].copy() if len(detail) and rid else pd.DataFrame()
        if not len(g) and len(detail) and did:
            g = detail[detail["designation_id"].astype(str).eq(did)].copy()

        d = g[g.get("decided", pd.Series(dtype=bool)).astype(bool)].copy() if len(g) else pd.DataFrame()
        if len(d):
            truth = d["reference_membership_bool"].astype(bool)
            pred = d["predicted_membership_bool"].astype(bool)
            tp = int((truth & pred).sum())
            tn = int((~truth & ~pred).sum())
            fp = int((~truth & pred).sum())
            fn = int((truth & ~pred).sum())
            acc = (tp + tn) / len(d)
            n_pos = int(truth.sum())
            n_neg = int((~truth).sum())
        else:
            tp = tn = fp = fn = n_pos = n_neg = 0
            acc = np.nan

        false_safe_rate = (fn / n_pos) if n_pos else np.nan
        # Exact one-sided 95% upper bound when zero false-safe events are
        # observed. This prevents "zero observed" from being reported as
        # proof that the underlying risk is zero.
        zero_event_upper_95 = (1.0 - 0.05 ** (1.0 / n_pos)) if n_pos and fn == 0 else np.nan

        rows.append({
            "rule_id": rid,
            "designation_id": did,
            "reference_parent_name": clean(rr.get("reference_parent_name")),
            "reference_smiles": clean(rr.get("reference_smiles")),
            "source_verified_bool": bool(rr.get("source_verified_bool", False)),
            "source_evidence_complete": bool(rr.get("source_evidence_complete", False)),
            "reference_rule_frozen_bool": bool(rr.get("reference_rule_frozen_bool", False)),
            "machine_executable": bool(rr.get("machine_executable", False)),
            "salt_scope": clean(rr.get("salt_scope")),
            "salt_scope_supported_bool": bool(rr.get("salt_scope_supported_bool", False)),
            "automation_status": clean(rr.get("automation_status")),
            "automation_allowed_bool": bool(rr.get("automation_allowed_bool", False)),
            "study_target": clean(rr.get("study_target")),
            "curation_status": clean(rr.get("curation_status")),
            "n_validation_rows": int(len(g)),
            "n_decided": int(len(d)),
            "n_positive": n_pos,
            "n_negative": n_neg,
            "TP": tp,
            "TN": tn,
            "FP": fp,
            "FN": fn,
            "selective_accuracy": acc,
            "false_safe_n": fn,
            "false_safe_rate": false_safe_rate,
            "false_safe_zero_event_upper_95": zero_event_upper_95,
            "over_screen_n": fp,
            "regulatory_source": clean(rr.get("regulatory_source")),
            "chemical_identity_source": clean(rr.get("chemical_identity_source")),
            "rule_version": clean(rr.get("rule_version")),
            "frozen_date": clean(rr.get("frozen_date")),
        })

    return pd.DataFrame(rows)


def build_rule_promotion_with_policy(
    validation_by_rule: pd.DataFrame,
    *,
    min_cases: int,
    min_positive: int,
    min_negative: int,
    min_accuracy: float,
    require_zero_false_safe: bool,
) -> pd.DataFrame:
    """Apply one explicitly supplied rule-promotion policy.

    Keeping this function parameterized separates the pre-specified primary
    gate from the secondary sensitivity analysis and prevents environment
    settings from being silently changed inside a scenario.
    """
    if validation_by_rule is None or not len(validation_by_rule):
        return pd.DataFrame()
    x = validation_by_rule.copy()
    x["independent_source_supported_validation_pass"] = (
        (x["n_decided"] >= int(min_cases))
        & (x["n_positive"] >= int(min_positive))
        & (x["n_negative"] >= int(min_negative))
        & pd.to_numeric(x["selective_accuracy"], errors="coerce").ge(float(min_accuracy))
    )
    if require_zero_false_safe:
        x["independent_source_supported_validation_pass"] &= x["false_safe_n"].eq(0)

    x["promotion_eligible"] = (
        x["source_evidence_complete"].astype(bool)
        & x["reference_rule_frozen_bool"].astype(bool)
        & x["machine_executable"].astype(bool)
        & x["salt_scope_supported_bool"].astype(bool)
        & x["automation_allowed_bool"].astype(bool)
        & x["independent_source_supported_validation_pass"].astype(bool)
    )

    def status(r):
        if not bool(r["source_evidence_complete"]):
            return "NOT_PROMOTED_SOURCE_EVIDENCE_INCOMPLETE"
        if not bool(r["reference_rule_frozen_bool"]):
            return "NOT_PROMOTED_REFERENCE_RULE_NOT_FROZEN"
        if not bool(r["machine_executable"]):
            return "NOT_PROMOTED_RULE_NOT_MACHINE_EXECUTABLE"
        if not bool(r["salt_scope_supported_bool"]):
            return "NOT_PROMOTED_SALT_SCOPE_NOT_SUPPORTED"
        if not bool(r["automation_allowed_bool"]):
            return "NOT_PROMOTED_AUTOMATION_STATUS_REVIEW"
        if not bool(r["independent_source_supported_validation_pass"]):
            return "NOT_PROMOTED_INDEPENDENT_VALIDATION_FAILED_OR_INSUFFICIENT"
        return "PROMOTED_SOURCE_VERIFIED_BROAD_SALT_AUTO_ELIGIBLE"

    x["promotion_status"] = x.apply(status, axis=1)
    return x


def build_rule_promotion(validation_by_rule: pd.DataFrame) -> pd.DataFrame:
    """Apply the frozen primary promotion policy."""
    return build_rule_promotion_with_policy(
        validation_by_rule,
        min_cases=MIN_CASES,
        min_positive=MIN_POSITIVE,
        min_negative=MIN_NEGATIVE,
        min_accuracy=MIN_ACCURACY,
        require_zero_false_safe=REQUIRE_ZERO_FALSE_SAFE,
    )


def build_promotion_sensitivity(
    validation_by_rule: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Evaluate secondary threshold scenarios without changing the primary gate.

    Returns
    -------
    scenario_summary
        One row per threshold combination.
    rule_detail
        One row per rule and threshold combination, including both the
        validation-only pass and the final governance-aware promotion result.
    """
    if validation_by_rule is None or not len(validation_by_rule):
        return pd.DataFrame(), pd.DataFrame()

    scenarios = [(*values, True) for values in product(
        SENSITIVITY_MIN_CASES,
        SENSITIVITY_MIN_POSITIVE,
        SENSITIVITY_MIN_ACCURACY,
    )]
    primary = (MIN_CASES, MIN_POSITIVE, MIN_ACCURACY, REQUIRE_ZERO_FALSE_SAFE)
    if primary not in scenarios:
        scenarios.append(primary)

    summaries = []
    details = []
    primary_validation_signature = None
    primary_promotion_signature = None

    evaluated = []
    for min_cases, min_positive, min_accuracy, require_zero_false_safe in scenarios:
        result = build_rule_promotion_with_policy(
            validation_by_rule,
            min_cases=min_cases,
            min_positive=min_positive,
            min_negative=SENSITIVITY_MIN_NEGATIVE,
            min_accuracy=min_accuracy,
            require_zero_false_safe=require_zero_false_safe,
        )
        is_primary = bool(
            min_cases == MIN_CASES
            and min_positive == MIN_POSITIVE
            and SENSITIVITY_MIN_NEGATIVE == MIN_NEGATIVE
            and abs(float(min_accuracy) - float(MIN_ACCURACY)) < 1e-12
            and bool(require_zero_false_safe) == bool(REQUIRE_ZERO_FALSE_SAFE)
        )
        fs_label = "FS0" if require_zero_false_safe else "FS_ALLOWED"
        scenario_id = f"C{min_cases}_P{min_positive}_N{SENSITIVITY_MIN_NEGATIVE}_A{min_accuracy:.2f}_{fs_label}"
        result = result.copy()
        result.insert(0, "scenario_id", scenario_id)
        result.insert(1, "is_prespecified_primary", is_primary)
        result.insert(2, "min_decided_cases", int(min_cases))
        result.insert(3, "min_positive_cases", int(min_positive))
        result.insert(4, "min_negative_cases", int(SENSITIVITY_MIN_NEGATIVE))
        result.insert(5, "min_selective_accuracy", float(min_accuracy))
        result.insert(6, "require_zero_false_safe", bool(require_zero_false_safe))

        ordered = result.sort_values("rule_id")
        validation_signature = "|".join(
            ordered["independent_source_supported_validation_pass"].astype(int).astype(str)
        )
        promotion_signature = "|".join(ordered["promotion_eligible"].astype(int).astype(str))
        evaluated.append((result, validation_signature, promotion_signature))
        if is_primary:
            primary_validation_signature = validation_signature
            primary_promotion_signature = promotion_signature

    for result, validation_signature, promotion_signature in evaluated:
        is_primary = bool(result["is_prespecified_primary"].iloc[0])
        scenario_id = str(result["scenario_id"].iloc[0])
        n_rules = len(result)
        summaries.append({
            "scenario_id": scenario_id,
            "is_prespecified_primary": is_primary,
            "analysis_role": "PRIMARY_PRE_SPECIFIED" if is_primary else "SECONDARY_COUNTERFACTUAL_SENSITIVITY",
            "min_decided_cases": int(result["min_decided_cases"].iloc[0]),
            "min_positive_cases": int(result["min_positive_cases"].iloc[0]),
            "min_negative_cases": int(result["min_negative_cases"].iloc[0]),
            "min_selective_accuracy": float(result["min_selective_accuracy"].iloc[0]),
            "require_zero_false_safe": bool(result["require_zero_false_safe"].iloc[0]),
            "n_rules_evaluated": int(n_rules),
            "n_independent_validation_pass": int(result["independent_source_supported_validation_pass"].sum()),
            "pct_independent_validation_pass": float(result["independent_source_supported_validation_pass"].mean()),
            "n_final_promotion_eligible": int(result["promotion_eligible"].sum()),
            "pct_final_promotion_eligible": float(result["promotion_eligible"].mean()),
            "same_validation_result_as_primary": bool(validation_signature == primary_validation_signature),
            "same_promotion_result_as_primary": bool(promotion_signature == primary_promotion_signature),
        })
        details.append(result)

    summary = pd.DataFrame(summaries).sort_values(
        ["is_prespecified_primary", "min_decided_cases", "min_positive_cases", "min_selective_accuracy"],
        ascending=[False, True, True, True],
    ).reset_index(drop=True)
    detail = pd.concat(details, ignore_index=True)
    return summary, detail


def build_rule_coverage_audit(
    rules: pd.DataFrame,
    cases: pd.DataFrame,
    validation_by_rule: pd.DataFrame,
) -> pd.DataFrame:
    """Audit whether the planned 10--15 rule expansion is actually study-ready."""
    if rules is None or not len(rules):
        return pd.DataFrame()
    summaries = validation_by_rule.set_index("rule_id") if validation_by_rule is not None and len(validation_by_rule) else pd.DataFrame()
    rows = []
    for _, rr in rules.iterrows():
        rid = clean(rr.get("rule_id"))
        g = cases[cases["rule_id"].astype(str).eq(rid)].copy() if cases is not None and len(cases) else pd.DataFrame()
        labels = g.get("reference_membership", pd.Series(dtype=str)).map(parse_membership) if len(g) else pd.Series(dtype=object)
        reviewed = g.get("reference_review_status", pd.Series("", index=g.index)).map(reference_reviewed) if len(g) else pd.Series(dtype=bool)
        source_present = g.get("reference_source", pd.Series("", index=g.index)).map(clean).ne("") if len(g) else pd.Series(dtype=bool)
        curated = labels.notna() & reviewed & source_present if len(g) else pd.Series(dtype=bool)
        n_positive_planned = int(labels.eq(True).sum()) if len(g) else 0
        n_negative_planned = int(labels.eq(False).sum()) if len(g) else 0
        n_curated = int(curated.sum()) if len(g) else 0
        by = summaries.loc[rid] if len(summaries) and rid in summaries.index else None
        rows.append({
            "rule_id": rid,
            "designation_id": clean(rr.get("designation_id")),
            "reference_parent_name": clean(rr.get("reference_parent_name")),
            "study_target": clean(rr.get("study_target")),
            "curation_status": clean(rr.get("curation_status")),
            "n_cases_planned": int(len(g)),
            "n_positive_planned": n_positive_planned,
            "n_negative_planned": n_negative_planned,
            "n_cases_source_reviewed": n_curated,
            "n_cases_pending_review": int(len(g) - n_curated),
            "n_decided": int(by.get("n_decided", 0)) if by is not None else 0,
            "case_count_target_met": bool(len(g) >= TARGET_CASES_PER_RULE),
            "positive_target_met": bool(n_positive_planned >= TARGET_POSITIVE_PER_RULE),
            "negative_target_met": bool(n_negative_planned >= TARGET_NEGATIVE_PER_RULE),
            "source_verified": bool(rr.get("source_verified_bool", False)),
            "reference_rule_frozen": bool(rr.get("reference_rule_frozen_bool", False)),
            "automation_allowed": bool(rr.get("automation_allowed_bool", False)),
            "expansion_ready": bool(
                len(g) >= TARGET_CASES_PER_RULE
                and n_positive_planned >= TARGET_POSITIVE_PER_RULE
                and n_negative_planned >= TARGET_NEGATIVE_PER_RULE
                and n_curated == len(g)
                and bool(rr.get("source_verified_bool", False))
                and bool(rr.get("reference_rule_frozen_bool", False))
                and bool(rr.get("automation_allowed_bool", False))
            ),
        })
    return pd.DataFrame(rows)


def summarize_expansion_coverage(audit: pd.DataFrame) -> pd.DataFrame:
    if audit is None or not len(audit):
        return pd.DataFrame([{
            "target_rule_count": STUDY_TARGET_RULES,
            "loaded_rule_count": 0,
            "target_rule_count_met": False,
            "expansion_ready_rule_count": 0,
            "pending_rule_count": STUDY_TARGET_RULES,
        }])
    ready = audit["expansion_ready"].astype(bool)
    return pd.DataFrame([{
        "target_rule_count": STUDY_TARGET_RULES,
        "loaded_rule_count": int(len(audit)),
        "target_rule_count_met": bool(len(audit) >= STUDY_TARGET_RULES),
        "rules_with_case_plan_complete": int((
            audit["case_count_target_met"].astype(bool)
            & audit["positive_target_met"].astype(bool)
            & audit["negative_target_met"].astype(bool)
        ).sum()),
        "expansion_ready_rule_count": int(ready.sum()),
        "pending_rule_count": int((~ready).sum()),
        "total_validation_rows": int(audit["n_cases_planned"].sum()),
        "pending_case_review_rows": int(audit["n_cases_pending_review"].sum()),
    }])


def build_unpromoted_rule_worklist(promotion: pd.DataFrame, audit: pd.DataFrame) -> pd.DataFrame:
    if promotion is None or not len(promotion):
        return pd.DataFrame()
    cols = [
        "rule_id", "designation_id", "reference_parent_name", "promotion_eligible",
        "promotion_status", "source_evidence_complete", "reference_rule_frozen_bool",
        "machine_executable", "salt_scope_supported_bool", "automation_allowed_bool",
        "n_validation_rows", "n_decided", "n_positive", "n_negative", "selective_accuracy",
    ]
    x = promotion[[c for c in cols if c in promotion.columns]].copy()
    if audit is not None and len(audit):
        keep = [
            "rule_id", "curation_status", "n_cases_planned", "n_cases_source_reviewed",
            "n_cases_pending_review", "case_count_target_met", "positive_target_met",
            "negative_target_met", "expansion_ready",
        ]
        x = x.merge(audit[[c for c in keep if c in audit.columns]], on="rule_id", how="left")
    return x[~x["promotion_eligible"].astype(bool)].reset_index(drop=True)


def _find_promotion_for_ref(row: pd.Series, promotion: pd.DataFrame) -> Optional[pd.Series]:
    if promotion is None or not len(promotion):
        return None
    rid = clean(row.get("rule_id"))
    did = clean(row.get("designation_id"))
    if rid:
        g = promotion[promotion["rule_id"].astype(str).eq(rid)]
        if len(g) == 1:
            return g.iloc[0]
    if did:
        g = promotion[promotion["designation_id"].astype(str).eq(did)]
        if len(g) == 1:
            return g.iloc[0]
    return None


def build_broad_salt_capability(
    semantic_ref: pd.DataFrame,
    scored: pd.DataFrame,
    promotion: pd.DataFrame,
) -> pd.DataFrame:
    """Create rule-level first-paper capability table for broad salts only."""
    if semantic_ref is None or not len(semantic_ref):
        return pd.DataFrame()

    ai_cols = [c for c in ["rule_id", "ai_status", "ai_scope_type"] if c in scored.columns] if scored is not None else []
    ai = scored[ai_cols].drop_duplicates("rule_id") if scored is not None and len(scored) and ai_cols else pd.DataFrame(columns=["rule_id", "ai_status", "ai_scope_type"])
    x = semantic_ref.merge(ai, on="rule_id", how="left")

    rows = []
    for _, r in x.iterrows():
        text = clean(r.get("source_text"))
        ref_scope = clean(r.get("ref_scope_type"))
        is_broad = ref_scope == "BROAD_SALT_SCOPE" or broad_salt_scope(text)
        if not is_broad:
            continue

        pr = _find_promotion_for_ref(r, promotion)
        promoted = bool(pr is not None and pr.get("promotion_eligible", False))
        ai_gate = clean(r.get("ai_status")) == "OK" and clean(r.get("ai_scope_type")) == "BROAD_SALT_SCOPE"
        phrase_gate = broad_salt_scope(text)
        source_gate = bool(pr is not None and pr.get("source_evidence_complete", False))
        frozen_gate = bool(pr is not None and pr.get("reference_rule_frozen_bool", False))
        machine_gate = bool(pr is not None and pr.get("machine_executable", False))
        scope_gate = bool(pr is not None and pr.get("salt_scope_supported_bool", False))
        authorization_gate = bool(pr is not None and pr.get("automation_allowed_bool", False))
        validation_gate = bool(
            pr is not None and pr.get("independent_source_supported_validation_pass", False)
        )
        governance_gate = bool(
            source_gate and frozen_gate and machine_gate and scope_gate and authorization_gate
        )
        auto = bool(promoted and ai_gate and phrase_gate)

        rows.append({
            "rule_id": clean(r.get("rule_id")),
            "designation_id": clean(r.get("designation_id")),
            "substance_name_ko": clean(r.get("substance_name_ko")),
            "ref_scope_type": ref_scope,
            "ai_status": clean(r.get("ai_status")),
            "ai_scope_type": clean(r.get("ai_scope_type")),
            "semantic_llm_broad_salt_gate": ai_gate,
            "deterministic_broad_salt_phrase_gate": phrase_gate,
            "source_evidence_complete_gate": source_gate,
            "reference_rule_frozen_gate": frozen_gate,
            "machine_executable_gate": machine_gate,
            "supported_salt_scope_gate": scope_gate,
            "automation_authorized_gate": authorization_gate,
            "governance_rule_qualified_before_validation": governance_gate,
            "independent_validation_gate": validation_gate,
            "independent_source_supported_validation_promoted": promoted,
            "broad_salt_auto_eligible": auto,
            "capability_status": (
                "SOURCE_VERIFIED_BROAD_SALT_AUTO_ELIGIBLE"
                if auto
                else "BROAD_SALT_REVIEW_OR_AI_ASSISTED_ONLY"
            ),
            "promotion_status": clean(pr.get("promotion_status")) if pr is not None else "NO_SOURCE_VERIFIED_RULE",
            "source_text": text,
        })

    return pd.DataFrame(rows)


def summarize_capability(capability: pd.DataFrame) -> pd.DataFrame:
    if capability is None or not len(capability):
        return pd.DataFrame()
    return pd.DataFrame([{
        "n_broad_salt_rules_in_semantic_universe": int(len(capability)),
        "n_validation_promoted": int(capability["independent_source_supported_validation_promoted"].astype(bool).sum()),
        "n_auto_eligible_after_all_gates": int(capability["broad_salt_auto_eligible"].astype(bool).sum()),
        "n_remaining_nonautomatic": int((~capability["broad_salt_auto_eligible"].astype(bool)).sum()),
    }])


def build_gate_ablation(
    capability: pd.DataFrame,
    validation_detail: Optional[pd.DataFrame] = None,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """Build a four-stage, non-deployable counterfactual gate comparison.

    Stages 1--3 intentionally omit safeguards and are analysis-only. Only stage
    4 corresponds to the deployable primary framework.
    """
    if capability is None or not len(capability):
        return pd.DataFrame(), pd.DataFrame(), pd.DataFrame()

    x = capability.copy()
    required = {
        "semantic_llm_broad_salt_gate": False,
        "deterministic_broad_salt_phrase_gate": False,
        "governance_rule_qualified_before_validation": False,
        "independent_validation_gate": False,
    }
    for col, default in required.items():
        if col not in x.columns:
            x[col] = default
        x[col] = x[col].fillna(False).astype(bool)

    x["stage_1_llm_only_auto"] = x["semantic_llm_broad_salt_gate"]
    x["stage_2_phrase_plus_llm_auto"] = (
        x["stage_1_llm_only_auto"] & x["deterministic_broad_salt_phrase_gate"]
    )
    x["stage_3_source_frozen_rule_auto"] = (
        x["stage_2_phrase_plus_llm_auto"]
        & x["governance_rule_qualified_before_validation"]
    )
    x["stage_4_full_promotion_auto"] = (
        x["stage_3_source_frozen_rule_auto"] & x["independent_validation_gate"]
    )

    stage_specs = [
        ("S1_LLM_ONLY", "stage_1_llm_only_auto", "COUNTERFACTUAL_NOT_DEPLOYABLE"),
        ("S2_PHRASE_PLUS_LLM", "stage_2_phrase_plus_llm_auto", "COUNTERFACTUAL_NOT_DEPLOYABLE"),
        ("S3_SOURCE_FROZEN_RULE", "stage_3_source_frozen_rule_auto", "COUNTERFACTUAL_NOT_DEPLOYABLE"),
        ("S4_FULL_PROMOTION", "stage_4_full_promotion_auto", "PRIMARY_DEPLOYABLE_FRAMEWORK"),
    ]

    rule_summary = []
    validation_summary = []
    n_rules = len(x)
    for stage_id, column, role in stage_specs:
        eligible = x[column].astype(bool)
        rule_summary.append({
            "stage_id": stage_id,
            "analysis_role": role,
            "n_rules_in_broad_salt_semantic_universe": int(n_rules),
            "n_auto_eligible_rules": int(eligible.sum()),
            "pct_auto_eligible_rules": float(eligible.mean()),
            "n_rules_retained_for_review": int((~eligible).sum()),
        })

        if validation_detail is None or not len(validation_detail):
            continue
        active_ids = set(x.loc[eligible, "rule_id"].astype(str))
        exposed = validation_detail[
            validation_detail["rule_id"].astype(str).isin(active_ids)
            & validation_detail["decided"].astype(bool)
        ].copy()
        truth = exposed.get("reference_membership_bool", pd.Series(dtype=bool)).astype(bool)
        pred = exposed.get("predicted_membership_bool", pd.Series(dtype=bool)).astype(bool)
        tp = int((truth & pred).sum()) if len(exposed) else 0
        tn = int((~truth & ~pred).sum()) if len(exposed) else 0
        fp = int((~truth & pred).sum()) if len(exposed) else 0
        fn = int((truth & ~pred).sum()) if len(exposed) else 0
        n_positive = int(truth.sum()) if len(exposed) else 0
        n_negative = int((~truth).sum()) if len(exposed) else 0
        validation_summary.append({
            "stage_id": stage_id,
            "analysis_role": role,
            "n_auto_eligible_rules": int(eligible.sum()),
            "n_source_supported_decided_cases_exposed": int(len(exposed)),
            "n_positive_cases_exposed": n_positive,
            "n_negative_cases_exposed": n_negative,
            "TP": tp,
            "TN": tn,
            "FP_over_screen": fp,
            "FN_false_safe": fn,
            "selective_accuracy": float((tp + tn) / len(exposed)) if len(exposed) else np.nan,
            "observed_false_safe_rate": float(fn / n_positive) if n_positive else np.nan,
            "observed_over_screen_rate": float(fp / n_negative) if n_negative else np.nan,
            "interpretation_limit": (
                "Validation metrics use only source-supported decided cases belonging to rules exposed "
                "at this counterfactual stage; zero observed errors do not establish zero underlying risk."
            ),
        })

    return x, pd.DataFrame(rule_summary), pd.DataFrame(validation_summary)


def upgrade_rule_routing(
    base_routing: pd.DataFrame,
    capability: pd.DataFrame,
    eligibility_column: str = "broad_salt_auto_eligible",
    routing_class: str = "SOURCE_VERIFIED_BROAD_SALT_AUTO_CANDIDATE",
    routing_reason: str = "",
) -> pd.DataFrame:
    """Promote ONLY source-verified broad-salt rules that pass independent validation.

    Everything else retains the original routing class. No other extended-scope
    type is upgraded in the first paper.
    """
    if base_routing is None or not len(base_routing):
        return pd.DataFrame()
    out = base_routing.copy()
    if capability is None or not len(capability):
        out["first_paper_route_changed"] = False
        return out

    if eligibility_column not in capability.columns:
        out["first_paper_route_changed"] = False
        return out
    optional = [c for c in ["promotion_status", "capability_status"] if c in capability.columns]
    cap = capability[[
        "rule_id", "designation_id", eligibility_column, *optional,
    ]].drop_duplicates("rule_id")
    cap = cap.rename(columns={eligibility_column: "broad_salt_auto_eligible"})
    out = out.merge(cap, on=["rule_id", "designation_id"], how="left")
    mask = out["broad_salt_auto_eligible"].eq(True)
    out["base_routing_class"] = out["routing_class"]
    out.loc[mask, "routing_class"] = routing_class
    reason = routing_reason or (
        "Explicit broad-salt scope + LLM broad-salt agreement + source-verified frozen parent rule "
        "+ independent source-supported validation passed."
    )
    out.loc[mask, "routing_reason"] = reason
    out["first_paper_route_changed"] = out["routing_class"].astype(str).ne(out["base_routing_class"].astype(str))
    return out


def summarize_routing(routing: pd.DataFrame) -> pd.DataFrame:
    if routing is None or not len(routing):
        return pd.DataFrame()
    x = routing["routing_class"].value_counts(dropna=False).rename_axis("routing_class").reset_index(name="n_rules")
    x["pct_rules"] = x["n_rules"] / len(routing)
    return pd.concat([x, pd.DataFrame([{"routing_class": "TOTAL", "n_rules": len(routing), "pct_rules": 1.0}])], ignore_index=True)


def compare_routing_before_after(base: pd.DataFrame, upgraded: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if base is None or not len(base) or upgraded is None or not len(upgraded):
        return pd.DataFrame(), pd.DataFrame()
    b = base[["rule_id", "designation_id", "routing_class"]].rename(columns={"routing_class": "base_route"})
    u = upgraded[["rule_id", "designation_id", "routing_class"]].rename(columns={"routing_class": "upgraded_route"})
    x = b.merge(u, on=["rule_id", "designation_id"], how="inner")
    auto_routes = {"DETERMINISTIC_AUTO_CANDIDATE", "SOURCE_VERIFIED_BROAD_SALT_AUTO_CANDIDATE"}
    x["review_before"] = x["base_route"].eq("REVIEW_REQUIRED")
    x["review_after"] = x["upgraded_route"].eq("REVIEW_REQUIRED")
    x["nonautomatic_before"] = ~x["base_route"].isin(auto_routes)
    x["nonautomatic_after"] = ~x["upgraded_route"].isin(auto_routes)
    x["promoted_to_automatic"] = x["nonautomatic_before"] & (~x["nonautomatic_after"])
    summary = pd.DataFrame([{
        "n_rules_compared": len(x),
        "review_required_before": int(x["review_before"].sum()),
        "review_required_after": int(x["review_after"].sum()),
        "review_queue_reduction_n": int(x["review_before"].sum() - x["review_after"].sum()),
        "nonautomatic_before": int(x["nonautomatic_before"].sum()),
        "nonautomatic_after": int(x["nonautomatic_after"].sum()),
        "nonautomatic_burden_reduction_n": int(x["nonautomatic_before"].sum() - x["nonautomatic_after"].sum()),
        "promoted_to_automatic_n": int(x["promoted_to_automatic"].sum()),
    }])
    transitions = x.groupby(["base_route", "upgraded_route"], dropna=False).size().reset_index(name="n_rules")
    return summary, transitions.sort_values("n_rules", ascending=False)


def build_update_routing(update_diff: pd.DataFrame, rule_routing: pd.DataFrame) -> pd.DataFrame:
    """Build changed-designation routing with a rank that knows the new salt-auto class."""
    if update_diff is None or not len(update_diff):
        return pd.DataFrame()
    changed = update_diff[update_diff["update_change_type"].ne("UNCHANGED")].copy()
    if rule_routing is None or not len(rule_routing):
        changed["automation_route"] = "REVIEW_REQUIRED"
        return changed

    rank = {
        "DETERMINISTIC_AUTO_CANDIDATE": 1,
        "SOURCE_VERIFIED_BROAD_SALT_AUTO_CANDIDATE": 1,
        "AI_ASSISTED_PARENT_SCOPE_CANDIDATE": 2,
        "REVIEW_REQUIRED": 3,
    }
    rows = []
    for did, g in rule_routing.groupby("designation_id"):
        routes = g["routing_class"].astype(str).tolist()
        selected = max(routes, key=lambda z: rank.get(z, 9))
        rows.append({
            "designation_id": str(did),
            "automation_route": selected,
            "rule_scope_types": ";".join(sorted(set(g.get("ref_scope_type", pd.Series(dtype=str)).astype(str)))),
        })
    route_df = pd.DataFrame(rows)
    out = changed.copy()
    out["designation_id"] = out["designation_id"].astype(str)
    out = out.merge(route_df, on="designation_id", how="left")
    out["automation_route"] = out["automation_route"].fillna("DETERMINISTIC_AUTO_CANDIDATE")
    return out


def summarize_update_routing(update_routing: pd.DataFrame) -> pd.DataFrame:
    if update_routing is None or not len(update_routing):
        return pd.DataFrame()
    x = update_routing["automation_route"].value_counts().rename_axis("automation_route").reset_index(name="n_changed_designations")
    x["pct_changed_designations"] = x["n_changed_designations"] / len(update_routing)
    return x


def compare_update_routing(base: pd.DataFrame, upgraded: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if base is None or not len(base) or upgraded is None or not len(upgraded):
        return pd.DataFrame(), pd.DataFrame()
    b = base[["designation_id", "automation_route"]].rename(columns={"automation_route": "base_route"})
    u = upgraded[["designation_id", "automation_route"]].rename(columns={"automation_route": "upgraded_route"})
    x = b.merge(u, on="designation_id", how="inner")
    auto_routes = {"DETERMINISTIC_AUTO_CANDIDATE", "SOURCE_VERIFIED_BROAD_SALT_AUTO_CANDIDATE"}
    x["review_before"] = x["base_route"].eq("REVIEW_REQUIRED")
    x["review_after"] = x["upgraded_route"].eq("REVIEW_REQUIRED")
    x["nonautomatic_before"] = ~x["base_route"].isin(auto_routes)
    x["nonautomatic_after"] = ~x["upgraded_route"].isin(auto_routes)
    x["promoted_to_automatic"] = x["nonautomatic_before"] & (~x["nonautomatic_after"])
    summary = pd.DataFrame([{
        "n_changed_designations_compared": len(x),
        "review_required_before": int(x["review_before"].sum()),
        "review_required_after": int(x["review_after"].sum()),
        "review_queue_reduction_n": int(x["review_before"].sum() - x["review_after"].sum()),
        "nonautomatic_before": int(x["nonautomatic_before"].sum()),
        "nonautomatic_after": int(x["nonautomatic_after"].sum()),
        "nonautomatic_burden_reduction_n": int(x["nonautomatic_before"].sum() - x["nonautomatic_after"].sum()),
        "promoted_to_automatic_n": int(x["promoted_to_automatic"].sum()),
    }])
    transitions = x.groupby(["base_route", "upgraded_route"], dropna=False).size().reset_index(name="n_changed_designations")
    return summary, transitions.sort_values("n_changed_designations", ascending=False)


def _rule_lookup_from_promotion(promotion: pd.DataFrame, rules: pd.DataFrame) -> pd.DataFrame:
    if promotion is None or not len(promotion) or rules is None or not len(rules):
        return pd.DataFrame()
    p = promotion[promotion["promotion_eligible"].astype(bool)].copy()
    keep = [
        "rule_id", "designation_id", "reference_smiles", "reference_parent_name",
        "reference_parent_key", "reference_parent_connectivity_key", "isomer_scope",
    ]
    r = rules[[c for c in keep if c in rules.columns]].drop_duplicates("rule_id")
    return p[["rule_id", "designation_id", "promotion_eligible"]].merge(r, on=["rule_id", "designation_id"], how="left")


def evaluate_enterprise_salt_candidates(
    inventory: pd.DataFrame,
    semantic_ref: pd.DataFrame,
    rules: pd.DataFrame,
    promotion: pd.DataFrame,
    notice: str,
) -> pd.DataFrame:
    """Optional enterprise application for promoted broad-salt rules only.

    This is a rule-level screening output. A NO-MATCH for one salt rule does not mean
    that the material is unregulated under every other rule.
    """
    if inventory is None or not len(inventory):
        return pd.DataFrame()
    active = _rule_lookup_from_promotion(promotion, rules)
    if not len(active):
        return pd.DataFrame()

    ref_cols = [
        "rule_id", "designation_id", "substance_name_ko", "source_text",
        "acute_threshold_pct", "chronic_threshold_pct", "eco_threshold_pct",
    ]
    ref = semantic_ref[[c for c in ref_cols if c in semantic_ref.columns]].drop_duplicates("rule_id")
    active = active.merge(ref, on=["rule_id", "designation_id"], how="left")

    rows = []
    for _, inv in inventory.iterrows():
        conc = pd.to_numeric(pd.Series([inv.get("concentration_pct")]), errors="coerce").iloc[0]
        for _, rr in active.iterrows():
            isomer_scope = normalize_isomer_scope(rr.get("isomer_scope"))
            membership, comparison_status, inv_key, ref_key, inv_connectivity, ref_connectivity = (
                _compare_parent_identity(inv.get("smiles"), rr.get("reference_smiles"), isomer_scope)
            )
            if membership is True:
                status = "SOURCE_VERIFIED_SALT_PARENT_MATCH"
            elif membership is False:
                status = "SOURCE_VERIFIED_SALT_PARENT_NO_MATCH"
            else:
                status = comparison_status

            thresholds = []
            for c in ["acute_threshold_pct", "chronic_threshold_pct", "eco_threshold_pct"]:
                v = pd.to_numeric(pd.Series([rr.get(c)]), errors="coerce").iloc[0]
                if np.isfinite(v):
                    thresholds.append(float(v))
            thr = min(thresholds) if thresholds else np.nan

            if membership is True:
                if not np.isfinite(conc):
                    triggered = None
                    applicability = "REVIEW_MISSING_CONCENTRATION"
                elif not np.isfinite(thr):
                    triggered = None
                    applicability = "REVIEW_MISSING_THRESHOLD"
                elif float(conc) >= float(thr):
                    triggered = True
                    applicability = "SALT_MATCH_THRESHOLD_TRIGGER"
                else:
                    triggered = False
                    applicability = "SALT_MATCH_BELOW_THRESHOLD"
            elif membership is False:
                triggered = False
                applicability = "NO_MATCH_TO_THIS_SOURCE_VERIFIED_SALT_RULE"
            else:
                triggered = None
                applicability = status

            rows.append({
                **inv.to_dict(),
                "assessment_notice": notice,
                "rule_id": clean(rr.get("rule_id")),
                "designation_id": clean(rr.get("designation_id")),
                "rule_substance_name": clean(rr.get("substance_name_ko")),
                "reference_parent_name": clean(rr.get("reference_parent_name")),
                "inventory_parent_key": inv_key,
                "inventory_parent_connectivity_key": inv_connectivity,
                "reference_parent_key": ref_key,
                "reference_parent_connectivity_key": ref_connectivity,
                "isomer_scope": isomer_scope,
                "parent_comparison_status": comparison_status,
                "salt_membership": membership,
                "rule_threshold_pct": thr,
                "screening_triggered": triggered,
                "screening_status": applicability,
                "matching_mode": "SOURCE_VERIFIED_BROAD_SALT_PARENT",
            })
    return pd.DataFrame(rows)


def summarize_enterprise_salt_screening(x: pd.DataFrame) -> pd.DataFrame:
    if x is None or not len(x):
        return pd.DataFrame()
    return pd.DataFrame([{
        "n_rule_material_pairs": len(x),
        "n_parent_matches": int(x["salt_membership"].eq(True).sum()),
        "n_parent_nonmatches": int(x["salt_membership"].eq(False).sum()),
        "n_review_missing_structure": int(x["salt_membership"].isna().sum()),
        "n_threshold_triggered": int(x["screening_triggered"].eq(True).sum()),
    }])
