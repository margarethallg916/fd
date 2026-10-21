import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import {
  AlertTriangle,
  Download,
  Eye,
  FileText,
  Loader2,
  RefreshCw,
  RotateCcw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import { Checkbox } from "../common/Checkbox";
import { ConfirmDialog } from "../common/ConfirmDialog";
import { GlassSelect } from "../common/GlassSelect";
import { Pagination } from "../common/Pagination";
import { PanelSearchInput } from "../common/PanelSearchInput";
import { getFileTypeInfo } from "../documents/utils";
import { formatFileSize } from "../documents/utils/fileSize";
import { formatDateTimeShort } from "../../utils/datetime";
import {
  knowledgeBaseApi,
  type KBConfig,
  type KBDocument,
} from "../../services/api/knowledgeBase";
import { DocumentStatusBadge } from "./DocumentStatusBadge";
import { UploadDialog } from "./UploadDialog";
import type { DocumentSidebarTab } from "./DocumentDetailSidebar";
import type { useKbDetail } from "./hooks/useKbDetail";
import { useKbUploadQueue } from "./hooks/useKbUploadQueue";
import {
  DOCUMENT_PAGE_SIZE,
  DOCUMENT_STATUSES,
  POST_ACTION_REFRESH_MS,
  getDocStatusLabel,
  hasActiveTask,
  isFailedStatus,
  pruneSelection,
  taskProgressPercent,
  validateUploadFile,
} from "./knowledgeUtils";
import {
  DANGER_BTN,
  GHOST_BTN,
  NOTICE_WARNING,
  OUTLINE_BTN,
  PRIMARY_BTN,
  ROW_ACTION_BTN,
} from "./knowledgeStyles";

interface DocumentsTabProps {
  kbId: string;
  detail: ReturnType<typeof useKbDetail>;
  config: KBConfig | null;
  onOpenDocument: (doc: KBDocument, tab?: DocumentSidebarTab) => void;
}

const VIEWABLE_STATUSES = new Set(["parsed", "indexing", "indexed", "error_indexing"]);

function errMessage(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

export function DocumentsTab({ kbId, detail, config, onOpenDocument }: DocumentsTabProps) {
  const { t } = useTranslation();
  const {
    kb,
    docs,
    loading,
    fetching,
    search,
    setSearch,
    statusFilter,
    setStatusFilter,
    page,
    setPage,
    refresh,
    scheduleRefresh,
  } = detail;

  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busyIds, setBusyIds] = useState<Set<string>>(new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [uploadOpen, setUploadOpen] = useState(false);
  const [deleteIds, setDeleteIds] = useState<string[] | null>(null);
  const [reindexBusy, setReindexBusy] = useState(false);
  const [cancelBusy, setCancelBusy] = useState(false);

  const items = useMemo(() => docs?.items ?? [], [docs]);

  useEffect(() => {
    setSelected((prev) => {
      const next = pruneSelection(prev, items);
      return next.size === prev.size ? prev : next;
    });
  }, [items]);

  const queue = useKbUploadQueue({
    kbId,
    validate: (file) => {
      const err = validateUploadFile(file, config);
      if (!err) return null;
      if (err.code === "extension") {
        return t("knowledge.upload.errors.extension", {
          ext: err.ext,
          defaultValue: "不支持的文件类型：{{ext}}",
        });
      }
      if (err.code === "empty") {
        return t("knowledge.upload.errors.empty", "文件为空");
      }
      return t("knowledge.upload.errors.size", {
        max: err.maxMb,
        defaultValue: "文件超过 {{max}} MB 限制",
      });
    },
    onSettled: () => scheduleRefresh(POST_ACTION_REFRESH_MS),
  });

  const withBusy = async (ids: string[], action: () => Promise<void>) => {
    setBusyIds((prev) => new Set([...prev, ...ids]));
    try {
      await action();
    } catch (e) {
      toast.error(errMessage(e));
    } finally {
      setBusyIds((prev) => {
        const next = new Set(prev);
        ids.forEach((id) => next.delete(id));
        return next;
      });
    }
  };

  const retryDocs = async (ids: string[]) => {
    await withBusy(ids, async () => {
      const res = await knowledgeBaseApi.retryDocuments(kbId, ids);
      toast.success(
        t("knowledge.toast.retryQueued", {
          count: res.count,
          defaultValue: "已将 {{count}} 个文档重新加入处理队列",
        }),
      );
      scheduleRefresh(POST_ACTION_REFRESH_MS);
    });
  };

  const retrySelected = async () => {
    const failedIds = items
      .filter((d) => selected.has(d.id) && isFailedStatus(d.status))
      .map((d) => d.id);
    if (failedIds.length === 0) return;
    setBulkBusy(true);
    try {
      await retryDocs(failedIds);
      setSelected(new Set());
    } finally {
      setBulkBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteIds) return;
    const ids = deleteIds;
    setBulkBusy(true);
    try {
      const res = await knowledgeBaseApi.deleteDocuments(kbId, ids);
      toast.success(
        t("knowledge.toast.docsDeleted", {
          count: res.deleted,
          defaultValue: "已删除 {{count}} 个文档",
        }),
      );
      setSelected(new Set());
      setDeleteIds(null);
      await refresh();
    } catch (e) {
      toast.error(errMessage(e));
    } finally {
      setBulkBusy(false);
    }
  };

  const download = (doc: KBDocument) =>
    withBusy([doc.id], () => knowledgeBaseApi.downloadDocument(kbId, doc));

  const reindex = async () => {
    setReindexBusy(true);
    try {
      const res = await knowledgeBaseApi.reindex(kbId);
      toast.success(
        res.count > 0
          ? t("knowledge.toast.reindexQueued", {
              count: res.count,
              defaultValue: "已开始重建索引，共 {{count}} 个文档",
            })
          : t("knowledge.toast.reindexNothing", "没有需要重建索引的文档"),
      );
      scheduleRefresh(POST_ACTION_REFRESH_MS);
    } catch (e) {
      toast.error(errMessage(e));
    } finally {
      setReindexBusy(false);
    }
  };

  const cancelTask = async () => {
    if (!kb?.active_task) return;
    setCancelBusy(true);
    try {
      await knowledgeBaseApi.cancelTask(kbId, kb.active_task.id);
      toast.success(t("knowledge.toast.taskCancelled", "已请求取消处理任务"));
      scheduleRefresh(POST_ACTION_REFRESH_MS);
    } catch (e) {
      toast.error(errMessage(e));
    } finally {
      setCancelBusy(false);
    }
  };

  const allSelected = items.length > 0 && items.every((d) => selected.has(d.id));
  const toggleAll = () =>
    setSelected(allSelected ? new Set() : new Set(items.map((d) => d.id)));
  const toggleOne = (id: string) =>
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const selectedFailedCount = items.filter(
    (d) => selected.has(d.id) && isFailedStatus(d.status),
  ).length;

  const statusOptions = [
    { value: "", label: t("knowledge.filter.allStatus", "全部状态") },
    ...DOCUMENT_STATUSES.map((s) => ({ value: s, label: getDocStatusLabel(t, s) })),
  ];

  const task = kb?.active_task;
  const taskActive = hasActiveTask(task);
  const hasFilter = search.trim() !== "" || statusFilter !== "";

  return (
    <div className="px-4 py-4 sm:px-6">
      {kb?.index_outdated && (
        <div role="alert" className={`${NOTICE_WARNING} mb-3 items-center justify-between`}>
          <span className="flex items-start gap-2">
            <AlertTriangle size={16} className="mt-0.5 shrink-0" />
            {t(
              "knowledge.outdated.banner",
              "向量模型已变更，现有索引与当前模型不一致，检索暂时退化为关键词匹配。重建索引后恢复语义检索。",
            )}
          </span>
          <button
            type="button"
            onClick={reindex}
            disabled={reindexBusy || taskActive}
            className="inline-flex shrink-0 items-center gap-1.5 rounded-lg bg-amber-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-amber-800 disabled:opacity-50 dark:bg-amber-200 dark:text-amber-950"
          >
            {reindexBusy && <Loader2 size={12} className="animate-spin" />}
            {t("knowledge.outdated.reindex", "重建索引")}
          </button>
        </div>
      )}

      {task && taskActive && (
        <div className="mb-3 rounded-2xl border border-stone-200 bg-white px-4 py-3 dark:border-stone-700 dark:bg-stone-900">
          <div className="flex items-center justify-between gap-3">
            <div className="flex min-w-0 items-center gap-2 text-sm text-stone-800 dark:text-stone-100">
              <Loader2 size={14} className="shrink-0 animate-spin" />
              <span className="truncate">
                {task.status === "queued"
                  ? t("knowledge.task.queued", "处理任务排队中")
                  : t("knowledge.task.running", {
                      done: task.progress.done + task.progress.failed,
                      total: task.progress.total,
                      defaultValue: "已处理 {{done}}/{{total}}",
                    })}
              </span>
              {task.progress.failed > 0 && (
                <span className="shrink-0 text-xs text-red-600 dark:text-red-400">
                  {t("knowledge.task.failed", {
                    count: task.progress.failed,
                    defaultValue: "{{count}} 个失败",
                  })}
                </span>
              )}
            </div>
            <button
              type="button"
              onClick={cancelTask}
              disabled={cancelBusy}
              className={`${GHOST_BTN} !px-3 !py-1.5 text-xs`}
            >
              {t("knowledge.task.cancel", "取消处理")}
            </button>
          </div>
          <div
            className="mt-2 h-1.5 overflow-hidden rounded-full bg-stone-100 dark:bg-stone-800"
            role="progressbar"
            aria-valuenow={taskProgressPercent(task)}
            aria-valuemin={0}
            aria-valuemax={100}
          >
            <div
              className="h-full rounded-full bg-stone-900 transition-[width] duration-500 dark:bg-stone-100"
              style={{ width: `${taskProgressPercent(task)}%` }}
            />
          </div>
        </div>
      )}

      <div className="mb-3 flex flex-wrap items-center gap-2">
        <button type="button" onClick={() => setUploadOpen(true)} className={PRIMARY_BTN}>
          <Upload size={16} />
          {t("knowledge.upload.button", "上传文档")}
          {queue.active && <Loader2 size={14} className="animate-spin" />}
        </button>
        <div className="relative min-w-[12rem] flex-1 sm:max-w-xs">
          <Search
            size={16}
            className="absolute left-3 top-1/2 -translate-y-1/2 text-theme-text-tertiary"
          />
          <PanelSearchInput
            type="text"
            value={search}
            onValueChange={setSearch}
            className="panel-search h-10"
            placeholder={t("knowledge.docs.searchPlaceholder", "搜索文件名")}
          />
        </div>
        <GlassSelect
          value={statusFilter}
          onChange={(v) => setStatusFilter(v as typeof statusFilter)}
          options={statusOptions}
          className="w-36"
        />
        <button
          type="button"
          onClick={() => void refresh()}
          disabled={fetching}
          className={ROW_ACTION_BTN}
          aria-label={t("common.refresh", "刷新")}
          title={t("common.refresh", "刷新")}
        >
          <RefreshCw size={16} className={fetching ? "animate-spin" : ""} />
        </button>
      </div>

      {selected.size > 0 && (
        <div className="mb-3 flex flex-wrap items-center gap-2 rounded-xl bg-[var(--theme-bg-subtle)] px-3 py-2 text-sm">
          <span className="text-stone-700 dark:text-stone-200">
            {t("knowledge.docs.selected", {
              count: selected.size,
              defaultValue: "已选 {{count}} 项",
            })}
          </span>
          <div className="flex-1" />
          {selectedFailedCount > 0 && (
            <button
              type="button"
              onClick={() => void retrySelected()}
              disabled={bulkBusy}
              className={OUTLINE_BTN}
            >
              <RotateCcw size={14} />
              {t("knowledge.docs.retrySelected", "重试失败项")}
            </button>
          )}
          <button
            type="button"
            onClick={() => setDeleteIds([...selected])}
            disabled={bulkBusy}
            className={DANGER_BTN}
          >
            <Trash2 size={14} />
            {t("knowledge.docs.deleteSelected", "批量删除")}
          </button>
          <button
            type="button"
            onClick={() => setSelected(new Set())}
            className={GHOST_BTN}
          >
            {t("knowledge.docs.clearSelection", "取消选择")}
          </button>
        </div>
      )}

      {loading ? (
        <div className="flex items-center justify-center py-16 text-stone-400">
          <Loader2 className="animate-spin" size={22} />
        </div>
      ) : items.length === 0 ? (
        <div className="mx-auto mt-4 max-w-xl rounded-3xl border border-stone-200 p-6 text-center dark:border-stone-800">
          <FileText className="mx-auto mb-3 text-stone-400" size={36} />
          <h3 className="text-lg font-semibold text-stone-900 dark:text-stone-100">
            {hasFilter
              ? t("knowledge.docs.noMatchTitle", "没有匹配的文档")
              : t("knowledge.docs.emptyTitle", "还没有文档")}
          </h3>
          <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
            {hasFilter
              ? t("knowledge.docs.noMatchDesc", "换个文件名或状态筛选试试。")
              : t(
                  "knowledge.docs.emptyDesc",
                  "上传 PDF、Word、PPT、表格、文本等文档，系统会自动解析并建立索引。",
                )}
          </p>
          {!hasFilter && (
            <button
              type="button"
              onClick={() => setUploadOpen(true)}
              className={`${PRIMARY_BTN} mt-4`}
            >
              <Upload size={16} />
              {t("knowledge.upload.button", "上传文档")}
            </button>
          )}
        </div>
      ) : (
        <div
          className={`overflow-hidden rounded-2xl border border-[var(--theme-border)] bg-[var(--theme-bg-card)] transition-opacity ${
            fetching ? "opacity-80" : ""
          }`}
        >
          <div className="overflow-x-auto">
            <table className="w-full min-w-[34rem] text-left text-sm">
              <thead className="bg-[var(--theme-bg-subtle)] text-xs text-[var(--theme-text-secondary)]">
                <tr>
                  <th className="w-10 px-3 py-2.5">
                    <Checkbox checked={allSelected} onChange={toggleAll} size="sm" />
                  </th>
                  <th className="px-3 py-2.5 font-medium">
                    {t("knowledge.docs.colName", "文件名")}
                  </th>
                  <th className="px-3 py-2.5 font-medium">
                    {t("knowledge.docs.colStatus", "状态")}
                  </th>
                  <th className="hidden px-3 py-2.5 font-medium md:table-cell">
                    {t("knowledge.docs.colChunks", "分块")}
                  </th>
                  <th className="hidden px-3 py-2.5 font-medium md:table-cell">
                    {t("knowledge.docs.colSize", "大小")}
                  </th>
                  <th className="hidden px-3 py-2.5 font-medium lg:table-cell">
                    {t("knowledge.docs.colUploaded", "上传时间")}
                  </th>
                  <th className="px-3 py-2.5 text-right font-medium">
                    {t("knowledge.docs.colActions", "操作")}
                  </th>
                </tr>
              </thead>
              <tbody>
                {items.map((doc) => {
                  const info = getFileTypeInfo(doc.filename);
                  const Icon = info.icon;
                  const busy = busyIds.has(doc.id);
                  const viewable = VIEWABLE_STATUSES.has(doc.status);
                  return (
                    <tr
                      key={doc.id}
                      className="border-t border-[var(--theme-border)] hover:bg-[var(--theme-bg-subtle)]"
                    >
                      <td className="px-3 py-2.5">
                        <Checkbox
                          checked={selected.has(doc.id)}
                          onChange={() => toggleOne(doc.id)}
                          size="sm"
                        />
                      </td>
                      <td className="max-w-[16rem] px-3 py-2.5">
                        <button
                          type="button"
                          onClick={() => onOpenDocument(doc)}
                          disabled={!viewable}
                          className="flex w-full min-w-0 items-center gap-2 text-left disabled:cursor-default"
                          title={doc.filename}
                        >
                          <span
                            className={`flex h-8 w-8 shrink-0 items-center justify-center rounded-lg ${info.bg}`}
                          >
                            <Icon size={16} className={info.color} />
                          </span>
                          <span className="truncate font-medium text-[var(--theme-text)]">
                            {doc.filename}
                          </span>
                        </button>
                      </td>
                      <td className="px-3 py-2.5">
                        <DocumentStatusBadge
                          doc={doc}
                          disabled={busy}
                          onRetry={() => void retryDocs([doc.id])}
                        />
                      </td>
                      <td className="hidden px-3 py-2.5 text-[var(--theme-text-secondary)] md:table-cell">
                        {doc.chunk_count || "–"}
                      </td>
                      <td className="hidden whitespace-nowrap px-3 py-2.5 text-[var(--theme-text-secondary)] md:table-cell">
                        {formatFileSize(doc.size)}
                      </td>
                      <td className="hidden whitespace-nowrap px-3 py-2.5 text-[var(--theme-text-secondary)] lg:table-cell">
                        {formatDateTimeShort(doc.created_at)}
                      </td>
                      <td className="px-3 py-2.5">
                        <div className="flex items-center justify-end gap-0.5">
                          <button
                            type="button"
                            onClick={() => onOpenDocument(doc)}
                            disabled={!viewable}
                            className={ROW_ACTION_BTN}
                            aria-label={t("knowledge.docs.view", "查看")}
                            title={t("knowledge.docs.view", "查看")}
                          >
                            <Eye size={16} />
                          </button>
                          <button
                            type="button"
                            onClick={() => void download(doc)}
                            disabled={busy}
                            className={ROW_ACTION_BTN}
                            aria-label={t("knowledge.docs.download", "下载")}
                            title={t("knowledge.docs.download", "下载")}
                          >
                            <Download size={16} />
                          </button>
                          {isFailedStatus(doc.status) && (
                            <button
                              type="button"
                              onClick={() => void retryDocs([doc.id])}
                              disabled={busy}
                              className={ROW_ACTION_BTN}
                              aria-label={t("knowledge.docs.retry", "重试")}
                              title={t("knowledge.docs.retry", "重试")}
                            >
                              <RotateCcw size={16} />
                            </button>
                          )}
                          <button
                            type="button"
                            onClick={() => setDeleteIds([doc.id])}
                            disabled={busy}
                            className={`${ROW_ACTION_BTN} hover:!text-red-600`}
                            aria-label={t("common.delete", "删除")}
                            title={t("common.delete", "删除")}
                          >
                            <Trash2 size={16} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {docs && docs.total > DOCUMENT_PAGE_SIZE && (
        <div className="mt-3">
          <Pagination
            page={page}
            pageSize={docs.page_size || DOCUMENT_PAGE_SIZE}
            total={docs.total}
            onChange={setPage}
          />
        </div>
      )}

      {uploadOpen && (
        <UploadDialog
          config={config}
          items={queue.items}
          active={queue.active}
          onAddFiles={queue.addFiles}
          onRetry={queue.retry}
          onRemove={queue.remove}
          onCancelAll={queue.cancelAll}
          onClose={() => {
            setUploadOpen(false);
            queue.clearFinished();
          }}
        />
      )}

      <ConfirmDialog
        isOpen={!!deleteIds}
        title={t("knowledge.docs.deleteTitle", "删除文档")}
        message={t("knowledge.docs.deleteConfirm", {
          count: deleteIds?.length ?? 0,
          defaultValue: "确定删除选中的 {{count}} 个文档吗？原文件、分块和检索索引都会被删除，无法恢复。",
        })}
        confirmText={t("common.delete", "删除")}
        variant="danger"
        loading={bulkBusy}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleteIds(null)}
      />
    </div>
  );
}
