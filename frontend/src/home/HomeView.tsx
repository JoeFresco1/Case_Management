import { useEffect, useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { api, ApiClientError } from "../api/client";
import type {
  DashboardResponse,
  EffectivePreferencesResponse,
} from "../api/client";
import { ImportPreview } from "../imports/ImportPreview";
import { newCaseUrl } from "../shell/demoWorkspaces";
import styles from "./HomeView.module.css";

/**
 * Worker home dashboard (T5 worker UX / C-7).
 *
 * Covers the `specs/002-leftovers/spec.md` T5 worker-UX paragraph as a
 * modern web UX (not a cloned ribbon):
 *
 * - configurable home/dashboard (widget checklist persisted via
 *   `PUT /api/v1/home/dashboard`);
 * - favorites (pinned case ids with links into the case workspace);
 * - personal layout shortcuts (label + target, add/remove);
 * - list grouping/sorting (dashboard `grouping`/`sort`);
 * - task-pane equivalent (dockable case context via
 *   `task_pane_case_id`, side from the `task_pane_side` preference);
 * - recent/history (local tray bounded by the `recent_limit`
 *   preference — browser-local, never sent to the server);
 * - import preview (the `import_preview` widget embeds the
 *   dry-run/commit surface);
 * - user preferences + admin-enforced preference policy (stored vs
 *   effective; locked keys render the admin value with a badge and
 *   disable editing — admin policy overrides user preference).
 *
 * All wire shapes come from the generated OpenAPI contract; this view
 * never hand-types API payloads. Identity and idempotency stay in the
 * shared client (`Idempotency-Key` on every PUT).
 */

const ALLOWED_WIDGETS = [
  "pinned_cases",
  "queue_summary",
  "overdue_tasks",
  "recent_history",
  "favorites",
  "shortcuts",
  "import_preview",
  "task_pane",
] as const;

type WidgetKey = (typeof ALLOWED_WIDGETS)[number];

const GROUPING_OPTIONS = ["none", "queue", "priority", "status"] as const;
const SORT_OPTIONS = ["recent", "due", "priority"] as const;

const PREFERENCE_OPTIONS: Record<string, readonly string[]> = {
  theme: ["light", "dark", "system"],
  density: ["comfortable", "compact"],
  default_grouping: ["none", "queue", "priority", "status"],
  default_sort: ["recent", "due", "priority"],
  task_pane_side: ["left", "right", "hidden"],
  recent_limit: ["5", "10", "20"],
  page_size: ["25", "50", "100"],
};

const PREFERENCE_KEYS = Object.keys(PREFERENCE_OPTIONS);

const RECENTS_STORAGE_KEY = "worker-home:recents";

interface RecentEntry {
  caseId: string;
  viewedAt: string;
}

/** Allow saved workspace paths to become links only when they resolve to this origin. */
function isWorkspacePath(target: string): boolean {
  try {
    return (
      target.startsWith("/") &&
      new URL(target, window.location.origin).origin === window.location.origin
    );
  } catch {
    return false;
  }
}

function readRecents(): RecentEntry[] {
  try {
    const raw = localStorage.getItem(RECENTS_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw) as RecentEntry[];
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(
      (entry) =>
        typeof entry?.caseId === "string" &&
        typeof entry?.viewedAt === "string",
    );
  } catch {
    return [];
  }
}

function writeRecents(entries: RecentEntry[]): void {
  try {
    localStorage.setItem(RECENTS_STORAGE_KEY, JSON.stringify(entries));
  } catch {
    // why: private-mode quota errors must not break the home view.
  }
}

function effectiveValue(
  effective: EffectivePreferencesResponse | undefined,
  key: string,
  fallback: string,
): string {
  const found = effective?.preferences.find((entry) => entry.key === key);
  return found?.value ?? fallback;
}

function isLocked(
  effective: EffectivePreferencesResponse | undefined,
  key: string,
): boolean {
  return (
    effective?.preferences.find((entry) => entry.key === key)?.locked ?? false
  );
}

async function loadDashboard(): Promise<DashboardResponse | null> {
  try {
    return await api.workerHome.getDashboard();
  } catch (error) {
    // why: 404 means "never saved" — the home view renders editable
    // defaults instead of an error banner.
    if (error instanceof ApiClientError && error.status === 404) return null;
    throw error;
  }
}

export function HomeView() {
  const queryClient = useQueryClient();
  const [recents, setRecents] = useState<RecentEntry[]>([]);
  const [favoriteInput, setFavoriteInput] = useState("");
  const [shortcutLabel, setShortcutLabel] = useState("");
  const [shortcutTarget, setShortcutTarget] = useState("");
  const [taskPaneInput, setTaskPaneInput] = useState("");
  const [dashboardDraft, setDashboardDraft] = useState<{
    widgets: string[];
    grouping: string;
    sort: string;
  } | null>(null);
  const [preferenceDraft, setPreferenceDraft] = useState<Record<
    string,
    string
  > | null>(null);
  const [saveMessage, setSaveMessage] = useState<string | null>(null);

  const latestCases = useQuery({
    queryKey: ["cases", "list", { limit: 5, offset: 0 }],
    queryFn: () => api.listCases({ limit: 5, offset: 0 }),
    staleTime: 15_000,
  });
  const cases = latestCases.data?.items ?? [];

  const dashboardQuery = useQuery({
    queryKey: ["home", "dashboard"],
    queryFn: loadDashboard,
    staleTime: 15_000,
  });
  const storedQuery = useQuery({
    queryKey: ["home", "preferences", "stored"],
    queryFn: api.workerHome.getPreferences,
    staleTime: 15_000,
  });
  const effectiveQuery = useQuery({
    queryKey: ["home", "preferences", "effective"],
    queryFn: api.workerHome.getEffectivePreferences,
    staleTime: 15_000,
  });
  const policyQuery = useQuery({
    queryKey: ["home", "policy"],
    queryFn: api.workerHome.getPolicy,
    staleTime: 30_000,
  });

  const dashboard = dashboardQuery.data ?? null;
  const effective = effectiveQuery.data;

  const widgets = useMemo<string[]>(
    () => dashboardDraft?.widgets ?? dashboard?.widgets ?? [],
    [dashboardDraft, dashboard],
  );
  const grouping = dashboardDraft?.grouping ?? dashboard?.grouping ?? "none";
  const sort = dashboardDraft?.sort ?? dashboard?.sort ?? "recent";
  const favorites = useMemo<string[]>(
    () => [...(dashboard?.favorites ?? [])],
    [dashboard],
  );
  const shortcuts = useMemo(
    () => [...(dashboard?.shortcuts ?? [])],
    [dashboard],
  );
  const taskPaneCaseId = dashboard?.task_pane_case_id ?? null;

  const recentLimit = Number.parseInt(
    effectiveValue(effective, "recent_limit", "10"),
    10,
  );
  const taskPaneSide = effectiveValue(effective, "task_pane_side", "right");

  useEffect(() => {
    setRecents(
      readRecents().slice(0, Number.isNaN(recentLimit) ? 10 : recentLimit),
    );
  }, [recentLimit]);

  useEffect(() => {
    // why: keep the task-pane input in sync after the dashboard loads
    // so the worker sees the persisted docked case without retyping.
    if (dashboard && taskPaneInput === "") {
      setTaskPaneInput(dashboard.task_pane_case_id ?? "");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dashboard]);

  useEffect(() => {
    // why: seed the preference form from the effective view once so
    // locked admin values are visible even before the stored row loads.
    if (effective && preferenceDraft === null) {
      const seed: Record<string, string> = {};
      for (const key of PREFERENCE_KEYS) {
        seed[key] = effectiveValue(effective, key, PREFERENCE_OPTIONS[key][0]);
      }
      setPreferenceDraft(seed);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [effective]);

  const taskPaneQuery = useQuery({
    queryKey: ["home", "task-pane", "case", taskPaneCaseId],
    queryFn: () => api.getCase(String(taskPaneCaseId)),
    enabled: Boolean(taskPaneCaseId),
    staleTime: 15_000,
  });

  const saveDashboard = useMutation({
    mutationFn: api.workerHome.saveDashboard,
    onSuccess: (saved) => {
      setDashboardDraft(null);
      setSaveMessage("Dashboard saved.");
      void queryClient.invalidateQueries({ queryKey: ["home", "dashboard"] });
      // why: docking a case is an explicit "visit" — record it in the
      // local recent/history tray bounded by `recent_limit`.
      if (saved.task_pane_case_id) {
        const next: RecentEntry[] = [
          {
            caseId: saved.task_pane_case_id,
            viewedAt: new Date().toISOString(),
          },
          ...readRecents().filter((r) => r.caseId !== saved.task_pane_case_id),
        ].slice(0, Number.isNaN(recentLimit) ? 10 : recentLimit);
        writeRecents(next);
        setRecents(next);
      }
    },
    onError: (error) => {
      setSaveMessage(
        error instanceof ApiClientError
          ? error.message
          : "Dashboard save failed.",
      );
    },
  });

  const savePreferences = useMutation({
    mutationFn: api.workerHome.savePreferences,
    onSuccess: () => {
      setSaveMessage("Preferences saved (admin locks still apply).");
      void queryClient.invalidateQueries({
        queryKey: ["home", "preferences"],
      });
    },
    onError: (error) => {
      setSaveMessage(
        error instanceof ApiClientError
          ? error.message
          : "Preferences save failed.",
      );
    },
  });

  function toggleWidget(widget: WidgetKey): void {
    const current = new Set(widgets);
    if (current.has(widget)) {
      current.delete(widget);
    } else {
      current.add(widget);
    }
    setDashboardDraft({
      widgets: ALLOWED_WIDGETS.filter((w) => current.has(w)),
      grouping,
      sort,
    });
  }

  function persistDashboard(overrides?: {
    favorites?: string[];
    shortcuts?: { label: string; target: string }[];
    taskPane?: string | null;
  }): void {
    setSaveMessage(null);
    saveDashboard.mutate({
      widgets,
      favorites: overrides?.favorites ?? favorites,
      shortcuts: overrides?.shortcuts ?? shortcuts,
      grouping,
      sort,
      task_pane_case_id:
        overrides?.taskPane !== undefined
          ? overrides.taskPane === ""
            ? null
            : overrides.taskPane
          : taskPaneCaseId,
    });
  }

  function addFavorite(): void {
    const candidate = favoriteInput.trim();
    if (!candidate) return;
    persistDashboard({ favorites: [...favorites, candidate] });
    setFavoriteInput("");
  }

  function removeFavorite(caseId: string): void {
    persistDashboard({
      favorites: favorites.filter((id) => id !== caseId),
    });
  }

  function addShortcut(): void {
    const label = shortcutLabel.trim();
    const target = shortcutTarget.trim();
    if (!label || !target) return;
    persistDashboard({
      shortcuts: [...shortcuts, { label, target }],
    });
    setShortcutLabel("");
    setShortcutTarget("");
  }

  function removeShortcut(index: number): void {
    persistDashboard({
      shortcuts: shortcuts.filter((_, i) => i !== index),
    });
  }

  function dockTaskPane(): void {
    persistDashboard({ taskPane: taskPaneInput.trim() });
  }

  function undockTaskPane(): void {
    setTaskPaneInput("");
    persistDashboard({ taskPane: null });
  }

  function clearRecents(): void {
    writeRecents([]);
    setRecents([]);
  }

  function persistPreferences(): void {
    if (!preferenceDraft) return;
    setSaveMessage(null);
    // why: locked keys are admin-authoritative — omit them from the PUT
    // so the worker cannot mistake a stored value for an override.
    const values: Record<string, string> = {};
    for (const key of PREFERENCE_KEYS) {
      if (isLocked(effective, key)) continue;
      const value = preferenceDraft[key];
      if (value !== undefined) values[key] = value;
    }
    savePreferences.mutate({ values });
  }

  const isLoading =
    dashboardQuery.isLoading ||
    storedQuery.isLoading ||
    effectiveQuery.isLoading ||
    policyQuery.isLoading;
  const loadError =
    dashboardQuery.error ??
    storedQuery.error ??
    effectiveQuery.error ??
    policyQuery.error;

  return (
    <section className={styles.detail} aria-label="Worker home">
      <header className={styles.hero}>
        <div>
          <h1>My workspace</h1>
          <p>Open a case, pick up assigned work, or return to a favorite.</p>
        </div>
        <a className={styles.primaryAction} href={newCaseUrl()}>
          <span aria-hidden="true">+</span> Create case
        </a>
      </header>
      <nav className={styles.workLinks} aria-label="Work areas">
        <a href="/cases">
          Case register <span aria-hidden="true">→</span>
        </a>
        <a href="/tasks">
          My tasks <span aria-hidden="true">→</span>
        </a>
        <a href="/queues">
          Work queues <span aria-hidden="true">→</span>
        </a>
      </nav>

      {isLoading ? <p>Loading home…</p> : null}
      {loadError ? (
        <p className={styles.error} role="alert">
          {loadError instanceof ApiClientError
            ? loadError.message
            : "Failed to load home."}
        </p>
      ) : null}
      {saveMessage ? <p role="status">{saveMessage}</p> : null}

      <datalist id="home-case-options">
        {cases.map((item) => (
          <option key={item.id} value={item.id}>
            {item.reference} · {item.title}
          </option>
        ))}
      </datalist>
      <datalist id="home-shortcut-options">
        {["/cases", "/cases/new", "/tasks", "/queues", "/search"].map(
          (path) => (
            <option key={path} value={path} />
          ),
        )}
      </datalist>
      <div className={styles.cards}>
        <section className={styles.latest} aria-label="Latest cases">
          <h2>Latest cases</h2>
          {latestCases.isLoading ? (
            <p>Loading cases…</p>
          ) : latestCases.isError ? (
            <p className={styles.error} role="alert">
              Could not load the case list.
            </p>
          ) : cases.length === 0 ? (
            <p className={styles.empty}>No cases available.</p>
          ) : (
            <ul className={styles.caseList}>
              {cases.map((item) => (
                <li key={item.id}>
                  <a href={`/cases/${item.id}`}>
                    <strong>{item.title}</strong>
                    <small>{item.reference}</small>
                  </a>
                  <span>{item.status}</span>
                  <button
                    type="button"
                    disabled={
                      !dashboardQuery.isSuccess ||
                      dashboardQuery.isFetching ||
                      saveDashboard.isPending ||
                      favorites.includes(item.id)
                    }
                    onClick={() =>
                      persistDashboard({ favorites: [...favorites, item.id] })
                    }
                  >
                    {favorites.includes(item.id)
                      ? "Favorited"
                      : "Add to favorites"}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>

        <section aria-label="Favorites">
          <h2>Favorites</h2>
          {favorites.length === 0 ? (
            <p className={styles.empty}>No favorites yet.</p>
          ) : (
            <ul className={styles.list}>
              {favorites.map((caseId) => (
                <li key={caseId} className={styles.listItem}>
                  <a href={`/cases/${caseId}`}>
                    {cases.find((item) => item.id === caseId)?.title ??
                      caseId.slice(0, 8)}
                  </a>{" "}
                  <button type="button" onClick={() => removeFavorite(caseId)}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className={styles.controls}>
            <input
              aria-label="Favorite case id"
              list="home-case-options"
              placeholder="Choose or enter a case ID"
              value={favoriteInput}
              onChange={(event) => setFavoriteInput(event.target.value)}
            />
            <button type="button" onClick={addFavorite}>
              Add favorite
            </button>
          </div>
        </section>

        <section aria-label="Shortcuts">
          <h2>Shortcuts</h2>
          {shortcuts.length === 0 ? (
            <p className={styles.empty}>No shortcuts yet.</p>
          ) : (
            <ul className={styles.list}>
              {shortcuts.map((shortcut, index) => (
                <li
                  key={`${shortcut.label}-${index}`}
                  className={styles.listItem}
                >
                  {isWorkspacePath(shortcut.target) ? (
                    <a href={shortcut.target}>{shortcut.label}</a>
                  ) : (
                    <strong>{shortcut.label}</strong>
                  )}{" "}
                  · <code>{shortcut.target}</code>{" "}
                  <button type="button" onClick={() => removeShortcut(index)}>
                    Remove
                  </button>
                </li>
              ))}
            </ul>
          )}
          <div className={styles.controls}>
            <input
              aria-label="Shortcut label"
              placeholder="Shortcut name"
              value={shortcutLabel}
              onChange={(event) => setShortcutLabel(event.target.value)}
            />
            <input
              aria-label="Shortcut target"
              list="home-shortcut-options"
              placeholder="Choose a destination"
              value={shortcutTarget}
              onChange={(event) => setShortcutTarget(event.target.value)}
            />
            <button type="button" onClick={addShortcut}>
              Add shortcut
            </button>
          </div>
        </section>

        <section aria-label="Task pane">
          <h2>Task pane</h2>
          <p className={styles.meta}>
            Pane position · <strong>{taskPaneSide}</strong>
            {taskPaneSide === "hidden" ? " (pane hidden by preference)" : null}
          </p>
          <div className={styles.controls}>
            <input
              aria-label="Task pane case id"
              list="home-case-options"
              placeholder="Choose or enter a case ID"
              value={taskPaneInput}
              onChange={(event) => setTaskPaneInput(event.target.value)}
            />
            <button type="button" onClick={dockTaskPane}>
              Dock
            </button>
            <button type="button" onClick={undockTaskPane}>
              Undock
            </button>
          </div>
          {taskPaneCaseId ? (
            <article aria-label="Docked case">
              {taskPaneQuery.isLoading ? (
                <p>Loading docked case…</p>
              ) : taskPaneQuery.error ? (
                <p className={styles.error}>
                  {taskPaneQuery.error instanceof ApiClientError
                    ? taskPaneQuery.error.message
                    : "Failed to load docked case."}
                </p>
              ) : taskPaneQuery.data ? (
                <p className={styles.meta}>
                  Docked:{" "}
                  <a href={`/cases/${taskPaneQuery.data.id}`}>
                    {taskPaneQuery.data.reference}
                  </a>{" "}
                  · {taskPaneQuery.data.status} · {taskPaneQuery.data.priority}
                </p>
              ) : null}
            </article>
          ) : (
            <p className={styles.empty}>No case docked.</p>
          )}
        </section>

        <section aria-label="Recent history">
          <h2>
            Recent history{" "}
            <span className={styles.meta}>(local, last {recentLimit})</span>
          </h2>
          {widgets.includes("recent_history") || recents.length > 0 ? (
            <>
              {recents.length === 0 ? (
                <p className={styles.empty}>
                  No recents yet — docking a case records it here.
                </p>
              ) : (
                <ul className={styles.list}>
                  {recents.map((entry) => (
                    <li key={entry.caseId} className={styles.listItem}>
                      <a href={`/cases/${entry.caseId}`}>
                        <code>{entry.caseId.slice(0, 8)}</code>
                      </a>{" "}
                      <span className={styles.meta}>{entry.viewedAt}</span>
                    </li>
                  ))}
                </ul>
              )}
              <button type="button" onClick={clearRecents}>
                Clear recents
              </button>
            </>
          ) : (
            <p className={styles.empty}>
              Enable the <code>recent_history</code> widget to show recents
              here.
            </p>
          )}
        </section>

        {widgets.includes("import_preview") ? (
          <section aria-label="Import preview">
            <ImportPreview />
          </section>
        ) : null}
      </div>
      <details className={styles.settings}>
        <summary>Workspace settings</summary>
        <div className={styles.settingsBody}>
          <section aria-label="Dashboard layout">
            <h2>Dashboard layout</h2>
            <fieldset>
              <legend>Widgets</legend>
              {ALLOWED_WIDGETS.map((widget) => (
                <label key={widget} className={styles.widget}>
                  <input
                    type="checkbox"
                    checked={widgets.includes(widget)}
                    onChange={() => toggleWidget(widget)}
                  />{" "}
                  {widget.replaceAll("_", " ")}
                </label>
              ))}
            </fieldset>
            <div className={styles.controls}>
              <label>
                Grouping{" "}
                <select
                  aria-label="Default grouping"
                  value={grouping}
                  onChange={(event) =>
                    setDashboardDraft({
                      widgets,
                      grouping: event.target.value,
                      sort,
                    })
                  }
                >
                  {GROUPING_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Sort{" "}
                <select
                  aria-label="Default sort"
                  value={sort}
                  onChange={(event) =>
                    setDashboardDraft({
                      widgets,
                      grouping,
                      sort: event.target.value,
                    })
                  }
                >
                  {SORT_OPTIONS.map((option) => (
                    <option key={option} value={option}>
                      {option}
                    </option>
                  ))}
                </select>
              </label>
              <button
                type="button"
                onClick={() => persistDashboard()}
                disabled={saveDashboard.isPending}
              >
                {saveDashboard.isPending ? "Saving…" : "Save layout"}
              </button>
            </div>
            {dashboard === null && !dashboardQuery.isLoading ? (
              <p className={styles.empty}>
                No dashboard saved yet — pick widgets and save your layout.
              </p>
            ) : null}
          </section>

          <section aria-label="Preferences">
            <h2>Preferences</h2>
            <p className={styles.meta}>
              Your administrator manages locked preferences.
            </p>
            {PREFERENCE_KEYS.map((key) => {
              const locked = isLocked(effective, key);
              const value =
                preferenceDraft?.[key] ??
                effectiveValue(effective, key, PREFERENCE_OPTIONS[key][0]);
              return (
                <div key={key} className={styles.preference}>
                  <label>
                    {key.replaceAll("_", " ")}{" "}
                    <select
                      aria-label={`Preference ${key}`}
                      value={value}
                      disabled={locked}
                      onChange={(event) =>
                        setPreferenceDraft({
                          ...(preferenceDraft ?? {}),
                          [key]: event.target.value,
                        })
                      }
                    >
                      {PREFERENCE_OPTIONS[key].map((option) => (
                        <option key={option} value={option}>
                          {option}
                        </option>
                      ))}
                    </select>
                  </label>
                  {locked ? (
                    <span aria-label={`${key} locked by admin`}>
                      🔒 admin-locked
                    </span>
                  ) : null}
                </div>
              );
            })}
            <button
              type="button"
              onClick={persistPreferences}
              disabled={savePreferences.isPending}
            >
              {savePreferences.isPending ? "Saving…" : "Save preferences"}
            </button>
          </section>

          <section aria-label="Admin preference policy">
            <h2>Admin preference policy</h2>
            {policyQuery.data ? (
              <>
                <p className={styles.meta}>
                  Locked:{" "}
                  {Object.keys(policyQuery.data.locked).length === 0
                    ? "(none)"
                    : Object.entries(policyQuery.data.locked)
                        .map(([key, value]) => `${key}=${value}`)
                        .join(", ")}
                </p>
                <p className={styles.meta}>
                  Allowed:{" "}
                  {Object.keys(policyQuery.data.allowed).length === 0
                    ? "(none)"
                    : Object.entries(policyQuery.data.allowed)
                        .map(([key, values]) => `${key}=[${values.join("|")}]`)
                        .join(", ")}
                </p>
              </>
            ) : (
              <p className={styles.empty}>No policy published.</p>
            )}
          </section>
        </div>
      </details>
    </section>
  );
}

export default HomeView;
