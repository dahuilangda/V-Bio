import { FormEvent, useEffect, useState } from 'react';
import { Clipboard, KeyRound, LoaderCircle, Power, PowerOff, Trash2 } from 'lucide-react';
import {
  createJwtClient,
  deleteJwtClient,
  issueJwtClientToken,
  listJwtClients,
  updateJwtClient,
  type JwtClientRecord
} from '../api/jwtClientsApi';
import { useAuth } from '../hooks/useAuth';

export function JwtClientsPage() {
  const { session, loading: authLoading, ensureManagementSession } = useAuth();
  const [managementToken, setManagementToken] = useState('');
  const canManage = Boolean(managementToken);
  const [clients, setClients] = useState<JwtClientRecord[]>([]);
  const [busyClientId, setBusyClientId] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [visibleToken, setVisibleToken] = useState<{
    clientId: string;
    token: string;
    expiresAt: number;
  } | null>(null);

  useEffect(() => {
    if (authLoading) return;
    let cancelled = false;
    const loadClients = async () => {
      setLoading(true);
      setError(null);
      setNotice(null);
      setVisibleToken(null);
      try {
        const token = await ensureManagementSession();
        if (!token) throw new Error('Administrator management session is unavailable.');
        const nextClients = await listJwtClients(token);
        if (cancelled) return;
        setManagementToken(token);
        setClients(nextClients);
      } catch (err) {
        if (cancelled) return;
        setManagementToken('');
        setError(err instanceof Error ? err.message : 'Failed to load integrations.');
      } finally {
        if (!cancelled) setLoading(false);
      }
    };
    void loadClients();
    return () => {
      cancelled = true;
    };
  }, [authLoading, session?.userId]);

  const onCreateClient = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setNotice(null);
    setVisibleToken(null);
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    try {
      if (!managementToken) throw new Error('Administrator management session is unavailable.');
      const result = await createJwtClient(managementToken, {
        name: String(form.get('name') || '').trim(),
        issuer: 'navigation',
        audience: 'vbio',
        max_ttl_seconds: 300
      });
      setClients((prev) => [result.client, ...prev]);
      setVisibleToken({
        clientId: result.client.client_id,
        token: result.token,
        expiresAt: result.expires_at
      });
      formElement.reset();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create integration.');
    }
  };

  const toggleClient = async (client: JwtClientRecord) => {
    if (busyClientId) return;
    setBusyClientId(client.client_id);
    setError(null);
    setNotice(null);
    setVisibleToken(null);
    try {
      if (!managementToken) throw new Error('Administrator management session is unavailable.');
      const next = await updateJwtClient(managementToken, client.client_id, { active: !client.active });
      setClients((prev) => prev.map((item) => (item.client_id === next.client_id ? next : item)));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update integration.');
    } finally {
      setBusyClientId(null);
    }
  };

  const issueToken = async (client: JwtClientRecord) => {
    if (busyClientId) return;
    setBusyClientId(client.client_id);
    setError(null);
    setNotice(null);
    setVisibleToken(null);
    try {
      if (!managementToken) throw new Error('Administrator management session is unavailable.');
      const result = await issueJwtClientToken(managementToken, client.client_id);
      setVisibleToken({
        clientId: result.client.client_id,
        token: result.token,
        expiresAt: result.expires_at
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to issue JWT.');
    } finally {
      setBusyClientId(null);
    }
  };

  const copyVisibleToken = async () => {
    if (!visibleToken?.token) return;
    try {
      await navigator.clipboard.writeText(visibleToken.token);
      setNotice('JWT copied.');
      setError(null);
    } catch {
      setError('Unable to copy JWT. Select the token and copy it manually.');
    }
  };

  const removeClient = async (client: JwtClientRecord) => {
    if (busyClientId) return;
    if (!window.confirm(`Delete integration "${client.name}"?`)) return;
    setBusyClientId(client.client_id);
    setError(null);
    setNotice(null);
    setVisibleToken(null);
    try {
      if (!managementToken) throw new Error('Administrator management session is unavailable.');
      await deleteJwtClient(managementToken, client.client_id);
      setClients((prev) => prev.filter((item) => item.client_id !== client.client_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete JWT client.');
    } finally {
      setBusyClientId(null);
    }
  };

  return (
    <div className="page-grid users-page">
      <section className="page-header">
        <div>
          <h1>JWT Integrations</h1>
          <p className="muted">Create a short-lived JWT for an external system.</p>
        </div>
      </section>

      {!loading && !canManage ? (
        <div className="alert error">Administrator management session is unavailable.</div>
      ) : null}
      {loading ? <div className="muted">Loading integrations...</div> : null}

      <section className="panel">
        <div className="settings-panel-head">
          <h2><KeyRound size={18} /> Create JWT</h2>
        </div>
        <form className="form-grid integration-create" onSubmit={onCreateClient}>
          <label className="field">
            <span>Name</span>
            <input name="name" placeholder="External system" required />
          </label>
          <button className="btn btn-primary" type="submit" disabled={!canManage}>Create JWT</button>
        </form>

        {visibleToken ? (
          <div className="token-plain-block integration-token-block">
            <strong>JWT</strong>
            <textarea
              className="integration-jwt-value"
              value={visibleToken.token}
              readOnly
              rows={4}
              aria-label="Issued JWT"
            />
            <div className="muted small">
              Expires {new Date(visibleToken.expiresAt * 1000).toLocaleString()}
            </div>
            <button className="btn btn-secondary btn-compact" type="button" onClick={() => void copyVisibleToken()}>
              <Clipboard size={14} /> Copy JWT
            </button>
          </div>
        ) : null}
        {notice ? <div className="alert success">{notice}</div> : null}
        {error ? <div className="alert error">{error}</div> : null}
      </section>

      <section className="panel">
        <h2>Integrations</h2>
        <div className="table-wrap">
          <table className="table">
            <thead>
              <tr>
                <th>Client ID</th>
                <th>Name</th>
                <th>JWT lifetime</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {clients.map((client) => (
                <tr key={client.client_id}>
                  <td><code>{client.client_id}</code></td>
                  <td>{client.name}</td>
                  <td>{client.max_ttl_seconds}s</td>
                  <td>{client.active ? 'Active' : 'Disabled'}</td>
                  <td>
                    <div className="integration-actions">
                      <button
                        className="icon-btn"
                        type="button"
                        title="Issue new JWT"
                        aria-label={`Issue new JWT for ${client.name}`}
                        disabled={!client.active || busyClientId === client.client_id}
                        aria-busy={busyClientId === client.client_id}
                        onClick={() => void issueToken(client)}
                      >
                        {busyClientId === client.client_id ? <LoaderCircle size={14} className="spin" /> : <KeyRound size={14} />}
                      </button>
                      <button
                        className="icon-btn"
                        type="button"
                        title={client.active ? 'Disable integration' : 'Enable integration'}
                        aria-label={`${client.active ? 'Disable' : 'Enable'} ${client.name}`}
                        disabled={busyClientId === client.client_id}
                        aria-busy={busyClientId === client.client_id}
                        onClick={() => void toggleClient(client)}
                      >
                        {busyClientId === client.client_id ? <LoaderCircle size={14} className="spin" /> : client.active ? <PowerOff size={14} /> : <Power size={14} />}
                      </button>
                      <button
                        className="icon-btn danger"
                        type="button"
                        title="Delete integration"
                        aria-label={`Delete ${client.name}`}
                        disabled={busyClientId === client.client_id}
                        aria-busy={busyClientId === client.client_id}
                        onClick={() => void removeClient(client)}
                      >
                        {busyClientId === client.client_id ? <LoaderCircle size={14} className="spin" /> : <Trash2 size={14} />}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
