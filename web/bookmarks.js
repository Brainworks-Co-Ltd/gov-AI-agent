/* 공고 북마크 저장소.
 *
 * 서버나 회사 프로필에는 손대지 않고 공고 ID만 현재 브라우저에 저장한다. Node에서
 * 같은 로직을 직접 테스트할 수 있도록 브라우저 전역과 CommonJS 양쪽에 공개한다.
 */
(function exposeBookmarks(root, factory) {
  const bookmarks = factory();
  if (typeof module === "object" && module.exports) module.exports = bookmarks;
  if (root) root.NoticeBookmarks = bookmarks;
})(typeof globalThis === "undefined" ? this : globalThis, function buildBookmarks() {
  "use strict";

  const DEFAULT_KEY = "gov-ai-agent:bookmarks:v1";

  function parseIds(raw) {
    try {
      const parsed = JSON.parse(raw || "[]");
      if (!Array.isArray(parsed)) return [];
      return [...new Set(parsed.filter(
        (id) => typeof id === "string" && id.length > 0,
      ))];
    } catch (_) {
      return [];
    }
  }

  function createStore(storage, key = DEFAULT_KEY) {
    let ids;
    try {
      ids = new Set(parseIds(storage && storage.getItem(key)));
    } catch (_) {
      ids = new Set();
    }

    function persist() {
      try {
        if (storage) storage.setItem(key, JSON.stringify([...ids]));
      } catch (_) {
        // 저장소가 차단되어도 현재 탭에서 고른 상태는 유지한다.
      }
    }

    return {
      has(id) {
        return ids.has(id);
      },
      count() {
        return ids.size;
      },
      toggle(id) {
        if (ids.has(id)) ids.delete(id);
        else ids.add(id);
        persist();
        return ids.has(id);
      },
      filter(items) {
        return items.filter((item) => ids.has(item.id));
      },
    };
  }

  return { DEFAULT_KEY, parseIds, createStore };
});
