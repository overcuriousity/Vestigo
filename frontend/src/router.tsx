import {
  createBrowserRouter,
  createRoutesFromElements,
  Route,
} from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { RequireAdmin } from "@/components/auth/RequireAdmin";
import { RequireAuth } from "@/components/auth/RequireAuth";
import { CasesPage } from "@/pages/CasesPage";
import { CaseOverviewPage } from "@/pages/CaseOverviewPage";
import { ExplorerPage } from "@/pages/ExplorerPage";
import { VisualizePage } from "@/pages/VisualizePage";
import { LoginPage } from "@/pages/LoginPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { AdminLayout } from "@/pages/admin/AdminLayout";
import { AdminUsersPage } from "@/pages/admin/AdminUsersPage";
import { AdminTeamsPage } from "@/pages/admin/AdminTeamsPage";
import { AdminTeamDetailPage } from "@/pages/admin/AdminTeamDetailPage";
import { AdminAuditPage } from "@/pages/admin/AdminAuditPage";

export const router = createBrowserRouter(
  createRoutesFromElements(
    <>
      <Route path="login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<AppShell />}>
          <Route index element={<CasesPage />} />
          <Route path="cases/:caseId" element={<CaseOverviewPage />} />
          <Route
            path="cases/:caseId/timelines/:timelineId"
            element={<ExplorerPage />}
          />
          <Route
            path="cases/:caseId/timelines/:timelineId/visualize"
            element={<VisualizePage />}
          />
          <Route path="settings" element={<SettingsPage />} />
          <Route path="admin" element={<RequireAdmin />}>
            <Route element={<AdminLayout />}>
              <Route index element={<AdminUsersPage />} />
              <Route path="users" element={<AdminUsersPage />} />
              <Route path="teams" element={<AdminTeamsPage />} />
              <Route path="teams/:teamId" element={<AdminTeamDetailPage />} />
              <Route path="audit" element={<AdminAuditPage />} />
            </Route>
          </Route>
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </>,
  ),
);
