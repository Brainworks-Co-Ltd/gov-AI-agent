"use strict";

const assert = require("node:assert/strict");
const { DEFAULT_KEY, parseIds, createStore } = require("./web/bookmarks.js");

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem(key) {
      return values.has(key) ? values.get(key) : null;
    },
    setItem(key, value) {
      values.set(key, value);
    },
  };
}

function testParseIdsKeepsUniqueStringsOnly() {
  assert.deepEqual(
    parseIds('["기업마당:1", "기업마당:1", 3, "", null, "K-Startup:2"]'),
    ["기업마당:1", "K-Startup:2"],
  );
  assert.deepEqual(parseIds("깨진 JSON"), []);
  assert.deepEqual(parseIds('{"id":"기업마당:1"}'), []);
}

function testTogglePersistsAcrossStoreInstances() {
  const storage = memoryStorage();
  const store = createStore(storage);

  assert.equal(store.toggle("기업마당:1"), true);
  assert.equal(store.count(), 1);
  assert.equal(createStore(storage).has("기업마당:1"), true);

  assert.equal(store.toggle("기업마당:1"), false);
  assert.equal(createStore(storage).has("기업마당:1"), false);
}

function testFilterKeepsServerOrder() {
  const storage = memoryStorage({
    [DEFAULT_KEY]: JSON.stringify(["기업마당:1", "K-Startup:2"]),
  });
  const store = createStore(storage);
  const items = [
    { id: "K-Startup:3" },
    { id: "K-Startup:2" },
    { id: "기업마당:1" },
  ];

  assert.deepEqual(store.filter(items), [items[1], items[2]]);
}

function testStorageFailureKeepsCurrentTabState() {
  const storage = {
    getItem() {
      throw new Error("저장소 읽기 차단");
    },
    setItem() {
      throw new Error("저장소 쓰기 차단");
    },
  };
  const store = createStore(storage);

  assert.equal(store.toggle("기업마당:1"), true);
  assert.equal(store.has("기업마당:1"), true);
  assert.equal(store.count(), 1);
}

testParseIdsKeepsUniqueStringsOnly();
testTogglePersistsAcrossStoreInstances();
testFilterKeepsServerOrder();
testStorageFailureKeepsCurrentTabState();

console.log("북마크 저장 테스트 모두 통과 (4개)");
