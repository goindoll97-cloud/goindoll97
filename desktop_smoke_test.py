"""Offline, no-AI integration smoke test. Run before creating a Windows EXE."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import ZipFile

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

        example = backend.write_inventory_example(Path(temp) / "작성예시파일.xlsx")
        assert example.is_file()
        parsed = backend.read_inventory(None, default_path=example)
        assert len(parsed) == 2
        assert list(parsed["chemical_name"]) == ["포름알데히드", "카페인"]
        assert list(parsed["cas"]) == ["50-00-0", "58-08-2"]
        assert list(parsed["concentration_pct"]) == [2, 1]
        assert "smiles" in parsed.columns
        catalog = backend.initialize(online_law=False, progress=log)
        assert catalog["api_status"]["sync_status"] != "CURRENT"
        result = backend.screen_inventory(
            catalog, str(source), fetch_pubchem=False,
            use_local_ai=False, progress=log,
        )
        assert len(result["summary"]) == 6
        assert result["saved"]["file_name"].startswith("사업장_화학물질_규제판정_")
        assert result["saved"]["file_name"].endswith(".xlsx")
        assert Path(result["saved"]["path"]).is_file()
        assert Path(result["saved"]["path"]).parent == backend.RESULTS_DIR
        assert result["workbook"][:2] == b"PK"
        # Audit sheets are always included, including when running offline/AI OFF.
        with ZipFile(BytesIO(result["workbook"])) as archive:
            workbook_xml = ElementTree.fromstring(archive.read("xl/workbook.xml"))
            ns = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
            sheet_names = {sheet.attrib["name"] for sheet in workbook_xml.iter(f"{ns}sheet")}
            required = {
                "판정결과", "상세판정요약", "신뢰게이트", "카테고리검색기록",
                "법률근거", "규제근거", "물질식별", "PubChem조회기록",
                "규제체계별판정", "타법규근거", "법규API상태",
            }
            assert required.issubset(sheet_names), (
                f"Missing detailed audit sheets: {required - sheet_names}"
            )
        assert Path(result["saved"]["path"]).read_bytes() == result["workbook"]
    print("PASS: portable workspace, offline/AI-OFF screening, full audit Excel always exported")


if __name__ == "__main__":
    main()
