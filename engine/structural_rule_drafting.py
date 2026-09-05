"""Draft structural-range candidate rules for compound-group text that has no
CAS number at all (e.g. "수산화 트리아릴주석 및 그 염류... 다만, 본 고시에서
별도로 규정한 것은 제외").

This is the missing half of the appendix pipeline: appendix_sync.py already
captures these clauses as text (a catch-all row with no CAS), but nothing
turns that text into something screen_company_inventory can actually test a
company's SMILES against. This module asks the local LLM to propose a SMARTS
pattern for the structural family plus any named exclusions, then validates
that proposal mechanically before it is ever shown to a person.

Design rule, unchanged from the rest of this project: a draft here is never
more trustworthy than an appendix-extraction candidate. It is always emitted
with approval_status="CANDIDATE_REVIEW_REQUIRED" and can only reach
extended_scope_rules_APPROVED.csv after a chemist confirms the pattern is
correct. An LLM-guessed chemical structure is, if anything, MORE dangerous to
auto-trust than a mis-scanned PDF table -- a wrong SMARTS either silently
misses a regulated substance or wrongly flags a compliant one, and neither
failure is visible without someone who can read SMARTS checking it.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

import pandas as pd
import requests

from local_llm import MAX_SOURCE_CHARS, _base_url, _is_loopback_url, _model_name
from scope_engine import clean


# This exact phrase is a fixed piece of legal drafting, not something that
# needs the LLM's judgment: it means "if this substance already has its own
# separate listing elsewhere in this notice, that specific listing applies
# instead of this catch-all group." Detecting it with a rule-based regex is
# far more reliable than asking a language model to notice it every time.
CROSS_NOTICE_EXCLUSION_RE = re.compile(r"본\s*고시에서\s*별도로\s*규정한\s*것은\s*제외")

EXTENDED_SCOPE_COLUMNS = [
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

DRAFT_AUDIT_COLUMNS = [
    "case_id",
    "designation_id",
    "regime",
    "source_text",
    "llm_scope_type",
    "llm_required_smarts",
    "llm_excluded_smarts",
    "llm_excluded_cas",
    "llm_cross_notice_exclusion",
    "llm_explanation_ko",
    "llm_confidence",
    "llm_evidence_quotes",
    "llm_smarts_valid",
    "llm_status",
    "approval_status",
]


def _response_schema() -> dict:
    return {
        "type": "object",
        "properties": {
            "scope_type": {
                "type": "string",
                "enum": [
                    "SALT_FAMILY",
                    "ELEMENT_COMPOUND_CLASS",
                    "STRUCTURAL_RANGE",
                    "NAMED_MIXTURE",
                    "DERIVATIVE_CLASS",
                    "UNCLEAR",
                ],
            },
            "required_smarts": {"type": "string"},
            "excluded_smarts": {"type": "string"},
            "excluded_cas": {"type": "string"},
            "explanation_ko": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "evidence_quotes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["scope_type", "explanation_ko", "confidence"],
    }


def _build_prompt(source_text: str) -> str:
    return (
        "다음은 한국 화학물질 규제 고시의 총칭(화합물군) 조항입니다. CAS 번호가 "
        "없는 화합물군·구조범위 조항을 화학구조 패턴(SMARTS)으로 표현해 주십시오.\n\n"
        f"조항 원문:\n{source_text[:MAX_SOURCE_CHARS]}\n\n"
        "다음을 JSON으로만 답하십시오. 마크다운이나 다른 텍스트를 절대 포함하지 마십시오.\n"
        "- scope_type: 이 조항의 성격(염류군/원소화합물군/구조범위/특정혼합물/유도체군/불명확)\n"
        "- required_smarts: 이 화합물군을 포괄하는 SMARTS 패턴. 확신이 없으면 빈 문자열.\n"
        "- excluded_smarts: 조항에 명시된 제외 화합물의 SMARTS. 없으면 빈 문자열.\n"
        "- excluded_cas: 조항에 명시된 제외 화합물 중 CAS로 아는 것이 있으면 콤마로 구분. 모르면 빈 문자열.\n"
        "- explanation_ko: 이 패턴이 왜 이 조항을 대표하는지 한국어로 간단히 설명.\n"
        "- confidence: 0~1 사이 확신도. 원문에 없는 정보를 추측했다면 낮게.\n"
        "- evidence_quotes: 판단의 근거가 된 원문 구절을 그대로(변형 없이) 배열로.\n"
    )


def _post_draft_chat(source_text: str, timeout: int = 60) -> dict:
    base_url = _base_url()
    if not _is_loopback_url(base_url):
        raise RuntimeError("로컬(localhost) Ollama 주소가 아니라 요청을 거부했습니다.")
    response = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": _model_name(),
            "messages": [{"role": "user", "content": _build_prompt(source_text)}],
            "format": _response_schema(),
            "stream": False,
            "options": {"temperature": 0, "seed": 42},
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload.get("message", {}).get("content", "")
    return json.loads(content)


def _smarts_is_valid(pattern: str) -> bool:
    """RDKit is the ground truth here: if it can't parse the pattern, the
    pattern is unusable regardless of what the model intended."""
    pattern = clean(pattern)
    if not pattern:
        return True  # an intentionally empty pattern is not an error
    try:
        from rdkit import Chem  # imported lazily; only needed when validating

        return Chem.MolFromSmarts(pattern) is not None
    except Exception:
        return False


def _quote_is_verbatim(quote: str, source_text: str) -> bool:
    quote = clean(quote)
    if len(quote) < 4:
        return False
    return quote in source_text


def draft_structural_rule(case: dict) -> dict:
    """Draft one structural-range candidate from a single compound-group case.

    ``case`` needs at minimum ``case_id`` and ``source_text`` (and ideally
    ``designation_id``/``regime``/``substance_name`` for citation). Returns a
    dict shaped for a DRAFT_AUDIT_COLUMNS row -- never for direct use, always
    for human review.
    """
    source_text = clean(case.get("source_text"))
    audit: dict[str, Any] = {
        "case_id": clean(case.get("case_id")),
        "designation_id": clean(case.get("designation_id")),
        "regime": clean(case.get("regime")),
        "source_text": source_text,
        "llm_scope_type": "",
        "llm_required_smarts": "",
        "llm_excluded_smarts": "",
        "llm_excluded_cas": "",
        "llm_cross_notice_exclusion": bool(CROSS_NOTICE_EXCLUSION_RE.search(source_text)),
        "llm_explanation_ko": "",
        "llm_confidence": "",
        "llm_evidence_quotes": "",
        "llm_smarts_valid": "",
        "llm_status": "",
        "approval_status": "CANDIDATE_REVIEW_REQUIRED",
    }
    if not source_text:
        audit["llm_status"] = "NO_SOURCE_TEXT"
        return audit
    try:
        raw = _post_draft_chat(source_text)
    except Exception as exc:
        audit["llm_status"] = f"LLM_ERROR: {type(exc).__name__}: {exc}"
        return audit

    quotes = [clean(q) for q in raw.get("evidence_quotes", []) if clean(q)]
    verified_quotes = [q for q in quotes if _quote_is_verbatim(q, source_text)]
    required_smarts = clean(raw.get("required_smarts"))
    excluded_smarts = clean(raw.get("excluded_smarts"))
    required_valid = _smarts_is_valid(required_smarts)
    excluded_valid = _smarts_is_valid(excluded_smarts)

    # A pattern that doesn't parse is worse than no pattern at all -- drop it
    # rather than hand a reviewer something that will crash on first use.
    if required_smarts and not required_valid:
        required_smarts = ""
    if excluded_smarts and not excluded_valid:
        excluded_smarts = ""

    confidence = raw.get("confidence")
    try:
        confidence = max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence = 0.0
    if not verified_quotes:
        # No quote could be confirmed against the source text at all --
        # treat this the same way local_llm.py treats an unverifiable claim.
        confidence = min(confidence, 0.3)

    audit.update(
        {
            "llm_scope_type": clean(raw.get("scope_type")) or "UNCLEAR",
            "llm_required_smarts": required_smarts,
            "llm_excluded_smarts": excluded_smarts,
            "llm_excluded_cas": clean(raw.get("excluded_cas")),
            "llm_explanation_ko": clean(raw.get("explanation_ko")),
            "llm_confidence": confidence,
            "llm_evidence_quotes": "; ".join(verified_quotes),
            "llm_smarts_valid": required_valid and excluded_valid,
            "llm_status": "DRAFTED",
        }
    )
    return audit


def draft_structural_rules(
    cases: list[dict], max_cases: int = 20, on_progress=None
) -> pd.DataFrame:
    """Draft candidates for up to ``max_cases`` compound-group cases.

    Mirrors local_llm.analyze_cases' shape: sequential, one call per case
    (structural drafting needs the model's full attention on one clause at a
    time, unlike the batched review-explanation calls), always returning
    CANDIDATE_REVIEW_REQUIRED rows.

    ``on_progress``, if given, is called as ``on_progress(done, total, row)``
    after each case -- a notice-wide scan is on the order of 100 sequential
    model calls, which can genuinely take a long time depending on hardware,
    and with no feedback that looks identical to a hang.
    """
    selected = [case for case in cases if clean(case.get("case_id"))][: max(0, int(max_cases))]
    rows: list[dict] = []
    for i, case in enumerate(selected, start=1):
        row = draft_structural_rule(case)
        rows.append(row)
        if on_progress is not None:
            on_progress(i, len(selected), row)
    if not rows:
        return pd.DataFrame(columns=DRAFT_AUDIT_COLUMNS)
    return pd.DataFrame(rows, columns=DRAFT_AUDIT_COLUMNS)


def test_draft_against_smiles(smiles: str, draft_row: dict) -> dict:
    """Check whether one company material's structure falls inside an
    unverified draft pattern. This is a REVIEW HINT ONLY -- it never sets a
    gate decision, never appears as "규제 해당," and is always labeled as
    coming from an unapproved AI draft. A reviewer still has to look at it.
    """
    smiles = clean(smiles)
    required = clean(draft_row.get("llm_required_smarts") or draft_row.get("required_smarts"))
    excluded = clean(draft_row.get("llm_excluded_smarts") or draft_row.get("excluded_smarts"))
    if not smiles or not required:
        return {"matched": False, "reason": "구조 또는 제안 패턴 없음"}
    try:
        from rdkit import Chem

        mol = Chem.MolFromSmiles(smiles)
        if mol is None:
            return {"matched": False, "reason": "SMILES 파싱 실패"}
        required_pattern = Chem.MolFromSmarts(required)
        if required_pattern is None:
            return {"matched": False, "reason": "제안된 패턴이 유효하지 않음"}
        if not mol.HasSubstructMatch(required_pattern):
            return {"matched": False, "reason": "제안된 패턴과 불일치"}
        if excluded:
            excluded_pattern = Chem.MolFromSmarts(excluded)
            if excluded_pattern is not None and mol.HasSubstructMatch(excluded_pattern):
                return {"matched": False, "reason": "제외 패턴에 해당하여 제외됨"}
    except Exception as exc:
        return {"matched": False, "reason": f"구조 대조 오류: {type(exc).__name__}"}
    return {
        "matched": True,
        "reason": "AI가 제안한 패턴과 구조가 일치함(미검증)",
        "designation_id": clean(draft_row.get("designation_id")),
        "regime": clean(draft_row.get("regime")),
        "source_text": clean(draft_row.get("source_text")),
        "explanation_ko": clean(draft_row.get("llm_explanation_ko")),
        "confidence": draft_row.get("llm_confidence"),
    }


def build_draft_review_hints(
    plain_summary: pd.DataFrame,
    enriched_inventory: pd.DataFrame,
    draft_audit: pd.DataFrame,
) -> pd.DataFrame:
    """For every material still under review, test its structure against
    every drafted (unapproved) rule and return one hint row per real match.

    This is purely additive information for a reviewer -- it must never be
    merged into gate_decision, applicable_regulatory_categories, or any other
    field that the plain-language "판정결과" treats as a confirmed outcome.
    Joins on CAS rather than material_id, since the simplified plain summary
    does not carry material_id but both frames carry CAS.
    """
    empty = pd.DataFrame(
        columns=["cas", "chemical_name", "designation_id", "regime",
                 "explanation_ko", "confidence", "source_text"]
    )
    if draft_audit is None or draft_audit.empty or plain_summary is None or plain_summary.empty:
        return empty
    smiles_by_cas: dict[str, str] = {}
    if enriched_inventory is not None and not enriched_inventory.empty:
        for _, inv_row in enriched_inventory.iterrows():
            cas = clean(inv_row.get("cas") or inv_row.get("input_cas"))
            if cas:
                smiles_by_cas[cas] = clean(inv_row.get("smiles"))

    active_drafts = draft_audit[draft_audit["llm_status"].map(clean).eq("DRAFTED")]
    if active_drafts.empty:
        return empty

    rows: list[dict] = []
    for _, item in plain_summary.iterrows():
        if clean(item.get("판정결과")) == "규제 해당":
            continue  # already confirmed through a real, approved path
        cas = clean(item.get("CAS번호"))
        material_smiles = smiles_by_cas.get(cas, "")
        for _, draft in active_drafts.iterrows():
            result = test_draft_against_smiles(material_smiles, draft.to_dict())
            if result.get("matched"):
                rows.append(
                    {
                        "cas": cas,
                        "chemical_name": item.get("물질명"),
                        "designation_id": result.get("designation_id"),
                        "regime": result.get("regime"),
                        "explanation_ko": result.get("explanation_ko"),
                        "confidence": result.get("confidence"),
                        "source_text": result.get("source_text"),
                    }
                )
    return pd.DataFrame(rows, columns=empty.columns) if rows else empty


def extract_no_cas_cross_regime_cases(cross_rules: pd.DataFrame) -> list[dict]:
    """The cross-regime equivalent of extract_no_cas_master_cases: scan
    사고대비·제한·금지물질 rules (approved or still-candidate, since both share
    the same schema from appendix_sync.py) for entries with no direct CAS.

    A pure back-reference row like "1~N까지의 화학물질 중 하나를..." names no
    new compound family and is harmless but useless to draft a SMARTS for --
    it is included anyway rather than guessed away, since the model itself
    reliably returns an empty/low-confidence pattern for it (see
    draft_structural_rule's own honesty check), and filtering it out here
    would risk silently dropping a genuinely new "기타 ~" clause with an
    unusual phrasing this heuristic does not anticipate.
    """
    if cross_rules is None or cross_rules.empty:
        return []
    no_cas = cross_rules[
        cross_rules["direct_cas"].isna() | cross_rules["direct_cas"].astype(str).str.strip().eq("")
    ]
    cases: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for _, row in no_cas.iterrows():
        designation_id = clean(row.get("designation_id"))
        substance_name = clean(row.get("substance_name"))
        source_text = clean(row.get("source_text")) or substance_name
        if not source_text:
            continue
        key = (clean(row.get("regime")), designation_id, substance_name)
        if key in seen:
            continue
        seen.add(key)
        cases.append(
            {
                "case_id": f"CROSS-{clean(row.get('regime'))}-{designation_id}-{len(cases)}",
                "designation_id": designation_id,
                "regime": clean(row.get("regime")),
                "source_text": source_text,
            }
        )
    return cases


def extract_no_cas_master_cases(master: pd.DataFrame, notice: str, regime: str = "HUMAN_HAZARD") -> list[dict]:
    """Scan the ENTIRE approved master list (or any similarly-shaped table)
    for rows that carry no direct CAS at all -- the human-hazard equivalent
    of the no-CAS catch-all rows appendix_sync.py finds in cross-regime PDFs.

    This is the "don't do it one substance family at a time" entry point: it
    covers every compound-group, structural-range, or named-mixture entry in
    the notice in one pass, not just the ones a person happened to ask about.
    Abolished ("삭제") rows are skipped since there is nothing to draft.
    """
    if master is None or master.empty:
        return []
    frame = master[master["notice"].astype(str) == str(notice)] if "notice" in master.columns else master
    no_direct_cas = frame["direct_cas"].isna() | frame["direct_cas"].astype(str).str.strip().isin(["", "-"])
    abolished = frame["substance_name_ko"].astype(str).str.strip().isin(["삭제", "<삭제>", "<삭 제>"])
    candidates = frame[no_direct_cas & ~abolished]

    cases: list[dict] = []
    for _, row in candidates.iterrows():
        source_text = clean(row.get("main_rule_text")) or clean(row.get("source_text"))
        if not source_text:
            continue
        cases.append(
            {
                "case_id": f"MASTER-{clean(row.get('designation_id')) or clean(row.get('rule_id'))}",
                "designation_id": clean(row.get("designation_id")),
                "regime": regime,
                "source_text": source_text,
            }
        )
    return cases


def draft_and_cache(
    cases: list[dict],
    cache_path,
    max_cases: int = 200,
    force_refresh: bool = False,
    on_progress=None,
) -> pd.DataFrame:
    """Draft structural rules for many cases, but only once per case, and
    only again if that specific case's law text actually changed.

    Drafting is one call per case to the local LLM (see draft_structural_rule);
    for a notice-wide scan (order of 100 cases) that is a real, multi-minute
    background operation, not something to repeat on every screening run. The
    cache is keyed on (case_id, source_text): a brand-new designation gets
    drafted once and reused after that, and if a law revision changes the
    WORDING of an existing designation (same case_id, different text), that
    one case is redrafted while every unaffected case is still reused --
    matching case_id alone would silently keep serving a stale draft for a
    revised clause, which is worse than not caching at all.

    The result is written to ``cache_path`` after EVERY case, not only once
    at the end: a scan over ~100 cases has no visible progress otherwise (it
    looks identical to a hang), and stopping partway used to lose everything
    drafted so far. ``on_progress(done, total, row)``, if given, is called
    after each case for a live progress display.
    """
    from pathlib import Path

    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cached_by_id: dict[str, dict] = {}
    if not force_refresh and cache_path.exists():
        cached = pd.read_csv(cache_path, dtype=str).fillna("")
        cached_by_id = {row["case_id"]: row.to_dict() for _, row in cached.iterrows()}

    to_draft: list[dict] = []
    reused_rows: list[dict] = []
    for case in cases:
        case_id = clean(case.get("case_id"))
        cached_row = cached_by_id.get(case_id)
        if cached_row is not None and clean(cached_row.get("source_text")) == clean(
            case.get("source_text")
        ):
            reused_rows.append(cached_row)
        else:
            to_draft.append(case)
    to_draft = to_draft[: max(0, int(max_cases))]

    drafted_rows: list[dict] = []

    def _save(rows: list[dict]) -> pd.DataFrame:
        combined = pd.DataFrame(reused_rows + rows, columns=DRAFT_AUDIT_COLUMNS)
        combined.to_csv(cache_path, index=False, encoding="utf-8-sig")
        return combined

    total = len(to_draft)
    for i, case in enumerate(to_draft, start=1):
        row = draft_structural_rule(case)
        drafted_rows.append(row)
        result = _save(drafted_rows)
        if on_progress is not None:
            on_progress(i, total, row)
    return result if to_draft else _save(drafted_rows)


def to_extended_scope_rows(draft_audit: pd.DataFrame, notice_reference: str) -> pd.DataFrame:
    """Reshape drafted, human-reviewed rows into the extended_scope_rules
    schema so they can be appended to extended_scope_rules_APPROVED.csv.

    This does NOT filter for review status -- callers should only pass rows a
    person has actually confirmed. It exists purely to save re-typing the
    column layout by hand after review.
    """
    if draft_audit is None or draft_audit.empty:
        return pd.DataFrame(columns=EXTENDED_SCOPE_COLUMNS)
    rows: list[dict] = []
    for _, item in draft_audit.iterrows():
        rows.append(
            {
                "rule_id": f"LLM-{clean(item.get('case_id'))}",
                "regime": clean(item.get("regime")),
                "designation_id": clean(item.get("designation_id")),
                "substance_name": clean(item.get("source_text"))[:120],
                "scope_type": clean(item.get("llm_scope_type")),
                "direct_cas": "",
                "reference_parent_cas": "",
                "reference_smiles": "",
                "required_smarts": clean(item.get("llm_required_smarts")),
                "excluded_smarts": clean(item.get("llm_excluded_smarts")),
                "allowed_elements": "",
                "required_elements": "",
                "excluded_cas": clean(item.get("llm_excluded_cas")),
                "isomer_scope": "",
                "salt_scope": "",
                "composition_condition": (
                    "본 고시 내 별도 지정 물질은 이 조항보다 그 지정이 우선함"
                    if item.get("llm_cross_notice_exclusion")
                    else ""
                ),
                "concentration_threshold_pct": "",
                "use_condition": "",
                "quantity_threshold": "",
                "quantity_unit": "",
                "temporal_condition": "",
                "authorization_condition": "",
                "automation_status": False,
                "source_verified": False,
                "regulatory_source": notice_reference,
                "rule_version": "",
                "notes": (
                    f"AI 초안(미검증, 확신도 {item.get('llm_confidence')}) - "
                    f"{clean(item.get('llm_explanation_ko'))} 화학 전문가 검토 후 "
                    "approval_status를 APPROVED로 바꿔야 사용됩니다."
                ),
            }
        )
    return pd.DataFrame(rows, columns=EXTENDED_SCOPE_COLUMNS)
