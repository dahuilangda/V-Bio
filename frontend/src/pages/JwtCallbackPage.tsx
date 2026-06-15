import { useEffect, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { FlaskConical } from 'lucide-react';
import { useAuth } from '../hooks/useAuth';

function readTarget(search: string): { token: string; next: string } {
  const params = new URLSearchParams(search);
  const token = String(params.get('token') || '').trim();
  const next = String(params.get('next') || '/projects').trim();
  return { token, next: next.startsWith('/') ? next : '/projects' };
}

export function JwtCallbackPage() {
  const { completeJwtLoginAction } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    const { token, next } = readTarget(location.search);
    if (!token) {
      setError('Missing JWT token.');
      return () => {
        active = false;
      };
    }

    completeJwtLoginAction(token)
      .then(() => {
        if (active) navigate(next, { replace: true });
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : 'JWT sign-in failed.');
      });

    return () => {
      active = false;
    };
  }, [completeJwtLoginAction, location.search, navigate]);

  return (
    <div className="auth-page">
      <div className="auth-card auth-status-card">
        <div className="auth-brand">
          <FlaskConical size={20} />
          <span>V-Bio</span>
        </div>
        <h1>Signing In</h1>
        {error ? <div className="alert error">{error}</div> : <p className="auth-subtitle">Completing secure sign-in...</p>}
      </div>
    </div>
  );
}
