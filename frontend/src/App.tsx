import { Suspense, lazy } from 'react';
import { Navigate, Route, Routes, useLocation, useParams } from 'react-router-dom';
import { AppShell } from './components/layout/AppShell';
import { AdminRoute, ProtectedRoute } from './components/layout/ProtectedRoute';
import { LoginPage } from './pages/LoginPage';
import { RegisterPage } from './pages/RegisterPage';

const ProjectsPage = lazy(() => import('./pages/ProjectsPage').then((m) => ({ default: m.ProjectsPage })));
const ProjectDetailPage = lazy(() => import('./pages/ProjectDetailPage').then((m) => ({ default: m.ProjectDetailPage })));
const ProjectTasksPage = lazy(() => import('./pages/ProjectTasksPage').then((m) => ({ default: m.ProjectTasksPage })));
const SettingsPage = lazy(() => import('./pages/SettingsPage').then((m) => ({ default: m.SettingsPage })));
const UsersPage = lazy(() => import('./pages/UsersPage').then((m) => ({ default: m.UsersPage })));
const MmpLifecycleAdminPage = lazy(() => import('./pages/MmpLifecycleAdminPage').then((m) => ({ default: m.MmpLifecycleAdminPage })));

function ShellPage({ children }: { children: JSX.Element }) {
  return <AppShell>{children}</AppShell>;
}

function PageLoading() {
  return <div className="centered-page">Loading page...</div>;
}

function ProjectApiAccessRedirect() {
  const { projectId = '' } = useParams();
  const location = useLocation();
  const query = new URLSearchParams(location.search);
  query.set('view', 'api');
  const nextSearch = query.toString();
  return <Navigate to={`/projects/${projectId}/tasks${nextSearch ? `?${nextSearch}` : ''}`} replace />;
}

export default function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/register" element={<RegisterPage />} />

      <Route
        path="/projects"
        element={
          <ProtectedRoute>
            <ShellPage>
              <Suspense fallback={<PageLoading />}>
                <ProjectsPage />
              </Suspense>
            </ShellPage>
          </ProtectedRoute>
        }
      />
      <Route
        path="/projects/:projectId"
        element={
          <ProtectedRoute>
            <ShellPage>
              <Suspense fallback={<PageLoading />}>
                <ProjectDetailPage />
              </Suspense>
            </ShellPage>
          </ProtectedRoute>
        }
      />
      <Route
        path="/projects/:projectId/tasks"
        element={
          <ProtectedRoute>
            <ShellPage>
              <Suspense fallback={<PageLoading />}>
                <ProjectTasksPage />
              </Suspense>
            </ShellPage>
          </ProtectedRoute>
        }
      />
      <Route
        path="/api-access"
        element={
          <ProtectedRoute>
            <Navigate to="/projects" replace />
          </ProtectedRoute>
        }
      />
      <Route
        path="/projects/:projectId/api-access"
        element={
          <ProtectedRoute>
            <ProjectApiAccessRedirect />
          </ProtectedRoute>
        }
      />
      <Route
        path="/settings"
        element={
          <ProtectedRoute>
            <ShellPage>
              <Suspense fallback={<PageLoading />}>
                <SettingsPage />
              </Suspense>
            </ShellPage>
          </ProtectedRoute>
        }
      />
      <Route
        path="/admin/users"
        element={
          <AdminRoute>
            <ShellPage>
              <Suspense fallback={<PageLoading />}>
                <UsersPage />
              </Suspense>
            </ShellPage>
          </AdminRoute>
        }
      />
      <Route path="/users" element={<Navigate to="/admin/users" replace />} />
      <Route
        path="/admin/mmp-lifecycle"
        element={
          <AdminRoute>
            <ShellPage>
              <Suspense fallback={<PageLoading />}>
                <MmpLifecycleAdminPage />
              </Suspense>
            </ShellPage>
          </AdminRoute>
        }
      />
      <Route path="/" element={<Navigate to="/projects" replace />} />
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}
