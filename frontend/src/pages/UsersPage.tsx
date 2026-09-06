import { FormEvent, useEffect, useState } from 'react';
import { RefreshCcw, ShieldCheck, ShieldX, Trash2, LoaderCircle} from 'lucide-react';
import type { AppUser } from '../types/models';
import { formatDateTime } from '../utils/date';
import { isSuperAdminIdentity } from '../api/authApi';
import { adminCreateUser, adminListUsers, adminUpdateUser, toAppUser } from '../api/authServerApi';

export function UsersPage() {
  const [users, setUsers] = useState<AppUser[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setUsers((await adminListUsers()).map(toAppUser));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load users.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void load();
  }, []);

  const onCreate = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    setCreateError(null);

    const form = new FormData(e.currentTarget);
    const username = String(form.get('username') || '').trim().toLowerCase();
    const name = String(form.get('name') || '').trim();
    const email = String(form.get('email') || '').trim().toLowerCase();
    const password = String(form.get('password') || '').trim();
    const is_admin = String(form.get('is_admin') || '') === 'on' || isSuperAdminIdentity(username, email);

    if (!username || !name || !password) {
      setCreateError('Username, display name, and password are required.');
      return;
    }

    setCreating(true);
    try {
      const created = await adminCreateUser({
        username,
        name,
        email: email || undefined,
        password,
        is_admin
      });
      setUsers((prev) => [toAppUser(created), ...prev]);
      (e.currentTarget as HTMLFormElement).reset();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : 'Failed to create user.');
    } finally {
      setCreating(false);
    }
  };

  const [busyUserId, setBusyUserId] = useState<string | null>(null);

  const toggleAdmin = async (user: AppUser) => {
    if (isSuperAdminIdentity(user.username, user.email) || busyUserId) return;
    setBusyUserId(user.id);
    try {
      const next = await adminUpdateUser(user.id, { is_admin: !user.is_admin });
      setUsers((prev) => prev.map((u) => (u.id === user.id ? toAppUser(next) : u)));
    } finally {
      setBusyUserId(null);
    }
  };

  const resetPassword = async (user: AppUser) => {
    const value = window.prompt(`Enter a new password for ${user.username}`);
    if (!value || busyUserId) return;
    setBusyUserId(user.id);
    try {
      const next = await adminUpdateUser(user.id, { password: value });
      setUsers((prev) => prev.map((u) => (u.id === user.id ? toAppUser(next) : u)));
    } finally {
      setBusyUserId(null);
    }
  };

  const removeUser = async (user: AppUser) => {
    if (isSuperAdminIdentity(user.username, user.email) || busyUserId) return;
    if (!window.confirm(`Delete user "${user.username}"?`)) return;
    setBusyUserId(user.id);
    try {
      const next = await adminUpdateUser(user.id, { deleted_at: new Date().toISOString() });
      setUsers((prev) => prev.filter((u) => u.id !== next.id));
    } finally {
      setBusyUserId(null);
    }
  };

  return (
    <div className="page-grid users-page">
      <section className="page-header">
        <div>
          <h1>User Management</h1>
        </div>
        <button type="button"
          className="btn btn-ghost"
          onClick={() => {
            void load();
          }}
        >
          <RefreshCcw size={14} />
          Refresh
        </button>
      </section>

      <section className="panel">
        <h2>Create User</h2>
        <form className="form-grid users-create" onSubmit={onCreate}>
          <label className="field">
            <span>Username</span>
            <input name="username" required />
          </label>
          <label className="field">
            <span>Display Name</span>
            <input name="name" required />
          </label>
          <label className="field">
            <span>Email</span>
            <input name="email" type="email" />
          </label>
          <label className="field">
            <span>Initial Password</span>
            <input name="password" type="password" required />
          </label>
          <label className="switch-field">
            <input type="checkbox" name="is_admin" />
            <span>Admin role</span>
          </label>
          <button className="btn btn-primary" type="submit" disabled={creating}>
            {creating ? 'Creating...' : 'Create user'}
          </button>
        </form>
        {createError && <div className="alert error">{createError}</div>}
      </section>

      <section className="panel">
        <h2>User List</h2>
        {error && <div className="alert error">{error}</div>}
        {loading ? (
          <div className="muted">Loading users...</div>
        ) : (
          <div className="table-wrap">
            <table className="table">
              <thead>
                <tr>
                  <th>Username</th>
                  <th>Display Name</th>
                  <th>Email</th>
                  <th>Role</th>
                  <th>Last Login</th>
                  <th>Created At</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.id}>
                    <td>{user.username}</td>
                    <td>{user.name}</td>
                    <td>{user.email || '-'}</td>
                    <td>{isSuperAdminIdentity(user.username, user.email) ? 'Super Admin' : user.is_admin ? 'Admin' : 'Member'}</td>
                    <td>{formatDateTime(user.last_login_at)}</td>
                    <td>{formatDateTime(user.created_at)}</td>
                    <td>
                      <div className="row gap-6">
                        <button type="button"
                          className="icon-btn"
                          title="Toggle admin role"
                          aria-busy={busyUserId === user.id}
                          disabled={isSuperAdminIdentity(user.username, user.email) || busyUserId === user.id}
                          onClick={() => void toggleAdmin(user)}
                        >
                          {busyUserId === user.id ? <LoaderCircle size={14} className="spin" /> : user.is_admin ? <ShieldX size={14} /> : <ShieldCheck size={14} />}
                        </button>
                        <button type="button" className="icon-btn" title="Reset password" aria-busy={busyUserId === user.id} disabled={busyUserId === user.id} onClick={() => void resetPassword(user)}>
                          {busyUserId === user.id ? <LoaderCircle size={14} className="spin" /> : 'Reset'}
                        </button>
                        <button type="button"
                          className="icon-btn danger"
                          title="Delete user"
                          aria-busy={busyUserId === user.id}
                          disabled={isSuperAdminIdentity(user.username, user.email) || busyUserId === user.id}
                          onClick={() => void removeUser(user)}
                        >
                          {busyUserId === user.id ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

    </div>
  );
}
