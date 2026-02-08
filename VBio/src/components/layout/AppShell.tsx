import { Link, NavLink, useNavigate } from 'react-router-dom';
import { FlaskConical, FolderKanban, Settings, Users } from 'lucide-react';
import { useAuth } from '../../hooks/useAuth';
import { getAvatarOverride } from '../../utils/profilePrefs';

export function AppShell({ children }: { children: React.ReactNode }) {
  const { session, logoutAction } = useAuth();
  const navigate = useNavigate();
  const avatarUrl = session ? session.avatarUrl || getAvatarOverride(session.userId) : '';

  return (
    <div className="app-shell">
      <header className="top-nav">
        <div className="top-nav-left">
          <Link to="/projects" className="brand">
            <FlaskConical size={18} />
            <span>VBio</span>
          </Link>
          <NavLink to="/projects" className="top-link">
            <FolderKanban size={16} />
            <span>Projects</span>
          </NavLink>
          {session?.isAdmin && (
            <NavLink to="/users" className="top-link">
              <Users size={16} />
              <span>Users</span>
            </NavLink>
          )}
          <NavLink to="/settings" className="top-link">
            <Settings size={16} />
            <span>Settings</span>
          </NavLink>
        </div>

        <div className="top-nav-right">
          <div className="user-chip">
            {avatarUrl ? (
              <img className="avatar avatar-img" src={avatarUrl} alt="avatar" />
            ) : (
              <span className="avatar">{session?.name?.slice(0, 1).toUpperCase() || 'U'}</span>
            )}
            <div>
              <div className="user-name">{session?.name || '-'}</div>
              <div className="user-role">{session?.isAdmin ? 'Admin' : 'Member'}</div>
            </div>
          </div>
          <button
            className="btn btn-ghost"
            onClick={() => {
              logoutAction();
              navigate('/login');
            }}
          >
            Sign out
          </button>
        </div>
      </header>
      <main className="main-content">{children}</main>
    </div>
  );
}
