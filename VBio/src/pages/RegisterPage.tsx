import { FormEvent, useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { FlaskConical, Lock, Mail, Signature, User } from 'lucide-react';
import { useAuth } from '../hooks/useAuth';

export function RegisterPage() {
  const { registerAction } = useAuth();
  const navigate = useNavigate();

  const [name, setName] = useState('');
  const [username, setUsername] = useState('');
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const onSubmit = async (e: FormEvent) => {
    e.preventDefault();
    setError(null);
    if (password !== confirmPassword) {
      setError('Passwords do not match.');
      return;
    }

    setSubmitting(true);
    try {
      await registerAction({ name, username, email, password });
      navigate('/projects', { replace: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Registration failed.');
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
        <h1>Register</h1>

        <form onSubmit={onSubmit} className="form-grid">
          <label className="field">
            <span>Display Name</span>
            <div className="input-wrap">
              <Signature size={16} />
              <input value={name} onChange={(e) => setName(e.target.value)} required />
            </div>
          </label>

          <label className="field">
            <span>Username</span>
            <div className="input-wrap">
              <User size={16} />
              <input
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                placeholder="a-zA-Z0-9_.-"
                autoComplete="username"
                required
              />
            </div>
          </label>

          <label className="field">
            <span>Email (optional)</span>
            <div className="input-wrap">
              <Mail size={16} />
              <input value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" />
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
                autoComplete="new-password"
                required
              />
            </div>
          </label>

          <label className="field">
            <span>Confirm Password</span>
            <div className="input-wrap">
              <Lock size={16} />
              <input
                value={confirmPassword}
                type="password"
                onChange={(e) => setConfirmPassword(e.target.value)}
                autoComplete="new-password"
                required
              />
            </div>
          </label>

          {error && <div className="alert error">{error}</div>}

          <button className="btn btn-primary" type="submit" disabled={submitting}>
            {submitting ? 'Creating account...' : 'Create account'}
          </button>
        </form>

        <div className="auth-footer">
          Already have an account? <Link to="/login">Back to sign in</Link>
        </div>
      </div>
    </div>
  );
}
