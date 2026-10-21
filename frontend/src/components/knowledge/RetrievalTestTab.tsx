import { useState } from "react";
import { useTranslation } from "react-i18next";
import toast from "react-hot-toast";
import { AlertTriangle, Loader2, Search } from "lucide-react";
import { GlassSelect } from "../common/GlassSelect";
import {
  knowledgeBaseApi,
  type KBConfig,
  type KBSearchHit,
  type KBSearchMode,
  type KBSearchResponse,
  type KnowledgeBase,
} from "../../services/api/knowledgeBase";
import {
  SEARCH_TOP_K_DEFAULT,
  SEARCH_TOP_K_MAX,
  formatLineRange,
  formatScorePercent,
} from "./knowledgeUtils";
import { INPUT_CLS, NOTICE_WARNING, PRIMARY_BTN } from "./knowledgeStyles";

interface RetrievalTestTabProps {
  kb: KnowledgeBase;
  config: KBConfig | null;
  onOpenHit: (hit: KBSearchHit) => void;
}

const MODES: KBSearchMode[] = ["hybrid", "vector", "keyword"];
const MODE_DEFAULT_LABELS: Record<KBSearchMode, string> = {
  hybrid: "混合检索",
  vector: "语义检索",
  keyword: "关键词检索",
};

export function RetrievalTestTab({ kb, config, onOpenHit }: RetrievalTestTabProps) {
  const { t } = useTranslation();
  const rerankAvailable = config?.rerank_available ?? false;
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<KBSearchMode>("hybrid");
  const [topK, setTopK] = useState(String(SEARCH_TOP_K_DEFAULT));
  const [threshold, setThreshold] = useState("0");
  const [useRerank, setUseRerank] = useState(true);
  const [loading, setLoading] = useState(false);
  const [response, setResponse] = useState<KBSearchResponse | null>(null);

  const topKValue = Math.max(1, Math.min(SEARCH_TOP_K_MAX, Math.round(Number(topK)) || SEARCH_TOP_K_DEFAULT));
  const thresholdNumber = Number(threshold);
  const thresholdValue = Number.isFinite(thresholdNumber)
    ? Math.max(0, Math.min(1, thresholdNumber))
    : 0;

  const run = async () => {
    const q = query.trim();
    if (!q || loading) return;
    setLoading(true);
    try {
      const res = await knowledgeBaseApi.search(kb.id, {
        query: q,
        top_k: topKValue,
        mode,
        use_rerank: rerankAvailable ? useRerank : false,
        score_threshold: thresholdValue,
      });
      setResponse(res);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  };

  const modeOptions = MODES.map((m) => ({
    value: m,
    label: t(`knowledge.search.mode.${m}`, MODE_DEFAULT_LABELS[m]),
  }));
  const labelCls = "mb-1 block text-xs font-medium text-stone-500 dark:text-stone-400";
  const warnings = response?.warnings ?? [];

  return (
    <div className="mx-auto max-w-4xl px-4 py-4 sm:px-6">
      <div className="rounded-2xl border border-stone-200 bg-white p-4 dark:border-stone-700 dark:bg-stone-900">
        <div className="flex gap-2">
          <input
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void run();
              }
            }}
            placeholder={t("knowledge.search.placeholder", "输入问题或关键词，按回车检索")}
            aria-label={t("knowledge.search.placeholder", "输入问题或关键词，按回车检索")}
            className={INPUT_CLS}
          />
          <button
            type="button"
            onClick={() => void run()}
            disabled={loading || !query.trim()}
            className={`${PRIMARY_BTN} shrink-0`}
          >
            {loading ? <Loader2 size={16} className="animate-spin" /> : <Search size={16} />}
            {t("knowledge.search.button", "检索")}
          </button>
        </div>

        <div className="mt-3 grid grid-cols-2 items-end gap-3 sm:grid-cols-4">
          <div>
            <label className={labelCls}>{t("knowledge.search.modeLabel", "检索方式")}</label>
            <GlassSelect
              value={mode}
              onChange={(v) => setMode(v as KBSearchMode)}
              options={modeOptions}
            />
          </div>
          <div>
            <label className={labelCls} htmlFor="kb-search-topk">
              {t("knowledge.search.topK", "返回数量")}
            </label>
            <input
              id="kb-search-topk"
              type="number"
              min={1}
              max={SEARCH_TOP_K_MAX}
              value={topK}
              onChange={(e) => setTopK(e.target.value)}
              className={INPUT_CLS}
            />
          </div>
          <div>
            <label className={labelCls} htmlFor="kb-search-threshold">
              {t("knowledge.search.threshold", "相似度阈值")}
            </label>
            <input
              id="kb-search-threshold"
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={threshold}
              onChange={(e) => setThreshold(e.target.value)}
              className={INPUT_CLS}
            />
          </div>
          <label
            className={`flex h-[42px] items-center gap-2 text-sm ${
              rerankAvailable
                ? "cursor-pointer text-stone-700 dark:text-stone-200"
                : "cursor-not-allowed text-stone-400 dark:text-stone-500"
            }`}
            title={
              rerankAvailable
                ? undefined
                : t("knowledge.search.rerankUnavailable", "管理员尚未配置重排序模型")
            }
          >
            <input
              type="checkbox"
              checked={rerankAvailable && useRerank}
              disabled={!rerankAvailable}
              onChange={(e) => setUseRerank(e.target.checked)}
              className="h-4 w-4 accent-stone-900 dark:accent-stone-100"
            />
            {t("knowledge.search.rerank", "重排序")}
          </label>
        </div>
      </div>

      {kb.stats.indexed_count === 0 && (
        <div className={`${NOTICE_WARNING} mt-3`}>
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          {t("knowledge.search.noIndexed", "这个知识库还没有处理完成的文档，检索不会有结果。")}
        </div>
      )}

      {warnings.map((warning) => (
        <div key={warning} role="alert" className={`${NOTICE_WARNING} mt-3`}>
          <AlertTriangle size={16} className="mt-0.5 shrink-0" />
          {warning === "index_outdated"
            ? t(
                "knowledge.search.indexOutdated",
                "索引与当前向量模型不一致，本次已退化为关键词检索。请在文档页重建索引。",
              )
            : warning}
        </div>
      ))}

      <div className="mt-4">
        {!response ? (
          <div className="rounded-3xl border border-dashed border-stone-300 p-8 text-center text-sm text-stone-500 dark:border-stone-700 dark:text-stone-400">
            {t("knowledge.search.initialHint", "输入一个问题试试，看看知识库能召回哪些内容。")}
          </div>
        ) : response.results.length === 0 ? (
          <div className="rounded-3xl border border-stone-200 p-8 text-center dark:border-stone-800">
            <div className="text-base font-semibold text-stone-900 dark:text-stone-100">
              {t("knowledge.search.noResults", "没有找到相关内容")}
            </div>
            <div className="mt-1 text-sm text-stone-500 dark:text-stone-400">
              {t("knowledge.search.noResultsHint", "可以降低相似度阈值，或换一种检索方式。")}
            </div>
          </div>
        ) : (
          <>
            <div className="mb-2 text-xs text-stone-500 dark:text-stone-400">
              {t("knowledge.search.summary", {
                count: response.results.length,
                ms: response.took_ms,
                defaultValue: "找到 {{count}} 条结果，用时 {{ms}} ms",
              })}
            </div>
            <ul className="space-y-3">
              {response.results.map((hit, idx) => {
                const primary = hit.rerank_score ?? hit.score;
                return (
                  <li key={hit.chunk_id}>
                    <button
                      type="button"
                      onClick={() => onOpenHit(hit)}
                      className="block w-full rounded-2xl border border-stone-200 bg-white p-4 text-left transition hover:border-stone-400 hover:shadow-sm dark:border-stone-700 dark:bg-stone-900 dark:hover:border-stone-500"
                    >
                      <div className="flex items-center gap-2">
                        <span className="flex h-5 min-w-5 items-center justify-center rounded-md bg-stone-100 px-1 text-[11px] font-semibold text-stone-700 dark:bg-stone-800 dark:text-stone-200">
                          {idx + 1}
                        </span>
                        <span
                          className="min-w-0 flex-1 truncate text-sm font-medium text-stone-900 dark:text-stone-100"
                          title={hit.filename}
                        >
                          {hit.filename}
                        </span>
                        <span className="shrink-0 text-[11px] text-stone-500 dark:text-stone-400">
                          {t("knowledge.doc.lines", {
                            range: formatLineRange(hit.start_line, hit.end_line),
                            defaultValue: "第 {{range}} 行",
                          })}
                        </span>
                        <span
                          className="shrink-0 rounded-full bg-emerald-50 px-2 py-0.5 text-[11px] font-semibold text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
                          title={
                            hit.rerank_score !== null && hit.rerank_score !== undefined
                              ? t("knowledge.search.scoreBoth", {
                                  score: formatScorePercent(hit.score),
                                  rerank: formatScorePercent(hit.rerank_score),
                                  defaultValue: "检索得分 {{score}} · 重排得分 {{rerank}}",
                                })
                              : undefined
                          }
                        >
                          {formatScorePercent(primary)}
                        </span>
                      </div>
                      <p className="mt-2 line-clamp-6 whitespace-pre-wrap break-words text-[13px] leading-relaxed text-stone-700 dark:text-stone-300">
                        {hit.text}
                      </p>
                    </button>
                  </li>
                );
              })}
            </ul>
          </>
        )}
      </div>
    </div>
  );
}
