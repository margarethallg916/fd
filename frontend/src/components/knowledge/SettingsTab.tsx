import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { Info, Loader2, RefreshCw, Trash2 } from "lucide-react";
import { ConfirmDialog } from "../common/ConfirmDialog";
import {
  knowledgeBaseApi,
  type KBConfig,
  type KnowledgeBase,
} from "../../services/api/knowledgeBase";
import { useKbForm } from "./hooks/useKbForm";
import { KnowledgeBaseFormFields } from "./KnowledgeBaseFormFields";
import { DANGER_BTN, OUTLINE_BTN, PRIMARY_BTN } from "./knowledgeStyles";
import { hasActiveTask } from "./knowledgeUtils";

interface SettingsTabProps {
  kb: KnowledgeBase;
  config: KBConfig | null;
  onSaved: (kb: KnowledgeBase) => void;
  onReindexQueued: () => void;
}

export function SettingsTab({ kb, config, onSaved, onReindexQueued }: SettingsTabProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const form = useKbForm(kb, config);
  const [saving, setSaving] = useState(false);
  const [showRebuildHint, setShowRebuildHint] = useState(false);
  const [reindexBusy, setReindexBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [deleting, setDeleting] = useState(false);

  const save = async () => {
    if (!form.payload || saving) return;
    const chunkWasChanged = form.chunkChanged;
    setSaving(true);
    try {
      const saved = await knowledgeBaseApi.update(kb.id, form.payload);
      toast.success(t("knowledge.toast.updated", "已保存修改"));
      setShowRebuildHint(chunkWasChanged && kb.stats.doc_count > 0);
      onSaved(saved);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  const reindex = async () => {
    setReindexBusy(true);
    try {
      const res = await knowledgeBaseApi.reindex(kb.id);
      toast.success(
        res.count > 0
          ? t("knowledge.toast.reindexQueued", {
              count: res.count,
              defaultValue: "已开始重建索引，共 {{count}} 个文档",
            })
          : t("knowledge.toast.reindexNothing", "没有需要重建索引的文档"),
      );
      setShowRebuildHint(false);
      onReindexQueued();
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setReindexBusy(false);
    }
  };

  const remove = async () => {
    setDeleting(true);
    try {
      await knowledgeBaseApi.remove(kb.id);
      toast.success(t("knowledge.toast.deleted", "知识库已删除"));
      navigate("/knowledge", { replace: true });
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
      setDeleting(false);
    }
  };

  const infoCls =
    "flex items-start gap-2 rounded-xl bg-sky-50 px-3 py-2 text-sm text-sky-800 dark:bg-sky-900/30 dark:text-sky-200";

  return (
    <div className="mx-auto max-w-2xl space-y-6 px-4 py-6 sm:px-6">
      <section className="space-y-4 rounded-2xl border border-stone-200 bg-white p-5 dark:border-stone-700 dark:bg-stone-900">
        <h3 className="text-sm font-semibold text-stone-900 dark:text-stone-100">
          {t("knowledge.settings.basic", "基本信息")}
        </h3>
        <KnowledgeBaseFormFields form={form} idPrefix="kb-settings" />

        {form.chunkChanged && (
          <div className={infoCls}>
            <Info size={16} className="mt-0.5 shrink-0" />
            {t(
              "knowledge.settings.chunkNote",
              "分块设置的修改仅对之后新入库的文档生效。保存后可以点击“重建索引”，把新设置应用到全部已有文档。",
            )}
          </div>
        )}

        <div className="flex justify-end">
          <button
            type="button"
            onClick={() => void save()}
            disabled={!form.dirty || !form.valid || saving}
            className={PRIMARY_BTN}
          >
            {saving ? t("common.saving", "保存中...") : t("common.save", "保存")}
          </button>
        </div>

        {showRebuildHint && !form.chunkChanged && (
          <div className={`${infoCls} items-center justify-between`}>
            <span className="flex items-start gap-2">
              <Info size={16} className="mt-0.5 shrink-0" />
              {t(
                "knowledge.settings.rebuildHint",
                "新的分块设置只对新入库的文档生效。要应用到已有文档，请重建索引。",
              )}
            </span>
            <button
              type="button"
              onClick={() => void reindex()}
              disabled={reindexBusy || hasActiveTask(kb.active_task)}
              className={`${OUTLINE_BTN} shrink-0`}
            >
              {reindexBusy ? (
                <Loader2 size={14} className="animate-spin" />
              ) : (
                <RefreshCw size={14} />
              )}
              {t("knowledge.outdated.reindex", "重建索引")}
            </button>
          </div>
        )}
      </section>

      <section className="rounded-2xl border border-red-200 p-5 dark:border-red-900/60">
        <h3 className="text-sm font-semibold text-red-700 dark:text-red-400">
          {t("knowledge.settings.danger", "危险操作")}
        </h3>
        <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
          {t(
            "knowledge.settings.deleteDesc",
            "删除知识库会同时删除库内所有文档、分块与检索索引，且无法恢复。",
          )}
        </p>
        <button
          type="button"
          onClick={() => setConfirmDelete(true)}
          className={`${DANGER_BTN} mt-3`}
        >
          <Trash2 size={14} />
          {t("knowledge.settings.deleteButton", "删除知识库")}
        </button>
      </section>

      <ConfirmDialog
        isOpen={confirmDelete}
        title={t("knowledge.deleteTitle", "删除知识库")}
        message={t("knowledge.deleteConfirm", {
          name: kb.name,
          defaultValue:
            "确定删除“{{name}}”吗？库内所有文档、分块和检索索引都会被永久删除，此操作无法撤销。",
        })}
        confirmText={t("common.delete", "删除")}
        variant="danger"
        loading={deleting}
        onConfirm={() => void remove()}
        onCancel={() => setConfirmDelete(false)}
      />
    </div>
  );
}
