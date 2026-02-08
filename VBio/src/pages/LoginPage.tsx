import { FormEvent, useState } from 'react';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { FlaskConical, Lock, User } from 'lucide-react';
import { useAuth } from '../hooks/useAuth';

export function LoginPage() {
  const { loginAction } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const target = (location.state as { from?: string } | null)?.from || '/projects';

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await loginAction({ identifier, password });
      navigate(target, { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign-in failed.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <FlaskConical size={20} />
          <span>VBio</span>
        </div>
        <h1>Sign In</h1>

        <form onSubmit={onSubmit} className="form-grid">
          <label className="field">
            <span>Username or Email</span>
            <div className="input-wrap">
              <User size={16} />
              <input
                value={identifier}
                onChange={(e) => setIdentifier(e.target.value)}
                placeholder="username / email"
                autoComplete="username"
                required
              />
            </div>
          </label>

          <label className="field">
            <span>Password</span>
            <div className="input-wrap">
              <Lock size={16} />
              <input
                value={password}
                type="password"
                onChange={(e) => setPassword(e.target.value)}
                placeholder="At least 6 characters"
                autoComplete="current-password"
                required
              />
            </div>
          </label>

          {error && <div className="alert error">{error}</div>}

          <button className="btn btn-primary" type="submit" disabled={submitting}>
            {submitting ? 'Signing in...' : 'Sign in'}
          </button>
        </form>

        <div className="auth-footer">
          New here? <Link to="/register">Create an account</Link>
        </div>
      </div>
    </div>
  );
}
