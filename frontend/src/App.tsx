import { Link, NavLink, Outlet, useLocation } from "react-router-dom";

import { Banner } from "./shell/Banner";

import styles from "./App.module.css";

export function App() {
  const location = useLocation();
  return (
    <div className={styles.shell}>
      <Banner />
      <header className={styles.header}>
        <Link to="/" className={styles.brand}>
          Case Management Platform
        </Link>
        <nav className={styles.navigation} aria-label="Primary navigation">
          <NavLink to="/home">Home</NavLink>
          <NavLink to="/cases">Cases</NavLink>
          <NavLink to="/cases/new">Create case</NavLink>
          <NavLink to="/search">Search</NavLink>
          <NavLink to="/search/audit">Search audit</NavLink>
          <NavLink to="/tasks">Tasks</NavLink>
          <NavLink to="/queues">Queues</NavLink>
          <NavLink to="/admin/queues">Queue admin</NavLink>
          <NavLink to="/admin/workflows">Workflows</NavLink>
          <NavLink to="/admin/roles">Roles</NavLink>
          <NavLink to="/admin/publications">Publications</NavLink>
          <NavLink to="/admin/ops">Ops</NavLink>
          <NavLink to="/bundles">Bundles</NavLink>
        </nav>
        <div
          className={styles.envMarker}
          title="SYNTHETIC ENVIRONMENT — no real data"
        >
          SYNTHETIC
        </div>
      </header>
      <main className={styles.main}>
        <Outlet context={{ pathname: location.pathname }} />
      </main>
    </div>
  );
}
