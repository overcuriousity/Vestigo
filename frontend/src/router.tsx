import {
  createBrowserRouter,
  createRoutesFromElements,
  Navigate,
  Route,
} from "react-router-dom";
import { AppShell } from "@/components/layout/AppShell";
import { RequireAdmin } from "@/components/auth/RequireAdmin";
import { RequireAuth } from "@/components/auth/RequireAuth";
import { CasesPage } from "@/pages/CasesPage";
import { CaseOverviewPage } from "@/pages/CaseOverviewPage";
import { StoriesPage } from "@/pages/StoriesPage";
import { StoryEditorPage } from "@/pages/StoryEditorPage";
import { ExplorerPage } from "@/pages/ExplorerPage";
import { VisualizePage } from "@/pages/VisualizePage";
import { LoginPage } from "@/pages/LoginPage";
import { NotFoundPage } from "@/pages/NotFoundPage";
import { RouteErrorPage } from "@/pages/RouteErrorPage";
import { SettingsPage } from "@/pages/SettingsPage";
import { AdminLayout } from "@/pages/admin/AdminLayout";
import { AdminUsersPage } from "@/pages/admin/AdminUsersPage";
import { AdminTeamsPage } from "@/pages/admin/AdminTeamsPage";
import { AdminTeamDetailPage } from "@/pages/admin/AdminTeamDetailPage";
import { AdminAuditPage } from "@/pages/admin/AdminAuditPage";
import { AdminEnrichersPage } from "@/pages/admin/AdminEnrichersPage";
import { AdminSettingsPage } from "@/pages/admin/AdminSettingsPage";

export const router = createBrowserRouter(
  createRoutesFromElements(
    <>
      <Route path="login" element={<LoginPage />} errorElement={<RouteErrorPage />} />
      {/* One net per branch that can actually reach the router: AppShell wraps
          its own Outlet in an ErrorBoundary, so a page throw never gets this
          far, and AppShell's own throw is caught by the RequireAuth route. */}
      <Route element={<RequireAuth />} errorElement={<RouteErrorPage />}>
        <Route element={<AppShell />}>
          <Route index element={<CasesPage />} />
          <Route path="cases/:caseId" element={<CaseOverviewPage />} />
          <Route path="cases/:caseId/stories" element={<StoriesPage />} />
          <Route path="cases/:caseId/stories/:storyId" element={<StoryEditorPage />} />
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
              <Route path="enrichers" element={<AdminEnrichersPage />} />
              {/* The agent is configured on Settings like everything else
                  (migration 0033); keep old links and bookmarks working. */}
              <Route path="agent" element={<Navigate to="/admin/settings" replace />} />
              <Route path="settings" element={<AdminSettingsPage />} />
            </Route>
          </Route>
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </>,
  ),
);
