import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const currentDir = dirname(fileURLToPath(import.meta.url));
const localesDir = resolve(currentDir, "../locales");
const knowledgeDir = resolve(currentDir, "../../components/knowledge");
const maintainedLocales = ["en", "zh"] as const;
const generatedPlaceholder = /^(?:\[TODO\]|【待翻译】|knowledge\.)/;
const cjk = /[\u3400-\u9fff]/;

type Json = { [key: string]: Json | string };

function readLocale(locale: string): Json {
  return JSON.parse(readFileSync(resolve(localesDir, `${locale}.json`), "utf8"));
}

function flatten(node: Json | string, prefix = ""): Record<string, string> {
  if (typeof node === "string") return { [prefix]: node };
  return Object.entries(node).reduce<Record<string, string>>(
    (acc, [key, value]) =>
      Object.assign(acc, flatten(value, prefix ? `${prefix}.${key}` : key)),
    {},
  );
}

function get(locale: Json, path: string): unknown {
  return path
    .split(".")
    .reduce<unknown>(
      (node, part) =>
        node && typeof node === "object" ? (node as Json)[part] : undefined,
      locale,
    );
}

function sourceFiles(dir: string): string[] {
  return readdirSync(dir).flatMap((name) => {
    const full = join(dir, name);
    if (statSync(full).isDirectory()) {
      return name === "__tests__" ? [] : sourceFiles(full);
    }
    return /\.tsx?$/.test(name) ? [full] : [];
  });
}

function placeholders(value: string): string[] {
  return [...value.matchAll(/\{\{\s*(\w+)\s*\}\}/g)].map((m) => m[1]).sort();
}

const locales = Object.fromEntries(
  maintainedLocales.map((name) => [name, readLocale(name)]),
) as Record<(typeof maintainedLocales)[number], Json>;

test("knowledge namespace has identical keys in en and zh", () => {
  const en = Object.keys(flatten(locales.en.knowledge, "knowledge")).sort();
  const zh = Object.keys(flatten(locales.zh.knowledge, "knowledge")).sort();
  assert.ok(en.length > 100, "knowledge namespace should be populated");
  assert.deepEqual(en, zh);
});

test("knowledge strings are translated, not generated placeholders", () => {
  for (const name of maintainedLocales) {
    for (const [key, value] of Object.entries(
      flatten(locales[name].knowledge, "knowledge"),
    )) {
      assert.ok(value.trim().length > 0, `${name}:${key} must not be empty`);
      assert.doesNotMatch(value, generatedPlaceholder, `${name}:${key}`);
    }
  }
  for (const [key, value] of Object.entries(
    flatten(locales.en.knowledge, "knowledge"),
  )) {
    assert.doesNotMatch(value, cjk, `en:${key} must not contain Chinese text`);
  }
});

test("interpolation variables match between en and zh", () => {
  const en = flatten(locales.en.knowledge, "knowledge");
  const zh = flatten(locales.zh.knowledge, "knowledge");
  for (const key of Object.keys(en)) {
    assert.deepEqual(placeholders(en[key]), placeholders(zh[key]), key);
  }
});

test("navigation, SEO, admin category and setting descriptions exist", () => {
  const required = [
    "nav.knowledge",
    "seo.knowledge.title",
    "seo.knowledge.description",
    "categories.knowledge_base",
    "settingDesc.KB_ENABLED",
    "settingDesc.KB_EMBEDDING_MODEL_ID",
    "settingDesc.KB_RERANK_MODEL_ID",
    "settingDesc.KB_WORKER_CONCURRENCY",
    "settingDesc.KB_MAX_FILE_MB",
    "settingDesc.KB_EMBEDDING_BATCH_SIZE",
    "knowledge.errors.disabled",
  ];
  for (const name of maintainedLocales) {
    for (const path of required) {
      const value = get(locales[name], path);
      assert.equal(typeof value, "string", `${name}:${path}`);
      assert.doesNotMatch(value as string, generatedPlaceholder, `${name}:${path}`);
    }
  }
});

test("document status, preset and search mode labels cover every enum value", () => {
  const groups: Record<string, string[]> = {
    "knowledge.status": [
      "uploaded",
      "parsing",
      "parsed",
      "indexing",
      "indexed",
      "error_parsing",
      "error_indexing",
    ],
    "knowledge.search.mode": ["hybrid", "vector", "keyword"],
  };
  for (const name of maintainedLocales) {
    for (const [prefix, values] of Object.entries(groups)) {
      for (const value of values) {
        assert.equal(typeof get(locales[name], `${prefix}.${value}`), "string", `${name}:${prefix}.${value}`);
      }
    }
    for (const preset of ["general", "qa", "book", "laws"]) {
      assert.equal(typeof get(locales[name], `knowledge.preset.${preset}.name`), "string");
      assert.equal(typeof get(locales[name], `knowledge.preset.${preset}.desc`), "string");
    }
  }
  assert.equal(get(locales.zh, "knowledge.status.uploaded"), "排队中");
  assert.equal(get(locales.zh, "knowledge.status.error_indexing"), "入库失败");
});

test("every static knowledge.* key used by the components exists in both locales", () => {
  const used = new Set<string>();
  for (const file of sourceFiles(knowledgeDir)) {
    const source = readFileSync(file, "utf8");
    for (const match of source.matchAll(/["'](knowledge\.[A-Za-z0-9_.]+)["']/g)) {
      used.add(match[1]);
    }
  }
  assert.ok(used.size > 50, "should discover the component keys");
  for (const name of maintainedLocales) {
    const keys = flatten(locales[name].knowledge, "knowledge");
    for (const key of used) {
      assert.ok(key in keys, `${name}.json is missing ${key}`);
    }
  }
});
