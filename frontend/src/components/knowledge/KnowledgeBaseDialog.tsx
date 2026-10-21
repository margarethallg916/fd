import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import { Check, ChevronRight, X } from "lucide-react";
import {
  knowledgeBaseApi,
  type KBChunkPreset,
  type KBConfig,
  type KnowledgeBase,
} from "../../services/api/knowledgeBase";
import {
  CHUNK_PRESET_KEYS,
  CHUNK_TOKEN_DEFAULT,
  CHUNK_TOKEN_MAX,
  CHUNK_TOKEN_MIN,
  OVERLAP_MAX,
  OVERLAP_MIN,
  getDefaultChunkSettings,
  parseBoundedInt,
} from "./knowledgeUtils";
import { GHOST_BTN, INPUT_CLS, PRIMARY_BTN } from "./knowledgeStyles";

const PRESET_DEFAULTS: Record<KBChunkPreset, { name: string; desc: string }> = {
  general: { name: "通用", desc: "适合大多数文档，按段落与句子智能切分。" },
  qa: { name: "问答", desc: "适合问答对、FAQ，按问题与答案成对切分。" },
  book: { name: "书籍", desc: "适合长篇书籍与报告，按章节层级切分。" },
  laws: { name: "法规", desc: "适合制度、法规条文，按条款层级切分。" },
};

interface KnowledgeBaseDialogProps {
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
  const initialPreset: KBChunkPreset = kb?.chunk_preset ?? "general";
  const initialChunk = kb
    ? { chunk_token_num: kb.chunk_token_num, overlap_percent: kb.overlap_percent }
    : getDefaultChunkSettings(config, initialPreset);

  const [name, setName] = useState(kb?.name ?? "");
  const [description, setDescription] = useState(kb?.description ?? "");
  const [preset, setPreset] = useState<KBChunkPreset>(initialPreset);
  const [tokenNum, setTokenNum] = useState(
    String(initialChunk.chunk_token_num || CHUNK_TOKEN_DEFAULT),
  );
  const [overlap, setOverlap] = useState(String(initialChunk.overlap_percent));
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [advancedTouched, setAdvancedTouched] = useState(isEdit);
  const [saving, setSaving] = useState(false);
  const nameRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    nameRef.current?.focus();
  }, []);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && !saving && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, saving]);

  const tokenValue = parseBoundedInt(tokenNum, CHUNK_TOKEN_MIN, CHUNK_TOKEN_MAX);
  const overlapValue = parseBoundedInt(overlap, OVERLAP_MIN, OVERLAP_MAX);
  const trimmedName = name.trim();
  const canSave =
    trimmedName.length > 0 && tokenValue !== null && overlapValue !== null && !saving;

  const selectPreset = (key: KBChunkPreset) => {
    setPreset(key);
    if (!advancedTouched) {
      const defaults = getDefaultChunkSettings(config, key);
      setTokenNum(String(defaults.chunk_token_num));
      setOverlap(String(defaults.overlap_percent));
    }
  };

  const save = async () => {
    if (!canSave || tokenValue === null || overlapValue === null) return;
    setSaving(true);
    try {
      const body = {
        name: trimmedName,
        description: description.trim(),
        chunk_preset: preset,
        chunk_token_num: tokenValue,
        overlap_percent: overlapValue,
      };
      const saved = kb
        ? await knowledgeBaseApi.update(kb.id, body)
        : await knowledgeBaseApi.create(body);
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

  const labelCls = "mb-1.5 block text-sm font-medium text-stone-900 dark:text-stone-100";

  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-0 sm:items-center sm:p-4"
      onClick={() => !saving && onClose()}
    >
      <div
        role="dialog"
        aria-modal="true"
        aria-label={
          isEdit
            ? t("knowledge.dialog.editTitle", "编辑知识库")
            : t("knowledge.dialog.createTitle", "新建知识库")
        }
        className="flex max-h-[92vh] w-full flex-col overflow-hidden rounded-t-3xl bg-white shadow-2xl dark:bg-stone-900 sm:max-w-xl sm:rounded-3xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-stone-100 px-5 py-4 dark:border-stone-800">
          <h2 className="text-base font-semibold text-stone-900 dark:text-stone-100">
            {isEdit
              ? t("knowledge.dialog.editTitle", "编辑知识库")
              : t("knowledge.dialog.createTitle", "新建知识库")}
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

        <div className="flex-1 space-y-5 overflow-y-auto px-5 py-5">
          <div>
            <label className={labelCls} htmlFor="kb-dialog-name">
              {t("knowledge.dialog.name", "名称")}
              <span className="ml-0.5 text-red-500">*</span>
            </label>
            <input
              id="kb-dialog-name"
              ref={nameRef}
              value={name}
              onChange={(e) => setName(e.target.value)}
              maxLength={64}
              placeholder={t("knowledge.dialog.namePlaceholder", "例如：产品手册、项目资料")}
              className={INPUT_CLS}
            />
          </div>

          <div>
            <label className={labelCls} htmlFor="kb-dialog-desc">
              {t("knowledge.dialog.description", "描述")}
            </label>
            <textarea
              id="kb-dialog-desc"
              value={description}
              onChange={(e) => setDescription(e.target.value)}
              maxLength={500}
              rows={3}
              placeholder={t(
                "knowledge.dialog.descriptionPlaceholder",
                "简单介绍这个知识库里放了什么（可选）",
              )}
              className={`${INPUT_CLS} resize-y leading-relaxed`}
            />
          </div>

          <div>
            <div className={labelCls}>{t("knowledge.dialog.preset", "分块方式")}</div>
            <div role="radiogroup" className="grid gap-2 sm:grid-cols-2">
              {CHUNK_PRESET_KEYS.map((key) => {
                const selected = preset === key;
                return (
                  <button
                    key={key}
                    type="button"
                    role="radio"
                    aria-checked={selected}
                    onClick={() => selectPreset(key)}
                    className={`relative rounded-2xl border p-3 text-left transition ${
                      selected
                        ? "border-stone-900 bg-stone-50 dark:border-stone-100 dark:bg-stone-800"
                        : "border-stone-200 hover:border-stone-400 dark:border-stone-700 dark:hover:border-stone-500"
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2">
                      <span className="text-sm font-medium text-stone-900 dark:text-stone-100">
                        {t(`knowledge.preset.${key}.name`, PRESET_DEFAULTS[key].name)}
                      </span>
                      {selected && (
                        <Check size={14} className="shrink-0 text-stone-900 dark:text-stone-100" />
                      )}
                    </div>
                    <div className="mt-1 text-xs leading-relaxed text-stone-500 dark:text-stone-400">
                      {t(`knowledge.preset.${key}.desc`, PRESET_DEFAULTS[key].desc)}
                    </div>
                  </button>
                );
              })}
            </div>
          </div>

          <div>
            <button
              type="button"
              onClick={() => setAdvancedOpen((v) => !v)}
              aria-expanded={advancedOpen}
              className="inline-flex items-center gap-1 text-sm font-medium text-stone-600 hover:text-stone-900 dark:text-stone-300 dark:hover:text-stone-100"
            >
              <ChevronRight
                size={14}
                className={`transition-transform ${advancedOpen ? "rotate-90" : ""}`}
              />
              {t("knowledge.dialog.advanced", "高级")}
            </button>
            {advancedOpen && (
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <div>
                  <label className={labelCls} htmlFor="kb-dialog-tokens">
                    {t("knowledge.dialog.chunkTokens", "块大小（tokens）")}
                  </label>
                  <input
                    id="kb-dialog-tokens"
                    inputMode="numeric"
                    value={tokenNum}
                    onChange={(e) => {
                      setTokenNum(e.target.value);
                      setAdvancedTouched(true);
                    }}
                    aria-invalid={tokenValue === null}
                    className={`${INPUT_CLS} ${tokenValue === null ? "border-red-400 dark:border-red-500" : ""}`}
                  />
                  <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
                    {t("knowledge.dialog.range", {
                      min: CHUNK_TOKEN_MIN,
                      max: CHUNK_TOKEN_MAX,
                      defaultValue: "范围 {{min}} – {{max}}",
                    })}
                  </p>
                </div>
                <div>
                  <label className={labelCls} htmlFor="kb-dialog-overlap">
                    {t("knowledge.dialog.overlap", "重叠比例（%）")}
                  </label>
                  <input
                    id="kb-dialog-overlap"
                    inputMode="numeric"
                    value={overlap}
                    onChange={(e) => {
                      setOverlap(e.target.value);
                      setAdvancedTouched(true);
                    }}
                    aria-invalid={overlapValue === null}
                    className={`${INPUT_CLS} ${overlapValue === null ? "border-red-400 dark:border-red-500" : ""}`}
                  />
                  <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
                    {t("knowledge.dialog.range", {
                      min: OVERLAP_MIN,
                      max: OVERLAP_MAX,
                      defaultValue: "范围 {{min}} – {{max}}",
                    })}
                  </p>
                </div>
              </div>
            )}
          </div>
        </div>

        <div className="flex justify-end gap-2 border-t border-stone-100 px-5 py-4 dark:border-stone-800">
          <button type="button" onClick={onClose} disabled={saving} className={GHOST_BTN}>
            {t("common.cancel", "取消")}
          </button>
          <button type="button" onClick={save} disabled={!canSave} className={PRIMARY_BTN}>
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
