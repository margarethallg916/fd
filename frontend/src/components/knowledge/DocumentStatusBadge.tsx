import { Loader2, RotateCcw } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Tooltip } from "../common/Tooltip";
import type { KBDocument } from "../../services/api/knowledgeBase";
import {
  STATUS_TONE_CLASSES,
  getDocErrorMessage,
  getDocStatusLabel,
  getDocStatusMeta,
} from "./knowledgeUtils";

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
  const label = getDocStatusLabel(t, doc.status);
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

  // Tooltip only renders string content.
  const message = getDocErrorMessage(doc) || t("knowledge.status.errorUnknown", "未知错误");
  const tooltip = onRetry
    ? `${message} (${t("knowledge.status.clickToRetry", "点击重试")})`
    : message;

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
