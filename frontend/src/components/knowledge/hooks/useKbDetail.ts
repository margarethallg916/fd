import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import toast from "react-hot-toast";
import {
  knowledgeBaseApi,
  type KBDocumentListResponse,
  type KBDocumentStatus,
  type KnowledgeBase,
} from "../../../services/api/knowledgeBase";
import {
  DOCUMENT_PAGE_SIZE,
  buildPollSignature,
  isKbProcessing,
} from "../knowledgeUtils";
import { useAdaptivePolling } from "./useAdaptivePolling";

export type KbLoadError = "notFound" | "disabled" | "failed" | null;

function classifyError(e: unknown): Exclude<KbLoadError, null> {
  const err = e as { status?: number; code?: string } | null;
  if (err?.code === "kb_disabled") return "disabled";
  if (err?.status === 404) return "notFound";
  return "failed";
}

/** Knowledge base + current documents page, kept fresh by adaptive polling. */
export function useKbDetail(kbId: string) {
  const [kb, setKb] = useState<KnowledgeBase | null>(null);
  const [docs, setDocs] = useState<KBDocumentListResponse | null>(null);
  const [loadError, setLoadError] = useState<KbLoadError>(null);
  const [loading, setLoading] = useState(true);
  const [fetching, setFetching] = useState(false);

  const [search, setSearch] = useState("");
  const [debouncedSearch, setDebouncedSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState<KBDocumentStatus | "">("");
  const [page, setPage] = useState(1);

  const paramsRef = useRef({ q: "", status: "" as KBDocumentStatus | "", page: 1 });
  paramsRef.current = { q: debouncedSearch, status: statusFilter, page };
  const seqRef = useRef(0);

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedSearch(search.trim()), 300);
    return () => clearTimeout(timer);
  }, [search]);

  const fetchAll = useCallback(async (): Promise<string> => {
    const seq = ++seqRef.current;
    const params = paramsRef.current;
    try {
      const [kbRes, docRes] = await Promise.all([
        knowledgeBaseApi.get(kbId),
        knowledgeBaseApi.listDocuments(kbId, {
          status: params.status,
          q: params.q,
          page: params.page,
          page_size: DOCUMENT_PAGE_SIZE,
        }),
      ]);
      if (seq === seqRef.current) {
        setKb(kbRes);
        setDocs(docRes);
        setLoadError(null);
      }
      return buildPollSignature(kbRes, docRes.items);
    } catch (e) {
      const kind = classifyError(e);
      if (kind !== "failed" && seq === seqRef.current) setLoadError(kind);
      throw e;
    }
  }, [kbId]);

  const { trigger } = useAdaptivePolling({
    enabled: !loadError && isKbProcessing(kb, docs?.items),
    refresh: fetchAll,
  });

  useEffect(() => {
    let cancelled = false;
    setFetching(true);
    fetchAll()
      .catch((e) => {
        if (!cancelled && classifyError(e) === "failed") {
          toast.error(e instanceof Error ? e.message : String(e));
        }
      })
      .finally(() => {
        if (cancelled) return;
        setFetching(false);
        setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fetchAll, debouncedSearch, statusFilter, page]);

  // A page past the end (after deletions) falls back to the last page.
  useEffect(() => {
    if (!docs) return;
    const totalPages = Math.max(1, Math.ceil(docs.total / docs.page_size));
    if (page > totalPages) setPage(totalPages);
  }, [docs, page]);

  const refresh = useCallback(async () => {
    setFetching(true);
    try {
      await fetchAll();
    } catch (e) {
      if (classifyError(e) === "failed") {
        toast.error(e instanceof Error ? e.message : String(e));
      }
    } finally {
      setFetching(false);
    }
  }, [fetchAll]);

  const changeSearch = useCallback((value: string) => {
    setSearch(value);
    setPage(1);
  }, []);

  const changeStatus = useCallback((value: KBDocumentStatus | "") => {
    setStatusFilter(value);
    setPage(1);
  }, []);

  return useMemo(
    () => ({
      kb,
      setKb,
      docs,
      loading,
      fetching,
      loadError,
      search,
      setSearch: changeSearch,
      statusFilter,
      setStatusFilter: changeStatus,
      page,
      setPage,
      refresh,
      scheduleRefresh: trigger,
    }),
    [kb, docs, loading, fetching, loadError, search, changeSearch, statusFilter, changeStatus, page, refresh, trigger],
  );
}
