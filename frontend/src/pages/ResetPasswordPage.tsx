import { FormEvent, useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { FlaskConical, Lock } from 'lucide-react';
import { useEffect, useRef } from 'react';
import { Field } from '../components/common/Field';
import { resetPassword } from '../api/authApi';

export function ResetPasswordPage() {
  const [searchParams] = useSearchParams();
  const navigate = useNavigate();
  const [token] = useState(() => {
    const raw = searchParams.get('token')?.trim() || '';
    if (raw) {
      // one-time token: read once, then remove from history/address bar
      window.history.replaceState(null, '', '/reset-password');
    }
    return raw;
  });
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const redirectTimerRef = useRef<number | null>(null);
  useEffect(() => () => {
    if (redirectTimerRef.current !== null) {
      window.clearTimeout(redirectTimerRef.current);
    }
  }, []);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password.length < 8) {
      setError('The new password must be at least 8 characters.');
      return;
    }
    if (password !== confirm) {
      setError('The two passwords do not match.');
      return;
    }
    setSubmitting(true);
    try {
      await resetPassword(token, password);
      setDone(true);
      redirectTimerRef.current = window.setTimeout(
        () => navigate('/login', { replace: true }), 2500);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Reset failed.');
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="auth-page">
      <div className="auth-card">
        <div className="auth-brand">
          <FlaskConical size={20} />
          <span>V-Bio</span>
        </div>
        <h1>Set a New Password</h1>

        {!token ? (
          <div className="auth-footer">
            <p className="muted">This link is missing its reset token.</p>
            <p>
              <Link to="/forgot-password">Request a new reset link</Link>
            </p>
          </div>
        ) : done ? (
          <div className="auth-footer">
            <p className="muted">Password updated. Redirecting to sign in…</p>
            <p>
              <Link to="/login">Go to sign in now</Link>
            </p>
          </div>
        ) : (
          <form onSubmit={onSubmit} className="form-grid">
            <Field label="New password (min 8 characters)">
              <div className="input-wrap">
                <Lock size={16} />
                <input
                  value={password}
                  type="password"
                  onChange={(e) => setPassword(e.target.value)}
                  autoComplete="new-password"
                  minLength={8}
                  required
                />
              </div>
            </Field>
            <Field label="Confirm new password">
              <div className="input-wrap">
                <Lock size={16} />
                <input
                  value={confirm}
                  type="password"
                  onChange={(e) => setConfirm(e.target.value)}
                  autoComplete="new-password"
                  minLength={8}
                  required
                />
              </div>
            </Field>
            {error ? <div className="alert error">{error}</div> : null}
            <button type="submit" className="btn btn-primary" disabled={submitting}>
              {submitting ? 'Updating…' : 'Update password'}
            </button>
            <div className="auth-footer">
              <Link to="/login">Back to sign in</Link>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
