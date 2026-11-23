from __future__ import annotations

import sys
from pathlib import Path
from contextlib import contextmanager
from html import escape

# 배포본에서는 판정 모듈을 engine 폴더에 모아 최상위 파일 수를 줄인다.
APP_ROOT = Path(__file__).resolve().parent
ENGINE_DIR = APP_ROOT / "engine"
if ENGINE_DIR.is_dir() and str(ENGINE_DIR) not in sys.path:
    sys.path.insert(0, str(ENGINE_DIR))

import pandas as pd
import streamlit as st

from company_engine import (
    DATA_DIR,
    korean_summary,
    load_approved_master,
    load_promoted_salt_rules,
    read_inventory,
)
from cross_regime import (
    PENDING_DIR as CROSS_REGIME_PENDING_DIR,
    REGIME_DECISION_COLUMNS,
    fetch_cross_regime_candidates,
    load_approved_cross_regime_rules,
)
from regulatory_api import check_latest_notice, compare_with_master, download_notice_candidate
from local_llm import get_local_llm_status
from scope_engine import load_extended_scope_rules
from trust_gate import (
    CATEGORY_LOG_KO,
    GATE_LOG_KO,
    LEGAL_BASIS_KO,
)
from result_store import list_saved_results, read_saved_result, save_result_workbook
from screening_pipeline import (
    AI_REVIEW_AUTO_CEILING,
    result_guide,
    rerun_ai,
    run_screening,
)


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
    /* 화면 중앙에 로딩 상태를 안내하는 애니메이션 */
    .app-loading-overlay{
        position:fixed;inset:0;z-index:999999;display:flex;
        justify-content:center;align-items:center;
        background:rgba(17,37,55,.42);backdrop-filter:blur(3px);
    }
    .app-loading-card{
        width:min(90vw,360px);padding:36px 30px 32px;text-align:center;
        background:#fff;border:1px solid #dce6ec;border-radius:20px;
        box-shadow:0 18px 55px rgba(8,28,44,.24);
    }
    .app-loading-spinner{
        width:62px;height:62px;margin:0 auto 20px;border-radius:50%;
        border:6px solid #e4edf2;border-top-color:#236487;
        animation:app-loading-spin .85s linear infinite;
    }
    .app-loading-title{color:#18354f;font-size:1.45rem;font-weight:750;letter-spacing:.02em}
    .app-loading-dots{display:flex;gap:5px;justify-content:center;margin:10px 0 12px}
    .app-loading-dots span{
        width:7px;height:7px;border-radius:50%;background:#2a7f76;
        animation:app-loading-pulse 1.15s ease-in-out infinite;
    }
    .app-loading-dots span:nth-child(2){animation-delay:.15s}
    .app-loading-dots span:nth-child(3){animation-delay:.3s}
    .app-loading-detail{color:#527082;font-size:.94rem;line-height:1.55;margin:0}
    @keyframes app-loading-spin{to{transform:rotate(360deg)}}
    @keyframes app-loading-pulse{
        0%,65%,100%{opacity:.3;transform:translateY(0)}
        30%{opacity:1;transform:translateY(-5px)}
    }
    @media (prefers-reduced-motion:reduce){
        .app-loading-spinner,.app-loading-dots span{animation:none}
    }
    </style>
    """,
    unsafe_allow_html=True,
)


@contextmanager
def loading_overlay(title: str = "로딩 중", detail: str = "데이터를 처리하고 있습니다."):
    """긴 작업 동안 로딩 애니메이션을 표시하고 예외 발생 시에도 제거한다."""
    placeholder = st.empty()
    placeholder.markdown(
        f"""
        <div class="app-loading-overlay" role="status" aria-live="polite">
          <div class="app-loading-card">
            <div class="app-loading-spinner" aria-hidden="true"></div>
            <div class="app-loading-title">{escape(title)}</div>
            <div class="app-loading-dots" aria-hidden="true">
              <span></span><span></span><span></span>
            </div>
            <p class="app-loading-detail">{escape(detail)}</p>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    try:
        yield
    finally:
        placeholder.empty()


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


with loading_overlay("로딩 중", "규제 마스터와 최신 법규 정보를 불러오고 있습니다."):
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
      <p>인벤토리 업로드 한 번으로 물질을 식별하고, 로컬 AI와 검증 규칙을 결합해 인체등유해성·사고대비·제한·금지물질 규제를 함께 확인합니다.</p>
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
            help="cross_regime_rules_APPROVED.csv, broad_salt_rules.csv 등을 수정한 뒤 "
            "앱을 재시작하지 않고 반영하려면 눌러주세요.",
        ):
            load_regulatory_data.clear()
            st.rerun()
        st.caption("디스크에 저장된 승인 규칙 파일을 다시 읽습니다(API 호출 없음).")
        if st.button("로컬 AI 상태 다시 확인", width="stretch"):
            cached_local_llm_status.clear()
            st.rerun()

        st.markdown("**데이터 현황**")
        st.metric("승인 규제 마스터", master_notice)
        st.metric(
            "인체유해성 규칙",
            f"{len(master):,}행",
            help="인체등유해성물질 목록입니다. 혼합물·화합물·반응생성물·중합체·유도체는 "
            "대부분 CAS가 여기 직접 등재되어 있어 별도 규칙이 필요 없습니다.",
        )
        st.metric(
            "염류 매칭 규칙",
            f"{len(salt_rules):,}개",
            help="CAS가 목록에 직접 없어도 '이 물질이 ○○의 염류다'를 화학구조로 판단하기 위한 "
            "승인된 기준 물질(모체구조) 수입니다. 염류만 구조 대조가 필요해서 별도로 관리합니다.",
        )
        st.metric(
            "구조범위 규칙",
            f"{len(extended_rules):,}개",
            help="탄소사슬 범위·치환기 등 SMARTS 화학패턴으로 판단하는 규칙입니다. "
            "현재 0개면 아직 하나도 승인되지 않은 것으로, 이런 유형의 물질은 "
            "자동 확정 대신 '전문가 검토 필요'로 분류됩니다.",
        )
        st.metric(
            "사고대비·제한·금지물질 승인 규칙",
            f"{len(cross_approved):,}개",
            help="사고대비물질·제한물질·금지물질 목록에서, 사람이 확인을 마쳐 판정에 실제로 "
            "쓸 수 있게 승인해둔 CAS·기준값 개수입니다. "
            "예: 포름알데히드 1%(사고대비), 벤지딘 0.1%(금지). "
            "파일을 고쳤는데도 그대로면 위 '승인 규칙 파일 다시 불러오기'를 눌러주세요.",
        )
        st.metric(
            "검증 전 API 자동추출",
            f"{len(cross_candidates):,}개",
            help="법제처 API가 이번 세션에 실시간으로 받아온 CAS·기준값 후보 개수입니다. "
            "아직 사람이 확인하지 않아 판정에는 쓰이지 않고, 승인되면 위 숫자로 옮겨갑니다.",
        )
        if api_status.get("official_notice"):
            st.caption(f"공식 확인 고시: {api_status.get('official_notice')}")

        with st.expander("타 규제체계 API 상세 조회 결과"):
            if cross_api_status.empty:
                st.caption("조회 결과가 없습니다.")
            else:
                status_view = cross_api_status.rename(
                    columns={
                        "regime_label": "대상 고시",
                        "status_ko": "조회 상태",
                        "official_notice": "공식 고시",
                        "effective_date": "시행일",
                        "candidate_rule_rows": "추출 후보 행",
                        "candidate_status_ko": "후보 상태",
                    }
                )
                columns = [
                    col for col in ["대상 고시", "조회 상태", "공식 고시", "시행일", "추출 후보 행", "후보 상태"]
                    if col in status_view.columns
                ]
                st.dataframe(status_view[columns], hide_index=True, width="stretch")
            if "appendix_status_ko" in cross_api_status.columns:
                st.caption("별표(부속서) 자동추출 결과")
                appendix_view = cross_api_status[
                    [col for col in ["regime_label", "appendix_status_ko", "appendix_rows"] if col in cross_api_status.columns]
                ].rename(
                    columns={
                        "regime_label": "대상 고시",
                        "appendix_status_ko": "별표 인식 상태",
                        "appendix_rows": "추출 행 수",
                    }
                )
                st.dataframe(appendix_view, hide_index=True, width="stretch")
                for _, row in cross_api_status.iterrows():
                    for item in row.get("appendix_attachments", []) or []:
                        if item.get("status") not in {"PARSED"}:
                            st.caption(
                                f"⚠ {row.get('regime_label','')} · {item.get('appendix','')}: "
                                f"{item.get('status')} {item.get('message','')}"
                            )
                st.caption(
                    "자동 추출된 별표 행은 항상 '검증 전 후보'로만 들어갑니다. "
                    "승인 규칙(cross_regime_rules_APPROVED.csv)에 반영하려면 검토 후 승인 처리가 필요합니다."
                )

        with st.expander("별표 자동추출 후보 검토"):
            appendix_files = sorted(
                CROSS_REGIME_PENDING_DIR.glob("appendix_candidates_*.csv"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            ) if CROSS_REGIME_PENDING_DIR.exists() else []
            if not appendix_files:
                st.caption("API에서 자동 추출된 별표 후보가 아직 없습니다. LAW_OC 설정 후 '최신 법규 다시 확인'을 눌러주세요.")
            else:
                chosen_appendix = st.selectbox(
                    "검토할 별표 후보 파일",
                    options=[p.name for p in appendix_files],
                    label_visibility="collapsed",
                    key="appendix_candidate_select",
                )
                target_path = next(p for p in appendix_files if p.name == chosen_appendix)
                preview = pd.read_csv(target_path, dtype=str).fillna("")
                n_inferred = preview.get("designation_id_inferred", pd.Series(dtype=str)).eq("True").sum()
                st.caption(f"{len(preview)}행 추출됨" + (f" · 고유번호 자동인식 실패 {n_inferred}행" if n_inferred else ""))
                st.dataframe(
                    preview[[c for c in ["designation_id", "substance_name", "direct_cas",
                                          "concentration_threshold_pct", "concentration_operator",
                                          "regime"] if c in preview.columns]].head(20),
                    hide_index=True, width="stretch",
                )
                st.download_button(
                    "이 후보 CSV 다운로드 (검토 후 승인 파일에 병합)",
                    target_path.read_bytes(),
                    file_name=target_path.name,
                    width="stretch",
                )
                st.caption(
                    "검토 후 approval_status를 APPROVED로 바꿔 "
                    "cross_regime_rules_APPROVED.csv에 합치면 다음 실행부터 확정 판정에 사용됩니다."
                )

        with st.expander("로컬 AI 설정 방법"):
            st.write("`setup_local_ai.bat`을 실행하면 Ollama와 `qwen3.5:4b` 모델을 확인·설치합니다.")
            st.caption("인벤토리와 법규 원문은 이 PC의 localhost Ollama로만 전달됩니다.")
            if local_ai_status.get("message"):
                st.caption(local_ai_status.get("message"))

        with st.expander("법규 API 설정 방법"):
            st.write("프로그램 폴더의 `setup_law_api.bat`을 실행하고 발급받은 `LAW_OC` 값을 입력합니다.")
            st.caption("API 인증값은 결과파일이나 화면에 기록되지 않습니다.")

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
        '<div class="warn"><b>API 최신 여부 미확인:</b> 현재 판정은 내장된 승인 규제 마스터를 사용합니다. '
        '실무 사용 전 LAW_OC를 설정하고 최신 법규 확인을 실행하십시오.</div>',
        unsafe_allow_html=True,
    )

st.subheader("1. 인벤토리 업로드")
st.markdown(
    '<div class="step">CSV 또는 Excel 파일을 올려주십시오. CAS가 있으면 PubChem에서 정확한 CAS 동의어를 확인한 뒤 '
    'SMILES를 자동 수집합니다. 제한·금지·사고대비 규칙에 용도·수량·시행일 조건이 있으면 입력값을 함께 적용하고, '
    '누락된 정보는 <b>추가정보 필요</b>로 표시합니다.</div>',
    unsafe_allow_html=True,
)

left, right = st.columns([1.3, 1])
with left:
    inventory_file = st.file_uploader(
        "화학물질 인벤토리",
        type=["csv", "xlsx", "xls"],
        help="열 이름은 한글 또는 영문을 사용할 수 있습니다.",
    )
with right:
    fetch_pubchem = st.toggle("누락 SMILES를 PubChem에서 자동 수집", value=True)
    st.caption("사용자가 입력한 SMILES가 있으면 해당 값을 우선 사용합니다.")
    st.caption("CAS 없이 물질명만 있는 행은 자동 확정하지 않고 검토 대상으로 보냅니다.")
    use_local_ai = st.toggle(
        "어려운 보류 사례만 AI 자동검토",
        value=bool(local_ai_status.get("ready")),
        disabled=not bool(local_ai_status.get("ready")),
        help="규칙 판정을 먼저 완료한 뒤 구조범위나 복잡한 조건 해석이 필요한 보류 사례만 "
        "AI로 검토합니다. 확정 물질과 단순 입력 누락은 AI에 보내지 않습니다.",
    )
    st.caption(
        "먼저 전체 물질을 규칙으로 빠르게 판정하고, AI가 도움을 줄 수 있는 어려운 보류 "
        f"사례만 자동 선별합니다. 한 번에 최대 {AI_REVIEW_AUTO_CEILING}건을 처리합니다."
    )

run = st.button(
    "규제 판정 실행",
    type="primary",
    width="stretch",
    disabled=inventory_file is None,
)

if run:
    try:
        with loading_overlay(
            "로딩 중", "인벤토리를 분석하고 화학물질 규제 판정 결과를 생성하고 있습니다."
        ), st.status("물질 식별 및 규제 판정을 수행하고 있습니다.", expanded=True) as status:
            raw_inventory = read_inventory(inventory_file)
            if raw_inventory.empty:
                raise ValueError("업로드한 인벤토리에 물질 행이 없습니다.")
            st.write(f"인벤토리 {len(raw_inventory):,}개 행을 읽었습니다.")

            _report_llm_progress = None
            if use_local_ai:
                llm_progress_bar = st.progress(0.0)
                llm_progress_text = st.empty()

                def _report_llm_progress(done: int, total: int) -> None:
                    llm_progress_bar.progress(done / total if total else 1.0)
                    llm_progress_text.write(f"검토 필요 사례 해석 중: {done}/{total}건")

            catalog = {
                "master": master, "notice": master_notice, "salt_rules": salt_rules,
                "extended": extended_rules, "cross_approved": cross_approved,
                "api_status": api_status, "cross_candidates": cross_candidates,
                "cross_api_status": cross_api_status, "ai_status": local_ai_status,
            }
            result = run_screening(
                raw_inventory, catalog, fetch_pubchem=fetch_pubchem,
                use_local_ai=use_local_ai, progress=st.write,
                ai_progress=_report_llm_progress, ai_ceiling=AI_REVIEW_AUTO_CEILING,
            )
            if use_local_ai:
                llm_progress_bar.progress(1.0)
            if len(result["llm_cases"]) > AI_REVIEW_AUTO_CEILING:
                st.write(
                    f"검토 필요 물질이 {len(result['llm_cases'])}건이라 안전 상한 "
                    f"{AI_REVIEW_AUTO_CEILING}건까지만 우선 처리했습니다. "
                    "나머지는 결과 화면의 버튼으로 이어서 처리할 수 있습니다."
                )
            summary = result["summary"]
            saved = save_result_workbook(
                result["workbook"],
                master_notice,
                len(summary),
                api_status=api_status,
                gate_counts={
                    key: int(value)
                    for key, value in summary["gate_decision"].value_counts().items()
                },
            )
            result.update({
                "saved_file": saved,
                "api_status": api_status,
                "master_notice": master_notice,
                "master_len": len(master),
            })
            st.session_state["company_screen_result"] = result
            st.write(f"결과 파일을 보존 폴더에 저장했습니다: {saved['file_name']}")
            status.update(label="규제 판정이 완료되었습니다.", state="complete", expanded=False)
    except Exception as exc:
        st.error(f"판정 중 오류가 발생했습니다: {type(exc).__name__}: {exc}")

result = st.session_state.get("company_screen_result")
if result:
    summary = result["summary"]

    unrecognized = []
    hwp_only = []
    if not cross_api_status.empty:
        for _, row in cross_api_status.iterrows():
            for item in row.get("appendix_attachments", []) or []:
                label = f"{row.get('regime_label','')} · {item.get('appendix','')}"
                if item.get("status") == "TABLE_FORMAT_NOT_RECOGNIZED":
                    unrecognized.append(label)
                elif item.get("status") == "PDF_LINK_MISSING":
                    hwp_only.append(label)
    if unrecognized:
        st.warning(
            "⚠ 법규 원문의 별표 표 형식이 바뀌어서 자동으로 못 읽은 항목이 있습니다: "
            + "; ".join(unrecognized)
            + ". 이 별표는 이번 판정에 반영되지 않았습니다. PDF를 다시 확인해 주세요."
        )
    if hwp_only:
        st.warning(
            "⚠ PDF가 없고 HWP 파일만 제공되어 자동으로 읽지 못한 별표가 있습니다: "
            + "; ".join(hwp_only)
            + ". 이 별표는 이번 판정에 반영되지 않았습니다. 해당 고시의 HWP를 PDF로 "
            "변환해서 올려주시면 반영해드릴 수 있습니다."
        )

    st.subheader("2. 판정 결과")
    with st.expander("결과 읽는 방법과 용어 설명"):
        st.markdown(
            "**안전 원칙:** 공식 기준으로 확실히 입증된 경우만 규제 해당으로 표시합니다. "
            "AI는 관련 후보와 확인할 내용을 정리할 뿐, 최종 규제 해당·비해당을 결정하지 않습니다."
        )
        st.dataframe(result_guide(), hide_index=True, width="stretch")
    plain = result.get("plain", pd.DataFrame())
    counts = plain["판정결과"].value_counts()
    metrics = [
        ("전체", len(plain)),
        ("규제 해당", int(counts.get("규제 해당", 0))),
        ("기준 미만", int(counts.get("기준 미만", 0))),
        ("비해당", int(counts.get("비해당", 0))),
        ("보류", int(counts.get("보류(추가 확인 필요)", 0))),
    ]
    cols = st.columns(len(metrics))
    for col, (label, value) in zip(cols, metrics):
        col.metric(label, f"{value:,}개")

    st.caption("규제 카테고리별 확정 해당 건수")
    category_cols = st.columns(len(REGIME_DECISION_COLUMNS))
    for col, (decision_column, label) in zip(
        category_cols, REGIME_DECISION_COLUMNS.items()
    ):
        col.metric(label, f"{int(summary[decision_column].eq('규제 해당').sum()):,}개")

    decision_filter = st.multiselect(
        "결과 필터",
        options=plain["판정결과"].dropna().unique().tolist(),
        default=plain["판정결과"].dropna().unique().tolist(),
    )
    shown = plain[plain["판정결과"].isin(decision_filter)].copy()
    for threshold_col in ("법규 인체급성 기준(%)", "법규 인체만성 기준(%)", "법규 생태 기준(%)"):
        shown[threshold_col] = pd.to_numeric(shown[threshold_col], errors="coerce")
    st.dataframe(
        shown,
        hide_index=True,
        width="stretch",
        height=min(620, 95 + 42 * max(1, len(plain))),
        column_config={
            "함량(%)": st.column_config.NumberColumn(format="%.4g"),
            "판단근거": st.column_config.TextColumn(width="large"),
            "규제 카테고리": st.column_config.TextColumn(width="medium"),
        },
    )
    st.caption(
        "「규제 해당」은 저장된 승인 규칙과 확인된 근거를 기준으로 한 표시이며 최종 법적 확정이 아닙니다. "
        "「보류」는 비규제를 뜻하지 않으며, 판정에 필요한 근거가 아직 확보되지 않았다는 의미입니다. "
        "법규 최신성 경고는 판정결과를 바꾸지 않고 검증 상태·추가 확인사항에 표시됩니다."
    )

    with st.expander("판정 상세 (매칭·게이트·AI 검토)"):
        display = korean_summary(summary).rename(columns={
            "overall_decision": "통합 판정",
            "HUMAN_HAZARD_decision": "인체등유해성 판정",
            "ACCIDENT_PREPAREDNESS_decision": "사고대비물질 판정",
            "RESTRICTED_decision": "제한물질 판정",
            "PROHIBITED_decision": "금지물질 판정",
        })
        # Local-AI output is optional.  A disabled model, an empty review queue, or
        # a failed model batch must not prevent deterministic results from showing.
        for optional_column in ["로컬 AI 평가", "AI 해석 범위유형", "AI 후보 지정번호", "AI 검토 설명", "AI 원문 근거"]:
            if optional_column not in display.columns:
                display[optional_column] = ""
        st.dataframe(
            display[
                [
                    "인벤토리 ID",
                    "입력 물질명",
                    "입력 CAS",
                    "게이트 판정",
                    "보류 사유",
                    "구조근거 검증",
                    "인체급성 판정",
                    "인체만성 판정",
                    "생태유해성 판정",
                    "사고대비물질 판정",
                    "제한물질 판정",
                    "금지물질 판정",
                    "규제 매칭 방식",
                    "PubChem 근거",
                    "로컬 AI 평가",
                    "AI 후보 지정번호",
                    "규칙 관계 분류",
                    "AI 검토 설명",
                ]
            ],
            hide_index=True,
            width="stretch",
            column_config={"PubChem 근거": st.column_config.LinkColumn("PubChem 근거")},
        )

    saved = result.get("saved_file", {})
    st.download_button(
        "판정 결과 Excel 다운로드",
        result["workbook"],
        file_name=saved.get("file_name") or f"기업_화학물질_규제판정_{master_notice}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        type="primary",
        width="stretch",
    )
    if saved.get("path"):
        st.caption(f"이 결과는 `{saved['path']}`에 보존되었습니다. 기존 파일은 덮어쓰지 않습니다.")

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
            st.dataframe(
                category_log.rename(columns=CATEGORY_LOG_KO), hide_index=True, width="stretch"
            )

    with st.expander("신뢰 게이트 단계별 기록"):
        gate_log = result.get("gate_log", pd.DataFrame())
        if gate_log is None or gate_log.empty:
            st.info("게이트 기록이 없습니다.")
        else:
            st.dataframe(gate_log.rename(columns=GATE_LOG_KO), hide_index=True, width="stretch")

    with st.expander("규제 근거 상세보기"):
        if result["evidence"].empty:
            st.info("일치한 규제 근거가 없습니다.")
        else:
            evidence_view = result["evidence"].rename(
                columns={
                    "material_id": "인벤토리 ID",
                    "match_type": "매칭 방식",
                    "designation_id": "지정번호",
                    "regulatory_substance_name": "규제 물질명",
                    "acute_threshold_pct": "급성 기준(%)",
                    "chronic_threshold_pct": "만성 기준(%)",
                    "eco_threshold_pct": "생태 기준(%)",
                    "source_text": "규제 원문",
                }
            )
            st.dataframe(
                evidence_view[
                    [
                        "인벤토리 ID",
                        "매칭 방식",
                        "지정번호",
                        "규제 물질명",
                        "급성 기준(%)",
                        "만성 기준(%)",
                        "생태 기준(%)",
                        "규제 원문",
                    ]
                ],
                hide_index=True,
                width="stretch",
            )

    draft_hints = result.get("draft_hints", pd.DataFrame())
    no_cas_candidates = result.get("no_cas_candidates", pd.DataFrame())
    if no_cas_candidates is not None and not no_cas_candidates.empty:
        with st.expander("CAS 없는 총칭·혼합물·반응생성물 후보", expanded=True):
            st.warning(
                "구성성분 CAS 또는 물질명으로 연결된 후보입니다. 단일 구성성분 보유만으로 "
                "혼합물·반응생성물과 동일하다고 확정하지 않으며, AI 설명과 SDS·조성·제조정보 "
                "확인이 필요합니다."
            )
            st.dataframe(
                no_cas_candidates.rename(columns={
                    "material_id":"인벤토리 ID", "chemical_name":"회사 물질명", "cas":"회사 CAS",
                    "designation_id":"지정번호", "regulatory_name":"CAS 없는 규제명",
                    "scope_type":"범위 유형", "candidate_match_type":"후보 근거 유형",
                    "candidate_reason":"보류 사유", "source_text":"규제 원문",
                }), hide_index=True, width="stretch"
            )
    if draft_hints is not None and not draft_hints.empty:
        st.warning(
            f"⚠ CAS 없이 화합물군·구조범위로 지정된 법규 항목과 구조가 일치하는 물질 "
            f"{len(draft_hints)}건이 있습니다. AI가 만든 미검증 초안이며 판정에는 반영되지 "
            "않았습니다 — 화학 전문가 확인이 필요합니다."
        )
        st.dataframe(
            draft_hints.rename(
                columns={
                    "cas": "CAS번호",
                    "chemical_name": "물질명",
                    "designation_id": "지정번호",
                    "regime": "규제 카테고리",
                    "explanation_ko": "AI 설명(미검증)",
                    "confidence": "확신도",
                    "source_text": "규제 원문",
                }
            ),
            hide_index=True,
            width="stretch",
        )

    with st.expander("로컬 AI 검토기록"):
        llm_cases_all = result.get("llm_cases", [])
        # Only validated outputs count as completed. Errors and missing outputs
        # stay in the queue and are retried on demand.
        processed_ids = set()
        failed_count = 0
        if not result["llm_audit"].empty and "case_id" in result["llm_audit"].columns:
            status_series = result["llm_audit"].get(
                "llm_status", pd.Series("", index=result["llm_audit"].index)
            ).astype(str)
            processed_ids = set(
                result["llm_audit"].loc[status_series.eq("VALIDATED_OUTPUT"), "case_id"].astype(str)
            )
            failed_count = int((~status_series.eq("VALIDATED_OUTPUT")).sum())
        remaining_cases = [
            case for case in llm_cases_all if str(case.get("case_id")) not in processed_ids
        ]
        if result["llm_audit"].empty:
            st.info("이번 실행에서는 로컬 AI 분석을 수행하지 않았습니다.")
        else:
            st.dataframe(result["llm_audit"], hide_index=True, width="stretch")
        if failed_count:
            st.warning(
                f"AI 응답 오류 {failed_count}건은 완료로 처리하지 않았습니다. "
                "아래 버튼을 누르면 오류 건부터 자동 재시도합니다."
            )
        if remaining_cases:
            st.warning(
                f"재시도 또는 추가 검토가 필요한 사례가 {len(remaining_cases)}건 있습니다."
            )
            if st.button(f"오류·미처리 {len(remaining_cases)}건 AI 재검토", width="stretch"):
                retry_bar = st.progress(0.0)
                retry_text = st.empty()

                def _report_retry(done: int, total: int) -> None:
                    retry_bar.progress(done / total if total else 1.0)
                    retry_text.write(f"AI 재검토 중: {done}/{total}건")

                with loading_overlay("로딩 중", "오류·미처리 물질을 AI가 다시 검토하고 있습니다."):
                    # Re-check the AI connection now; it may have changed since the run.
                    result["catalog"] = {**result["catalog"], "ai_status": get_local_llm_status()}
                    result = rerun_ai(result, ai_progress=_report_retry)
                    saved_again = save_result_workbook(
                        result["workbook"],
                        result["master_notice"],
                        len(result["summary"]),
                        api_status=result["api_status"],
                        gate_counts={
                            key: int(value)
                            for key, value in result["summary"]["gate_decision"].value_counts().items()
                        },
                    )
                    result["saved_file"] = saved_again
                    st.session_state["company_screen_result"] = result
                    retry_bar.progress(1.0)
                st.rerun()

st.markdown(
    '<div class="warn"><b>판정 범위:</b> 인체등유해성은 내장 승인 마스터를, 사고대비·제한·금지물질은 승인 규칙과 공식 API 후보를 사용합니다. '
    '<b>허가물질은 이 프로그램의 판정 범위에 포함되지 않습니다</b>(별도로 확인 필요). '
    'API 자동 추출행은 검증 전 “해당 후보”이며, 용도·수량·시행일·예외조건을 포함한 최종 법률의견을 의미하지 않습니다. '
    '<b>보류</b>는 규제 비해당을 뜻하지 않으며, 자동 확정에 필요한 근거가 아직 확보되지 않았다는 의미입니다.</div>',
    unsafe_allow_html=True,
)
