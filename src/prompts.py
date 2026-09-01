"""프롬프트 조립.

캐시 효율을 위해 프롬프트를 **안정(stable) / 가변(volatile)** 으로 분리한다.
  system[0] = 역할·규칙·세만틱 레이어  ← 요청마다 동일 → prompt caching 대상
  system[1] = 렌더링된 스키마            ← 스키마 링킹 결과에 따라 변함
  messages  = 질문 + 유사 예제 + (재시도 시) 오류 피드백
이 순서를 지키면 캐시 프리픽스가 깨지지 않아 반복 질의 비용이 크게 줄어든다.
"""
from __future__ import annotations

from .knowledge import BUSINESS_RULES

SQL_SYSTEM = f"""당신은 사내 데이터 분석 플랫폼의 SQL 생성 엔진입니다.
사용자의 한국어 질문을 **SQLite 방언의 단일 SELECT 문**으로 정확하게 번역하는 것이 임무입니다.

# 절대 규칙
1. SELECT 또는 WITH 로 시작하는 조회문 하나만 생성합니다. INSERT/UPDATE/DELETE/DROP/
   ALTER/CREATE/PRAGMA/ATTACH 는 어떤 이유로도 생성하지 않습니다.
2. 제공된 스키마에 실제로 존재하는 테이블·컬럼만 사용합니다. 추측해서 만들어내지 않습니다.
3. SQLite 문법만 사용합니다. EXTRACT / DATE_TRUNC / TOP / ILIKE / :: 캐스팅은 쓸 수 없습니다.
   날짜는 strftime, date, julianday 를 사용합니다.
4. 아래 【지표 표준 정의】를 반드시 따릅니다. 임의로 다른 정의를 쓰지 않습니다.
5. 질문에 답하기에 스키마 정보가 부족하거나, 질문이 데이터로 답할 수 없는 것이면
   SQL 대신 <clarify> 태그에 무엇이 필요한지 한국어로 씁니다.

# 출력 형식 (이 형식을 정확히 지킵니다)
<reasoning>
어떤 테이블을 왜 조인하는지, 어떤 필터·집계를 쓰는지 3~5줄로 간결하게.
</reasoning>
<sql>
SELECT ...
</sql>
<assumption>
질문에서 모호했던 부분을 어떻게 해석했는지 1~2줄. 모호함이 없으면 "없음".
</assumption>

# 회사 표준 비즈니스 규칙
{BUSINESS_RULES}
"""

ANSWER_SYSTEM = """당신은 데이터 분석 결과를 경영진에게 브리핑하는 애널리스트입니다.
SQL 실행 결과 표를 보고 한국어로 답합니다.

규칙:
- 첫 문장에서 질문에 직접 답합니다. 서론을 붙이지 않습니다.
- 표에 실제로 있는 숫자만 인용합니다. 없는 값을 추정하거나 만들지 않습니다.
- 큰 금액은 "12.3억원(1,230,000,000원)" 처럼 읽기 쉬운 단위를 함께 씁니다.
- 눈에 띄는 패턴·이상치가 있으면 1~2개만 짚습니다.
- 전체 4문장 이내. 마크다운 불릿은 최대 3개까지만 사용합니다.
- 결과가 0행이면 "조건에 맞는 데이터가 없습니다"라고 명확히 말하고 가능한 원인을 한 줄 덧붙입니다.
"""


def build_sql_messages(question: str, schema_text: str, examples_text: str,
                       today: str) -> tuple[list[dict], list[dict]]:
    """(system 블록, messages) 를 반환한다. system[0] 이 캐시 대상."""
    system = [
        {"type": "text", "text": SQL_SYSTEM},
        {"type": "text", "text": f"# 이번 질문에 사용할 스키마\n{schema_text}"},
    ]
    user = (
        f"# 검증된 유사 질의 예시 (문법·지표 정의 참고용)\n{examples_text}\n\n"
        f"# 오늘 날짜\n{today}\n\n"
        f"# 사용자 질문\n{question}"
    )
    return system, [{"role": "user", "content": user}]


def build_repair_message(bad_sql: str, error: str, attempt: int) -> dict:
    """자기수정(self-correction) 턴. 실패한 SQL 과 오류를 그대로 보여주고 고치게 한다."""
    return {
        "role": "user",
        "content": (
            f"방금 생성한 SQL 이 실패했습니다 (재시도 {attempt}회차).\n\n"
            f"## 실패한 SQL\n```sql\n{bad_sql}\n```\n\n"
            f"## 오류 내용\n{error}\n\n"
            "원인을 진단하고 **수정된 SQL 전체**를 같은 출력 형식(<reasoning>/<sql>/<assumption>)으로 "
            "다시 작성하세요. 스키마에 있는 컬럼만 쓰고, 오류가 지적한 부분을 반드시 고치세요."
        ),
    }


def build_answer_prompt(question: str, sql: str, table_markdown: str,
                        row_count: int, truncated: bool) -> str:
    note = f"\n(결과가 {row_count}행이며 표시 한도로 잘렸습니다)" if truncated else ""
    return (
        f"# 사용자 질문\n{question}\n\n"
        f"# 실행된 SQL\n```sql\n{sql}\n```\n\n"
        f"# 실행 결과 (총 {row_count}행){note}\n{table_markdown}\n\n"
        "위 결과로 질문에 답하세요."
    )
