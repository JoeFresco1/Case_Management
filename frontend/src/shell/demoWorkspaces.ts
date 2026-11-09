/** Isolated loopback demo origins, each with a server-selected synthetic actor. */
export function demoWorkspaces() {
  if (
    !import.meta.env.DEV ||
    import.meta.env.VITE_DEMO_WORKSPACES !== "1" ||
    window.location.hostname !== "127.0.0.1"
  )
    return null;
  if (!["5173", "5174", "5175"].includes(window.location.port)) return null;
  return {
    supervisor: "http://127.0.0.1:5173",
    intake: "http://127.0.0.1:5174",
    admin: "http://127.0.0.1:5175",
  };
}

/** Direct demo intake to the isolated origin that serves the intake identity. */
export function newCaseUrl() {
  const workspaces = demoWorkspaces();
  return workspaces && window.location.port === "5173"
    ? workspaces.intake + "/cases/new"
    : "/cases/new";
}
