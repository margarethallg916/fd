/**
 * Knowledge base API - 个人知识库
 * Types mirror the backend `/api/kb` contract (snake_case).
 */

import i18n from "i18next";
import { API_BASE } from "./config";
import { ApiRequestError, authFetch } from "./fetch";
import { authenticatedRequest } from "./authenticatedRequest";
import { getRefreshToken } from "./token";
import {
  getValidAccessToken,
  redirectToLogin,
  refreshAccessToken,
} from "./tokenManager";

const BASE = `${API_BASE}/api/kb`;

export type KBChunkPreset = "general" | "qa" | "book" | "laws";

export type KBDocumentStatus =
  | "uploaded"
  | "parsing"
  | "parsed"
  | "indexing"
  | "indexed"
  | "error_parsing"
  | "error_indexing";

export type KBTaskStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled";

export type KBSearchMode = "hybrid" | "vector" | "keyword";

export interface KBPresetConfig {
  key: KBChunkPreset;
  chunk_token_num: number;
  overlap_percent: number;
}

export interface KBConfig {
  enabled: boolean;
  ready: boolean;
  not_ready_reason: "embedding_model_missing" | null;
  presets: KBPresetConfig[];
  allowed_extensions: string[];
  mineru_enabled: boolean;
  max_file_mb: number;
  rerank_available: boolean;
}

export interface KBStats {
  doc_count: number;
  indexed_count: number;
  processing_count: number;
  failed_count: number;
  chunk_count: number;
  size_bytes: number;
}

export interface KBTaskProgress {
  total: number;
  done: number;
  failed: number;
  current_doc_id: string | null;
}

export interface KBTask {
  id: string;
  kb_id: string;
  type: string;
  status: KBTaskStatus;
  progress: KBTaskProgress;
  attempts: number;
  error: string | null;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

export interface KnowledgeBase {
  id: string;
  name: string;
  description: string;
  icon: string;
  owner_id: string;
  owner_username: string;
  my_role: "owner";
  chunk_preset: KBChunkPreset;
  chunk_token_num: number;
  overlap_percent: number;
  visibility: string;
  graph_enabled: boolean;
  embedding: { model_name: string; dim: number } | null;
  index_outdated: boolean;
  stats: KBStats;
  active_task: KBTask | null;
  created_at: string;
  updated_at: string;
}

export interface KBCreate {
  name: string;
  description?: string;
  icon?: string;
  chunk_preset?: KBChunkPreset;
  chunk_token_num?: number;
  overlap_percent?: number;
}

export type KBUpdate = Partial<KBCreate>;

export interface KBListResponse {
  items: KnowledgeBase[];
}

export interface KBDocumentError {
  stage: "parse" | "index";
  message: string;
  at?: string;
}

export interface KBDocument {
  id: string;
  kb_id: string;
  filename: string;
  ext: string;
  mime: string;
  size: number;
  status: KBDocumentStatus;
  error: KBDocumentError | string | null;
  parse_method: "mineru" | "pdf_text" | "local" | null;
  chunk_count: number;
  token_count: number;
  line_count: number;
  uploader_username: string;
  source: string;
  created_at: string;
  updated_at: string;
  indexed_at: string | null;
}

export interface KBDocumentListParams {
  status?: KBDocumentStatus | "";
  q?: string;
  page?: number;
  page_size?: number;
}

export interface KBDocumentListResponse {
  items: KBDocument[];
  total: number;
  page: number;
  page_size: number;
}

export interface KBUploadResult {
  document: KBDocument;
  duplicate: boolean;
}

export interface KBTaskEnqueueResult {
  task_id: string | null;
  count: number;
}

export interface KBContentLine {
  n: number;
  text: string;
}

export interface KBDocumentContent {
  doc_id: string;
  filename: string;
  total_lines: number;
  offset: number;
  lines: KBContentLine[];
  has_more: boolean;
}

export interface KBChunk {
  chunk_id: string;
  index: number;
  text: string;
  start_line: number;
  end_line: number;
  token_count: number;
}

export interface KBChunkListResponse {
  items: KBChunk[];
  total: number;
  page: number;
  page_size: number;
}

export interface KBSearchRequest {
  query: string;
  top_k?: number;
  mode?: KBSearchMode;
  use_rerank?: boolean | null;
  score_threshold?: number;
  vector_weight?: number;
}

export interface KBSearchHit {
  chunk_id: string;
  kb_id: string;
  kb_name: string;
  doc_id: string;
  filename: string;
  index: number;
  text: string;
  start_line: number;
  end_line: number;
  score: number;
  rerank_score?: number | null;
}

export interface KBSearchResponse {
  results: KBSearchHit[];
  warnings: string[];
  took_ms: number;
}

export interface KBUploadOptions {
  onProgress?: (progress: number, loaded: number, total: number) => void;
}

export interface KBUploadHandle {
  promise: Promise<KBUploadResult>;
  abort: () => void;
}

function getErrorEnvelope(value: unknown): {
  message?: string;
  code?: string;
  detail?: unknown;
} {
  if (!value || typeof value !== "object") return {};
  const payload = value as { detail?: unknown; message?: unknown };
  const detail = payload.detail;
  if (detail && typeof detail === "object") {
    const typed = detail as { message?: unknown; code?: unknown };
    return {
      message: typeof typed.message === "string" ? typed.message : undefined,
      code: typeof typed.code === "string" ? typed.code : undefined,
      detail,
    };
  }
  return {
    message:
      typeof detail === "string"
        ? detail
        : typeof payload.message === "string"
          ? payload.message
          : undefined,
    detail,
  };
}

function buildQuery(params: Record<string, string | number | boolean | undefined | null>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

function kbUrl(kbId: string, suffix = ""): string {
  return `${BASE}/${encodeURIComponent(kbId)}${suffix}`;
}

function docUrl(kbId: string, docId: string, suffix = ""): string {
  return kbUrl(kbId, `/documents/${encodeURIComponent(docId)}${suffix}`);
}

/** Single-file multipart upload via XHR so the UI can show progress and abort. */
function uploadDocument(
  kbId: string,
  file: File,
  options: KBUploadOptions = {},
): KBUploadHandle {
  let xhr = new XMLHttpRequest();
  let aborted = false;

  const promise = new Promise<KBUploadResult>((resolve, reject) => {
    const uploadOnce = async (retried: boolean) => {
      const formData = new FormData();
      formData.append("file", file);

      const token = await getValidAccessToken();
      if (aborted) {
        reject(new Error("Upload was aborted"));
        return;
      }

      xhr = new XMLHttpRequest();

      if (options.onProgress) {
        const onProgress = options.onProgress;
        xhr.upload.addEventListener("progress", (event) => {
          if (event.lengthComputable) {
            onProgress(
              Math.round((event.loaded / event.total) * 100),
              event.loaded,
              event.total,
            );
          }
        });
      }

      xhr.addEventListener("load", async () => {
        if (xhr.status >= 200 && xhr.status < 300) {
          try {
            resolve(JSON.parse(xhr.responseText) as KBUploadResult);
          } catch {
            reject(new Error("Failed to parse upload response"));
          }
          return;
        }

        if (xhr.status === 401 && !retried && getRefreshToken()) {
          try {
            await refreshAccessToken();
          } catch {
            redirectToLogin();
            reject(new Error("Unauthorized"));
            return;
          }
          await uploadOnce(true);
          return;
        }

        let errorData: unknown;
        try {
          errorData = JSON.parse(xhr.responseText);
        } catch {
          errorData = undefined;
        }
        const envelope = getErrorEnvelope(errorData);
        reject(
          new ApiRequestError(
            envelope.message ||
              i18n.t("backendErrors.uploadFailed", {
                status: xhr.status,
                defaultValue: "Upload failed (HTTP {{status}})",
              }),
            xhr.status,
            envelope.code,
            envelope.detail,
          ),
        );
      });

      xhr.addEventListener("error", () => {
        reject(new Error("Network error during upload"));
      });

      xhr.addEventListener("abort", () => {
        aborted = true;
        reject(new Error("Upload was aborted"));
      });

      xhr.open("POST", kbUrl(kbId, "/documents/upload"));
      xhr.withCredentials = true;
      if (token) {
        xhr.setRequestHeader("Authorization", `Bearer ${token}`);
      }
      xhr.send(formData);
    };

    void uploadOnce(false).catch(reject);
  });

  return {
    promise,
    abort: () => {
      aborted = true;
      xhr.abort();
    },
  };
}

export const knowledgeBaseApi = {
  getConfig(): Promise<KBConfig> {
    return authFetch<KBConfig>(`${BASE}/config`);
  },

  list(scope: "mine" = "mine"): Promise<KBListResponse> {
    return authFetch<KBListResponse>(`${BASE}${buildQuery({ scope })}`);
  },

  create(data: KBCreate): Promise<KnowledgeBase> {
    return authFetch<KnowledgeBase>(BASE, {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  get(kbId: string): Promise<KnowledgeBase> {
    return authFetch<KnowledgeBase>(kbUrl(kbId));
  },

  update(kbId: string, data: KBUpdate): Promise<KnowledgeBase> {
    return authFetch<KnowledgeBase>(kbUrl(kbId), {
      method: "PATCH",
      body: JSON.stringify(data),
    });
  },

  async remove(kbId: string): Promise<void> {
    await authFetch<void>(kbUrl(kbId), { method: "DELETE" });
  },

  listDocuments(
    kbId: string,
    params: KBDocumentListParams = {},
  ): Promise<KBDocumentListResponse> {
    return authFetch<KBDocumentListResponse>(
      kbUrl(
        kbId,
        `/documents${buildQuery({
          status: params.status,
          q: params.q?.trim(),
          page: params.page,
          page_size: params.page_size,
        })}`,
      ),
    );
  },

  uploadDocument,

  retryDocuments(kbId: string, docIds: string[] = []): Promise<KBTaskEnqueueResult> {
    return authFetch<KBTaskEnqueueResult>(kbUrl(kbId, "/documents/retry"), {
      method: "POST",
      body: JSON.stringify({ doc_ids: docIds }),
    });
  },

  deleteDocuments(kbId: string, docIds: string[]): Promise<{ deleted: number }> {
    return authFetch<{ deleted: number }>(kbUrl(kbId, "/documents/delete"), {
      method: "POST",
      body: JSON.stringify({ doc_ids: docIds }),
    });
  },

  getDocument(kbId: string, docId: string): Promise<KBDocument> {
    return authFetch<KBDocument>(docUrl(kbId, docId));
  },

  getDocumentContent(
    kbId: string,
    docId: string,
    params: { offset?: number; limit?: number } = {},
  ): Promise<KBDocumentContent> {
    return authFetch<KBDocumentContent>(
      docUrl(kbId, docId, `/content${buildQuery(params)}`),
    );
  },

  getDocumentChunks(
    kbId: string,
    docId: string,
    params: { page?: number; page_size?: number } = {},
  ): Promise<KBChunkListResponse> {
    return authFetch<KBChunkListResponse>(
      docUrl(kbId, docId, `/chunks${buildQuery(params)}`),
    );
  },

  /** The download route needs the bearer token, so fetch a blob instead of using an anchor href. */
  async downloadDocument(kbId: string, doc: Pick<KBDocument, "id" | "filename">): Promise<void> {
    const response = await authenticatedRequest(docUrl(kbId, doc.id, "/download"));
    if (!response.ok) {
      const errorData: unknown = await response.json().catch(() => undefined);
      const envelope = getErrorEnvelope(errorData);
      throw new ApiRequestError(
        envelope.message ||
          i18n.t("backendErrors.requestFailed", {
            status: response.status,
            defaultValue: "Request failed (HTTP {{status}})",
          }),
        response.status,
        envelope.code,
        envelope.detail,
      );
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = doc.filename;
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  },

  search(kbId: string, data: KBSearchRequest): Promise<KBSearchResponse> {
    return authFetch<KBSearchResponse>(kbUrl(kbId, "/search"), {
      method: "POST",
      body: JSON.stringify(data),
    });
  },

  listTasks(kbId: string, active = true): Promise<{ items: KBTask[] }> {
    return authFetch<{ items: KBTask[] }>(
      kbUrl(kbId, `/tasks${buildQuery({ active })}`),
    );
  },

  cancelTask(kbId: string, taskId: string): Promise<KBTask> {
    return authFetch<KBTask>(
      kbUrl(kbId, `/tasks/${encodeURIComponent(taskId)}/cancel`),
      { method: "POST" },
    );
  },

  reindex(kbId: string): Promise<KBTaskEnqueueResult> {
    return authFetch<KBTaskEnqueueResult>(kbUrl(kbId, "/reindex"), {
      method: "POST",
    });
  },
};
