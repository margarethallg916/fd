# 昆小智 frontend conventions — research for the 知识库 (Knowledge Base) feature

Scope: read-only study of `/tmp/hoplite/workspace/frontend` @ main `1498b534`. All paths are relative to `frontend/` unless they start with `.trellis/` or `src/` (backend). Line numbers were read from the working tree. Items I could not prove are tagged **UNCONFIRMED**.

Where the Trellis spec and the code disagree, **the code wins** (see §0 "spec drift").

---

## 0. Top conventions / pitfalls (read first)

1. **No shared Button/Input/Modal/Tabs/Badge components.** Buttons are CSS classes (`btn-primary`, `btn-secondary`, `btn-danger`, `btn-icon`) or hand-written Tailwind strings; modals are hand-rolled `fixed inset-0` overlays; the only shared dialog is `ConfirmDialog`; the only shared drawer is `EditorSidebar`; tabs are an inline segmented-control `role="tablist"`. Copy the nearest existing markup (§3), do not invent a UI kit.
2. **Spec drift:** `.trellis/spec/frontend/*.md` says "Vitest + Testing Library" and "React 18 / react-router". Reality: React **19.2.5**, `react-router-dom` **^7.13**, tests are **`node:test` run through `tsx --test`** with `renderToStaticMarkup` or source-string assertions (`readFileSync` + `assert.match`). Vitest/@testing-library are NOT installed. There is **no `test` script** in `package.json`.
3. **A new nav tab touches 8 places** (§2.4): `TabType`, `TabContent` `panelMap`, `App.tsx` page wrapper + `<Route>`, `SidebarRail`, `SessionListContent`, `nav.<tab>` i18n key (the `Header` renders `t(\`nav.${activeTab}\`)` — missing key shows the raw tab id), `seo.<name>.*` i18n keys, optional skeleton. The existing **Files nav item never sets `is-active`** — set it for KB yourself.
4. **WebSocket is only mounted in `ChatAppContent`** (`components/layout/AppContent/ChatAppContent.tsx:600`). Non-chat pages (including a `/knowledge` page) receive **no** WS events, and every `useWebSocket()` call opens its own socket. Use polling (pattern in §5) for KB progress; WS "acceleration" would need a new hook mounted in `NonChatAppContent` or a lifted provider.
5. **Role-limit form rebuilds `limits` from scratch** (`components/panels/RolesPanel.tsx:111`) and the backend **replaces** `limits` wholesale (`src/infra/role/storage.py:316-317`). Any new RoleLimits key (e.g. `max_kb_count`) that is not added to the form state will be **silently wiped** the next time an admin saves a role.
6. **Per-run extra data cannot ride in `agent_options`** (typed `Record<string, boolean|string|number>`) and `ChatView` drops the composer's `options` arg (`ChatView.tsx:405-412`). To send `kb_ids` you must thread a new getter through `useAgent` → `sessionApi.submitChat` → `buildSubmitChatBody` (§9.4).
7. **nginx limits request bodies to 1 MB outside `/api/upload/`** (`nginx/nginx.conf:154-192`: only `/api/upload/` has `client_max_body_size 100M`). A KB upload endpoint under `/api/knowledge/...` will 413 behind the repo nginx unless the config gains a matching `location`. (Prod ingress config UNCONFIRMED — none in `k8s/` or `deploy/`.)
8. **Cards: no gradient banners** (`.trellis/spec/frontend/workbuddy-card-banner.md`). Do not name anything `*__banner`, do not use `linear-gradient` in card components, no `::before` color strips on cards. Small icon tiles with `bg-gradient-to-br from-theme-bg-subtle to-theme-bg` exist (PanelHeader, files EmptyState) and are fine — they are not banners.
9. **Business UI must not use CDN assets/emoji** (spec `quality-guidelines.md`): Lucide icons only, local assets only. `crypto.subtle` must be guarded (use `src/workers/sha256.ts` fallback) — relevant to any client-side file hash for dedupe.
10. **i18n:** zh + en are the "maintained locales"; `ja/ko/ru` simply lack whole top-level namespaces (e.g. `scheduledTask`) and rely on `fallbackLng: "en"`. Add keys to `zh.json` and `en.json` only (§6).

---

## 1. Tooling

| Item | Fact | Evidence |
|---|---|---|
| Package manager | pnpm 10.32.1 | `frontend/package.json`, `pnpm-lock.yaml` |
| Framework/build | React 19.2.5, TypeScript ~5.6.2, Vite ^6.0.5, Tailwind ^3.4.17, `darkMode: "class"` | `package.json`, `tailwind.config.js:3` |
| Router | `react-router-dom` ^7.13 (`BrowserRouter`-style `<Routes>/<Route>`), declared in `src/App.tsx:436-684` | |
| Scripts | `dev` = `vite` (port 3001, proxies `/api`, `/ws` → `127.0.0.1:8000`); `build` = `tsc -b && vite build`; `lint` = `eslint .`; `i18n:extract` = `tsx scripts/extract-i18n.ts`; no `test`, no `typecheck` | `package.json` scripts, `vite.config.ts:192-240` |
| Typecheck baseline | `pnpm exec tsc --noEmit -p .` exit 0 | `.trellis/tasks/10-21-personal-knowledge-base/research/baseline-tests.md` |
| TS strictness | `strict`, `noUnusedLocals`, `noUnusedParameters`, `noFallthroughCasesInSwitch`, `noUncheckedSideEffectImports`, `allowImportingTsExtensions`, `isolatedModules`; target ES2020 | `tsconfig.json` |
| Path aliases | **None.** Only relative imports (`../../common/...`). Vite aliases exist only for `pdfjs-dist`, `opentype.js`, `stream`, `process` | `vite.config.ts:91-152` |
| ESLint | typescript-eslint recommended + react-hooks + react-refresh (`only-export-components` = warn); `no-explicit-any` = warn; unused vars warn unless `_`-prefixed | `eslint.config.js` |
| Test framework | **`node:test` + `assert/strict`, executed with `tsx --test`** (219 test files; zero import `vitest`). Component tests use `react-dom/server` `renderToStaticMarkup`; many tests are *source-contract tests* that `readFileSync` a `.tsx`/`.css` file and regex-assert on it | e.g. `src/components/common/__tests__/BrandWordmark.test.tsx`, `src/components/panels/SidebarParts/__tests__/sidebarNavStyles.test.ts` |
| Run one test | `cd frontend && ./node_modules/.bin/tsx --test src/components/panels/SidebarParts/__tests__/sidebarNavStyles.test.ts` (verified: 2 pass, ~1 s). Glob for many: `tsx --test "src/**/__tests__/*.test.ts"` (**UNCONFIRMED** glob form; a single path is verified) | |
| Test placement | adjacent `__tests__/<name>.test.ts(x)` | spec `quality-guidelines.md` |

Test pattern to copy (source-contract style):

```ts
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const src = readFileSync(new URL("../KnowledgeBasePanel.tsx", import.meta.url), "utf8");
test("KB panel never renders gradient banners", () => {
  assert.ok(!src.includes("linear-gradient"));
  assert.ok(!src.includes("__banner"));
});
```

i18n key-parity tests live in `src/i18n/__tests__/*Keys.test.ts` (e.g. `roleMaxChannelsKeys.test.ts` uses `maintainedLocales = ["en","zh"]` and rejects `[TODO]`/`【待翻译】`/`roles.` placeholder values). Add `knowledgeKeys.test.ts` in the same style.

---

## 2. Left sidebar navigation

### 2.1 Components (3 render paths)

| Surface | File | Notes |
|---|---|---|
| Orchestrator | `src/components/panels/SessionSidebar.tsx` (877 lines) | owns `isCollapsed`, feature flags, "More" menu items (`moreMenuFeatureItems` @120-135), renders `SidebarRail` @758 (collapsed desktop), `SessionListContent` (expanded desktop @~700-720 and mobile drawer @646-681), `MobileMoreMenuSheet`/`DesktopMoreMenu` @686/868 |
| Collapsed rail (icons only, 52 px) | `src/components/panels/SidebarParts/SidebarRail.tsx` | `railBtn` const @18; nav buttons @122-199 |
| Expanded list / mobile drawer | `src/components/panels/SidebarParts/SessionListContent.tsx` | action buttons @199-262: New chat, **Workspace @212-221**, Team (permission-gated) @223-231, **Files @233-240**, More @242-254 |
| Shell | `components/layout/AppContent/AppShell.tsx`, `NonChatAppContent.tsx` (mounts `SessionSidebar` + `TabContent`), `AppContent/index.tsx` (collapsed state, localStorage `lamb-sidebar-collapsed`, default collapsed = **true**) |

### 2.2 Item markup (copy exactly)

Expanded (`SessionListContent.tsx:233-240`, Files item; Workspace item @212-221 shows the active variant):

```tsx
<button
  onClick={() => navigate("/files")}
  className="sidebar-nav-btn w-full h-8 rounded-[10px] flex items-center gap-3 px-[9px] focus:outline-none transition-colors"
>
  <FolderOpen size={20} />
  <span>{t("fileLibrary.title")}</span>
</button>
```

Active variant (Workspace): add `${isActive ? "is-active" : ""}` to className and `aria-current={isActive ? "page" : undefined}`; `isWorkspaceActive` is computed from `location.pathname` in the same file (@132-139) and again in `SidebarRail.tsx:61-68`. For a prefix route use `location.pathname.startsWith("/knowledge")`.

Collapsed rail (`SidebarRail.tsx:152-159`):

```tsx
<button type="button" onClick={onOpenFileLibrary} className={railBtn}
  title={t("fileLibrary.title")} aria-label={t("fileLibrary.title")}>
  <FolderOpen size={20} />
</button>
```
(`railBtn` = `"sidebar-rail-btn flex h-9 w-9 items-center justify-center rounded-full transition-colors mx-2 touch-manipulation"`; active = append `is-active`.) The rail takes callbacks as props (`onOpenFileLibrary`, `onOpenWorkspace`, `onOpenTeamBuilder` wired in `SessionSidebar.tsx:772-774` as `() => navigate("/files")`), so a new item needs a new prop `onOpenKnowledgeBase`.

### 2.3 Styling / states

* CSS in `src/styles/base.css:274-310`: `.sidebar-nav-btn` (color `--theme-text-secondary`, 0.875rem/500), `:hover` bg `--theme-sidebar-hover`, `.is-active`/`[aria-current="page"]` bg `--theme-sidebar-active` + weight 600, `.sidebar-rail-btn` same, shared `:focus-visible` ring `--theme-ring`.
* Icons: `lucide-react` **0.468.0**, size 20 in nav, 16-18 elsewhere. Verified present: `LibraryBig`, `BookMarked`, `Library`, `BookOpenText`, `Network`, `Share2`, `Store`, `Compass`, `Globe`, `Users`, `FileSearch`, `Database`. `BookOpen` is already used by Dify KB (`DifyKbMultiSelect.tsx`) — pick `LibraryBig` (or `BookMarked`) to avoid confusion with the Dify picker.
* Permission/feature gating: `useAuth().hasPermission(Permission.X)` and `useSettingsContext().enableX`. "More" menu items are plain objects `{ path, label, icon, show }` (`SessionSidebar.tsx:120-135`); `DesktopMoreMenu` (@28-73) and `MobileMoreMenuSheet` (@43) render them with the same `sidebar-nav-btn` markup, `show` filtered, `matchPaths?` supported for active state.
* Skeleton: `components/skeletons/SidebarSkeleton.tsx:104` has a "File Library" placeholder row — add a row if the nav grows (avoid layout shift).
* Test that pins nav style: `SidebarParts/__tests__/sidebarNavStyles.test.ts` (asserts `.sidebar-nav-btn` rule values and a regex on `SessionListContent.tsx`). Do not change those class names.

### 2.4 Checklist: add nav item + route + page

Current patterns: `/files` and `/scheduled-tasks` (both are "tab" panels rendered through `AppContent`).

1. `src/components/layout/AppContent/types.ts` — add `| "knowledge"` to `TabType`.
2. `src/components/layout/AppContent/TabContent.tsx` — `lazy()` import the panel (`import(...).then(m => ({default: m.KnowledgeBasePanel}))`, @5-85 pattern) and add `knowledge: KnowledgeBasePanel` to `panelMap` (@88-107). `TabContent` renders `<Panel />` **with no props** inside `main.flex-1.overflow-hidden.bg-[var(--theme-bg)]` > `div.mx-auto.w-full.h-full.flex.flex-col.overflow-hidden lg:max-w-[80rem] xl:max-w-[96rem] 2xl:max-w-[120rem] sm:px-4` (@110-125) — so the panel reads the route with `useParams()` itself and must manage its own scrolling (`h-full flex flex-col overflow-hidden`, scroll in an inner `flex-1 overflow-y-auto`).
3. `src/App.tsx` — page wrapper (pattern @257-264 / @293-300):
   ```tsx
   function KnowledgePage() {
     useSEO({ title: "seo.knowledge.title", description: "seo.knowledge.description", path: "/knowledge" });
     return <AppContent key="knowledge" activeTab="knowledge" />;
   }
   ```
   and a route inside `<Routes>` before `path="*"` (@683). Routes are wrapped in `ProtectedRoute` (props: `permissions` any-of, `requireAllPermissions`, `requireAdmin`, `redirectTo`, `showToast`, `toastMessage`, `loadingComponent`; `components/auth/ProtectedRoute.tsx:14-76`):
   ```tsx
   <Route path="/knowledge/:kbId?" element={
     <ProtectedRoute permissions={[Permission.KNOWLEDGE_USE]} redirectTo="/chat" showToast toastMessage={t("errors.noPermission")}>
       <KnowledgePage />
     </ProtectedRoute>} />
   ```
   Use **one** route with an optional param (like `/chat/:sessionId?` @447): two sibling routes with the same `key="knowledge"` would remount `AppContent`. For tabs use `?tab=` (see §3.4) rather than extra routes.
4. `SessionListContent.tsx` + `SidebarRail.tsx` + `SessionSidebar.tsx` (new `onOpenKnowledgeBase` prop) — nav button as in §2.2; gate on `enableKnowledgeBase` setting + permission.
5. `Header.tsx:191` renders `t(\`nav.${activeTab}\`, {defaultValue: activeTab})` for every non-chat tab, with a back chevron `navigate(-1)` — add `nav.knowledge` to `zh.json`/`en.json`.
6. i18n `seo.knowledge.{title,description}` (see `jq .seo.files`).
7. Permission enum: `src/types/auth.ts:6-80` mirrors backend `src/kernel/schemas/permission.py` (+ default role grants in `src/infra/auth/rbac.py:~174`). A new `Permission.KNOWLEDGE_*` must be added on **both** sides, else `ProtectedRoute` redirects everyone.
8. Feature flag: `SettingsContext` exposes `enableSkills/enableMemory/enableScheduledTasks` (`contexts/SettingsContext.tsx:28-30,161-163`) via `getBooleanSetting("KEY")`; the key must be defined in backend `src/kernel/config/_definitions_extra.py` with `"frontend_visible": True` (see `SCHEDULED_TASK_ENABLED` @708-715) so non-admins can read it. Add `enableKnowledgeBase` the same way.

SPA fallback: backend `serve_spa` (`src/api/main.py:~946`) is a catch-all `GET /{full_path:path}`; PWA `pwaRouting.ts` only treats `/api,/ws,/health,/tools,/human,/services,/default,/data_pipeline,/simple_workflow` as backend — `/knowledge` is safe; avoid those prefixes for SPA routes.

---

## 3. Page layout conventions, primitives, tokens

### 3.1 Representative pages

| Page | File | Why it is a good template |
|---|---|---|
| Scheduled tasks (list of cards + dialog + drawer + ConfirmDialog + empty + feature-off banner) | `components/panels/ScheduledTaskPanel/index.tsx` (415 lines), `TaskEditorDialog.tsx`, `TaskRunsDrawer.tsx` | most recent feature, closest to "KB list + detail + dialogs"; polling & event refresh |
| Persona plaza (tabs/scope dropdown, search, tag filter, card grid, Pagination, skeleton) | `components/persona/PersonaPlazaPanel.tsx`, `PersonaPresetCard.tsx`, `PersonaScopeDropdown.tsx`, `usePersonaPlaza.ts` | scope switch = analogue of 我的/共享/广场 |
| Skills hub / workspace tabs | `components/panels/SkillsHubPanel.tsx` | canonical tab bar + `?tab=` URL state |
| File library (toolbar, grid/list toggle, infinite scroll, context menu) | `components/fileLibrary/*` | toolbar + per-item menu |
| Memory panel (list + batch select + Pagination + EditorSidebar) | `components/panels/MemoryPanel/index.tsx` | CRUD list with checkboxes/batch delete |

### 3.2 Page skeleton (copy)

```tsx
// ScheduledTaskPanel/index.tsx:150-215 (abridged)
<div className="flex h-full flex-col overflow-hidden">
  <PanelHeader
    title={t("scheduledTask.title", "定时任务")}
    subtitle={t("scheduledTask.subtitle", "…")}
    icon={<CalendarClock />}
    actions={
      <button type="button" onClick={...}
        className="inline-flex items-center gap-1.5 rounded-xl bg-stone-900 px-4 py-2 text-sm font-medium text-white hover:bg-stone-800 disabled:cursor-not-allowed disabled:opacity-50 dark:bg-stone-100 dark:text-stone-900">
        <Plus size={16} />{t("scheduledTask.create", "新建定时任务")}
      </button>}
  />
  <div className="flex-1 overflow-y-auto px-4 pb-8 sm:px-6">
    {loading ? <PanelLoadingState text={t("common.loading", "加载中...")} />
      : items.length === 0 ? <EmptyBlock/> : <div className="grid gap-3 lg:grid-cols-2">{cards}</div>}
  </div>
  {/* dialogs / drawers / <ConfirmDialog/> at the end */}
</div>
```

Persona plaza variant adds search + accessories in the header: `PanelHeader` props `searchValue / onSearchChange / searchPlaceholder / searchAccessory` (`common/PanelHeader.tsx:12-31`), content wrapper `skill-content-area flex-1 overflow-y-auto py-2 sm:py-4 px-4 sm:p-6 lg:px-8 lg:py-8`, grid `grid auto-grid-cols gap-4 sm:gap-5` (`.auto-grid-cols` = `repeat(auto-fill, minmax(320px,1fr))`, `styles/utilities.css:100`), and `<Pagination page pageSize total onChange />` inside `div.glass-divider.px-3.py-3.sm:px-6` (`PersonaPlazaPanel.tsx:325-336`). **Pagination `page` is 1-based**, renders nothing when `totalPages <= 1`.

### 3.3 Primitive inventory (import path → props)

| Need | Use | Path / props |
|---|---|---|
| Page header | `PanelHeader` | `components/common/PanelHeader.tsx` — `{title, subtitle?, icon?, actions?, searchValue?, onSearchChange?, searchPlaceholder?, searchAccessory?, children?, className?}`; CSS `.panel-header` is `position: sticky; z-10`, border-bottom |
| Search input | `PanelSearchInput` (CJK-IME-safe, `forwardRef`) | `components/common/PanelSearchInput.tsx` — `{value, onValueChange, ...input props}`; CSS `.panel-search` |
| Buttons | CSS classes | `btn-primary`, `btn-secondary`, `btn-danger`, `btn-icon`, `btn-sm` (`styles/components.css:213-330,1227`); panels also use the inline dark/black style above (`bg-stone-900 … dark:bg-stone-100`) for the primary CTA and `actionBtn = "inline-flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-xs text-stone-600 hover:bg-stone-100 disabled:opacity-40 dark:text-stone-300 dark:hover:bg-stone-800"` for card actions |
| Text input / textarea | `glass-input` (+ `es-input` inside EditorSidebar forms); inline: `w-full rounded-xl border border-stone-200 bg-white px-3 py-2.5 text-sm dark:border-stone-700 dark:bg-stone-900` | `styles/glass.css:59`, `components.css:868` |
| Select | `GlassSelect` | `components/common/GlassSelect.tsx` — `{value, onChange(value), options: {value,label,disabled?}[], disabled?, placeholder?, className?}` (portal dropdown) |
| Multi-select chips | `DifyKbMultiSelect` (reference only) | `components/common/DifyKbMultiSelect.tsx` |
| Checkbox | `Checkbox` | `components/common/Checkbox.tsx` — `{checked, onChange?, pending?, disabled?, size?: "sm"|"md"|"lg", className?}` (amber when checked) |
| Switch | `Toggle` (with label) `components/skill/Toggle.tsx` `{checked,onChange(val),label,disabled?}`; `ToggleSwitch` `components/panels/AgentPanel/shared/ToggleSwitch.tsx` `{enabled,onToggle,disabled?,ariaLabel?}`; inline `role="switch"` in `ScheduledTaskPanel/index.tsx:310-328` (emerald when on, `h-6 w-11` track) | |
| Confirm | `ConfirmDialog` | `components/common/ConfirmDialog.tsx` — `{isOpen,title,message,confirmText?,cancelText?,onConfirm,onCancel,variant?: "danger"|"warning"|"info",loading?,helpText?,helpLabel?}`; portal, `z-[300]`, Esc closes unless `loading`, `data-yields-sidebar` |
| Right-side form drawer | `EditorSidebar` | `components/common/EditorSidebar.tsx` — `{open,onClose,title,subtitle?,icon?,children,footer?,width?: "default"|"wide",defaultWidthPct?,widthStorageKey?}`; resizable, swipe-to-close on mobile; footer example `MemoryPanel/MemoryEditor.tsx:139-160` (`btn-secondary` Cancel + spacer + primary). Form classes `es-field / es-label / es-hint / es-section / es-section-title / es-row es-row-2` (`components.css:769-1100`) |
| Modal | none shared. Pattern: `fixed inset-0 z-50 flex items-end justify-center bg-black/40 p-0 sm:items-center sm:p-4` + `role="dialog" aria-modal="true"` + `rounded-t-3xl bg-white shadow-2xl dark:bg-stone-900 sm:max-w-2xl sm:rounded-3xl` (`TaskEditorDialog.tsx:146-160`); smaller: `NewProjectModal.tsx:26-29` (`z-[300]`, `w-[90vw] max-w-md`). Always Esc-to-close + backdrop click |
| Drawer (read-only/detail) | inline `fixed inset-0 z-50 flex justify-end bg-black/30` + `aside.max-w-lg` (`TaskRunsDrawer.tsx:62-66`) | |
| Dropdown / context menu | `DropdownShell` (`fileLibrary/components/DropdownShell.tsx`), `PersonaScopeDropdown` (portal, `z-[999]`), `FileContextMenu` (fixed, `z-[999]`, `useContextMenu` hook), `DesktopMoreMenu` (portal `z-[301]`) | no generic `<Dropdown>` |
| Tabs | inline segmented control (§3.4) | |
| Badge / pill | inline spans; status colour map `STATUS_STYLES` (`ScheduledTaskPanel/scheduleUtils.ts:115-126`, e.g. `bg-emerald-50 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300`); card pills `scb__status-pill scb__status-pill--installed|inactive|published|danger|disabled`, tags `scb__mini-tag` (`styles/card-base.css:176-300`) | use for document status badges |
| Tooltip | `Tooltip` | `components/common/Tooltip.tsx` — `{content: ReactNode, placement?: "top"|"bottom"|"auto", children, className?, zIndex?}` (hover + long-press touch) |
| Toast | `react-hot-toast` | `import toast from "react-hot-toast"` → `toast.success/error/loading`; `<Toaster>` configured in `App.tsx:376-440` (top offset `calc(56px + safe-area)`, success 3 s, error 5 s). Error toasts show `e.message` verbatim (`toast.error(e instanceof Error ? e.message : String(e))`) |
| Spinner / loading | `LoadingSpinner {size: xs|sm|md|lg|xl, className?, static?, color?}`; `Loading`; `PanelLoadingState {text?, containerClassName?, className?}` (`components/common/`) |
| Skeletons | `components/skeletons/primitives.tsx` `SkeletonLine/SkeletonBlock/SkeletonList/SkeletonCard` + `.skeleton-line` shimmer; panel-specific skeletons (`PersonaPlazaSkeleton`, `FilesPageSkeleton`) exported from `components/skeletons/index.ts` |
| Empty state | `.skill-empty-state / __icon / __title / __description / __action` (`styles/skill.css:251-301`) — used by Persona plaza; or the rounded-3xl bordered block in ScheduledTaskPanel (`rounded-3xl border border-stone-200 p-6 text-center dark:border-stone-800` + 36 px icon `text-stone-400`); files `EmptyState` |
| Pagination | `Pagination {page,pageSize,total,onChange}` | `components/common/Pagination.tsx` (1-based) |
| Infinite scroll | `react-intersection-observer` `useInView({threshold:0.1})` + `loadMoreRef` (`hooks/useRevealedFiles.ts:39`, `RevealedFilesPanel.tsx:208-222`); `react-virtuoso` for chat lists |
| File-type icon/colour | `getFileTypeInfo(name, mime)` → `{icon,color}` (`components/documents/utils`); `AttachmentCard`, `formatFileSize` (`components/common/AttachmentCard`); `FileIcon` |
| Dates | `utils/datetime.ts` `formatDateTime/formatDate/formatTime/formatTimeAgo(t, input)/formatRelativeDate`; scheduled tasks pin `Asia/Shanghai` via `formatFireTime` |

### 3.4 Tabs (copy for 我的知识库 / 共享知识库 / 知识广场)

`components/panels/SkillsHubPanel.tsx:124-148` (URL state via `?tab=`, `navigate(..., {replace:true})`; resolver pure function in `SkillsHubPanel/state.ts:resolveWorkspaceHubTab`):

```tsx
<div className="flex shrink-0 items-center border-b border-[var(--theme-border)] px-4 py-2">
  <div className="inline-flex items-center gap-1 rounded-lg bg-[var(--theme-bg-subtle)] p-1" role="tablist" aria-label={...}>
    {tabEntries.map(({ key, label, icon: Icon }) => {
      const isActive = visibleTab === key;
      return (
        <button key={key} type="button" role="tab" aria-selected={isActive} onClick={() => handleTabChange(key)}
          className={`flex items-center gap-1.5 rounded-md px-3 py-1.5 text-sm font-medium transition-colors ${
            isActive ? "is-active bg-[var(--theme-sidebar-active)] text-[var(--theme-text)]"
                     : "text-[var(--theme-text-secondary)] hover:bg-[var(--theme-sidebar-hover)] hover:text-[var(--theme-text)]"}`}>
          <Icon size={16} /><span>{label}</span>
        </button>);
    })}
  </div>
</div>
<div className="min-h-0 flex-1 overflow-hidden">{/* active tab content */}</div>
```

### 3.5 Card style (post-banner-ban)

Shared card base `.scb` (`styles/card-base.css:45-110`) used by `PersonaPresetCard.tsx:74` (`scb group flex h-full flex-col overflow-hidden bg-[var(--theme-bg-card)]` > `flex flex-1 flex-col p-4 sm:p-5`; title row `truncate text-base font-semibold text-[var(--theme-text)]`; meta `text-[11px] text-[var(--theme-text-secondary)]` with dot separators; description `text-[13px] leading-relaxed … line-clamp-2`; footer `mt-4 flex items-center justify-between gap-2 border-t border-[var(--theme-border)] pt-3`). Simpler variants: ScheduledTask `TaskCard` (`rounded-2xl border p-4 … border-stone-200 bg-white dark:border-stone-700 dark:bg-stone-900`) and file grid card (`rounded-xl border border-theme-border bg-theme-bg-card hover:shadow-lg hover:border-theme-border-hover`). Pin/star/status controls go in the **title row, right-aligned** (spec). Guard test to extend: `components/team/__tests__/cardBannerMigration.test.ts`.

### 3.6 Design tokens / theme / dark mode

* `src/styles/tokens.css` (single source). Light: `--theme-primary #71717a`, `--theme-bg #ffffff`, `--theme-bg-card #fff`, `--theme-bg-sidebar #f4f4f5`, `--theme-bg-subtle #f6f6f7`, `--theme-sidebar-hover #ebebed`, `--theme-sidebar-active #e3e3e6`, `--theme-border #e6e6e9`, `--theme-text #18181b` / `-secondary #71717a` / `-tertiary #a1a1aa`, radii `--radius-sm…2xl`, `--shadow-card/-card-hover/-dropdown`, `--sidebar-width 16rem`, `--sidebar-rail-width 3.25rem`. Dark (`.dark`): bg `#121010`, card `#1e1b18`, border `#3d3835`, text `#fff`. "Content surfaces are pure white; the sidebar is the only grey plane."
* Tailwind: `tailwind.config.js` maps `theme.*` colors to the vars → `text-theme-text`, `text-theme-text-secondary`, `bg-theme-bg-card`, `bg-theme-bg-subtle`, `border-theme-border`, `border-theme-border-hover`, `bg-theme-primary-light`… Panels use **both** these tokens (`text-theme-*`) and raw stone palette with explicit `dark:` variants (`text-stone-900 dark:text-stone-100`). Either is accepted; always include `dark:` for raw colors. Font: `font-serif` for headings (`PanelHeader` h1), body `Source Sans 3`.
* Dark mode: Tailwind `darkMode: "class"` (`.dark` on `<html>`, toggled by `contexts/ThemeContext.tsx`); both `dark:` utilities and `.dark …` CSS exist.
* Responsive: mobile-first, `sm 640 / lg 1024 / xl 1280 / 2xl 1536`; the files page uses `@container` / `@md:` classes, but Tailwind is ^3.4 with `plugins: []` and `@tailwindcss/container-queries` is not installed, so those variants are most likely **no-ops** (UNCONFIRMED visual effect; only `.components.css:749` declares a real `container-type`). Do not rely on `@sm:/@md:` in new code — use `sm:/lg:` viewport breakpoints. Safe areas: `env(safe-area-inset-*)`, classes `safe-area-top/bottom`. Mobile sidebar breakpoint is `max-width: 639px`.
* Z-index conventions seen: sticky header 10, drawers/dialogs 50, mobile sidebar 60/70, `ConfirmDialog` & small modals 300, more menu 301, context menus/dropdowns 999.
* Strings: user-facing text through `t()`; the dominant style passes a **zh default** as 2nd arg: `t("scheduledTask.create", "新建定时任务")` or `t("k", {count, defaultValue: "…{{count}}…"})`.

---

## 4. API layer

* **Client:** plain `fetch` wrapped by `authFetch<T>(url, options)` in `src/services/api/fetch.ts:57-169` (not axios). It: sets `Content-Type: application/json` unless body is `FormData`; sets `Accept-Language: i18n.language`; injects `Authorization: Bearer <token>` from `getValidAccessToken()` (proactively refreshes); on `401` refreshes once and retries (`_retry`), else `redirectToLogin()`; honors `X-Force-Relogin`; on `!ok` throws `ApiRequestError(message, status, code?, detail?)` where `message = detail.message ‖ detail ‖ "Request failed (HTTP n)"` passed through `translateBackendError()`; empty body → returns `null` (type `T` lies — handle `T | null` for DELETE/204); forwards `signal` for abort.
* **Base URL:** `API_BASE` from `VITE_API_BASE` (default `""` → same origin) in `services/api/config.ts:7-14`; helpers `getFullUrl(path)` (use for signed/relative file URLs), `buildWebSocketUrl("/ws")`, `buildApiUrl`.
* **Error convention:** backend FastAPI `{detail: string | {code|error, message}}`. `ApiRequestError.code` carries the stable code; map codes to i18n in `src/utils/backendErrors.ts` (`BACKEND_ERROR_KEYS`, e.g. `storage_quota_exceeded: "storage.quotaExceeded"`) so toasts are localized — add `kb_*` quota/permission codes there. `isSandboxCapacityError` shows the duck-typed code check pattern.
* **Module layout:** `services/api/<domain>.ts` exporting a `camelCase` API object (`scheduledTaskApi`, `revealedFileApi`, `memoryApi`, `roleApi`…) + co-located types; barrel `services/api.ts` re-exports (`export { memoryApi } from "./api/memory"`); several newer modules (scheduledTask) are imported directly by path and keep types in `src/types/scheduledTask.ts`. Types are **snake_case mirroring backend** (spec). `interface` for shapes, `type` for unions; CRUD families `X`, `XCreate`, `XUpdate`, `XListResponse`.

Representative module to copy (`services/api/scheduledTask.ts:1-60`):

```ts
import { authFetch } from "./fetch";
import { API_BASE } from "./config";
const BASE = `${API_BASE}/api/scheduled-tasks`;

export const scheduledTaskApi = {
  list(): Promise<ScheduledTaskListResponse> { return authFetch<ScheduledTaskListResponse>(BASE); },
  create(data: ScheduledTaskInput): Promise<ScheduledTask> {
    return authFetch<ScheduledTask>(BASE, { method: "POST", body: JSON.stringify(data) });
  },
  update(id: string, data: Partial<ScheduledTaskInput>): Promise<ScheduledTask> {
    return authFetch<ScheduledTask>(`${BASE}/${id}`, { method: "PATCH", body: JSON.stringify(data) });
  },
  remove(id: string): Promise<void> { return authFetch<void>(`${BASE}/${id}`, { method: "DELETE" }); },
};
```
Query-string style (`revealedFile.ts:98-118`): build `URLSearchParams`, only `set` truthy params, `favorites_only` as `"true"`.

**Optional in-memory TTL cache** (spec `state-management.md`): `roleApi` keeps `Map<url,{data,expiresAt,authScope,promise}>` (TTL 10 s, invalidated when `getAccessToken()` changes) — only for hot, read-mostly lists (e.g. KB list for the composer mention popup).

**Multipart upload with progress:** `authFetch` + `FormData` works but has **no progress**. The only progress implementation is `services/api/upload.ts` `uploadApi.uploadFile(file, {folder, onProgress(progress, loaded, total)}) → {promise, abort}` — raw `XMLHttpRequest` (@92-210): refreshes token via `getValidAccessToken()`, one 401 retry via `refreshAccessToken()`, `xhr.withCredentials = true`, error mapping to `ApiRequestError` via `getErrorEnvelope`, `abort()` handle. It is **hard-wired to `/api/upload/file?folder=…`**; for KB write a sibling `uploadKbDocument(kbId, file, {onProgress})` copying this structure (unit-test-free today). Client dedupe: `uploadApi.checkFile(hash,size,name,mime)` + `useFileUpload` (SHA-256 via Web Crypto with `workers/sha256.ts` fallback; `useFileUpload.ts:201-330`) — reuse the hashing helper, not the hook (it is attachment-specific; it reads global `UploadConfig` limits via `uploadApi.getConfig`).

---

## 5. State management & data fetching

* **No Redux/Zustand/React-Query/SWR.** Layers: `useState/useReducer` (default) → React Context (`AuthContext`/`useAuth`, `SettingsContext`/`useSettingsContext`, `ThemeContext`) → `createSingletonStore` (`components/chat/ChatMessage/items/createSingletonStore.ts`: `get/set/subscribe`, used for tool-panel/preview/sidebar-history stores) → API-module TTL caches. Spec: do **not** put server data in global state; keep KB lists in the panel (`useState`) or a feature hook.
* **Fetch-on-mount pattern** (cancelled flag): `RevealedFilesPanel.tsx:57-70`:
  ```tsx
  useEffect(() => {
    let cancelled = false;
    projectApi.list().then((d) => { if (!cancelled) setProjects(d); }).catch(() => {});
    return () => { cancelled = true; };
  }, []);
  ```
  Panel pattern: `const load = useCallback(async () => { try { … } catch (e) { toast.error(e instanceof Error ? e.message : String(e)); } finally { setLoading(false); } }, []); useEffect(() => { void load(); }, [load]);` (`ScheduledTaskPanel/index.tsx:48-69`).
* **Polling while something is in-flight** (copy for document status): `TaskRunsDrawer.tsx:52-60`:
  ```tsx
  const hasRunning = runs.some((r) => r.status === "running" || r.status === "pending");
  useEffect(() => {
    if (!hasRunning) return;
    const id = window.setInterval(() => void load(0), 5000);
    return () => window.clearInterval(id);
  }, [hasRunning, load]);
  ```
  Another: `persona/useWeComStatusPoll.ts` (15 s, `enabled` flag, `presetIdsRef` to keep the interval stable, `cancelled` guard). Backoff to 30 s (Yuxi-style) is **not** implemented anywhere — you write it; keep a ref-based timer and clear on unmount. Also refresh on `visibilitychange` is not an existing pattern (UNCONFIRMED — not found).
* **Optimistic / local patching:** `ScheduledTaskPanel` `replace(task)` upserts the server response into local array after PATCH/POST; `useRevealedFiles.toggleFavorite` patches one row from the response (no rollback; failure swallowed). Prefer "apply server response" over blind optimism; use a `busyId` state to disable a row's buttons during a mutation (`ScheduledTaskPanel/index.tsx:46,76-90`).
* **Pagination hooks:** `useClientPagination.ts`, `usePersonaPlaza` (server paging with `page` 1-based), `useRevealedFiles` (infinite scroll, `PAGE_SIZE = 20`, `paramsRef` + `JSON.stringify(params)` to avoid refetch loops).
* **Cross-component refresh events:** `window` `CustomEvent`s with a constants + dispatch/subscribe helper module — `utils/scheduledTaskEvents.ts` (`SCHEDULED_TASK_REFRESH_EVENT = "scheduled-task:refresh"`, `dispatchScheduledTaskRefresh()`, `subscribeScheduledTaskRefresh(handler)` returns unsubscribe); also `hooks/personaPresetEvents.ts`, `sessionTitleEvents`. Copy this for `utils/knowledgeBaseEvents.ts` (`kb:refresh`) — "never inline the event string" (spec `scheduled-task-panel.md`).
* **WebSocket client:** `hooks/useWebSocket.ts` (357 lines). One socket per hook instance: `buildWebSocketUrl("/ws")`, sends `{type:"auth", token}` on open, waits for `auth:ok`, exponential backoff 1 s ×1.5 → 30 s, auth-failure cooldown. Dispatch is a hard-coded `if (message.type === …)` chain @182-199 for exactly three events: `task:complete`, `notification:feedback`, `scheduled_task:run_finished`; callbacks are passed as options (`onTaskComplete`, `onFeedbackNotification`, `onScheduledTaskFinished`) and held in refs. Adding `kb:*` = new option + new `if` + typed message in this hook, and a handler in `useWebSocketNotifications.tsx` (which is the only consumer, mounted in `ChatAppContent.tsx:600` with `enabled: isAuthenticated`). Handler example for display (`useWebSocketNotifications.tsx:44-83`): `dispatchScheduledTaskRefresh()` + `appNotificationService.notify({type:"task", title, body, route, dedupeKey})` + `toast.success(...)` + browser Notification fallback (spec `hook-guidelines.md` "three-layer display"). Reminder: not mounted on `/knowledge`.

---

## 6. i18n

* **Library:** `i18next` ^25 + `react-i18next` ^16; init in `src/i18n/index.ts`: resources `en, zh, ja, ko, ru` (static JSON imports), `lng` = `localStorage.language` → `navigator.language` → `"en"`, **`fallbackLng: "en"`**, `escapeValue: false`.
* **Files:** `src/i18n/locales/{zh,en,ja,ko,ru}.json` (2-space JSON, ~147–199 KB each). Key counts (top-level): zh 66, en 66, ja/ko/ru 62 — they lack whole namespaces (`scheduledTask`, `scheduledTaskAdmin`, `builtinSkills`, `openSandboxAdmin`). **Fallback behaviour:** a missing key in ja/ko/ru resolves from `en`; if also missing in `en`, i18next returns the `defaultValue`/2nd-arg string, else the key itself. So for the KB feature: write complete `zh` + `en`; **skip ja/ko/ru** (matches the PRD "ja/ko/ru 先用英文兜底") and add a key-parity test for `["en","zh"]` only.
* **Key naming:** one top-level namespace per feature, camelCase, nested: `fileLibrary.context.goToSession`, `scheduledTask.toast.created`, `scheduledTask.runStatus.<status>`, `personaPresets.*`, `roles.maxChannels`, `memory.*`. Reuse `common.*` (`common.save/cancel/delete/edit/close/refresh/loading/loadMore/confirm/…` — there are ~70 keys incl. `common.noPermission`). Per-page: `nav.<tab>` (+ `navDesc`), `seo.<page>.{title,description}`. Backend setting descriptions are keys `settingDesc.<SETTING_KEY>` (219 entries) and categories `categories.<category>`; error codes → `backendErrors.*`/feature namespace. Suggested: new top-level `knowledge` (all UI), `nav.knowledge`, `seo.knowledge`, `settingDesc.KB_*`, `roles.maxKbCount…`.
* **Calling:** `const { t } = useTranslation();` (or `i18n` from `useTranslation` for `i18n.language`); `t("ns.key")`, `t("ns.key", "zh default")`, `t("ns.key", { count, name, defaultValue: "…{{name}}…" })`; dynamic: ``t(`scheduledTask.runStatus.${status}`)``; outside React: `import i18n from "i18next"; i18n.t(...)` (`fetch.ts`, `useAgent.ts`). `scripts/extract-i18n.ts` regex only recognizes literal first args `t("key"` + `,`/`)` and lists dynamic template keys as unknowns (known dynamic prefixes whitelist at its top).
* File order: roughly alphabetical but not strict (new namespaces were appended; `scheduledTaskAdmin` is the last key). Insert `knowledge` anywhere at top level; keep JSON valid (the i18n tests `JSON.parse` the files).

---

## 7. `/files` page (agent-produced files)

* **Route/tab:** `App.tsx:257-264,597-604` (`FilesPage` → `AppContent activeTab="files"`; route wrapped in `ProtectedRoute loadingComponent={<FilesPageSkeleton />}`); `TabContent.tsx:33-37,88` maps `files` → `components/fileLibrary/RevealedFilesPanel.tsx` (`RevealedFilesPage` lazy alias) (exported through `fileLibrary/index.ts`).
* **Component tree:** `RevealedFilesPanel` (state: search, filter, sort, viewMode `grid|list`, selectedProject, favoritesOnly; previews: `DocumentPreview`, `ImageViewer`, `VideoViewer`, `ExcalidrawDirectViewer`) → `Toolbar` (`components/Toolbar.tsx`, search `<input>` @323, filter chips, sort dropdown, project filter) → `SessionGroup` (groups by session) → `RevealedFileCard` (grid/list switch) → `GridCard`/`ListCard` → `FileContextMenu`.
* **Data:** `hooks/useRevealedFiles.ts` `useRevealedFilesGrouped(params)` → `revealedFileApi.listGrouped` (`GET /api/files/revealed/grouped`), infinite scroll `PAGE_SIZE 20`, returns `{sessionGroups, isLoading, isLoadingMore, hasMore, loadMoreRef, toggleFavorite}`. Other endpoints: `GET /api/files/revealed`, `PATCH /api/files/revealed/{id}/favorite`, `/stats`, `/sessions` (`services/api/revealedFile.ts`).
* **File object** (`revealedFile.ts:23-65`): `RevealedFileItem { id, file_key, file_name, file_type: "image"|"video"|"document"|"code"|"project"|"other", mime_type: string|null, file_size, url: string|null (signed; use getFullUrl), session_id, session_name, trace_id, project_id, user_id, source: "reveal_file"|"reveal_project", description, original_path, created_at, is_favorite, card_preview?, project_meta? }`. `file_type === "project"` items are multi-file projects (no `url`; `project_meta.files`) — **not importable** as a single doc; hide the KB action for them (like `hasUrl` gating below).
* **Where actions live — `components/fileLibrary/components/FileContextMenu.tsx`** (the only action menu; opened by right-click or the `MoreHorizontal` button in both cards via `useContextMenu()`, `hooks/useContextMenu.ts`, fixed-position at the click, closes on document click, repositions to stay in viewport). Items are an array `{icon, label, action}` (@28-73); conditional spreads for `hasUrl`:
  ```tsx
  const items: { icon: typeof MessageSquare; label: string; action: () => void }[] = [
    { icon: MessageSquare, label: t("fileLibrary.context.goToSession"), action: () => onGoToSession(file.session_id, file) },
    { icon: Star, label: file.is_favorite ? t("fileLibrary.context.unfavorite") : t("fileLibrary.context.favorite"), action: () => onToggleFavorite(file) },
    ...(hasUrl ? [{ icon: Download, label: t("fileLibrary.context.download"), action: () => {/* anchor click */} }] : []),
    ...(hasUrl ? [{ icon: ExternalLink, label: t("fileLibrary.context.openInNewTab"), action: () => window.open(getFullUrl(file.url!), "_blank", "noopener noreferrer") }] : []),
  ];
  ```
  Row markup: `flex items-center gap-2 w-full p-2 rounded-lg hover:bg-theme-bg-subtle cursor-pointer text-[13px] text-theme-text transition-colors` with a 16 px `text-theme-text-secondary` icon.
* **Threading for "转到知识库":** the menu receives only `{menu, menuRef, file, onGoToSession, onToggleFavorite}`. Add `onImportToKnowledgeBase(file)` and thread it **panel → `SessionGroup` (props @21) → `RevealedFileCard` → `GridCard` & `ListCard` → `FileContextMenu`** (all five signatures list the same three callbacks today; `grep` shows no other consumers). Insert the new item after `goToSession`, gated by `hasUrl && enableKnowledgeBase && hasPermission(...)`, icon e.g. `LibraryBig`. The click should open a **KB picker dialog** (new component in `components/knowledge/` using the modal pattern in §3.3; list of writable KBs from `GET …/knowledge` with role ∈ owner/editor) and then call a `knowledgeBaseApi.importFromFile(kbId, {revealed_file_id: file.id})` (server copies + sha256 dedupe per PRD). Show result with `toast.success` and a "查看" link to `/knowledge/{kbId}`. i18n under `fileLibrary.context.addToKnowledge` (+ `knowledge.*` for the picker).
* The panel's own scroll container: `div.flex-1.overflow-y-auto.min-h-0.relative.z-[1]`; container-query wrapper `flex min-h-full flex-col @container`.

---

## 8. Admin area

### 8.1 Settings (schema-driven)

* `components/panels/SettingsPanel.tsx` (1087 lines; route `/settings`, tab `settings`, `Permission.SETTINGS_MANAGE`). The backend returns `SettingsResponse { settings: Record<SettingCategory, SettingItem[]> }` (`types/settings.ts:12-75`); each `SettingItem { key, value, type: string|text|number|boolean|json|select, category, subcategory, description (i18n key), default_value, requires_restart, is_sensitive, frontend_visible, depends_on?, options?, json_schema?, minimum?, maximum?, step? }`. The UI is **generic**: per-category list; each row has an inline editor, `handleSave(setting)` (@346-373) calls `updateSetting(key, value)` from `useSettingsContext()` → `settingsApi.update` (`PUT/PATCH /api/settings/{key}`); `depends_on` hides children; `savedKeys`, reset/reset-all, import/export JSON.
* **To add KB admin settings you mostly add backend definitions** in `src/kernel/config/_definitions_extra.py` (pattern @705-745: `type`, `category`, `subcategory`, `"description": "settingDesc.KEY"`, `default`, `minimum/maximum`, `depends_on`, `frontend_visible: True`). Frontend work is only:
  1. If using a **new category** (`knowledge_base`): add to `type SettingCategory` (`types/settings.ts:12-39`), to `CATEGORY_ORDER` (`SettingsPanel.constants.ts:3-31`), to the `CATEGORY_LABELS` memo (`SettingsPanel.tsx:63-91`: `knowledge_base: t("categories.knowledge_base")`), and `categories.knowledge_base` in locales. (A `Record<SettingCategory,string>` requires an entry or `tsc` fails.) Re-using `SettingCategory.AGENT` with `subcategory: "knowledge_base"` (like `scheduled_task`) needs no frontend change; unknown subcategory labels fall back to the raw key (`SUBCATEGORY_LABELS[key] || key`, @301).
  2. `settingDesc.<KEY>` strings (zh + en) — `t(setting.description)` is rendered @757.
  3. Model-card pickers: settings whose key is in `MODEL_CONFIG_SETTING_KEYS` render a model dropdown filtered by `MODEL_CARD_KIND_FILTER` (`embedding`/`rerank`/`chat`/`transcribe`) — add `KB_EMBEDDING_MODEL_ID`/`KB_RERANK_MODEL_ID` there if admin picks models for KB (existing: `NATIVE_MEMORY_EMBEDDING_MODEL_ID`, `NATIVE_MEMORY_RERANK_MODEL_ID`, `DIFY_KB_RERANK_MODEL_ID`).
  4. A feature flag read by non-admins: add `enableKnowledgeBase: getBooleanSetting("KB_ENABLED")` to `SettingsContext` value + interface (@28-30, @161-163).
* Existing precedent for KB-ish settings: category `dify` (`categories.dify`), `document_parse` (MinerU) — check those definitions before adding duplicates.
* Admin-only pages are gated by `Permission.*` in `ProtectedRoute` and listed in `components/layout/UserMenu.tsx` (`adminItems` @135-154, `sysItems` @156-181, entries `{path,label,icon,show}`) — only needed for a **dedicated** KB admin page (e.g. "知识库管理": list all KBs, takedown public KBs). Scheduled-task admin (`/admin/scheduled-tasks`, `ScheduledTaskAdminPanel`, `services/api/scheduledTaskAdmin.ts`, `PauseReasonDialog`) is the template: cross-user list + search + status filters + `skip/limit` pagination + reason-required action dialog + reuse of a user-side drawer with an injected `fetch` fn.

### 8.2 RoleLimits (per-role quotas)

* Types: `types/auth.ts:140-151` `RoleLimits { max_channels?, max_concurrent_chats?, max_queued_chats?, max_file_size_image|video|audio|document?, max_files?, storage_quota_mb?: number|null; [key: string]: number|null|undefined }` (index signature = extension point); backend `src/kernel/schemas/role.py:11-50` `RoleLimits(BaseModel)` with `extra="allow"` — new keys round-trip without a schema change but should still be declared (documented defaults, `gt=0` validators).
* Editor: `RolesPanel.tsx` → `RoleFormModal` (@43-…) rendered inside `EditorSidebar`. Per-limit state is `useState<number | "">(role?.limits?.X ?? "")` (@59-86); submit builds `const limits: RoleLimits = {}` and for each field `if (v !== "" && v != null) { const n = Number(v); if (!isNaN(n) && n >= 0) limits.key = n; }` (@111-205) then `limits: Object.keys(limits).length > 0 ? limits : undefined` (@206). Blank = "no limit / use global default". Field markup (`es-field`): 
  ```tsx
  <div className="es-field">
    <label className="es-label">{t("roles.maxChannels")}</label>
    <input type="number" min="0" value={maxChannels}
      onChange={(e) => setMaxChannels(e.target.value === "" ? "" : Number(e.target.value))}
      className="glass-input es-input px-3" placeholder={t("roles.maxChannelsPlaceholder")} />
    <p className="es-hint">{t("roles.maxChannelsHint")}</p>
  </div>
  ```
  The upload-limit block (@~385-430) is a collapsible `es-section-title` (ChevronDown toggle, `showUploadLimits`) rendering an array `[{label, value, setter}].map(...)` with i18n `roles.${label}` — **the cheapest way to add a "知识库限额" collapsible section** (`kbMaxCount`, `kbMaxDocsPerKb`, `kbStorageQuotaMb`, `kbMaxFileSizeMb`) in the same shape. Remember pitfall 5 (rebuild-from-state wipes unknown keys), the `n >= 0` vs `> 0` rule (storage quota uses `> 0`), and add `roles.*` keys (zh/en) next to `maxChannels*`, `storageQuotaMb`.
* API: `services/api/role.ts` (`roleApi.list/get/create/update/delete`, 10 s TTL list cache, cleared on mutate). Role list cards do not display limits.
* Enforcement UX: surface backend 4xx quota errors via `ApiRequestError.code` + `backendErrors.ts` mapping; the scheduled-task panel also pre-checks `tasks.length >= limit` using a `limit` returned by the list endpoint (`toast.error(t("scheduledTask.limitReached", {limit, defaultValue}))`) — mirror this: have `GET /knowledge` return `{items, limit, …usage}` so "新建知识库" can disable/explain before the request.

---

## 9. Chat composer & message rendering

### 9.1 Composer (`components/chat/ChatInput.tsx`, 1101 lines; props in `chatInputTypes.ts`)

* `@` mention today = **persona or team selection only**. `hooks/useMentionState.ts` (`findMentionMatch`: `@` at start/after whitespace, no whitespace in query; returns `{isActive, query, atIndex, highlightedIndex}`; `moveHighlight/resetMention/dismissMention`), `hooks/useMentionSearch.ts` (persona list, `personaPresetApi.list({q, skip, limit: 20})`, cancel flag, load-more) / `useTeamMentionSearch.ts`. `ChatInput.tsx:193-196`: `mentionMode = currentAgent === "team" ? "team" : "persona"`; `mentionEnabled` needs `onUsePersonaPreset`/`onSelectTeam`. Popups render @775-801: `<MentionPopup presets highlightedIndex selectedPresetId isLoading isLoadingMore hasMore onSelect onHover onClose onLoadMore placement/>` and `<TeamMentionPopup …/>` (template for a KB popup: `components/chat/TeamMentionPopup.tsx` — `mention-popup-anchor`/`mention-popup`/`mention-popup-list`/`mention-popup-empty` CSS vars `--mention-popup-left/width/bottom/max-height`, skeleton rows `mention-skeleton-*`, `role="option"`, `scrollIntoView` on highlight, mousedown-outside closes). Placement via `getMentionPopupFixedPlacement` (`chatInputViewport.ts`) recomputed on resize/scroll (@337-375).
* Selection removes `@query` from the textarea (`before + after`), restores cursor, calls the callback, `resetMention()` (`applyMentionSelection` @467-487). Keys: ArrowUp/Down via `moveMentionHighlight`; Enter/Tab selects `mentionSearch.presets[mention.highlightedIndex]` (@624-634); Esc `resetMention`. IME: Enter ignored when composing (see slash handling).
* **Selected mention is not stored in the text.** Persona → `onUsePersonaPreset(preset)` → session config (`sessionConfigRef.current.personaPresetId`, wired in `ChatAppContent.tsx:217` as `getPersonaPresetId`). Spec contract (`composer-mention-slash-skills.md`): popups render whenever `mention.isActive` (never gate on `!onMentionQueryChange`); Welcome page filters cards via `onMentionQueryChange`.
* **Slash skills** (`chatInputSlashCommands.ts`; `getSlashCommandQuery`, `getMatchingSlashItems(input, cursor, skills)`, `/goal` first) select skill names into **chips** (`emphasizedSkillNames`) and on send **prefix the message string** (`buildEmphasizedUserMessage`, `chatInputSkillEmphasis.ts`); display side `parseEmphasizedUserMessage` strips the prefix and renders a pill. Chip state lives in `ChatView.tsx:152-172` (because Welcome and in-conversation each mount a ChatInput) and is passed as props (`emphasizedSkillNames,onEmphasizeSkill,onRemoveEmphasizedSkill,onClearEmphasizedSkills,onRestoreEmphasizedSkills` — `ChatView.tsx:464-468`, `chatInputTypes.ts:97-101`). Chips render above the textarea (`ChatInput.tsx:811-840`: `Sparkles` icon, `text-sm font-semibold text-blue-600 dark:text-blue-400 font-serif`, hover-revealed `X` with `aria-label`). **This chip block is the template for "@知识库" chips** (`Library` icon, same removal/restore-on-sandbox-error semantics @515-570).
* **Feature menu (“+”)**: `components/selectors/FeatureMenu.tsx` `type FeaturePanel = "persona"|"team"|"tools"|"skills"|"agent"|null`, panels `SkillSelector`, `ToolSelector` — alternative place for a KB multi-select (`activePanel`/`onActivePanelChange` in `ChatInput.tsx:137,955,998`).
* **Conflict to resolve:** `@` is single-purpose; to add `@知识库` you must either merge KB candidates into one popup (sections "专家 / 知识库") or pick a second trigger. If merged, `useMentionState.resultCountRef` (set by `setMentionResultCount`, @225-238) and Enter/Tab dispatch (@624-634) must index the merged list. The Welcome-page `@` also drives persona card filtering — don't break it (spec).
* `onSend` signature (`chatInputTypes.ts:17-22`): `(message: string, options?: Record<string, boolean|string|number>, attachments?: MessageAttachment[], onAccepted?: () => void) => void | Promise<void>`.

### 9.2 How the message request is built (where `kb_ids` must go)

```
ChatInput.handleSubmit (ChatInput.tsx:515-570)
 → ChatView chatInputProps.onSend (ChatView.tsx:404-412)   // drops `options`; forwards (content, attachments, onAccepted)
 → useAgent.sendMessage (hooks/useAgent.ts:~640-690)       // reads getters: getPersonaPresetId/getDisabledSkills/getEnabledSkills/getDisabledMcpTools/getAgentOptions
 → sessionApi.submitChat(agentId, message, sessionId, agentOptions, attachments, projectId, disabledSkills, disabledMcpTools, personaPresetId, enabledSkills, teamId, goal)   // services/api/session.ts:403-436
 → buildSubmitChatBody({...}) (session.ts:160-211) → POST `${API_BASE}/api/chat/stream?agent_id=…` body {message, session_id, agent_options, attachments, disabled_skills, enabled_skills, persona_preset_id, disabled_mcp_tools, user_timezone?, project_id?, team_id?, goal?}
```
Getters are declared in `hooks/useAgent/types.ts:149-171` (`UseAgentOptions`) and supplied in `ChatAppContent.tsx:~213-220`. Streaming responses come over SSE (`hooks/useAgent/sseConnection.ts`, `@microsoft/fetch-event-source`, `/api/chat/sessions/{id}/stream?run_id=`). Adding `kb_ids?: string[]` = new `getKnowledgeBaseIds` getter + new `submitChat` param (**append at the end**; it already has 12 positional params) + `buildSubmitChatBody` key + the session-metadata echo in `useAgent.ts:~745-760,~800-812` (`conversationConfig`). Add a contract test like `buildSubmitChatBody` tests in `services/api/__tests__` (UNCONFIRMED exact filename; see `ls services/api/__tests__`).

### 9.3 Message rendering & tool calls

* Entry: `components/chat/ChatMessage/index.tsx` (818 lines) renders a message's `parts` via `MessagePartRenderer.tsx` (shared by the main agent and subagents). `MessagePart` union in `types/message.ts` (`text|tool|thinking|subagent|sandbox|todo|summary|recommend_questions|cancelled|token_usage…`); **`ToolPart { type:"tool", id?, name, args: Record<string,unknown>, result?: string|Record<string,unknown>, success?, error?, isPending?, cancelled?, depth?, agent_id?, startedAt?, completedAt? }`** (`types/message.ts:127-141`).
* **Per-tool special renderers** are `if (part.name === "…")` branches in `MessagePartRenderer.tsx:79-248`: `read_file→ReadFileItem`, `reveal_file→FileRevealItem` (wrapped in an anchor div with external-navigation highlight classes), `reveal_project`, `edit_file`, `write_file`, `grep`, `ls`, `glob`, `execute`; then a *predicate + parser* pattern for structured results (`MessagePartRenderer.tsx:224-236`):
  ```tsx
  if (isScheduledTaskDraftToolName(part.name) && !part.isPending && part.success !== false) {
    const draft = parseScheduledTaskDraftResult(part.result);
    if (draft) return <ScheduledTaskDraftCard result={draft} />;
  }
  return <ToolCallItem name={part.name} args={part.args} result={part.result} … />;   // generic fallback
  ```
  Parser module `items/scheduledTaskDraftResult.ts` (`isXToolName(name)` accepts `name` or MCP-prefixed `*:name`; tolerant `isRecord` guards, returns `null` on malformed input so the generic card is the fallback), card `items/ScheduledTaskDraftCard.tsx`, structured-result mutation hook example `items/personaPresetToolResult.ts` (`entity_type`/`action` envelope → dispatch refresh event). **Copy this for KB tool results:** `items/knowledgeBaseResult.ts` (`isKnowledgeBaseToolName`, `parseKnowledgeBaseSearchResult → {query, hits:[{kb_id,kb_name,doc_id,file_name,chunk_id,score,snippet,line_start,line_end,…}]}`), `items/KnowledgeBaseCitationCard.tsx`. Backend tool results should be a JSON object with an `entity_type`-style discriminator, as the draft tool does.
* **Process-group coupling (important):** per `.trellis/spec/frontend/agent-execution-collapse.md`, tool parts are bucketed into a collapsible "execution process" group unless classified as body. `chat/ChatMessage/executionProcess.ts:180-187 isProcessToolPart` returns false only for `reveal_file`, `reveal_project`, and valid exposed draft cards (`isExposedDraftCard` @154-178 mirrors the MessagePartRenderer condition exactly: non-pending, `success !== false`, parseable JSON with `entity_type`). If KB citations should show **outside** the collapsed process group, add a matching clause there with the **same condition** as the renderer, extend `__tests__/executionProcess.test.ts`, and keep `partIndex` ordering (spec: "never hoist groups"). Otherwise KB tool calls render as plain process rows (icon by `classifyGenericToolKind`, @612-640 — first substring hit wins; note `file` hits `read` before `search`, so a tool named `search_file` shows a read icon; `query`/`find` → grep icon, `list` → ls) with details in the side panel via `processStepDetails.tsx` → `ToolResultPanel` (`items/ToolResultPanel.tsx`, `openPersistentToolPanel` from `items/persistentToolPanelState.tsx`). Process step details for a new special tool must be mirrored in `processStepDetails.tsx` (it re-implements each Item's panel payload, "zero loss" rule).
* **Markdown:** `components/chat/ChatMessage/MarkdownContent.tsx` — `MarkdownContent({content, isStreaming?, headingAnchorContext?: {messageId, partIndex}})` (memo; `react-markdown` + `remark-gfm` + `remark-breaks` + `remark-math` + `rehype-katex`, custom components for headings with outline anchors, code blocks via `DeferredCodeMirrorViewer`, mermaid, images via `ImageWithSkeleton`/`ImageViewer`, file links via `getFileLinkInfo` → reveal preview). Used for text, thinking, summary. For citation markers like `[1]` in text you would add a custom `a`/text renderer here (**UNCONFIRMED** whether a link/footnote hook already exists; none found).
* **Existing “source/citation card” UI:** **none.** Dify KB tool results (`src/infra/tool/dify_kb_tool.py`) render through the generic `ToolCallItem` (`ToolCallItem.tsx:~70-269`: `CollapsiblePill` + `ToolResultContent`/`McpBlockPreview`), no Dify-specific frontend code exists besides settings/persona pickers (`DifyKbMultiSelect`, `PersonaEditorModal.tsx:214-1220`). No web-search source cards either. Closest visual refs for a citation card: `RevealedFileCard`/`GridCard` meta styles, `scb__mini-tag`, `AttachmentCard`, and the recommend-question chips (`MessagePartRenderer.tsx:~305-325`: `ring-1 ring-inset ring-theme-border bg-theme-bg-card rounded-xl px-3.5 py-2 text-sm hover:ring-theme-border-hover`).
* User-bubble: `UserMessageBubble.tsx` (pill for emphasized skills via `parseEmphasizedUserMessage`; copy uses visible text). A KB mention shown in the user bubble would need the same encode/parse-prefix trick **or** a structured metadata field on the stored message (UNCONFIRMED how message `metadata` is round-tripped — not investigated).

### 9.4 Checklist for “@知识库” (frontend only)

1. `hooks/useKnowledgeMentionSearch.ts` (copy `useMentionSearch.ts` shape) + `components/chat/KnowledgeMentionPopup.tsx` (copy `TeamMentionPopup`).
2. Chip state in `ChatView.tsx` (beside `emphasizedSkillNames`) + new `ChatInputProps` fields + chip row in `ChatInput.tsx` near @811.
3. Decide trigger conflict (merged popup vs second trigger) — spec test `teamMentionMode.test.ts` asserts popups render without `!onMentionQueryChange`; keep it green.
4. Thread `kb_ids` per §9.2; clear chips on accepted send, restore on sandbox-capacity error (mirror `handleAccepted`/`onRestoreEmphasizedSkills`).

---

## 10. Notifications UI

* **There is no per-user inbox.** The header bell/`nav.notifications` opens `components/notification/NotificationDialog.tsx` (mode `"auto"`/`"manual"`) which lists **admin announcements** from `notificationApi.getActive()` / `getPopupEligible()` (`services/api/notification.ts`: `GET /api/notifications/active[?popup_eligible=true]`, `POST …/{id}/dismiss`); items are `Notification { id, title_i18n, content_i18n (I18nText en/zh/ja/ko/ru), type: info|success|warning|maintenance, start_time, end_time, is_active, popup, … }` (`types/notification.ts`). Badge count `activeNotifCount` in `layout/AppContent/Header.tsx:64-90,278-285`. The admin editor is `/notifications` → `NotificationPanel.tsx` (`Permission.NOTIFICATION_MANAGE`). Announcements have **no link/route field**.
* **Real-time per-user events** go through the WebSocket → `useWebSocketNotifications.tsx` and are surfaced by the three-layer display (spec `hook-guidelines.md`): `appNotificationService.notify({type: "task"|"announcement"|"approval"|"message"|"auth"|"error", title, body?, route?, dedupeKey?, importance?})` (`services/notifications/appNotificationService.ts:6-21`; native runtimes only — silently dropped in a plain browser), `useBrowserNotification().notify(title,{body,onClick,url})` gated by `shouldAttemptBrowserNotification`, and `toast.success/error/custom`. **Deep link = `route` string**: `appNotificationService.setNavigator(route => navigate(route))` is installed in `App.tsx:363-366`; handlers also do `navigate(route)` in `onClick` (`feedbackRoute = "/persona"`, `scheduled: /chat/${session_id}`). So “知识库已分享给你” = new WS message (e.g. `kb:shared {kb_id, kb_name, role, from}`) + toast with a click-through to `/knowledge/{kb_id}?tab=shared` + dispatch a `kb:refresh` window event.
* **Persistence gap:** WS events are fire-and-forget (offline users miss them) and there is no inbox UI. For "发一条站内通知" the options are: (a) reuse the announcement system with a per-user target (needs backend + dialog changes, announcements are global today), or (b) show a "新" badge/dot on the 共享知识库 tab computed from the list endpoint (`shared_at > last_seen`), with the toast as a bonus. Decide with the backend owner. (UNCONFIRMED: backend `src/infra/websocket.py` / `pubsub_hub.py` per-user push API — not read in this task.)

---

## 11. Graph / network visualization libraries (for the future 图谱 view)

From `package.json` dependencies:

| Library | Version | Use today |
|---|---|---|
| `@xyflow/react` (React Flow) | ^12.10.2 | `components/layout/AppContent/MessageOutlinePanel.tsx` (`ReactFlow`, `useReactFlow`, `fitView`, `setViewport`), `components/sop/SopFlow.tsx`/`SopNode.tsx`, CSS imported in `src/main.tsx` |
| `@dagrejs/dagre` | ^3.1.0 | automatic DAG layout (`components/sop/sopLayout.ts`, tests in `sop/__tests__/sopLayout.test.ts`) |
| `recharts` | ^2.15 | analytics charts (`components/panels/analytics/*`) |
| `mermaid` | — | markdown diagrams (`MermaidDiagram.tsx`) |
| `@upsetjs/venn.js` | — | set diagrams |

**Not present:** `g6`, `cytoscape`, `echarts`, `sigma`, `react-force-graph`, `vis-network`, `d3-force`. A knowledge-graph view must therefore either use **React Flow + dagre (already bundled; hierarchical/DAG, interactive pan/zoom, custom nodes)** or add a force-layout dependency (would grow the bundle; `vite.config.ts` `manualChunks` splits vendors — add a chunk if you add one). For ≤ a few hundred nodes React Flow with a precomputed layout is adequate; for dense graphs a canvas lib (UNCONFIRMED choice) is needed. Lazy-load the graph view (`lazy(() => import(...))`, as `TabContent.tsx` does) to keep it out of the main chunk.

---

## 12. Suggested file map for the KB feature (mirrors existing structure)

```
frontend/src/
  components/knowledge/                      # new domain folder (like fileLibrary/, persona/)
    KnowledgeBasePanel.tsx                   # tab host: reads :kbId + ?tab=; PanelHeader + tablist (§3.4)
    KnowledgeBaseListTab.tsx / KnowledgeBaseCard.tsx / CreateKnowledgeBaseDialog.tsx
    KnowledgeBaseDetail.tsx                  # docs table + upload + status badges + retry/delete; chunk preview drawer; retrieval test; settings; ShareDialog
    ShareDialog.tsx                          # paste employee ids → validate → echo → role
    PlazaTab.tsx                             # public KBs + subscribe
    ImportToKnowledgeBaseDialog.tsx          # used by FileContextMenu
    hooks/useKnowledgeBases.ts, useKbDocuments.ts (polling), 
    __tests__/
  components/chat/KnowledgeMentionPopup.tsx, hooks/useKnowledgeMentionSearch.ts
  components/chat/ChatMessage/items/knowledgeBaseResult.ts, KnowledgeBaseCitationCard.tsx
  services/api/knowledgeBase.ts              # knowledgeBaseApi + types (+ XHR upload with progress)
  utils/knowledgeBaseEvents.ts               # refresh event constants/helpers
  i18n/locales/{zh,en}.json                  # knowledge.*, nav.knowledge, seo.knowledge, settingDesc.KB_*, roles.kb*
  i18n/__tests__/knowledgeKeys.test.ts
```
Edits to existing files: `types.ts`, `TabContent.tsx`, `App.tsx`, `SessionSidebar.tsx`, `SidebarRail.tsx`, `SessionListContent.tsx`, `SettingsContext.tsx`, `types/auth.ts` (+ backend permission), `types/settings.ts`/`SettingsPanel*` (if new category), `RolesPanel.tsx`, `fileLibrary/*` (5 files), `ChatInput.tsx`, `chatInputTypes.ts`, `ChatView.tsx`, `ChatAppContent.tsx`, `hooks/useAgent.ts` + `useAgent/types.ts`, `services/api/session.ts`, `MessagePartRenderer.tsx`, `executionProcess.ts`, `useWebSocket.ts`/`useWebSocketNotifications.tsx` (optional), `utils/backendErrors.ts`, `nginx/nginx.conf`.

---

## 13. UNCONFIRMED / not investigated

* Exact glob form for running all frontend tests (`tsx --test "<glob>"`); only single-file runs were executed. No CI workflow runs frontend tests/lint (`.github/workflows/docker-build.yml` only builds images — not read in full).
* Whether production nginx is the repo's `nginx/nginx.conf` (no body-size config found in `k8s/`, `deploy/`, `Dockerfile`).
* Backend per-user WebSocket push API and message metadata round-trip for displaying a KB mention in the user bubble.
* `services/api/__tests__` file list for `buildSubmitChatBody` contract tests (not listed).
* Whether markdown `[n]` citation rendering hooks exist in `MarkdownContent.tsx` (only the top ~300 lines were read; none seen).
* Mobile behaviour of `EditorSidebar` for wide forms (bottom-sheet via `useSidebarPanel`, swipe-to-close) was read in signature only.
* I did not run `pnpm lint`, `tsc`, `vite build` or start a dev server (read-only task). Baseline typecheck result is quoted from `research/baseline-tests.md`.
