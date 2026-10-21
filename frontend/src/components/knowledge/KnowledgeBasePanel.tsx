import { useParams } from "react-router-dom";
import { useSettingsContext } from "../../contexts/SettingsContext";
import { KnowledgeBaseDetail } from "./KnowledgeBaseDetail";
import { KnowledgeBaseListPage } from "./KnowledgeBaseListPage";
import { KnowledgeDisabledState } from "./KnowledgeNotices";

export function KnowledgeBasePanel() {
  const { kbId } = useParams<{ kbId?: string }>();
  const { settings, isLoading, enableKnowledgeBase } = useSettingsContext();

  // The flag is only trustworthy once settings have loaded.
  if (settings !== null && !isLoading && !enableKnowledgeBase) {
    return <KnowledgeDisabledState />;
  }

  return kbId ? <KnowledgeBaseDetail key={kbId} kbId={kbId} /> : <KnowledgeBaseListPage />;
}
