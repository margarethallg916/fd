import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import { X } from "lucide-react";
import {
  knowledgeBaseApi,
  type KBConfig,
  type KnowledgeBase,
} from "../../services/api/knowledgeBase";
import { useKbForm } from "./hooks/useKbForm";
import { KnowledgeBaseFormFields } from "./KnowledgeBaseFormFields";
import { GHOST_BTN, PRIMARY_BTN } from "./knowledgeStyles";

interface KnowledgeBaseDialogProps {
  /** null = create. */
  kb: KnowledgeBase | null;
  config: KBConfig | null;
  onClose: () => void;
  onSaved: (kb: KnowledgeBase) => void;
}

export function KnowledgeBaseDialog({
  kb,
  config,
  onClose,
  onSaved,
}: KnowledgeBaseDialogProps) {
  const { t } = useTranslation();
  const isEdit = !!kb;
  const form = useKbForm(kb, config);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !saving && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, saving]);

  const title = isEdit
    ? t("knowledge.dialog.editTitle", "编辑知识库")
    : t("knowledge.dialog.createTitle", "新建知识库");

  const save = async () => {
    if (!form.payload || saving) return;
    setSaving(true);
    try {
      const saved = kb
        ? await knowledgeBaseApi.update(kb.id, form.payload)
        : await knowledgeBaseApi.create(form.payload);
      toast.success(
        isEdit
          ? t("knowledge.toast.updated", "已保存修改")
          : t("knowledge.toast.created", "知识库已创建，现在可以上传文档了"),
      );
      onSaved(saved);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-0 sm:items-center sm:p-4"
      onClick={() => !saving && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={title}
        className="flex max-h-[92vh] w-full flex-col overflow-hidden rounded-t-3xl bg-white shadow-2xl dark:bg-stone-900 sm:max-w-xl sm:rounded-3xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-stone-100 px-5 py-4 dark:border-stone-800">
          <h2 className="text-base font-semibold text-stone-900 dark:text-stone-100">
            {title}
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

        <div className="flex-1 overflow-y-auto px-5 py-5">
          <KnowledgeBaseFormFields form={form} idPrefix="kb-dialog" autoFocusName />
        </div>

        <div className="flex justify-end gap-2 border-t border-stone-100 px-5 py-4 dark:border-stone-800">
          <button type="button" onClick={onClose} disabled={saving} className={GHOST_BTN}>
            {t("common.cancel", "取消")}
          </button>
          <button
            type="button"
            onClick={save}
            disabled={!form.valid || saving}
            className={PRIMARY_BTN}
          >
            {saving
              ? t("common.saving", "保存中...")
              : isEdit
                ? t("common.save", "保存")
                : t("knowledge.dialog.createSubmit", "创建知识库")}
          </button>
        </div>
      </div>
    </div>
  );
}
