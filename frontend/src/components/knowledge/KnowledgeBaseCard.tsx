import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { Loader2, LibraryBig, MoreHorizontal, Pencil, Trash2 } from "lucide-react";
import type { KnowledgeBase } from "../../services/api/knowledgeBase";
import { formatFileSize } from "../documents/utils/fileSize";
import { formatTimeAgo } from "../../utils/datetime";
import { STATUS_TONE_CLASSES, isKbProcessing } from "./knowledgeUtils";

interface KnowledgeBaseCardProps {
  kb: KnowledgeBase;
  onOpen: () => void;
  onEdit: () => void;
  onDelete: () => void;
}

function Dot() {
  return <span className="inline-block h-1 w-1 rounded-full bg-[var(--theme-border)]" />;
}

function CardMenu({
  onEdit,
  onDelete,
  label,
}: {
  onEdit: () => void;
  onDelete: () => void;
  label: string;
}) {
  const { t } = useTranslation();
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);

  useEffect(() => {
    if (!pos) return;
    const close = () => setPos(null);
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && close();
    const onDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (menuRef.current?.contains(target) || buttonRef.current?.contains(target)) return;
      close();
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [pos]);

  // Keep the menu inside the viewport once its size is known.
  useLayoutEffect(() => {
    const el = menuRef.current;
    if (!pos || !el) return;
    const rect = el.getBoundingClientRect();
    if (rect.bottom > window.innerHeight) {
      el.style.top = `${Math.max(8, window.innerHeight - rect.height - 8)}px`;
    }
  }, [pos]);

  const toggle = (e: React.MouseEvent) => {
    e.stopPropagation();
    if (pos) {
      setPos(null);
      return;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    if (!rect) return;
    setPos({ top: rect.bottom + 4, left: Math.max(8, rect.right - 144) });
  };

  const itemCls =
    "flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-stone-100 dark:hover:bg-stone-800";

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={toggle}
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={!!pos}
        className="-mr-1.5 flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-[var(--theme-text-secondary)] hover:bg-[var(--theme-sidebar-hover)]"
      >
        <MoreHorizontal size={16} />
      </button>
      {pos &&
        createPortal(
          <div
            ref={menuRef}
            role="menu"
            style={{ top: pos.top, left: pos.left }}
            onClick={(e) => e.stopPropagation()}
            className="fixed z-[999] w-36 overflow-hidden rounded-xl border border-stone-200 bg-white py-1 shadow-lg dark:border-stone-700 dark:bg-stone-900"
          >
            <button
              type="button"
              role="menuitem"
              className={`${itemCls} text-stone-700 dark:text-stone-200`}
              onClick={() => {
                setPos(null);
                onEdit();
              }}
            >
              <Pencil size={14} />
              {t("common.edit", "编辑")}
            </button>
            <button
              type="button"
              role="menuitem"
              className={`${itemCls} text-red-600 dark:text-red-400`}
              onClick={() => {
                setPos(null);
                onDelete();
              }}
            >
              <Trash2 size={14} />
              {t("common.delete", "删除")}
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}

export function KnowledgeBaseCard({ kb, onOpen, onEdit, onDelete }: KnowledgeBaseCardProps) {
  const { t } = useTranslation();
  const processing = isKbProcessing(kb);
  const { stats } = kb;

  return (
    <div
      role="link"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.target === e.currentTarget && (e.key === "Enter" || e.key === " ")) {
          e.preventDefault();
          onOpen();
        }
      }}
      className="scb group flex h-full cursor-pointer flex-col overflow-hidden bg-[var(--theme-bg-card)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[var(--theme-ring)]"
    >
      <div className="flex flex-1 flex-col p-4 sm:p-5">
        <div className="flex items-start gap-3">
          <div className="scb__icon-ring shrink-0">
            <LibraryBig size={20} className="text-[var(--theme-primary)]" />
          </div>
          <div className="min-w-0 flex-1">
            <h3
              className="truncate text-base font-semibold leading-tight text-[var(--theme-text)]"
              title={kb.name}
            >
              {kb.name}
            </h3>
            <div className="mt-1.5 flex flex-wrap items-center gap-1.5 text-[11px] text-[var(--theme-text-secondary)]">
              <span>
                {t("knowledge.card.docCount", {
                  count: stats.doc_count,
                  defaultValue: "{{count}} 个文档",
                })}
              </span>
              <Dot />
              <span>
                {t("knowledge.card.chunkCount", {
                  count: stats.chunk_count,
                  defaultValue: "{{count}} 个分块",
                })}
              </span>
              <Dot />
              <span>{formatFileSize(stats.size_bytes)}</span>
            </div>
          </div>
          {processing && (
            <span
              className={`inline-flex shrink-0 items-center gap-1 rounded-full px-2 py-0.5 text-[10px] font-semibold ${STATUS_TONE_CLASSES.info}`}
            >
              <Loader2 size={10} className="animate-spin" aria-hidden="true" />
              {t("knowledge.card.processing", "处理中")}
            </span>
          )}
          <CardMenu
            onEdit={onEdit}
            onDelete={onDelete}
            label={t("knowledge.card.more", "更多操作")}
          />
        </div>

        <p className="mt-3 line-clamp-2 min-h-[2.5rem] text-[13px] leading-relaxed text-[var(--theme-text-secondary)]">
          {kb.description || t("knowledge.card.noDescription", "暂无描述")}
        </p>

        <div className="mt-4 flex items-center justify-between gap-2 border-t border-[var(--theme-border)] pt-3">
          <div className="flex min-w-0 flex-wrap items-center gap-1.5">
            <span className="scb__category-tag">
              {t(`knowledge.preset.${kb.chunk_preset}.name`, kb.chunk_preset)}
            </span>
            {stats.failed_count > 0 && (
              <span className="scb__status-pill scb__status-pill--danger">
                {t("knowledge.card.failedCount", {
                  count: stats.failed_count,
                  defaultValue: "{{count}} 个失败",
                })}
              </span>
            )}
            {kb.index_outdated && (
              <span className="scb__status-pill scb__status-pill--disabled">
                {t("knowledge.card.indexOutdated", "需重建索引")}
              </span>
            )}
          </div>
          <span className="shrink-0 text-[11px] text-[var(--theme-text-tertiary)]">
            {formatTimeAgo(t, kb.updated_at)}
          </span>
        </div>
      </div>
    </div>
  );
}
