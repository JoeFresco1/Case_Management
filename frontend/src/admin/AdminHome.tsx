/** Directory of administration tools in the case management workspace. */
import { Link } from "react-router-dom";

import { Icon, type IconName } from "../shell/Icon";
import styles from "./AdminHome.module.css";

const sections = [
  {
    title: "Access and assignments",
    tools: [
      {
        name: "Roles and permissions",
        path: "/admin/roles",
        icon: "settings",
        description:
          "Define roles, review their permissions, and manage individual assignments.",
      },
      {
        name: "Bulk role assignments",
        path: "/admin/roles/bulk",
        icon: "tasks",
        description: "Assign a role to several users in one operation.",
      },
    ],
  },
  {
    title: "Workflow configuration",
    tools: [
      {
        name: "Workflows",
        path: "/admin/workflows",
        icon: "bundles",
        description:
          "Review published states and transitions, then simulate a case moving through a workflow.",
      },
      {
        name: "Queues",
        path: "/admin/queues",
        icon: "queues",
        description: "Define work queues and review how tasks are distributed.",
      },
      {
        name: "Publication history",
        path: "/admin/publications",
        icon: "audit",
        description:
          "Review configuration changes and record publication approvals.",
      },
    ],
  },
  {
    title: "Reporting and oversight",
    tools: [
      {
        name: "Report studio",
        path: "/admin/reports",
        icon: "dashboard",
        description:
          "Review operational reports and build report definitions for your team.",
      },
      {
        name: "Operations",
        path: "/admin/ops",
        icon: "settings",
        description:
          "Inspect failed deliveries, review event compatibility, and check service diagnostics.",
      },
      {
        name: "Records log",
        path: "/records/log",
        icon: "cases",
        description:
          "Find a case by title or reference and inspect its recorded activity.",
      },
    ],
  },
] satisfies {
  title: string;
  tools: { name: string; path: string; icon: IconName; description: string }[];
}[];

/** Open an administration workspace using its normal, permission-checked routes. */
export function AdminHome() {
  return (
    <section className={styles.page} aria-label="Administration">
      <header className={styles.header}>
        <h1>Administration</h1>
        <p>
          Manage access, configure how work moves, and review operating records.
        </p>
      </header>
      {sections.map(({ title, tools }) => (
        <section className={styles.group} key={title} aria-label={title}>
          <h2>{title}</h2>
          <ul className={styles.grid}>
            {tools.map(({ name, path, icon, description }) => (
              <li key={path}>
                <Link className={styles.card} to={path}>
                  <span className={styles.icon}>
                    <Icon name={icon} />
                  </span>
                  <span className={styles.copy}>
                    <strong>{name}</strong>
                    <span>{description}</span>
                  </span>
                  <span className={styles.arrow} aria-hidden="true">
                    →
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      ))}
    </section>
  );
}
