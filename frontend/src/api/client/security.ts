/**
 * Role and permission administration (P11-US4 / FM-048).
 */
import type { components } from "../types.gen";
import type { RoleSummary } from "./types";
import { request, requestVoid } from "./http";

/** Role and permission administration (P11-US4 / FM-048). */
export const roles = {
  /** List roles for the actor's tenant. */
  list: () => request<RoleSummary[]>("/admin/roles"),
  /** Define a new role with permissions. */
  define: (body: components["schemas"]["DefineRoleRequest"]) =>
    request<RoleSummary>("/admin/roles", {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify(body),
    }),
  /** Replace a role's permission set wholesale. */
  replacePermissions: (roleId: string, permissions: string[]) =>
    request<RoleSummary>(`/admin/roles/${roleId}/permissions`, {
      method: "PUT",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ permissions }),
    }),
  /** Grant one permission (idempotent). */
  grantPermission: (roleId: string, permission: string) =>
    request<RoleSummary>(`/admin/roles/${roleId}/permissions`, {
      method: "POST",
      headers: { "Idempotency-Key": crypto.randomUUID() },
      body: JSON.stringify({ permission }),
    }),
  /** Revoke one permission (idempotent). */
  revokePermission: (roleId: string, permission: string) =>
    request<RoleSummary>(
      `/admin/roles/${roleId}/permissions/${encodeURIComponent(permission)}`,
      {
        method: "DELETE",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      },
    ),
  /** Assign a role to a user. */
  assign: (roleId: string, userId: string) =>
    request<components["schemas"]["AssignmentResponse"]>(
      `/admin/roles/${roleId}/assignments`,
      {
        method: "POST",
        headers: { "Idempotency-Key": crypto.randomUUID() },
        body: JSON.stringify({ user_id: userId }),
      },
    ),
  /** Revoke a role assignment (immediate). */
  revokeAssignment: (roleId: string, userId: string) =>
    requestVoid(
      `/admin/roles/${roleId}/assignments/${encodeURIComponent(userId)}`,
      {
        method: "DELETE",
        headers: { "Idempotency-Key": crypto.randomUUID() },
      },
    ),
  /** Delete a non-reserved role. */
  deleteRole: (roleId: string) =>
    requestVoid(`/admin/roles/${roleId}`, {
      method: "DELETE",
      headers: { "Idempotency-Key": crypto.randomUUID() },
    }),
};
