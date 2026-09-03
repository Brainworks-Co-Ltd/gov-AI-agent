# Background Verdict Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 데모 웹은 저장된 판정 결과만 즉시 읽고, 수집·판정 갱신은 버튼 또는 예약 CLI가 백그라운드에서 수행하도록 만든다.

**Architecture:** 기존 SQLite 스키마에 판정 입력 해시를 추가하고, `agent.orchestrator`가 프로필·공고 묶음·판정 버전으로 캐시 유효성을 판단한다. `tools.refresh`는 신규·변경 공고만 판정하는 공통 실행기와 단일 백그라운드 작업 조정기를 제공하며, `serve.py`와 CLI가 이를 함께 사용한다.

**Tech Stack:** Python 표준 라이브러리 `sqlite3`, `threading`, `hashlib`, 바닐라 JavaScript

---

### Task 1: SQLite와 판정 캐시 유효성

**Files:**
- Modify: `tools/store.py`
- Modify: `agent/orchestrator.py`
- Create: `tests_cache_refresh.py`

- [x] 변경 전 테스트로 공고 본문·프로필·판정 버전 변화가 캐시를 무효화하는 요구사항을 작성한다.
- [x] `python -X utf8 tests_cache_refresh.py`를 실행해 `input_hash` 기능 부재로 실패하는지 확인한다.
- [x] `verdicts.input_hash` 마이그레이션과 WAL·busy timeout 연결 설정을 구현한다.
- [x] 대표 공고와 중복 공고 본문을 포함한 SHA-256 입력 해시를 구현한다.
- [x] 캐시된 D-day를 현재 날짜 기준으로 다시 계산한다.
- [x] 신규 테스트가 통과하는지 확인한다.

### Task 2: 공통 갱신 실행기와 백그라운드 조정기

**Files:**
- Create: `tools/refresh.py`
- Modify: `tests_cache_refresh.py`

- [x] 유효한 캐시는 건너뛰고 구버전 캐시만 평가하는 실패 테스트를 작성한다.
- [x] 작업 중 두 번째 시작 요청을 거부하고 상태를 복사해 반환하는 실패 테스트를 작성한다.
- [x] 저장 공고 판정과 선택적 공식 API 수집을 조합하는 `run()`을 구현한다.
- [x] `RefreshCoordinator`가 데몬 스레드에서 작업하고 진행률·오류를 보관하도록 구현한다.
- [x] `--collect`, `--offline`, `--no-llm` CLI 인자를 구현한다.
- [x] 신규 테스트가 통과하는지 확인한다.

### Task 3: 빠른 웹 조회와 백그라운드 갱신 UI

**Files:**
- Modify: `serve.py`
- Modify: `web/app.js`
- Modify: `web/index.html`
- Modify: `tests_cache_refresh.py`

- [x] 정적 코드 검사로 자동 판정 상수와 브라우저 반복 판정이 남아 있으면 실패하는 테스트를 작성한다.
- [x] `GET /api/refresh` 상태 조회와 기존 수집·전체 판정 POST의 즉시 시작 응답을 구현한다.
- [x] 목록 조회가 입력 해시까지 일치하는 판정만 배지로 표시하도록 변경한다.
- [x] 최초 목록 자동 판정과 중지 버튼을 제거한다.
- [x] 프론트엔드가 상태 API를 조회해 진행률을 표시하고 완료 후 목록만 다시 읽게 한다.
- [x] `node --check web/app.js`와 신규 테스트를 실행한다.

### Task 4: 문서화와 전체 검증

**Files:**
- Modify: `README.md`

- [x] 수동 사전 판정과 예약 실행용 명령을 README 빠른 실행 절에 추가한다.
- [x] `python -X utf8 tests_cache_refresh.py`를 실행한다.
- [x] `python -X utf8 tests_eligibility_region.py`를 실행한다.
- [x] `node --check web/app.js`를 실행한다.
- [x] 임시 DB로 `python -m tools.refresh` 실행 경로를 확인한다.
- [x] diff를 검토해 요청 범위 밖 변경과 디버그 로그가 없는지 확인한다.
