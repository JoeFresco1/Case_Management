import { useEffect, useState } from "react";
import { Link, NavLink, Outlet, useLocation } from "react-router-dom";
import { QuickOpen } from "./shell/QuickOpen";
import { newCaseUrl } from "./shell/demoWorkspaces";
import { Banner } from "./shell/Banner";
import { Icon, type IconName } from "./shell/Icon";
import styles from "./App.module.css";

type Destination = { path: string; label: string; icon?: IconName };
const groups: { label: string; links: Destination[] }[] = [
  {
    label: "Workspace",
    links: [
      { path: "/home", label: "Overview", icon: "home" },
      { path: "/cases", label: "Cases", icon: "cases" },
      { path: "/tasks", label: "Tasks", icon: "tasks" },
      { path: "/queues", label: "Queues", icon: "queues" },
    ],
  },
  {
    label: "Library",
    links: [
      { path: "/search", label: "Search", icon: "search" },
      { path: "/bundles", label: "Document bundles", icon: "bundles" },
    ],
  },
  {
    label: "Insights",
    links: [
      { path: "/records/log", label: "Records log", icon: "audit" },
      { path: "/dashboard", label: "Dashboard", icon: "dashboard" },
      { path: "/search/audit", label: "Search audit", icon: "audit" },
    ],
  },
];
const admin: Destination[] = [
  {path:"/admin",label:"Admin overview"},
  {path:"/admin/reports",label:"Report studio"},
  {path:"/admin/roles/bulk",label:"Bulk role assignments"},
  { path: "/admin/workflows", label: "Workflows" },
  { path: "/admin/queues", label: "Queue settings" },
  { path: "/admin/roles", label: "Roles & permissions" },
  { path: "/admin/publications", label: "Publications" },
  { path: "/admin/ops", label: "Operations" },
];

/** Shared application frame with task-based navigation and route context. */
export function App() {
  const { pathname } = useLocation();
  const [mobileOpen, setMobileOpen] = useState(false);
  const inAdmin = pathname === "/admin" || pathname.startsWith("/admin/");
  const [adminOpen, setAdminOpen] = useState(inAdmin);
  useEffect(() => {
    setMobileOpen(false);
    if (inAdmin) setAdminOpen(true);
  }, [pathname, inAdmin]);
  const group =
    groups.find((section) =>
      section.links.some((item) => item.path === pathname),
    ) ?? (pathname.startsWith("/documents/") ? groups[1] : groups[0]);
  const current =
    [...group.links, ...admin].find((item) => item.path === pathname)?.label ??
    (pathname === "/cases/new"
      ? "New case"
      : pathname.startsWith("/cases/")
        ? "Case details"
        : pathname.startsWith("/tasks/")
          ? "Task details"
          : pathname.startsWith("/documents/")
            ? "Document"
            : "Overview");
  function navItem(item: Destination) {
    return (
      <NavLink
        key={item.path}
        to={item.path}
        end={["/search","/admin","/admin/roles"].includes(item.path)}
        className={({ isActive }) =>
          isActive ? styles.selected : styles.navLink
        }
      >
        {item.icon && <Icon name={item.icon} />}
        <span>{item.label}</span>
      </NavLink>
    );
  }
  return (
    <div className={styles.shell}>
      <a className={styles.skip} href="#workspace-content">
        Skip to content
      </a>
      <aside className={styles.sidebar}>
        <div className={styles.brandRow}>
          <Link to="/home" className={styles.brand}>
            <svg width="36" height="36" viewBox="0 0 36 36" aria-hidden="true">
              <rect width="36" height="36" rx="10" fill="#4378fe" />
              <path d="M8 11h8l3 3h9v11H8z" fill="#abc8ff" />
              <path d="M8 17h20l-3 9H8z" fill="white" />
            </svg>
            <span>Case Management</span>
          </Link>
          <button
            type="button"
            className={styles.menu}
            aria-expanded={mobileOpen}
            aria-controls="side-navigation"
            aria-label="Toggle navigation"
            onClick={() => setMobileOpen(!mobileOpen)}
          >
            <Icon name="menu" />
          </button>
        </div>
        <nav
          id="side-navigation"
          aria-label="Primary navigation"
          className={mobileOpen ? styles.openNav : styles.nav}
        >
          {groups.map((section) => (
            <div key={section.label} className={styles.group}>
              <h2>{section.label}</h2>
              {section.links.map(navItem)}
            </div>
          ))}
          <div className={styles.admin}>
            <button
              type="button"
              onClick={() => setAdminOpen(!adminOpen)}
              aria-expanded={adminOpen}
            >
              <Icon name="settings" />
              Administration <Icon name="chevron" />
            </button>
            {adminOpen && (
              <div className={styles.adminLinks}>{admin.map(navItem)}</div>
            )}
          </div>
        </nav>
      </aside>
      <div className={styles.body}>
        <header className={styles.topbar}>
          <div className={styles.crumb}>
            <span>{inAdmin ? "Administration" : group.label}</span>
            <i>/</i>
            {current}
          </div>
          <div className={styles.tools}>
            <QuickOpen
              pages={[
                ...groups.flatMap((section) => section.links),
                ...admin,
                { path: "/cases/new", label: "New case" },
              ]}
            />
            <Banner />
            <a className={styles.create} href={newCaseUrl()}>
              <Icon name="plus" />
              New case
            </a>
          </div>
        </header>
        <main className={styles.main} id="workspace-content" tabIndex={-1}>
          <Outlet context={{ pathname }} />
        </main>
      </div>
    </div>
  );
}
