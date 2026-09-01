"""DB 접근 계층.

책임 3가지:
 1) 샘플 DB 를 임시 파일로 1회 빌드 (배포 인스턴스당 1회)
 2) **읽기 전용(mode=ro) 커넥션**만 발급 → SQLite 드라이버 수준에서 쓰기 차단
 3) 스키마 메타데이터 자동 추출 + LLM 이 읽기 좋은 M-Schema 문자열 렌더링

실제 사내 DB(PostgreSQL/BigQuery 등)로 교체할 때 수정 지점은 `get_connection()` 과
`introspect()` 두 곳뿐이며, 그 위의 파이프라인/가드레일은 그대로 재사용된다.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .sample_db import (COLUMN_COMMENTS, DB_SCHEMA_VERSION, TABLE_COMMENTS,
                        build_database)

DB_FILENAME = f"text2sql_sample_{DB_SCHEMA_VERSION}.db"
# 저(低)카디널리티 TEXT 컬럼은 값 목록을 프롬프트에 넣어준다 (값 매핑 오류를 크게 줄임)
MAX_ENUM_VALUES = 12
QUERY_TIMEOUT_SEC = 8.0


class QueryTimeout(Exception):
    pass


# --------------------------------------------------------------------------
# 커넥션
# --------------------------------------------------------------------------
def db_path() -> Path:
    return Path(tempfile.gettempdir()) / DB_FILENAME


def ensure_database() -> Path:
    """샘플 DB 파일이 없으면 생성한다. 이미 있으면 재사용."""
    path = db_path()
    if path.exists() and path.stat().st_size > 0:
        return path
    tmp = path.with_suffix(".building")
    if tmp.exists():
        tmp.unlink()
    conn = sqlite3.connect(tmp)
    try:
        build_database(conn)
    finally:
        conn.close()
    os.replace(tmp, path)
    return path


def get_connection() -> sqlite3.Connection:
    """읽기 전용 커넥션. INSERT/UPDATE/DDL 은 드라이버가 거부한다."""
    path = ensure_database()
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# --------------------------------------------------------------------------
# 스키마 인트로스펙션
# --------------------------------------------------------------------------
@dataclass
class Column:
    name: str
    type: str
    nullable: bool
    is_pk: bool
    comment: str = ""
    fk: str | None = None          # "customers.customer_id"
    enum_values: list[str] = field(default_factory=list)
    value_range: str | None = None  # 날짜/숫자 컬럼의 최소~최대


@dataclass
class Table:
    name: str
    comment: str
    row_count: int
    columns: list[Column]

    @property
    def column_names(self) -> list[str]:
        return [c.name for c in self.columns]


@dataclass
class Schema:
    tables: dict[str, Table]
    foreign_keys: list[tuple[str, str]]  # ("orders.customer_id", "customers.customer_id")

    def table_names(self) -> list[str]:
        return list(self.tables)

    def neighbors(self, table: str) -> set[str]:
        """FK 로 직접 연결된 테이블 집합 (조인 경로 확장용)."""
        out: set[str] = set()
        for left, right in self.foreign_keys:
            lt, rt = left.split(".")[0], right.split(".")[0]
            if lt == table:
                out.add(rt)
            elif rt == table:
                out.add(lt)
        out.discard(table)
        return out


def introspect(conn: sqlite3.Connection) -> Schema:
    """PRAGMA 로 스키마를 읽고, 대표값/범위까지 샘플링한다."""
    tables: dict[str, Table] = {}
    fks: list[tuple[str, str]] = []

    names = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]

    for tname in names:
        fk_map: dict[str, str] = {}
        for fk in conn.execute(f"PRAGMA foreign_key_list('{tname}')"):
            fk_map[fk["from"]] = f"{fk['table']}.{fk['to']}"
            fks.append((f"{tname}.{fk['from']}", f"{fk['table']}.{fk['to']}"))

        row_count = conn.execute(f"SELECT COUNT(*) FROM '{tname}'").fetchone()[0]
        columns: list[Column] = []
        for info in conn.execute(f"PRAGMA table_info('{tname}')"):
            col = Column(
                name=info["name"],
                type=(info["type"] or "TEXT").upper(),
                nullable=not info["notnull"],
                is_pk=bool(info["pk"]),
                comment=COLUMN_COMMENTS.get(f"{tname}.{info['name']}", ""),
                fk=fk_map.get(info["name"]),
            )
            _enrich_column(conn, tname, col, row_count)
            columns.append(col)

        tables[tname] = Table(
            name=tname,
            comment=TABLE_COMMENTS.get(tname, ""),
            row_count=row_count,
            columns=columns,
        )
    return Schema(tables=tables, foreign_keys=fks)


def _enrich_column(conn: sqlite3.Connection, tname: str, col: Column, row_count: int) -> None:
    """저카디널리티 TEXT → 값 목록, 날짜/숫자 → 최소~최대 범위."""
    if col.is_pk or col.fk or row_count == 0:
        return
    try:
        if col.type.startswith("TEXT"):
            distinct = conn.execute(
                f"SELECT COUNT(DISTINCT \"{col.name}\") FROM '{tname}'").fetchone()[0]
            if 0 < distinct <= MAX_ENUM_VALUES:
                col.enum_values = [
                    str(r[0]) for r in conn.execute(
                        f"SELECT DISTINCT \"{col.name}\" FROM '{tname}' "
                        f"WHERE \"{col.name}\" IS NOT NULL "
                        f"ORDER BY 1 LIMIT {MAX_ENUM_VALUES}")]
            else:
                lo, hi = conn.execute(
                    f"SELECT MIN(\"{col.name}\"), MAX(\"{col.name}\") FROM '{tname}'").fetchone()
                if lo is not None and len(str(lo)) <= 24:
                    col.value_range = f"{lo} ~ {hi}"
        elif col.type.startswith(("INT", "REAL", "NUM")):
            lo, hi = conn.execute(
                f"SELECT MIN(\"{col.name}\"), MAX(\"{col.name}\") FROM '{tname}'").fetchone()
            if lo is not None:
                col.value_range = f"{lo} ~ {hi}"
    except sqlite3.Error:
        pass


# --------------------------------------------------------------------------
# M-Schema 렌더링 (LLM 입력용)
# --------------------------------------------------------------------------
def render_schema(schema: Schema, include: list[str] | None = None,
                  with_values: bool = True) -> str:
    """선택된 테이블만 상세 렌더링. include=None 이면 전체.

    M-Schema 형식: 테이블 목적 + 행 수 + (컬럼:타입, 제약, 코멘트, 예시값) + FK 목록.
    자연어 코멘트와 실제 값 예시를 함께 주는 것이 Text2SQL 정확도에 가장 크게 기여한다.
    """
    targets = include if include is not None else schema.table_names()
    targets = [t for t in targets if t in schema.tables]
    lines = ["【DB】 sample_commerce (SQLite / 읽기 전용)", "【Schema】"]

    for tname in targets:
        t = schema.tables[tname]
        lines.append(f"# Table: {t.name}  —  {t.comment}  (행 수 {t.row_count:,})")
        lines.append("[")
        for c in t.columns:
            parts = [f"{c.name}:{c.type}"]
            if c.is_pk:
                parts.append("PK")
            if c.fk:
                parts.append(f"FK→{c.fk}")
            if not c.nullable and not c.is_pk:
                parts.append("NOT NULL")
            if c.comment:
                parts.append(c.comment)
            if with_values and c.enum_values:
                parts.append("값: " + " | ".join(c.enum_values))
            elif with_values and c.value_range:
                parts.append(f"범위: {c.value_range}")
            lines.append("  (" + ", ".join(parts) + "),")
        lines.append("]")

    rel = [f"{a} = {b}" for a, b in schema.foreign_keys
           if a.split(".")[0] in targets and b.split(".")[0] in targets]
    if rel:
        lines.append("【Foreign keys】")
        lines.extend("  " + r for r in rel)

    skipped = [t for t in schema.table_names() if t not in targets]
    if skipped:
        lines.append("【이번 질문과 무관하다고 판단해 생략된 테이블】 " + ", ".join(skipped))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 실행
# --------------------------------------------------------------------------
def run_query(sql: str, max_rows: int = 1000,
              timeout: float = QUERY_TIMEOUT_SEC) -> tuple[pd.DataFrame, float]:
    """읽기 전용 커넥션에서 SQL 을 실행한다. (DataFrame, 소요 초) 반환.

    폭주 쿼리 방어: SQLite progress handler 로 wall-clock 타임아웃을 강제한다.
    """
    conn = get_connection()
    deadline = time.time() + timeout

    def _guard():
        return 1 if time.time() > deadline else 0

    conn.set_progress_handler(_guard, 10_000)
    started = time.time()
    try:
        cur = conn.execute(sql)
        rows = cur.fetchmany(max_rows)
        cols = [d[0] for d in cur.description] if cur.description else []
        df = pd.DataFrame([tuple(r) for r in rows], columns=cols)
    except sqlite3.OperationalError as exc:
        if "interrupted" in str(exc).lower():
            raise QueryTimeout(f"쿼리가 {timeout:.0f}초를 초과해 중단되었습니다.") from exc
        raise
    finally:
        conn.set_progress_handler(None, 0)
        conn.close()
    return df, time.time() - started


def explain(sql: str) -> str:
    """실행 없이 계획만 확인 → 문법/컬럼 오류를 실행 전에 잡는다."""
    conn = get_connection()
    try:
        rows = conn.execute(f"EXPLAIN QUERY PLAN {sql}").fetchall()
        return "\n".join(str(r["detail"]) for r in rows)
    finally:
        conn.close()
