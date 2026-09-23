/**
 * Worker home dashboard and preference endpoints (T5 / C-7).
 */
import type {
  DashboardResponse,
  EffectivePreferencesResponse,
  PreferencePolicyResponse,
  SaveDashboardRequest,
  SavePreferencesRequest,
  StoredPreferencesResponse,
} from "./types";
import { request } from "./http";

/**
 * Worker home dashboard + preferences (T5 worker UX / C-7).
 *
 * The dashboard holds the configurable home layout (widgets,
 * favorites, shortcuts, grouping/sorting, dockable task-pane case).
 * Preferences are personal values merged server-side through the
 * admin-enforced policy — callers must render `effective` (not
 * `stored`) and surface `locked` so workers see admin overrides.
 */
export const workerHome = {
  /** Read the actor's home dashboard config (404 when never saved). */
  getDashboard: () => request<DashboardResponse>("/home/dashboard"),
  /** Replace the actor's home dashboard config (idempotent). */
  saveDashboard: (body: SaveDashboardRequest) =>
    request<DashboardResponse>("/home/dashboard", {
      method: "PUT",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Read the actor's stored preference values (pre-policy). */
  getPreferences: () =>
    request<StoredPreferencesResponse>("/home/preferences"),
  /** Replace the actor's preference values (idempotent). */
  savePreferences: (body: SavePreferencesRequest) =>
    request<StoredPreferencesResponse>("/home/preferences", {
      method: "PUT",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Read merged effective preferences (admin policy applied). */
  getEffectivePreferences: () =>
    request<EffectivePreferencesResponse>("/home/preferences/effective"),
  /** Read the tenant preference policy (empty when never published). */
  getPolicy: () =>
    request<PreferencePolicyResponse>("/admin/preference-policy"),
};
