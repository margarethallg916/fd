import { useCallback, useEffect, useRef, useState } from "react";
import {
  knowledgeBaseApi,
  type KBUploadHandle,
} from "../../../services/api/knowledgeBase";
import { UPLOAD_CONCURRENCY } from "../knowledgeUtils";

export type UploadItemStatus =
  | "pending"
  | "uploading"
  | "done"
  | "duplicate"
  | "error";

export interface UploadItem {
  id: string;
  file: File;
  status: UploadItemStatus;
  progress: number;
  error?: string;
  /** Rejected client-side; retrying cannot help. */
  invalid?: boolean;
}

interface UseKbUploadQueueOptions {
  kbId: string;
  /** Returns an error message for files that must not be uploaded. */
  validate: (file: File) => string | null;
  /** Called after each upload settles so the document list can refresh. */
  onSettled?: () => void;
}

let uploadSeq = 0;

export function useKbUploadQueue({
  kbId,
  validate,
  onSettled,
}: UseKbUploadQueueOptions) {
  const [items, setItems] = useState<UploadItem[]>([]);
  const itemsRef = useRef<UploadItem[]>([]);
  const handlesRef = useRef(new Map<string, KBUploadHandle>());
  const validateRef = useRef(validate);
  const onSettledRef = useRef(onSettled);
  const pumpRef = useRef<() => void>(() => undefined);

  useEffect(() => {
    validateRef.current = validate;
    onSettledRef.current = onSettled;
  });

  const commit = useCallback((next: UploadItem[]) => {
    itemsRef.current = next;
    setItems(next);
  }, []);

  const patch = useCallback(
    (id: string, changes: Partial<UploadItem>) => {
      commit(
        itemsRef.current.map((item) =>
          item.id === id ? { ...item, ...changes } : item,
        ),
      );
    },
    [commit],
  );

  const startItem = useCallback(
    (item: UploadItem) => {
      patch(item.id, { status: "uploading", progress: 0, error: undefined });
      const handle = knowledgeBaseApi.uploadDocument(kbId, item.file, {
        onProgress: (progress) => patch(item.id, { progress }),
      });
      handlesRef.current.set(item.id, handle);
      handle.promise
        .then((res) => {
          patch(item.id, {
            status: res.duplicate ? "duplicate" : "done",
            progress: 100,
          });
        })
        .catch((e: unknown) => {
          const current = itemsRef.current.find((i) => i.id === item.id);
          // Aborted items were already removed or reset by the caller.
          if (!current || current.status !== "uploading") return;
          patch(item.id, {
            status: "error",
            error: e instanceof Error ? e.message : String(e),
          });
        })
        .finally(() => {
          handlesRef.current.delete(item.id);
          onSettledRef.current?.();
          pumpRef.current();
        });
    },
    [kbId, patch],
  );

  useEffect(() => {
    pumpRef.current = () => {
      const running = itemsRef.current.filter((i) => i.status === "uploading").length;
      let slots = UPLOAD_CONCURRENCY - running;
      for (const item of itemsRef.current) {
        if (slots <= 0) break;
        if (item.status !== "pending") continue;
        slots -= 1;
        startItem(item);
      }
    };
  }, [startItem]);

  const addFiles = useCallback(
    (files: File[]) => {
      const added: UploadItem[] = [];
      for (const file of files) {
        const duplicateInQueue = itemsRef.current.some(
          (i) =>
            i.file.name === file.name &&
            i.file.size === file.size &&
            i.file.lastModified === file.lastModified &&
            i.status !== "error",
        );
        if (duplicateInQueue) continue;
        const invalid = validateRef.current(file);
        uploadSeq += 1;
        added.push({
          id: `u${uploadSeq}`,
          file,
          status: invalid ? "error" : "pending",
          progress: 0,
          error: invalid ?? undefined,
          invalid: invalid ? true : undefined,
        });
      }
      if (added.length === 0) return;
      commit([...itemsRef.current, ...added]);
      pumpRef.current();
    },
    [commit],
  );

  const retry = useCallback(
    (id: string) => {
      const item = itemsRef.current.find((i) => i.id === id);
      if (!item || item.status !== "error" || item.invalid) return;
      patch(id, { status: "pending", progress: 0, error: undefined });
      pumpRef.current();
    },
    [patch],
  );

  const remove = useCallback(
    (id: string) => {
      handlesRef.current.get(id)?.abort();
      commit(itemsRef.current.filter((i) => i.id !== id));
      pumpRef.current();
    },
    [commit],
  );

  const clearFinished = useCallback(() => {
    commit(
      itemsRef.current.filter(
        (i) => i.status === "pending" || i.status === "uploading",
      ),
    );
  }, [commit]);

  const cancelAll = useCallback(() => {
    for (const handle of handlesRef.current.values()) handle.abort();
    commit(
      itemsRef.current.filter(
        (i) => i.status !== "pending" && i.status !== "uploading",
      ),
    );
  }, [commit]);

  const active = items.some(
    (i) => i.status === "pending" || i.status === "uploading",
  );

  return { items, active, addFiles, retry, remove, clearFinished, cancelAll };
}
