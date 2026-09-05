from __future__ import annotations

import json
import re
from typing import Any

import pandas as pd

CAS_RE = re.compile(r"(?<!\d)(\d{2,7}-\d{2}-\d)(?!\d)")
STOP = {
    "and", "with", "the", "its", "of", "reaction", "product", "products", "mixture", "salt", "salts",
    "반응생성물", "반응혼합물", "혼합물", "염류", "화합물",
}
CHEMICAL_TOKEN_ALIASES = {
    "chloric": "chlorate", "chlorates": "chlorate",
    "nitrous": "nitrite", "nitrites": "nitrite",
    "chromic": "chromate", "chromates": "chromate", "dichromate": "chromate", "dichromates": "chromate",
    "hexafluorosilicic": "hexafluorosilicate", "hexafluorosilicates": "hexafluorosilicate",
    "tetrafluoroboric": "tetrafluoroborate", "tetrafluoroborates": "tetrafluoroborate",
}
DISTINCTIVE_FAMILY_TOKENS = {
    "antimony", "cadmium", "chromate", "hexafluorosilicate", "lead", "mercury",
    "nitrite", "silver", "tin", "tetrafluoroborate", "zinc", "chlorate",
}

HAZARD_CATEGORY_LABELS = {
    "threshold_acute": "인체급성유해성물질",
    "threshold_chronic": "인체만성유해성물질",
    "threshold_eco": "생태유해성물질",
}


def _clean(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _tokens(value: Any) -> set[str]:
    words = re.findall(r"[a-z0-9가-힣]{3,}", _clean(value).lower())
    tokens = {CHEMICAL_TOKEN_ALIASES.get(w, w) for w in words if w not in STOP}
    # 붙여 쓴 체계명(예: ethyltrimethyllead)에서도 규제 화합물군 핵심어를 보존한다.
    for word in words:
        for family in DISTINCTIVE_FAMILY_TOKENS:
            if word.endswith(family):
                tokens.add(family)
    return tokens


def build_no_cas_scope_index(master: pd.DataFrame, notice: str) -> pd.DataFrame:
    """Create a deterministic index for approved rows lacking a direct CAS."""
    if master is None or master.empty:
        return pd.DataFrame()
    x = master[master["notice"].astype(str).eq(str(notice))].copy()
    direct = x.get("direct_cas", pd.Series("", index=x.index)).map(_clean)
    x = x[direct.eq("") | direct.eq("-")]
    rows = []
    for _, r in x.iterrows():
        source = _clean(r.get("source_text"))
        english = _clean(r.get("substance_name_en"))
        korean = _clean(r.get("substance_name_ko"))
        embedded = sorted(set(CAS_RE.findall(" ".join([source, english, _clean(r.get("all_cas_in_row"))]))))
        lower = f"{korean} {english}".lower()
        if any(k in lower for k in ("reaction product", "reaction mixture", "반응생성물", "반응혼합물")):
            scope = "REACTION_PRODUCT"
        elif "mixture" in lower or "혼합물" in lower:
            scope = "MIXTURE"
        elif bool(r.get("include_salts")) or "그 염류" in korean or "its salt" in lower:
            scope = "SALT_FAMILY"
        else:
            scope = "NAME_OR_UVCB"
        rows.append({
            "designation_id": _clean(r.get("designation_id")), "notice": str(notice),
            "regulatory_name": korean, "regulatory_name_en": english,
            "scope_type": scope, "embedded_component_cas": ";".join(embedded),
            "threshold_acute": r.get("acute_threshold_raw"),
            "threshold_chronic": r.get("chronic_threshold_raw"),
            "threshold_eco": r.get("eco_threshold_raw"),
            "source_text": source, "authoritative_source": bool(r.get("authoritative_source")),
            "exception_cas": ";".join(sorted(set(CAS_RE.findall(_clean(r.get("exception_cas")))))),
            "name_tokens": _tokens(f"{korean} {english}"),
        })
    return pd.DataFrame(rows)


def screen_no_cas_scopes(inventory: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    """Return conservative candidates; never returns an automatic legal match."""
    if inventory is None or inventory.empty or index is None or index.empty:
        return pd.DataFrame()
    out = []
    for _, item in inventory.iterrows():
        material_id = _clean(item.get("material_id"))
        cas = _clean(item.get("cas"))
        text = " ".join(_clean(item.get(c)) for c in ("chemical_name", "product_name", "use_description"))
        item_tokens = _tokens(text)
        for _, rule in index.iterrows():
            components = {v for v in _clean(rule.get("embedded_component_cas")).split(";") if v}
            exceptions = {v for v in _clean(rule.get("exception_cas")).split(";") if v}
            exception_hit = bool(cas and cas in exceptions)
            component_hit = bool(cas and cas in components)
            rule_tokens = rule.get("name_tokens") if isinstance(rule.get("name_tokens"), set) else set()
            exact = item_tokens & rule_tokens
            fuzzy_prefixes = {
                a[:8] for a in item_tokens for b in rule_tokens
                if len(a) >= 8 and len(b) >= 8 and a[:8] == b[:8]
            }
            matched_token_count = len(exact) + len(fuzzy_prefixes - {a[:8] for a in exact if len(a) >= 8})
            overlap = matched_token_count / max(1, min(len(item_tokens), len(rule_tokens)))
            scope = _clean(rule.get("scope_type"))
            family_hit = bool(exact & DISTINCTIVE_FAMILY_TOKENS)
            name_hit = (overlap >= 0.45 and (scope == "SALT_FAMILY" or matched_token_count >= 2)) or family_hit
            if not (exception_hit or component_hit or name_hit):
                continue
            if exception_hit:
                reason = "법규 범위의 명시적 제외 CAS 후보입니다. 포함으로 확정하지 말고 원문의 제외조건을 우선 확인해야 합니다."
                match = "EXPLICIT_EXCEPTION_CAS"
            elif component_hit and scope in {"REACTION_PRODUCT", "MIXTURE"}:
                reason = "법규 원문 속 구성성분 CAS와 일치하나, 단일 성분과 규제 혼합물·반응생성물은 동일하지 않을 수 있습니다."
                match = "COMPONENT_CAS_CANDIDATE"
            elif name_hit:
                reason = "회사 물질명과 CAS 없는 총칭·염류·UVCB 규제명이 관련되어 제품 동일성 확인이 필요합니다."
                match = "NAME_SCOPE_CANDIDATE"
            else:
                reason = "CAS 없는 규제범위의 구성성분 후보입니다."
                match = "SCOPE_CANDIDATE"
            out.append({
                "material_id": material_id, "chemical_name": _clean(item.get("chemical_name")), "cas": cas,
                "designation_id": rule.get("designation_id"), "notice": rule.get("notice"),
                "regulatory_name": rule.get("regulatory_name"), "scope_type": scope,
                "regulatory_categories": "; ".join(
                    label
                    for column, label in HAZARD_CATEGORY_LABELS.items()
                    if _clean(rule.get(column))
                ),
                "candidate_match_type": match, "candidate_reason": reason,
                "is_explicit_exception": exception_hit,
                "source_text": rule.get("source_text"),
                "candidate_status": "전문가 검토 필요(AI 자동확정 금지)",
            })
    return pd.DataFrame(out).drop_duplicates(subset=["material_id", "designation_id", "candidate_match_type"])


def attach_no_cas_candidates(summary: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    out = summary.copy()
    out["no_cas_scope_candidate_count"] = 0
    out["no_cas_scope_candidates"] = ""
    out["no_cas_scope_designation_ids"] = ""
    out["no_cas_scope_regulatory_names"] = ""
    out["no_cas_scope_regulatory_categories"] = ""
    out["no_cas_scope_candidate_context"] = "[]"
    if candidates is None or candidates.empty:
        return out
    for material_id, rows in candidates.groupby("material_id", sort=False):
        mask = out["material_id"].astype(str).eq(str(material_id))
        if not mask.any():
            continue
        labels = "; ".join(f"{r.designation_id} {r.regulatory_name}" for r in rows.itertuples())
        designation_ids = "; ".join(
            dict.fromkeys(_clean(v) for v in rows["designation_id"] if _clean(v))
        )
        regulatory_names = "; ".join(
            dict.fromkeys(_clean(v) for v in rows["regulatory_name"] if _clean(v))
        )
        regulatory_categories = "; ".join(
            dict.fromkeys(
                category.strip()
                for value in rows.get("regulatory_categories", pd.Series(dtype=str))
                for category in _clean(value).split(";")
                if category.strip()
            )
        )
        context = []
        for r in rows.itertuples():
            context.append({
                "designation_id": _clean(getattr(r, "designation_id", "")),
                "regulatory_name": _clean(getattr(r, "regulatory_name", "")),
                "scope_type": _clean(getattr(r, "scope_type", "")),
                "match_type": _clean(getattr(r, "candidate_match_type", "")),
                "explicit_exception": bool(getattr(r, "is_explicit_exception", False)),
                "reason": _clean(getattr(r, "candidate_reason", "")),
            })
        sources = "\n---\n".join(dict.fromkeys(_clean(v) for v in rows["source_text"] if _clean(v)))
        out.loc[mask, "no_cas_scope_candidate_count"] = len(rows)
        out.loc[mask, "no_cas_scope_candidates"] = labels
        out.loc[mask, "no_cas_scope_designation_ids"] = designation_ids
        out.loc[mask, "no_cas_scope_regulatory_names"] = regulatory_names
        out.loc[mask, "no_cas_scope_regulatory_categories"] = regulatory_categories
        out.loc[mask, "no_cas_scope_candidate_context"] = json.dumps(
            context, ensure_ascii=False
        )
        existing = out.loc[mask, "review_regulatory_text"].fillna("") if "review_regulatory_text" in out else pd.Series("", index=out.index[mask])
        out.loc[mask, "review_regulatory_text"] = existing.map(lambda v: "\n---\n".join(x for x in [str(v).strip(), sources] if x))
        confirmed = out.loc[mask, "gate_decision"].astype(str).str.contains("자동 확정")
        change_mask = mask.copy()
        change_mask.loc[mask] = ~confirmed.values
        out.loc[change_mask, "final_decision"] = "전문가 검토 필요"
        out.loc[change_mask, "overall_decision"] = "추가 확인 필요"
        out.loc[change_mask, "gate_status"] = "GATE_HOLD"
        out.loc[change_mask, "gate_decision"] = "보류(CAS 없는 규제범위 후보)"
        out.loc[change_mask, "gate_hold_reason"] = rows.iloc[0]["candidate_reason"]
    return out
