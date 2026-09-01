"""SQL 가드레일 — LLM 이 생성한 SQL 을 실행 전에 정적 검증한다.

사내 DB 에 붙이는 순간 가장 중요한 계층. 방어 항목:
  1) 단일 문장만 허용 (세미콜론으로 이어붙인 문장 주입 차단)
  2) SELECT / WITH 만 허용 — INSERT·UPDATE·DELETE·DROP·ALTER·CREATE 전면 차단
  3) ATTACH / PRAGMA / sqlite_master 등 시스템 접근 차단
  4) 스키마에 없는 테이블 참조 차단 (환각 테이블 조기 검출)
  5) 카티전 곱·전체 스캔 방어용 LIMIT 자동 주입
  6) 금칙 컬럼(PII 등) 접근 차단 — 실제 DB 연동 시 화이트리스트/마스킹 지점

`db.py` 의 읽기 전용 커넥션과 합쳐 2중 방어(defense in depth)를 구성한다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp

DIALECT = "sqlite"

# 존재 자체가 금지된 표현식 노드
FORBIDDEN_NODES: tuple[type[exp.Expression], ...] = (
    exp.Insert, exp.Update, exp.Delete, exp.Drop, exp.Create, exp.Alter,
    exp.TruncateTable, exp.Attach, exp.Detach, exp.Pragma, exp.Set,
    exp.Command,  # VACUUM, REINDEX 등 파서가 Command 로 떨어뜨리는 것들
)
FORBIDDEN_KEYWORDS = (
    "attach", "detach", "pragma", "vacuum", "reindex", "sqlite_master",
    "sqlite_temp_master", "load_extension", "writefile", "readfile",
)
# 민감 컬럼 예시 — 실제 운영에서는 데이터 카탈로그의 태그를 읽어 채운다.
BLOCKED_COLUMNS: set[str] = {"employees.annual_salary"}

DEFAULT_ROW_LIMIT = 500


@dataclass
class GuardResult:
    ok: bool
    sql: str                              # 정규화 + LIMIT 주입된 최종 SQL
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    tables_used: list[str] = field(default_factory=list)
    limit_injected: bool = False


_ANSI = re.compile(r"\[[0-9;]*m")


def clean_error(message: str) -> str:
    """sqlglot 오류 메시지의 ANSI 컬러 코드를 제거한다(UI/LLM 재입력용)."""
    return _ANSI.sub("", str(message)).strip()


def strip_fences(text: str) -> str:
    """```sql ... ``` 코드펜스와 후행 세미콜론을 제거한다."""
    text = text.strip()
    fence = re.search(r"```(?:sql)?\s*(.+?)```", text, re.S | re.I)
    if fence:
        text = fence.group(1)
    return text.strip().rstrip(";").strip()


def validate(sql: str, allowed_tables: set[str],
             row_limit: int = DEFAULT_ROW_LIMIT,
             block_columns: bool = True) -> GuardResult:
    sql = strip_fences(sql)
    if not sql:
        return GuardResult(False, "", ["빈 SQL 이 생성되었습니다."])

    # (1) 단일 문장 검증
    try:
        statements = [s for s in sqlglot.parse(sql, dialect=DIALECT) if s is not None]
    except Exception as exc:  # sqlglot.ParseError 및 파생
        return GuardResult(False, sql, [f"SQL 구문 오류: {clean_error(exc)}"])

    if len(statements) != 1:
        return GuardResult(False, sql,
                           [f"한 번에 하나의 SELECT 문만 실행할 수 있습니다 "
                            f"(감지된 문장 수: {len(statements)})."])

    tree = statements[0]
    errors: list[str] = []
    warnings: list[str] = []

    # (2) 루트가 조회문인지
    if not isinstance(tree, (exp.Select, exp.Union, exp.Except, exp.Intersect, exp.Subquery)):
        errors.append(f"조회(SELECT/WITH) 문만 허용됩니다. 감지된 유형: {type(tree).__name__}")

    # (3) 금지 노드 / 키워드
    for node_type in FORBIDDEN_NODES:
        if list(tree.find_all(node_type)):
            errors.append(f"데이터를 변경하거나 시스템에 접근하는 구문({node_type.__name__})은 차단됩니다.")
            break
    lowered = sql.lower()
    for kw in FORBIDDEN_KEYWORDS:
        if re.search(rf"\b{re.escape(kw)}\b", lowered):
            errors.append(f"금지된 키워드가 포함되어 있습니다: {kw}")

    # (4) 테이블 존재 검증 — CTE 이름은 제외
    cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    used: list[str] = []
    for tbl in tree.find_all(exp.Table):
        name = (tbl.name or "").lower()
        if not name or name in cte_names:
            continue
        if name not in used:
            used.append(name)
        if name not in allowed_tables:
            errors.append(f"존재하지 않는 테이블을 참조했습니다: '{tbl.name}'. "
                          f"사용 가능한 테이블: {', '.join(sorted(allowed_tables))}")

    # (5) 금칙 컬럼
    if block_columns and BLOCKED_COLUMNS:
        for col in tree.find_all(exp.Column):
            for blocked in BLOCKED_COLUMNS:
                btable, bcol = blocked.split(".")
                if col.name.lower() == bcol and (btable in used or col.table.lower() in ("", btable)):
                    if btable in used:
                        errors.append(f"'{blocked}' 은(는) 접근이 제한된 민감 컬럼입니다.")
                        break

    # (6) LIMIT 자동 주입
    limit_injected = False
    if not errors and isinstance(tree, (exp.Select, exp.Union)):
        if tree.args.get("limit") is None:
            tree = tree.limit(row_limit)
            limit_injected = True
        if not list(tree.find_all(exp.Group)) and not list(tree.find_all(exp.AggFunc)):
            warnings.append("집계가 없는 조회입니다. 결과 행이 많을 수 있어 LIMIT 이 적용됩니다.")

    final_sql = tree.sql(dialect=DIALECT, pretty=True)
    return GuardResult(
        ok=not errors, sql=final_sql, errors=errors, warnings=warnings,
        tables_used=used, limit_injected=limit_injected,
    )
