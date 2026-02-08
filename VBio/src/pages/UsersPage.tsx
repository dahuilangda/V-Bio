import { FormEvent, useEffect, useState } from 'react';
import { RefreshCcw, ShieldCheck, ShieldX, Trash2 } from 'lucide-react';
import type { AppUser } from '../types/models';
import { hashPassword } from '../utils/crypto';
import { formatDateTime } from '../utils/date';
import { insertUser, listUsers, updateUser } from '../api/supabaseLite';

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
      setUsers(await listUsers());
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
    const is_admin = String(form.get('is_admin') || '') === 'on';

    if (!username || !name || !password) {
      setCreateError('Username, display name, and password are required.');
      return;
    }

    setCreating(true);
    try {
      const password_hash = await hashPassword(username, password);
      const created = await insertUser({
        username,
        name,
        email: email || null,
        password_hash,
        is_admin,
        last_login_at: null
      });
      setUsers((prev) => [created, ...prev]);
      (e.currentTarget as HTMLFormElement).reset();
    } catch (err) {
      setCreateError(err instanceof Error ? err.message : 'Failed to create user.');
    } finally {
      setCreating(false);
    }
  };

  const toggleAdmin = async (user: AppUser) => {
    const next = await updateUser(user.id, { is_admin: !user.is_admin });
    setUsers((prev) => prev.map((u) => (u.id === user.id ? next : u)));
  };

  const resetPassword = async (user: AppUser) => {
    const value = window.prompt(`Enter a new password for ${user.username}`);
    if (!value) return;
    const password_hash = await hashPassword(user.username, value);
    const next = await updateUser(user.id, { password_hash });
    setUsers((prev) => prev.map((u) => (u.id === user.id ? next : u)));
  };

  const removeUser = async (user: AppUser) => {
    if (!window.confirm(`Delete user "${user.username}"?`)) return;
    const next = await updateUser(user.id, { deleted_at: new Date().toISOString() });
    setUsers((prev) => prev.filter((u) => u.id !== next.id));
  };

  return (
    <div className="page-grid users-page">
      <section className="page-header">
        <div>
          <h1>User Management</h1>
          <p className="muted">Create users, grant admin role, reset passwords, and deactivate accounts.</p>
        </div>
        <button className="btn btn-ghost" onClick={() => void load()}>
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
                    <td>{user.is_admin ? 'Admin' : 'Member'}</td>
                    <td>{formatDateTime(user.last_login_at)}</td>
                    <td>{formatDateTime(user.created_at)}</td>
                    <td>
                      <div className="row gap-6">
                        <button className="icon-btn" title="Toggle admin role" onClick={() => void toggleAdmin(user)}>
                          {user.is_admin ? <ShieldX size={14} /> : <ShieldCheck size={14} />}
                        </button>
                        <button className="icon-btn" title="Reset password" onClick={() => void resetPassword(user)}>
                          Reset
                        </button>
                        <button className="icon-btn danger" title="Delete user" onClick={() => void removeUser(user)}>
                          <Trash2 size={14} />
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

