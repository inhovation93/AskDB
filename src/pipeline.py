"""Text2SQL 파이프라인 오케스트레이션.

  질문
   → ① 스키마 링킹        (관련 테이블 축소)
   → ② 유사 예제 검색      (few-shot)
   → ③ SQL 생성           (Claude)
   → ④ 정적 검증·가드레일   (sqlglot / 권한 / LIMIT)
   → ⑤ 읽기전용 실행
   → ⑥ 실패 시 자기수정 루프 (오류를 모델에 되먹임, 최대 N회)

각 단계는 Step 으로 기록되어 UI 에 그대로 타임라인으로 렌더링된다.
"관측 가능한 파이프라인"이 프로토타입을 실제 시스템으로 키우는 출발점이다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import date

import pandas as pd

from . import guardrails, knowledge, llm, prompts, schema_linker
from .db import QueryTimeout, Schema, render_schema, run_query


@dataclass
class Options:
    model: str = llm.DEFAULT_MODEL
    effort: str = "medium"
    use_schema_linking: bool = True
    use_fewshot: bool = True
    max_retries: int = 2
    row_limit: int = 500
    max_tables: int = 6


@dataclass
class Step:
    name: str
    status: str          # ok | warn | error | skip
    detail: str = ""
    duration: float = 0.0
    payload: dict = field(default_factory=dict)


@dataclass
class Result:
    question: str
    ok: bool = False
    sql: str | None = None
    df: pd.DataFrame | None = None
    steps: list[Step] = field(default_factory=list)
    usage: llm.Usage = field(default_factory=llm.Usage)
    attempts: int = 1
    reasoning: str | None = None
    assumption: str | None = None
    clarify: str | None = None
    error: str | None = None
    elapsed: float = 0.0
    linked_tables: list[str] = field(default_factory=list)
    examples: list = field(default_factory=list)
    row_count: int = 0
    exec_sec: float = 0.0


def run(question: str, *, client, schema: Schema, options: Options,
        example_bank: list[knowledge.Example] | None = None) -> Result:
    res = Result(question=question)
    t0 = time.time()

    # ── ① 스키마 링킹 ────────────────────────────────────────────────
    t = time.time()
    if options.use_schema_linking:
        linked = schema_linker.link(question, schema, max_tables=options.max_tables)
        tables = linked.tables
        if linked.fallback_all:
            detail = "관련 신호가 약해 전체 스키마를 사용합니다(재현율 우선)."
            status = "warn"
        else:
            detail = (f"{len(schema.tables)}개 중 {len(tables)}개 테이블 선택"
                      + (f" (조인 경로 보강: {', '.join(linked.bridges)})" if linked.bridges else ""))
            status = "ok"
        payload = {"scores": linked.scores, "evidence": linked.evidence,
                   "bridges": linked.bridges, "tables": tables}
    else:
        tables = schema.table_names()
        detail = "링킹 비활성화 — 전체 스키마 주입"
        status = "skip"
        payload = {"tables": tables}
    res.linked_tables = tables
    res.steps.append(Step("① 스키마 링킹", status, detail, time.time() - t, payload))

    schema_text = render_schema(schema, include=tables)

    # ── ② 유사 예제 검색 ─────────────────────────────────────────────
    t = time.time()
    if options.use_fewshot:
        pairs = knowledge.retrieve_examples(question, example_bank, k=4)
        examples_text = knowledge.render_examples(pairs)
        res.examples = pairs
        res.steps.append(Step("② 유사 예제 검색", "ok",
                              f"{len(pairs)}개 예제 선택 (최고 유사도 {pairs[0][1]:.2f})"
                              if pairs else "예제 없음",
                              time.time() - t, {"pairs": pairs}))
    else:
        examples_text = "(few-shot 비활성화)"
        res.steps.append(Step("② 유사 예제 검색", "skip", "비활성화", time.time() - t))

    # ── ③~⑥ 생성 → 검증 → 실행, 실패 시 자기수정 ──────────────────────
    system, messages = prompts.build_sql_messages(
        question, schema_text, examples_text, date.today().isoformat())
    allowed = set(schema.table_names())
    last_error = "알 수 없는 오류"

    for attempt in range(1, options.max_retries + 2):
        res.attempts = attempt
        label = "③ SQL 생성" if attempt == 1 else f"⑥ 자기수정 {attempt - 1}회차"

        t = time.time()
        try:
            out = llm.complete(client, model=options.model, system=system,
                               messages=messages, effort=options.effort)
        except llm.LLMError as exc:
            res.steps.append(Step(label, "error", str(exc), time.time() - t))
            res.error = str(exc)
            res.elapsed = time.time() - t0
            return res

        res.usage.add(out.usage)
        parsed = llm.parse_sql_response(out.text)
        res.reasoning = parsed["reasoning"] or res.reasoning
        res.assumption = parsed["assumption"] or res.assumption

        # 모델이 스스로 "정보 부족"을 선언한 경우 — 환각 SQL 보다 이게 낫다
        if parsed["clarify"] and not parsed["sql"]:
            res.clarify = parsed["clarify"]
            res.steps.append(Step(label, "warn", "모델이 추가 정보를 요청했습니다.",
                                  time.time() - t, {"usage": out.usage}))
            res.elapsed = time.time() - t0
            return res

        # 출력 한도에 걸려 SQL 이 잘렸을 가능성을 명시적으로 처리한다.
        truncated = out.stop_reason == "max_tokens"
        if truncated:
            last_error = ("모델 출력이 길이 한도에 걸려 SQL 이 잘렸습니다. "
                          "더 짧고 간결한 SQL 로 다시 작성하세요.")
            res.steps.append(Step(label, "warn", last_error, time.time() - t,
                                  {"usage": out.usage}))
            messages += [{"role": "assistant", "content": out.text or "(빈 응답)"},
                         prompts.build_repair_message(parsed["sql"] or "(잘림)",
                                                      last_error, attempt)]
            continue

        if not parsed["sql"]:
            last_error = "모델 응답에서 SQL 을 찾지 못했습니다."
            res.steps.append(Step(label, "error", last_error, time.time() - t,
                                  {"usage": out.usage, "raw": out.text[:800]}))
            messages += [{"role": "assistant", "content": out.text or "(빈 응답)"},
                         prompts.build_repair_message("(없음)", last_error, attempt)]
            continue

        res.steps.append(Step(label, "ok",
                              f"{out.usage.output_tokens:,} 출력 토큰 / "
                              f"{out.usage.latency_sec:.1f}초"
                              + (f" · 캐시 적중 {out.usage.cache_read:,} 토큰"
                                 if out.usage.cache_read else ""),
                              time.time() - t,
                              {"usage": out.usage, "reasoning": parsed["reasoning"],
                               "sql": parsed["sql"]}))

        # ── ④ 가드레일 ────────────────────────────────────────────────
        t = time.time()
        guard = guardrails.validate(parsed["sql"], allowed, row_limit=options.row_limit)
        if not guard.ok:
            last_error = " / ".join(guard.errors)
            res.steps.append(Step("④ 정적 검증·가드레일", "error", last_error, time.time() - t,
                                  {"sql": parsed["sql"]}))
            messages += [{"role": "assistant", "content": out.text},
                         prompts.build_repair_message(parsed["sql"], last_error, attempt)]
            continue
        res.steps.append(Step(
            "④ 정적 검증·가드레일", "ok",
            f"통과 · 참조 테이블 {', '.join(guard.tables_used)}"
            + (f" · LIMIT {options.row_limit} 자동 적용" if guard.limit_injected else ""),
            time.time() - t, {"warnings": guard.warnings}))

        # ── ⑤ 실행 ────────────────────────────────────────────────────
        t = time.time()
        try:
            df, exec_sec = run_query(guard.sql, max_rows=options.row_limit)
        except QueryTimeout as exc:
            last_error = str(exc)
            res.steps.append(Step("⑤ 실행", "error", last_error, time.time() - t))
            messages += [{"role": "assistant", "content": out.text},
                         prompts.build_repair_message(
                             guard.sql, last_error + " 더 좁은 조건이나 집계를 사용하세요.", attempt)]
            continue
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            res.steps.append(Step("⑤ 실행", "error", last_error, time.time() - t))
            messages += [{"role": "assistant", "content": out.text},
                         prompts.build_repair_message(guard.sql, last_error, attempt)]
            continue

        res.steps.append(Step("⑤ 실행", "ok",
                              f"{len(df):,}행 반환 · {exec_sec * 1000:.0f}ms",
                              time.time() - t))
        res.ok = True
        res.sql = guard.sql
        res.df = df
        res.row_count = len(df)
        res.exec_sec = exec_sec
        res.elapsed = time.time() - t0
        return res

    res.error = f"{options.max_retries}회 자기수정 후에도 실행 가능한 SQL 을 만들지 못했습니다. 마지막 오류: {last_error}"
    res.elapsed = time.time() - t0
    return res


def _to_markdown(df: pd.DataFrame) -> str:
    """의존성(tabulate) 없이 파이프 테이블을 만든다 — 배포 실패 지점을 줄이기 위함."""
    cols = [str(c) for c in df.columns]
    head = "| " + " | ".join(cols) + " |"
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    body = [
        "| " + " | ".join("" if pd.isna(v) else str(v) for v in row) + " |"
        for row in df.itertuples(index=False, name=None)
    ]
    return "\n".join([head, sep, *body])


def summarize(client, *, question: str, sql: str, df: pd.DataFrame,
              model: str, max_table_rows: int = 30):
    """실행 결과를 자연어로 요약 — 제너레이터(스트리밍)."""
    shown = df.head(max_table_rows)
    table_md = _to_markdown(shown) if not shown.empty else "(결과 0행)"
    prompt = prompts.build_answer_prompt(question, sql, table_md, len(df),
                                         truncated=len(df) > max_table_rows)
    return llm.stream_text(client, model=model, system=prompts.ANSWER_SYSTEM,
                           messages=[{"role": "user", "content": prompt}],
                           max_tokens=1200, effort="low")
