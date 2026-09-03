# Local Notice Bookmarks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 브라우저에 관심 공고 ID를 저장하고 카드에서 토글하며 북마크한 공고만 빠르게 볼 수 있게 함.

**Architecture:** 저장·필터의 순수 로직은 `web/bookmarks.js`에 분리하고 `web/app.js`는 카드 버튼과 목록 렌더링을 연결함. 서버 응답은 그대로 두고 북마크만 보기일 때 클라이언트에서 공고와 추천 목록을 거른 뒤 화면 집계를 다시 계산함.

**Tech Stack:** 바닐라 JavaScript, Web Storage API, HTML, CSS, Node 내장 `assert`

---

### Task 1: 북마크 저장 로직

**Files:**
- Create: `tests_bookmarks.js`
- Create: `web/bookmarks.js`

- [x] **Step 1: 실패하는 저장 로직 테스트 작성**

```javascript
const assert = require("node:assert/strict");
const { parseIds, createStore } = require("./web/bookmarks.js");

assert.deepEqual(parseIds('["공고:1", "공고:1", 3]'), ["공고:1"]);
assert.deepEqual(parseIds("깨진 JSON"), []);

const values = new Map();
const storage = {
  getItem: (key) => values.get(key) || null,
  setItem: (key, value) => values.set(key, value),
};
const store = createStore(storage);
assert.equal(store.toggle("공고:1"), true);
assert.equal(createStore(storage).has("공고:1"), true);
assert.deepEqual(store.filter([{ id: "공고:2" }, { id: "공고:1" }]), [{ id: "공고:1" }]);
```

- [x] **Step 2: 테스트 실패 확인**

Run: `node tests_bookmarks.js`

Expected: `web/bookmarks.js` 모듈을 찾지 못해 실패함.

- [x] **Step 3: 최소 저장 모듈 구현**

```javascript
const DEFAULT_KEY = "gov-ai-agent:bookmarks:v1";

function parseIds(raw) {
  try {
    const parsed = JSON.parse(raw || "[]");
    return Array.isArray(parsed)
      ? [...new Set(parsed.filter((id) => typeof id === "string" && id))]
      : [];
  } catch (_) {
    return [];
  }
}

function createStore(storage, key = DEFAULT_KEY) {
  let ids;
  try { ids = new Set(parseIds(storage && storage.getItem(key))); }
  catch (_) { ids = new Set(); }
  const persist = () => {
    try { if (storage) storage.setItem(key, JSON.stringify([...ids])); }
    catch (_) { /* 현재 탭의 메모리 상태는 유지함. */ }
  };
  return {
    has: (id) => ids.has(id),
    count: () => ids.size,
    toggle(id) {
      if (ids.has(id)) ids.delete(id); else ids.add(id);
      persist();
      return ids.has(id);
    },
    filter: (items) => items.filter((item) => ids.has(item.id)),
  };
}
```

- [x] **Step 4: 저장 로직 테스트 통과 확인**

Run: `node tests_bookmarks.js`

Expected: `북마크 테스트 모두 통과` 출력 후 종료 코드 0.

### Task 2: 카드와 북마크만 보기 연결

**Files:**
- Modify: `tests_bookmarks.js`
- Modify: `web/index.html`
- Modify: `web/app.js`
- Modify: `web/style.css`

- [x] **Step 1: 실패하는 화면 연결 테스트 추가**

```javascript
const fs = require("node:fs");
const html = fs.readFileSync("web/index.html", "utf8");
const app = fs.readFileSync("web/app.js", "utf8");

assert.match(html, /id="btn-bookmarks"/);
assert.ok(html.indexOf("bookmarks.js") < html.indexOf("app.js"));
assert.match(app, /aria-pressed/);
assert.match(app, /stopPropagation/);
```

- [x] **Step 2: 화면 연결 테스트 실패 확인**

Run: `node tests_bookmarks.js`

Expected: `btn-bookmarks`가 없어 assertion 실패함.

- [x] **Step 3: HTML과 카드 토글 구현**

`web/index.html`에서 검색 입력 앞에 다음 버튼을 추가하고 `bookmarks.js`를 `app.js`보다
먼저 불러옴.

```html
<button id="btn-bookmarks" class="bookmark-filter" type="button"
        aria-pressed="false">북마크만 0</button>
<script src="bookmarks.js"></script>
<script src="app.js"></script>
```

`web/app.js`에서 `NoticeBookmarks.createStore(window.localStorage)`로 저장소를 만들고,
각 카드에 실제 버튼을 생성함. 버튼의 `click`과 `keydown`에서 `stopPropagation()`을
호출하고 토글 뒤 같은 ID를 가진 모든 버튼의 `aria-pressed`와 별 모양을 갱신함.

- [x] **Step 4: 로컬 필터와 집계 구현**

마지막 서버 응답을 보관하고 `북마크만`이 켜져 있으면 다음과 같이 목록을 고름.

```javascript
const items = bookmarksOnly ? bookmarkStore.filter(data.items) : data.items;
const picks = bookmarksOnly
  ? (data.recommended || []).filter((pick) => bookmarkStore.has(pick.item.id))
  : data.recommended;
```

필터된 `items`로 전체 건수, 판정 완료 건수, 판정별 집계를 계산하여 `renderTally`에
넘김. 북마크만 보기에서 해제하면 마지막 서버 응답을 즉시 다시 그림.

- [x] **Step 5: 상태 스타일 구현**

기존 색 토큰만 사용해 별 버튼은 작고 평평한 보조 동작으로, 북마크만 버튼은 선택 시
`--primary-soft` 배경과 `--accent-foreground` 글자로 표시함. 카드 hover 이동이 버튼
hover와 충돌하지 않도록 북마크 버튼의 그림자와 이동 효과를 제거함.

- [x] **Step 6: 화면 연결 테스트 통과 확인**

Run: `node tests_bookmarks.js`

Expected: 저장 로직과 화면 연결 assertion이 모두 통과함.

### Task 3: 전체 회귀 검증

**Files:**
- Modify: `docs/superpowers/plans/2026-09-03-local-notice-bookmarks.md`

- [x] **Step 1: 정적·회귀 테스트 실행**

Run: `node --check web/bookmarks.js`

Expected: 출력 없이 종료 코드 0.

Run: `node --check web/app.js`

Expected: 출력 없이 종료 코드 0.

Run: `node tests_bookmarks.js`

Expected: `북마크 테스트 모두 통과`.

Run: `python -X utf8 tests_cache_refresh.py`

Expected: `모두 통과 (10개)`.

Run: `python -X utf8 tests_eligibility_region.py`

Expected: 모든 지역 판정 회귀 테스트가 통과함.

- [x] **Step 2: 브라우저 동작 확인**

로컬 서버에서 공고 카드 하나를 북마크하고 새로고침한 뒤 별 상태가 남는지 확인함.
`북마크만`을 켜서 해당 공고만 남는지, 해제하면 빈 상태가 되는지, 별 버튼을 눌렀을
때 자격 판정 화면으로 이동하지 않는지 확인함.

- [x] **Step 3: 계획 체크와 변경 범위 확인**

Run: `git diff --check`

Expected: 출력 없이 종료 코드 0.

Run: `git status --short`

Expected: 설계·계획·북마크 테스트와 `web/`의 북마크 관련 파일만 표시됨.
