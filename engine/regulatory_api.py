from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import requests


ROOT = Path(__file__).resolve().parent
PENDING_DIR = ROOT / "data" / "pending_updates"

REGULATION_TITLE = (
    "인체급성유해성물질, 인체만성유해성물질 및 "
    "생태유해성물질의 지정고시"
)


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _load_local_env() -> None:
    """Load simple KEY=VALUE entries without overwriting process variables."""
    for path in (ROOT / ".env", Path.cwd() / ".env"):
        if not path.exists():
            continue
        try:
            for raw in path.read_text(encoding="utf-8", errors="ignore").splitlines():
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and value:
                    os.environ.setdefault(key, value)
        except OSError:
            continue


def get_api_credential() -> tuple[str, str]:
    _load_local_env()
    value = os.getenv("LAW_OC", "").strip()
    if value:
        return value, "환경변수 또는 .env"
    return "", "미설정"


def _recursive_dicts(obj: Any) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _recursive_dicts(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _recursive_dicts(value)


def _notice_label(record: dict) -> str:
    number = _clean(record.get("발령번호") or record.get("행정규칙발령번호"))
    date = _clean(record.get("발령일자"))
    match = re.search(r"(?P<year>20\d{2})\s*[-–]\s*(?P<num>\d+)", number)
    if match:
        return f"{match.group('year')}-{int(match.group('num'))}"
    nums = re.findall(r"\d+", number)
    year = date[:4] if re.match(r"^20\d{2}", date) else ""
    return f"{year}-{int(nums[-1])}" if year and nums else ""


def notice_key(label: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(20\d{2})\s*[-–]\s*(\d+)\s*", _clean(label))
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def check_latest_notice(timeout: int = 45) -> dict:
    """Query the official National Law Information shared-use API.

    The credential is never returned, logged, or written to the result workbook.
    """
    oc, source = get_api_credential()
    checked_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    if not oc:
        return {
            "status": "API_KEY_MISSING",
            "status_ko": "API 인증정보 미설정",
            "checked_at_utc": checked_at,
            "credential_source": source,
            "message": ".env 파일에 LAW_OC 값을 입력하면 공식 최신 고시를 확인합니다.",
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
                "query": REGULATION_TITLE,
                "display": 100,
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        candidates: list[dict] = []
        for record in _recursive_dicts(payload):
            title = _clean(record.get("행정규칙명") or record.get("행정규칙제목"))
            serial = _clean(record.get("행정규칙일련번호") or record.get("행정규칙 일련번호"))
            if not title or not serial:
                continue
            if not (
                title == REGULATION_TITLE
                or ("인체급성유해성물질" in title and "생태유해성물질" in title)
            ):
                continue
            candidates.append(
                {
                    "official_notice": _notice_label(record),
                    "official_serial": serial,
                    "official_title": title,
                    "issue_date": _clean(record.get("발령일자")),
                    "effective_date": _clean(record.get("시행일자")),
                    "revision_type": _clean(record.get("제개정구분명")),
                    "ministry": _clean(record.get("소관부처명")),
                }
            )
        if not candidates:
            return {
                "status": "NO_MATCH",
                "status_ko": "대상 고시 검색 결과 없음",
                "checked_at_utc": checked_at,
                "credential_source": source,
                "message": "공식 API 응답에서 대상 고시를 확인하지 못했습니다.",
            }
        best = sorted(candidates, key=lambda row: notice_key(row["official_notice"]))[-1]
        return {
            "status": "FOUND",
            "status_ko": "공식 최신 고시 확인 완료",
            "checked_at_utc": checked_at,
            "credential_source": source,
            "message": "국가법령정보 공동활용 API 조회 결과",
            **best,
        }
    except Exception as exc:  # the URL can contain the credential
        text = f"{type(exc).__name__}: {exc}".replace(oc, "***")
        text = re.sub(r"([?&]OC=)[^&\s]+", r"\1***", text, flags=re.I)
        return {
            "status": "API_ERROR",
            "status_ko": "공식 API 확인 실패",
            "checked_at_utc": checked_at,
            "credential_source": source,
            "message": text,
        }


def compare_with_master(api_status: dict, local_notice: str) -> dict:
    out = dict(api_status)
    out["local_notice"] = _clean(local_notice)
    official = _clean(out.get("official_notice"))
    if out.get("status") != "FOUND" or not official:
        out["sync_status"] = "UNVERIFIED"
        out["sync_status_ko"] = "최신 여부 확인 불가"
    elif notice_key(official) > notice_key(local_notice):
        out["sync_status"] = "UPDATE_PENDING"
        out["sync_status_ko"] = "새 고시 발견·검증 대기"
    elif notice_key(official) == notice_key(local_notice):
        out["sync_status"] = "CURRENT"
        out["sync_status_ko"] = "승인 규칙 최신"
    else:
        out["sync_status"] = "LOCAL_NEWER"
        out["sync_status_ko"] = "로컬 승인본이 더 최신"
    return out


def download_notice_candidate(api_status: dict, timeout: int = 90) -> dict:
    """Store the official JSON as a pending candidate; never auto-activate it."""
    if api_status.get("status") != "FOUND" or not _clean(api_status.get("official_serial")):
        return {"download_status": "NOT_AVAILABLE", "message": "다운로드 가능한 고시 정보가 없습니다."}
    oc, _ = get_api_credential()
    if not oc:
        return {"download_status": "API_KEY_MISSING", "message": "LAW_OC 설정이 필요합니다."}
    try:
        response = requests.get(
            "https://www.law.go.kr/DRF/lawService.do",
            params={
                "OC": oc,
                "target": "admrul",
                "ID": api_status["official_serial"],
                "type": "JSON",
            },
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        label = re.sub(r"[^0-9-]", "_", _clean(api_status.get("official_notice"))) or "unknown"
        output = PENDING_DIR / f"admrul_{label}_candidate.json"
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return {
            "download_status": "DOWNLOADED_PENDING_REVIEW",
            "message": "신규 고시 원문 후보를 저장했습니다. 승인 규칙에는 아직 적용되지 않았습니다.",
            "candidate_file": str(output),
        }
    except Exception as exc:
        text = f"{type(exc).__name__}: {exc}".replace(oc, "***")
        return {"download_status": "DOWNLOAD_ERROR", "message": text}
