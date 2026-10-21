import { useCallback, useEffect, useMemo, useState } from "react";
import toast from "react-hot-toast";
import {
  knowledgeBaseApi,
  type KnowledgeBase,
} from "../../../services/api/knowledgeBase";
import { subscribeKnowledgeBaseRefresh } from "../../../utils/knowledgeBaseEvents";
import { isKbProcessing } from "../knowledgeUtils";
import { useAdaptivePolling } from "./useAdaptivePolling";

function signatureOf(items: KnowledgeBase[]): string {
  return items
    .map((kb) => `${kb.id}:${kb.stats.doc_count}:${kb.stats.processing_count}:${kb.stats.indexed_count}`)
    .join("|");
}

export function useKnowledgeBases() {
  const [items, setItems] = useState<KnowledgeBase[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<unknown>(null);

  const fetchList = useCallback(async (): Promise<string> => {
    const res = await knowledgeBaseApi.list("mine");
    setItems(res.items);
    setError(null);
    return signatureOf(res.items);
  }, []);

  const reload = useCallback(async () => {
    try {
      await fetchList();
    } catch (e) {
      setError(e);
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }, [fetchList]);

  useEffect(() => {
    void reload();
    return subscribeKnowledgeBaseRefresh(() => void reload());
  }, [reload]);

  const processing = useMemo(
    () => items.some((kb) => isKbProcessing(kb)),
    [items],
  );
  useAdaptivePolling({ enabled: processing, refresh: fetchList });

  return { items, setItems, loading, error, reload };
}
