"""Desktop shell around the shared screening pipeline (engine/screening_pipeline.py).

The Streamlit app calls the same pipeline; this module only adds the desktop
workspace, inventory file reading and result-file saving/cleanup.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Callable

import pandas as pd

APP_DIR = Path(__file__).resolve().parent
BUNDLE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))
ENGINE_DIR = BUNDLE_DIR / "engine" if (BUNDLE_DIR / "engine").exists() else APP_DIR / "engine"
if str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

# Portable by default: EXE folder/workspace contains the app's generated data.
# The user may choose another dedicated folder for the current session.
# Never write to LOCALAPPDATA by default.
EXECUTABLE_DIR = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else APP_DIR
)
WORKSPACE_DIR = EXECUTABLE_DIR / "workspace"
RESULTS_DIR = WORKSPACE_DIR / "results"
CACHE_DIR = WORKSPACE_DIR / "cache"
PENDING_DIR = WORKSPACE_DIR / "pending_updates"

import company_engine as company
import cross_regime
import regulatory_api
from company_engine import (
    load_approved_master,
    load_promoted_salt_rules,
    read_inventory,
)
from cross_regime import (
    RULE_COLUMNS,
    fetch_cross_regime_candidates,
    load_approved_cross_regime_rules,
)
from local_llm import get_local_llm_status
from desktop_results import keep_latest_desktop_result
from result_store import save_result_workbook
from scope_engine import load_extended_scope_rules
from screening_pipeline import rerun_ai as _rerun_ai, run_screening

Progress = Callable[[str], None]


def set_workspace(directory: str | Path) -> Path:
    """Set all desktop-generated file paths; call only while no worker runs.

    Creates an exclusive throwaway test file to verify write permissions.
    It does not delete or overwrite existing files.
    """
    global WORKSPACE_DIR, RESULTS_DIR, CACHE_DIR, PENDING_DIR
    import tempfile

    proposed = Path(directory).expanduser().resolve()
    proposed.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryFile(dir=proposed):
        pass

    WORKSPACE_DIR = proposed
    RESULTS_DIR = proposed / "results"
    CACHE_DIR = proposed / "cache"
    PENDING_DIR = proposed / "pending_updates"
    company.DEFAULT_CACHE = CACHE_DIR / "pubchem_structure_cache.csv"
    cross_regime.PENDING_DIR = PENDING_DIR / "cross_regime"
    regulatory_api.PENDING_DIR = PENDING_DIR
    return WORKSPACE_DIR


# Set dependent module paths without eagerly creating any files.
set_workspace(WORKSPACE_DIR)



def write_inventory_example(path: str | Path) -> Path:
    """Save an editable Excel example; the first worksheet is importable as-is.

    Sample rows are explicitly examples, not regulatory determinations.
    The user replaces them with their actual material inventory before screening.
    """
    from openpyxl.styles import Alignment, Font, PatternFill

    target = Path(path)
    if target.suffix.lower() != ".xlsx":
        raise ValueError("작성예시파일은 .xlsx 형식이어야 합니다.")

    # Only what the screening actually uses. Approved rules currently carry no
    # use/quantity/date/authorization conditions, and product name, department
    # or handling type never change a result. An older file that still has those
    # columns is read as before.
    columns = ["물질명", "CAS번호", "함량(%)", "SMILES"]
    examples = pd.DataFrame(
        [
            ["포름알데히드", "50-00-0", 2, ""],
            ["카페인", "58-08-2", 1, ""],
        ],
        columns=columns,
    )
    guide = pd.DataFrame(
        [
            ("작성 방법", "'물질입력' 시트의 예시 2행을 실제 물질로 바꾸거나 삭제한 뒤 저장하세요."),
            ("필수 정보", "물질명, CAS번호, 함량(%) 3가지만 작성하면 됩니다."),
            ("함량 예시", "2%는 숫자 2로 입력합니다. 0.02가 아닙니다."),
            ("CAS번호", "50-00-0처럼 하이픈을 포함한 번호를 입력하세요."),
            ("SMILES(선택)", "모르면 비워두세요. 인터넷에서 자동 찾기 기능을 사용할 수 있습니다."),
            ("판정 방법", "파일 저장 후 프로그램의 '파일 찾기'를 눌러 작성한 Excel을 선택하세요."),
            ("주의", "예시 행을 남겨두면 예시 물질도 판정 대상에 포함됩니다."),
        ],
        columns=["항목", "작성 안내"],
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    with pd.ExcelWriter(target, engine="openpyxl") as writer:
        examples.to_excel(writer, sheet_name="물질입력", index=False)
        guide.to_excel(writer, sheet_name="작성안내", index=False)
        ws = writer.book["물질입력"]
        ws.freeze_panes = "A2"
        ws.auto_filter.ref = "A1:D3"
        ws.row_dimensions[1].height = 29
        for cell in ws[1]:
            cell.fill = PatternFill(fill_type="solid", fgColor="234968")
            cell.font = Font(name="맑은 고딕", size=11, bold=True, color="FFFFFF")
            cell.alignment = Alignment(horizontal="center", vertical="center")
        widths = [23, 18, 14, 40]
        for i, width in enumerate(widths, start=1):
            ws.column_dimensions[ws.cell(1, i).column_letter].width = width
        for row in range(2, 4):
            ws.row_dimensions[row].height = 24
            for cell in ws[row]:
                cell.fill = PatternFill(fill_type="solid", fgColor="EAF3FB")
            ws.cell(row, 2).number_format = "@"
            ws.cell(row, 3).number_format = "0.00"
        tip = writer.book["작성안내"]
        tip.freeze_panes = "A2"
        tip.column_dimensions["A"].width = 22
        tip.column_dimensions["B"].width = 90
        tip.row_dimensions[1].height = 28
        for cell in tip[1]:
            cell.fill = PatternFill(fill_type="solid", fgColor="234968")
            cell.font = Font(name="맑은 고딕", size=11, bold=True, color="FFFFFF")
        for row in range(2, tip.max_row + 1):
            tip.row_dimensions[row].height = 30
            tip.cell(row, 1).fill = PatternFill(fill_type="solid", fgColor="EAF3FB")
            tip.cell(row, 2).alignment = Alignment(wrap_text=True, vertical="center")
    return target

def notice_status_offline() -> dict:
    return {
        "status": "NOT_CHECKED",
        "status_ko": "공식 API 조회하지 않음",
        "sync_status": "UNVERIFIED",
        "sync_status_ko": "최신 법규 여부 확인하지 않음",
    }


def initialize(online_law: bool, progress: Progress) -> dict:
    progress("승인된 화학물질 규제 마스터를 불러오는 중...")
    master, notice = load_approved_master()
    salt_rules = load_promoted_salt_rules(master)
    extended = load_extended_scope_rules()
    cross_approved = load_approved_cross_regime_rules()
    progress("로컬 AI 설치 여부를 확인하는 중...")
    ai_status = get_local_llm_status(timeout=3)

    if online_law:
        progress("국가법령정보 API에서 최신 고시를 확인하는 중...")
        api_status = regulatory_api.compare_with_master(
            regulatory_api.check_latest_notice(), notice
        )
        progress("다른 규제체계의 공식 고시 후보를 확인하는 중...")
        cross_candidates, cross_api_status = fetch_cross_regime_candidates()
    else:
        api_status = notice_status_offline()
        cross_candidates = pd.DataFrame(columns=RULE_COLUMNS)
        cross_api_status = pd.DataFrame()

    return {
        "master": master,
        "notice": notice,
        "salt_rules": salt_rules,
        "extended": extended,
        "cross_approved": cross_approved,
        "api_status": api_status,
        "cross_candidates": cross_candidates,
        "cross_api_status": cross_api_status,
        "ai_status": ai_status,
    }


def _finalize_desktop_save(saved: dict, *, source_path: str = "") -> dict:
    """Preserve the new result, then prune only the program's older XLSX files.

    A locked older Excel remains in place with an explicit warning; failures
    never discard the completed new workbook or the current UI result.
    """
    try:
        saved.update(keep_latest_desktop_result(
            RESULTS_DIR, saved, protected_paths=(source_path,)
        ))
    except (OSError, ValueError) as exc:
        saved["cleanup_removed"] = 0
        saved["cleanup_failed"] = []
        saved["manifest_reset"] = False
        saved["cleanup_warning"] = (
            "새 판정은 저장했지만 이전 결과 자동 정리에 실패했습니다: " + str(exc)
        )
    return saved


def _save(result: dict, source_path: str = "") -> dict:
    catalog, summary = result["catalog"], result["summary"]
    saved = save_result_workbook(
        result["workbook"], catalog["notice"], len(summary),
        api_status=catalog["api_status"],
        gate_counts={
            str(k): int(v) for k, v in summary["gate_decision"].value_counts().items()
        },
        directory=RESULTS_DIR,
        filename_prefix="사업장_화학물질_규제판정",
    )
    return _finalize_desktop_save(saved, source_path=source_path)


def screen_inventory(
    catalog: dict, source: str, fetch_pubchem: bool,
    use_local_ai: bool, progress: Progress,
) -> dict:
    progress("인벤토리 Excel/CSV 파일을 불러오는 중...")
    raw = read_inventory(None, default_path=Path(source))
    if raw.empty:
        raise ValueError("인벤토리에 물질 행이 없습니다.")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    result = run_screening(
        raw, catalog, fetch_pubchem=fetch_pubchem,
        use_local_ai=use_local_ai, progress=progress,
    )
    result["source_path"] = str(Path(source).resolve())
    progress("판정 결과 Excel 파일을 저장하는 중...")
    result["saved"] = _save(result, source)
    progress("분석이 완료되었습니다.")
    return result


def rerun_ai(result: dict, progress: Progress) -> dict:
    result = _rerun_ai(result, progress)
    result["saved"] = _save(result, result.get("source_path", ""))
    return result
