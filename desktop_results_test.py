"""Offline regression: desktop results retain only the newest program workbook."""
from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from desktop_results import keep_latest_desktop_result
import desktop_backend as backend


def main():
    with TemporaryDirectory(prefix="desktop_results_test_") as temp:
        root = Path(temp) / "workspace" / "results"
        root.mkdir(parents=True)
        old = root / "사업장_화학물질_규제판정_2026-5_2026-11-22_12-00-00_KST.xlsx"
        legacy = root / "기업_화학물질_규제판정_2026-5_2026-11-22_13-00-00_KST.xlsx"
        custom = root / "내_다른_개인파일.xlsx"
        protected_source = root / "사업장_화학물질_규제판정_사용자_인벤토리.xlsx"
        other = root / "notes.csv"
        subdir = root / "archive"
        subdir.mkdir()
        child = subdir / "기업_화학물질_규제판정_old.xlsx"
        for file in (old, legacy, custom, protected_source, other, child):
            file.write_bytes(b"user data" if file == protected_source else b"old data")
        now = root / "사업장_화학물질_규제판정_2026-5_2026-11-23_14-00-00_KST.xlsx"
        now.write_bytes(b"new valid workbook stored first")
        manifest = root / "run_manifest.jsonl"
        manifest.write_text("\n".join([
            json.dumps({"file_name": old.name, "inventory_rows": 11}, ensure_ascii=False),
            json.dumps({"file_name": legacy.name, "inventory_rows": 12}, ensure_ascii=False),
            json.dumps({"file_name": now.name, "inventory_rows": 13}, ensure_ascii=False),
        ]) + "\n", encoding="utf-8")
        result = keep_latest_desktop_result(
            root, {"path": str(now), "file_name": now.name, "inventory_rows": 13},
            protected_paths=(protected_source,)
        )
        assert result["cleanup_removed"] == 2, result
        assert not old.exists() and not legacy.exists()
        assert now.read_bytes() == b"new valid workbook stored first"
        for file in (custom, protected_source, other, child):
            assert file.exists(), f"User file or nested folder deleted: {file}"
        history = [json.loads(line) for line in manifest.read_text(encoding="utf-8").splitlines()]
        assert len(history) == 1 and history[0]["file_name"] == now.name
        assert result["cleanup_warning"] == ""

        # A second completed screening removes the previous auto-result too,
        # but may never delete the user's input or other personal files.
        latest = root / "사업장_화학물질_규제판정_2026-5_2026-11-23_15-00-00_KST.xlsx"
        latest.write_bytes(b"newer")
        extra = keep_latest_desktop_result(
            root, {"path": str(latest), "file_name": latest.name, "inventory_rows": 16},
            protected_paths=(protected_source,)
        )
        assert extra["cleanup_removed"] == 1, extra
        assert not now.exists() and latest.read_bytes() == b"newer"

        # Never clear results based on a missing, unowned or outside workbook.
        unowned = root / "user_manual.xlsx"
        unowned.write_bytes(b"manual")
        try:
            keep_latest_desktop_result(root, {"path": str(unowned), "file_name": unowned.name})
        except ValueError:
            pass
        else:
            raise AssertionError("Unowned workbook incorrectly authorized cleanup")
        assert latest.exists() and unowned.exists()

        # Desktop app must use the selected workspace, not globally scan the PC.
        backend.set_workspace(Path(temp) / "another_workspace")
        assert backend.RESULTS_DIR != root
        assert latest.exists()

    print("PASS: previous desktop results cleaned, newest+inventory+other files protected")


if __name__ == "__main__":
    main()
