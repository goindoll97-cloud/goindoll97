"""Offline, no-AI integration smoke test. Run before creating a Windows EXE."""
from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory

import desktop_backend as backend


def main():
    log = lambda message: None  # Windows CI consoles may not encode Korean.
    source = (
        Path(__file__).resolve().parent
        / "engine" / "data" / "sample_company_inventory.csv"
    )
    with TemporaryDirectory(prefix="chemreg_demo_test_") as temp:
        workspace = Path(temp) / "user_selected_data_folder"
        backend.set_workspace(workspace)
        assert backend.WORKSPACE_DIR == workspace.resolve()
        assert backend.RESULTS_DIR.parent == workspace.resolve()
        assert backend.CACHE_DIR.parent == workspace.resolve()
        assert backend.PENDING_DIR.parent == workspace.resolve()
        assert backend.company.DEFAULT_CACHE.parent == backend.CACHE_DIR
        assert backend.cross_regime.PENDING_DIR.parent == backend.PENDING_DIR
        assert backend.regulatory_api.PENDING_DIR == backend.PENDING_DIR

        catalog = backend.initialize(online_law=False, progress=log)
        assert catalog["api_status"]["sync_status"] != "CURRENT"
        result = backend.screen_inventory(
            catalog, str(source), fetch_pubchem=False,
            use_local_ai=False, detailed=False, progress=log,
        )
        assert len(result["summary"]) == 6
        assert result["saved"]["file_name"].endswith(".xlsx")
        assert Path(result["saved"]["path"]).is_file()
        assert Path(result["saved"]["path"]).parent == backend.RESULTS_DIR
        assert result["workbook"][:2] == b"PK"
    print("PASS: portable selected workspace, offline/no-AI screening, Excel export")


if __name__ == "__main__":
    main()
