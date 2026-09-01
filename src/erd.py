"""ERD(개체-관계도) 생성 — 스키마를 Graphviz DOT 문자열로 렌더링한다.

왜 DOT 문자열인가:
  `st.graphviz_chart()` 는 DOT 문자열을 그대로 받아 **브라우저에서** 렌더링한다.
  파이썬 graphviz 패키지도, 시스템 `dot` 바이너리도 필요 없다.
  → 배포 환경(Streamlit Cloud)에 추가 설치물이 없으므로 배포 실패 지점이 늘지 않는다.

`db.introspect()` 결과에서 자동 생성되므로, 실제 사내 DB 로 교체해도
ERD 가 스키마와 자동으로 동기화된다(문서가 코드와 어긋나지 않는다).
"""
from __future__ import annotations

from .db import Schema

# 테이블 성격별 색상 — 팩트(트랜잭션) / 디멘션(마스터) / 독립 테이블
FACT_TABLES = {"orders", "order_items", "payments", "support_tickets"}
STANDALONE_TABLES = {"marketing_spend"}

PALETTE = {
    "fact": {"head": "#1d4ed8", "sub": "#dbeafe", "subtext": "#1e3a8a"},
    "dim": {"head": "#0891b2", "sub": "#cffafe", "subtext": "#155e75"},
    "standalone": {"head": "#7c3aed", "sub": "#ede9fe", "subtext": "#5b21b6"},
}
HIGHLIGHT_BORDER = "#f59e0b"
NORMAL_BORDER = "#cbd5e1"


def _kind(table_name: str) -> str:
    if table_name in FACT_TABLES:
        return "fact"
    if table_name in STANDALONE_TABLES:
        return "standalone"
    return "dim"


def _esc(text: str) -> str:
    """HTML-like 레이블용 이스케이프."""
    return (str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def _short_comment(comment: str, limit: int = 26) -> str:
    """긴 한글 코멘트는 자른다 — 렌더러가 한글 폭을 과소 계산해 박스가 깨지는 것을 방지."""
    head = comment.split(".")[0].split("·")[0].strip()
    return head[:limit] + ("…" if len(head) > limit else "")


def build_dot(schema: Schema, *, highlight: set[str] | None = None,
              keys_only: bool = False, rankdir: str = "LR") -> str:
    """스키마로부터 ERD DOT 소스를 만든다.

    highlight : 강조할 테이블 이름 (예: 직전 질문에서 파이프라인이 선택한 테이블)
    keys_only : True 면 PK/FK 컬럼만 표시 (한눈에 관계만 보고 싶을 때)
    rankdir   : "LR"(좌→우) 또는 "TB"(위→아래)
    """
    highlight = highlight or set()
    lines = [
        "digraph ERD {",
        f'  graph [rankdir={rankdir}, bgcolor="transparent", pad="0.3",'
        '         nodesep="0.55", ranksep="1.15", fontname="Helvetica"];',
        '  node  [shape=plain, fontname="Helvetica"];',
        '  edge  [color="#94a3b8", penwidth="1.4", fontname="Helvetica",'
        '         fontsize="9", fontcolor="#475569"];',
    ]

    # ── 노드 (테이블) ────────────────────────────────────────────────
    for name, table in schema.tables.items():
        colors = PALETTE[_kind(name)]
        border = HIGHLIGHT_BORDER if name in highlight else NORMAL_BORDER
        width = "3" if name in highlight else "1"

        rows = [
            f'    <TR><TD BGCOLOR="{colors["head"]}" ALIGN="CENTER">'
            f'<FONT COLOR="#ffffff" POINT-SIZE="14"><B>{_esc(name)}</B></FONT></TD></TR>',
            f'    <TR><TD BGCOLOR="{colors["sub"]}" ALIGN="CENTER">'
            f'<FONT COLOR="{colors["subtext"]}" POINT-SIZE="9">'
            f'{_esc(_short_comment(table.comment))} · {table.row_count:,}행'
            f'</FONT></TD></TR>',
        ]

        for col in table.columns:
            is_key = col.is_pk or bool(col.fk)
            if keys_only and not is_key:
                continue
            if col.is_pk:
                badge = '<FONT COLOR="#b45309"><B>PK</B></FONT> '
                text = f"<B>{_esc(col.name)}</B>"
            elif col.fk:
                badge = '<FONT COLOR="#0e7490"><B>FK</B></FONT> '
                text = _esc(col.name)
            else:
                badge = '<FONT COLOR="#cbd5e1">　</FONT> '
                text = _esc(col.name)
            rows.append(
                f'    <TR><TD PORT="{col.name}" ALIGN="LEFT" BGCOLOR="#ffffff">'
                f'<FONT POINT-SIZE="10">{badge}{text}'
                f'<FONT COLOR="#94a3b8"> {_esc(col.type[:7])}</FONT>'
                f'</FONT></TD></TR>')

        label = (
            "<\n  <TABLE BORDER=\"" + width + f'" CELLBORDER="0" CELLSPACING="0" '
            f'CELLPADDING="5" COLOR="{border}">\n'
            + "\n".join(rows)
            + "\n  </TABLE>\n  >"
        )
        lines.append(f"  {name} [label={label}];")

    # ── 간선 (외래키) ────────────────────────────────────────────────
    # 자식(N측) → 부모(1측). dir=back + arrowtail=crow 로 N 측에 까치발 표기.
    for left, right in schema.foreign_keys:
        ct, cc = left.split(".")
        pt, pc = right.split(".")
        if ct not in schema.tables or pt not in schema.tables:
            continue
        self_ref = ct == pt
        style = ', style="dashed"' if self_ref else ""
        extra = ', constraint=false' if self_ref else ""
        lines.append(
            f'  {ct}:{cc} -> {pt}:{pc} '
            f'[dir=back, arrowtail=crow, arrowhead=none, '
            f'taillabel="N", headlabel="1", labeldistance="1.6"{style}{extra}];')

    lines.append("}")
    return "\n".join(lines)


# 범례는 Graphviz 가 아니라 HTML 칩으로 그린다.
# (작은 그래프를 컬럼 폭에 맞춰 늘리면 글자가 비정상적으로 커진다)
def _chip(bg: str, label: str) -> str:
    return (f'<span style="background:{bg};color:#fff;padding:.22rem .62rem;'
            'border-radius:6px;font-size:.78rem;font-weight:600;'
            'display:inline-block;margin:0 .35rem .35rem 0">'
            f'{label}</span>')


LEGEND_HTML = (
    '<div style="margin:.1rem 0 .5rem">'
    + _chip(PALETTE["fact"]["head"], "트랜잭션 테이블 (팩트)")
    + _chip(PALETTE["dim"]["head"], "마스터 테이블 (디멘션)")
    + _chip(PALETTE["standalone"]["head"], "독립 테이블")
    + _chip(HIGHLIGHT_BORDER, "직전 질문에서 선택된 테이블")
    + '</div>'
)


def relation_sentences(schema: Schema) -> list[str]:
    """FK 관계를 한국어 문장으로 풀어 쓴다 (ERD 를 못 읽는 사람을 위한 보조 설명)."""
    label = {
        "regions": "권역", "employees": "임직원", "customers": "고객",
        "products": "상품", "orders": "주문", "order_items": "주문상세",
        "payments": "결제", "support_tickets": "CS티켓", "marketing_spend": "마케팅비",
    }
    out = []
    for left, right in schema.foreign_keys:
        ct, cc = left.split(".")
        pt, _ = right.split(".")
        cn, pn = label.get(ct, ct), label.get(pt, pt)
        if ct == pt:
            out.append(f"**{cn}** 는 자기 자신을 참조한다 (`{cc}` → 상사). "
                       f"조직도를 표현하는 자기참조 관계")
        else:
            out.append(f"**{pn}** 1건에 **{cn}** 여러 건이 달린다 "
                       f"(`{ct}.{cc}` → `{right}`)")
    return out
