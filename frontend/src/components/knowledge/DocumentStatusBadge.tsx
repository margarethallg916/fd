import { Loader2, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Tooltip } from "../common/Tooltip";
import type { KBDocument, KBDocumentStatus } from "../../services/api/knowledgeBase";
import {
  STATUS_TONE_CLASSES,
  getDocErrorMessage,
  getDocStatusMeta,
} from "./knowledgeUtils";

const STATUS_DEFAULT_LABELS: Record<KBDocumentStatus, string> = {
  uploaded: "排队中",
  parsing: "解析中",
  parsed: "待入库",
  indexing: "入库中",
  indexed: "已完成",
  error_parsing: "解析失败",
  error_indexing: "入库失败",
};

export function useDocStatusLabel() {
  const { t } = useTranslation();
  return (status: KBDocumentStatus) =>
    t(`knowledge.status.${status}`, STATUS_DEFAULT_LABELS[status]);
}

interface DocumentStatusBadgeProps {
  doc: Pick<KBDocument, "status" | "error">;
  /** Clicking a failed badge retries the document. */
  onRetry?: () => void;
  disabled?: boolean;
}

export function DocumentStatusBadge({
  doc,
  onRetry,
  disabled,
}: DocumentStatusBadgeProps) {
  const { t } = useTranslation();
  const label = useDocStatusLabel()(doc.status);
  const meta = getDocStatusMeta(doc.status);
  const className = `inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-[11px] font-medium ${STATUS_TONE_CLASSES[meta.tone]}`;

  if (!meta.failed) {
    return (
      <span className={className} data-status={doc.status}>
        {meta.processing && (
          <Loader2 size={11} className="animate-spin" aria-hidden="true" />
        )}
        {label}
      </span>
    );
  }

  const message = getDocErrorMessage(doc);
  const tooltip = (
    <div className="max-w-xs whitespace-pre-wrap break-words text-xs">
      {message || t("knowledge.status.errorUnknown", "未知错误")}
      {onRetry && (
        <div className="mt-1 opacity-80">
          {t("knowledge.status.clickToRetry", "点击重试")}
        </div>
      )}
    </div>
  );

  return (
    <Tooltip content={tooltip} placement="top">
      {onRetry ? (
        <button
          type="button"
          onClick={onRetry}
          disabled={disabled}
          data-status={doc.status}
          className={`${className} cursor-pointer hover:opacity-80 disabled:cursor-not-allowed disabled:opacity-50`}
        >
          <RotateCcw size={11} aria-hidden="true" />
          {label}
        </button>
      ) : (
        <span className={className} data-status={doc.status}>
          {label}
        </span>
      )}
    </Tooltip>
  );
}
