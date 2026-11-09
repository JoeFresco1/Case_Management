import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Navigate, RouterProvider, createBrowserRouter } from "react-router-dom";

import { RecordsLog } from "./audit/RecordsLog";
import { AdminHome } from "./admin/AdminHome";
import { ReportStudio } from "./reports/ReportStudio";
import { BulkRoles } from "./security/BulkRoles";
import { App } from "./App";
import { CaseList } from "./cases/CaseList";
import { CaseWorkspace } from "./cases/CaseWorkspace";
import { CreateCasePage } from "./cases/CreateCasePage";
import { NotFound } from "./shell/NotFound";
import { Dashboard } from "./reports/Dashboard";
import { SearchPage } from "./search/SearchPage";
import { SearchAuditPage } from "./search/SearchAudit";
import { TaskPage } from "./tasks/TaskList";
import { QueueBoard } from "./tasks/QueueBoard";
import { QueueAdmin } from "./tasks/QueueAdmin";
import { RoleAdmin } from "./security/RoleAdmin";
import { HomeView } from "./home/HomeView";
import { PublicationHistory } from "./admin/PublicationHistory";
import { OutboxOps } from "./admin/OutboxOps";
import { DocumentViewerPage } from "./documents/DocumentViewerPage";
import { BundleSharingPage } from "./documents/BundleSharingPage";
import { WorkflowAdmin } from "./workflow/WorkflowAdmin";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
      retry: 1,
    },
  },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <Navigate to="/home" replace /> },
      { path: "cases", element: <CaseList /> },
      { path: "cases/new", element: <CreateCasePage /> },
      { path: "cases/:caseId", element: <CaseWorkspace /> },
      { path: "search", element: <SearchPage /> },
      { path: "records/log", element: <RecordsLog /> },
      { path: "search/audit", element: <SearchAuditPage /> },
      { path: "dashboard", element: <Dashboard /> },
      { path: "home", element: <HomeView /> },
      { path: "tasks", element: <TaskPage /> },
      { path: "tasks/:taskId", element: <TaskPage /> },
      { path: "queues", element: <QueueBoard /> },
      { path: "admin", element: <AdminHome /> },
      { path: "admin/reports", element: <ReportStudio /> },
      { path: "admin/roles/bulk", element: <BulkRoles /> },
      { path: "admin/queues", element: <QueueAdmin /> },
      { path: "admin/workflows", element: <WorkflowAdmin /> },
      { path: "admin/roles", element: <RoleAdmin /> },
      { path: "admin/publications", element: <PublicationHistory /> },
      { path: "admin/ops", element: <OutboxOps /> },
      { path: "documents/:documentId", element: <DocumentViewerPage /> },
      { path: "bundles", element: <BundleSharingPage /> },
      { path: "*", element: <NotFound /> },
    ],
  },
]);

const root = document.getElementById("root");
if (!root) throw new Error("missing #root");

createRoot(root).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
