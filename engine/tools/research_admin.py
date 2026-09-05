from __future__ import annotations

from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

from dashboard_engine import (
    DATA_DIR,
    DEFAULT_CASES,
    DEFAULT_RULES,
    apply_threshold_table,
    calculate_framework,
    dataframe_to_excel,
    molecular_image,
    prepare_inventory,
    read_uploaded_table,
    screen_inventory,
)


st.set_page_config(
    page_title="Broad-Salt Regulatory Screening",
    page_icon="🧪",
    layout="wide",
    initial_sidebar_state="expanded",
)

st.markdown(
    """
    <style>
    :root { --navy:#16324F; --blue:#245B8A; --teal:#2A7F7F; --amber:#C78616; --red:#B84242; }
    .block-container {padding-top: 1.2rem; padding-bottom: 3rem; max-width: 1500px;}
    h1, h2, h3 {color: var(--navy);}
    .hero {background:linear-gradient(120deg,#15334f 0%,#1e5b77 65%,#2b7b75 100%);
           color:white;border-radius:16px;padding:25px 30px;margin-bottom:18px;}
    .hero h1 {color:white;margin:0 0 7px 0;font-size:2rem;}
    .hero p {margin:0;color:#e8f3f7;font-size:1rem;}
    .notice {border-left:5px solid #c78616;background:#fff8e8;padding:12px 16px;border-radius:6px;margin:8px 0 15px;}
    .scope {border-left:5px solid #2a7f7f;background:#eef9f7;padding:12px 16px;border-radius:6px;margin:8px 0 15px;}
    .danger {border-left:5px solid #b84242;background:#fff1f1;padding:12px 16px;border-radius:6px;margin:8px 0 15px;}
    div[data-testid="stMetric"] {background:white;border:1px solid #dbe5ec;padding:12px 15px;border-radius:12px;box-shadow:0 2px 8px rgba(22,50,79,.05);}
    div[data-testid="stMetricLabel"] {color:#486275;}
    .status-pill {display:inline-block;padding:4px 10px;border-radius:14px;background:#eef3f6;color:#16324f;font-weight:650;}
    </style>
    """,
    unsafe_allow_html=True,
)


STATUS_KO = {
    "REGULATORY_CANDIDATE": "규제 해당 후보",
    "MATCH_BELOW_THRESHOLD": "구조 일치·농도기준 미만",
    "REVIEW_REQUIRED": "전문가 검토 필요",
    "NO_MATCH_WITHIN_PROMOTED_RULES": "승격 규칙 내 불일치",
    "REVIEW_NO_PROMOTED_RULE": "승격 규칙 없음",
    "PROMOTED_SOURCE_VERIFIED_BROAD_SALT_AUTO_ELIGIBLE": "자동판정 승격",
}


def kstatus(value):
    return STATUS_KO.get(str(value), str(value))


def downloadable_path(path: Path, label: str):
    st.download_button(label, path.read_bytes(), file_name=path.name, width="stretch")


st.markdown(
    """
    <div class="hero">
      <h1>Broad-Salt 규제 판정 프레임워크</h1>
      <p>CAS 기반 구조 수집 · 염/parent 정규화 · 규칙 단위 승격 게이트 · 안전한 검토 라우팅</p>
    </div>
    """,
    unsafe_allow_html=True,
)

with st.sidebar:
    st.header("분석 설정")
    st.caption("기본값은 사전 지정된 1차 승격 기준입니다.")
    min_cases = st.number_input("최소 판정 사례 수", 1, 100, 5, 1)
    min_positive = st.number_input("최소 양성 사례 수", 1, 100, 2, 1)
    min_negative = st.number_input("최소 음성 사례 수", 1, 100, 2, 1)
    min_accuracy = st.slider("최소 정확도", 0.50, 1.00, 0.95, 0.01)
    zero_false_safe = st.toggle("False-safe 0건 필수", value=True)
    st.divider()
    st.subheader("규칙·검증자료 교체")
    uploaded_rules = st.file_uploader("규칙 파일", type=["csv", "xlsx"], key="rule_file")
    uploaded_cases = st.file_uploader("검증사례 파일", type=["csv", "xlsx"], key="case_file")
    with st.expander("입력 템플릿"):
        downloadable_path(DATA_DIR / "broad_salt_rules_TEMPLATE.csv", "규칙 템플릿")
        downloadable_path(DATA_DIR / "broad_salt_validation_cases_TEMPLATE.csv", "검증사례 템플릿")

try:
    raw_rules = read_uploaded_table(uploaded_rules, DEFAULT_RULES)
    raw_cases = read_uploaded_table(uploaded_cases, DEFAULT_CASES)
    fw = calculate_framework(
        raw_rules,
        raw_cases,
        min_cases=int(min_cases),
        min_positive=int(min_positive),
        min_negative=int(min_negative),
        min_accuracy=float(min_accuracy),
        require_zero_false_safe=bool(zero_false_safe),
    )
except Exception as exc:
    st.error(f"규칙 또는 검증자료를 처리할 수 없습니다: {type(exc).__name__}: {exc}")
    st.stop()

rules = fw["rules"]
cases = fw["cases"]
promotion = fw["promotion"]
detail = fw["detail"]
by_rule = fw["by_rule"]
coverage = fw["coverage"]
target_rules = 12
loaded_rules = len(rules)
promoted_rules = int(promotion["promotion_eligible"].sum()) if len(promotion) else 0

tabs = st.tabs([
    "대시보드",
    "기업 물질 판정",
    "규칙 승격",
    "민감도 분석",
    "검증사례",
    "연구 범위",
])

with tabs[0]:
    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("연구 목표 규칙", f"{target_rules}개")
    c2.metric("현재 입력 규칙", f"{loaded_rules}개", f"{loaded_rules-target_rules}개")
    c3.metric("승격 규칙", f"{promoted_rules}개")
    c4.metric("검증사례", f"{len(cases)}개")
    decided = int(detail["decided"].sum()) if len(detail) else 0
    c5.metric("판정 가능 사례", f"{decided}개", f"보류 {len(detail)-decided}개")

    if loaded_rules < target_rules:
        st.markdown(
            f'<div class="notice"><b>확장 진행 중:</b> 코드의 목표는 {target_rules}개 규칙이지만 '
            f'현재 규칙 파일에는 {loaded_rules}개만 있습니다. 추가로 {target_rules-loaded_rules}개 규칙의 '
            '법적 범위·parent 구조·검증사례를 구축해야 12개 규칙 실증이 완성됩니다.</div>',
            unsafe_allow_html=True,
        )

    left, right = st.columns([1.15, 1])
    with left:
        st.subheader("규칙별 검증 현황")
        chart_df = by_rule[["reference_parent_name", "n_positive", "n_negative"]].melt(
            id_vars="reference_parent_name", var_name="case_type", value_name="n"
        )
        chart_df["case_type"] = chart_df["case_type"].map({"n_positive": "양성", "n_negative": "음성"})
        fig = px.bar(
            chart_df,
            x="reference_parent_name",
            y="n",
            color="case_type",
            barmode="stack",
            color_discrete_map={"양성": "#2A7F7F", "음성": "#8AA7B8"},
            labels={"reference_parent_name": "기준 parent", "n": "사례 수", "case_type": "구분"},
        )
        fig.update_layout(height=390, margin=dict(l=20, r=20, t=20, b=20), legend_orientation="h")
        st.plotly_chart(fig, width="stretch")
    with right:
        st.subheader("승격 게이트 결과")
        show = promotion[[
            "reference_parent_name", "n_decided", "selective_accuracy", "false_safe_n",
            "promotion_eligible", "promotion_status"
        ]].copy()
        show.columns = ["기준 parent", "판정사례", "정확도", "false-safe", "승격", "상태"]
        show["정확도"] = show["정확도"].map(lambda x: f"{x:.1%}" if pd.notna(x) else "-")
        show["상태"] = show["상태"].map(kstatus)
        st.dataframe(show, hide_index=True, width="stretch", height=360)

    st.markdown(
        '<div class="scope"><b>해석:</b> 현재 5개 규칙은 모두 관찰된 false-safe 0건과 정확도 100%로 '
        '승격되지만, 양성 표본 수가 규칙당 4~7개 수준이므로 “실제 위험이 0”임을 뜻하지 않습니다. '
        '본 화면은 관찰 결과와 자동화 경계를 구분해 표시합니다.</div>',
        unsafe_allow_html=True,
    )

with tabs[1]:
    st.subheader("회사 보유물질 등록 및 구조 판정")
    st.markdown(
        '<div class="scope">회사의 CAS·물질명·함량 목록을 업로드하면 누락된 SMILES를 PubChem에서 '
        '확인하고, 승격된 broad-salt 규칙의 parent 구조와 비교합니다.</div>',
        unsafe_allow_html=True,
    )
    u1, u2 = st.columns([1, 1])
    with u1:
        inventory_file = st.file_uploader("회사 물질 파일", type=["csv", "xlsx"], key="inventory_file")
        downloadable_path(DATA_DIR / "sample_company_inventory.csv", "회사 물질 예시파일 다운로드")
    with u2:
        threshold_file = st.file_uploader(
            "농도기준 연결표(선택)", type=["csv", "xlsx"], key="threshold_file"
        )
        downloadable_path(DATA_DIR / "regulatory_thresholds_TEMPLATE.csv", "농도기준 템플릿 다운로드")

    pubchem_run = st.checkbox("SMILES가 없는 CAS를 PubChem에서 자동 조회", value=True)
    if threshold_file is None:
        st.info("현재 ZIP에는 규칙별 농도기준 연결표가 없습니다. 구조범위 판정은 가능하지만 최종 농도 판정은 검토로 남깁니다.")

    if st.button("물질 판정 실행", type="primary", width="stretch"):
        try:
            inv_raw = read_uploaded_table(inventory_file, DATA_DIR / "sample_company_inventory.csv")
            inv, pubchem_audit = prepare_inventory(inv_raw, fetch_pubchem=pubchem_run)
            thresholds = read_uploaded_table(threshold_file) if threshold_file else pd.DataFrame()
            active_rules = apply_threshold_table(rules, thresholds)
            screen_detail, material_summary = screen_inventory(inv, active_rules, promotion)
            st.session_state["inventory_result"] = {
                "inventory": inv,
                "pubchem_audit": pubchem_audit,
                "detail": screen_detail,
                "summary": material_summary,
            }
        except Exception as exc:
            st.error(f"물질 판정 중 오류가 발생했습니다: {type(exc).__name__}: {exc}")

    result = st.session_state.get("inventory_result")
    if result:
        summary = result["summary"].copy()
        summary["판정"] = summary["final_decision"].map(kstatus)
        counts = summary["판정"].value_counts()
        cols = st.columns(max(1, min(4, len(counts))))
        for i, (label, n) in enumerate(counts.items()):
            cols[i % len(cols)].metric(label, f"{n}개")

        st.dataframe(
            summary[["material_id", "chemical_name", "cas", "concentration_pct", "matched_parent", "판정", "decision_reason"]],
            hide_index=True,
            width="stretch",
        )
        report = dataframe_to_excel({
            "material_summary": result["summary"],
            "rule_material_detail": result["detail"],
            "inventory_enriched": result["inventory"],
            "pubchem_audit": result["pubchem_audit"],
            "rule_promotion": promotion,
        })
        st.download_button(
            "판정 결과 Excel 다운로드", report,
            file_name="broad_salt_screening_results.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            width="stretch",
        )

        with st.expander("개별 물질의 구조 비교 보기"):
            selected = st.selectbox(
                "물질 선택",
                summary["material_id"].astype(str).tolist(),
                format_func=lambda x: f"{x} · {summary.loc[summary['material_id'].astype(str).eq(x), 'chemical_name'].iloc[0]}",
            )
            inv_row = result["inventory"][result["inventory"]["material_id"].astype(str).eq(selected)].iloc[0]
            candidates = result["detail"][
                result["detail"]["material_id"].astype(str).eq(selected)
                & result["detail"]["salt_membership"].eq(True)
            ]
            a, b = st.columns(2)
            with a:
                st.markdown("**입력/수집 구조**")
                img = molecular_image(inv_row.get("smiles"))
                if img is not None:
                    st.image(img, width="stretch")
                st.code(str(inv_row.get("smiles", "")), language=None)
            with b:
                st.markdown("**일치한 규칙 parent**")
                if len(candidates):
                    rr = candidates.iloc[0]
                    img = molecular_image(rr.get("reference_parent_key"))
                    if img is not None:
                        st.image(img, width="stretch")
                    st.write(f"기준물질: {rr.get('reference_parent_name', '')}")
                    st.code(str(rr.get("reference_parent_key", "")), language=None)
                else:
                    st.info("현재 승격 규칙에서 일치한 parent가 없습니다.")

with tabs[2]:
    st.subheader("규칙 단위 승격 심사")
    st.caption("결정론적 구조판정 결과만으로 자동화하지 않고, 출처·동결·검증 조건을 모두 통과한 규칙만 승격합니다.")
    table = promotion.copy()
    table["promotion_label"] = table["promotion_status"].map(kstatus)
    view_cols = [
        "reference_parent_name", "designation_id", "n_validation_rows", "n_decided",
        "n_positive", "n_negative", "selective_accuracy", "false_safe_n",
        "false_safe_zero_event_upper_95", "promotion_eligible", "promotion_label",
    ]
    st.dataframe(table[view_cols], hide_index=True, width="stretch")

    selected_rule = st.selectbox("규칙 상세 보기", rules["rule_id"].astype(str).tolist())
    rr = rules[rules["rule_id"].astype(str).eq(selected_rule)].iloc[0]
    pp = promotion[promotion["rule_id"].astype(str).eq(selected_rule)].iloc[0]
    c1, c2, c3 = st.columns([1, 1, 1])
    c1.metric("기준 parent", rr.get("reference_parent_name", ""))
    c2.metric("검증 정확도", f"{float(pp.get('selective_accuracy', 0)):.1%}")
    c3.metric("승격", "승격" if bool(pp.get("promotion_eligible")) else "미승격")
    img = molecular_image(rr.get("reference_smiles"))
    left, right = st.columns([1, 1.4])
    with left:
        if img is not None:
            st.image(img, width="stretch")
        st.code(str(rr.get("reference_smiles", "")), language=None)
    with right:
        st.write(f"- 기준 CAS: `{rr.get('reference_parent_cas', '')}`")
        st.write(f"- 염 범위: `{rr.get('salt_scope', '')}`")
        st.write(f"- 이성질체 범위: `{rr.get('isomer_scope_normalized', '')}`")
        st.write(f"- 규칙 버전: `{rr.get('rule_version', '')}`")
        st.write(f"- 동결일: `{rr.get('frozen_date', '')}`")
        st.write(f"- 규제 출처: {rr.get('regulatory_source', '')}")
        st.write(f"- 화학정보 출처: {rr.get('chemical_identity_source', '')}")

with tabs[3]:
    st.subheader("승격 기준 민감도 분석")
    st.caption("아래 분석은 현재 사이드바의 기준을 바꾸는 용도가 아니라, 사전 지정 기준의 강건성을 확인하는 2차 반사실적 분석입니다.")
    sensitivity = fw["sensitivity_summary"].copy()
    if len(sensitivity):
        sensitivity["조건"] = sensitivity.apply(
            lambda r: f"n≥{int(r['min_decided_cases'])}, 양성≥{int(r['min_positive_cases'])}, 정확도≥{r['min_selective_accuracy']:.0%}",
            axis=1,
        )
        fig = px.scatter(
            sensitivity,
            x="min_decided_cases",
            y="n_final_promotion_eligible",
            color="min_positive_cases",
            symbol="min_selective_accuracy",
            hover_data=["scenario_id", "same_promotion_result_as_primary"],
            labels={
                "min_decided_cases": "최소 판정사례 수",
                "n_final_promotion_eligible": "승격 규칙 수",
                "min_positive_cases": "최소 양성 수",
                "min_selective_accuracy": "최소 정확도",
            },
            color_continuous_scale="Tealgrn",
        )
        fig.update_traces(marker={"size": 12})
        fig.update_layout(height=450)
        st.plotly_chart(fig, width="stretch")
        st.dataframe(
            sensitivity[[
                "scenario_id", "analysis_role", "min_decided_cases", "min_positive_cases",
                "min_negative_cases", "min_selective_accuracy", "n_final_promotion_eligible",
                "same_promotion_result_as_primary",
            ]],
            hide_index=True,
            width="stretch",
        )

with tabs[4]:
    st.subheader("독립 검증사례")
    filters = st.columns([1, 1, 2])
    parents = ["전체"] + sorted(by_rule["reference_parent_name"].astype(str).tolist())
    parent_filter = filters[0].selectbox("기준 parent", parents)
    membership_filter = filters[1].selectbox("참조 판정", ["전체", "MATCH", "NO_MATCH"])
    filtered = detail.copy()
    if parent_filter != "전체":
        rid = by_rule.loc[by_rule["reference_parent_name"].eq(parent_filter), "rule_id"].iloc[0]
        filtered = filtered[filtered["rule_id"].eq(rid)]
    if membership_filter != "전체":
        filtered = filtered[filtered["reference_membership"].eq(membership_filter)]
    st.dataframe(
        filtered[[
            "case_id", "chemical_name", "cas", "reference_membership", "decision_status",
            "correct", "false_safe", "reference_support_status",
        ]],
        hide_index=True,
        width="stretch",
    )
    st.caption("현재 60개 중 58개가 자동 비교되며, 2개는 이성질체 법적 범위가 명시되지 않아 검토로 보류됩니다.")

with tabs[5]:
    st.subheader("현재 프로그램이 하는 것과 하지 않는 것")
    a, b = st.columns(2)
    with a:
        st.markdown("#### 자동화 대상")
        st.markdown(
            """
            - CAS 기반 PubChem 구조 조회와 정확 CAS 동의어 확인
            - 염·counterion 제거 후 parent 구조 정규화
            - 승격된 broad-salt 규칙과 구조 비교
            - 구조 누락·API 오류·이성질체 불확실성의 검토 라우팅
            - 규칙별 검증과 승격 기준 민감도 분석
            - 농도기준 연결표가 있을 때 구조판정 후 농도 조건 적용
            """
        )
    with b:
        st.markdown("#### 자동화하지 않는 범위")
        st.markdown(
            """
            - 법령에 명시되지 않은 이성질체 포함범위의 추정
            - generic derivative·구조범위·UVCB·반응생성물 자동판정
            - SMILES 또는 PubChem 조회 실패를 비규제로 처리
            - 한 broad-salt 규칙의 NO_MATCH를 전체 법령상 비규제로 해석
            - 연구자가 검토하지 않은 규칙의 자동 승격
            """
        )
    st.markdown(
        '<div class="danger"><b>중요:</b> 이 프로그램은 현재 논문용 proof-of-concept입니다. '
        '법적 최종판정 시스템이 아니며, 공식 고시 원문과 전문가 확인을 대체하지 않습니다.</div>',
        unsafe_allow_html=True,
    )
    st.markdown("#### 현재 데이터 완성도")
    st.dataframe(coverage, hide_index=True, width="stretch")
