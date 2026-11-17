"""Durable storage for screening result workbooks.

Every run is written to ``outputs/results`` under a unique, timestamped name.
Nothing is ever overwritten or deleted automatically, so a result that was
handed to a reviewer can always be produced again together with the run
manifest describing the legal version it was based on.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parent
RESULTS_DIR = ROOT / "outputs" / "results"
MANIFEST = RESULTS_DIR / "run_manifest.jsonl"
KST = ZoneInfo("Asia/Seoul")


def _safe(value: Any) -> str:
    return re.sub(r"[^0-9A-Za-z가-힣._-]", "_", str(value or "")).strip("_")


def save_result_workbook(
    data: bytes,
    master_notice: str,
    row_count: int,
    api_status: Optional[dict] = None,
    gate_counts: Optional[dict] = None,
    directory: Path = RESULTS_DIR,
    filename_prefix: str = "기업_화학물질_규제판정",
) -> dict:
    """Persist one workbook and append an audit line to the run manifest."""
    directory.mkdir(parents=True, exist_ok=True)
    manifest_path = directory / MANIFEST.name
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc.astimezone(KST)
    # Human-readable local time makes repeated runs distinguishable in the
    # sidebar without forcing Korean users to convert a UTC compact stamp.
    stamp = now_kst.strftime("%Y-%m-%d_%H-%M-%S_KST")
    name = f"{_safe(filename_prefix) or '화학물질_규제판정'}_{_safe(master_notice) or 'unknown'}_{stamp}.xlsx"
    path = directory / name
    suffix = 1
    while path.exists():  # never overwrite an already delivered result
        path = directory / f"{path.stem}_{suffix}.xlsx"
        suffix += 1
    path.write_bytes(data)

    record = {
        "saved_at_utc": now_utc.isoformat(timespec="seconds"),
        "saved_at_kst": now_kst.isoformat(timespec="seconds"),
        "file_name": path.name,
        "file_bytes": len(data),
        "master_notice": master_notice,
        "inventory_rows": int(row_count),
        "law_api_status": (api_status or {}).get("sync_status", ""),
        "official_notice": (api_status or {}).get("official_notice", ""),
        "gate_counts": gate_counts or {},
    }
    try:
        with manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        record["manifest_error"] = "매니페스트 기록에 실패했습니다."
    record["path"] = str(path)
    return record


def list_saved_results(directory: Path = RESULTS_DIR, limit: int = 20) -> list[dict]:
    """Return the most recent preserved result files, newest first."""
    if not directory.exists():
        return []
    files = sorted(
        (p for p in directory.glob("*.xlsx") if p.is_file()),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    manifest_path = directory / MANIFEST.name
    manifest_by_name: dict[str, dict] = {}
    if manifest_path.exists():
        try:
            for line in manifest_path.read_text(encoding="utf-8").splitlines():
                record = json.loads(line)
                if record.get("file_name"):
                    manifest_by_name[str(record["file_name"])] = record
        except (OSError, json.JSONDecodeError):
            manifest_by_name = {}
    rows = []
    for path in files[:limit]:
        record = manifest_by_name.get(path.name, {})
        saved_at = record.get("saved_at_kst")
        if not saved_at:
            utc_text = record.get("saved_at_utc")
            try:
                saved_at = datetime.fromisoformat(str(utc_text)).astimezone(KST).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                saved_at = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).astimezone(KST).isoformat(timespec="seconds")
        try:
            display_time = datetime.fromisoformat(saved_at).strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            display_time = saved_at
        notice = _safe(record.get("master_notice"))
        inventory_rows = record.get("inventory_rows")
        details = [display_time]
        if notice:
            details.append(f"고시 {notice}")
        if inventory_rows not in (None, ""):
            details.append(f"{int(inventory_rows):,}건")
        rows.append({
            "name": path.name,
            "display_name": " · ".join(details),
            "path": str(path),
            "size_kb": round(path.stat().st_size / 1024, 1),
            "saved_at": saved_at,
        })
    return rows


def read_saved_result(path: str | Path) -> bytes:
    """Read a preserved result, refusing paths outside the results directory."""
    target = Path(path).resolve()
    root = RESULTS_DIR.resolve()
    if root not in target.parents:
        raise ValueError("보존 폴더 밖의 파일은 제공하지 않습니다.")
    return target.read_bytes()
