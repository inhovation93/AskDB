# AskDB — Text2SQL 시스템 (Claude Code 작업 지침)

한국어 질문을 SQL 로 번역해 실행하는 Text2SQL 애플리케이션. Streamlit 웹앱.
현재는 합성 샘플 DB(SQLite)를 쓰는 PoC 이고, **최종 목표는 사내 실제 DB 에 붙이는 것**이다.

## 이 프로젝트의 존재 이유 (설계를 되돌리지 말 것)

"LLM 에 스키마 넣고 SQL 뽑기"는 이 프로젝트의 목표가 **아니다**. 그건 30줄이면 된다.
이 코드베이스의 가치는 **사내 DB 에 붙였을 때 터지는 두 문제를 막는 장치**에 있다.

1. **틀린 숫자를 자신 있게 답한다** → 세만틱 레이어 + 값 기반 스키마 + 평가 루프로 막는다
2. **위험한 쿼리가 실행된다** → 정적 검증 + 읽기전용 커넥션 2중 방어로 막는다

기능을 추가할 때 이 두 축을 약화시키지 않는지 먼저 확인한다.

## 파이프라인 (7단계)

```
질문 → ① 스키마 링킹 → ② 유사 예제 검색 → ③ SQL 생성 → ④ 정적 검증·가드레일
     → ⑤ 읽기전용 실행 → ⑥ 실패 시 자기수정 → ⑦ 사용 테이블·컬럼 추출
```

`src/pipeline.py` 의 `run()` 이 전 과정을 오케스트레이션하고, 각 단계를 `Step` 으로
기록한다. UI 의 "파이프라인 실행 추적"은 이 `Step` 리스트를 그대로 렌더링한 것이므로,
단계를 추가하면 `Step` 을 append 하는 것만으로 UI 에 자동 반영된다.

## 파일 구조와 책임

| 파일 | 책임 | 수정 시 주의 |
|---|---|---|
| `app.py` | UI 조립 + 세션 상태 **only** | 비즈니스 로직을 여기에 두지 않는다 |
| `src/db.py` | 커넥션 · 스키마 인트로스펙션 · M-Schema 렌더링 · 실행 | **사내 DB 교체 지점** (아래 참조) |
| `src/sample_db.py` | 합성 데이터 생성 (seed 고정) | 실제 DB 연결 시 삭제 대상 |
| `src/schema_linker.py` | ① 질문 → 관련 테이블 선별 | 임베딩 검색으로 교체 예정. `link()` 시그니처 유지 |
| `src/knowledge.py` | ② 세만틱 레이어(지표 정의) + few-shot 뱅크 | **지표 정의 변경은 신중히** — 답변 숫자가 바뀐다 |
| `src/prompts.py` | ③ 프롬프트 조립 | 캐시 프리픽스 순서를 지킬 것 (아래) |
| `src/llm.py` | LLM 호출 (OpenAI/Anthropic 추상화) | **공급자 분기는 이 파일 안에만** 존재해야 한다 |
| `src/guardrails.py` | ④ sqlglot 정적 검증 | 규칙을 느슨하게 하지 말 것 |
| `src/pipeline.py` | ①~⑦ 오케스트레이션 | |
| `src/lineage.py` | ⑦ SQL → 실제 사용 테이블·컬럼 | |
| `src/erd.py` | ERD Graphviz DOT 생성 | 스키마에서 자동 파생 — 하드코딩 금지 |
| `src/evaluation.py` | 골든셋 + Execution Accuracy 채점 | 골든셋 질문은 few-shot 과 겹치지 말 것 |
| `scripts/smoke_test.py` | E2E 점검 CLI | |

## 개발 명령

```bash
pip install -r requirements.txt
streamlit run app.py
```

```bash
python scripts/smoke_test.py --offline
```

LLM 없이 검증 가능한 전부를 점검한다: few-shot SQL 16개 실행, 골든셋 12개 실행,
가드레일 공격 7종 차단. **코드를 고친 뒤 반드시 이걸 먼저 돌린다.**

```bash
python scripts/smoke_test.py --full
```

`OPENAI_API_KEY` 또는 `ANTHROPIC_API_KEY` 가 환경변수에 있으면 골든셋 12문항의
실행 정확도를 측정한다. 프롬프트나 세만틱 레이어를 수정했으면 이 숫자를 수정 전후로
비교한다. 숫자로 확인하지 않은 프롬프트 변경은 개선이 아니라 도박이다.

## 반드시 알아야 하는 함정 (실제로 겪은 것들)

### Streamlit
- **`src/` 아래 파일을 고치면 서버를 재시작해야 한다.** Streamlit 은 rerun 때
  `app.py` 만 다시 실행하고 이미 import 된 모듈은 캐시를 쓴다. 새 함수를 추가하고
  `AttributeError: module 'src.x' has no attribute 'y'` 가 나면 이것이 원인이다.
- `use_container_width` 는 쓰지 않는다. **`width="stretch"`** 를 쓴다 (전자는 deprecated).
- 작은 Graphviz 그래프에 폭 맞춤을 걸면 글자가 거대해진다. 범례처럼 작은 것은
  Graphviz 대신 HTML 로 그린다 (`src/erd.py` 의 `LEGEND_HTML`).
- `st.dataframe` 은 canvas 로 렌더링되므로 `innerText` 에 셀 값이 안 잡힌다.
  브라우저로 검증할 때는 `[data-testid="stDataFrame"]` 의 `role="row"` 개수를 센다.

### OpenAI API
- **토큰 상한 파라미터를 보내지 않는다.** 모델 세대별로 `max_tokens` 와
  `max_completion_tokens` 중 하나만 허용해서 400 이 나기 쉽다. 출력이 SQL 한 개라
  프롬프트로 길이가 이미 제한된다.
- `reasoning_effort` 는 추론 모델만 받는다. `src/llm.py` 가 400 을 받으면 그 파라미터를
  빼고 1회 재시도하고, 해당 모델을 `_NO_EFFORT` 에 기억한다. 이 폴백을 제거하지 말 것.
- 모델 ID 를 코드에 확신 없이 박지 않는다. 사이드바의 "사용 가능한 모델 확인"이
  `models.list()` 로 실제 목록을 조회한다.
- 단가를 모르는 모델은 비용을 **표시하지 않는다** (`cost_usd()` 가 `None`).
  틀린 금액을 보여주는 것보다 낫다.

### 프롬프트 캐싱
`prompts.build_sql_messages()` 의 순서를 지킨다: `system[0]` = 불변 규칙·세만틱 레이어,
`system[1]` = 스키마, `messages` = 질문. 앞쪽에 가변 값(타임스탬프 등)을 넣으면
캐시 프리픽스가 매번 깨진다.

### sqlglot
- 오류 메시지에 ANSI 컬러 코드가 섞여 나온다. `guardrails.clean_error()` 로 제거한다.
- `exp.Star` 를 전부 훑으면 **`COUNT(*)` 의 `*` 까지 걸린다.** `SELECT *` 판정은
  SELECT 프로젝션만 검사해야 한다 (`src/lineage.py`). 이걸 놓치면 쓰지 않은 컬럼이
  근거 표에 전부 포함된다 — 실제로 7컬럼이 14컬럼으로 부풀었던 버그.

### 배포 (Streamlit Community Cloud)
- **`packages.txt` 로 시스템 패키지를 추가하지 않는다.** 의존성은 `requirements.txt`
  6개로 유지한다. ERD 를 Graphviz DOT 문자열로 넘기는 이유가 이것이다
  (브라우저가 렌더링하므로 시스템 `dot` 바이너리가 필요 없다).
- `.streamlit/secrets.toml` 은 **절대 커밋하지 않는다** (`.gitignore` 에 있음).
  형식 예시는 `.streamlit/secrets.toml.example` 참조.
- API 키는 Streamlit Secrets / 환경변수 / 세션 입력으로만 들어온다. 코드·화면에
  노출되는 경로를 만들지 말 것.

### Windows 환경
- 파일을 읽고 쓸 때 `encoding="utf-8"` 을 **항상 명시한다**. 생략하면 cp949 로 읽혀 깨진다.
- 콘솔 출력이 깨져 보이는 것은 cp949 표시 문제일 뿐 파일 내용은 정상인 경우가 많다.
  판단하기 전에 `ascii()` 나 `cat -A` 로 실제 바이트를 확인한다.

## 테스트 전략

실제 API 키 없이도 파이프라인 전체를 검증할 수 있게 만들어 두었다.

- **가짜 LLM 주입**: `llm.complete` 를 함수로 교체해 정해진 응답을 돌려준다.
  자기수정 루프, 출력 잘림, clarify 분기, `reasoning_effort` 폴백을 전부 이렇게 검증했다.
- **가드레일 공격 테스트**: `scripts/smoke_test.py` 가 DROP/다중문장/PRAGMA/ATTACH 등
  7종을 넣어 모두 차단되는지 확인한다. 가드레일을 수정하면 이 목록에 케이스를 추가한다.
- **골든셋**: `src/evaluation.py`. 실행 결과 비교(Execution Accuracy)로 채점하며,
  SQL 문자열 비교는 쓰지 않는다 (같은 뜻의 다른 SQL 을 오답 처리하므로).

## 사내 실제 DB 로 교체하는 방법

수정 지점은 `src/db.py` 두 함수뿐이다.

1. `get_connection()` — 사내 DB 커넥션 반환. **읽기 전용 계정**을 쓸 것
   (SQLite 의 `mode=ro` 에 대응하는 2차 방어선).
2. `introspect()` — `Schema` 데이터클래스를 채운다. 컬럼 코멘트와 저카디널리티
   컬럼의 실제 값 목록을 반드시 채울 것. 이 메타데이터 품질이 정확도를 좌우한다.

그 다음 순서로:
- `guardrails.DIALECT` 를 대상 DB 방언으로 변경 (`postgres`, `bigquery` 등)
- `knowledge.BUSINESS_RULES` 를 사내 지표 정의로 교체 — **가장 중요한 작업**
- `knowledge.FEWSHOT_BANK` 를 사내에서 실제로 검증된 쿼리로 교체
- `evaluation.GOLDEN_SET` 을 사내 질문으로 재구성 후 정확도 베이스라인 측정
- `guardrails.BLOCKED_COLUMNS` 에 사내 민감 컬럼 등록
- `src/sample_db.py` 삭제

## 코드 컨벤션

- 주석과 문서는 한국어. 코드 식별자는 영어.
- 주석은 "무엇을" 이 아니라 **"왜"** 를 쓴다. 특히 함정을 피하려고 그렇게 쓴 코드는
  이유를 남긴다 (그렇지 않으면 다음 사람이 "정리"하다가 버그를 되살린다).
- 새 의존성 추가는 신중히. 배포 실패 지점이 늘어난다.
- 사용자에게 보이는 오류 메시지는 한국어로, 다음 행동을 알려주는 문장으로 쓴다.
