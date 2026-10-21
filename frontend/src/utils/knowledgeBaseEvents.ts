/**
 * 知识库刷新事件。
 *
 * 场景：知识库的文档状态变化（WebSocket 通知）、其他入口（如文件库导入）写入文档后，
 * 知识库页面需要重新拉取数据。
 */
export const KNOWLEDGE_BASE_REFRESH_EVENT = "knowledge-base:refresh";

export interface KnowledgeBaseRefreshDetail {
  kbId?: string;
}

function getEventTarget(target?: EventTarget): EventTarget | null {
  if (target) return target;
  if (typeof window === "undefined") return null;
  return window;
}

export function dispatchKnowledgeBaseRefresh(
  detail: KnowledgeBaseRefreshDetail = {},
  target?: EventTarget,
): void {
  const eventTarget = getEventTarget(target);
  if (!eventTarget) return;
  eventTarget.dispatchEvent(
    new CustomEvent<KnowledgeBaseRefreshDetail>(KNOWLEDGE_BASE_REFRESH_EVENT, {
      detail,
    }),
  );
}

/** `kbId` filters events to one knowledge base; events without a kbId always match. */
export function subscribeKnowledgeBaseRefresh(
  handler: (detail: KnowledgeBaseRefreshDetail) => void,
  options: { kbId?: string; target?: EventTarget } = {},
): () => void {
  const eventTarget = getEventTarget(options.target);
  if (!eventTarget) return () => undefined;

  const listener = (event: Event) => {
    const detail = (event as CustomEvent<KnowledgeBaseRefreshDetail>).detail ?? {};
    if (options.kbId && detail.kbId && detail.kbId !== options.kbId) return;
    handler(detail);
  };
  eventTarget.addEventListener(KNOWLEDGE_BASE_REFRESH_EVENT, listener);
  return () => {
    eventTarget.removeEventListener(KNOWLEDGE_BASE_REFRESH_EVENT, listener);
  };
}
