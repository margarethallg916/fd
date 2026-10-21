import test from "node:test";
import assert from "node:assert/strict";
import {
  KNOWLEDGE_BASE_REFRESH_EVENT,
  dispatchKnowledgeBaseRefresh,
  subscribeKnowledgeBaseRefresh,
} from "../../../utils/knowledgeBaseEvents.ts";

test("uses a stable event name", () => {
  assert.equal(KNOWLEDGE_BASE_REFRESH_EVENT, "knowledge-base:refresh");
});

test("notifies subscribers and stops after unsubscribe", () => {
  const target = new EventTarget();
  let calls = 0;
  const unsubscribe = subscribeKnowledgeBaseRefresh(() => {
    calls += 1;
  }, { target });

  dispatchKnowledgeBaseRefresh({}, target);
  dispatchKnowledgeBaseRefresh({ kbId: "a" }, target);
  assert.equal(calls, 2);

  unsubscribe();
  dispatchKnowledgeBaseRefresh({}, target);
  assert.equal(calls, 2);
});

test("filters by knowledge base id but always accepts untargeted events", () => {
  const target = new EventTarget();
  const seen: Array<string | undefined> = [];
  subscribeKnowledgeBaseRefresh((detail) => seen.push(detail.kbId), {
    kbId: "kb-1",
    target,
  });

  dispatchKnowledgeBaseRefresh({ kbId: "kb-2" }, target);
  dispatchKnowledgeBaseRefresh({ kbId: "kb-1" }, target);
  dispatchKnowledgeBaseRefresh({}, target);
  assert.deepEqual(seen, ["kb-1", undefined]);
});
