"""지식 계층 — 세만틱 레이어(비즈니스 용어집) + Few-shot 예제 뱅크 + 검색기.

Text2SQL 의 정확도를 결정하는 것은 모델 크기가 아니라 **회사의 맥락을 얼마나 잘
주입하는가** 이다. 여기서 두 가지를 관리한다.

1. BUSINESS_RULES : "매출이란 무엇인가"를 못 박은 지표 정의(semantic layer).
   동일 질문에 대해 항상 같은 SQL 이 나오게 만드는 핵심 장치.
2. FEWSHOT_BANK   : (질문, 검증된 SQL) 쌍. 질문과 유사한 예제만 골라 프롬프트에 넣는다.
   사용자가 👍 한 쿼리를 여기에 축적하면 시스템이 스스로 좋아진다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# 1) 세만틱 레이어 — 지표 표준 정의
# ---------------------------------------------------------------------------
DATA_MIN_DATE = "2024-01-01"
DATA_MAX_DATE = "2026-08-31"

BUSINESS_RULES = f"""
【데이터 기간】 {DATA_MIN_DATE} ~ {DATA_MAX_DATE}. "최근 N개월/작년/올해" 같은 상대 기간은
  반드시 이 범위 안에서 해석한다. 필요하면 (SELECT MAX(order_date) FROM orders) 를 기준점으로 쓴다.
  날짜 컬럼은 모두 TEXT('YYYY-MM-DD' 또는 'YYYY-MM-DD HH:MM:SS')이므로
  strftime('%Y-%m', col) / date(col) / julianday(col) 로 다룬다. EXTRACT() 는 SQLite 에서 쓸 수 없다.

【지표 표준 정의 — 반드시 이 정의를 따른다】
- 매출(순매출, GMV): SUM(oi.quantity * oi.unit_price - oi.discount_amount)
    · order_items(oi) 를 orders(o) 와 조인하고 **o.status = '완료'** 조건을 붙인다.
    · orders 테이블에는 금액 컬럼이 없다. 배송비(o.shipping_fee)는 매출에 포함하지 않는다.
- 주문 건수: COUNT(DISTINCT o.order_id), 역시 status='완료' 기준.
- 평균 주문금액(AOV): 매출 / 주문 건수. order_items 를 조인하면 행이 불어나므로
    반드시 CTE 로 주문별 금액을 먼저 집계한 뒤 AVG 를 낸다.
- 매출총이익(마진): SUM(oi.quantity * (oi.unit_price - p.unit_cost) - oi.discount_amount)
- 마진율: 마진 / 매출
- 취소율/환불률: status IN ('취소','환불') 주문 수 / 전체 주문 수 (분모에는 status 조건 없음)
- 활성 고객: customers.is_active = 1
- 신규 고객: customers.signup_date 가 해당 기간에 속하는 고객
- 재구매 고객: status='완료' 주문이 2건 이상인 고객
- 배송 리드타임(일): julianday(o.ship_date) - julianday(o.order_date), ship_date IS NOT NULL 만
- CSAT: AVG(csat_score), csat_score IS NOT NULL 인 티켓만
- 티켓 미해결: support_tickets.closed_at IS NULL
- ROAS: 해당 월 매출 / 해당 월 marketing_spend.cost 합계
    (marketing_spend.spend_month 는 'YYYY-MM' 이므로 strftime('%Y-%m', o.order_date) 와 맞춘다)
- CTR: SUM(clicks) / SUM(impressions)

【컨벤션】
- 금액 단위는 원(KRW) 정수다. 억/만 단위 변환은 하지 말고 원 단위로 반환한다.
- 순위/TOP N 질문은 ORDER BY ... DESC LIMIT N 을 명시한다.
- 사람이 읽을 결과이므로 컬럼에 한글 알리아스를 붙인다. 예: AS 매출
- 0 나눗셈 방지: 비율 계산에는 NULLIF(분모, 0) 을 쓴다.
"""

# ---------------------------------------------------------------------------
# 2) Few-shot 예제 뱅크 (모두 실제 실행 검증된 SQL)
# ---------------------------------------------------------------------------
@dataclass
class Example:
    question: str
    sql: str
    tags: str = ""


FEWSHOT_BANK: list[Example] = [
    Example(
        "월별 매출 추이를 보여줘",
        """SELECT strftime('%Y-%m', o.order_date) AS 월,
       SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS 매출,
       COUNT(DISTINCT o.order_id) AS 주문건수
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
WHERE o.status = '완료'
GROUP BY 1
ORDER BY 1""",
        "시계열 매출",
    ),
    Example(
        "카테고리별 매출 상위 5개",
        """SELECT p.category AS 카테고리,
       SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS 매출
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.status = '완료'
GROUP BY 1
ORDER BY 매출 DESC
LIMIT 5""",
        "카테고리 순위",
    ),
    Example(
        "채널별 평균 주문금액(AOV)을 알려줘",
        """WITH order_total AS (
    SELECT o.order_id, o.channel,
           SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS amount
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.status = '완료'
    GROUP BY o.order_id, o.channel
)
SELECT channel AS 채널,
       COUNT(*) AS 주문건수,
       ROUND(AVG(amount), 0) AS 평균주문금액
FROM order_total
GROUP BY 1
ORDER BY 평균주문금액 DESC""",
        "AOV 주문단위 집계 CTE",
    ),
    Example(
        "2026년에 매출이 가장 높은 상품 10개와 그 마진율",
        """SELECT p.product_name AS 상품명,
       SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS 매출,
       ROUND(SUM(oi.quantity * (oi.unit_price - p.unit_cost) - oi.discount_amount) * 100.0
             / NULLIF(SUM(oi.quantity * oi.unit_price - oi.discount_amount), 0), 1) AS 마진율_퍼센트
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
JOIN products p ON p.product_id = oi.product_id
WHERE o.status = '완료'
  AND strftime('%Y', o.order_date) = '2026'
GROUP BY p.product_id, p.product_name
ORDER BY 매출 DESC
LIMIT 10""",
        "마진율 NULLIF 연도필터",
    ),
    Example(
        "권역별 고객 수와 매출을 함께 보여줘",
        """SELECT r.region_name AS 권역,
       COUNT(DISTINCT c.customer_id) AS 고객수,
       COALESCE(SUM(oi.quantity * oi.unit_price - oi.discount_amount), 0) AS 매출
FROM regions r
LEFT JOIN customers c ON c.region_id = r.region_id
LEFT JOIN orders o ON o.customer_id = c.customer_id AND o.status = '완료'
LEFT JOIN order_items oi ON oi.order_id = o.order_id
GROUP BY r.region_id, r.region_name
ORDER BY 매출 DESC""",
        "LEFT JOIN 조건절 위치",
    ),
    Example(
        "재구매 고객은 몇 명이고 전체 고객 중 몇 퍼센트야?",
        """WITH per_customer AS (
    SELECT customer_id, COUNT(DISTINCT order_id) AS cnt
    FROM orders
    WHERE status = '완료'
    GROUP BY customer_id
)
SELECT SUM(CASE WHEN cnt >= 2 THEN 1 ELSE 0 END) AS 재구매고객수,
       COUNT(*) AS 구매고객수,
       ROUND(SUM(CASE WHEN cnt >= 2 THEN 1 ELSE 0 END) * 100.0
             / NULLIF(COUNT(*), 0), 1) AS 재구매율_퍼센트
FROM per_customer""",
        "재구매 비율",
    ),
    Example(
        "전월 대비 매출 성장률을 계산해줘",
        """WITH monthly AS (
    SELECT strftime('%Y-%m', o.order_date) AS ym,
           SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS rev
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.status = '완료'
    GROUP BY 1
)
SELECT ym AS 월,
       rev AS 매출,
       LAG(rev) OVER (ORDER BY ym) AS 전월매출,
       ROUND((rev - LAG(rev) OVER (ORDER BY ym)) * 100.0
             / NULLIF(LAG(rev) OVER (ORDER BY ym), 0), 1) AS 전월대비_퍼센트
FROM monthly
ORDER BY ym""",
        "윈도우함수 LAG 성장률",
    ),
    Example(
        "월별 ROAS(광고비 대비 매출)를 보여줘",
        """WITH rev AS (
    SELECT strftime('%Y-%m', o.order_date) AS ym,
           SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS revenue
    FROM orders o
    JOIN order_items oi ON oi.order_id = o.order_id
    WHERE o.status = '완료'
    GROUP BY 1
),
spend AS (
    SELECT spend_month AS ym, SUM(cost) AS cost
    FROM marketing_spend
    GROUP BY 1
)
SELECT s.ym AS 월,
       r.revenue AS 매출,
       s.cost AS 광고비,
       ROUND(r.revenue * 1.0 / NULLIF(s.cost, 0), 2) AS ROAS
FROM spend s
LEFT JOIN rev r ON r.ym = s.ym
ORDER BY s.ym""",
        "ROAS 두 CTE 조인",
    ),
    Example(
        "배송 리드타임이 가장 긴 권역은?",
        """SELECT r.region_name AS 권역,
       ROUND(AVG(julianday(o.ship_date) - julianday(o.order_date)), 2) AS 평균리드타임_일,
       COUNT(*) AS 출고건수
FROM orders o
JOIN customers c ON c.customer_id = o.customer_id
JOIN regions r ON r.region_id = c.region_id
WHERE o.ship_date IS NOT NULL
GROUP BY r.region_id, r.region_name
ORDER BY 평균리드타임_일 DESC""",
        "julianday 리드타임",
    ),
    Example(
        "CS 문의 카테고리별 만족도와 미해결 건수",
        """SELECT category AS 문의유형,
       COUNT(*) AS 총건수,
       SUM(CASE WHEN closed_at IS NULL THEN 1 ELSE 0 END) AS 미해결건수,
       ROUND(AVG(csat_score), 2) AS 평균만족도
FROM support_tickets
GROUP BY 1
ORDER BY 총건수 DESC""",
        "CSAT NULL 처리",
    ),
    Example(
        "취소·환불된 주문의 비율을 채널별로 알려줘",
        """SELECT channel AS 채널,
       COUNT(*) AS 전체주문,
       SUM(CASE WHEN status IN ('취소','환불') THEN 1 ELSE 0 END) AS 취소환불건수,
       ROUND(SUM(CASE WHEN status IN ('취소','환불') THEN 1 ELSE 0 END) * 100.0
             / NULLIF(COUNT(*), 0), 2) AS 취소환불률_퍼센트
FROM orders
GROUP BY 1
ORDER BY 취소환불률_퍼센트 DESC""",
        "비율 분모에 status 조건 없음",
    ),
    Example(
        "영업사원별 담당 매출 순위와 그 사원의 상사 이름",
        """SELECT e.employee_name AS 사원,
       e.title AS 직급,
       m.employee_name AS 상사,
       SUM(oi.quantity * oi.unit_price - oi.discount_amount) AS 담당매출
FROM orders o
JOIN order_items oi ON oi.order_id = o.order_id
JOIN employees e ON e.employee_id = o.employee_id
LEFT JOIN employees m ON m.employee_id = e.manager_id
WHERE o.status = '완료'
GROUP BY e.employee_id, e.employee_name, e.title, m.employee_name
ORDER BY 담당매출 DESC
LIMIT 10""",
        "자기참조 조인 SELF JOIN",
    ),
    Example(
        "고객 세그먼트별 1인당 평균 결제금액",
        """SELECT c.segment AS 고객구분,
       COUNT(DISTINCT c.customer_id) AS 고객수,
       ROUND(SUM(pay.amount) * 1.0 / NULLIF(COUNT(DISTINCT c.customer_id), 0), 0) AS 인당결제금액
FROM customers c
JOIN orders o ON o.customer_id = c.customer_id
JOIN payments pay ON pay.order_id = o.order_id AND pay.status = '성공'
GROUP BY 1
ORDER BY 인당결제금액 DESC""",
        "결제 성공만 세그먼트",
    ),
    Example(
        "2026년에 가입한 신규 고객이 월별로 몇 명이야?",
        """SELECT strftime('%Y-%m', signup_date) AS 가입월,
       COUNT(*) AS 신규고객수
FROM customers
WHERE strftime('%Y', signup_date) = '2026'
GROUP BY 1
ORDER BY 1""",
        "신규 가입 코호트",
    ),
    Example(
        "주문이 한 번도 없는 고객 목록",
        """SELECT c.customer_id, c.customer_name AS 고객명, c.segment AS 고객구분, c.signup_date AS 가입일
FROM customers c
LEFT JOIN orders o ON o.customer_id = c.customer_id
WHERE o.order_id IS NULL
ORDER BY c.signup_date DESC""",
        "안티조인 NOT EXISTS",
    ),
    Example(
        "결제 수단별 실패율",
        """SELECT method AS 결제수단,
       COUNT(*) AS 결제시도,
       SUM(CASE WHEN status <> '성공' THEN 1 ELSE 0 END) AS 실패건수,
       ROUND(SUM(CASE WHEN status <> '성공' THEN 1 ELSE 0 END) * 100.0
             / NULLIF(COUNT(*), 0), 2) AS 실패율_퍼센트
FROM payments
GROUP BY 1
ORDER BY 실패율_퍼센트 DESC""",
        "결제 실패율",
    ),
]

# ---------------------------------------------------------------------------
# 3) 유사 예제 검색 — 한국어에 강한 문자 n-gram 자카드 유사도
#    (임베딩 API 없이 즉시 동작하고, 배포 환경에 추가 의존성이 없다.
#     실 서비스에서는 이 함수만 벡터 DB 검색으로 교체하면 된다.)
# ---------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"[0-9A-Za-z가-힣]+")


def _ngrams(text: str, n: int = 2) -> set[str]:
    norm = "".join(_TOKEN_RE.findall(text.lower()))
    if len(norm) < n:
        return {norm} if norm else set()
    return {norm[i:i + n] for i in range(len(norm) - n + 1)}


def similarity(a: str, b: str) -> float:
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def retrieve_examples(question: str, bank: list[Example] | None = None,
                      k: int = 4) -> list[tuple[Example, float]]:
    """질문과 가장 유사한 예제 k개를 (예제, 점수) 로 반환."""
    pool = FEWSHOT_BANK if bank is None else bank
    scored = [(ex, similarity(question, ex.question + " " + ex.tags)) for ex in pool]
    scored.sort(key=lambda x: x[1], reverse=True)
    return scored[:k]


def render_examples(pairs: list[tuple[Example, float]]) -> str:
    if not pairs:
        return "(유사 예제 없음)"
    out = []
    for i, (ex, score) in enumerate(pairs, 1):
        out.append(f"<예제 {i} 유사도=\"{score:.2f}\">\n"
                   f"질문: {ex.question}\nSQL:\n{ex.sql}\n</예제 {i}>")
    return "\n\n".join(out)
