# 인수인계 — 사내 환경에서 이어받기

작성 시점: 2026-09-01. 교육용 PC 에서 작업했고 그 PC 는 초기화되므로,
**이 저장소에 있는 것이 전부**다. 로컬에만 있던 것은 없다(확인 완료).

---

## 1. 사내 PC 에서 시작하기

```bash
git clone https://github.com/inhovation93/AskDB.git
cd AskDB
pip install -r requirements.txt
```

```bash
python scripts/smoke_test.py --offline
```

`✅ 전체 통과` 가 나오면 환경이 정상이다. (LLM 없이 DB·가드레일·골든셋을 점검한다.)

```bash
streamlit run app.py
```

API 키 없이도 **오프라인 데모 모드**로 뜬다. 실제 SQL 생성을 쓰려면 환경변수에
`OPENAI_API_KEY` 또는 `ANTHROPIC_API_KEY` 를 넣거나, 사이드바에 키를 붙여넣는다.

Claude Code 로 열면 `CLAUDE.md` 가 자동으로 읽힌다. 설계 의도·함정·컨벤션이 거기 있다.

---

## 2. 현재 상태

| 항목 | 상태 |
|---|---|
| 코드 | GitHub `main` 에 전부 있음. 로컬 전용 파일 없음 |
| 배포 | Streamlit Community Cloud, 저장소 연동 → `main` 푸시 시 자동 재배포 |
| Secrets | Streamlit Cloud 의 **Manage app → Settings → Secrets** 에 API 키 저장됨 |
| 데이터 | 합성 샘플 DB (SQLite, 9테이블 24,366행). 개인정보·기밀 없음 |
| LLM | OpenAI / Anthropic 양쪽 지원. 키 접두어로 자동 판별 |

### 검증된 것

- few-shot SQL 16개 + 골든셋 정답 SQL 12개 = **28개 전부 실행 성공**
- 가드레일 공격 **7종 전부 차단** (DROP · 다중문장 · UPDATE · PRAGMA · sqlite_master ·
  없는 테이블 · ATTACH)
- 파이프라인 제어 흐름 **7개 시나리오** (정상 / 환각 테이블 / 문법 오류 / 2회 실패 후 성공 /
  전부 실패 / 출력 잘림 / clarify) — 가짜 LLM 주입으로 검증
- OpenAI 백엔드 **7개 시나리오** (사용량 파싱 / `reasoning_effort` 폴백 /
  `finish_reason=length` 매핑 / 파이프라인 통합 / 401·404·429 메시지 / 공급자 판별 / 비용 표시)
- 계보 분석: 28개 SQL 전부 컬럼 귀속 성공, 미해결 컬럼 0건
- Streamlit 4개 탭 렌더링, 서버 예외·경고 0건
- ERD 렌더링 (노드 9 / 간선 10), 계보 표 렌더링 — 브라우저 DOM 으로 확인

### ⚠️ 검증되지 않은 것 — 가장 먼저 확인할 항목

**실제 LLM API 호출을 한 번도 하지 않았다.** 교육용 PC 에 키가 없었고,
사용자가 검증 생략을 선택했다.

- 호출 파라미터는 설치된 SDK 시그니처와 정적으로 대조해 맞는 것을 확인함
- 하지만 실제 HTTP 응답을 받아본 적은 없음
- **가장 가능성 있는 실패**: 기본 모델 `gpt-5` 가 계정에서 안 먹는 경우.
  이때 화면에 `'gpt-5' 모델을 사용할 수 없습니다…` 가 뜨고, 사이드바
  **"사용 가능한 모델 확인"** 버튼으로 실제 목록을 조회해 바꾸면 된다.

첫 작업으로 이걸 돌린다:

```bash
python scripts/smoke_test.py --full
```

---

## 3. 다음에 할 일 (우선순위 순)

### 즉시

1. **실제 LLM 경로 검증** — 위 `--full` 실행. 골든셋 12문항 정확도 베이스라인을 기록한다.
   이 숫자가 이후 모든 개선의 기준선이 된다.
2. 정확도가 낮게 나온 문항의 생성 SQL 을 확인한다. UI 자동 평가 탭에서
   문항별로 생성 SQL 과 정답 SQL 을 나란히 비교할 수 있다.
3. 오답 패턴이 지표 정의 문제면 `knowledge.BUSINESS_RULES` 를 고치고 정확도를 재측정한다.

### 사내 DB 연결 (핵심 작업)

`CLAUDE.md` 의 "사내 실제 DB 로 교체하는 방법" 절을 따른다. 요약하면:

1. `src/db.py` 의 `get_connection()` — **읽기 전용 계정**으로 사내 DB 연결
2. `src/db.py` 의 `introspect()` — 컬럼 코멘트와 저카디널리티 값 목록을 반드시 채운다
3. `guardrails.DIALECT` 를 대상 방언으로 변경
4. `knowledge.BUSINESS_RULES` 를 **사내 지표 정의**로 교체 ← 정확도에 가장 큰 영향
5. `knowledge.FEWSHOT_BANK` 를 사내 검증 쿼리로 교체
6. `evaluation.GOLDEN_SET` 을 사내 질문 30~50개로 재구성, 정확도 재측정
7. `guardrails.BLOCKED_COLUMNS` 에 사내 민감 컬럼 등록
8. `src/sample_db.py` 삭제

**순서가 중요하다.** 1~3 만 하고 4~6 을 건너뛰면 "그럴듯하지만 틀린 숫자"를 답하는
시스템이 된다. 지표 정의와 평가셋이 정확도의 대부분을 만든다.

### 그 다음

- 스키마 링킹을 임베딩 검색 + 리랭커로 교체 (`schema_linker.link()` 시그니처 유지)
- 예제 뱅크를 벡터 DB 로 영속화, 👍 피드백을 개선 신호로 축적
  (현재는 세션 메모리에만 쌓여 새로고침하면 사라진다)
- 사용자 권한(Row/Column Level Security)을 가드레일에 연동
- 골든셋을 CI 회귀 테스트로 편입
- 질의 로그 적재 → 자주 묻는 질문 대시보드화

---

## 4. 사내 환경에서 달라질 것들

| 항목 | 교육 PC | 사내에서 확인할 것 |
|---|---|---|
| API 키 | 없음 (오프라인 모드) | 사내에서 허용된 LLM 공급자·모델 확인. 사내 프록시/게이트웨이를 쓰면 `src/llm.py` 의 클라이언트 생성에 `base_url` 추가 |
| 배포 | Streamlit Community Cloud (공개) | 사내 데이터를 다루면 **공개 배포 금지**. 사내 서버 / Streamlit 사내 호스팅으로 이전 |
| DB | 합성 SQLite | 읽기 전용 계정, 네트워크 접근 권한, 쿼리 타임아웃 정책 확인 |
| 데이터 민감도 | 없음 | 개인정보 컬럼 마스킹 정책을 `BLOCKED_COLUMNS` 에 반영 |

**공개 배포 주의**: 현재 Streamlit Community Cloud 배포본은 누구나 접근할 수 있다.
사내 스키마나 데이터를 넣는 순간 그 배포는 내려야 한다. 지금 배포본은 합성
데이터만 쓰므로 제출·시연 용도로는 문제없다.

---

## 5. 이 PC 를 떠나기 전 정리 (보안)

교육용 공유 PC 이므로 자격 증명을 남기지 않는다.

```powershell
cmdkey /delete:git:https://github.com
```

- GitHub → Settings → **Sessions** 에서 이 PC 세션 로그아웃
- GitHub → Settings → **Applications** → Git Credential Manager 있으면 Revoke
- 브라우저에서 GitHub / Streamlit / OpenAI 로그아웃 + 저장된 비밀번호·기록 삭제
- **OpenAI API 키 재발급** (공유 PC 브라우저에 키를 붙여넣었으므로).
  재발급 후 Streamlit Secrets 도 새 키로 업데이트해야 앱이 계속 돈다.

**하지 말 것**: Streamlit Cloud ↔ GitHub 연동 해제, 저장소 삭제·private 전환.
배포 링크가 죽는다. 채점이 끝난 뒤에 정리한다.
