"""자동 평가 — 골든셋 기반 실행 정확도(Execution Accuracy) 측정.

Text2SQL 을 "데모"에서 "제품"으로 넘기는 결정적 차이는 **평가 루프의 존재**다.
프롬프트를 고칠 때마다 정확도가 오르는지 내리는지 숫자로 확인할 수 있어야 한다.

지표: Execution Accuracy
  생성 SQL 의 실행 결과와 정답 SQL 의 실행 결과가 (컬럼명 무시, 행 순서 무시,
  소수 2자리 반올림 기준) 동일한 값 집합이면 정답으로 센다.
  SQL 문자열 일치(Exact Match)는 같은 뜻의 다른 SQL 을 오답 처리하므로 쓰지 않는다.

골든셋 질문은 few-shot 예제 뱅크와 **겹치지 않게** 구성했다(정보 누출 방지).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import pandas as pd

from . import pipeline
from .db import Schema, run_query


@dataclass
class GoldItem:
    id: str
    question: str
    gold_sql: str
    difficulty: str      # 쉬움 | 보통 | 어려움
    skill: str           # 평가하려는 역량


GOLDEN_SET: list[GoldItem] = [
    GoldItem("G01", "2025년 총 매출은 얼마야?",
             """SELECT SUM(oi.quantity * oi.unit_price - oi.discount_amount)
FROM orders o JOIN order_items oi ON oi.order_id = o.order_id
WHERE o.status = '완료' AND strftime('%Y', o.order_date) = '2025'""",
             "쉬움", "지표 정의 준수 + 연도 필터"),
    GoldItem("G02", "긴급 우선순위로 접수된 CS 티켓은 몇 건이야?",
             "SELECT COUNT(*) FROM support_tickets WHERE priority = '긴급'",
             "쉬움", "값 매핑(enum)"),
    GoldItem("G03", "부서별 직원 수를 알려줘",
             "SELECT department, COUNT(*) FROM employees GROUP BY department ORDER BY 2 DESC",
             "쉬움", "단순 그룹핑"),
    GoldItem("G04", "결제가 성공한 건들의 총 결제금액은?",
             "SELECT SUM(amount) FROM payments WHERE status = '성공'",
             "쉬움", "상태 필터"),
    GoldItem("G05", "출생연도가 1990년 이후인 고객은 몇 명이야?",
             "SELECT COUNT(*) FROM customers WHERE birth_year >= 1990",
             "쉬움", "NULL 안전 비교"),
    GoldItem("G06", "판매 수량이 가장 많은 상품 카테고리 3개를 알려줘",
             """SELECT p.category, SUM(oi.quantity)
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.status = '완료'
GROUP BY p.category ORDER BY 2 DESC LIMIT 3""",
             "보통", "3중 조인 + TOP N"),
    GoldItem("G07", "고객 구분별 완료 주문 건수를 보여줘",
             """SELECT c.segment, COUNT(DISTINCT o.order_id)
FROM orders o JOIN customers c ON c.customer_id = o.customer_id
WHERE o.status = '완료'
GROUP BY c.segment ORDER BY 2 DESC""",
             "보통", "조인 + DISTINCT 카운트"),
    GoldItem("G08", "2026년에 월별로 신규 가입한 고객 수를 알려줘",
             """SELECT strftime('%Y-%m', signup_date), COUNT(*)
FROM customers WHERE strftime('%Y', signup_date) = '2026'
GROUP BY 1 ORDER BY 1""",
             "보통", "날짜 함수 + 월 집계"),
    GoldItem("G09", "단종되지 않은 상품 중 정가가 가장 비싼 상품 5개의 이름과 정가",
             """SELECT product_name, unit_price FROM products
WHERE is_discontinued = 0 ORDER BY unit_price DESC, product_name LIMIT 5""",
             "보통", "불리언 플래그 + 정렬"),
    GoldItem("G10", "미해결 티켓이 가장 많은 문의 유형 하나만 알려줘",
             """SELECT category, COUNT(*) FROM support_tickets
WHERE closed_at IS NULL GROUP BY category ORDER BY 2 DESC LIMIT 1""",
             "보통", "NULL 조건 + 최대값"),
    GoldItem("G11", "완료 주문이 5건 이상인 고객은 몇 명이야?",
             """SELECT COUNT(*) FROM (
  SELECT customer_id FROM orders WHERE status = '완료'
  GROUP BY customer_id HAVING COUNT(DISTINCT order_id) >= 5)""",
             "어려움", "HAVING 후 재집계(서브쿼리)"),
    GoldItem("G12", "상품 카테고리별 매출총이익을 큰 순서로 보여줘",
             """SELECT p.category,
       SUM(oi.quantity * (oi.unit_price - p.unit_cost) - oi.discount_amount)
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.status = '완료'
GROUP BY p.category ORDER BY 2 DESC""",
             "어려움", "마진 공식 정확히 적용"),
]


# --------------------------------------------------------------------------
# 결과 비교
# --------------------------------------------------------------------------
def _normalize(df: pd.DataFrame) -> list[tuple]:
    """컬럼명 무시, 행 순서 무시, 숫자는 2자리 반올림한 값 집합으로 정규화."""
    rows: list[tuple] = []
    for row in df.itertuples(index=False, name=None):
        norm = []
        for v in row:
            if v is None or (isinstance(v, float) and pd.isna(v)):
                norm.append(None)
            elif isinstance(v, (int, float)):
                norm.append(round(float(v), 2))
            else:
                norm.append(str(v).strip())
        rows.append(tuple(norm))
    return sorted(rows, key=lambda r: [(x is None, str(x)) for x in r])


def compare(pred: pd.DataFrame, gold: pd.DataFrame) -> tuple[bool, str]:
    if pred.shape[1] != gold.shape[1]:
        return False, f"컬럼 수 불일치 (생성 {pred.shape[1]} vs 정답 {gold.shape[1]})"
    if len(pred) != len(gold):
        return False, f"행 수 불일치 (생성 {len(pred)} vs 정답 {len(gold)})"
    if _normalize(pred) == _normalize(gold):
        return True, "값 일치"
    return False, "행 수는 같지만 값이 다릅니다"


@dataclass
class EvalRow:
    item: GoldItem
    correct: bool
    reason: str
    pred_sql: str | None
    attempts: int
    elapsed: float
    usage: object = None
    error: str | None = None


@dataclass
class EvalReport:
    rows: list[EvalRow] = field(default_factory=list)

    @property
    def accuracy(self) -> float:
        return sum(r.correct for r in self.rows) / len(self.rows) if self.rows else 0.0

    def by_difficulty(self) -> dict[str, tuple[int, int]]:
        out: dict[str, tuple[int, int]] = {}
        for r in self.rows:
            hit, total = out.get(r.item.difficulty, (0, 0))
            out[r.item.difficulty] = (hit + int(r.correct), total + 1)
        return out

    @property
    def avg_elapsed(self) -> float:
        return sum(r.elapsed for r in self.rows) / len(self.rows) if self.rows else 0.0

    @property
    def self_corrected(self) -> int:
        return sum(1 for r in self.rows if r.attempts > 1)


def evaluate_one(item: GoldItem, *, client, schema: Schema,
                 options: pipeline.Options) -> EvalRow:
    res = pipeline.run(item.question, client=client, schema=schema, options=options)
    if not res.ok or res.df is None:
        return EvalRow(item, False, "SQL 생성/실행 실패", res.sql, res.attempts,
                       res.elapsed, res.usage, res.error or res.clarify)
    gold_df, _ = run_query(item.gold_sql, max_rows=options.row_limit)
    correct, reason = compare(res.df, gold_df)
    return EvalRow(item, correct, reason, res.sql, res.attempts, res.elapsed, res.usage)
