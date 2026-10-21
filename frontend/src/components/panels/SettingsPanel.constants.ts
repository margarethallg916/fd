import type { SettingCategory, SettingType } from "../../types";

export const CATEGORY_ORDER: SettingCategory[] = [
  "frontend",
  "wecom",
  "agent",
  "llm",
  "session",
  "mongodb",
  "redis",
  "checkpoint",
  "long_term_storage",
  "memory",
  "memory_embedding",
  "memory_search",
  "memory_storage",
  "security",
  "email",
  "captcha",
  "s3",
  "file_upload",
  "sandbox",
  "skills",
  "tools",
  "audio_transcription",
  "dify",
  "document_parse",
  "knowledge_base",
  "tracing",
  "user",
  "oauth",
];

export const TYPE_COLORS: Record<SettingType, string> = {
  string: "bg-stone-100 text-stone-700 dark:bg-stone-800 dark:text-stone-300",
  text: "bg-cyan-100 text-cyan-700 dark:bg-cyan-900/50 dark:text-cyan-300",
  number:
    "bg-green-100 text-green-700 dark:bg-green-900/50 dark:text-green-300",
  boolean:
    "bg-purple-100 text-purple-700 dark:bg-purple-900/50 dark:text-purple-300",
  json: "bg-orange-100 text-orange-700 dark:bg-orange-900/50 dark:text-orange-300",
  select: "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300",
};

export const MODEL_CONFIG_SETTING_KEYS = new Set([
  "DEFAULT_MODEL_ID",
  "NATIVE_MEMORY_COMPACTION_MODEL_ID",
  "SESSION_TITLE_MODEL_ID",
  "NATIVE_MEMORY_MODEL_ID",
  "AUDIO_TRANSCRIPTION_MODEL_ID",
  "NATIVE_MEMORY_EMBEDDING_MODEL_ID",
  "NATIVE_MEMORY_RERANK_MODEL_ID",
  "DIFY_KB_RERANK_MODEL_ID",
  "KB_EMBEDDING_MODEL_ID",
  "KB_RERANK_MODEL_ID",
]);

// Map of model-card ID setting keys to the card `kind` they reference.
// Settings not in this map reference chat-kind cards (default behaviour).
export const MODEL_CARD_KIND_FILTER: Record<string, string> = {
  NATIVE_MEMORY_MODEL_ID: "chat",
  AUDIO_TRANSCRIPTION_MODEL_ID: "transcribe",
  NATIVE_MEMORY_EMBEDDING_MODEL_ID: "embedding",
  NATIVE_MEMORY_RERANK_MODEL_ID: "rerank",
  DIFY_KB_RERANK_MODEL_ID: "rerank",
  KB_EMBEDDING_MODEL_ID: "embedding",
  KB_RERANK_MODEL_ID: "rerank",
};
