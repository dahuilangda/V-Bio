import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { AuthLoginInput, AuthRegisterInput, Session } from '../types/models';
import { clearSession, loadSession, login, register, saveSession } from '../api/authApi';
import { findUserByUsername } from '../api/supabaseLite';

interface AuthContextValue {
  session: Session | null;
  loading: boolean;
  loginAction: (input: AuthLoginInput) => Promise<void>;
  registerAction: (input: AuthRegisterInput) => Promise<void>;
  logoutAction: () => void;
  refreshSession: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [session, setSession] = useState<Session | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    const existing = loadSession();
    setSession(existing);
    setLoading(false);
  }, []);

  const refreshSession = async () => {
    if (!session) return;
    const user = await findUserByUsername(session.username);
    if (!user || user.deleted_at) {
      clearSession();
      setSession(null);
      return;
    }
    const refreshed: Session = {
      userId: user.id,
      username: user.username,
      name: user.name,
      email: user.email || null,
      avatarUrl: user.avatar_url || session.avatarUrl || null,
      isAdmin: user.is_admin,
      loginAt: session.loginAt
    };
    saveSession(refreshed);
    setSession(refreshed);
  };

  const value = useMemo<AuthContextValue>(
    () => ({
      session,
      loading,
      loginAction: async (input) => {
        const next = await login(input);
        setSession(next);
      },
      registerAction: async (input) => {
        const next = await register(input);
        setSession(next);
      },
      logoutAction: () => {
        clearSession();
        setSession(null);
      },
      refreshSession
    }),
    [session, loading]
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth() {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used inside AuthProvider');
  }
  return ctx;
}
