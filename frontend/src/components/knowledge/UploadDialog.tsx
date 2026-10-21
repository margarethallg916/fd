import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import {
  AlertCircle,
  CheckCircle2,
  Copy,
  Loader2,
  RotateCcw,
  UploadCloud,
  X,
} from "lucide-react";
import type { KBConfig } from "../../services/api/knowledgeBase";
import { formatFileSize } from "../documents/utils/fileSize";
import type { UploadItem } from "./hooks/useKbUploadQueue";
import { GHOST_BTN, PRIMARY_BTN } from "./knowledgeStyles";

interface UploadDialogProps {
  config: KBConfig | null;
  items: UploadItem[];
  active: boolean;
  onAddFiles: (files: File[]) => void;
  onRetry: (id: string) => void;
  onRemove: (id: string) => void;
  onCancelAll: () => void;
  onClose: () => void;
}

function ItemStatus({ item }: { item: UploadItem }) {
  const { t } = useTranslation();
  switch (item.status) {
    case "done":
      return (
        <span className="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400">
          <CheckCircle2 size={13} />
          {t("knowledge.upload.done", "已上传")}
        </span>
      );
    case "duplicate":
      return (
        <span className="inline-flex items-center gap-1 text-xs text-amber-600 dark:text-amber-400">
          <Copy size={13} />
          {t("knowledge.upload.duplicate", "已存在，已跳过")}
        </span>
      );
    case "error":
      return (
        <span
          className="inline-flex min-w-0 items-center gap-1 text-xs text-red-600 dark:text-red-400"
          title={item.error}
        >
          <AlertCircle size={13} className="shrink-0" />
          <span className="truncate">{item.error}</span>
        </span>
      );
    case "uploading":
      return (
        <span className="inline-flex items-center gap-1 text-xs text-stone-500 dark:text-stone-400">
          <Loader2 size={13} className="animate-spin" />
          {item.progress}%
        </span>
      );
    default:
      return (
        <span className="text-xs text-stone-400">
          {t("knowledge.upload.pending", "等待上传")}
        </span>
      );
  }
}

export function UploadDialog({
  config,
  items,
  active,
  onAddFiles,
  onRetry,
  onRemove,
  onCancelAll,
  onClose,
}: UploadDialogProps) {
  const { t } = useTranslation();
  const inputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const accept = (config?.allowed_extensions ?? []).join(",");
  const extensionsText = (config?.allowed_extensions ?? []).join(" ");

  const pickFiles = (list: FileList | null) => {
    if (!list || list.length === 0) return;
    onAddFiles(Array.from(list));
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-0 sm:items-center sm:p-4"
      onClick={onClose}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={t("knowledge.upload.title", "上传文档")}
        className="flex max-h-[92vh] w-full flex-col overflow-hidden rounded-t-3xl bg-white shadow-2xl dark:bg-stone-900 sm:max-w-xl sm:rounded-3xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-stone-100 px-5 py-4 dark:border-stone-800">
          <h2 className="text-base font-semibold text-stone-900 dark:text-stone-100">
            {t("knowledge.upload.title", "上传文档")}
          </h2>
          <button
            type="button"
            onClick={onClose}
            className="rounded-lg p-1 text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800"
            aria-label={t("common.close", "关闭")}
          >
            <X size={18} />
          </button>
        </div>

        <div className="flex-1 space-y-4 overflow-y-auto px-5 py-5">
          <div
            role="button"
            tabIndex={0}
            onClick={() => inputRef.current?.click()}
            onKeyDown={(e) => {
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                inputRef.current?.click();
              }
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              pickFiles(e.dataTransfer.files);
            }}
            className={`flex cursor-pointer flex-col items-center justify-center gap-2 rounded-2xl border-2 border-dashed px-4 py-8 text-center transition-colors ${
              dragging
                ? "border-stone-900 bg-stone-50 dark:border-stone-100 dark:bg-stone-800"
                : "border-stone-300 hover:border-stone-500 dark:border-stone-600 dark:hover:border-stone-400"
            }`}
          >
            <UploadCloud size={28} className="text-stone-400" />
            <div className="text-sm font-medium text-stone-800 dark:text-stone-100">
              {t("knowledge.upload.dropHint", "拖拽文件到这里，或点击选择文件")}
            </div>
            {config && (
              <div className="max-w-sm text-xs leading-relaxed text-stone-500 dark:text-stone-400">
                {t("knowledge.upload.limits", {
                  max: config.max_file_mb,
                  defaultValue: "单个文件不超过 {{max}} MB",
                })}
                {extensionsText && (
                  <>
                    <br />
                    {t("knowledge.upload.supported", {
                      list: extensionsText,
                      defaultValue: "支持：{{list}}",
                    })}
                  </>
                )}
              </div>
            )}
            <input
              ref={inputRef}
              type="file"
              multiple
              accept={accept || undefined}
              className="hidden"
              onChange={(e) => {
                pickFiles(e.target.files);
                e.target.value = "";
              }}
            />
          </div>

          {items.length > 0 && (
            <ul className="space-y-2">
              {items.map((item) => (
                <li
                  key={item.id}
                  className="rounded-xl border border-stone-200 px-3 py-2 dark:border-stone-700"
                >
                  <div className="flex items-center gap-2">
                    <div className="min-w-0 flex-1">
                      <div
                        className="truncate text-sm text-stone-900 dark:text-stone-100"
                        title={item.file.name}
                      >
                        {item.file.name}
                      </div>
                      <div className="mt-0.5 flex items-center gap-2">
                        <span className="shrink-0 text-[11px] text-stone-400">
                          {formatFileSize(item.file.size)}
                        </span>
                        <ItemStatus item={item} />
                      </div>
                    </div>
                    {item.status === "error" && !item.invalid && (
                      <button
                        type="button"
                        onClick={() => onRetry(item.id)}
                        className="rounded-lg p-1.5 text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800"
                        aria-label={t("knowledge.upload.retry", "重试")}
                        title={t("knowledge.upload.retry", "重试")}
                      >
                        <RotateCcw size={14} />
                      </button>
                    )}
                    <button
                      type="button"
                      onClick={() => onRemove(item.id)}
                      className="rounded-lg p-1.5 text-stone-500 hover:bg-stone-100 dark:hover:bg-stone-800"
                      aria-label={t("common.remove", "移除")}
                      title={t("common.remove", "移除")}
                    >
                      <X size={14} />
                    </button>
                  </div>
                  {item.status === "uploading" && (
                    <div
                      className="mt-2 h-1 overflow-hidden rounded-full bg-stone-100 dark:bg-stone-800"
                      role="progressbar"
                      aria-valuenow={item.progress}
                      aria-valuemin={0}
                      aria-valuemax={100}
                    >
                      <div
                        className="h-full rounded-full bg-stone-900 transition-[width] dark:bg-stone-100"
                        style={{ width: `${item.progress}%` }}
                      />
                    </div>
                  )}
                </li>
              ))}
            </ul>
          )}
        </div>

        <div className="flex items-center justify-between gap-2 border-t border-stone-100 px-5 py-4 dark:border-stone-800">
          <p className="text-xs text-stone-500 dark:text-stone-400">
            {t("knowledge.upload.autoProcessHint", "上传完成后会自动解析并入库，可在文档列表查看进度。")}
          </p>
          <div className="flex shrink-0 gap-2">
            {active && (
              <button type="button" onClick={onCancelAll} className={GHOST_BTN}>
                {t("knowledge.upload.cancelAll", "取消上传")}
              </button>
            )}
            <button type="button" onClick={onClose} className={PRIMARY_BTN}>
              {t("common.done", "完成")}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
