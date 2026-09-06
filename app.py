from __future__ import annotations

import sys
from pathlib import Path

# 배포본에서는 판정 모듈을 engine 폴더에 모아 최상위 파일 수를 줄인다.
APP_ROOT = Path(__file__).resolve().parent
ENGINE_DIR = APP_ROOT / "engine"
if ENGINE_DIR.is_dir() and str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import pandas as pd
import streamlit as st

from company_engine import (
    DATA_DIR,
    build_result_workbook,
    korean_summary,
    load_approved_master,
    load_promoted_salt_rules,
    prepare_company_inventory,
    read_inventory,
    screen_company_inventory,
)
from cross_regime import (
    PENDING_DIR as CROSS_REGIME_PENDING_DIR,
    REGIME_DECISION_COLUMNS,
    add_regulatory_category_columns,
    fetch_cross_regime_candidates,
    load_approved_cross_regime_rules,
    screen_cross_regimes,
)
from regulatory_api import check_latest_notice, compare_with_master, download_notice_candidate
from local_llm import (
    analyze_cases,
    build_llm_review_cases,
    get_local_llm_status,
    merge_llm_audit,
)
from scope_engine import load_extended_scope_rules
from trust_gate import (
    CATEGORY_LOG_KO,
    GATE_LOG_KO,
    LEGAL_BASIS_KO,
    apply_trust_gate,
    build_category_search_log,
    build_legal_basis,
    cross_rule_availability,
)
from plain_report import build_decision_policy, build_plain_summary, build_reader_guide
from result_store import list_saved_results, read_saved_result, save_result_workbook
from structural_rule_drafting import (
    build_draft_review_hints,
    draft_and_cache,
    extract_no_cas_cross_regime_cases,
    extract_no_cas_master_cases,
)
from no_cas_scope import build_no_cas_scope_index, screen_no_cas_scopes, attach_no_cas_candidates

STRUCTURAL_DRAFT_CACHE = DATA_DIR / "pending_updates" / "structural_rule_drafts"
AI_REVIEW_AUTO_CEILING = 40
STRUCTURAL_DRAFT_BATCH_PER_RUN = 300

st.set_page_config(
    page_title="기업 화학물질 규제 판정",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root {--navy:#18354f;--blue:#236487;--teal:#2a7f76;--red:#bd3f3f;--amber:#c48418;--line:#dce6ec;}
    .block-container{padding-top:1.1rem;padding-bottom:3rem;max-width:1450px}
    h1,h2,h3{color:var(--navy)}
    .hero{background:linear-gradient(118deg,#173750 0%,#1f617c 62%,#2b7c73 100%);color:#fff;
          padding:27px 31px;border-radius:17px;margin-bottom:18px;box-shadow:0 6px 20px rgba(23,55,80,.14)}
    .hero h1{color:#fff;margin:0 0 7px;font-size:2rem}.hero p{margin:0;color:#eaf5f7;font-size:1.02rem}
    .step{background:#f7fafc;border:1px solid var(--line);border-radius:12px;padding:14px 17px;margin:7px 0 14px}
    .ok{border-left:5px solid var(--teal);background:#edf8f5;padding:12px 16px;border-radius:7px;margin:8px 0 14px}
    .warn{border-left:5px solid var(--amber);background:#fff8e8;padding:12px 16px;border-radius:7px;margin:8px 0 14px}
    .danger{border-left:5px solid var(--red);background:#fff0f0;padding:12px 16px;border-radius:7px;margin:8px 0 14px}
    div[data-testid="stMetric"]{background:white;border:1px solid var(--line);padding:13px 15px;border-radius:12px;
          box-shadow:0 2px 8px rgba(24,53,79,.05)}
    div[data-testid="stFileUploader"]{border:1px solid var(--line);border-radius:12px;padding:8px;background:#fbfdfe}
    </style>
    """,
    unsafe_allow_html=True,
)

@st.cache_resource
def load_regulatory_data():
    master, notice = load_approved_master()
    salt_rules = load_promoted_salt_rules(master)
    extended_rules = load_extended_scope_rules()
    cross_approved = load_approved_cross_regime_rules()
    return master, notice, salt_rules, extended_rules, cross_approved

@st.cache_data(ttl=3600, show_spinner=False)
def cached_api_status(local_notice: str):
    return compare_with_master(check_latest_notice(), local_notice)

@st.cache_data(ttl=3600, show_spinner=False)
def cached_cross_regime_api():
    return fetch_cross_regime_candidates()

@st.cache_data(ttl=60, show_spinner=False)
def cached_local_llm_status():
    return get_local_llm_status()

def download_local_file(path: Path, label: str):
    st.download_button(label, path.read_bytes(), file_name=path.name, width="stretch")

try:
    master, master_notice, salt_rules, extended_rules, cross_approved = load_regulatory_data()
except Exception as exc:
    st.error(f"승인 규제 마스터를 불러오지 못했습니다: {type(exc).__name__}: {exc}")
    st.stop()

api_status = cached_api_status(master_notice)
cross_candidates, cross_api_status = cached_cross_regime_api()
local_ai_status = cached_local_llm_status()
if api_status.get("sync_status") == "UPDATE_PENDING" and "candidate_download" not in st.session_state:
    st.session_state["candidate_download"] = download_notice_candidate(api_status)

st.markdown(
    """
    <div class="hero">
      <h1>기업 화학물질 규제 판정</h1>
      <p>직접 CAS 대조와 검증된 염류 구조를 핵심 자동판정 경로로 사용하고, 포괄 규제범위는 보수적으로 검토 대상으로 분류합니다.</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("현재 상태")
    sync = api_status.get("sync_status")
    if sync == "CURRENT":
        st.success(f"규제 데이터 최신 확인됨 ({master_notice})")
    elif sync == "UPDATE_PENDING":
        st.warning("새 고시가 발견됐지만 아직 검증 전이라 적용하지 않았습니다")
    else:
        st.warning("최신 여부 미확인 — 내장된 규제 데이터를 사용 중입니다")

    if local_ai_status.get("ready"):
        st.success(f"로컬 AI 사용 가능 ({local_ai_status.get('model')})")
    else:
        st.info("로컬 AI 미사용 (꺼져 있거나 아직 설치 전)")

    st.divider()
    st.subheader("이전 결과 파일")
    saved_files = list_saved_results()
    if not saved_files:
        st.caption("아직 저장된 판정 결과가 없습니다.")
    else:
        chosen_index = st.selectbox(
            "다시 받을 결과 파일",
            options=list(range(len(saved_files))),
            format_func=lambda idx: saved_files[idx].get("display_name") or saved_files[idx]["name"],
            label_visibility="collapsed",
        )
        target = saved_files[chosen_index]
        st.caption(target["name"])
        try:
            st.download_button(
                "다시 받기",
                read_saved_result(target["path"]),
                file_name=target["name"],
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                width="stretch",
            )
        except Exception as exc:
            st.warning(f"파일을 읽지 못했습니다: {type(exc).__name__}")

    st.divider()
    st.subheader("입력 예시")
    download_local_file(DATA_DIR / "sample_company_inventory.csv", "인벤토리 예시파일")
    st.caption("권장 열: CAS, 물질명, 함량(%), 용도, 연간취급량(kg), 판정기준일, 허가상태")

    st.divider()
    with st.expander("⚙️ 고급 설정 및 진단 정보"):
        st.caption("문제가 있어 원인을 확인하거나, 규제 데이터를 직접 관리할 때만 사용하세요.")
        st.markdown("**새로고침**")
        if st.button("최신 법규 다시 확인", width="stretch"):
            cached_api_status.clear()
            cached_cross_regime_api.clear()
            st.session_state.pop("candidate_download", None)
            st.rerun()
        st.caption("법제처 API에서 최신 고시·별표를 다시 받아옵니다.")
        if st.button(
            "승인 규칙 파일 다시 불러오기",
            width="stretch",
            help="cross_regime_rules_APPROVED.csv, broad_salt_rules.csv 등을 수정한 뒤 앱을 재시작하지 않고 반영하려면 눌러주세요.",
        ):
            load_regulatory_data.clear()
            st.rerun()
        st.caption("디스크에 저장된 승인 규칙 파일을 다시 읽습니다(API 호출 없음).")
        if st.button("로컬 AI 상태 다시 확인", width="stretch"):
            cached_local_llm_status.clear()
            st.rerun()

        st.markdown("**데이터 현황**")
        st.metric("승인 규제 마스터", master_notice)
        st.metric("인체유해성 규칙", f"{len(master):,}행")
        st.metric(
            "염류 매칭 규칙",
            f"{len(salt_rules):,}개",
            help="'~와 그 염류' 규정을 판정하기 위한 승인된 기준물질(모체구조) 규칙입니다. 염류 자동확정에는 검증된 구조가 필요합니다.",
        )
        st.metric(
            "구조범위 규칙",
            f"{len(extended_rules):,}개",
            help="일반 구조범위 규칙은 검토 후보 분류에만 사용하며 자동확정하지 않습니다.",
        )
        st.metric("사고대비·제한·금지물질 승인 규칙", f"{len(cross_approved):,}개")
        st.metric("검증 전 API 자동추출", f"{len(cross_candidates):,}개")

if api_status.get("sync_status") == "CURRENT":
    st.markdown(
        f'<div class="ok"><b>법규 상태:</b> 공식 API 확인 결과와 승인 규제 마스터가 모두 {master_notice}입니다.</div>',
        unsafe_allow_html=True,
    )
elif api_status.get("sync_status") == "UPDATE_PENDING":
    candidate = st.session_state.get("candidate_download", {})
    st.markdown(
        '<div class="danger"><b>새 고시 발견:</b> 공식 원문 후보를 자동 저장했지만, 검증 전 자료는 판정에 적용하지 않았습니다. '
        f"현재 결과는 승인된 {master_notice} 기준입니다. {candidate.get('message', '')}</div>",
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div class="warn"><b>API 최신 여부 미확인:</b> 현재 판정은 내장된 승인 규제 마스터를 사용합니다.</div>',
        unsafe_allow_html=True,
    )

st.subheader("1. 인벤토리 업로드")
st.markdown(
    '<div class="step">CSV 또는 Excel 파일을 올려주십시오. 직접 CAS 일치를 우선 대조하고, '~와 그 염류' 규정은 승인된 염류 규칙과 검증 구조로 확인합니다. 혼합물·반응생성물·화합물군 등은 자동확정하지 않고 보류합니다.</div>',
    unsafe_allow_html=True,
)

left, right = st.columns([1.3, 1])
with left:
    inventory_file = st.file_uploader("화학물질 인벤토리", type=["csv", "xlsx", "xls"], help="열 이름은 한글 또는 영문을 사용할 수 있습니다.")
with right:
    fetch_pubchem = st.toggle("누락 SMILES를 PubChem에서 자동 수집", value=True)
    st.caption("염류 자동판정을 위해서는 검증된 구조정보가 필요합니다.")
    use_local_ai = st.toggle(
        "어려운 보류 사례만 AI 자동검토",
        value=bool(local_ai_status.get("ready")),
        disabled=not bool(local_ai_status.get("ready")),
        help="AI는 포괄 규제범위 후보의 분류와 확인사항 정리에만 사용하며 자동확정에는 사용하지 않습니다.",
    )
    detailed_export = st.toggle(
        "결과 파일에 상세 감사자료 포함",
        value=False,
        help="AI 검토기록과 포괄 규제범위 후보는 항상 저장됩니다. 이 옵션을 켜면 게이트·검색기록·물질식별·PubChem 기록까지 추가합니다.",
    )

run = st.button("규제 판정 실행", type="primary", width="stretch", disabled=inventory_file is None)

if run:
    try:
        with st.status("물질 식별 및 규제 판정을 수행하고 있습니다.", expanded=True) as status:
            raw_inventory = read_inventory(inventory_file)
            if raw_inventory.empty:
                raise ValueError("업로드한 인벤토리에 물질 행이 없습니다.")
            st.write(f"인벤토리 {len(raw_inventory):,}개 행을 읽었습니다.")
            enriched, pubchem_audit = prepare_company_inventory(raw_inventory, fetch_pubchem=fetch_pubchem)
            st.write("CAS와 구조정보를 정리했습니다.")
            summary, evidence, review = screen_company_inventory(
                enriched, master, salt_rules, master_notice, extended_rules=extended_rules
            )
            cross_summary, cross_detail = screen_cross_regimes(enriched, cross_approved, cross_candidates)
            summary = summary.merge(cross_summary, on="material_id", how="left")
            summary["HUMAN_HAZARD_decision"] = summary["final_decision"]
            regime_cols = [
                "HUMAN_HAZARD_decision", "ACCIDENT_PREPAREDNESS_decision", "RESTRICTED_decision", "PROHIBITED_decision",
            ]
            def overall(row):
                values = [str(row.get(col, "")) for col in regime_cols]
                if "규제 해당" in values:
                    return "규제 해당"
                if any("해당 후보" in value for value in values):
                    return "규제 해당 후보"
                if any(value in {"추가정보 필요", "조건 검토 필요", "법규 데이터 확인 필요", "전문가 검토 필요", "물질정보 확인 실패", "정보 부족"} for value in values):
                    return "추가 확인 필요"
                return "확인 범위 내 비해당"
            summary["overall_decision"] = summary.apply(overall, axis=1)
            summary = add_regulatory_category_columns(summary)

            availability = cross_rule_availability(cross_approved, cross_candidates, cross_api_status)
            category_log = build_category_search_log(summary, cross_detail, availability, master_notice, len(master))
            legal_basis = build_legal_basis(evidence, cross_detail, master_notice)
            summary, gate_log = apply_trust_gate(summary, evidence, cross_detail, category_log, legal_basis, api_status)

            no_cas_index = build_no_cas_scope_index(master, master_notice)
            no_cas_candidates = screen_no_cas_scopes(enriched, no_cas_index)
            summary = attach_no_cas_candidates(summary, no_cas_candidates)
            if not no_cas_candidates.empty:
                st.write(
                    f"포괄 규제범위 후보 {len(no_cas_candidates):,}건을 찾아 분류·전문가 검토 대상으로 연결했습니다."
                )

            if use_local_ai:
                llm_cases = build_llm_review_cases(summary, cross_detail, evidence)
                initial_cap = min(len(llm_cases), AI_REVIEW_AUTO_CEILING)
                llm_audit = analyze_cases(llm_cases, max_cases=initial_cap)
            else:
                llm_cases = []
                llm_audit = pd.DataFrame()
            summary_before_llm = summary.copy()
            summary = merge_llm_audit(summary, llm_audit)
            plain = build_plain_summary(summary, evidence, cross_detail, master_notice)
            draft_hints = pd.DataFrame()

            workbook = build_result_workbook(
                summary, evidence, enriched, pubchem_audit, review, api_status, master_notice, len(master),
                cross_summary=summary[[
                    "material_id", "HUMAN_HAZARD_decision", "ACCIDENT_PREPAREDNESS_decision",
                    "RESTRICTED_decision", "PROHIBITED_decision", "applicable_regulatory_categories",
                    "review_regulatory_categories", "overall_decision",
                ]].copy(),
                cross_detail=cross_detail, cross_api_status=cross_api_status, llm_audit=llm_audit,
                llm_status=local_ai_status, gate_log=gate_log, category_log=category_log,
                legal_basis=legal_basis, draft_hints=draft_hints, no_cas_candidates=no_cas_candidates,
                detailed=bool(detailed_export),
            )
            saved = save_result_workbook(
                workbook, master_notice, len(summary), api_status=api_status,
                gate_counts={key: int(value) for key, value in summary["gate_decision"].value_counts().items()},
            )
            st.session_state["company_screen_result"] = {
                "summary": summary, "summary_before_llm": summary_before_llm, "evidence": evidence,
                "review": review, "enriched": enriched, "pubchem_audit": pubchem_audit, "workbook": workbook,
                "cross_detail": cross_detail, "llm_audit": llm_audit, "llm_cases": llm_cases,
                "gate_log": gate_log, "category_log": category_log, "legal_basis": legal_basis, "plain": plain,
                "draft_hints": draft_hints, "no_cas_candidates": no_cas_candidates, "saved_file": saved,
                "api_status": api_status, "master_notice": master_notice, "master_len": len(master),
            }
            status.update(label="규제 판정이 완료되었습니다.", state="complete", expanded=False)
    except Exception as exc:
        st.error(f"판정 중 오류가 발생했습니다: {type(exc).__name__}: {exc}")

result = st.session_state.get("company_screen_result")
if result:
    summary = result["summary"]
    st.subheader("2. 판정 결과")
    with st.expander("결과 읽는 방법과 용어 설명"):
        st.markdown(
            "**안전 원칙:** 자동확정은 직접 CAS 일치 또는 검증된 염류 구조만 허용합니다. 포괄 규제범위와 AI 판단은 보류·검토 보조에만 사용합니다."
        )
        st.dataframe(build_decision_policy(), hide_index=True, width="stretch")
        st.markdown("**결과파일에 나오는 주요 용어**")
        st.dataframe(build_reader_guide(), hide_index=True, width="stretch")

    counts = summary["gate_decision"].value_counts()
    held = int(sum(value for key, value in counts.items() if "보류" in str(key)))
    metrics = [
        ("전체", len(summary)),
        ("규제 해당(자동 확정)", int(counts.get("규제 해당(자동 확정)", 0))),
        ("보류", held),
        ("확인 범위 내 비해당", int(counts.get("확인 범위 내 비해당", 0))),
    ]
    cols = st.columns(len(metrics))
    for col, (label, value) in zip(cols, metrics):
        col.metric(label, f"{value:,}개")

    plain = result.get("plain", pd.DataFrame())
    decision_filter = st.multiselect(
        "결과 필터",
        options=plain["판정결과"].dropna().unique().tolist(),
        default=plain["판정결과"].dropna().unique().tolist(),
    )
    st.dataframe(
        plain[plain["판정결과"].isin(decision_filter)], hide_index=True, width="stretch",
        height=min(620, 95 + 42 * max(1, len(plain))),
    )

    with st.expander("판정 상세 (매칭·게이트·AI 검토)"):
        display = korean_summary(summary)
        for optional_column in ["로컬 AI 평가", "AI 해석 범위유형", "AI 후보 지정번호", "AI 검토 설명", "AI 원문 근거"]:
            if optional_column not in display.columns:
                display[optional_column] = ""
        columns = [
            "인벤토리 ID", "입력 물질명", "입력 CAS", "게이트 판정", "보류 사유", "구조근거 검증",
            "인체급성 판정", "인체만성 판정", "생태유해성 판정", "사고대비물질 판정",
            "제한물질 판정", "금지물질 판정", "규제 매칭 방식", "PubChem 근거", "로컬 AI 평가",
            "AI 후보 지정번호", "규칙 관계 분류", "AI 검토 설명",
        ]
        st.dataframe(display[[c for c in columns if c in display.columns]], hide_index=True, width="stretch")

    saved = result.get("saved_file", {})
    st.download_button(
        "판정 결과 Excel 다운로드", result["workbook"],
        file_name=saved.get("file_name") or f"기업_화학물질_규제판정_{master_notice}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", type="primary", width="stretch",
    )

    with st.expander("물질별 법률근거", expanded=True):
        basis = result.get("legal_basis", pd.DataFrame())
        if basis is None or basis.empty:
            st.info("자동 확정에 사용할 수 있는 법률근거 행이 없습니다.")
        else:
            st.dataframe(basis.rename(columns=LEGAL_BASIS_KO), hide_index=True, width="stretch")

    with st.expander("CAS 1차 카테고리 검색기록 (5개 규제)"):
        category_log = result.get("category_log", pd.DataFrame())
        if category_log is None or category_log.empty:
            st.info("검색기록이 없습니다.")
        else:
            st.dataframe(category_log.rename(columns=CATEGORY_LOG_KO), hide_index=True, width="stretch")

    with st.expander("신뢰 게이트 단계별 기록"):
        gate_log = result.get("gate_log", pd.DataFrame())
        if gate_log is None or gate_log.empty:
            st.info("게이트 기록이 없습니다.")
        else:
            st.dataframe(gate_log.rename(columns=GATE_LOG_KO), hide_index=True, width="stretch")

    no_cas_candidates = result.get("no_cas_candidates", pd.DataFrame())
    if no_cas_candidates is not None and not no_cas_candidates.empty:
        with st.expander("포괄 규제범위 후보", expanded=True):
            st.warning(
                "구성성분 CAS·기준물질/원문 CAS 또는 물질명으로 연결된 검토 후보입니다. 직접 CAS 일치 또는 검증된 염류 구조가 아니면 자동확정하지 않습니다. 혼합물·반응생성물은 SDS·조성·제조정보 확인이 필요합니다."
            )
            st.dataframe(
                no_cas_candidates.rename(columns={
                    "material_id":"인벤토리 ID", "chemical_name":"회사 물질명", "cas":"회사 CAS",
                    "designation_id":"지정번호", "regulatory_name":"후보 규제명",
                    "scope_type":"범위 유형", "candidate_match_type":"후보 근거 유형",
                    "candidate_reason":"보류 사유", "source_text":"규제 원문",
                }), hide_index=True, width="stretch"
            )

st.markdown(
    '<div class="warn"><b>판정 원칙:</b> 직접 CAS와 검증된 염류 구조만 자동확정 경로입니다. 혼합물·반응생성물·화합물군·UVCB·기준물질 CAS·이름 유사성은 자동확정하지 않습니다. <b>보류</b>는 규제 비해당을 뜻하지 않습니다.</div>',
    unsafe_allow_html=True,
)
