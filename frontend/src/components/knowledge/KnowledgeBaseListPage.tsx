import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import toast from "react-hot-toast";
import { LibraryBig, Plus } from "lucide-react";
import { PanelHeader } from "../common/PanelHeader";
import { PanelLoadingState } from "../common/PanelLoadingState";
import { ConfirmDialog } from "../common/ConfirmDialog";
import { knowledgeBaseApi, type KnowledgeBase } from "../../services/api/knowledgeBase";
import { useKbConfig } from "./hooks/useKbConfig";
import { useKnowledgeBases } from "./hooks/useKnowledgeBases";
import { KnowledgeBaseCard } from "./KnowledgeBaseCard";
import { KnowledgeBaseDialog } from "./KnowledgeBaseDialog";
import { KnowledgeDisabledState, KnowledgeNotReadyNotice } from "./KnowledgeNotices";
import { PRIMARY_BTN } from "./knowledgeStyles";

type ListTabKey = "mine";

interface ListTabEntry {
  key: ListTabKey;
  label: string;
  icon: typeof LibraryBig;
}

export function KnowledgeBaseListPage() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { config, error: configError } = useKbConfig();
  const { items, setItems, loading, error, reload } = useKnowledgeBases();
  const [search, setSearch] = useState("");
  const [activeTab] = useState<ListTabKey>("mine");
  const [dialog, setDialog] = useState<{ kb: KnowledgeBase | null } | null>(null);
  const [deleting, setDeleting] = useState<KnowledgeBase | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);

  // Shared / plaza tabs are added here when those scopes ship.
  const tabEntries: ListTabEntry[] = [
    { key: "mine", label: t("knowledge.tabs.mine", "我的知识库"), icon: LibraryBig },
  ];

  const filtered = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return items;
    return items.filter(
      (kb) =>
        kb.name.toLowerCase().includes(q) || kb.description.toLowerCase().includes(q),
    );
  }, [items, search]);

  const isDisabled =
    config?.enabled === false ||
    (error as { code?: string } | null)?.code === "kb_disabled" ||
    (configError as { code?: string } | null)?.code === "kb_disabled";
  if (isDisabled) return <KnowledgeDisabledState />;

  const confirmDelete = async () => {
    if (!deleting) return;
    setDeleteBusy(true);
    try {
      await knowledgeBaseApi.remove(deleting.id);
      setItems((prev) => prev.filter((kb) => kb.id !== deleting.id));
      toast.success(t("knowledge.toast.deleted", "知识库已删除"));
      setDeleting(null);
    } catch (e) {
      toast.error(e instanceof Error ? e.message : String(e));
    } finally {
      setDeleteBusy(false);
    }
  };

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <PanelHeader
        title={t("knowledge.title", "知识库")}
        subtitle={t("knowledge.subtitle", "把文档整理成知识库，让昆小智基于你的资料回答问题")}
        icon={<LibraryBig />}
        searchValue={search}
        onSearchChange={setSearch}
        searchPlaceholder={t("knowledge.searchPlaceholder", "搜索知识库名称或描述")}
        actions={
          <button
            type="button"
            onClick={() => setDialog({ kb: null })}
            className={PRIMARY_BTN}
          >
            <Plus size={16} />
            {t("knowledge.create", "新建知识库")}
          </button>
        }
      />

      {config && !config.ready && <KnowledgeNotReadyNotice className="mx-4 mb-3 sm:mx-6" />}

      <div className="flex shrink-0 items-center border-b border-[var(--theme-border)] px-4 py-2">
        <div
          className="inline-flex items-center gap-1 rounded-lg bg-[var(--theme-bg-subtle)] p-1"
          role="tablist"
          aria-label={t("knowledge.title", "知识库")}
        >
          {tabEntries.map(({ key, label, icon: Icon }) => {
            const isActive = activeTab === key;
            return (
              <button
                key={key}
                type="button"
                role="tab"
                aria-selected={isActive}
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

      <div className="flex-1 overflow-y-auto px-4 py-4 sm:px-6 sm:py-6">
        {loading ? (
          <PanelLoadingState text={t("common.loading", "加载中...")} />
        ) : filtered.length === 0 ? (
          <div className="mx-auto mt-6 max-w-xl rounded-3xl border border-stone-200 p-6 text-center dark:border-stone-800">
            <LibraryBig className="mx-auto mb-3 text-stone-400" size={36} />
            <h3 className="text-lg font-semibold text-stone-900 dark:text-stone-100">
              {items.length === 0
                ? t("knowledge.emptyTitle", "还没有知识库")
                : t("knowledge.noMatchTitle", "没有找到匹配的知识库")}
            </h3>
            <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
              {items.length === 0
                ? t(
                    "knowledge.emptyDesc",
                    "新建一个知识库并上传文档，系统会自动解析、切分并建立检索索引。",
                  )
                : t("knowledge.noMatchDesc", "换个关键词试试。")}
            </p>
            {items.length === 0 && (
              <button
                type="button"
                onClick={() => setDialog({ kb: null })}
                className={`${PRIMARY_BTN} mt-4`}
              >
                <Plus size={16} />
                {t("knowledge.create", "新建知识库")}
              </button>
            )}
          </div>
        ) : (
          <div className="auto-grid-cols grid gap-4 sm:gap-5">
            {filtered.map((kb) => (
              <KnowledgeBaseCard
                key={kb.id}
                kb={kb}
                onOpen={() => navigate(`/knowledge/${kb.id}`)}
                onEdit={() => setDialog({ kb })}
                onDelete={() => setDeleting(kb)}
              />
            ))}
          </div>
        )}
      </div>

      {dialog && (
        <KnowledgeBaseDialog
          kb={dialog.kb}
          config={config}
          onClose={() => setDialog(null)}
          onSaved={(saved) => {
            setDialog(null);
            if (dialog.kb) {
              setItems((prev) => prev.map((kb) => (kb.id === saved.id ? saved : kb)));
            } else {
              void reload();
              navigate(`/knowledge/${saved.id}`);
            }
          }}
        />
      )}

      <ConfirmDialog
        isOpen={!!deleting}
        title={t("knowledge.deleteTitle", "删除知识库")}
        message={t("knowledge.deleteConfirm", {
          name: deleting?.name ?? "",
          defaultValue:
            "确定删除“{{name}}”吗？库内所有文档、分块和检索索引都会被永久删除，此操作无法撤销。",
        })}
        confirmText={t("common.delete", "删除")}
        variant="danger"
        loading={deleteBusy}
        onConfirm={confirmDelete}
        onCancel={() => setDeleting(null)}
      />
    </div>
  );
}
