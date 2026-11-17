"""Offline, no-AI integration smoke test. Run before creating a Windows EXE."""
from __future__ import annotations

from pathlib import Path

import desktop_backend as backend


def main():
    log = print
    catalog = backend.initialize(online_law=False, progress=log)
    assert not catalog["api_status"]["sync_status"] == "CURRENT"
    source = (
        Path(__file__).resolve().parent
        / "engine" / "data" / "sample_company_inventory.csv"
    )
    result = backend.screen_inventory(
        catalog, str(source), fetch_pubchem=False,
        use_local_ai=False, detailed=False, progress=log,
    )
    assert len(result["summary"]) == 6
    assert result["saved"]["file_name"].endswith(".xlsx")
    assert Path(result["saved"]["path"]).is_file()
    assert result["workbook"][:2] == b"PK"
    print("PASS: offline/no-AI screening and Excel report generation")


if __name__ == "__main__":
    main()
