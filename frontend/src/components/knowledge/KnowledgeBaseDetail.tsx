import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";
import toast from "react-hot-toast";
import {
  ArrowLeft,
  FileText,
  LibraryBig,
  Search,
  Settings2,
} from "lucide-react";
import { PanelHeader } from "../common/PanelHeader";
import { PanelLoadingState } from "../common/PanelLoadingState";
import { formatFileSize } from "../documents/utils/fileSize";
import { useWebSocket, type KbEventNotification } from "../../hooks/useWebSocket";
import {
  knowledgeBaseApi,
  type KBDocument,
  type KBSearchHit,
} from "../../services/api/knowledgeBase";
import { subscribeKnowledgeBaseRefresh } from "../../utils/knowledgeBaseEvents";
import {
  DocumentDetailSidebar,
  type DocumentFocus,
  type DocumentSidebarTab,
} from "./DocumentDetailSidebar";
import { DocumentsTab } from "./DocumentsTab";
import { useKbConfig } from "./hooks/useKbConfig";
import { useKbDetail } from "./hooks/useKbDetail";
import { KnowledgeDisabledState, KnowledgeNotFoundState, KnowledgeNotReadyNotice } from "./KnowledgeNotices";
import { RetrievalTestTab } from "./RetrievalTestTab";
import { SettingsTab } from "./SettingsTab";
import { OUTLINE_BTN, PRIMARY_BTN } from "./knowledgeStyles";
import {
  POST_ACTION_REFRESH_MS,
  resolveKbDetailTab,
  type KbDetailTab,
} from "./knowledgeUtils";

interface ViewingState {
  doc: KBDocument;
  focus: DocumentFocus | null;
  tab: DocumentSidebarTab;
}

interface KnowledgeBaseDetailProps {
  kbId: string;
}

export function KnowledgeBaseDetail({ kbId }: KnowledgeBaseDetailProps) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const tab = resolveKbDetailTab(searchParams.get("tab"));
  const detail = useKbDetail(kbId);
  const { config } = useKbConfig();
  const { kb, setKb, loading, loadError, scheduleRefresh, refresh } = detail;
  const [viewing, setViewing] = useState<ViewingState | null>(null);

  const onKbEvent = useCallback(
    (event: KbEventNotification) => {
      const eventKbId = event.data?.kb_id;
      if (!eventKbId || eventKbId === kbId) scheduleRefresh(300);
    },
    [kbId, scheduleRefresh],
  );
  useWebSocket({ enabled: true, onKbEvent });

  useEffect(
    () => subscribeKnowledgeBaseRefresh(() => scheduleRefresh(0), { kbId }),
    [kbId, scheduleRefresh],
  );

  const changeTab = useCallback(
    (next: KbDetailTab) => {
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams(prev);
          if (next === "documents") params.delete("tab");
          else params.set("tab", next);
          return params;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  const openHit = useCallback(
    async (hit: KBSearchHit) => {
      try {
        const doc = await knowledgeBaseApi.getDocument(kbId, hit.doc_id);
        setViewing({
          doc,
          focus: { startLine: hit.start_line, endLine: hit.end_line },
          tab: "content",
        });
      } catch (e) {
        toast.error(e instanceof Error ? e.message : String(e));
      }
    },
    [kbId],
  );

  if (loadError === "disabled") return <KnowledgeDisabledState />;
  if (loadError === "notFound") {
    return <KnowledgeNotFoundState onBack={() => navigate("/knowledge")} />;
  }
  if (!kb) {
    return loading ? (
      <PanelLoadingState text={t("common.loading", "加载中...")} />
    ) : (
      <div className="flex h-full items-center justify-center px-6">
        <div className="text-center">
          <p className="text-sm text-stone-500 dark:text-stone-400">
            {t("knowledge.loadFailed", "知识库加载失败")}
          </p>
          <button type="button" onClick={() => void refresh()} className={`${PRIMARY_BTN} mt-3`}>
            {t("knowledge.retryLoad", "重新加载")}
          </button>
        </div>
      </div>
    );
  }

  const { stats } = kb;
  const chips: Array<{ key: string; text: string; danger?: boolean }> = [
    {
      key: "docs",
      text: t("knowledge.stats.docs", { count: stats.doc_count, defaultValue: "{{count}} 个文档" }),
    },
    {
      key: "indexed",
      text: t("knowledge.stats.indexed", { count: stats.indexed_count, defaultValue: "{{count}} 已完成" }),
    },
  ];
  if (stats.processing_count > 0) {
    chips.push({
      key: "processing",
      text: t("knowledge.stats.processing", {
        count: stats.processing_count,
        defaultValue: "{{count}} 处理中",
      }),
    });
  }
  if (stats.failed_count > 0) {
    chips.push({
      key: "failed",
      danger: true,
      text: t("knowledge.stats.failed", { count: stats.failed_count, defaultValue: "{{count}} 失败" }),
    });
  }
  chips.push(
    {
      key: "chunks",
      text: t("knowledge.stats.chunks", { count: stats.chunk_count, defaultValue: "{{count}} 个分块" }),
    },
    { key: "size", text: formatFileSize(stats.size_bytes) },
  );

  const tabEntries: Array<{ key: KbDetailTab; label: string; icon: typeof FileText }> = [
    { key: "documents", label: t("knowledge.tabs.documents", "文档"), icon: FileText },
    { key: "search", label: t("knowledge.tabs.search", "检索测试"), icon: Search },
    { key: "settings", label: t("knowledge.tabs.settings", "设置"), icon: Settings2 },
  ];

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <div className="shrink-0 px-4 pt-3 sm:px-6 lg:px-8">
        <button
          type="button"
          onClick={() => navigate("/knowledge")}
          className="inline-flex items-center gap-1 rounded-lg py-1 pr-2 text-sm text-[var(--theme-text-secondary)] hover:text-[var(--theme-text)]"
        >
          <ArrowLeft size={16} />
          {t("knowledge.backToList", "返回知识库列表")}
        </button>
      </div>

      <PanelHeader
        title={kb.name}
        subtitle={kb.description || undefined}
        icon={<LibraryBig />}
        actions={
          <button
            type="button"
            onClick={() => changeTab("settings")}
            className={OUTLINE_BTN}
          >
            <Settings2 size={16} />
            {t("knowledge.tabs.settings", "设置")}
          </button>
        }
      >
        <div className="mt-3 flex flex-wrap items-center gap-1.5">
          {chips.map((chip) => (
            <span
              key={chip.key}
              className={`rounded-full px-2.5 py-0.5 text-[11px] font-medium ${
                chip.danger
                  ? "bg-red-50 text-red-700 dark:bg-red-900/40 dark:text-red-300"
                  : "bg-[var(--theme-bg-subtle)] text-[var(--theme-text-secondary)]"
              }`}
            >
              {chip.text}
            </span>
          ))}
        </div>
      </PanelHeader>

      {config && !config.ready && <KnowledgeNotReadyNotice className="mx-4 mt-3 sm:mx-6" />}

      <div className="flex shrink-0 items-center border-b border-[var(--theme-border)] px-4 py-2">
        <div
          className="inline-flex items-center gap-1 rounded-lg bg-[var(--theme-bg-subtle)] p-1"
          role="tablist"
          aria-label={kb.name}
        >
          {tabEntries.map(({ key, label, icon: Icon }) => {
            const isActive = tab === key;
            return (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => changeTab(key)}
                className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
                  isActive
                    ? "is-active bg-[var(--theme-sidebar-active)] text-[var(--theme-text)]"
                    : "text-[var(--theme-text-secondary)] hover:bg-[var(--theme-sidebar-hover)] hover:text-[var(--theme-text)]"
                }`}
              >
                <Icon size={16} />
                <span>{label}</span>
              </button>
            );
          })}
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
        {tab === "documents" && (
          <DocumentsTab
            kbId={kbId}
            detail={detail}
            config={config}
            onOpenDocument={(doc, docTab = "content") =>
              setViewing({ doc, focus: null, tab: docTab })
            }
          />
        )}
        {tab === "search" && (
          <RetrievalTestTab kb={kb} config={config} onOpenHit={(hit) => void openHit(hit)} />
        )}
        {tab === "settings" && (
          <SettingsTab
            kb={kb}
            config={config}
            onSaved={setKb}
            onReindexQueued={() => scheduleRefresh(POST_ACTION_REFRESH_MS)}
          />
        )}
      </div>

      {viewing && (
        <DocumentDetailSidebar
          key={`${viewing.doc.id}:${viewing.focus?.startLine ?? 0}`}
          kbId={kbId}
          doc={viewing.doc}
          focus={viewing.focus}
          initialTab={viewing.tab}
          onClose={() => setViewing(null)}
        />
      )}
    </div>
  );
}
