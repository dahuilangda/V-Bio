import { FormEvent, useState } from 'react';
import { Link } from 'react-router-dom';
import { FlaskConical, MailQuestion } from 'lucide-react';
import { Field } from '../components/common/Field';
import { forgotPassword } from '../api/authApi';

export function ForgotPasswordPage() {
  const [identifier, setIdentifier] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [sent, setSent] = useState(false);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    setSubmitting(true);
    try {
      await forgotPassword(identifier.trim());
      setSent(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Request failed.');
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
        <h1>Reset Password</h1>

        {sent ? (
          <div className="auth-footer">
            <p className="muted">
              <MailQuestion size={14} /> If the account exists, a reset link has been
              emailed to the account's address. The link is valid for 30 minutes
              and can be used once.
            </p>
            <p>
              <Link to="/login">Back to sign in</Link>
            </p>
          </div>
        ) : (
          <form onSubmit={onSubmit} className="form-grid">
            <Field label="Username or Email">
              <div className="input-wrap">
                <input
                  value={identifier}
                  onChange={(e) => setIdentifier(e.target.value)}
                  placeholder="username / email"
                  autoComplete="username"
                  required
                />
              </div>
            </Field>
            {error ? <div className="alert error">{error}</div> : null}
            <button type="submit" className="btn btn-primary" disabled={submitting || !identifier.trim()}>
              {submitting ? 'Sending…' : 'Email me a reset link'}
            </button>
            <div className="auth-footer">
              Remembered it? <Link to="/login">Sign in</Link>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}
