"""LLM 을 포함한 E2E 스모크 테스트 (CLI).

배포 전에 "실제로 SQL 이 생성되고 실행되는가"를 브라우저 없이 확인한다.

    python scripts/smoke_test.py            # 대표 질문 4개
    python scripts/smoke_test.py --full     # 골든셋 12문항 전체 평가 + 정확도
    python scripts/smoke_test.py --offline  # LLM 없이 DB·가드레일·골든SQL 만 점검
"""
from __future__ import annotations

import argparse
import io
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src import evaluation, llm, pipeline  # noqa: E402
from src.db import get_connection, introspect, run_query  # noqa: E402
from src.guardrails import validate  # noqa: E402
from src.knowledge import FEWSHOT_BANK  # noqa: E402

SMOKE_QUESTIONS = [
    "월별 매출 추이를 보여줘",
    "긴급 우선순위 CS 티켓은 몇 건이야?",
    "카테고리별 매출총이익을 큰 순서로 알려줘",
    "재구매 고객 비율이 얼마야?",
]


def load_schema():
    conn = get_connection()
    try:
        return introspect(conn)
    finally:
        conn.close()


def check_offline(schema) -> bool:
    """LLM 없이 검증 가능한 모든 것을 점검한다."""
    ok = True
    allowed = set(schema.table_names())
    print(f"■ 스키마: 테이블 {len(schema.tables)}개, FK {len(schema.foreign_keys)}개, "
          f"총 {sum(t.row_count for t in schema.tables.values()):,}행")

    print("\n■ Few-shot 예제 뱅크 실행 검증")
    for ex in FEWSHOT_BANK:
        g = validate(ex.sql, allowed)
        if not g.ok:
            print(f"  ✗ 가드레일 실패: {ex.question} → {g.errors}"); ok = False; continue
        try:
            df, _ = run_query(g.sql)
            print(f"  ✓ {len(df):4d}행  {ex.question[:40]}")
        except Exception as exc:
            print(f"  ✗ 실행 실패: {ex.question} → {exc!r}"); ok = False

    print("\n■ 골든셋 정답 SQL 실행 검증")
    for item in evaluation.GOLDEN_SET:
        try:
            df, _ = run_query(item.gold_sql)
            print(f"  ✓ {item.id} {len(df):4d}행  {item.question[:40]}")
        except Exception as exc:
            print(f"  ✗ {item.id} 실행 실패 → {exc!r}"); ok = False

    print("\n■ 가드레일 차단 검증 (모두 차단되어야 정상)")
    attacks = [
        "DROP TABLE orders",
        "SELECT 1; DELETE FROM orders",
        "UPDATE orders SET status='완료'",
        "PRAGMA table_info(orders)",
        "SELECT * FROM sqlite_master",
        "SELECT * FROM nonexistent_table",
        "ATTACH DATABASE '/etc/passwd' AS x",
    ]
    for sql in attacks:
        g = validate(sql, allowed)
        mark = "✓ 차단" if not g.ok else "✗ 통과됨(위험!)"
        if g.ok:
            ok = False
        print(f"  {mark}  {sql}")
    return ok


def check_online(schema, api_key: str, full: bool, model: str) -> bool:
    client = llm.make_client(api_key)
    options = pipeline.Options(model=model, effort="medium", max_retries=2)
    ok = True

    if full:
        print(f"\n■ 골든셋 전체 평가 (모델 {model})")
        report = evaluation.EvalReport()
        for i, item in enumerate(evaluation.GOLDEN_SET, 1):
            row = evaluation.evaluate_one(item, client=client, schema=schema, options=options)
            report.rows.append(row)
            mark = "✓ 정답" if row.correct else "✗ 오답"
            print(f"  [{i:2d}/{len(evaluation.GOLDEN_SET)}] {mark}  {item.id} "
                  f"({item.difficulty}) 시도{row.attempts} {row.elapsed:4.1f}s  "
                  f"{item.question[:34]}")
            if not row.correct:
                print(f"        사유: {row.reason}")
                if row.error:
                    print(f"        오류: {row.error}")
        print(f"\n  ▶ 실행 정확도: {report.accuracy * 100:.1f}% "
              f"({sum(r.correct for r in report.rows)}/{len(report.rows)})")
        for name, (hit, total) in report.by_difficulty().items():
            print(f"    - {name}: {hit}/{total}")
        print(f"  ▶ 평균 응답시간 {report.avg_elapsed:.1f}초 · "
              f"자기수정 발생 {report.self_corrected}건")
        total = llm.Usage()
        for r in report.rows:
            if r.usage:
                total.add(r.usage)
        print(f"  ▶ 토큰 입력 {total.input_tokens:,} / 캐시적중 {total.cache_read:,} / "
              f"출력 {total.output_tokens:,} · 비용 ${total.cost_usd(model):.4f}")
        ok = report.accuracy >= 0.6
    else:
        print(f"\n■ 대표 질문 파이프라인 점검 (모델 {model})")
        for q in SMOKE_QUESTIONS:
            res = pipeline.run(q, client=client, schema=schema, options=options)
            if res.ok:
                print(f"  ✓ {res.row_count:4d}행 {res.elapsed:4.1f}s 시도{res.attempts} "
                      f"테이블{len(res.linked_tables)}개  {q}")
                print("      " + " ".join(res.sql.split())[:150])
            else:
                print(f"  ✗ 실패  {q}\n      {res.error or res.clarify}")
                ok = False
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--full", action="store_true", help="골든셋 전체 평가 실행")
    ap.add_argument("--offline", action="store_true", help="LLM 호출 없이 점검")
    ap.add_argument("--model", default=llm.DEFAULT_MODEL)
    args = ap.parse_args()

    schema = load_schema()
    ok = check_offline(schema)

    if not args.offline:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            print("\n⚠ ANTHROPIC_API_KEY 가 없어 LLM 구간을 건너뜁니다. "
                  "(--offline 로 이 경고를 숨길 수 있습니다)")
        else:
            ok = check_online(schema, api_key, args.full, args.model) and ok

    print("\n" + ("=" * 60))
    print("결과: " + ("✅ 전체 통과" if ok else "❌ 실패 항목 있음 — 위 로그 확인"))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
