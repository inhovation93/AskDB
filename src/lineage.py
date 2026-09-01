"""SQL 계보(lineage) 분석 — 생성된 SQL 이 실제로 건드린 테이블·컬럼을 추출한다.

왜 필요한가:
  사용자가 답을 신뢰할 수 있는지 판단하려면 "이 숫자가 어느 컬럼에서 나왔는가"를
  알아야 한다. 매출을 물었는데 `payments.amount` 를 썼는지 `order_items` 를 썼는지에
  따라 숫자가 달라지기 때문이다. 실무에서 Text2SQL 이 불신받는 가장 큰 이유가
  "근거를 볼 수 없다"는 점이므로, 이 모듈이 그 근거를 만든다.

구현:
  1차로 sqlglot 의 `qualify()` 로 모든 컬럼을 테이블에 정확히 귀속시킨다.
  실패하면(비표준 구문 등) 스키마 기반 휴리스틱으로 폴백해 최대한 복원한다.
  더불어 각 컬럼이 어느 절(SELECT / WHERE / JOIN ON / GROUP BY / ORDER BY / HAVING)에서
  쓰였는지 역할을 함께 기록한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

from .db import Schema

DIALECT = "sqlite"

# 절(clause) → 표시할 역할 이름
ROLE_ORDER = ["출력", "조건", "조인", "그룹", "정렬", "기타"]


@dataclass
class Lineage:
    """table -> {column -> set(역할)}"""
    columns: dict[str, dict[str, set[str]]] = field(default_factory=dict)
    star_tables: set[str] = field(default_factory=set)   # SELECT * 로 전체 컬럼 사용
    ctes: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)  # 소속을 못 정한 컬럼
    exact: bool = True                                   # qualify 성공 여부

    @property
    def table_count(self) -> int:
        return len(self.columns)

    @property
    def column_count(self) -> int:
        return sum(len(c) for c in self.columns.values())

    def rows(self) -> list[dict]:
        """UI 표시용 평탄화 — 테이블·컬럼·역할."""
        out: list[dict] = []
        for table in sorted(self.columns):
            for col in sorted(self.columns[table]):
                roles = self.columns[table][col]
                ordered = [r for r in ROLE_ORDER if r in roles]
                out.append({"테이블": table, "컬럼": col,
                            "사용된 곳": " · ".join(ordered) or "기타"})
        return out

    def summary(self) -> str:
        parts = [f"{t}({len(c)})" for t, c in sorted(self.columns.items())]
        return " + ".join(parts) if parts else "없음"


def _schema_dict(schema: Schema) -> dict[str, dict[str, str]]:
    """sqlglot qualify 가 요구하는 {테이블: {컬럼: 타입}} 형태."""
    return {name: {c.name: c.type for c in t.columns}
            for name, t in schema.tables.items()}


def _clause_roles(tree: exp.Expression) -> dict[int, set[str]]:
    """컬럼 노드 id → 역할 집합. 절별 서브트리를 훑어 역할을 부여한다."""
    roles: dict[int, set[str]] = {}

    def mark(node: exp.Expression | None, role: str) -> None:
        if node is None:
            return
        for col in node.find_all(exp.Column):
            roles.setdefault(id(col), set()).add(role)

    for select in tree.find_all(exp.Select):
        for projection in select.expressions:
            mark(projection, "출력")
    for node, role in (
        (exp.Where, "조건"),
        (exp.Group, "그룹"),
        (exp.Order, "정렬"),
        (exp.Having, "조건"),
    ):
        for found in tree.find_all(node):
            mark(found, role)
    for join in tree.find_all(exp.Join):
        mark(join.args.get("on"), "조인")
    return roles


def _alias_map(tree: exp.Expression) -> tuple[dict[str, str], set[str]]:
    """알리아스/테이블명 → 실제 테이블명, 그리고 CTE 이름 집합."""
    cte_names = {c.alias_or_name for c in tree.find_all(exp.CTE)}
    mapping: dict[str, str] = {}
    for tbl in tree.find_all(exp.Table):
        real = tbl.name
        if real in cte_names:
            continue
        mapping[real] = real
        alias = tbl.alias
        if alias:
            mapping[alias] = real
    return mapping, cte_names


def analyze(sql: str, schema: Schema) -> Lineage:
    """SQL 이 참조한 실제 테이블·컬럼을 추출한다. 실패해도 예외를 던지지 않는다."""
    lin = Lineage()
    try:
        tree = sqlglot.parse_one(sql, dialect=DIALECT)
    except Exception:
        lin.exact = False
        return lin
    if tree is None:
        lin.exact = False
        return lin

    aliases, cte_names = _alias_map(tree)
    lin.ctes = sorted(cte_names)
    schema_map = _schema_dict(schema)

    # SELECT * → 해당 테이블 전체 컬럼.
    # 주의: 모든 exp.Star 를 훑으면 COUNT(*) 의 * 까지 걸려 쓰지 않은 컬럼이 전부
    # 포함된다. 그래서 SELECT 절의 '프로젝션'만 검사한다.
    for select in tree.find_all(exp.Select):
        scope_tables = {t.name for t in select.find_all(exp.Table)
                        if t.name in schema_map}
        for projection in select.expressions:
            if isinstance(projection, exp.Star):
                lin.star_tables |= scope_tables            # SELECT *
            elif isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
                real = aliases.get(projection.table)       # SELECT t.*
                if real in schema_map:
                    lin.star_tables.add(real)

    roles = _clause_roles(tree)

    # (1) 정확한 경로: sqlglot qualify 로 컬럼을 테이블에 귀속
    qualified = None
    try:
        from sqlglot.optimizer.qualify import qualify
        qualified = qualify(tree.copy(), schema=schema_map, dialect=DIALECT,
                            validate_qualify_columns=False, quote_identifiers=False)
    except Exception:
        lin.exact = False

    if qualified is not None:
        q_aliases, q_ctes = _alias_map(qualified)
        q_roles = _clause_roles(qualified)
        for col in qualified.find_all(exp.Column):
            if isinstance(col.this, exp.Star):
                continue
            owner = aliases.get(col.table) or q_aliases.get(col.table)
            if owner is None or owner not in schema_map:
                continue
            if col.name not in schema_map[owner]:
                continue
            role = q_roles.get(id(col), set())
            lin.columns.setdefault(owner, {}).setdefault(col.name, set()).update(role)

    # (2) 폴백/보강: 원본 트리에서 직접 귀속 (qualify 가 놓친 것 포함)
    single_owner: dict[str, list[str]] = {}
    used_tables = [t for t in set(aliases.values()) if t in schema_map]
    for table in used_tables:
        for cname in schema_map[table]:
            single_owner.setdefault(cname, []).append(table)

    for col in tree.find_all(exp.Column):
        if isinstance(col.this, exp.Star):
            continue
        name = col.name
        if col.table:
            owner = aliases.get(col.table)
            if owner is None or owner not in schema_map:
                continue
        else:
            candidates = single_owner.get(name, [])
            if len(candidates) != 1:
                if candidates:
                    lin.unresolved.append(f"{name} (후보: {', '.join(candidates)})")
                continue
            owner = candidates[0]
        if name not in schema_map.get(owner, {}):
            continue
        role = roles.get(id(col), set())
        existing = lin.columns.setdefault(owner, {}).setdefault(name, set())
        existing.update(role)

    # star 테이블은 전체 컬럼을 채워 넣는다
    for table in lin.star_tables:
        for cname in schema_map.get(table, {}):
            lin.columns.setdefault(table, {}).setdefault(cname, set()).add("출력")

    lin.unresolved = sorted(set(lin.unresolved))
    return lin
