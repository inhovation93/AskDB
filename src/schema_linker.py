"""스키마 링킹 — 질문과 관련된 테이블만 골라 프롬프트에 넣는다.

왜 필요한가:
  실제 사내 DW 는 테이블이 수백~수천 개다. 전체 스키마를 프롬프트에 넣으면
  (a) 컨텍스트 한계를 넘고 (b) 토큰 비용이 폭증하고 (c) 무관한 테이블이 오히려
  모델을 혼란시켜 정확도가 떨어진다. 그래서 "질문 → 후보 테이블" 축소가
  최신 Text2SQL 파이프라인의 1단계다.

이 구현은 임베딩 API 호출 없이(=지연/비용 0) 동작하는 하이브리드 어휘 매칭이다.
  · 한국어 도메인 동의어 사전
  · 테이블/컬럼명 + 카탈로그 코멘트의 문자 n-gram 유사도
  · 컬럼 실제 값(enum) 이 질문에 등장하는지 — 가장 강한 신호
  · FK 그래프로 조인 경로(브리지 테이블) 자동 보강
실서비스에서는 이 모듈만 벡터 검색/리랭커로 교체하면 나머지는 그대로 재사용된다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .db import Schema
from .knowledge import similarity

# 한국어 업무 용어 → 테이블 매핑 (도메인 온톨로지의 최소 형태)
SYNONYMS: dict[str, tuple[str, ...]] = {
    "매출": ("order_items", "orders"), "판매": ("order_items", "orders"),
    "수익": ("order_items", "orders"), "금액": ("order_items", "payments"),
    "객단가": ("order_items", "orders"), "aov": ("order_items", "orders"),
    "주문": ("orders", "order_items"), "구매": ("orders", "order_items"),
    "장바구니": ("order_items",), "수량": ("order_items",),
    "고객": ("customers",), "회원": ("customers",), "구매자": ("customers",),
    "세그먼트": ("customers",), "가입": ("customers",), "신규": ("customers",),
    "휴면": ("customers",), "활성": ("customers",), "재구매": ("orders", "customers"),
    "연령": ("customers",), "나이": ("customers",),
    "상품": ("products",), "제품": ("products",), "품목": ("products",),
    "sku": ("products",), "카테고리": ("products",), "단종": ("products",),
    "원가": ("products",), "마진": ("products", "order_items"),
    "이익": ("products", "order_items"), "마진율": ("products", "order_items"),
    "직원": ("employees",), "사원": ("employees",), "임직원": ("employees",),
    "영업사원": ("employees",), "담당자": ("employees",), "상사": ("employees",),
    "부서": ("employees",), "직급": ("employees",), "연봉": ("employees",),
    "입사": ("employees",), "팀장": ("employees",),
    "권역": ("regions",), "지역": ("regions", "customers"), "지방": ("regions",),
    "국가": ("regions",), "도시": ("customers",),
    "결제": ("payments",), "입금": ("payments",), "카드": ("payments",),
    "결제수단": ("payments",), "결제실패": ("payments",),
    "문의": ("support_tickets",), "cs": ("support_tickets",),
    "티켓": ("support_tickets",), "클레임": ("support_tickets",),
    "불만": ("support_tickets",), "만족도": ("support_tickets",),
    "csat": ("support_tickets",), "상담": ("support_tickets",),
    "광고": ("marketing_spend",), "마케팅": ("marketing_spend",),
    "캠페인": ("marketing_spend",), "roas": ("marketing_spend", "order_items"),
    "노출": ("marketing_spend",), "클릭": ("marketing_spend",),
    "ctr": ("marketing_spend",), "집행": ("marketing_spend",),
    "배송": ("orders",), "출고": ("orders",), "리드타임": ("orders",),
    "채널": ("orders",), "취소": ("orders",), "환불": ("orders",),
    "배송비": ("orders",),
}

# 최고 점수가 이 값 미만이면 링킹 신뢰 불가 → 전체 스키마 사용
MIN_CONFIDENCE = 3.0


@dataclass
class LinkResult:
    tables: list[str]                                   # 프롬프트에 넣을 테이블
    scores: dict[str, float] = field(default_factory=dict)
    evidence: dict[str, list[str]] = field(default_factory=dict)
    bridges: list[str] = field(default_factory=list)    # 조인 경로용으로 자동 추가
    fallback_all: bool = False                          # 확신 부족 → 전체 포함


def link(question: str, schema: Schema, max_tables: int = 6) -> LinkResult:
    q = question.lower()
    scores: dict[str, float] = {t: 0.0 for t in schema.table_names()}
    evidence: dict[str, list[str]] = {t: [] for t in schema.table_names()}

    # (1) 동의어 사전 — 가장 신뢰도 높은 신호
    for word, tables in SYNONYMS.items():
        if word in q:
            for i, t in enumerate(tables):
                if t in scores:
                    scores[t] += 3.0 if i == 0 else 1.5
                    evidence[t].append(f"용어 '{word}'")

    for tname, table in schema.tables.items():
        # (2) 테이블명 직접 언급
        if tname.lower() in q or tname.lower().replace("_", " ") in q:
            scores[tname] += 4.0
            evidence[tname].append(f"테이블명 '{tname}' 직접 언급")

        # (3) 테이블 코멘트 유사도
        if table.comment:
            sim = similarity(question, table.comment)
            if sim > 0.08:
                scores[tname] += sim * 4.0
                evidence[tname].append(f"설명 유사도 {sim:.2f}")

        for col in table.columns:
            # (4) 컬럼명/컬럼 코멘트 언급
            if col.name.lower() in q and len(col.name) > 3:
                scores[tname] += 2.0
                evidence[tname].append(f"컬럼 '{col.name}'")
            if col.comment:
                sim = similarity(question, col.comment)
                if sim > 0.16:
                    scores[tname] += sim * 2.0
                    evidence[tname].append(f"컬럼 '{col.name}' 설명 유사")

            # (5) 실제 값이 질문에 등장 — 매우 강한 신호 ("긴급 티켓", "간편결제 비중")
            for val in col.enum_values:
                if len(val) >= 2 and val.lower() in q:
                    scores[tname] += 5.0
                    evidence[tname].append(f"값 '{val}' ∈ {col.name}")

    selected = [t for t, s in sorted(scores.items(), key=lambda x: -x[1]) if s > 0][:max_tables]

    # (6) 확신이 부족하면 안전하게 전체 스키마를 넣는다 (정밀도보다 재현율 우선).
    #     테이블 "개수"가 아니라 최고 점수의 "세기"로 판단해야 단일 테이블 질문
    #     ("긴급 티켓 몇 건?")에서 불필요한 전체 폴백이 발생하지 않는다.
    if not selected or max(scores.values()) < MIN_CONFIDENCE:
        return LinkResult(tables=schema.table_names(), scores=scores,
                          evidence=evidence, fallback_all=True)

    # (7) 조인 경로 보강 — FK 그래프에서 선택된 테이블들이 끊어져 있을 때만
    #     연결에 필요한 브리지 테이블을 최소한으로 추가한다.
    #     (단순히 "이웃이 2개 이상"인 테이블을 다 넣으면 무관한 테이블이 딸려온다.)
    bridges = _connect(schema, selected)

    # order_items 는 status='완료' 필터를 위해 orders 가 반드시 함께 필요하다(의미적 의존).
    if "order_items" in selected and "orders" not in set(selected) | set(bridges):
        bridges.append("orders")

    ordered = [t for t in schema.table_names() if t in set(selected) | set(bridges)]
    return LinkResult(tables=ordered, scores=scores, evidence=evidence, bridges=bridges)


def _components(schema: Schema, nodes: list[str]) -> list[set[str]]:
    """주어진 테이블 집합을 FK 간선만으로 연결한 연결 요소들."""
    remaining = set(nodes)
    comps: list[set[str]] = []
    while remaining:
        seed = remaining.pop()
        comp, queue = {seed}, [seed]
        while queue:
            cur = queue.pop()
            for nb in schema.neighbors(cur) & remaining:
                remaining.discard(nb)
                comp.add(nb)
                queue.append(nb)
        comps.append(comp)
    return comps


def _connect(schema: Schema, selected: list[str], max_added: int = 3) -> list[str]:
    """끊어진 연결 요소를 잇는 브리지 테이블을 탐욕적으로 최소 개수만 추가한다."""
    added: list[str] = []
    current = list(selected)
    for _ in range(max_added):
        comps = _components(schema, current)
        if len(comps) <= 1:
            break
        best, best_touch = None, 1
        for cand in schema.table_names():
            if cand in current:
                continue
            nb = schema.neighbors(cand)
            touched = sum(1 for c in comps if nb & c)
            if touched > best_touch:
                best, best_touch = cand, touched
        if best is None:      # 더 이상 이을 수 없음 (예: marketing_spend 는 FK 가 없다)
            break
        added.append(best)
        current.append(best)
    return added
