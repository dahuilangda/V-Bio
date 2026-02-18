import { useEffect, useRef, useState } from 'react';
import { Link, NavLink, useNavigate } from 'react-router-dom';
import { ChevronDown, FlaskConical, FolderKanban, LogOut, Settings, Users } from 'lucide-react';
import { useAuth } from '../../hooks/useAuth';
import { getAvatarOverride } from '../../utils/profilePrefs';

export function AppShell({ children }: { children: React.ReactNode }) {
  const { session, logoutAction } = useAuth();
  const navigate = useNavigate();
  const avatarUrl = session ? session.avatarUrl || getAvatarOverride(session.userId) : '';
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const onPointerDown = (event: MouseEvent) => {
      if (!menuRef.current) return;
      if (menuRef.current.contains(event.target as Node)) return;
      setMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setMenuOpen(false);
    };
    window.addEventListener('mousedown', onPointerDown);
    window.addEventListener('keydown', onKeyDown);
    return () => {
      window.removeEventListener('mousedown', onPointerDown);
      window.removeEventListener('keydown', onKeyDown);
    };
  }, []);

  const signOut = () => {
    setMenuOpen(false);
    logoutAction();
    navigate('/login');
  };

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
        </div>

        <div className="top-nav-right">
          <div className="user-menu" ref={menuRef}>
            <button
              type="button"
              className={`user-chip user-chip-button ${menuOpen ? 'open' : ''}`}
              onClick={() => setMenuOpen((prev) => !prev)}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              aria-label="User menu"
            >
              {avatarUrl ? (
                <img className="avatar avatar-img" src={avatarUrl} alt="avatar" />
              ) : (
                <span className="avatar">{session?.name?.slice(0, 1).toUpperCase() || 'U'}</span>
              )}
              <div className="user-chip-text">
                <div className="user-name">{session?.name || '-'}</div>
                <div className="user-role">{session?.isAdmin ? 'Admin' : 'Member'}</div>
              </div>
              <ChevronDown size={14} className={`user-chip-caret ${menuOpen ? 'open' : ''}`} />
            </button>

            {menuOpen && (
              <div className="user-menu-popover" role="menu" aria-label="User actions">
                <Link className="user-menu-item" role="menuitem" to="/settings" onClick={() => setMenuOpen(false)}>
                  <Settings size={14} />
                  Settings
                </Link>
                <button className="user-menu-item danger" role="menuitem" onClick={signOut}>
                  <LogOut size={14} />
                  Sign out
                </button>
              </div>
            )}
          </div>
        </div>
      </header>
      <main className="main-content">{children}</main>
    </div>
  );
}
