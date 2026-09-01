# 🔎 AskDB — 사내 데이터 질문 → SQL 자동 생성·실행 시스템 (PoC)

한국어 질문 한 줄로 사내 DB 를 조회하는 Text2SQL 시스템의 프로토타입입니다.
"LLM 에 스키마 넣고 SQL 뽑기"에서 멈추지 않고, **실제 사내 DB 에 붙일 때 필요한
정확도 장치와 안전 장치를 파이프라인으로 명시화**한 것이 이 프로젝트의 핵심입니다.

```
질문 ─▶ ① 스키마 링킹 ─▶ ② 유사 예제 검색 ─▶ ③ SQL 생성(LLM)
        ─▶ ④ 정적 검증·가드레일 ─▶ ⑤ 읽기전용 실행 ─▶ ⑥ 실패 시 자기수정
        ─▶ ⑦ 사용 테이블·컬럼 추출 ─▶ 답변 + 차트 + 근거
```

---

## 1. 빠른 시작 (로컬)

```bash
pip install -r requirements.txt
```

API 키를 환경변수로 넣고 실행합니다.

```bash
export OPENAI_API_KEY="sk-..."   # Windows PowerShell: $env:OPENAI_API_KEY="sk-..."
streamlit run app.py
```

`ANTHROPIC_API_KEY` 를 넣으면 Claude 로 동작합니다. 공급자는 키 접두어로 자동 판별됩니다.

키 없이 실행해도 앱은 **오프라인 데모 모드**로 정상 동작합니다
(DB·가드레일·검증된 SQL 실행은 확인 가능, 자연어 → SQL 생성만 비활성).

### 파이프라인 단독 점검 (LLM 포함 E2E)

```bash
python scripts/smoke_test.py
```

`--full` 을 붙이면 골든셋 12문항 전체 평가를 CLI 에서 실행하고 정확도를 출력합니다.

---

## 2. Streamlit Community Cloud 배포

1. **GitHub 저장소에 푸시** (`.streamlit/secrets.toml` 은 `.gitignore` 로 제외되어 있음)

   ```bash
   git init
   git add .
   git commit -m "AskDB: Text2SQL PoC"
   git branch -M main
   git remote add origin https://github.com/<사용자명>/<저장소명>.git
   git push -u origin main
   ```

2. <https://share.streamlit.io> → **New app** → 저장소 선택
   - Main file path: `app.py`
   - Python version: 3.11 이상

3. **Secrets 설정** (⚠️ 이 단계를 빠뜨리면 제3자 접속 시 키 오류가 납니다)

   앱 화면 우측 상단 **⋮ → Settings → Secrets** 에 아래 한 줄을 붙여넣고 Save:

   ```toml
   OPENAI_API_KEY = "sk-실제키"
   ```

   (Claude 를 쓰려면 `ANTHROPIC_API_KEY` 로 넣으면 됩니다. 둘 중 하나만 있으면 됩니다.)

   저장하면 앱이 자동 재시작됩니다.

4. **제3자 접속 검증** — 시크릿 창(또는 휴대폰)으로 배포 URL 을 열고 확인:
   - [ ] 사이드바에 `OpenAI 연결됨 · 출처: 배포 Secrets (OPENAI_API_KEY)` 가 보인다
   - [ ] 예시 질문 버튼을 눌러 SQL 생성 → 결과 표·차트가 나온다
   - [ ] '자동 평가' 탭에서 `앞 6문항` 평가가 끝까지 돌아간다
   - [ ] 화면 어디에도 API 키가 노출되지 않는다

---

## 3. 프로젝트 구조

```
app.py                  Streamlit UI 조립 + 세션 상태 (비즈니스 로직 없음)
requirements.txt        의존성 6개 (배포 실패 지점을 줄이기 위해 최소화)
.streamlit/
  config.toml           테마
  secrets.toml.example  Secrets 형식 예시 (실제 키 없음)
scripts/
  smoke_test.py         LLM 포함 E2E 점검 CLI
src/
  sample_db.py          합성 샘플 DB 생성 (seed 고정, 9테이블 24,366행)
  db.py                 읽기전용 커넥션 · 스키마 인트로스펙션 · M-Schema 렌더링 · 실행
  erd.py                ERD 자동 생성 (Graphviz DOT, 스키마에서 파생 → 항상 최신)
  lineage.py            ⑦ 생성 SQL 이 실제로 읽은 테이블·컬럼 추출 (근거 제시)
  schema_linker.py      ① 질문 → 관련 테이블 선별 (동의어·코멘트·값·FK 연결성)
  knowledge.py          ② 세만틱 레이어(지표 정의) + Few-shot 뱅크 + 유사도 검색
  prompts.py            ③ 프롬프트 조립 (캐시 프리픽스 분리)
  llm.py                LLM 호출 래퍼 (OpenAI/Anthropic 추상화) · 토큰·비용 계측 · 오류 한글화
  guardrails.py         ④ sqlglot 기반 정적 검증 (DDL/DML 차단, LIMIT 주입 등)
  pipeline.py           ①~⑥ 오케스트레이션 + 실행 추적(Step) 기록
  evaluation.py         골든셋 12문항 · Execution Accuracy 채점기
```

**실제 사내 DB 로 교체할 때 손대는 곳은 `db.py` 의 `get_connection()` / `introspect()` 두 함수뿐**입니다.
가드레일·링킹·평가·UI 는 그대로 재사용됩니다.

---

## 4. 정확도를 만드는 3가지 설계

| 설계 | 내용 | 없으면 무슨 일이 생기나 |
|---|---|---|
| **세만틱 레이어** | "매출 = `SUM(수량×단가 − 할인)` where `status='완료'`" 등 지표를 프롬프트에 고정 | 같은 질문에 매번 다른 숫자가 나온다 |
| **값 기반 스키마 표현** | 컬럼 코멘트 + 실제 값 목록(`'완료' \| '취소'`) + 값 범위를 함께 주입 | "긴급 티켓"을 `priority='긴급'` 로 매핑하지 못한다 |
| **평가 루프** | 골든셋 실행 정확도를 언제든 재측정 (few-shot 과 질문 비중복) | 프롬프트 수정이 개선인지 퇴행인지 알 수 없다 |

여기에 **스키마 링킹**이 붙어, 테이블이 수천 개인 실제 DW 로 확장할 수 있는 구조를 갖춥니다
(현재 9테이블에서도 프롬프트 토큰 약 60% 절감).

---

## 5. 안전 설계

- **읽기 전용 2중 방어**
  1. 정적 검증: `sqlglot` AST 파싱 → 단일 `SELECT`/`WITH` 만 통과
  2. 드라이버: SQLite `file:...?mode=ro` 커넥션 (쓰기 자체가 불가능)
- **차단 대상**: `INSERT/UPDATE/DELETE/DROP/ALTER/CREATE/TRUNCATE/PRAGMA/ATTACH`,
  세미콜론 다중 문장, `sqlite_master`, `load_extension`, 존재하지 않는 테이블
- **민감 컬럼 훅**: `guardrails.BLOCKED_COLUMNS` — 데모에서는 `employees.annual_salary` 차단
- **자원 상한**: 결과 행 제한(기본 500), 쿼리 타임아웃 8초, 자기수정 횟수 상한
- **키 관리**: 코드·화면·저장소에 키가 없음. Streamlit Secrets / 환경변수 / 세션 입력만 사용
- **데이터**: 전부 seed 고정 난수로 만든 **가상 데이터**. 개인정보·사내 기밀 없음

---

## 6. 사용 기술

| 영역 | 기술 |
|---|---|
| LLM | **OpenAI GPT** (Chat Completions, `reasoning_effort`) 또는 **Anthropic Claude** (Messages API) — `src/llm.py` 가 키 접두어로 자동 판별 |
| 비용 최적화 | **프롬프트 캐싱** — 규칙·세만틱 레이어를 프롬프트 앞쪽에 고정 (OpenAI 자동 캐싱 / Anthropic `cache_control`) |
| SQL 안전성 | **sqlglot** — AST 파싱, 금지 노드 탐지, `LIMIT` 주입, 방언 정규화 |
| 데이터 | **SQLite** 읽기 전용 커넥션 + 합성 데이터셋 |
| ERD | **Graphviz DOT** 자동 생성 → `st.graphviz_chart` 가 브라우저에서 렌더링 (추가 설치물 없음) |
| UI | **Streamlit** (`chat_input`, `status`, `tabs`) + **Plotly Express** 자동 시각화 |
| 평가 | 자체 골든셋 + Execution Accuracy 채점기 (컬럼명·행순서 무시, 수치 반올림 비교) |

---

## 7. 다음 단계 (실서비스화 로드맵)

1. `db.py` 커넥션 팩토리 교체 → PostgreSQL / BigQuery / Snowflake 연결
2. `schema_linker.link()` 을 임베딩 검색 + 리랭커로 교체 (인터페이스 동일)
3. 예제 뱅크를 벡터 DB 로 영속화, 👍 피드백을 온라인 개선 신호로 사용
4. 사용자 권한(Row/Column Level Security)을 가드레일에 연동
5. 골든셋 200~500문항 확장 + CI 회귀 테스트
6. 질의 로그 기반 대시보드 자동 추천, 결과 캐싱, 동시 사용자 대응

---

© AskDB PoC — 심화과정 최종 개인 프로젝트. 모든 데이터는 가상입니다.
