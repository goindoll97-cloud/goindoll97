"""Shared regulatory-screening workflow for the PySide6 desktop application.

Streamlit remains unchanged; this module calls the same approved-rule engine.
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
    build_result_workbook,
    load_approved_master,
    load_promoted_salt_rules,
    prepare_company_inventory,
    read_inventory,
    screen_company_inventory,
)
from cross_regime import (
    RULE_COLUMNS,
    add_regulatory_category_columns,
    fetch_cross_regime_candidates,
    load_approved_cross_regime_rules,
    screen_cross_regimes,
)
from local_llm import analyze_cases, build_llm_review_cases, get_local_llm_status, merge_llm_audit
from no_cas_scope import build_no_cas_scope_index, screen_no_cas_scopes, attach_no_cas_candidates
from plain_report import build_plain_summary
from result_store import save_result_workbook
from scope_engine import load_extended_scope_rules
from trust_gate import (
    apply_trust_gate,
    build_category_search_log,
    build_legal_basis,
    cross_rule_availability,
)

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


def _overall(row: pd.Series) -> str:
    cols = (
        "HUMAN_HAZARD_decision",
        "ACCIDENT_PREPAREDNESS_decision",
        "RESTRICTED_decision",
        "PROHIBITED_decision",
    )
    values = [str(row.get(col, "")) for col in cols]
    if "규제 해당" in values:
        return "규제 해당"
    if any("해당 후보" in v for v in values):
        return "규제 해당 후보"
    if any(v in {"추가정보 필요", "조건 검토 필요", "법규 데이터 확인 필요",
                 "전문가 검토 필요", "물질정보 확인 실패", "정보 부족"} for v in values):
        return "추가 확인 필요"
    return "확인 범위 내 비해당"


def _create_workbook(result: dict, detailed: bool) -> bytes:
    summary = result["summary"]
    return build_result_workbook(
        summary,
        result["evidence"],
        result["enriched"],
        result["pubchem_audit"],
        result["review"],
        result["catalog"]["api_status"],
        result["catalog"]["notice"],
        len(result["catalog"]["master"]),
        cross_summary=summary[[
            "material_id", "HUMAN_HAZARD_decision",
            "ACCIDENT_PREPAREDNESS_decision", "RESTRICTED_decision",
            "PROHIBITED_decision", "applicable_regulatory_categories",
            "review_regulatory_categories", "overall_decision"
        ]].copy(),
        cross_detail=result["cross_detail"],
        cross_api_status=result["catalog"]["cross_api_status"],
        llm_audit=result["llm_audit"],
        llm_status=result["catalog"]["ai_status"],
        gate_log=result["gate_log"],
        category_log=result["category_log"],
        legal_basis=result["legal_basis"],
        no_cas_candidates=result["no_cas_candidates"],
        detailed=detailed,
    )


def screen_inventory(
    catalog: dict, source: str, fetch_pubchem: bool,
    use_local_ai: bool, detailed: bool, progress: Progress,
) -> dict:
    progress("인벤토리 Excel/CSV 파일을 불러오는 중...")
    raw = read_inventory(None, default_path=Path(source))
    if raw.empty:
        raise ValueError("인벤토리에 물질 행이 없습니다.")

    progress(f"{len(raw):,}개 물질의 CAS와 구조정보를 정리하는 중...")
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    enriched, pubchem_audit = prepare_company_inventory(raw, fetch_pubchem=fetch_pubchem)

    progress("인체등유해성물질 규제 기준을 대조하는 중...")
    summary, evidence, review = screen_company_inventory(
        enriched, catalog["master"], catalog["salt_rules"],
        catalog["notice"], extended_rules=catalog["extended"]
    )
    progress("사고대비·제한·금지물질 규제를 대조하는 중...")
    cross_summary, cross_detail = screen_cross_regimes(
        enriched, catalog["cross_approved"], catalog["cross_candidates"]
    )
    summary = summary.merge(cross_summary, on="material_id", how="left")
    summary["HUMAN_HAZARD_decision"] = summary["final_decision"]
    summary["overall_decision"] = summary.apply(_overall, axis=1)
    summary = add_regulatory_category_columns(summary)

    progress("규제 카테고리 검색기록과 법률근거를 검증하는 중...")
    availability = cross_rule_availability(
        catalog["cross_approved"], catalog["cross_candidates"],
        catalog["cross_api_status"]
    )
    category_log = build_category_search_log(
        summary, cross_detail, availability, catalog["notice"], len(catalog["master"])
    )
    legal_basis = build_legal_basis(evidence, cross_detail, catalog["notice"])
    summary, gate_log = apply_trust_gate(
        summary, evidence, cross_detail, category_log, legal_basis,
        catalog["api_status"]
    )

    progress("CAS가 없는 포괄 규제범위 후보를 검토하는 중...")
    no_cas_index = build_no_cas_scope_index(catalog["master"], catalog["notice"])
    no_cas_candidates = screen_no_cas_scopes(enriched, no_cas_index)
    summary = attach_no_cas_candidates(summary, no_cas_candidates)

    before_ai = summary.copy()
    llm_cases = build_llm_review_cases(summary, cross_detail, evidence)
    llm_audit = pd.DataFrame()
    if use_local_ai:
        if not catalog["ai_status"].get("ready"):
            raise RuntimeError("로컬 AI가 연결되지 않았습니다. AI를 OFF로 설정하세요.")
        max_cases = min(len(llm_cases), 40)
        progress(f"로컬 AI가 검토 필요 사례 {max_cases}건을 분석하는 중...")
        def on_ai_progress(done: int, total: int) -> None:
            progress(f"로컬 AI 분석 중: {done}/{total}건")
        llm_audit = analyze_cases(
            llm_cases, max_cases=max_cases, on_progress=on_ai_progress
        )
    summary = merge_llm_audit(summary, llm_audit)
    result = {
        "catalog": catalog,
        "summary": summary,
        "summary_before_llm": before_ai,
        "evidence": evidence,
        "review": review,
        "enriched": enriched,
        "pubchem_audit": pubchem_audit,
        "cross_detail": cross_detail,
        "category_log": category_log,
        "gate_log": gate_log,
        "legal_basis": legal_basis,
        "no_cas_candidates": no_cas_candidates,
        "llm_cases": llm_cases,
        "llm_audit": llm_audit,
        "plain": build_plain_summary(
            summary, evidence, cross_detail, catalog["notice"]
        ),
        "detailed": detailed,
    }

    progress("판정 결과 Excel 파일을 생성하고 저장하는 중...")
    workbook = _create_workbook(result, detailed)
    saved = save_result_workbook(
        workbook, catalog["notice"], len(summary),
        api_status=catalog["api_status"],
        gate_counts={
            str(k): int(v) for k, v in summary["gate_decision"].value_counts().items()
        },
        directory=RESULTS_DIR,
        filename_prefix="사업장_화학물질_규제판정",
    )
    result["workbook"] = workbook
    result["saved"] = saved
    progress("분석이 완료되었습니다.")
    return result


def rerun_ai(result: dict, progress: Progress) -> dict:
    if not result["catalog"]["ai_status"].get("ready"):
        raise RuntimeError("로컬 AI를 사용할 수 없습니다.")
    done_ids = set()
    audit = result["llm_audit"]
    if not audit.empty and {"case_id", "llm_status"}.issubset(audit.columns):
        done_ids = set(audit.loc[
            audit["llm_status"].astype(str).eq("VALIDATED_OUTPUT"), "case_id"
        ].astype(str))
    remaining = [
        case for case in result["llm_cases"]
        if str(case.get("case_id")) not in done_ids
    ]
    if not remaining:
        return result

    def on_ai_progress(done: int, total: int) -> None:
        progress(f"AI 보류·오류 사례 재검토 중: {done}/{total}건")

    updated_audit = analyze_cases(
        remaining, max_cases=len(remaining), on_progress=on_ai_progress
    )
    combined = pd.concat([audit, updated_audit], ignore_index=True)
    if "case_id" in combined.columns:
        combined = combined.drop_duplicates("case_id", keep="last").reset_index(drop=True)

    result["llm_audit"] = combined
    result["summary"] = merge_llm_audit(result["summary_before_llm"], combined)
    result["plain"] = build_plain_summary(
        result["summary"], result["evidence"],
        result["cross_detail"], result["catalog"]["notice"]
    )
    progress("재검토 결과 Excel 파일 저장 중...")
    result["workbook"] = _create_workbook(result, result["detailed"])
    result["saved"] = save_result_workbook(
        result["workbook"], result["catalog"]["notice"], len(result["summary"]),
        api_status=result["catalog"]["api_status"],
        gate_counts={
            str(k): int(v)
            for k, v in result["summary"]["gate_decision"].value_counts().items()
        },
        directory=RESULTS_DIR,
        filename_prefix="사업장_화학물질_규제판정",
    )
    return result
