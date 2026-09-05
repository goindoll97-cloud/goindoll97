from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
import requests

from regulatory_api import get_api_credential, notice_key
from scope_engine import classify_scope_text, clean, split_values
from appendix_sync import extract_candidates_from_payload


ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
PENDING_DIR = DATA_DIR / "pending_updates" / "cross_regime"
APPROVED_RULES = DATA_DIR / "cross_regime_rules_APPROVED.csv"

REGIMES = {
    "ACCIDENT_PREPAREDNESS": "사고대비물질",
    "RESTRICTED": "제한물질",
    "PROHIBITED": "금지물질",
}

REGIME_DECISION_COLUMNS = {
    "HUMAN_HAZARD_decision": "인체등유해성물질",
    "ACCIDENT_PREPAREDNESS_decision": REGIMES["ACCIDENT_PREPAREDNESS"],
    "RESTRICTED_decision": REGIMES["RESTRICTED"],
    "PROHIBITED_decision": REGIMES["PROHIBITED"],
}

CONFIRMED_CATEGORY_DECISIONS = {"규제 해당"}
REVIEW_CATEGORY_DECISIONS = {
    "해당 후보·검토 필요",
    "조건 검토 필요",
    "추가정보 필요",
    "정보 부족",
    "전문가 검토 필요",
}

NOTICE_SPECS = [
    {
        "regime": "ACCIDENT_PREPAREDNESS",
        "title": "사고대비물질의 지정",
        "terms": ["사고대비물질", "지정"],
    },
    {
        "regime": "RESTRICTED_PROHIBITED",
        "title": "제한물질·금지물질의 지정",
        "terms": ["제한물질", "금지물질", "지정"],
    },
    # AUTHORIZED (허가물질 지정 등에 관한 규정) is intentionally out of scope:
    # its attachments are score sheets and comment-submission forms, not a
    # CAS/threshold table, so there is nothing here for this pipeline to use.
]

RULE_COLUMNS = [
    "regime",
    "rule_id",
    "designation_id",
    "substance_name",
    "scope_type",
    "direct_cas",
    "source_text",
    "concentration_threshold_pct",
    "concentration_operator",
    "use_condition",
    "quantity_threshold",
    "quantity_unit",
    "temporal_condition",
    "authorization_condition",
    "source_name",
    "source_reference",
    "source_locator",
    "official_serial",
    "official_issue_date",
    "official_effective_date",
    "approval_status",
    "extraction_status",
    "designation_id_inferred",
]

CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
PERCENT_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?:%|퍼센트)\s*(?P<op>이상|초과|이하|미만)?")
QUANTITY_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>kg|킬로그램|톤|ton|t)\b", re.I)


def _recursive_dicts(obj: Any) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _recursive_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _recursive_dicts(value)


def _recursive_strings(obj: Any, path: str = "root") -> Iterable[tuple[str, str]]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            yield from _recursive_strings(value, f"{path}.{key}")
    elif isinstance(obj, list):
        for idx, value in enumerate(obj):
            yield from _recursive_strings(value, f"{path}[{idx}]")
    elif isinstance(obj, (str, int, float)):
        value = clean(obj)
        if value:
            yield path, value


def _normalize_title(value: Any) -> str:
    return re.sub(r"[\s·ㆍ\-–—]", "", clean(value)).lower()


def _title_matches(candidate: str, spec: dict) -> bool:
    value = _normalize_title(candidate)
    return value == _normalize_title(spec["title"]) or all(
        _normalize_title(term) in value for term in spec["terms"]
    )


def _notice_label(record: dict) -> str:
    number = clean(record.get("발령번호") or record.get("행정규칙발령번호"))
    issued = clean(record.get("발령일자"))
    match = re.search(r"(20\d{2})\s*[-–]\s*(\d+)", number)
    if match:
        return f"{match.group(1)}-{int(match.group(2))}"
    numbers = re.findall(r"\d+", number)
    return f"{issued[:4]}-{int(numbers[-1])}" if issued[:4].isdigit() and numbers else ""


def query_notice(spec: dict, timeout: int = 45) -> tuple[dict, dict]:
    oc, source = get_api_credential()
    checked = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not oc:
        return {}, {
            "regime": spec["regime"],
            "regime_label": spec["title"],
            "status": "API_KEY_MISSING",
            "status_ko": "API 인증정보 미설정",
            "checked_at_utc": checked,
            "credential_source": source,
            "message": "LAW_OC를 설정하면 현행 고시를 조회합니다.",
        }
    try:
        response = requests.get(
            "https://www.law.go.kr/DRF/lawSearch.do",
            params={
                "OC": oc,
                "target": "admrul",
                "type": "JSON",
                "nw": 1,
                "search": 1,
                "query": spec["title"],
                "display": 100,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        candidates = []
        for record in _recursive_dicts(response.json()):
            title = clean(record.get("행정규칙명") or record.get("행정규칙제목"))
            serial = clean(record.get("행정규칙일련번호") or record.get("행정규칙 일련번호"))
            if title and serial and _title_matches(title, spec):
                candidates.append(
                    {
                        "regime": spec["regime"],
                        "official_notice": _notice_label(record),
                        "official_serial": serial,
                        "official_title": title,
                        "issue_date": clean(record.get("발령일자")),
                        "effective_date": clean(record.get("시행일자")),
                        "ministry": clean(record.get("소관부처명")),
                    }
                )
        if not candidates:
            return {}, {
                "regime": spec["regime"],
                "regime_label": spec["title"],
                "status": "NO_MATCH",
                "status_ko": "대상 고시 검색 결과 없음",
                "checked_at_utc": checked,
            }
        best = sorted(candidates, key=lambda row: notice_key(row["official_notice"]))[-1]
        return best, {
            **best,
            "regime_label": spec["title"],
            "status": "FOUND",
            "status_ko": "공식 현행 고시 확인",
            "checked_at_utc": checked,
        }
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}".replace(oc, "***")
        return {}, {
            "regime": spec["regime"],
            "regime_label": spec["title"],
            "status": "API_ERROR",
            "status_ko": "공식 API 확인 실패",
            "checked_at_utc": checked,
            "message": message,
        }


def _infer_regime(text: str, default: str) -> str:
    compact = re.sub(r"\s+", "", text)
    has_prohibited = "금지물질" in compact or "금지대상" in compact
    has_restricted = "제한물질" in compact or "제한대상" in compact
    if has_prohibited and not has_restricted:
        return "PROHIBITED"
    if has_restricted and not has_prohibited:
        return "RESTRICTED"
    return default


def _extract_conditions(text: str) -> dict:
    percent = PERCENT_RE.search(text)
    quantity = QUANTITY_RE.search(text)
    use = text if re.search(r"용도|용으로|사용에|제조[·ㆍ]?수입[·ㆍ]?사용|판매", text) else ""
    temporal = text if re.search(r"시행|유예|경과조치|\d{4}\s*년\s*\d{1,2}\s*월", text) else ""
    authorization = text if re.search(r"허가|승인|신고|금지|제한", text) else ""
    return {
        "concentration_threshold_pct": float(percent.group("value")) if percent else np.nan,
        "concentration_operator": clean(percent.group("op")) if percent else "",
        "use_condition": use,
        "quantity_threshold": float(quantity.group("value")) if quantity else np.nan,
        "quantity_unit": clean(quantity.group("unit")) if quantity else "",
        "temporal_condition": temporal,
        "authorization_condition": authorization,
    }


def _parse_candidate_rules(payload: dict, info: dict, spec: dict) -> pd.DataFrame:
    records: list[dict] = []
    current = "" if spec["regime"] == "RESTRICTED_PROHIBITED" else spec["regime"]
    for locator, text in _recursive_strings(payload):
        current = _infer_regime(text, current)
        cas_values = list(dict.fromkeys(CAS_RE.findall(text)))
        if not cas_values:
            continue
        regime = current
        if spec["regime"] == "RESTRICTED_PROHIBITED" and regime not in {"RESTRICTED", "PROHIBITED"}:
            regime = "RESTRICTED_PROHIBITED_REVIEW"
        normalized = re.sub(r"\s+", " ", text).strip()
        token = f"{regime}|{';'.join(cas_values)}|{normalized}"
        rule_id = "API_" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
        records.append(
            {
                "regime": regime,
                "rule_id": rule_id,
                "designation_id": rule_id,
                "substance_name": "",
                "scope_type": classify_scope_text(text, has_direct_cas=True),
                "direct_cas": ";".join(cas_values),
                "source_text": normalized,
                **_extract_conditions(text),
                "source_name": info.get("official_title", spec["title"]),
                "source_reference": info.get("official_notice", ""),
                "source_locator": locator,
                "official_serial": info.get("official_serial", ""),
                "official_issue_date": info.get("issue_date", ""),
                "official_effective_date": info.get("effective_date", ""),
                "approval_status": "CANDIDATE_REVIEW_REQUIRED",
                "extraction_status": "OFFICIAL_API_AUTO_EXTRACTED",
            }
        )
    frame = pd.DataFrame(records)
    if frame.empty:
        return pd.DataFrame(columns=RULE_COLUMNS)
    return frame.drop_duplicates(subset=["regime", "direct_cas", "source_text"]).reset_index(drop=True)


def fetch_cross_regime_candidates(timeout: int = 90) -> tuple[pd.DataFrame, pd.DataFrame]:
    rules: list[pd.DataFrame] = []
    statuses: list[dict] = []
    oc, _ = get_api_credential()
    for spec in NOTICE_SPECS:
        info, status = query_notice(spec)
        statuses.append(status)
        if not info or not oc:
            continue
        try:
            response = requests.get(
                "https://www.law.go.kr/DRF/lawService.do",
                params={"OC": oc, "target": "admrul", "ID": info["official_serial"], "type": "JSON"},
                timeout=timeout,
            )
            response.raise_for_status()
            payload = response.json()
            PENDING_DIR.mkdir(parents=True, exist_ok=True)
            path = PENDING_DIR / f"{spec['regime']}_{info.get('official_notice', 'current')}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            parsed = _parse_candidate_rules(payload, info, spec)
            if not parsed.empty:
                rules.append(parsed)
            status["candidate_rule_rows"] = len(parsed)
            status["candidate_status_ko"] = "공식 원문 후보 추출·검토 대기"
        except Exception as exc:
            status["candidate_rule_rows"] = 0
            status["candidate_status_ko"] = "공식 원문 후보 추출 실패"
            status["message"] = f"{type(exc).__name__}: {exc}".replace(oc, "***")
            status["appendix_status"] = "BODY_FETCH_FAILED"
            status["appendix_status_ko"] = "고시 본문 조회 실패로 별표도 확인하지 못함"
            status["appendix_rows"] = 0
            status["appendix_attachments"] = []
            continue

        # The appendix (별표) tables that carry the actual CAS/threshold lists
        # live in a separate attached file, not in this article body, so they
        # get their own try/except: a failure here must never overwrite the
        # article-text extraction status above, and must always be visible
        # rather than silently folded into a generic "추출 실패" message.
        appendix_default = (
            "RESTRICTED_PROHIBITED_REVIEW"
            if spec["regime"] == "RESTRICTED_PROHIBITED"
            else spec["regime"]
        )
        try:
            appendix_rules, appendix_status = extract_candidates_from_payload(
                payload, appendix_default, info, timeout=timeout
            )
            if not appendix_rules.empty:
                rules.append(appendix_rules)
                appendix_rules.to_csv(
                    PENDING_DIR
                    / f"appendix_candidates_{spec['regime']}_{info.get('official_notice','current')}.csv",
                    index=False,
                    encoding="utf-8-sig",
                )
            status["appendix_status"] = appendix_status.get("status", "")
            status["appendix_status_ko"] = appendix_status.get("status_ko", "")
            status["appendix_rows"] = appendix_status.get("extracted_rows", 0)
            status["appendix_attachments"] = appendix_status.get("attachments", [])
        except Exception as exc:
            status["appendix_status"] = "APPENDIX_EXTRACTION_ERROR"
            status["appendix_status_ko"] = "별표 추출 중 예외 발생"
            status["appendix_rows"] = 0
            status["appendix_attachments"] = [
                {"appendix": "(전체)", "status": "EXCEPTION", "message": f"{type(exc).__name__}: {exc}"}
            ]
    combined = pd.concat(rules, ignore_index=True, sort=False) if rules else pd.DataFrame(columns=RULE_COLUMNS)
    return combined, pd.DataFrame(statuses)


def load_approved_cross_regime_rules(path: Path = APPROVED_RULES) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame(columns=RULE_COLUMNS)
    frame = pd.read_csv(path, dtype=str).fillna("")
    for column in RULE_COLUMNS:
        if column not in frame.columns:
            frame[column] = ""
    return frame[frame["approval_status"].str.upper().eq("APPROVED")].copy().reset_index(drop=True)


def _missing_fields(rule: pd.Series, item: pd.Series) -> list[str]:
    missing = []
    threshold = pd.to_numeric(pd.Series([rule.get("concentration_threshold_pct")]), errors="coerce").iloc[0]
    quantity = pd.to_numeric(pd.Series([rule.get("quantity_threshold")]), errors="coerce").iloc[0]
    if np.isfinite(threshold) and not np.isfinite(
        pd.to_numeric(pd.Series([item.get("concentration_pct")]), errors="coerce").iloc[0]
    ):
        missing.append("함량(%)")
    if clean(rule.get("use_condition")) and not clean(item.get("use_description") or item.get("use")):
        missing.append("용도")
    if np.isfinite(quantity) and not np.isfinite(
        pd.to_numeric(pd.Series([item.get("annual_quantity_kg")]), errors="coerce").iloc[0]
    ):
        missing.append("연간취급량(kg)")
    if clean(rule.get("temporal_condition")) and not clean(item.get("as_of_date")):
        missing.append("판정기준일")
    if clean(rule.get("authorization_condition")) and not clean(item.get("authorization_status")):
        missing.append("허가·신고상태")
    return missing


def _approved_simple_decision(rule: pd.Series, item: pd.Series) -> tuple[str, str]:
    missing = _missing_fields(rule, item)
    if missing:
        return "추가정보 필요", ", ".join(missing)
    if any(clean(rule.get(field)) for field in ["use_condition", "temporal_condition", "authorization_condition"]):
        return "조건 검토 필요", "용도·시행일·허가/금지 예외조건은 규제 원문 대조가 필요합니다."
    threshold = pd.to_numeric(pd.Series([rule.get("concentration_threshold_pct")]), errors="coerce").iloc[0]
    concentration = pd.to_numeric(pd.Series([item.get("concentration_pct")]), errors="coerce").iloc[0]
    if np.isfinite(threshold):
        operator = clean(rule.get("concentration_operator")) or "이상"
        passed = concentration >= threshold if operator == "이상" else concentration > threshold if operator == "초과" else concentration <= threshold if operator == "이하" else concentration < threshold
        if not passed:
            return "기준 미만·조건 비충족", f"함량 {concentration:g}% / 조건 {threshold:g}% {operator}"
    return "규제 해당", "승인 규칙의 직접 CAS와 구조화된 수치조건이 일치합니다."


def screen_cross_regimes(
    inventory: pd.DataFrame,
    approved_rules: pd.DataFrame,
    candidate_rules: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    detail: list[dict] = []
    regime_columns = ["ACCIDENT_PREPAREDNESS", "RESTRICTED", "PROHIBITED"]
    for _, item in inventory.iterrows():
        cas = clean(item.get("cas"))
        material_id = clean(item.get("material_id"))
        for source_kind, rules in [("APPROVED", approved_rules), ("API_CANDIDATE", candidate_rules)]:
            if rules is None or rules.empty or not cas:
                continue
            for _, rule in rules.iterrows():
                if cas not in split_values(rule.get("direct_cas")):
                    continue
                regime = clean(rule.get("regime"))
                if regime not in regime_columns:
                    regime = "RESTRICTED_PROHIBITED_REVIEW"
                missing = _missing_fields(rule, item)
                if source_kind == "APPROVED":
                    decision, reason = _approved_simple_decision(rule, item)
                else:
                    decision = "해당 후보·검토 필요"
                    reason = "공식 API 원문에서 CAS를 찾았으나 자동 추출 규칙은 아직 추적성 검증 전입니다."
                    if missing:
                        reason += " 추가정보: " + ", ".join(missing)
                detail.append(
                    {
                        "material_id": material_id,
                        "product_name": clean(item.get("product_name")),
                        "chemical_name": clean(item.get("chemical_name")),
                        "cas": cas,
                        "regime": regime,
                        "regime_label": REGIMES.get(regime, "제한·금지 구분 검토"),
                        "decision": decision,
                        "scope_type": clean(rule.get("scope_type")) or classify_scope_text(rule.get("source_text"), True),
                        "rule_id": clean(rule.get("rule_id")),
                        "designation_id": clean(rule.get("designation_id")),
                        "substance_name": clean(rule.get("substance_name")),
                        "missing_enterprise_fields": ";".join(missing),
                        "reason": reason,
                        "source_text": clean(rule.get("source_text")),
                        "source_name": clean(rule.get("source_name")),
                        "source_reference": clean(rule.get("source_reference")),
                        "official_effective_date": clean(rule.get("official_effective_date")),
                        "rule_source_status": source_kind,
                    }
                )
    detail_frame = pd.DataFrame(detail)
    rows = []
    priority = {
        "규제 해당": 5,
        "해당 후보·검토 필요": 4,
        "조건 검토 필요": 3,
        "추가정보 필요": 2,
        "기준 미만·조건 비충족": 1,
    }
    for _, item in inventory.iterrows():
        material_id = clean(item.get("material_id"))
        row = {"material_id": material_id}
        for regime in regime_columns:
            if detail_frame.empty:
                hits = pd.DataFrame()
            else:
                # A hit in the combined restricted/prohibited notice that the
                # parser could not attribute belongs to both columns as a review
                # item; dropping it would read as "no match found".
                regimes = detail_frame["regime"].map(clean)
                matched = regimes.eq(regime)
                if regime in {"RESTRICTED", "PROHIBITED"}:
                    matched = matched | regimes.str.startswith("RESTRICTED_PROHIBITED")
                hits = detail_frame[detail_frame["material_id"].eq(material_id) & matched]
            if hits.empty:
                row[f"{regime}_decision"] = "법규 데이터 확인 필요"
                row[f"{regime}_reason"] = "현재 승인 규칙 또는 API 후보에서 직접 CAS 일치를 확인하지 못했습니다."
            else:
                best = max(hits["decision"].tolist(), key=lambda value: priority.get(value, 0))
                row[f"{regime}_decision"] = best
                row[f"{regime}_reason"] = " | ".join(hits.loc[hits["decision"].eq(best), "reason"].drop_duplicates())
        rows.append(row)
    return pd.DataFrame(rows), detail_frame


def add_regulatory_category_columns(summary: pd.DataFrame) -> pd.DataFrame:
    """Add concise, multi-label category fields without promoting API candidates.

    A category is confirmed only when its regime-level decision is ``규제 해당``.
    Candidate matches and matched rules lacking enterprise conditions are kept in
    a separate review field so the UI cannot present them as final legal matches.
    """
    out = summary.copy()

    def collect(row: pd.Series, allowed: set[str]) -> str:
        labels = [
            label
            for column, label in REGIME_DECISION_COLUMNS.items()
            if clean(row.get(column)) in allowed
        ]
        return " / ".join(labels)

    out["applicable_regulatory_categories"] = out.apply(
        lambda row: collect(row, CONFIRMED_CATEGORY_DECISIONS), axis=1
    )
    out["review_regulatory_categories"] = out.apply(
        lambda row: collect(row, REVIEW_CATEGORY_DECISIONS), axis=1
    )
    out["applicable_regulatory_categories"] = out[
        "applicable_regulatory_categories"
    ].replace("", "확정 해당 없음")
    out["review_regulatory_categories"] = out[
        "review_regulatory_categories"
    ].replace("", "검토 대상 없음")
    return out
