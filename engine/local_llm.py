from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import pandas as pd
import requests

from scope_engine import SCOPE_TYPES, clean


DEFAULT_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_MODEL = "qwen3.5:4b"
MAX_SOURCE_CHARS = 6000

ASSESSMENTS = [
    "POSSIBLE_MATCH",
    "CONDITION_REVIEW",
    "INSUFFICIENT_INFORMATION",
    "NO_ADDITIONAL_EVIDENCE",
]
RULE_RELATIONS = [
    "INCLUDED_CANDIDATE",
    "EXPLICITLY_EXCLUDED",
    "COMPONENT_ONLY",
    "INSUFFICIENT_INFORMATION",
]

# The browser summary always exposes these columns, even when the local model
# is disabled or a batch fails before it can return every optional field.
LLM_SUMMARY_DEFAULTS = {
    "llm_status": "",
    "llm_assessment": "",
    "llm_scope_type": "",
    "llm_explanation_ko": "",
    "llm_confidence": "",
    "llm_evidence_quotes": "",
    "llm_missing_information": "",
    "llm_gate_issues": "",
    "llm_model": "",
    "llm_candidate_designation_ids": "",
    "llm_model_selected_designation_ids": "",
    "llm_rule_relation": "",
}


def _base_url() -> str:
    return os.getenv("OLLAMA_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")


def _model_name() -> str:
    return os.getenv("OLLAMA_MODEL", DEFAULT_MODEL).strip() or DEFAULT_MODEL


def _is_loopback_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and parsed.hostname in {
        "127.0.0.1",
        "localhost",
        "::1",
    }


def get_local_llm_status(timeout: int = 3) -> dict:
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    base_url = _base_url()
    model = _model_name()
    if not _is_loopback_url(base_url):
        return {
            "status": "REMOTE_URL_BLOCKED",
            "status_ko": "외부 LLM 주소 차단",
            "ready": False,
            "model": model,
            "base_url": base_url,
            "checked_at_utc": checked_at,
            "message": "회사 자료 보호를 위해 localhost Ollama 주소만 허용합니다.",
        }
    try:
        response = requests.get(f"{base_url}/api/tags", timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        names = [clean(item.get("name")) for item in payload.get("models", []) if isinstance(item, dict)]
        ready = model in names or any(name.split(":")[0] == model.split(":")[0] and model.endswith(":latest") for name in names)
        return {
            "status": "READY" if ready else "MODEL_MISSING",
            "status_ko": "로컬 AI 사용 가능" if ready else "Ollama 연결됨·모델 설치 필요",
            "ready": ready,
            "model": model,
            "installed_models": "; ".join(names),
            "base_url": base_url,
            "checked_at_utc": checked_at,
            "message": "" if ready else f"setup_local_ai.bat을 실행하여 {model} 모델을 설치하십시오.",
        }
    except Exception as exc:
        return {
            "status": "OLLAMA_OFFLINE",
            "status_ko": "로컬 AI 연결 안 됨",
            "ready": False,
            "model": model,
            "base_url": base_url,
            "checked_at_utc": checked_at,
            "message": f"{type(exc).__name__}: Ollama가 실행 중인지 확인하십시오.",
        }


def _response_schema(single: bool = False) -> dict:
    item = {
        "type": "object",
        "properties": {
            "case_id": {"type": "string"},
            "assessment": {"type": "string", "enum": ASSESSMENTS},
            "scope_type": {"type": "string", "enum": SCOPE_TYPES},
            "rule_relation": {"type": "string", "enum": RULE_RELATIONS},
            "candidate_designation_ids": {"type": "array", "items": {"type": "string"}},
            "extracted_cas": {"type": "array", "items": {"type": "string"}},
            "concentration_condition": {"type": "string"},
            "use_condition": {"type": "string"},
            "quantity_condition": {"type": "string"},
            "temporal_condition": {"type": "string"},
            "authorization_condition": {"type": "string"},
            "missing_information": {"type": "array", "items": {"type": "string"}},
            "evidence_quotes": {"type": "array", "items": {"type": "string"}},
            "explanation_ko": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "needs_human_review": {"type": "boolean"},
        },
        "required": [
            "case_id",
            "assessment",
            "scope_type",
            "rule_relation",
            "candidate_designation_ids",
            "extracted_cas",
            "concentration_condition",
            "use_condition",
            "quantity_condition",
            "temporal_condition",
            "authorization_condition",
            "missing_information",
            "evidence_quotes",
            "explanation_ko",
            "confidence",
            "needs_human_review",
        ],
        "additionalProperties": False,
    }
    if single:
        return item
    return {
        "type": "object",
        "properties": {"results": {"type": "array", "items": item}},
        "required": ["results"],
        "additionalProperties": False,
    }


SYSTEM_PROMPT = """당신은 한국 화학물질 규제 검토 보조자다.
제공된 입력과 법규 원문 발췌만 사용한다. 지식이나 기억으로 CAS, 법적 범위, 예외를 만들어내지 않는다.
LLM은 최종 규제 해당/비해당을 결정하지 않는다. 가능한 범위 유형, 적용조건, 누락정보를 추출해 인간 검토를 돕는다.
evidence_quotes에는 반드시 제공된 법규 원문에 실제로 연속해서 존재하는 짧은 문구만 넣는다.
근거가 없으면 NO_ADDITIONAL_EVIDENCE 또는 INSUFFICIENT_INFORMATION으로 답한다.
candidate_designation_ids에는 제공된 후보 지정번호 중 법규 원문과 입력 물질의 관계를
추가로 검토할 가치가 있는 번호만 넣는다. 제공되지 않은 지정번호는 절대 만들지 않는다.
후보 메타데이터의 scope_type과 match_type을 우선한다. 범위유형을 자유롭게 추측하지 않는다.
EXPLICIT_EXCEPTION_CAS는 해당 규정에서 명시적으로 제외된 관계이므로 후보 지정번호를 기록하고
rule_relation을 EXPLICITLY_EXCLUDED로 답한다. COMPONENT_CAS_CANDIDATE는 구성성분만 일치하므로
후보 지정번호를 기록하고 COMPONENT_ONLY로 답하며 혼합물·반응생성물 자체라고 확정하지 않는다.
법규명이 '염류/화합물'처럼 총칭이면 뒤에 열거된 개별 물질은 원문에 '한정한다/only'가 없는 한
예시 또는 별도 규정일 수 있으므로 목록에 없다는 이유만으로 총칭 범위에서 제외하지 않는다.
법규 원문에 용도·수량·시행일·허가 조건이 없으면 그런 정보가 필요하다고 요구하지 않는다.
deterministic_decision과 use_description은 참고 입력일 뿐 화학적 동일성이나 법규 조건의 근거가 아니다.
모든 설명은 간결한 한국어로 작성한다."""


def _case_payload(case: dict) -> dict:
    return {
        "case_id": clean(case.get("case_id")),
        "chemical_name": clean(case.get("chemical_name")),
        "cas": clean(case.get("cas")),
        "smiles": clean(case.get("smiles")),
        "concentration_pct": clean(case.get("concentration_pct")),
        "use_description": clean(case.get("use_description")),
        "annual_quantity_kg": clean(case.get("annual_quantity_kg")),
        "as_of_date": clean(case.get("as_of_date")),
        "authorization_status": clean(case.get("authorization_status")),
        "deterministic_decision": clean(case.get("deterministic_decision")),
        "deterministic_reason": clean(case.get("deterministic_reason")),
        "candidate_designation_ids": clean(case.get("candidate_designation_ids")),
        "candidate_regulatory_names": clean(case.get("candidate_regulatory_names")),
        "candidate_context": clean(case.get("candidate_context")),
        "regulatory_text": clean(case.get("regulatory_text"))[:MAX_SOURCE_CHARS],
    }


def _extract_json(content: Any) -> Any:
    """Recover one JSON value from common local-model wrappers/noise."""
    if isinstance(content, (dict, list)):
        return content
    text = clean(content).strip()
    text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError as first_error:
        decoder = json.JSONDecoder()
        for match in re.finditer(r"[\[{]", text):
            try:
                value, _ = decoder.raw_decode(text[match.start():])
                return value
            except json.JSONDecodeError:
                continue
        raise first_error


def _as_list(value: Any) -> list:
    """Normalize loose generic-JSON fields returned by small local models."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        return [part.strip() for part in re.split(r"[;\n]", text) if part.strip()]
    return [value]


def _post_chat(cases: list[dict], timeout: int = 180, simple: bool = False) -> dict:
    base_url = _base_url()
    if not _is_loopback_url(base_url):
        raise ValueError("외부 LLM 주소는 허용되지 않습니다.")
    single = len(cases) == 1
    schema = _response_schema(single=single)
    if single:
        prompt = (
            "다음 한 사례를 분석하고 설명문 없이 JSON 객체 하나만 출력하라. "
            "candidate_designation_ids는 제공된 번호 중에서만 선택하라.\n"
            + json.dumps(_case_payload(cases[0]), ensure_ascii=False)
        )
    else:
        prompt = (
            "다음 사례를 입력 순서대로 분석하고 설명문 없이 JSON만 출력하라.\n"
            + json.dumps({"cases": [_case_payload(case) for case in cases]}, ensure_ascii=False)
        )
    response = requests.post(
        f"{base_url}/api/chat",
        json={
            "model": _model_name(),
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "stream": False,
            # Some small Ollama models occasionally fail a complex JSON schema.
            # The retry asks only for generic JSON while validation below still
            # enforces the same safety boundary.
            "format": "json" if simple else schema,
            "think": False,
            "options": {"temperature": 0, "seed": 42, "num_predict": 2048},
            "keep_alive": "10m",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    content = payload.get("message", {}).get("content", "")
    parsed = _extract_json(content)
    if single:
        if isinstance(parsed, dict) and isinstance(parsed.get("results"), list):
            results = parsed["results"]
        elif isinstance(parsed, list):
            results = parsed
        elif isinstance(parsed, dict):
            results = [parsed]
        else:
            raise ValueError("JSON 객체 응답이 아닙니다.")
        parsed = {"results": results}
    elif isinstance(parsed, list):
        parsed = {"results": parsed}
    if not isinstance(parsed, dict):
        raise ValueError("JSON 응답 구조를 읽을 수 없습니다.")
    parsed["model"] = clean(payload.get("model")) or _model_name()
    parsed["total_duration_ns"] = payload.get("total_duration", "")
    return parsed


def _validate_one(raw: dict, source_case: dict, model: str) -> dict:
    source_text = clean(source_case.get("regulatory_text"))[:MAX_SOURCE_CHARS]
    input_cas = clean(source_case.get("cas"))
    source_cas = set(re.findall(r"\b\d{2,7}-\d{2}-\d\b", source_text))
    allowed_cas = source_cas | ({input_cas} if input_cas else set())
    raw_extracted = _as_list(raw.get("extracted_cas"))
    extracted = [value for value in raw_extracted if clean(value) in allowed_cas]
    allowed_designations = {
        clean(value)
        for value in re.split(r"[;,]", clean(source_case.get("candidate_designation_ids")))
        if clean(value)
    }
    model_selected_designations = [
        clean(value)
        for value in _as_list(raw.get("candidate_designation_ids"))
        if clean(value) in allowed_designations
    ]
    # These are the designations actually reviewed in this case, not an AI
    # legal conclusion. Preserve every deterministic pre-screen candidate and
    # separately retain what the model itself selected for auditing.
    selected_designations = sorted(allowed_designations)
    quotes = []
    rejected_quotes = []
    raw_quotes = _as_list(raw.get("evidence_quotes"))
    for quote in raw_quotes:
        quote = clean(quote)
        if quote and quote in source_text:
            quotes.append(quote)
        elif quote:
            rejected_quotes.append(quote)
    assessment = clean(raw.get("assessment"))
    scope_type = clean(raw.get("scope_type"))
    rule_relation = clean(raw.get("rule_relation"))
    confidence = pd.to_numeric(pd.Series([raw.get("confidence")]), errors="coerce").iloc[0]
    if assessment not in ASSESSMENTS:
        assessment = "INSUFFICIENT_INFORMATION"
    if scope_type not in SCOPE_TYPES:
        scope_type = "MANUAL_REVIEW"
    if rule_relation not in RULE_RELATIONS:
        rule_relation = "INSUFFICIENT_INFORMATION"
    if pd.isna(confidence):
        confidence = 0.0
    confidence = min(1.0, max(0.0, float(confidence)))
    gate_issues = []
    try:
        candidate_context = json.loads(clean(source_case.get("candidate_context")) or "[]")
    except json.JSONDecodeError:
        candidate_context = []
    candidate_context = [item for item in candidate_context if isinstance(item, dict)]
    context_by_id = {
        clean(item.get("designation_id")): item for item in candidate_context
        if clean(item.get("designation_id"))
    }
    forced_ids = {
        designation_id for designation_id, item in context_by_id.items()
        if clean(item.get("match_type")) in {"EXPLICIT_EXCEPTION_CAS", "COMPONENT_CAS_CANDIDATE"}
    }
    if forced_ids - set(model_selected_designations):
        gate_issues.append("명시적 제외·구성성분 관계 지정번호 보존")
    if rejected_quotes:
        gate_issues.append("원문에 없는 인용 제거")
    if len(extracted) < len(raw_extracted):
        gate_issues.append("입력·원문에 없는 CAS 제거")
    if len(model_selected_designations) < len(_as_list(raw.get("candidate_designation_ids"))):
        gate_issues.append("제공되지 않은 지정번호 제거")
    if not source_text and assessment in {"POSSIBLE_MATCH", "CONDITION_REVIEW"}:
        assessment = "INSUFFICIENT_INFORMATION"
        confidence = min(confidence, 0.3)
        gate_issues.append("법규 원문 부재로 적극적 평가 차단")
    if not quotes and assessment == "POSSIBLE_MATCH":
        assessment = "INSUFFICIENT_INFORMATION"
        confidence = min(confidence, 0.3)
        gate_issues.append("검증 가능한 원문 인용 부재")
    scope_text = f"{clean(source_case.get('candidate_regulatory_names'))} {source_text}".lower()
    expected_scope = ""
    deterministic_scopes = {clean(item.get("scope_type")) for item in candidate_context}
    if len(deterministic_scopes) == 1:
        expected_scope = {
            "SALT_FAMILY": "PARENT_SALT",
            "REACTION_PRODUCT": "REACTION_PRODUCT",
            "MIXTURE": "COMPOUND_GROUP",
            "NAME_OR_UVCB": "COMPOUND_GROUP",
        }.get(next(iter(deterministic_scopes)), "")
    elif any(word in scope_text for word in ("reaction product", "반응생성물")):
        expected_scope = "REACTION_PRODUCT"
    elif any(word in scope_text for word in ("reaction mixture", "mixture of", "반응혼합물", "혼합물")):
        expected_scope = "POLYMER_UVCB" if "polymer" in scope_text or "고분자" in scope_text else "COMPOUND_GROUP"
    elif any(word in scope_text for word in ("and its salts", "its salts", "그 염류", " 염류", " salts")):
        expected_scope = "PARENT_SALT"
    elif any(word in scope_text for word in ("compounds", "화합물")):
        expected_scope = "COMPOUND_GROUP"
    elif any(word in scope_text for word in ("derivative", "유도체")):
        expected_scope = "DERIVATIVE"
    if expected_scope and scope_type != expected_scope:
        scope_type = expected_scope
        gate_issues.append("범위유형을 규제 원문 기준으로 보정")
    match_types = {clean(item.get("match_type")) for item in candidate_context}
    if "EXPLICIT_EXCEPTION_CAS" in match_types:
        rule_relation = "EXPLICITLY_EXCLUDED"
    elif "COMPONENT_CAS_CANDIDATE" in match_types:
        rule_relation = "COMPONENT_ONLY"
    elif rule_relation in {"EXPLICITLY_EXCLUDED", "COMPONENT_ONLY"}:
        rule_relation = "INSUFFICIENT_INFORMATION"
        gate_issues.append("후보 근거와 맞지 않는 규칙관계 보정")

    missing_items = _as_list(raw.get("missing_information"))
    source_lower = source_text.lower()
    condition_markers = {
        "용도": ("용도", "사용", "제조", "세정제", "use", "purpose"),
        "수량": ("kg", "킬로그램", "톤", "연간", "취급량", "quantity", "amount"),
        "시행": ("시행", "부터", "까지", "effective", "date"),
        "허가": ("허가", "승인", "신고", "authorization", "permit"),
    }
    filtered_missing = []
    for value in missing_items:
        item_text = clean(value)
        item_lower = item_text.lower()
        categories = [
            key for key, markers in condition_markers.items()
            if any(marker in item_lower for marker in markers)
        ]
        if categories and not any(
            marker in source_lower
            for category in categories for marker in condition_markers[category]
        ):
            gate_issues.append(f"원문에 없는 조건정보 요구 제거({','.join(categories)})")
            continue
        filtered_missing.append(item_text)
    condition_fields = {
        "llm_use_condition": ("use_condition", condition_markers["용도"]),
        "llm_quantity_condition": ("quantity_condition", condition_markers["수량"]),
        "llm_temporal_condition": ("temporal_condition", condition_markers["시행"]),
        "llm_authorization_condition": ("authorization_condition", condition_markers["허가"]),
    }
    validated_conditions = {}
    for output_name, (raw_name, markers) in condition_fields.items():
        value = clean(raw.get(raw_name))
        if value and not any(marker in source_lower for marker in markers):
            validated_conditions[output_name] = ""
            gate_issues.append(f"원문에 없는 조건추출 제거({output_name})")
        else:
            validated_conditions[output_name] = value
    return {
        "case_id": clean(source_case.get("case_id")),
        "material_id": clean(source_case.get("material_id")),
        "llm_status": "VALIDATED_OUTPUT",
        "llm_assessment": assessment,
        "llm_scope_type": scope_type,
        "llm_rule_relation": rule_relation,
        "llm_extracted_cas": "; ".join(dict.fromkeys(extracted)),
        "llm_candidate_designation_ids": "; ".join(dict.fromkeys(selected_designations)),
        "llm_model_selected_designation_ids": "; ".join(dict.fromkeys(model_selected_designations)),
        "llm_concentration_condition": clean(raw.get("concentration_condition")),
        "llm_use_condition": validated_conditions["llm_use_condition"],
        "llm_quantity_condition": validated_conditions["llm_quantity_condition"],
        "llm_temporal_condition": validated_conditions["llm_temporal_condition"],
        "llm_authorization_condition": validated_conditions["llm_authorization_condition"],
        "llm_missing_information": "; ".join(x for x in filtered_missing if x),
        "llm_evidence_quotes": " | ".join(dict.fromkeys(quotes)),
        "llm_explanation_ko": clean(raw.get("explanation_ko")),
        "llm_confidence": confidence,
        "llm_needs_human_review": bool(raw.get("needs_human_review", True)),
        "llm_gate_issues": "; ".join(gate_issues),
        "llm_model": model,
        "llm_interpretation_boundary": "검토 보조 전용; 결정론적 규제판정을 변경하지 않음",
    }


def analyze_cases(
    cases: list[dict], max_cases: int = 25, batch_size: int = 1, on_progress=None
) -> pd.DataFrame:
    """Analyze one material per request and retry once with generic JSON."""
    selected = [case for case in cases if clean(case.get("case_id"))][: max(0, int(max_cases))]
    if not selected:
        return pd.DataFrame()
    rows = []
    # One case per call prevents one malformed answer from discarding four
    # otherwise valid materials. ``batch_size`` remains accepted for callers
    # from older app versions but is intentionally ignored.
    for start, case in enumerate(selected):
        batch = [case]
        try:
            try:
                response = _post_chat(batch)
                attempts = 1
            except Exception:
                response = _post_chat(batch, simple=True)
                attempts = 2
            model = clean(response.get("model")) or _model_name()
            raw_by_id = {
                clean(item.get("case_id")): item
                for item in response.get("results", [])
                if isinstance(item, dict)
            }
            for case in batch:
                case_id = clean(case.get("case_id"))
                raw = raw_by_id.get(case_id)
                if raw is None:
                    rows.append(
                        {
                            "case_id": case_id,
                            "material_id": clean(case.get("material_id")),
                            "llm_status": "MISSING_CASE_OUTPUT",
                            "llm_assessment": "INSUFFICIENT_INFORMATION",
                            "llm_scope_type": "MANUAL_REVIEW",
                            "llm_model": model,
                            "llm_needs_human_review": True,
                            "llm_interpretation_boundary": "검토 보조 전용; 결정론적 규제판정을 변경하지 않음",
                        }
                    )
                else:
                    validated = _validate_one(raw, case, model)
                    validated["llm_attempts"] = attempts
                    rows.append(validated)
        except Exception as exc:
            for case in batch:
                rows.append(
                    {
                        "case_id": clean(case.get("case_id")),
                        "material_id": clean(case.get("material_id")),
                        "llm_status": "LLM_ERROR",
                        "llm_assessment": "INSUFFICIENT_INFORMATION",
                        "llm_scope_type": "MANUAL_REVIEW",
                        "llm_explanation_ko": f"{type(exc).__name__}: 로컬 LLM 분석 실패",
                        "llm_error_detail": clean(exc)[:500],
                        "llm_attempts": 2,
                        "llm_model": _model_name(),
                        "llm_needs_human_review": True,
                        "llm_interpretation_boundary": "검토 보조 전용; 결정론적 규제판정을 변경하지 않음",
                    }
                )
        if on_progress is not None:
            on_progress(start + 1, len(selected))
    return pd.DataFrame(rows)


def build_llm_review_cases(
    summary: pd.DataFrame,
    cross_detail: pd.DataFrame,
    evidence: pd.DataFrame,
) -> list[dict]:
    cases: list[dict] = []
    detail = cross_detail if cross_detail is not None else pd.DataFrame()
    evidence_frame = evidence if evidence is not None else pd.DataFrame()
    for idx, row in summary.reset_index(drop=True).iterrows():
        material_id = clean(row.get("material_id"))

        # AI is useful only for genuinely interpretive holds.  Confirmed rows,
        # simple identity/input failures, coverage gaps and unapproved API
        # candidates cannot be resolved by an LLM and must not consume time.
        gate_decision = clean(row.get("gate_decision"))
        final_decision = clean(row.get("final_decision"))
        cross_decisions = [
            clean(row.get(f"{regime}_decision"))
            for regime in ("ACCIDENT_PREPAREDNESS", "RESTRICTED", "PROHIBITED", "AUTHORIZED")
        ]
        hold_reason = clean(row.get("gate_hold_reason"))
        has_structure = bool(clean(row.get("smiles")))
        structural_hold = has_structure and any(
            keyword in hold_reason
            for keyword in ("구조범위", "복합 규제범위", "염류", "유도체", "화합물군")
        )
        has_no_cas_candidates = bool(
            pd.to_numeric(
                pd.Series([row.get("no_cas_scope_candidate_count")]), errors="coerce"
            ).fillna(0).iloc[0] > 0
        )
        interpretive_hold = (
            final_decision == "전문가 검토 필요"
            or any(value == "조건 검토 필요" for value in cross_decisions)
            or structural_hold
            or has_no_cas_candidates
        )
        if gate_decision:
            # A material may already be confirmed under another regime while a
            # CAS-less human-hazard scope still needs interpretation.  Keep the
            # confirmed decision, but do not suppress that secondary AI review.
            if ("보류" not in gate_decision and not has_no_cas_candidates) or not interpretive_hold:
                continue
        elif clean(row.get("overall_decision")) not in {
            "추가 확인 필요",
            "전문가 검토 필요",
            "조건 검토 필요",
        }:
            continue
        related_text = []
        if not detail.empty:
            related_text.extend(
                detail.loc[detail["material_id"].astype(str).eq(material_id), "source_text"]
                .dropna()
                .astype(str)
                .tolist()
            )
        if not evidence_frame.empty:
            related_text.extend(
                evidence_frame.loc[
                    evidence_frame["material_id"].astype(str).eq(material_id), "source_text"
                ]
                .dropna()
                .astype(str)
                .tolist()
            )
        regulatory_text = "\n---\n".join(dict.fromkeys(text for text in related_text if clean(text)))
        review_text = clean(row.get("review_regulatory_text"))
        if review_text:
            regulatory_text = "\n---\n".join(
                dict.fromkeys(text for text in [regulatory_text, review_text] if clean(text))
            )
        if not regulatory_text:
            continue
        cases.append(
            {
                "case_id": f"LLM-{idx + 1:05d}",
                "material_id": material_id,
                "chemical_name": row.get("input_chemical_name"),
                "cas": row.get("input_cas"),
                "smiles": row.get("smiles"),
                "concentration_pct": row.get("concentration_pct"),
                "use_description": row.get("use_description"),
                "annual_quantity_kg": row.get("annual_quantity_kg"),
                "as_of_date": row.get("as_of_date"),
                "authorization_status": row.get("authorization_status"),
                "deterministic_decision": row.get("overall_decision"),
                "deterministic_reason": row.get("decision_reason"),
                "candidate_designation_ids": row.get("no_cas_scope_designation_ids"),
                "candidate_regulatory_names": row.get("no_cas_scope_regulatory_names"),
                "candidate_context": row.get("no_cas_scope_candidate_context"),
                "regulatory_text": regulatory_text,
            }
        )
    return cases


def merge_llm_audit(summary: pd.DataFrame, audit: pd.DataFrame) -> pd.DataFrame:
    out = summary.copy()
    if audit is None or audit.empty:
        for column, default in LLM_SUMMARY_DEFAULTS.items():
            out[column] = default
        return out
    columns = [
        "material_id",
        "llm_status",
        "llm_assessment",
        "llm_scope_type",
        "llm_explanation_ko",
        "llm_confidence",
        "llm_evidence_quotes",
        "llm_missing_information",
        "llm_gate_issues",
        "llm_model",
        "llm_candidate_designation_ids",
        "llm_model_selected_designation_ids",
        "llm_rule_relation",
    ]
    keep = [column for column in columns if column in audit.columns]
    if "material_id" not in keep:
        for column, default in LLM_SUMMARY_DEFAULTS.items():
            out[column] = default
        return out
    # A successful retry appended later must replace the earlier error row.
    unique = audit[keep].drop_duplicates(subset=["material_id"], keep="last")
    out = out.merge(unique, on="material_id", how="left")
    for column, default in LLM_SUMMARY_DEFAULTS.items():
        if column not in out.columns:
            out[column] = default
        else:
            out[column] = out[column].fillna(default)
    return out
