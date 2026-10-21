import { Suspense, lazy } from "react";
import type { TabType } from "./types";

const SkillsHubPanel = lazy(() =>
  import("../../panels/SkillsHubPanel").then((m) => ({
    default: m.SkillsHubPanel,
  })),
);
const UsersPanel = lazy(() =>
  import("../../panels/UsersPanel").then((m) => ({ default: m.UsersPanel })),
);
const RolesPanel = lazy(() =>
  import("../../panels/RolesPanel").then((m) => ({ default: m.RolesPanel })),
);
const SettingsPanel = lazy(() =>
  import("../../panels/SettingsPanel").then((m) => ({
    default: m.SettingsPanel,
  })),
);
const AgentModelPanel = lazy(() =>
  import("../../panels/AgentModelPanel").then((m) => ({
    default: m.AgentModelPanel,
  })),
);
const MCPPanel = lazy(() =>
  import("../../panels/MCPPanel").then((m) => ({ default: m.MCPPanel })),
);
const FeedbackPanel = lazy(() =>
  import("../../panels/FeedbackPanel").then((m) => ({
    default: m.FeedbackPanel,
  })),
);
const RevealedFilesPage = lazy(() =>
  import("../../fileLibrary/RevealedFilesPanel").then((m) => ({
    default: m.RevealedFilesPanel,
  })),
);
const NotificationPanel = lazy(() =>
  import("../../panels/NotificationPanel").then((m) => ({
    default: m.NotificationPanel,
  })),
);
const MemoryPanel = lazy(() =>
  import("../../panels/MemoryPanel").then((m) => ({
    default: m.MemoryPanel,
  })),
);
const PersonaPlazaPanel = lazy(() =>
  import("../../persona/PersonaPlazaPanel").then((m) => ({
    default: m.PersonaPlazaPanel,
  })),
);
const TeamBuilderPanel = lazy(() =>
  import("../../team/TeamBuilderWrapper").then((m) => ({
    default: m.TeamBuilderWrapper,
  })),
);
const ScheduledTaskAdminPanel = lazy(() =>
  import("../../panels/ScheduledTaskAdminPanel").then((m) => ({
    default: m.ScheduledTaskAdminPanel,
  })),
);
const ScheduledTaskPanel = lazy(() =>
  import("../../panels/ScheduledTaskPanel").then((m) => ({
    default: m.ScheduledTaskPanel,
  })),
);
const KnowledgeBasePanel = lazy(() =>
  import("../../knowledge/KnowledgeBasePanel").then((m) => ({
    default: m.KnowledgeBasePanel,
  })),
);
const AnalyticsPanel = lazy(() =>
  import("../../panels/analytics/AnalyticsPanel").then((m) => ({
    default: m.AnalyticsPanel,
  })),
);

const panelMap: Record<
  string,
  React.LazyExoticComponent<React.ComponentType>
> = {
  skills: SkillsHubPanel,
  workspace: SkillsHubPanel,
  marketplace: SkillsHubPanel,
  builtin: SkillsHubPanel,
  users: UsersPanel,
  roles: RolesPanel,
  settings: SettingsPanel,
  mcp: MCPPanel,
  feedback: FeedbackPanel,
  agents: AgentModelPanel,
  files: RevealedFilesPage,
  knowledge: KnowledgeBasePanel,
  persona: PersonaPlazaPanel,
  team: TeamBuilderPanel,
  notifications: NotificationPanel,
  memory: MemoryPanel,
  scheduled: ScheduledTaskPanel,
  scheduledAdmin: ScheduledTaskAdminPanel,
  analytics: AnalyticsPanel,
};

function PanelLoader() {
  return (
    <div className="flex h-full items-center justify-center">
      <div className="relative h-8 w-8">
        <div className="absolute inset-0 rounded-full border-2 border-stone-200 dark:border-stone-700" />
        <div className="absolute inset-0 rounded-full border-2 border-transparent border-t-stone-500 dark:border-t-stone-400 animate-spin will-change-transform" />
      </div>
    </div>
  );
}

export function TabContent({ activeTab }: { activeTab: TabType }) {
  if (activeTab === "chat") return null;

  const Panel = panelMap[activeTab];
  if (!Panel) return null;

  return (
    <main className="flex-1 overflow-hidden bg-[var(--theme-bg)]">
      <div className="mx-auto w-full h-full flex flex-col overflow-hidden lg:max-w-[80rem] xl:max-w-[96rem] 2xl:max-w-[120rem] sm:px-4">
        <Suspense fallback={<PanelLoader />}>
          <Panel />
        </Suspense>
      </div>
    </main>
  );
}
