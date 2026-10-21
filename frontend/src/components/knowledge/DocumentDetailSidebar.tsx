import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import { FileText, Loader2, LocateFixed } from "lucide-react";
import { EditorSidebar } from "../common/EditorSidebar";
import { Pagination } from "../common/Pagination";
import { formatFileSize } from "../documents/utils/fileSize";
import {
  knowledgeBaseApi,
  type KBChunk,
  type KBContentLine,
  type KBDocument,
} from "../../services/api/knowledgeBase";
import { formatLineRange } from "./knowledgeUtils";
import { OUTLINE_BTN } from "./knowledgeStyles";

const CONTENT_PAGE_LINES = 400;
const CHUNK_PAGE_SIZE = 20;

export type DocumentSidebarTab = "content" | "chunks";

export interface DocumentFocus {
  startLine: number;
  endLine: number;
}

interface DocumentDetailSidebarProps {
  kbId: string;
  doc: KBDocument;
  /** Callers remount via `key` when the document or target line changes. */
  focus?: DocumentFocus | null;
  initialTab?: DocumentSidebarTab;
  onClose: () => void;
}

function errMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

function ContentView({
  kbId,
  doc,
  focus,
}: {
  kbId: string;
  doc: KBDocument;
  focus: DocumentFocus | null;
}) {
  const { t } = useTranslation();
  const [lines, setLines] = useState<KBContentLine[]>([]);
  const [totalLines, setTotalLines] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [loading, setLoading] = useState(true);
  const [loadingMore, setLoadingMore] = useState<"up" | "down" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const focusRef = useRef<HTMLDivElement>(null);
  const scrolledFor = useRef<string | null>(null);

  const firstLine = lines[0]?.n ?? 1;
  const focusStart = focus?.startLine ?? null;
  const focusKey = focusStart !== null ? `${doc.id}:${focusStart}` : null;

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    setLines([]);
    scrolledFor.current = null;
    const offset = focusStart !== null ? Math.max(1, focusStart - 10) : 1;
    knowledgeBaseApi
      .getDocumentContent(kbId, doc.id, { offset, limit: CONTENT_PAGE_LINES })
      .then((res) => {
        if (cancelled) return;
        setLines(res.lines);
        setTotalLines(res.total_lines);
        setHasMore(res.has_more);
      })
      .catch((e) => {
        if (!cancelled) setError(errMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kbId, doc.id, focusStart]);

  useLayoutEffect(() => {
    if (loading || !focusKey || scrolledFor.current === focusKey) return;
    scrolledFor.current = focusKey;
    focusRef.current?.scrollIntoView({ block: "center" });
  }, [loading, focusKey, lines]);

  const loadMore = async (direction: "up" | "down") => {
    if (loadingMore || lines.length === 0) return;
    setLoadingMore(direction);
    try {
      if (direction === "down") {
        const last = lines[lines.length - 1].n;
        const res = await knowledgeBaseApi.getDocumentContent(kbId, doc.id, {
          offset: last + 1,
          limit: CONTENT_PAGE_LINES,
        });
        setLines((prev) => [...prev, ...res.lines]);
        setHasMore(res.has_more);
        setTotalLines(res.total_lines);
      } else {
        const offset = Math.max(1, firstLine - CONTENT_PAGE_LINES);
        const res = await knowledgeBaseApi.getDocumentContent(kbId, doc.id, {
          offset,
          limit: firstLine - offset,
        });
        setLines((prev) => [...res.lines, ...prev]);
      }
    } catch (e) {
      toast.error(errMessage(e));
    } finally {
      setLoadingMore(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center py-16 text-stone-400">
        <Loader2 className="animate-spin" size={20} />
      </div>
    );
  }
  if (error) {
    return <div className="p-5 text-sm text-red-600 dark:text-red-400">{error}</div>;
  }
  if (lines.length === 0) {
    return (
      <div className="p-5 text-sm text-stone-500 dark:text-stone-400">
        {t("knowledge.doc.noContent", "暂无解析内容")}
      </div>
    );
  }

  return (
    <div className="pb-6">
      <div className="px-4 pt-3 text-xs text-stone-500 dark:text-stone-400">
        {t("knowledge.doc.totalLines", {
          count: totalLines,
          defaultValue: "共 {{count}} 行",
        })}
      </div>
      {firstLine > 1 && (
        <div className="px-4 pt-2">
          <button
            type="button"
            className={OUTLINE_BTN}
            disabled={loadingMore !== null}
            onClick={() => void loadMore("up")}
          >
            {loadingMore === "up" && <Loader2 size={14} className="animate-spin" />}
            {t("knowledge.doc.loadPrevious", "加载上文")}
          </button>
        </div>
      )}
      <div className="mt-2 font-mono text-xs leading-5 text-stone-800 dark:text-stone-200">
        {lines.map((line) => {
          const highlighted =
            !!focus && line.n >= focus.startLine && line.n <= focus.endLine;
          return (
            <div
              key={line.n}
              ref={focus && line.n === focus.startLine ? focusRef : undefined}
              className={`flex ${highlighted ? "bg-amber-50 dark:bg-amber-900/25" : ""}`}
            >
              <span className="w-12 shrink-0 select-none pr-3 text-right text-stone-400 dark:text-stone-500">
                {line.n}
              </span>
              <span className="min-w-0 flex-1 whitespace-pre-wrap break-words pr-4">
                {line.text || " "}
              </span>
            </div>
          );
        })}
      </div>
      {hasMore && (
        <div className="px-4 pt-3">
          <button
            type="button"
            className={OUTLINE_BTN}
            disabled={loadingMore !== null}
            onClick={() => void loadMore("down")}
          >
            {loadingMore === "down" && <Loader2 size={14} className="animate-spin" />}
            {t("knowledge.doc.loadMore", "加载更多")}
          </button>
        </div>
      )}
    </div>
  );
}

function ChunksView({
  kbId,
  doc,
  onLocate,
}: {
  kbId: string;
  doc: KBDocument;
  onLocate: (focus: DocumentFocus) => void;
}) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  const [items, setItems] = useState<KBChunk[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const topRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    knowledgeBaseApi
      .getDocumentChunks(kbId, doc.id, { page, page_size: CHUNK_PAGE_SIZE })
      .then((res) => {
        if (cancelled) return;
        setItems(res.items);
        setTotal(res.total);
      })
      .catch((e) => {
        if (!cancelled) setError(errMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [kbId, doc.id, page]);

  const changePage = useCallback((next: number) => {
    setPage(next);
    topRef.current?.scrollIntoView({ block: "start" });
  }, []);

  if (loading && items.length === 0) {
    return (
      <div className="flex items-center justify-center py-16 text-stone-400">
        <Loader2 className="animate-spin" size={20} />
      </div>
    );
  }
  if (error) {
    return <div className="p-5 text-sm text-red-600 dark:text-red-400">{error}</div>;
  }
  if (items.length === 0) {
    return (
      <div className="p-5 text-sm text-stone-500 dark:text-stone-400">
        {t("knowledge.doc.noChunks", "暂无分块，文档入库完成后可在这里查看。")}
      </div>
    );
  }

  return (
    <div ref={topRef} className="space-y-3 p-4">
      <div className="text-xs text-stone-500 dark:text-stone-400">
        {t("knowledge.doc.totalChunks", {
          count: total,
          defaultValue: "共 {{count}} 个分块",
        })}
      </div>
      {items.map((chunk) => (
        <div
          key={chunk.chunk_id}
          className="rounded-xl border border-stone-200 bg-white p-3 dark:border-stone-700 dark:bg-stone-900"
        >
          <div className="mb-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-stone-500 dark:text-stone-400">
            <span className="font-semibold text-stone-800 dark:text-stone-100">
              #{chunk.index + 1}
            </span>
            <span>
              {t("knowledge.doc.lines", {
                range: formatLineRange(chunk.start_line, chunk.end_line),
                defaultValue: "第 {{range}} 行",
              })}
            </span>
            <span>
              {t("knowledge.doc.tokens", {
                count: chunk.token_count,
                defaultValue: "{{count}} tokens",
              })}
            </span>
            <button
              type="button"
              onClick={() =>
                onLocate({ startLine: chunk.start_line, endLine: chunk.end_line })
              }
              className="ml-auto inline-flex items-center gap-1 text-stone-500 hover:text-stone-900 dark:hover:text-stone-100"
            >
              <LocateFixed size={12} />
              {t("knowledge.doc.locate", "定位到原文")}
            </button>
          </div>
          <div className="whitespace-pre-wrap break-words text-[13px] leading-relaxed text-stone-800 dark:text-stone-200">
            {chunk.text}
          </div>
        </div>
      ))}
      <Pagination page={page} pageSize={CHUNK_PAGE_SIZE} total={total} onChange={changePage} />
    </div>
  );
}

export function DocumentDetailSidebar({
  kbId,
  doc,
  focus = null,
  initialTab = "content",
  onClose,
}: DocumentDetailSidebarProps) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<DocumentSidebarTab>(initialTab);
  const [activeFocus, setActiveFocus] = useState<DocumentFocus | null>(focus);

  const tabs: Array<{ key: DocumentSidebarTab; label: string }> = [
    { key: "content", label: t("knowledge.doc.tabContent", "解析内容") },
    { key: "chunks", label: t("knowledge.doc.tabChunks", "分块") },
  ];

  return (
    <EditorSidebar
      open
      onClose={onClose}
      title={doc.filename}
      subtitle={`${formatFileSize(doc.size)} · ${t("knowledge.doc.chunkCount", {
        count: doc.chunk_count,
        defaultValue: "{{count}} 个分块",
      })}`}
      icon={<FileText size={16} />}
      width="wide"
      defaultWidthPct={45}
      widthStorageKey="kb-document-sidebar-width"
    >
      <div className="sticky top-0 z-10 border-b border-[var(--theme-border)] bg-[var(--theme-bg)] px-4 py-2">
        <div
          role="tablist"
          className="inline-flex items-center gap-1 rounded-lg bg-[var(--theme-bg-subtle)] p-1"
        >
          {tabs.map(({ key, label }) => {
            const isActive = tab === key;
            return (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => setTab(key)}
                className={`rounded-md px-3 py-1 text-sm font-medium transition-colors ${
                  isActive
                    ? "is-active bg-[var(--theme-sidebar-active)] text-[var(--theme-text)]"
                    : "text-[var(--theme-text-secondary)] hover:bg-[var(--theme-sidebar-hover)] hover:text-[var(--theme-text)]"
                }`}
              >
                {label}
              </button>
            );
          })}
        </div>
      </div>
      {tab === "content" ? (
        <ContentView kbId={kbId} doc={doc} focus={activeFocus} />
      ) : (
        <ChunksView
          kbId={kbId}
          doc={doc}
          onLocate={(next) => {
            setActiveFocus(next);
            setTab("content");
          }}
        />
      )}
    </EditorSidebar>
  );
}
