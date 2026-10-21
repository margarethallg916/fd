import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Check, ChevronRight } from "lucide-react";
import type { KbFormState } from "./hooks/useKbForm";
import {
  CHUNK_PRESET_KEYS,
  CHUNK_TOKEN_MAX,
  CHUNK_TOKEN_MIN,
  OVERLAP_MAX,
  OVERLAP_MIN,
} from "./knowledgeUtils";
import { INPUT_CLS } from "./knowledgeStyles";

const PRESET_DEFAULTS = {
  general: { name: "通用", desc: "适合大多数文档，按段落与句子智能切分。" },
  qa: { name: "问答", desc: "适合问答对、FAQ，按问题与答案成对切分。" },
  book: { name: "书籍", desc: "适合长篇书籍与报告，按章节层级切分。" },
  laws: { name: "法规", desc: "适合制度、法规条文，按条款层级切分。" },
} as const;

interface KnowledgeBaseFormFieldsProps {
  form: KbFormState;
  idPrefix: string;
  autoFocusName?: boolean;
}

export function KnowledgeBaseFormFields({
  form,
  idPrefix,
  autoFocusName,
}: KnowledgeBaseFormFieldsProps) {
  const { t } = useTranslation();
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const labelCls = "mb-1.5 block text-sm font-medium text-stone-900 dark:text-stone-100";
  const rangeText = (min: number, max: number) =>
    t("knowledge.dialog.range", {
      min,
      max,
      defaultValue: "范围 {{min}} – {{max}}",
    });

  return (
    <div className="space-y-5">
      <div>
        <label className={labelCls} htmlFor={`${idPrefix}-name`}>
          {t("knowledge.dialog.name", "名称")}
          <span className="ml-0.5 text-red-500">*</span>
        </label>
        <input
          id={`${idPrefix}-name`}
          // eslint-disable-next-line jsx-a11y/no-autofocus
          autoFocus={autoFocusName}
          value={form.name}
          onChange={(e) => form.setName(e.target.value)}
          maxLength={64}
          placeholder={t("knowledge.dialog.namePlaceholder", "例如：产品手册、项目资料")}
          className={INPUT_CLS}
        />
      </div>

      <div>
        <label className={labelCls} htmlFor={`${idPrefix}-desc`}>
          {t("knowledge.dialog.description", "描述")}
        </label>
        <textarea
          id={`${idPrefix}-desc`}
          value={form.description}
          onChange={(e) => form.setDescription(e.target.value)}
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
            const selected = form.preset === key;
            return (
              <button
                key={key}
                type="button"
                role="radio"
                aria-checked={selected}
                onClick={() => form.selectPreset(key)}
                className={`rounded-2xl border p-3 text-left transition ${
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
              <label className={labelCls} htmlFor={`${idPrefix}-tokens`}>
                {t("knowledge.dialog.chunkTokens", "块大小（tokens）")}
              </label>
              <input
                id={`${idPrefix}-tokens`}
                inputMode="numeric"
                value={form.tokenNum}
                onChange={(e) => form.setTokenNum(e.target.value)}
                aria-invalid={!form.tokenValid}
                className={`${INPUT_CLS} ${form.tokenValid ? "" : "border-red-400 dark:border-red-500"}`}
              />
              <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
                {rangeText(CHUNK_TOKEN_MIN, CHUNK_TOKEN_MAX)}
              </p>
            </div>
            <div>
              <label className={labelCls} htmlFor={`${idPrefix}-overlap`}>
                {t("knowledge.dialog.overlap", "重叠比例（%）")}
              </label>
              <input
                id={`${idPrefix}-overlap`}
                inputMode="numeric"
                value={form.overlap}
                onChange={(e) => form.setOverlap(e.target.value)}
                aria-invalid={!form.overlapValid}
                className={`${INPUT_CLS} ${form.overlapValid ? "" : "border-red-400 dark:border-red-500"}`}
              />
              <p className="mt-1 text-xs text-stone-500 dark:text-stone-400">
                {rangeText(OVERLAP_MIN, OVERLAP_MAX)}
              </p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
