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
    for word in words:
        for family in DISTINCTIVE_FAMILY_TOKENS:
            if word.endswith(family):
                tokens.add(family)
    return tokens


def _extract_exception_cas(*texts: Any) -> set[str]:
    """Extract CAS numbers that are explicitly inside an exclusion clause.

    The approved master does not always populate exception_cas separately.
    Many rows instead carry the exclusion only in English source/name text,
    e.g. "Toluenediamines(excluding 2,6-toluenediamine(823-40-5)...)".
    We therefore parse only text following an explicit exclusion marker, never
    arbitrary CAS numbers elsewhere in the row.
    """
    found: set[str] = set()
    for raw in texts:
        text = _clean(raw)
        if not text:
            continue
        lower = text.lower()
        start = 0
        while True:
            idx = lower.find("excluding", start)
            if idx < 0:
                break
            end_candidates = [
                p for p in [
                    lower.find("with the exception", idx + 9),
                    lower.find("except as", idx + 9),
                    lower.find("provided that", idx + 9),
                ] if p >= 0
            ]
            end = min(end_candidates) if end_candidates else min(len(text), idx + 1200)
            found.update(CAS_RE.findall(text[idx:end]))
            start = idx + 9
        for m in re.finditer(r"제외", text):
            left = max(0, m.start() - 800)
            clause = text[left:m.end()]
            boundary = max(clause.rfind("("), clause.rfind("["), clause.rfind(";"), clause.rfind("|"))
            if boundary >= 0:
                clause = clause[boundary + 1:]
            found.update(CAS_RE.findall(clause))
    return found


def build_no_cas_scope_index(master: pd.DataFrame, notice: str) -> pd.DataFrame:
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
        explicit_exception_cas = set(CAS_RE.findall(_clean(r.get("exception_cas"))))
        explicit_exception_cas.update(_extract_exception_cas(source, english, korean))
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
            "exception_cas": ";".join(sorted(explicit_exception_cas)),
            "name_tokens": _tokens(f"{korean} {english}"),
        })
    return pd.DataFrame(rows)


def screen_no_cas_scopes(inventory: pd.DataFrame, index: pd.DataFrame) -> pd.DataFrame:
    """Return conservative candidates, with legal/exact evidence ranked first."""
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
                reason = "법규의 명시적 제외조건과 CAS번호가 일치합니다. 제외조건을 포함 후보보다 우선 적용해야 합니다."
                match = "EXPLICIT_EXCEPTION_CAS"
                score = 1000.0
            elif component_hit and scope in {"REACTION_PRODUCT", "MIXTURE"}:
                reason = "법규 원문 속 구성성분 CAS와 일치하나 규제 혼합물·반응생성물 자체인지 확인이 필요합니다."
                match = "COMPONENT_CAS_CANDIDATE"
                score = 800.0
            elif component_hit:
                reason = "법규 원문에 이 CAS번호가 직접 기재된 포괄 범위 후보입니다. 직접 CAS 지정과 구분하여 전문가 확인이 필요합니다."
                match = "EXACT_SCOPE_CAS_CANDIDATE"
                score = 700.0
            elif name_hit:
                reason = "회사 물질명과 포괄 규제범위의 규제명이 관련되어 동일성 확인이 필요합니다."
                match = "NAME_SCOPE_CANDIDATE"
                score = float(overlap * 100.0 + matched_token_count)
            else:
                reason = "포괄 규제범위의 구성성분 후보입니다."
                match = "SCOPE_CANDIDATE"
                score = float(overlap * 100.0)

            out.append({
                "material_id": material_id, "chemical_name": _clean(item.get("chemical_name")), "cas": cas,
                "designation_id": rule.get("designation_id"), "notice": rule.get("notice"),
                "regulatory_name": rule.get("regulatory_name"), "scope_type": scope,
                "regulatory_categories": "; ".join(
                    label for column, label in HAZARD_CATEGORY_LABELS.items() if _clean(rule.get(column))
                ),
                "candidate_match_type": match, "candidate_reason": reason,
                "is_explicit_exception": exception_hit,
                "candidate_score": score,
                "source_text": rule.get("source_text"),
                "candidate_status": "전문가 검토 필요(AI 자동확정 금지)",
            })
    if not out:
        return pd.DataFrame()
    return pd.DataFrame(out).drop_duplicates(subset=["material_id", "designation_id", "regulatory_name", "candidate_match_type"])


def _candidate_sort_key(row: pd.Series) -> tuple:
    explicit_rank = 0 if bool(row.get("is_explicit_exception")) else 1
    match_rank = {
        "EXPLICIT_EXCEPTION_CAS": 0,
        "COMPONENT_CAS_CANDIDATE": 1,
        "EXACT_SCOPE_CAS_CANDIDATE": 1,
        "NAME_SCOPE_CANDIDATE": 2,
        "SCOPE_CANDIDATE": 3,
    }.get(_clean(row.get("candidate_match_type")), 9)
    score = pd.to_numeric(pd.Series([row.get("candidate_score")]), errors="coerce").iloc[0]
    score = float(score) if pd.notna(score) else 0.0
    return (
        explicit_rank,
        match_rank,
        -score,
        _clean(row.get("designation_id")),
        _clean(row.get("regulatory_name")),
    )


def attach_no_cas_candidates(summary: pd.DataFrame, candidates: pd.DataFrame) -> pd.DataFrame:
    out = summary.copy()
    out["no_cas_scope_candidate_count"] = 0
    out["no_cas_scope_candidates"] = ""
    out["no_cas_scope_designation_ids"] = ""
    out["no_cas_scope_regulatory_names"] = ""
    out["no_cas_scope_regulatory_categories"] = ""
    out["no_cas_scope_candidate_context"] = "[]"
    out["no_cas_scope_primary_designation_id"] = ""
    out["no_cas_scope_primary_regulatory_name"] = ""
    out["no_cas_scope_primary_category"] = ""
    out["no_cas_scope_primary_match_type"] = ""
    out["no_cas_scope_primary_explicit_exception"] = False

    if candidates is None or candidates.empty:
        return out

    for material_id, rows in candidates.groupby("material_id", sort=False):
        mask = out["material_id"].astype(str).eq(str(material_id))
        if not mask.any():
            continue

        ranked = rows.copy()
        ranked["_sort_key"] = ranked.apply(_candidate_sort_key, axis=1)
        ranked = ranked.sort_values("_sort_key", kind="stable")
        primary = ranked.iloc[0]

        labels = "; ".join(
            dict.fromkeys(f"{_clean(r.designation_id)} {_clean(r.regulatory_name)}".strip() for r in ranked.itertuples())
        )
        designation_ids = "; ".join(dict.fromkeys(_clean(v) for v in ranked["designation_id"] if _clean(v)))
        regulatory_names = "; ".join(dict.fromkeys(_clean(v) for v in ranked["regulatory_name"] if _clean(v)))
        regulatory_categories = "; ".join(
            dict.fromkeys(
                category.strip()
                for value in ranked.get("regulatory_categories", pd.Series(dtype=str))
                for category in _clean(value).split(";")
                if category.strip()
            )
        )

        context = []
        for r in ranked.itertuples():
            context.append({
                "designation_id": _clean(getattr(r, "designation_id", "")),
                "regulatory_name": _clean(getattr(r, "regulatory_name", "")),
                "regulatory_categories": _clean(getattr(r, "regulatory_categories", "")),
                "scope_type": _clean(getattr(r, "scope_type", "")),
                "match_type": _clean(getattr(r, "candidate_match_type", "")),
                "explicit_exception": bool(getattr(r, "is_explicit_exception", False)),
                "candidate_score": getattr(r, "candidate_score", 0.0),
                "reason": _clean(getattr(r, "candidate_reason", "")),
            })

        sources = "\n---\n".join(dict.fromkeys(_clean(v) for v in ranked["source_text"] if _clean(v)))
        out.loc[mask, "no_cas_scope_candidate_count"] = len(ranked)
        out.loc[mask, "no_cas_scope_candidates"] = labels
        out.loc[mask, "no_cas_scope_designation_ids"] = designation_ids
        out.loc[mask, "no_cas_scope_regulatory_names"] = regulatory_names
        out.loc[mask, "no_cas_scope_regulatory_categories"] = regulatory_categories
        out.loc[mask, "no_cas_scope_candidate_context"] = json.dumps(context, ensure_ascii=False)
        out.loc[mask, "no_cas_scope_primary_designation_id"] = _clean(primary.get("designation_id"))
        out.loc[mask, "no_cas_scope_primary_regulatory_name"] = _clean(primary.get("regulatory_name"))
        out.loc[mask, "no_cas_scope_primary_category"] = _clean(primary.get("regulatory_categories"))
        out.loc[mask, "no_cas_scope_primary_match_type"] = _clean(primary.get("candidate_match_type"))
        out.loc[mask, "no_cas_scope_primary_explicit_exception"] = bool(primary.get("is_explicit_exception"))

        existing = (
            out.loc[mask, "review_regulatory_text"].fillna("")
            if "review_regulatory_text" in out else pd.Series("", index=out.index[mask])
        )
        out.loc[mask, "review_regulatory_text"] = existing.map(
            lambda v: "\n---\n".join(x for x in [str(v).strip(), sources] if x)
        )

        confirmed = out.loc[mask, "gate_decision"].astype(str).str.contains("자동 확정")
        change_mask = mask.copy()
        change_mask.loc[mask] = ~confirmed.values
        out.loc[change_mask, "final_decision"] = "전문가 검토 필요"
        out.loc[change_mask, "overall_decision"] = "추가 확인 필요"
        out.loc[change_mask, "gate_status"] = "GATE_HOLD"
        out.loc[change_mask, "gate_decision"] = "보류(포괄 규제범위 후보)"
        out.loc[change_mask, "gate_hold_reason"] = _clean(primary.get("candidate_reason"))

    return out
