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


LOCANT_RE = re.compile(r"(?<!\d)(\d(?:\s*,\s*\d){0,3})\s*-")
EXCLUSION_RE = re.compile(r"(\d(?:\s*,\s*\d){0,3})\s*-?\s*이성질체[^.]{0,20}제외")


def _clean(value: Any) -> str:
    return "" if pd.isna(value) else str(value).strip()


def _locant_set(text: str):
    """Extract a substitution-position set like {3, 5} from '3,5-톨루엔디아민'
    or '3,5-Diaminotoluene'. Returns None when no such locant is written --
    i.e. the name does not pin down a specific isomer."""
    match = LOCANT_RE.search(_clean(text))
    if not match:
        return None
    try:
        return frozenset(int(part.strip()) for part in match.group(1).split(","))
    except ValueError:
        return None


def _excluded_locants(source_text: str) -> set:
    """Find explicit isomer-exclusion notes in the notice text, e.g.
    '2,6-이성질체는 제외한다'."""
    out = set()
    for match in EXCLUSION_RE.finditer(_clean(source_text)):
        try:
            out.add(frozenset(int(part.strip()) for part in match.group(1).split(",")))
        except ValueError:
            continue
    return out


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
            "rule_locant": _locant_set(korean) or _locant_set(english),
            "excluded_locants": _excluded_locants(source),
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
        item_locant = _locant_set(text)
        for _, rule in index.iterrows():
            rule_locant = rule.get("rule_locant")
            excluded_locants = rule.get("excluded_locants") or set()
            if item_locant and item_locant in excluded_locants:
                # The notice itself names this exact substitution position as
                # excluded (e.g. '2,6-이성질체는 제외한다') -- this settles the
                # question; it is not a fuzzy "may relate" candidate.
                out.append({
                    "material_id": material_id, "chemical_name": _clean(item.get("chemical_name")), "cas": cas,
                    "designation_id": rule.get("designation_id"), "notice": rule.get("notice"),
                    "regulatory_name": rule.get("regulatory_name"), "scope_type": _clean(rule.get("scope_type")),
                    "regulatory_categories": "; ".join(
                        label
                        for column, label in HAZARD_CATEGORY_LABELS.items()
                        if _clean(rule.get(column))
                    ),
                    "candidate_match_type": "EXPLICIT_LOCANT_EXCLUSION",
                    "candidate_reason": (
                        "물질명에 표시된 치환 위치가 고시 원문에 명시된 제외 이성질체와 일치하여, "
                        "이 지정규정의 적용 대상에서 제외되는 것으로 보입니다."
                    ),
                    "is_explicit_exception": True,
                    "source_text": rule.get("source_text"),
                    "candidate_status": "확인 범위 내 비해당(명시적 제외 후보)",
                    "match_score": 1.0,
                })
                continue
            if item_locant and rule_locant and item_locant != rule_locant:
                # Both sides name a specific substitution position and they
                # disagree -- this is a different isomer, not a candidate for
                # the same designation (this is what previously let e.g. a
                # 3,5-isomer's result show a 2,5-isomer's name alongside it).
                continue
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
                score = 1.0
            elif component_hit and scope in {"REACTION_PRODUCT", "MIXTURE"}:
                reason = "법규 원문 속 구성성분 CAS와 일치하나, 단일 성분과 규제 혼합물·반응생성물은 동일하지 않을 수 있습니다."
                match = "COMPONENT_CAS_CANDIDATE"
                score = 0.85
            elif name_hit:
                reason = "회사 물질명과 CAS 없는 총칭·염류·UVCB 규제명이 관련되어 제품 동일성 확인이 필요합니다."
                match = "NAME_SCOPE_CANDIDATE"
                score = min(1.0, overlap + (0.05 if family_hit else 0.0))
            else:
                reason = "CAS 없는 규제범위의 구성성분 후보입니다."
                match = "SCOPE_CANDIDATE"
                score = 0.3
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
                "match_score": score,
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
    out["no_cas_scope_primary_designation_id"] = ""
    out["no_cas_scope_primary_regulatory_name"] = ""
    out["no_cas_scope_primary_category"] = ""
    out["no_cas_scope_excluded_designation_id"] = ""
    out["no_cas_scope_excluded_regulatory_name"] = ""
    if candidates is None or candidates.empty:
        return out
    candidates = candidates.copy()
    if "is_explicit_exception" not in candidates:
        candidates["is_explicit_exception"] = False
    if "match_score" not in candidates:
        candidates["match_score"] = 0.0
    candidates["is_explicit_exception"] = candidates["is_explicit_exception"].fillna(False).astype(bool)
    candidates["match_score"] = pd.to_numeric(candidates["match_score"], errors="coerce").fillna(0.0)

    def _regulatory_categories(rows: pd.DataFrame) -> str:
        return "; ".join(
            dict.fromkeys(
                category.strip()
                for value in rows.get("regulatory_categories", pd.Series(dtype=str))
                for category in _clean(value).split(";")
                if category.strip()
            )
        )

    for material_id, rows in candidates.groupby("material_id", sort=False):
        mask = out["material_id"].astype(str).eq(str(material_id))
        if not mask.any():
            continue

        excluded_rows = rows[rows["is_explicit_exception"]]
        other_rows = rows[~rows["is_explicit_exception"]]

        # Full context (including exclusion candidates) is kept for the audit
        # trail regardless of which branch below applies.
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
        out.loc[mask, "no_cas_scope_candidate_context"] = json.dumps(context, ensure_ascii=False)

        if other_rows.empty and not excluded_rows.empty:
            # Every candidate for this material is an explicit exclusion --
            # the exclusion condition settles the question rather than
            # leaving it as a "may apply" hold for a human to re-check.
            top = excluded_rows.sort_values("match_score", ascending=False).iloc[0]
            # Deliberately leave no_cas_scope_candidate_count at 0: this
            # material is resolved (excluded), not an open interpretive case
            # to route to AI review.
            out.loc[mask, "no_cas_scope_excluded_designation_id"] = _clean(top.get("designation_id"))
            out.loc[mask, "no_cas_scope_excluded_regulatory_name"] = _clean(top.get("regulatory_name"))
            confirmed = out.loc[mask, "gate_decision"].astype(str).str.contains("자동 확정")
            change_mask = mask.copy()
            change_mask.loc[mask] = ~confirmed.values
            out.loc[change_mask, "final_decision"] = "규제 비해당(명시적 제외)"
            out.loc[change_mask, "overall_decision"] = "확인 범위 내 비해당"
            out.loc[change_mask, "gate_status"] = "GATE_PASS"
            out.loc[change_mask, "gate_decision"] = "확인 범위 내 비해당"
            out.loc[change_mask, "gate_hold_reason"] = ""
            continue

        rows_for_summary = other_rows if not other_rows.empty else excluded_rows
        labels = "; ".join(f"{r.designation_id} {r.regulatory_name}" for r in rows_for_summary.itertuples())
        designation_ids = "; ".join(
            dict.fromkeys(_clean(v) for v in rows_for_summary["designation_id"] if _clean(v))
        )
        regulatory_names = "; ".join(
            dict.fromkeys(_clean(v) for v in rows_for_summary["regulatory_name"] if _clean(v))
        )
        regulatory_categories = _regulatory_categories(rows_for_summary)
        top = rows_for_summary.sort_values("match_score", ascending=False).iloc[0]
        sources = "\n---\n".join(dict.fromkeys(_clean(v) for v in rows_for_summary["source_text"] if _clean(v)))

        out.loc[mask, "no_cas_scope_candidate_count"] = len(rows_for_summary)
        out.loc[mask, "no_cas_scope_candidates"] = labels
        out.loc[mask, "no_cas_scope_designation_ids"] = designation_ids
        out.loc[mask, "no_cas_scope_regulatory_names"] = regulatory_names
        out.loc[mask, "no_cas_scope_regulatory_categories"] = regulatory_categories
        out.loc[mask, "no_cas_scope_primary_designation_id"] = _clean(top.get("designation_id"))
        out.loc[mask, "no_cas_scope_primary_regulatory_name"] = _clean(top.get("regulatory_name"))
        out.loc[mask, "no_cas_scope_primary_category"] = _regulatory_categories(top.to_frame().T)
        existing = out.loc[mask, "review_regulatory_text"].fillna("") if "review_regulatory_text" in out else pd.Series("", index=out.index[mask])
        out.loc[mask, "review_regulatory_text"] = existing.map(lambda v: "\n---\n".join(x for x in [str(v).strip(), sources] if x))
        confirmed = out.loc[mask, "gate_decision"].astype(str).str.contains("자동 확정")
        change_mask = mask.copy()
        change_mask.loc[mask] = ~confirmed.values
        out.loc[change_mask, "final_decision"] = "전문가 검토 필요"
        out.loc[change_mask, "overall_decision"] = "추가 확인 필요"
        out.loc[change_mask, "gate_status"] = "GATE_HOLD"
        out.loc[change_mask, "gate_decision"] = "보류(CAS 없는 규제범위 후보)"
        out.loc[change_mask, "gate_hold_reason"] = rows_for_summary.iloc[0]["candidate_reason"]
    return out
