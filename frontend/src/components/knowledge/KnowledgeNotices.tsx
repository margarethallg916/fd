import { AlertTriangle, LibraryBig } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";
import { NOTICE_WARNING, OUTLINE_BTN } from "./knowledgeStyles";

export function KnowledgeNotReadyNotice({ className = "" }: { className?: string }) {
  const { t } = useTranslation();
  return (
    <div role="alert" className={`${NOTICE_WARNING} ${className}`}>
      <AlertTriangle size={16} className="mt-0.5 shrink-0" />
      {t(
        "knowledge.notReady",
        "管理员尚未配置向量模型，暂时无法入库。你仍可以创建知识库，配置完成后文档会自动处理。",
      )}
    </div>
  );
}

export function KnowledgeDisabledState() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  return (
    <div className="flex h-full items-center justify-center px-6">
      <div className="max-w-md rounded-3xl border border-stone-200 p-6 text-center dark:border-stone-800">
        <LibraryBig className="mx-auto mb-3 text-stone-400" size={36} />
        <h3 className="text-lg font-semibold text-stone-900 dark:text-stone-100">
          {t("knowledge.disabledTitle", "知识库功能未开启")}
        </h3>
        <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
          {t("knowledge.disabledDesc", "管理员已关闭知识库功能，请联系管理员开启。")}
        </p>
        <button type="button" onClick={() => navigate("/chat")} className={`${OUTLINE_BTN} mt-4`}>
          {t("knowledge.backToChat", "返回聊天")}
        </button>
      </div>
    </div>
  );
}

export function KnowledgeNotFoundState({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  return (
    <div className="flex h-full items-center justify-center px-6">
      <div className="max-w-md rounded-3xl border border-stone-200 p-6 text-center dark:border-stone-800">
        <LibraryBig className="mx-auto mb-3 text-stone-400" size={36} />
        <h3 className="text-lg font-semibold text-stone-900 dark:text-stone-100">
          {t("knowledge.notFoundTitle", "知识库不存在")}
        </h3>
        <p className="mt-1 text-sm text-stone-500 dark:text-stone-400">
          {t("knowledge.notFoundDesc", "它可能已被删除，或你没有访问权限。")}
        </p>
        <button type="button" onClick={onBack} className={`${OUTLINE_BTN} mt-4`}>
          {t("knowledge.backToList", "返回知识库列表")}
        </button>
      </div>
    </div>
  );
}
