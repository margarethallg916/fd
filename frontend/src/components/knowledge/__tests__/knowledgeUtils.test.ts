import assert from "node:assert/strict";
import test from "node:test";

import type {
  KBDocumentStatus,
  KBTask,
  KnowledgeBase,
} from "../../../services/api/knowledgeBase.ts";
import {
  DOCUMENT_STATUSES,
  POLL_BASE_MS,
  POLL_MAX_MS,
  buildPollSignature,
  formatLineRange,
  formatScorePercent,
  getDocErrorMessage,
  getDocStatusMeta,
  hasActiveTask,
  isFailedStatus,
  isKbProcessing,
  isProcessingStatus,
  nextPollDelay,
  parseBoundedInt,
  pickPendingToStart,
  pruneSelection,
  resolveKbDetailTab,
  taskProgressPercent,
  validateUploadFile,
} from "../knowledgeUtils.ts";

const stats = {
  doc_count: 3,
  indexed_count: 1,
  processing_count: 1,
  failed_count: 1,
  chunk_count: 12,
  size_bytes: 2048,
};

function task(partial: Partial<KBTask> = {}): KBTask {
  return {
    id: "t1",
    kb_id: "kb1",
    type: "ingest",
    status: "running",
    progress: { total: 4, done: 1, failed: 1, current_doc_id: "d1" },
    attempts: 1,
    error: null,
    created_at: "2026-01-01T00:00:00Z",
    started_at: null,
    finished_at: null,
    ...partial,
  };
}

function kb(partial: Partial<KnowledgeBase> = {}): KnowledgeBase {
  return {
    id: "kb1",
    name: "n",
    description: "",
    icon: "library-big",
    owner_id: "u",
    owner_username: "u",
    my_role: "owner",
    chunk_preset: "general",
    chunk_token_num: 512,
    overlap_percent: 0,
    visibility: "private",
    graph_enabled: false,
    embedding: null,
    index_outdated: false,
    stats,
    active_task: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...partial,
  };
}

test("every document status has a tone and exactly the expected pipeline flags", () => {
  const processing: KBDocumentStatus[] = ["uploaded", "parsing", "parsed", "indexing"];
  const failed: KBDocumentStatus[] = ["error_parsing", "error_indexing"];
  for (const status of DOCUMENT_STATUSES) {
    const meta = getDocStatusMeta(status);
    assert.ok(meta.tone, status);
    assert.equal(isProcessingStatus(status), processing.includes(status), status);
    assert.equal(isFailedStatus(status), failed.includes(status), status);
  }
  assert.equal(getDocStatusMeta("indexed").tone, "success");
  assert.equal(getDocStatusMeta("error_parsing").tone, "danger");
});

test("poll delay resets on change and backs off x1.5 up to the cap", () => {
  assert.equal(nextPollDelay(30000, true), POLL_BASE_MS);
  assert.equal(nextPollDelay(5000, false), 7500);
  assert.equal(nextPollDelay(7500, false), 11250);
  let delay = POLL_BASE_MS;
  for (let i = 0; i < 20; i++) delay = nextPollDelay(delay, false);
  assert.equal(delay, POLL_MAX_MS);
  assert.equal(nextPollDelay(POLL_MAX_MS, false), POLL_MAX_MS);
});

test("poll signature changes when a document status, task progress or stats change", () => {
  const docs = [{ id: "a", status: "parsing" as const, chunk_count: 0 }];
  const base = buildPollSignature(kb(), docs);
  assert.equal(base, buildPollSignature(kb(), docs));
  assert.notEqual(
    base,
    buildPollSignature(kb(), [{ id: "a", status: "indexing" as const, chunk_count: 0 }]),
  );
  assert.notEqual(
    buildPollSignature(kb({ active_task: task() }), docs),
    buildPollSignature(
      kb({ active_task: task({ progress: { total: 4, done: 2, failed: 1, current_doc_id: "d2" } }) }),
      docs,
    ),
  );
  assert.notEqual(
    base,
    buildPollSignature(kb({ stats: { ...stats, chunk_count: 13 } }), docs),
  );
});

test("kb counts as processing for in-flight docs, counters or an active task", () => {
  assert.equal(isKbProcessing(null), false);
  assert.equal(
    isKbProcessing(kb({ stats: { ...stats, processing_count: 0 } })),
    false,
  );
  assert.equal(isKbProcessing(kb()), true);
  assert.equal(
    isKbProcessing(kb({ stats: { ...stats, processing_count: 0 } }), [{ status: "parsing" }]),
    true,
  );
  assert.equal(
    isKbProcessing(kb({ stats: { ...stats, processing_count: 0 }, active_task: task({ status: "queued" }) })),
    true,
  );
  assert.equal(hasActiveTask(task({ status: "succeeded" })), false);
  assert.equal(hasActiveTask(null), false);
});

test("task progress counts failed files as finished and stays within 0-100", () => {
  assert.equal(taskProgressPercent(null), 0);
  assert.equal(taskProgressPercent(task()), 50);
  assert.equal(
    taskProgressPercent(task({ progress: { total: 0, done: 0, failed: 0, current_doc_id: null } })),
    0,
  );
  assert.equal(
    taskProgressPercent(task({ progress: { total: 2, done: 5, failed: 0, current_doc_id: null } })),
    100,
  );
});

test("upload validation checks extension, empty files and size limit", () => {
  const config = { allowed_extensions: [".pdf", ".md"], max_file_mb: 1 };
  assert.equal(validateUploadFile({ name: "a.PDF", size: 10 }, config), null);
  assert.deepEqual(validateUploadFile({ name: "a.exe", size: 10 }, config), {
    code: "extension",
    ext: ".exe",
  });
  assert.deepEqual(validateUploadFile({ name: "noext", size: 10 }, config), {
    code: "extension",
    ext: "noext",
  });
  assert.deepEqual(validateUploadFile({ name: "a.md", size: 0 }, config), { code: "empty" });
  assert.deepEqual(validateUploadFile({ name: "a.md", size: 1024 * 1024 + 1 }, config), {
    code: "size",
    maxMb: 1,
  });
  assert.equal(validateUploadFile({ name: "a.md", size: 1024 * 1024 }, config), null);
  // Without a loaded config the backend stays the authority.
  assert.equal(validateUploadFile({ name: "a.exe", size: 10 }, null), null);
});

test("upload concurrency never exceeds the limit and keeps queue order", () => {
  const items = [
    { id: "1", status: "uploading" },
    { id: "2", status: "done" },
    { id: "3", status: "pending" },
    { id: "4", status: "pending" },
    { id: "5", status: "pending" },
  ];
  assert.deepEqual(pickPendingToStart(items).map((i) => i.id), ["3", "4"]);
  assert.deepEqual(pickPendingToStart(items, 1), []);
  assert.deepEqual(
    pickPendingToStart(items.map((i) => ({ ...i, status: "pending" }))).map((i) => i.id),
    ["1", "2", "3"],
  );
});

test("bounded integer parsing rejects out-of-range and non-numeric input", () => {
  assert.equal(parseBoundedInt("512", 100, 2048), 512);
  assert.equal(parseBoundedInt(" 100 ", 100, 2048), 100);
  assert.equal(parseBoundedInt("99", 100, 2048), null);
  assert.equal(parseBoundedInt("2049", 100, 2048), null);
  assert.equal(parseBoundedInt("1.5", 0, 50), null);
  assert.equal(parseBoundedInt("", 0, 50), null);
  assert.equal(parseBoundedInt("0", 0, 50), 0);
});

test("detail tab resolver falls back to documents", () => {
  assert.equal(resolveKbDetailTab("search"), "search");
  assert.equal(resolveKbDetailTab("settings"), "settings");
  assert.equal(resolveKbDetailTab("documents"), "documents");
  assert.equal(resolveKbDetailTab("bogus"), "documents");
  assert.equal(resolveKbDetailTab(null), "documents");
});

test("display helpers format scores, line ranges and document errors", () => {
  assert.equal(formatScorePercent(0.876), "88%");
  assert.equal(formatScorePercent(1.4), "100%");
  assert.equal(formatScorePercent(null), "–");
  assert.equal(formatLineRange(3, 3), "3");
  assert.equal(formatLineRange(3, 9), "3-9");
  assert.equal(getDocErrorMessage({ error: null }), "");
  assert.equal(getDocErrorMessage({ error: "boom" }), "boom");
  assert.equal(getDocErrorMessage({ error: { stage: "parse", message: "bad pdf" } }), "bad pdf");
});

test("selection pruning keeps only ids still on the page", () => {
  const next = pruneSelection(new Set(["a", "b", "z"]), [{ id: "a" }, { id: "b" }, { id: "c" }]);
  assert.deepEqual([...next].sort(), ["a", "b"]);
});
