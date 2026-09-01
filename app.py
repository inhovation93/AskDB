"""AskDB — 사내 데이터 질문 → SQL 자동 생성·실행 시스템 (PoC)

Streamlit 엔트리포인트. 비즈니스 로직은 전부 src/ 아래 모듈에 있고
이 파일은 UI 조립과 상태 관리만 담당한다.
"""
from __future__ import annotations

import os
import time
from datetime import date

import pandas as pd
import plotly.express as px
import streamlit as st

from src import erd, evaluation, knowledge, lineage, llm, pipeline
from src.db import get_connection, introspect, render_schema, run_query
from src.knowledge import DATA_MAX_DATE, DATA_MIN_DATE

st.set_page_config(
    page_title="AskDB — 사내 데이터 질문 시스템",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# 스타일
# ---------------------------------------------------------------------------
st.markdown("""
<style>
  .block-container {padding-top: 2.2rem; max-width: 1350px;}
  .hero {background: linear-gradient(120deg,#1e3a8a 0%,#2563eb 55%,#0ea5e9 100%);
         color:#fff; padding:1.4rem 1.7rem; border-radius:14px; margin-bottom:1.1rem;}
  .hero h1 {margin:0; font-size:1.65rem; letter-spacing:-.4px;}
  .hero p  {margin:.45rem 0 0; opacity:.93; font-size:.92rem; line-height:1.55;}
  .pill {display:inline-block; background:rgba(255,255,255,.18); border-radius:999px;
         padding:.16rem .62rem; font-size:.74rem; margin:.32rem .3rem 0 0;}
  .card {border:1px solid #e2e8f0; border-radius:12px; padding:.85rem 1rem; background:#fff;}
  .kpi {font-size:1.75rem; font-weight:700; color:#1e3a8a; line-height:1.2;}
  .kpi-l {font-size:.78rem; color:#64748b; margin-bottom:.15rem;}
  .step-ok {color:#047857;} .step-err {color:#b91c1c;}
  .step-warn {color:#b45309;} .step-skip {color:#94a3b8;}
  .muted {color:#64748b; font-size:.83rem;}
  div[data-testid="stMetricValue"] {font-size:1.45rem;}
  code {font-size:.86rem;}
</style>
""", unsafe_allow_html=True)

STATUS_ICON = {"ok": "✅", "warn": "⚠️", "error": "❌", "skip": "⏭️"}
STATUS_CLASS = {"ok": "step-ok", "warn": "step-warn", "error": "step-err", "skip": "step-skip"}

EXAMPLE_QUESTIONS = [
    "월별 매출 추이를 보여줘",
    "카테고리별 매출총이익 상위 5개",
    "채널별 평균 주문금액(AOV)은?",
    "재구매 고객 비율이 얼마야?",
    "월별 ROAS(광고비 대비 매출)를 계산해줘",
    "권역별 배송 리드타임이 가장 긴 곳은?",
    "미해결 CS 티켓이 많은 문의 유형",
    "전월 대비 매출 성장률을 알려줘",
]


# ---------------------------------------------------------------------------
# 리소스 / 상태
# ---------------------------------------------------------------------------
@st.cache_resource(show_spinner="샘플 데이터베이스를 준비하는 중…")
def load_schema():
    conn = get_connection()
    try:
        return introspect(conn)
    finally:
        conn.close()


KEY_NAMES = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def resolve_api_key() -> tuple[str | None, str]:
    """키와 그 출처를 반환한다. 우선순위: 세션 입력 > Secrets > 환경변수.

    OpenAI / Anthropic 키를 모두 받아들이며, 공급자는 키 접두어로 자동 판별한다.
    Secrets 에 어느 쪽을 넣어도 앱이 동작하므로 배포 실패 지점이 줄어든다.
    """
    if st.session_state.get("user_api_key"):
        return st.session_state["user_api_key"].strip(), "사용자 입력"
    for name in KEY_NAMES:
        try:
            if name in st.secrets:
                key = str(st.secrets[name]).strip()
                if key:
                    return key, f"배포 Secrets ({name})"
        except Exception:
            pass
    for name in KEY_NAMES:
        env = os.environ.get(name, "").strip()
        if env:
            return env, f"환경변수 ({name})"
    return None, "없음"


def init_state() -> None:
    ss = st.session_state
    ss.setdefault("history", [])
    ss.setdefault("total_usage", llm.Usage())
    ss.setdefault("example_bank", list(knowledge.FEWSHOT_BANK))
    ss.setdefault("approved", 0)
    ss.setdefault("eval_report", None)
    ss.setdefault("pending_question", None)
    ss.setdefault("user_api_key", "")
    ss.setdefault("model_list", [])


init_state()
schema = load_schema()
api_key, key_source = resolve_api_key()
provider = llm.detect_provider(api_key) if api_key else llm.OPENAI
ROW_TOTAL = sum(t.row_count for t in schema.tables.values())


# ---------------------------------------------------------------------------
# 사이드바
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### ⚙️ 실행 설정")

    if api_key:
        st.success(f"**{llm.provider_label(provider)}** 연결됨\n\n출처: {key_source}",
                   icon="🔑")
    else:
        st.warning("API 키가 없어 **오프라인 데모 모드**로 동작합니다.", icon="🔌")
        st.caption("OpenAI 또는 Anthropic 키를 넣으면 실제 SQL 생성이 활성화됩니다. "
                   "공급자는 키 형식으로 자동 판별하며, 입력한 키는 이 브라우저 "
                   "세션에만 보관되고 저장·전송되지 않습니다.")
        st.text_input("API 키", type="password", key="user_api_key",
                      placeholder="sk-... (OpenAI) 또는 sk-ant-... (Anthropic)")

    suggestions = llm.MODEL_SUGGESTIONS[provider]
    picked = st.selectbox(
        "모델", suggestions + ["직접 입력…"],
        help="계정에서 쓸 수 없는 모델이면 아래 '사용 가능한 모델 확인'으로 실제 목록을 조회하세요.",
    )
    if picked == "직접 입력…":
        model = st.text_input("모델 ID", value=llm.DEFAULT_MODELS[provider]).strip()
    else:
        model = picked

    if st.button("사용 가능한 모델 확인", width="stretch", disabled=not api_key):
        try:
            client_probe, prov_probe = llm.make_client(api_key)
            st.session_state["model_list"] = llm.list_models(client_probe, prov_probe)
        except llm.LLMError as exc:
            st.session_state["model_list"] = []
            st.error(str(exc))
    if st.session_state.get("model_list"):
        st.caption("이 계정에서 사용 가능한 모델 (위 '직접 입력…'에 붙여넣으세요)")
        st.code("\n".join(st.session_state["model_list"]), language="text")

    effort = st.select_slider(
        "추론 깊이 (effort)", options=["low", "medium", "high"], value="medium",
        help="깊을수록 정확하지만 느립니다. 추론 모델이 아니면 자동으로 무시됩니다.",
    )

    with st.expander("고급 파이프라인 옵션", expanded=False):
        use_linking = st.toggle("스키마 링킹", value=True,
                                help="질문과 관련된 테이블만 골라 프롬프트에 넣습니다. "
                                     "끄면 전체 스키마를 주입합니다.")
        use_fewshot = st.toggle("유사 예제 검색 (few-shot)", value=True,
                                help="검증된 유사 질의 예제를 함께 제공합니다.")
        max_retries = st.slider("자기수정 최대 횟수", 0, 3, 2,
                                help="검증·실행 실패 시 오류를 모델에 되먹여 재생성합니다.")
        row_limit = st.select_slider("결과 행 제한", [50, 100, 500, 1000], value=500)
        max_tables = st.slider("링킹 후보 테이블 수", 2, 9, 6)

    st.divider()
    st.markdown("### 📊 이번 세션 사용량")
    u: llm.Usage = st.session_state["total_usage"]
    c1, c2 = st.columns(2)
    cost = u.cost_usd(model)
    c1.metric("API 호출", f"{u.calls}회")
    c2.metric("비용(추정)", f"${cost:.4f}" if cost is not None else "—")
    c1.metric("입력 토큰", f"{u.total_input:,}")
    c2.metric("출력 토큰", f"{u.output_tokens:,}")
    if cost is None and u.calls:
        st.caption("이 모델의 단가가 등록되지 않아 비용 추정을 생략합니다(토큰 수는 정확).")
    if u.cache_read:
        unit = llm.PRICING.get(model)
        extra = ""
        if unit:
            extra = f" → 약 ${u.cache_read * unit['in'] * 0.9 / 1_000_000:.4f} 절감"
        st.caption(f"💾 프롬프트 캐시 적중 {u.cache_read:,} 토큰{extra}")

    st.divider()
    st.markdown("### 🗄️ 연결된 데이터")
    st.caption(f"**샘플 이커머스 DB** (SQLite, 읽기 전용)\n\n"
               f"테이블 {len(schema.tables)}개 · 총 {ROW_TOTAL:,}행\n\n"
               f"기간 {DATA_MIN_DATE} ~ {DATA_MAX_DATE}")
    st.info("모든 데이터는 seed 고정 난수로 생성한 **가상 데이터**입니다. "
            "실제 개인정보·사내 기밀이 포함되지 않습니다.", icon="🔒")

    if st.button("대화 기록 초기화", width="stretch"):
        st.session_state["history"] = []
        st.session_state["total_usage"] = llm.Usage()
        st.rerun()

options = pipeline.Options(
    provider=provider, model=model, effort=effort, use_schema_linking=use_linking,
    use_fewshot=use_fewshot, max_retries=max_retries,
    row_limit=row_limit, max_tables=max_tables,
)


# ---------------------------------------------------------------------------
# 헤더
# ---------------------------------------------------------------------------
st.markdown(f"""
<div class="hero">
  <h1>🔎 AskDB — 사내 데이터 질문 시스템</h1>
  <p>데이터팀에 요청하지 않고, 한국어 질문 한 줄로 사내 DB 를 조회합니다.
     스키마 링킹 → 검증된 예제 검색 → SQL 생성 → 정적 검증·가드레일 → 읽기전용 실행 →
     실패 시 자기수정까지, 전 과정을 눈으로 확인할 수 있는 파이프라인입니다.</p>
  <span class="pill">OpenAI · Anthropic 양쪽 지원</span>
  <span class="pill">스키마 링킹</span>
  <span class="pill">세만틱 레이어</span>
  <span class="pill">SQL 가드레일</span>
  <span class="pill">자기수정 루프</span>
  <span class="pill">골든셋 자동평가</span>
  <span class="pill">프롬프트 캐싱</span>
</div>
""", unsafe_allow_html=True)

tab_ask, tab_schema, tab_eval, tab_about = st.tabs(
    ["💬  질문하기", "🗄️  스키마 탐색", "🧪  자동 평가", "📖  프로젝트 설명"])


# ---------------------------------------------------------------------------
# 렌더링 헬퍼
# ---------------------------------------------------------------------------
def render_steps(steps: list[pipeline.Step]) -> None:
    for s in steps:
        icon = STATUS_ICON.get(s.status, "•")
        cls = STATUS_CLASS.get(s.status, "")
        st.markdown(
            f'<div class="{cls}">{icon} <b>{s.name}</b> — {s.detail} '
            f'<span class="muted">({s.duration:.2f}s)</span></div>',
            unsafe_allow_html=True)


def render_linking_detail(res: pipeline.Result) -> None:
    step = next((s for s in res.steps if s.name.startswith("①")), None)
    if not step or not step.payload.get("scores"):
        return
    scores = step.payload["scores"]
    evidence = step.payload.get("evidence", {})
    rows = [{
        "테이블": t,
        "관련도 점수": round(sc, 2),
        "선택": "✅" if t in res.linked_tables else "—",
        "근거": ", ".join(dict.fromkeys(evidence.get(t, [])))[:70] or "-",
    } for t, sc in sorted(scores.items(), key=lambda x: -x[1])]
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")


def auto_chart(df: pd.DataFrame):
    """결과 모양을 보고 적절한 차트를 자동 선택한다. 부적절하면 None."""
    if df.empty or len(df) < 2 or df.shape[1] < 2:
        return None
    num_cols = [c for c in df.columns if pd.api.types.is_numeric_dtype(df[c])]
    cat_cols = [c for c in df.columns if c not in num_cols]
    if not num_cols or not cat_cols:
        return None
    x, y = cat_cols[0], num_cols[0]
    label = str(x).lower()
    sample = str(df[x].iloc[0])
    is_time = any(k in label for k in ("월", "일", "년", "date", "ym", "기간")) or (
        len(sample) >= 7 and sample[:4].isdigit() and "-" in sample)
    try:
        if is_time and len(df) >= 3:
            fig = px.line(df.sort_values(x), x=x, y=y, markers=True)
        else:
            top = df.nlargest(min(15, len(df)), y)
            fig = px.bar(top, x=x, y=y)
        fig.update_layout(height=330, margin=dict(l=8, r=8, t=28, b=8),
                          xaxis_title=None, yaxis_title=str(y))
        return fig
    except Exception:
        return None


ROLE_COLOR = {"출력": "#1d4ed8", "조건": "#b45309", "조인": "#0e7490",
              "그룹": "#7c3aed", "정렬": "#be185d", "기타": "#64748b"}


def render_lineage(lin: lineage.Lineage | None, index: int | str,
                   title: str = "**📎 이 답의 근거 — 실제로 사용된 테이블 · 컬럼**") -> None:
    """이 답이 어느 테이블·컬럼에서 나왔는지 근거를 보여준다."""
    st.markdown(title)
    if lin is None or lin.table_count == 0:
        st.caption("SQL 에서 컬럼을 추출하지 못했습니다.")
        return

    chips = "".join(
        f'<span style="background:#f1f5f9;border:1px solid #cbd5e1;border-radius:6px;'
        f'padding:.2rem .55rem;margin:0 .3rem .3rem 0;display:inline-block;'
        f'font-size:.8rem"><b>{table}</b> '
        f'<span style="color:#64748b">{len(cols)}개 컬럼</span></span>'
        for table, cols in sorted(lin.columns.items()))
    st.markdown(f'<div style="margin:.15rem 0 .5rem">{chips}</div>',
                unsafe_allow_html=True)

    rows = lin.rows()
    st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch",
                 height=min(330, 45 + 35 * len(rows)))

    roles = "　".join(
        f'<span style="color:{ROLE_COLOR[r]};font-weight:600">{r}</span>'
        for r in ["출력", "조건", "조인", "그룹", "정렬"])
    st.markdown(
        f'<div style="font-size:.8rem;color:#64748b">사용된 곳 — {roles} '
        f'(각각 SELECT · WHERE/HAVING · JOIN ON · GROUP BY · ORDER BY)</div>',
        unsafe_allow_html=True)

    notes = []
    if lin.star_tables:
        notes.append(f"`SELECT *` 로 전체 컬럼을 읽은 테이블: "
                     f"{', '.join(sorted(lin.star_tables))}")
    if lin.ctes:
        notes.append(f"임시 결과셋(CTE): {', '.join(lin.ctes)}")
    if lin.unresolved:
        notes.append(f"소속 테이블이 모호해 제외한 컬럼: {', '.join(lin.unresolved)}")
    if not lin.exact:
        notes.append("일부 구문은 정밀 분석에 실패해 추정으로 표시했습니다.")
    for note in notes:
        st.caption(f"· {note}")

    st.download_button(
        "⬇️ 사용 컬럼 목록 CSV",
        pd.DataFrame(rows).to_csv(index=False).encode("utf-8-sig"),
        file_name=f"askdb_columns_{index}.csv", mime="text/csv",
        key=f"dl_lineage_{index}")


def render_result(item: dict, index: int) -> None:
    """저장된 결과 1건을 렌더링한다."""
    res: pipeline.Result = item["result"]

    with st.expander(f"🔬 파이프라인 실행 추적 — {res.attempts}회 시도 · 총 {res.elapsed:.1f}초",
                     expanded=False):
        render_steps(res.steps)
        if res.reasoning:
            st.markdown("**모델의 판단 근거**")
            st.info(res.reasoning)
        st.markdown("**스키마 링킹 상세**")
        render_linking_detail(res)
        if res.examples:
            st.markdown("**참조한 유사 예제**")
            st.dataframe(pd.DataFrame(
                [{"유사도": round(sc, 3), "예제 질문": ex.question} for ex, sc in res.examples]),
                hide_index=True, width="stretch")

    if res.clarify:
        st.warning(f"**추가 정보가 필요합니다**\n\n{res.clarify}", icon="❓")
        return
    if not res.ok:
        st.error(f"**처리 실패** — {res.error}", icon="🚫")
        st.caption("사이드바에서 '추론 깊이'를 high 로 올리거나 질문을 더 구체적으로 바꿔보세요.")
        return

    if item.get("answer"):
        st.markdown(item["answer"])

    df: pd.DataFrame = res.df
    lin = res.lineage
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("반환 행 수", f"{res.row_count:,}")
    m2.metric("DB 실행시간", f"{res.exec_sec * 1000:.0f} ms")
    m3.metric("사용 테이블 · 컬럼",
              f"{lin.table_count} · {lin.column_count}" if lin else "—",
              help="이 SQL 이 실제로 읽은 테이블 수와 컬럼 수입니다.")
    m4.metric("자기수정", f"{res.attempts - 1}회")

    left, right = st.columns([1.15, 1])
    with left:
        st.markdown("**조회 결과**")
        st.dataframe(df, width="stretch", height=min(340, 80 + 30 * len(df)))
        st.download_button("⬇️ CSV 다운로드", df.to_csv(index=False).encode("utf-8-sig"),
                           file_name=f"askdb_result_{index}.csv", mime="text/csv",
                           key=f"dl_{index}")
    with right:
        fig = auto_chart(df)
        if fig is not None:
            st.markdown("**자동 시각화**")
            st.plotly_chart(fig, width="stretch", key=f"chart_{index}")
        elif res.row_count == 1 and df.shape[1] <= 3:
            st.markdown("**핵심 지표**")
            for col in df.columns:
                val = df[col].iloc[0]
                shown = f"{val:,.0f}" if isinstance(val, (int, float)) else str(val)
                st.markdown(f'<div class="card"><div class="kpi-l">{col}</div>'
                            f'<div class="kpi">{shown}</div></div>',
                            unsafe_allow_html=True)
        else:
            st.caption("이 결과는 표 형태가 가장 적합해 차트를 생략했습니다.")

    st.markdown("**생성된 SQL**")
    st.code(res.sql, language="sql")
    if res.assumption and res.assumption != "없음":
        st.caption(f"📌 해석 가정: {res.assumption}")

    render_lineage(res.lineage, index)

    fb1, fb2, _ = st.columns([1, 1, 6])
    if fb1.button("👍 정확함", key=f"up_{index}",
                  help="이 질문-SQL 쌍을 예제 뱅크에 추가해 이후 질문의 정확도를 높입니다."):
        st.session_state["example_bank"].append(
            knowledge.Example(res.question, res.sql, "사용자 승인"))
        st.session_state["approved"] += 1
        st.toast("예제 뱅크에 추가했습니다. 다음 질문부터 참고합니다.", icon="✅")
    if fb2.button("👎 부정확", key=f"down_{index}"):
        st.toast("피드백 감사합니다. 실제 운영에서는 이 사례가 개선 큐로 적재됩니다.", icon="📝")


# ===========================================================================
# TAB 1 — 질문하기
# ===========================================================================
with tab_ask:
    st.markdown("##### 예시 질문으로 바로 시작해 보세요")
    cols = st.columns(4)
    for i, q in enumerate(EXAMPLE_QUESTIONS):
        if cols[i % 4].button(q, key=f"ex_{i}", width="stretch"):
            st.session_state["pending_question"] = q
            st.rerun()

    if not api_key:
        st.info("**오프라인 데모 모드** — API 키가 없어도 UI·DB·가드레일·평가 기능을 "
                "확인할 수 있도록, 아래 '자동 평가' 탭의 검증된 SQL 을 직접 실행해 볼 수 있습니다. "
                "실제 자연어 → SQL 생성은 사이드바에 키를 입력하면 활성화됩니다.", icon="🔌")
        with st.expander("검증된 질의 직접 실행해 보기 (LLM 미사용)", expanded=True):
            pick = st.selectbox("질문 선택", evaluation.GOLDEN_SET,
                                format_func=lambda g: f"[{g.difficulty}] {g.question}")
            if st.button("SQL 실행", type="primary"):
                gdf, sec = run_query(pick.gold_sql, max_rows=row_limit)
                st.code(pick.gold_sql, language="sql")
                cc1, cc2 = st.columns([1.2, 1])
                cc1.dataframe(gdf, width="stretch", hide_index=True)
                fig = auto_chart(gdf)
                if fig is not None:
                    cc2.plotly_chart(fig, width="stretch")
                st.caption(f"{len(gdf):,}행 · {sec * 1000:.0f}ms · 읽기 전용 커넥션에서 실행")
                render_lineage(lineage.analyze(pick.gold_sql, schema), "offline",
                               title="**📎 이 SQL 이 사용한 테이블 · 컬럼**")

    st.divider()

    for idx, item in enumerate(st.session_state["history"]):
        with st.chat_message("user"):
            st.markdown(item["question"])
        with st.chat_message("assistant", avatar="🔎"):
            render_result(item, idx)

    typed = st.chat_input("예: 2026년 상반기에 매출이 가장 많이 성장한 카테고리는?",
                          disabled=not api_key)
    question = typed or st.session_state.pop("pending_question", None)

    if question and not api_key:
        st.error("API 키를 먼저 입력해 주세요. (사이드바)", icon="🔑")
    elif question:
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant", avatar="🔎"):
            client, _prov = llm.make_client(api_key, provider)
            with st.status("파이프라인 실행 중…", expanded=True) as status:
                st.write("① 관련 테이블을 찾고 ② 유사 예제를 검색합니다…")
                res = pipeline.run(question, client=client, schema=schema,
                                   options=options,
                                   example_bank=st.session_state["example_bank"])
                render_steps(res.steps)
                if res.ok:
                    status.update(label=f"완료 — {res.row_count:,}행 조회 "
                                        f"({res.elapsed:.1f}초, 시도 {res.attempts}회)",
                                  state="complete", expanded=False)
                else:
                    status.update(label="실패 — 아래 내용을 확인해 주세요",
                                  state="error", expanded=True)

            st.session_state["total_usage"].add(res.usage)

            answer = ""
            if res.ok:
                try:
                    stream = pipeline.summarize(client, provider, question=question,
                                                sql=res.sql, df=res.df, model=model)
                    holder = st.empty()
                    for chunk in stream:
                        if isinstance(chunk, llm.Usage):
                            st.session_state["total_usage"].add(chunk)
                        else:
                            answer += chunk
                            holder.markdown(answer)
                except llm.LLMError as exc:
                    st.caption(f"(요약 생성 생략: {exc})")

            item = {"question": question, "result": res, "answer": answer}
            st.session_state["history"].append(item)
            render_result(item, len(st.session_state["history"]) - 1)


# ===========================================================================
# TAB 2 — 스키마 탐색
# ===========================================================================
with tab_schema:
    st.markdown("#### 연결된 데이터베이스 구조")
    st.caption("모델에게 주입되는 스키마 표현(M-Schema)에는 컬럼 코멘트·실제 값 목록·"
               "값 범위가 함께 들어갑니다. 이 메타데이터의 품질이 정확도를 좌우합니다.")

    k1, k2, k3, k4 = st.columns(4)
    k1.metric("테이블", f"{len(schema.tables)}개")
    k2.metric("총 행 수", f"{ROW_TOTAL:,}")
    k3.metric("외래키 관계", f"{len(schema.foreign_keys)}개")
    k4.metric("예제 뱅크", f"{len(st.session_state['example_bank'])}개",
              delta=f"+{st.session_state['approved']} 승인" if st.session_state["approved"] else None)

    view = st.radio("보기", ["🗺️ ERD 관계도", "테이블 상세",
                             "모델에 주입되는 스키마 원문", "관계(FK) 목록"],
                    horizontal=True, label_visibility="collapsed")

    if view == "🗺️ ERD 관계도":
        oc1, oc2, oc3 = st.columns([1.1, 1, 1.4])
        detail = oc1.radio("컬럼 표시", ["키 컬럼만", "전체 컬럼"], horizontal=True,
                           help="'키 컬럼만' 은 PK/FK 만 보여 관계 파악에 집중할 수 있습니다.")
        direction = oc2.radio("배치 방향", ["좌→우", "위→아래"], horizontal=True)
        # 강조 대상은 '실제로 SQL 이 읽은 테이블'을 우선한다.
        # (링킹 후보는 프롬프트에 넣은 목록이라 실제 사용분보다 넓다)
        last_tables: list[str] = []
        if st.session_state["history"]:
            last = st.session_state["history"][-1]["result"]
            last_tables = (sorted(last.lineage.columns)
                           if last.lineage and last.lineage.table_count
                           else last.linked_tables)
        do_hl = oc3.checkbox(
            f"직전 질문이 사용한 테이블 강조 ({len(last_tables)}개)",
            value=bool(last_tables), disabled=not last_tables,
            help="스키마 링킹이 어떤 테이블을 골랐는지 ERD 위에서 바로 확인합니다.")

        st.graphviz_chart(
            erd.build_dot(schema,
                          highlight=set(last_tables) if do_hl else set(),
                          keys_only=(detail == "키 컬럼만"),
                          rankdir="LR" if direction == "좌→우" else "TB"),
            width="stretch")

        st.markdown(erd.LEGEND_HTML, unsafe_allow_html=True)
        st.caption(
            "**PK** 기본키 · **FK** 외래키 　|　 "
            "선의 **까치발(⋔)** 쪽이 **N**, 반대쪽이 **1** (즉 `1 : N` 관계) 　|　 "
            "**점선** 은 자기참조(`employees.manager_id` → 상사) 　|　 "
            "그래프 우측 상단 확대 버튼으로 전체 화면으로 볼 수 있습니다.")

        with st.expander("관계를 문장으로 읽기 (ERD 가 익숙하지 않을 때)"):
            for sentence in erd.relation_sentences(schema):
                st.markdown(f"- {sentence}")

    elif view == "테이블 상세":
        for tname, table in schema.tables.items():
            with st.expander(f"**{tname}** — {table.comment}  ·  {table.row_count:,}행"):
                st.dataframe(pd.DataFrame([{
                    "컬럼": c.name, "타입": c.type,
                    "키": "PK" if c.is_pk else (f"FK→{c.fk}" if c.fk else ""),
                    "NULL": "허용" if c.nullable else "불가",
                    "설명": c.comment,
                    "값/범위": (" | ".join(c.enum_values) if c.enum_values
                              else (c.value_range or "")),
                } for c in table.columns]), hide_index=True, width="stretch")
                sample, _ = run_query(f'SELECT * FROM "{tname}" LIMIT 5')
                st.caption("샘플 5행 (가상 데이터)")
                st.dataframe(sample, hide_index=True, width="stretch")
    elif view == "모델에 주입되는 스키마 원문":
        st.code(render_schema(schema), language="text")
    else:
        st.dataframe(pd.DataFrame(
            [{"참조하는 쪽": a, "참조되는 쪽": b} for a, b in schema.foreign_keys]),
            hide_index=True, width="stretch")

    st.divider()
    st.markdown("#### 세만틱 레이어 — 회사 표준 지표 정의")
    st.caption("'매출'이 무엇인지 사람마다 다르게 해석하면 같은 질문에 다른 숫자가 나옵니다. "
               "지표 정의를 프롬프트에 못 박아 답변의 일관성을 확보합니다.")
    st.code(knowledge.BUSINESS_RULES, language="text")


# ===========================================================================
# TAB 3 — 자동 평가
# ===========================================================================
with tab_eval:
    st.markdown("#### 골든셋 기반 실행 정확도 자동 평가")
    st.caption("정답 SQL 과 **실행 결과가 같은지**로 채점합니다(Execution Accuracy). "
               "SQL 문자열 비교는 같은 뜻의 다른 SQL 을 오답 처리하므로 쓰지 않습니다. "
               "골든셋 질문은 few-shot 예제와 겹치지 않게 구성해 정보 누출을 막았습니다.")

    st.dataframe(pd.DataFrame([{
        "ID": g.id, "난이도": g.difficulty, "질문": g.question, "평가 역량": g.skill,
    } for g in evaluation.GOLDEN_SET]), hide_index=True, width="stretch")

    ec1, ec2 = st.columns([1, 2])
    subset = ec1.selectbox("평가 범위", ["전체 12문항", "앞 6문항 (빠른 확인)"])
    items = evaluation.GOLDEN_SET if subset.startswith("전체") else evaluation.GOLDEN_SET[:6]
    ec2.caption(f"선택한 {len(items)}문항을 순차 실행합니다. "
                f"약 {len(items) * 8}초, 예상 비용 ${len(items) * 0.012:.2f} 내외입니다.")

    if st.button("▶️ 평가 실행", type="primary", disabled=not api_key,
                 help=None if api_key else "API 키가 필요합니다"):
        client, _prov = llm.make_client(api_key, provider)
        report = evaluation.EvalReport()
        bar = st.progress(0.0, text="평가 준비 중…")
        live = st.empty()
        for i, gi in enumerate(items, 1):
            bar.progress((i - 1) / len(items), text=f"[{i}/{len(items)}] {gi.question}")
            row = evaluation.evaluate_one(gi, client=client, schema=schema, options=options)
            report.rows.append(row)
            if row.usage:
                st.session_state["total_usage"].add(row.usage)
            live.dataframe(pd.DataFrame([{
                "ID": r.item.id, "결과": "✅ 정답" if r.correct else "❌ 오답",
                "난이도": r.item.difficulty, "사유": r.reason,
                "시도": r.attempts, "소요(초)": round(r.elapsed, 1),
            } for r in report.rows]), hide_index=True, width="stretch")
        bar.progress(1.0, text="평가 완료")
        st.session_state["eval_report"] = report

    report: evaluation.EvalReport | None = st.session_state["eval_report"]
    if report and report.rows:
        st.divider()
        st.markdown("#### 평가 결과")
        r1, r2, r3, r4 = st.columns(4)
        r1.metric("실행 정확도", f"{report.accuracy * 100:.1f}%",
                  delta=f"{sum(r.correct for r in report.rows)}/{len(report.rows)} 정답")
        r2.metric("평균 응답시간", f"{report.avg_elapsed:.1f}초")
        r3.metric("자기수정 발생", f"{report.self_corrected}건")
        r4.metric("모델", options.model)

        st.markdown("**난이도별 정확도**")
        diff = report.by_difficulty()
        dcols = st.columns(max(len(diff), 1))
        for col, (name, (hit, total)) in zip(dcols, diff.items()):
            col.metric(name, f"{hit}/{total}", delta=f"{hit / total * 100:.0f}%")

        st.markdown("**문항별 상세**")
        for r in report.rows:
            icon = "✅" if r.correct else "❌"
            with st.expander(f"{icon} [{r.item.id}·{r.item.difficulty}] {r.item.question}"
                             f"  —  {r.reason}"):
                st.caption(f"평가 역량: {r.item.skill} · 시도 {r.attempts}회 · {r.elapsed:.1f}초")
                cA, cB = st.columns(2)
                cA.markdown("**모델이 생성한 SQL**")
                cA.code(r.pred_sql or "(생성 실패)", language="sql")
                cB.markdown("**정답 SQL**")
                cB.code(r.item.gold_sql, language="sql")
                if r.error:
                    st.error(r.error)

        csv = pd.DataFrame([{
            "id": r.item.id, "question": r.item.question, "difficulty": r.item.difficulty,
            "correct": r.correct, "reason": r.reason, "attempts": r.attempts,
            "elapsed_sec": round(r.elapsed, 2), "pred_sql": r.pred_sql,
            "gold_sql": r.item.gold_sql,
        } for r in report.rows]).to_csv(index=False).encode("utf-8-sig")
        st.download_button("⬇️ 평가 리포트 CSV", csv,
                           file_name=f"askdb_eval_{date.today().isoformat()}.csv",
                           mime="text/csv")
    elif not api_key:
        st.info("평가는 실제 모델 호출이 필요합니다. 사이드바에 API 키를 입력해 주세요.", icon="🔑")


# ===========================================================================
# TAB 4 — 프로젝트 설명
# ===========================================================================
with tab_about:
    st.markdown("""
#### 해결하려는 실무 문제

데이터는 DW 에 다 있는데, 정작 숫자가 필요한 사람은 SQL 을 못 쓴다.
"지난달 채널별 매출 좀"이라는 한 줄 요청이 데이터팀 티켓으로 쌓이고, 하루 이틀 뒤에 답이 온다.
반대로 데이터팀은 **반복적인 단순 추출 요청**에 시간을 갈아 넣는다.

AskDB 는 이 병목을 없애는 것을 목표로 한다. 단, 사내 DB 에 LLM 을 그냥 붙이면
(1) 틀린 숫자를 자신 있게 답하고 (2) 위험한 쿼리가 실행될 수 있다.
그래서 **정확도 장치와 안전 장치를 파이프라인으로 명시화**한 것이 이 프로젝트의 핵심이다.

#### 파이프라인 6단계

| 단계 | 하는 일 | 왜 필요한가 |
|---|---|---|
| ① 스키마 링킹 | 질문과 관련된 테이블만 선별 (동의어 사전 + 코멘트 유사도 + 실제 값 매칭 + FK 연결성) | 테이블 수천 개인 실제 DW 에서 전체 스키마 주입은 불가능. 토큰·정확도 모두 악화 |
| ② 유사 예제 검색 | 검증된 (질문, SQL) 쌍 중 유사한 것 4개를 프롬프트에 주입 | 회사 고유의 조인 패턴·지표 정의를 예시로 학습시킴. 👍 피드백이 뱅크에 축적됨 |
| ③ SQL 생성 | LLM 이 `<reasoning>/<sql>/<assumption>` 형식으로 생성 | 판단 근거와 해석 가정을 분리해 사람이 검토 가능하게 만듦 |
| ④ 정적 검증·가드레일 | sqlglot 파싱 → 단일 SELECT 강제, DDL/DML 차단, 없는 테이블 차단, 민감 컬럼 차단, LIMIT 주입 | 실행 **전에** 위험과 환각을 걸러냄 |
| ⑤ 읽기전용 실행 | `mode=ro` 커넥션 + 쿼리 타임아웃 | 드라이버 수준 2차 방어. 폭주 쿼리로 DB 를 물지 않게 함 |
| ⑥ 자기수정 | 오류 메시지를 모델에 되먹여 재생성 (기본 2회) | 실무 질문의 상당수는 1회차에 사소한 컬럼·문법 오류가 남 |
| ⑦ 사용 컬럼 분석 | 생성 SQL 을 AST 로 되짚어 실제로 읽은 테이블·컬럼과 그 용도를 추출 | 근거를 볼 수 없으면 답을 신뢰할 수 없음 |

#### 정확도를 만드는 3가지 설계

1. **세만틱 레이어** — "매출 = `SUM(수량×단가 − 할인)` where `status='완료'`" 처럼
   지표를 프롬프트에 못 박았다. 이것이 없으면 같은 질문에 매번 다른 숫자가 나온다.
2. **값 기반 스키마 표현** — 컬럼의 실제 값 목록(`'완료' | '취소' | ...`)과 범위를 함께 준다.
   "긴급 티켓"이 `priority='긴급'` 임을 모델이 추측하지 않아도 된다.
3. **평가 루프** — 골든셋 12문항의 실행 정확도를 언제든 측정할 수 있다.
   프롬프트를 고쳤을 때 좋아졌는지 나빠졌는지 숫자로 확인되지 않으면 개선이 아니라 도박이다.

#### 안전 설계 (사내 DB 연결 전제)

- **읽기 전용 2중 방어**: 정적 검증(sqlglot AST) + 드라이버 `mode=ro`
- **차단 대상**: INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/PRAGMA/ATTACH, 다중 문장, `sqlite_master`
- **민감 컬럼 마스킹 훅**: `guardrails.BLOCKED_COLUMNS` (데모에서는 `employees.annual_salary` 차단)
- **비용·자원 상한**: 결과 행 제한, 쿼리 타임아웃, 자기수정 횟수 상한
- **키 관리**: API 키는 Streamlit Secrets 로만 주입하며 코드·화면에 노출되지 않는다
- **공급자 교체 가능**: `src/llm.py` 한 파일만 바꾸면 다른 LLM 으로 갈아끼울 수 있다
- **데이터**: 전부 seed 고정 난수로 만든 가상 데이터. 개인정보·기밀 없음

#### 사용된 핵심 기술

| 영역 | 사용 기술 |
|---|---|
| LLM | **OpenAI GPT** (Chat Completions) / **Anthropic Claude** (Messages API) — 키 접두어로 자동 판별 |
| 비용 최적화 | **프롬프트 캐싱** — 안정적인 규칙·세만틱 레이어를 프롬프트 앞쪽에 고정 배치 (OpenAI 자동 캐싱 / Anthropic `cache_control`) |
| SQL 안전성 | **sqlglot** AST 파싱·검증·LIMIT 주입 |
| 데이터 | **SQLite** 읽기 전용 커넥션, 9테이블 합성 데이터셋 |
| ERD | **Graphviz DOT** — 스키마에서 자동 생성되어 문서가 코드와 어긋나지 않음 (브라우저 렌더링, 추가 설치물 없음) |
| UI | **Streamlit** (chat, status, tabs) + **Plotly** 자동 시각화 |
| 평가 | 자체 골든셋 + Execution Accuracy 채점기 |

#### 실제 DB 로 확장할 때의 다음 단계

- `db.py` 의 커넥션 팩토리와 `introspect()` 만 교체 (PostgreSQL / BigQuery / Snowflake)
- 스키마 링킹을 임베딩 검색 + 리랭커로 교체 (`schema_linker.link()` 만 갈아끼우면 됨)
- 예제 뱅크를 벡터 DB 로 영속화하고 👍 피드백을 온라인 학습 신호로 사용
- 사용자 권한(Row/Column Level Security)을 가드레일에 연동
- 골든셋을 200~500문항으로 확장하고 CI 에서 회귀 테스트로 실행
- 캐싱·큐잉으로 동시 사용자 대응, 질의 로그 기반 대시보드 자동 추천
""")
    st.caption("© AskDB PoC — 심화과정 최종 개인 프로젝트. 모든 데이터는 가상입니다.")
