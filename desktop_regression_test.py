"""Regression: future effective date must not erase approved-rule evidence.

Runs offline against bundled approved regulatory data, without Ollama.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

import pandas as pd

import desktop_backend as backend
from regulatory_api import compare_with_master
from trust_gate import law_version_gate


def main():
    api = {
        "status": "FOUND",
        "official_notice": "2026-16",
        "effective_date": "20261219",
    }
    before = compare_with_master(api, "2026-5", as_of=date(2026, 11, 17))
    assert before["sync_status"] == "FUTURE_NOTICE", before
    assert "2026-12-19" in before["sync_status_ko"]
    assert law_version_gate(before)["code"] == "LAW_VERSION_FUTURE_NOTICE"
    after = compare_with_master(api, "2026-5", as_of=date(2026, 12, 20))
    assert after["sync_status"] == "UPDATE_PENDING", after
    equal = compare_with_master(
        {**api, "effective_date": "20260701", "official_notice": "2026-5"},
        "2026-5", as_of=date(2026, 11, 17)
    )
    assert equal["sync_status"] == "CURRENT", equal
    assert compare_with_master({"status": "NO_MATCH"}, "2026-5", as_of=date(2026, 11, 17))["sync_status"] == "UNVERIFIED"

    with TemporaryDirectory(prefix="regression_rules_") as temp:
        backend.set_workspace(Path(temp) / "workspace")
        inventory = pd.DataFrame([
            {"material_id": "R01", "chemical_name": "Sodium cyanide", "cas": "143-33-9", "concentration_pct": 2},
            {"material_id": "R02", "chemical_name": "Formaldehyde", "cas": "50-00-0", "concentration_pct": 2},
            {"material_id": "R03", "chemical_name": "Unknown CAS", "cas": "000-00-0", "concentration_pct": 1},
        ])
        source = Path(temp) / "regression.csv"
        inventory.to_csv(source, index=False)
        catalog = backend.initialize(False, lambda _message: None)
        catalog["api_status"] = before
        result = backend.screen_inventory(
            catalog, str(source), fetch_pubchem=False, use_local_ai=False,
            progress=lambda _message: None,
        )
        plain = result["plain"].set_index("CAS번호")
        summary = result["summary"].set_index("input_cas")
        for cas, category in (
            ("143-33-9", "인체급성유해성물질"),
            ("50-00-0", "인체급성유해성물질"),
        ):
            row = plain.loc[cas]
            assert row["판정결과"] == "규제 해당", (cas, row.to_dict())
            assert category in row["규제 카테고리"], (cas, row.to_dict())
            assert row["검증 상태"].startswith("보류"), (cas, row.to_dict())
            assert "2026-12-19" in summary.loc[cas, "law_version_status"], cas
            assert row["법적 근거"] != "-", cas
        invalid = plain.loc["000-00-0"]
        assert invalid["판정결과"] == "보류(추가 확인 필요)", invalid.to_dict()
        assert "물질식별" in invalid["검증 상태"], invalid.to_dict()
        assert not result["llm_audit"].shape[0]
        assert result["workbook"].startswith(b"PK")
        assert "인벤토리 ID" in plain.columns
        assert "구조초안 힌트" in plain.columns
        assert "인체급성 기준(%)" in plain.columns
        assert "추가 확인사항" in plain.columns
    print("PASS: future notice, approved CAS positive evidence, audit separation, AI OFF")


if __name__ == "__main__":
    main()
