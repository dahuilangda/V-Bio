import { Navigate, useLocation } from 'react-router-dom';
import { useAuth } from '../../hooks/useAuth';

export function ProtectedRoute({ children }: { children: JSX.Element }) {
  const { session, loading } = useAuth();
  const location = useLocation();

  if (loading) {
    return <div className="centered-page">Loading...</div>;
  }

  if (!session) {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  }

  return children;
}

export function AdminRoute({ children }: { children: JSX.Element }) {
  const { session, loading } = useAuth();

  if (loading) {
    return <div className="centered-page">Loading...</div>;
  }

  if (!session) {
    return <Navigate to="/login" replace />;
  }

  if (!session.isAdmin) {
    return <Navigate to="/projects" replace />;
  }

  return children;
}
