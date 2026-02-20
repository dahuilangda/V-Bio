import { FormEvent, useEffect, useState } from 'react';
import { KeyRound, UserRound } from 'lucide-react';
import type { AppUser } from '../types/models';
import { useAuth } from '../hooks/useAuth';
import { findUserByUsername, updateUser } from '../api/supabaseLite';
import { hashPassword } from '../utils/crypto';
import { getAvatarOverride, setAvatarOverride } from '../utils/profilePrefs';
import { MmpDatabaseAdminPanel } from '../components/admin/MmpDatabaseAdminPanel';

export function SettingsPage() {
  const { session, refreshSession } = useAuth();
  const isAdmin = Boolean(session?.isAdmin);

  const [user, setUser] = useState<AppUser | null>(null);
  const [loading, setLoading] = useState(false);
  const [profileSaving, setProfileSaving] = useState(false);
  const [passwordSaving, setPasswordSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [avatarUrl, setAvatarUrl] = useState('');

  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  useEffect(() => {
    if (!session?.username) return;
    let cancelled = false;

    const load = async () => {
      setLoading(true);
      setError(null);
      try {
        const found = await findUserByUsername(session.username);
        if (!found) {
          throw new Error('Current user not found.');
        }
        if (cancelled) return;
        setUser(found);
        setDisplayName(found.name || '');
        setEmail(found.email || '');
        const localAvatar = getAvatarOverride(found.id);
        setAvatarUrl((found.avatar_url || localAvatar || '').trim());
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load profile.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [session?.username]);

  const saveProfile = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!user) return;
    setProfileSaving(true);
    setError(null);
    setSuccess(null);

    const patchBase: Partial<AppUser> = {
      name: displayName.trim(),
      email: email.trim().toLowerCase() || null
    };
    const avatar = avatarUrl.trim();

    try {
      let updated: AppUser;
      try {
        updated = await updateUser(user.id, {
          ...patchBase,
          avatar_url: avatar || null
        } as Partial<AppUser>);
      } catch (avatarError) {
        const text = avatarError instanceof Error ? avatarError.message : '';
        const looksLikeMissingAvatarColumn = text.toLowerCase().includes('avatar_url');
        if (!looksLikeMissingAvatarColumn) {
          throw avatarError;
        }
        updated = await updateUser(user.id, patchBase);
      }

      setAvatarOverride(user.id, avatar);
      setUser(updated);
      await refreshSession();
      setSuccess('Profile updated.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update profile.');
    } finally {
      setProfileSaving(false);
    }
  };

  const changePassword = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!user) return;
    setPasswordSaving(true);
    setError(null);
    setSuccess(null);

    try {
      if (!currentPassword || !newPassword) {
        throw new Error('Current password and new password are required.');
      }
      if (newPassword.length < 6) {
        throw new Error('New password must be at least 6 characters.');
      }
      if (newPassword !== confirmPassword) {
        throw new Error('New password confirmation does not match.');
      }

      const currentHash = await hashPassword(user.username, currentPassword);
      if (currentHash !== user.password_hash) {
        throw new Error('Current password is incorrect.');
      }

      const newHash = await hashPassword(user.username, newPassword);
      const updated = await updateUser(user.id, { password_hash: newHash });
      setUser(updated);
      setCurrentPassword('');
      setNewPassword('');
      setConfirmPassword('');
      setSuccess('Password updated.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to change password.');
    } finally {
      setPasswordSaving(false);
    }
  };

  return (
    <div className="page-grid settings-page">
      <section className="page-header">
        <div>
          <h1>Settings</h1>
          <p className="muted">Manage profile and password.</p>
        </div>
      </section>

      {error && <div className="alert error">{error}</div>}
      {success && <div className="alert success">{success}</div>}

      <div className="settings-grid">
        <section className="panel settings-panel">
          <div className="settings-panel-head">
            <h2><UserRound size={16} /> Profile</h2>
          </div>
          {loading ? (
            <div className="muted">Loading profile...</div>
          ) : (
            <form className="form-grid" onSubmit={saveProfile}>
              <label className="field">
                <span>Display Name</span>
                <input value={displayName} onChange={(e) => setDisplayName(e.target.value)} required />
              </label>

              <label className="field">
                <span>Email</span>
                <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="you@example.com" />
              </label>

              <label className="field">
                <span>Avatar URL</span>
                <input value={avatarUrl} onChange={(e) => setAvatarUrl(e.target.value)} placeholder="https://..." />
              </label>

              <div className="settings-avatar-preview">
                {avatarUrl.trim() ? (
                  <img src={avatarUrl.trim()} alt="avatar preview" className="settings-avatar-image" />
                ) : (
                  <span className="avatar settings-avatar-fallback">
                    {displayName.trim().slice(0, 1).toUpperCase() || session?.name?.slice(0, 1).toUpperCase() || 'U'}
                  </span>
                )}
              </div>

              <div className="row end">
                <button className="btn btn-primary" type="submit" disabled={profileSaving}>
                  {profileSaving ? 'Saving...' : 'Save Profile'}
                </button>
              </div>
            </form>
          )}
        </section>

        <section className="panel settings-panel">
          <div className="settings-panel-head">
            <h2><KeyRound size={16} /> Password</h2>
          </div>
          <form className="form-grid" onSubmit={changePassword}>
            <label className="field">
              <span>Current Password</span>
              <input
                type="password"
                value={currentPassword}
                onChange={(e) => setCurrentPassword(e.target.value)}
                autoComplete="current-password"
                required
              />
            </label>

            <label className="field">
              <span>New Password</span>
              <input
                type="password"
                value={newPassword}
                onChange={(e) => setNewPassword(e.target.value)}
                autoComplete="new-password"
                required
              />
            </label>

            <label className="field">
              <span>Confirm New Password</span>
              <input
                type="password"
                value={confirmPassword}
                onChange={(e) => setConfirmPassword(e.target.value)}
                autoComplete="new-password"
                required
              />
            </label>

            <div className="row end">
              <button className="btn btn-primary" type="submit" disabled={passwordSaving}>
                {passwordSaving ? 'Updating...' : 'Update Password'}
              </button>
            </div>
          </form>
        </section>
      </div>

      {isAdmin ? <MmpDatabaseAdminPanel compact /> : null}
    </div>
  );
}
