"""합성(合成) 샘플 데이터베이스 생성기.

실제 사내 데이터를 쓰지 않고, 동일한 스키마 복잡도(9개 테이블 / 다중 조인 / 자기참조 FK)를
가진 가상의 이커머스 회사 데이터를 결정론적으로(seed 고정) 생성한다.

- 개인정보/기밀 없음: 모든 값은 난수 생성물이다.
- 결정론적: seed 가 고정되어 있어 배포 환경마다 동일한 결과 → 평가 재현성 확보.
- 실제 DB 로 교체할 때는 이 파일을 버리고 `db.py` 의 커넥션 팩토리만 바꾸면 된다.
"""
from __future__ import annotations

import random
import sqlite3
from datetime import date, datetime, timedelta

SEED = 42
DB_SCHEMA_VERSION = "v1"

DDL = """
CREATE TABLE regions (
    region_id   INTEGER PRIMARY KEY,
    region_name TEXT    NOT NULL,
    country     TEXT    NOT NULL
);

CREATE TABLE employees (
    employee_id   INTEGER PRIMARY KEY,
    employee_name TEXT    NOT NULL,
    department    TEXT    NOT NULL,
    title         TEXT    NOT NULL,
    region_id     INTEGER REFERENCES regions(region_id),
    manager_id    INTEGER REFERENCES employees(employee_id),
    hire_date     TEXT    NOT NULL,
    annual_salary INTEGER NOT NULL
);

CREATE TABLE customers (
    customer_id   INTEGER PRIMARY KEY,
    customer_name TEXT    NOT NULL,
    segment       TEXT    NOT NULL,
    region_id     INTEGER REFERENCES regions(region_id),
    city          TEXT    NOT NULL,
    signup_date   TEXT    NOT NULL,
    birth_year    INTEGER,
    is_active     INTEGER NOT NULL
);

CREATE TABLE products (
    product_id      INTEGER PRIMARY KEY,
    product_name    TEXT    NOT NULL,
    category        TEXT    NOT NULL,
    subcategory     TEXT    NOT NULL,
    unit_price      INTEGER NOT NULL,
    unit_cost       INTEGER NOT NULL,
    launch_date     TEXT    NOT NULL,
    is_discontinued INTEGER NOT NULL
);

CREATE TABLE orders (
    order_id      INTEGER PRIMARY KEY,
    customer_id   INTEGER NOT NULL REFERENCES customers(customer_id),
    employee_id   INTEGER REFERENCES employees(employee_id),
    order_date    TEXT    NOT NULL,
    ship_date     TEXT,
    status        TEXT    NOT NULL,
    channel       TEXT    NOT NULL,
    shipping_fee  INTEGER NOT NULL
);

CREATE TABLE order_items (
    order_item_id   INTEGER PRIMARY KEY,
    order_id        INTEGER NOT NULL REFERENCES orders(order_id),
    product_id      INTEGER NOT NULL REFERENCES products(product_id),
    quantity        INTEGER NOT NULL,
    unit_price      INTEGER NOT NULL,
    discount_amount INTEGER NOT NULL
);

CREATE TABLE payments (
    payment_id INTEGER PRIMARY KEY,
    order_id   INTEGER NOT NULL REFERENCES orders(order_id),
    paid_at    TEXT    NOT NULL,
    amount     INTEGER NOT NULL,
    method     TEXT    NOT NULL,
    status     TEXT    NOT NULL
);

CREATE TABLE support_tickets (
    ticket_id   INTEGER PRIMARY KEY,
    customer_id INTEGER NOT NULL REFERENCES customers(customer_id),
    order_id    INTEGER REFERENCES orders(order_id),
    opened_at   TEXT    NOT NULL,
    closed_at   TEXT,
    priority    TEXT    NOT NULL,
    category    TEXT    NOT NULL,
    csat_score  INTEGER
);

CREATE TABLE marketing_spend (
    spend_id      INTEGER PRIMARY KEY,
    spend_month   TEXT    NOT NULL,
    channel       TEXT    NOT NULL,
    campaign_name TEXT    NOT NULL,
    cost          INTEGER NOT NULL,
    impressions   INTEGER NOT NULL,
    clicks        INTEGER NOT NULL
);

CREATE INDEX idx_orders_date ON orders(order_date);
CREATE INDEX idx_orders_customer ON orders(customer_id);
CREATE INDEX idx_items_order ON order_items(order_id);
CREATE INDEX idx_items_product ON order_items(product_id);
"""

# ---------------------------------------------------------------------------
# 데이터 카탈로그: SQLite 는 컬럼 코멘트를 저장하지 못하므로 별도 메타데이터로 관리한다.
# 실제 운영에서는 dbt / DataHub / Glue Catalog 등에서 주입되는 자리다.
# ---------------------------------------------------------------------------
TABLE_COMMENTS = {
    "regions": "영업 권역 마스터. 1행 = 1권역",
    "employees": "임직원 마스터. manager_id 는 같은 테이블을 참조하는 자기참조 FK",
    "customers": "고객 마스터. 1행 = 1고객",
    "products": "상품 마스터. 1행 = 1상품(SKU)",
    "orders": "주문 헤더. 1행 = 1주문. 헤더에 금액 컬럼이 없으므로 금액은 order_items 로 조인",
    "order_items": "주문 상세. 1행 = 주문 내 1상품 라인. 매출 계산의 기준 테이블",
    "payments": "결제 원장. 1행 = 1결제 시도. status='성공' 인 건만 실제 입금",
    "support_tickets": "고객 문의(CS) 티켓. csat_score 는 1~5, 미응답이면 NULL",
    "marketing_spend": "채널별 월간 마케팅 집행비. spend_month 는 'YYYY-MM' 문자열",
}

COLUMN_COMMENTS = {
    "regions.region_name": "권역명",
    "employees.department": "부서",
    "employees.title": "직급",
    "employees.manager_id": "직속 상사의 employee_id (자기참조)",
    "employees.annual_salary": "연봉(원)",
    "customers.segment": "고객 구분",
    "customers.city": "도시명",
    "customers.signup_date": "가입일 (YYYY-MM-DD)",
    "customers.birth_year": "출생연도. 미기재 고객은 NULL",
    "customers.is_active": "활성 여부. 1=활성, 0=휴면",
    "products.unit_price": "정가(원)",
    "products.unit_cost": "원가(원). 마진 계산에 사용",
    "products.is_discontinued": "단종 여부. 1=단종",
    "orders.order_date": "주문일 (YYYY-MM-DD)",
    "orders.ship_date": "출고일. 미출고/취소 주문은 NULL",
    "orders.status": "주문 상태. 매출 집계 시 '완료' 만 포함",
    "orders.channel": "유입 채널",
    "orders.employee_id": "담당 영업사원. 온라인 주문은 NULL",
    "orders.shipping_fee": "배송비(원)",
    "order_items.quantity": "수량",
    "order_items.unit_price": "주문 시점 판매단가(원). products.unit_price 와 다를 수 있음",
    "order_items.discount_amount": "라인 할인액(원)",
    "payments.paid_at": "결제 일시 (YYYY-MM-DD HH:MM:SS)",
    "payments.amount": "결제 금액(원)",
    "payments.method": "결제 수단",
    "payments.status": "결제 상태",
    "support_tickets.opened_at": "문의 접수 일시",
    "support_tickets.closed_at": "문의 종료 일시. 미해결이면 NULL",
    "support_tickets.csat_score": "만족도 1~5점. 미응답 NULL",
    "marketing_spend.spend_month": "집행 월 'YYYY-MM'",
    "marketing_spend.cost": "집행 금액(원)",
}

_REGIONS = [
    (1, "수도권", "대한민국"),
    (2, "영남권", "대한민국"),
    (3, "호남권", "대한민국"),
    (4, "충청권", "대한민국"),
    (5, "강원제주", "대한민국"),
    (6, "해외", "일본"),
]
_CITY_BY_REGION = {
    1: ["서울", "인천", "성남", "수원", "고양"],
    2: ["부산", "대구", "울산", "창원", "포항"],
    3: ["광주", "전주", "여수", "목포"],
    4: ["대전", "청주", "천안", "세종"],
    5: ["춘천", "강릉", "제주", "원주"],
    6: ["도쿄", "오사카", "후쿠오카"],
}
_SEGMENTS = ["개인", "중소기업", "대기업", "공공기관"]
_DEPARTMENTS = ["영업", "마케팅", "CS", "물류", "데이터"]
_TITLES = ["사원", "대리", "과장", "팀장", "이사"]
_CATEGORIES = {
    "전자기기": ["노트북", "스마트폰", "태블릿", "이어폰"],
    "생활가전": ["청소기", "공기청정기", "전자레인지"],
    "패션": ["아웃도어", "신발", "가방"],
    "뷰티": ["스킨케어", "메이크업"],
    "식품": ["간편식", "커피", "건강식품"],
    "스포츠": ["피트니스", "캠핑"],
    "도서": ["IT", "경영", "소설"],
}
_ORDER_STATUS = ["완료", "배송중", "취소", "환불"]
_ORDER_STATUS_W = [0.78, 0.10, 0.07, 0.05]
_CHANNELS = ["웹", "모바일앱", "오프라인매장", "파트너몰"]
_CHANNEL_W = [0.30, 0.42, 0.16, 0.12]
_PAY_METHODS = ["신용카드", "간편결제", "계좌이체", "법인카드"]
_TICKET_CATEGORIES = ["배송지연", "환불요청", "상품불량", "결제오류", "단순문의"]
_PRIORITIES = ["낮음", "보통", "높음", "긴급"]
_MK_CHANNELS = ["검색광고", "SNS광고", "이메일", "제휴마케팅"]

_SURNAMES = "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백유남심노"
_GIVEN = ["민준", "서연", "도윤", "지우", "예준", "하윤", "주원", "지호", "수아", "지훈",
          "다은", "현우", "유진", "성민", "채원", "건우", "소율", "재현", "은우", "나윤"]

DATA_START = date(2024, 1, 1)
DATA_END = date(2026, 8, 31)


def _rand_date(rng: random.Random, start: date, end: date) -> date:
    return start + timedelta(days=rng.randint(0, (end - start).days))


def _seasonal_weight(d: date) -> float:
    """연말/신학기 성수기 + 완만한 성장 추세를 반영한 일별 가중치."""
    growth = 1.0 + 0.45 * ((d - DATA_START).days / max((DATA_END - DATA_START).days, 1))
    season = {1: 0.85, 2: 0.80, 3: 1.05, 4: 0.95, 5: 1.00, 6: 0.95,
              7: 1.05, 8: 1.00, 9: 1.10, 10: 1.05, 11: 1.35, 12: 1.30}[d.month]
    return growth * season


def build_database(conn: sqlite3.Connection) -> dict:
    """빈 커넥션에 스키마와 합성 데이터를 적재하고 테이블별 행 수를 반환한다."""
    rng = random.Random(SEED)
    cur = conn.cursor()
    cur.executescript(DDL)

    # --- regions ---------------------------------------------------------
    cur.executemany("INSERT INTO regions VALUES (?,?,?)", _REGIONS)

    # --- employees (자기참조 FK: 팀장/이사가 상사) -------------------------
    employees = []
    leaders: list[int] = []
    for eid in range(1, 41):
        name = rng.choice(_SURNAMES) + rng.choice(_GIVEN)
        dept = _DEPARTMENTS[(eid - 1) % len(_DEPARTMENTS)]
        title = rng.choices(_TITLES, weights=[0.34, 0.26, 0.20, 0.14, 0.06])[0]
        region_id = rng.randint(1, 6)
        hire = _rand_date(rng, date(2018, 1, 1), date(2026, 3, 1))
        base = {"사원": 3600, "대리": 4800, "과장": 6300, "팀장": 8200, "이사": 11500}[title]
        salary = (base + rng.randint(-300, 500)) * 10_000
        manager = rng.choice(leaders) if (leaders and title != "이사") else None
        if title in ("팀장", "이사"):
            leaders.append(eid)
        employees.append((eid, name, dept, title, region_id, manager,
                          hire.isoformat(), salary))
    cur.executemany("INSERT INTO employees VALUES (?,?,?,?,?,?,?,?)", employees)
    sales_ids = [e[0] for e in employees if e[2] == "영업"]

    # --- customers -------------------------------------------------------
    customers = []
    for cid in range(1, 601):
        region_id = rng.choices(range(1, 7), weights=[0.42, 0.18, 0.12, 0.12, 0.09, 0.07])[0]
        segment = rng.choices(_SEGMENTS, weights=[0.55, 0.25, 0.12, 0.08])[0]
        suffix = {"개인": "", "중소기업": " 상사", "대기업": " 홀딩스", "공공기관": "청"}[segment]
        name = rng.choice(_SURNAMES) + rng.choice(_GIVEN) + suffix
        signup = _rand_date(rng, date(2023, 1, 1), DATA_END)
        birth = rng.randint(1960, 2006) if rng.random() > 0.08 else None
        customers.append((cid, name, segment, region_id, rng.choice(_CITY_BY_REGION[region_id]),
                          signup.isoformat(), birth, 1 if rng.random() > 0.22 else 0))
    cur.executemany("INSERT INTO customers VALUES (?,?,?,?,?,?,?,?)", customers)

    # --- products --------------------------------------------------------
    products = []
    cat_items = [(c, s) for c, subs in _CATEGORIES.items() for s in subs]
    for i in range(120):
        pid = i + 1
        cat, sub = cat_items[i % len(cat_items)]
        price = rng.choice([9900, 19900, 29900, 49000, 89000, 129000, 259000, 549000, 1290000])
        cost = int(price * rng.uniform(0.45, 0.78))
        launch = _rand_date(rng, date(2022, 6, 1), date(2026, 6, 1))
        products.append((pid, f"{sub} {chr(65 + i % 26)}{i + 1:03d}", cat, sub, price, cost,
                         launch.isoformat(), 1 if rng.random() < 0.12 else 0))
    cur.executemany("INSERT INTO products VALUES (?,?,?,?,?,?,?,?)", products)
    live_products = [p for p in products if p[7] == 0]

    # --- orders / order_items / payments --------------------------------
    orders, items, payments = [], [], []
    item_id = 0
    pay_id = 0
    total_days = (DATA_END - DATA_START).days
    day_pool = [DATA_START + timedelta(days=i) for i in range(total_days + 1)]
    day_weights = [_seasonal_weight(d) for d in day_pool]
    order_days = sorted(rng.choices(day_pool, weights=day_weights, k=6000))

    for oid, od in enumerate(order_days, start=1):
        cust = rng.choice(customers)
        if cust[5] > od.isoformat():  # 가입일 이전 주문 방지(데이터 정합성)
            cust = customers[0]
        channel = rng.choices(_CHANNELS, weights=_CHANNEL_W)[0]
        status = rng.choices(_ORDER_STATUS, weights=_ORDER_STATUS_W)[0]
        emp = rng.choice(sales_ids) if channel in ("오프라인매장", "파트너몰") else None
        ship = None
        if status in ("완료", "환불"):
            ship = (od + timedelta(days=rng.randint(1, 6))).isoformat()
        elif status == "배송중":
            ship = (od + timedelta(days=rng.randint(1, 3))).isoformat()
        fee = 0 if rng.random() < 0.55 else rng.choice([2500, 3000, 5000])
        orders.append((oid, cust[0], emp, od.isoformat(), ship, status, channel, fee))

        order_amount = 0
        for _ in range(rng.choices([1, 2, 3, 4], weights=[0.50, 0.28, 0.15, 0.07])[0]):
            item_id += 1
            prod = rng.choice(live_products)
            qty = rng.choices([1, 2, 3, 5], weights=[0.68, 0.20, 0.08, 0.04])[0]
            unit = int(prod[4] * rng.choice([1.0, 1.0, 1.0, 0.95, 0.9]))
            disc = int(unit * qty * rng.choice([0, 0, 0, 0.05, 0.10, 0.15]))
            items.append((item_id, oid, prod[0], qty, unit, disc))
            order_amount += unit * qty - disc

        if status != "취소":
            pay_id += 1
            paid_at = datetime.combine(od, datetime.min.time()) + timedelta(
                hours=rng.randint(8, 23), minutes=rng.randint(0, 59))
            pstatus = "성공" if rng.random() > 0.04 else rng.choice(["실패", "취소"])
            method = rng.choices(_PAY_METHODS, weights=[0.42, 0.34, 0.12, 0.12])[0]
            payments.append((pay_id, oid, paid_at.strftime("%Y-%m-%d %H:%M:%S"),
                             order_amount + fee, method, pstatus))

    cur.executemany("INSERT INTO orders VALUES (?,?,?,?,?,?,?,?)", orders)
    cur.executemany("INSERT INTO order_items VALUES (?,?,?,?,?,?)", items)
    cur.executemany("INSERT INTO payments VALUES (?,?,?,?,?,?)", payments)

    # --- support_tickets -------------------------------------------------
    tickets = []
    for tid in range(1, 1201):
        order = rng.choice(orders)
        opened = datetime.fromisoformat(order[3]) + timedelta(
            days=rng.randint(0, 20), hours=rng.randint(0, 23))
        if opened.date() > DATA_END:
            opened = datetime.combine(DATA_END, datetime.min.time())
        resolved = rng.random() > 0.12
        closed = None
        if resolved:
            closed = (opened + timedelta(hours=rng.randint(1, 120))).strftime("%Y-%m-%d %H:%M:%S")
        csat = None
        if resolved and rng.random() > 0.25:
            csat = rng.choices([1, 2, 3, 4, 5], weights=[0.06, 0.09, 0.20, 0.35, 0.30])[0]
        tickets.append((tid, order[1], order[0], opened.strftime("%Y-%m-%d %H:%M:%S"),
                        closed, rng.choices(_PRIORITIES, weights=[0.30, 0.40, 0.22, 0.08])[0],
                        rng.choice(_TICKET_CATEGORIES), csat))
    cur.executemany("INSERT INTO support_tickets VALUES (?,?,?,?,?,?,?,?)", tickets)

    # --- marketing_spend -------------------------------------------------
    spends = []
    sid = 0
    y, m = DATA_START.year, DATA_START.month
    while (y, m) <= (DATA_END.year, DATA_END.month):
        for ch in _MK_CHANNELS:
            sid += 1
            cost = rng.randint(800, 6500) * 10_000
            impr = cost // rng.randint(80, 260)
            clicks = int(impr * rng.uniform(0.008, 0.045))
            spends.append((sid, f"{y:04d}-{m:02d}", ch,
                           f"{y}년 {m}월 {ch} 캠페인", cost, impr, clicks))
        m += 1
        if m == 13:
            y, m = y + 1, 1
    cur.executemany("INSERT INTO marketing_spend VALUES (?,?,?,?,?,?,?)", spends)

    conn.commit()
    return {
        "regions": len(_REGIONS), "employees": len(employees), "customers": len(customers),
        "products": len(products), "orders": len(orders), "order_items": len(items),
        "payments": len(payments), "support_tickets": len(tickets),
        "marketing_spend": len(spends),
    }
