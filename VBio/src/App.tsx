import { Navigate, Route, Routes } from 'react-router-dom';
import { AppShell } from './components/layout/AppShell';
import { AdminRoute, ProtectedRoute } from './components/layout/ProtectedRoute';
import { LoginPage } from './pages/LoginPage';
import { ProjectDetailPage } from './pages/ProjectDetailPage';
import { ProjectTasksPage } from './pages/ProjectTasksPage';
import { ProjectsPage } from './pages/ProjectsPage';
import { RegisterPage } from './pages/RegisterPage';
import { SettingsPage } from './pages/SettingsPage';
import { UsersPage } from './pages/UsersPage';

function ShellPage({ children }: { children: JSX.Element }) {
  return <AppShell>{children}</AppShell>;
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
              <ProjectsPage />
            </ShellPage>
          </ProtectedRoute>
        }
      />
      <Route
        path="/projects/:projectId"
        element={
          <ProtectedRoute>
            <ShellPage>
              <ProjectDetailPage />
            </ShellPage>
          </ProtectedRoute>
        }
      />
      <Route
        path="/projects/:projectId/tasks"
        element={
          <ProtectedRoute>
            <ShellPage>
              <ProjectTasksPage />
            </ShellPage>
          </ProtectedRoute>
        }
      />
      <Route
        path="/settings"
        element={
          <ProtectedRoute>
            <ShellPage>
              <SettingsPage />
            </ShellPage>
          </ProtectedRoute>
        }
      />
      <Route
        path="/users"
        element={
          <AdminRoute>
            <ShellPage>
              <UsersPage />
            </ShellPage>
          </AdminRoute>
        }
      />
      <Route path="/" element={<Navigate to="/projects" replace />} />
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}
