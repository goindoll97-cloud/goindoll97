"""Keep only the latest auto-generated Windows desktop workbook.

Deliberately desktop-only: Streamlit's versioned result history stays intact.
This function is called *after* the new workbook has been successfully saved.
Only application-owned result XLSX files in the chosen workspace/results
directory are removed. Input data, manual exports, other files, other folders,
symlinks, cache and pending updates are never modified.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import tempfile
from typing import Iterable


GENERATED_NAME = re.compile(
    r"^(?:사업장|기업)_화학물질_규제판정_.+\.xlsx$", flags=re.IGNORECASE
)
MANIFEST_NAME = "run_manifest.jsonl"


def keep_latest_desktop_result(
    directory: str | Path,
    latest_saved: dict,
    *,
    protected_paths: Iterable[str | Path] = (),
) -> dict:
    """Remove older *program-created* XLSX only after the new save succeeded.

    An active source Excel is never deleted even if saved inside results and
    even if it accidentally follows a report-like name. No traversal of
    subfolders, symlink following, or deletion of unrelated user files.
    """
    root = Path(directory).resolve(strict=True)
    latest_path = Path(latest_saved["path"]).resolve(strict=True)
    if not root.is_dir() or latest_path.parent != root:
        raise ValueError("보호된 results 폴더 외부의 파일은 삭제할 수 없습니다.")
    if not latest_path.is_file() or not GENERATED_NAME.fullmatch(latest_path.name):
        raise ValueError("새로운 프로그램 결과파일을 확인한 후에만 이전 결과를 정리합니다.")

    protected = {latest_path}
    for path in protected_paths:
        if path:
            protected.add(Path(path).resolve(strict=False))

    manifest = root / MANIFEST_NAME
    tracked: set[str] = set()
    # Preserve compatibility with previous program-generated names recorded
    # by result_store, even if the prefix was changed between versions.
    if manifest.exists() and not manifest.is_symlink():
        try:
            for line in manifest.read_text(encoding="utf-8").splitlines():
                entry = json.loads(line)
                name = entry.get("file_name")
                if isinstance(name, str) and Path(name).name == name and name.lower().endswith(".xlsx"):
                    tracked.add(name)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            # Regex still recognizes earlier desktop result names even when
            # an old or corrupted manifest cannot be read.
            pass

    removed: list[str] = []
    failed: list[str] = []
    for path in root.iterdir():
        if path.is_symlink() or not path.is_file() or path.suffix.lower() != ".xlsx":
            continue
        if path.resolve(strict=False) in protected:
            continue
        if not (GENERATED_NAME.fullmatch(path.name) or path.name in tracked):
            continue
        try:
            path.unlink()
            removed.append(path.name)
        except OSError:
            failed.append(path.name)

    # Remove old audit-history entries too: the folder is intentionally a
    # "latest result only" workspace, not an archival results database.
    # Write atomically and only after the new XLSX is available.
    record = {key: value for key, value in latest_saved.items()
              if key not in {"path", "cleanup_removed", "cleanup_failed",
                             "cleanup_warning", "manifest_reset"}}
    pending: Path | None = None
    manifest_error = ""
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=root,
            prefix=".run_manifest_", suffix=".tmp", delete=False
        ) as handle:
            pending = Path(handle.name)
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(pending, manifest)
    except OSError as error:
        manifest_error = f"실행 기록 정리 실패: {error}"
    finally:
        if pending is not None and pending.exists():
            try:
                pending.unlink()
            except OSError:
                pass

    warnings = []
    if failed:
        warnings.append("열려 있거나 잠긴 기존 판정파일을 삭제하지 못했습니다: " + ", ".join(failed))
    if manifest_error:
        warnings.append(manifest_error)
    return {
        "cleanup_removed": len(removed),
        "cleanup_failed": failed,
        "manifest_reset": not bool(manifest_error),
        "cleanup_warning": "\n".join(warnings),
    }
