import { FormEvent, useEffect, useState } from 'react';
import { Download, KeyRound, Power, PowerOff, RotateCcw, Trash2 } from 'lucide-react';
import {
  createJwtClient,
  deleteJwtClient,
  listJwtClients,
  rotateJwtClient,
  updateJwtClient,
  type JwtClientRecord
} from '../api/jwtClientsApi';
import { useAuth } from '../hooks/useAuth';

export function JwtClientsPage() {
  const { session } = useAuth();
  const managementToken = session?.managementToken || '';
  const canManage = Boolean(managementToken);
  const [clients, setClients] = useState<JwtClientRecord[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visibleSecret, setVisibleSecret] = useState<{ clientId: string; secret: string } | null>(null);

  const loadClients = async () => {
    if (!managementToken) return;
    setLoading(true);
    setError(null);
    setVisibleSecret(null);
    try {
      setClients(await listJwtClients(managementToken));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load integrations.');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    void loadClients();
  }, [managementToken]);

  const onCreateClient = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setError(null);
    setVisibleSecret(null);
    const formElement = event.currentTarget;
    const form = new FormData(formElement);
    try {
      if (!managementToken) throw new Error('Please sign in again.');
      const result = await createJwtClient(managementToken, {
        name: String(form.get('name') || '').trim(),
        issuer: String(form.get('issuer') || 'navigation').trim(),
        audience: String(form.get('audience') || 'vbio').trim(),
        max_ttl_seconds: Number(form.get('max_ttl_seconds') || 300)
      });
      setClients((prev) => [result.client, ...prev]);
      setVisibleSecret({ clientId: result.client.client_id, secret: result.secret });
      formElement.reset();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create integration.');
    }
  };

  const toggleClient = async (client: JwtClientRecord) => {
    setError(null);
    setVisibleSecret(null);
    try {
      if (!managementToken) throw new Error('Please sign in again.');
      const next = await updateJwtClient(managementToken, client.client_id, { active: !client.active });
      setClients((prev) => prev.map((item) => (item.client_id === next.client_id ? next : item)));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update integration.');
    }
  };

  const rotateSecret = async (client: JwtClientRecord) => {
    if (!window.confirm(`Rotate secret for "${client.name}"? The old secret will stop working.`)) return;
    setError(null);
    setVisibleSecret(null);
    try {
      if (!managementToken) throw new Error('Please sign in again.');
      const result = await rotateJwtClient(managementToken, client.client_id);
      setClients((prev) => prev.map((item) => (item.client_id === result.client.client_id ? result.client : item)));
      setVisibleSecret({ clientId: result.client.client_id, secret: result.secret });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to rotate integration secret.');
    }
  };

  const buildClientConfigText = (client: JwtClientRecord, clientSecret: string) => {
    const vbioBaseUrl = typeof window !== 'undefined' ? window.location.origin : 'https://<vbio-host>';
    const loginUrl = `${vbioBaseUrl}/auth/jwt?token=<JWT>&next=/projects`;
    return [
      'V-Bio external login',
      '',
      `V-Bio URL: ${vbioBaseUrl}`,
      `Login redirect: ${loginUrl}`,
      '',
      'Client',
      `name=${client.name}`,
      `client_id=${client.client_id}`,
      `secret=${clientSecret}`,
      `issuer=${client.issuer}`,
      `audience=${client.audience}`,
      `ttl_seconds=${client.max_ttl_seconds}`,
      '',
      'JWT header',
      JSON.stringify({ alg: 'HS256', typ: 'JWT', kid: client.client_id }),
      '',
      'JWT payload',
      '{',
      `  "iss": "${client.issuer}",`,
      `  "aud": "${client.audience}",`,
      '  "sub": "<external-user-id>",',
      '  "username": "<username>",',
      '  "email": "<user@example.com>",',
      '  "name": "<display-name>",',
      '  "iat": "<unix-seconds>",',
      '  "exp": "<unix-seconds>"',
      '}',
      '',
      'Signing',
      'algorithm=HS256',
      'signing_input=base64url(header) + "." + base64url(payload)',
      'signature=HMAC-SHA256(signing_input, secret)',
      'jwt=signing_input + "." + base64url(signature)',
      '',
      'Call',
      'After the user signs in to your system, create the JWT on your backend and redirect the browser to:',
      loginUrl,
      '',
      'Do not send the secret to browser code, mobile apps, logs, or URL parameters.'
    ].join('\n');
  };

  const downloadClientConfig = (client: JwtClientRecord) => {
    const clientSecret = visibleSecret?.clientId === client.client_id ? visibleSecret.secret : '';
    if (!clientSecret) {
      setError('Rotate this integration, then download the config file with the new secret.');
      return;
    }
    setError(null);
    const fileName = `${client.name || client.client_id}-vbio-integration.txt`
      .replace(/[^a-zA-Z0-9._-]+/g, '-')
      .replace(/^-+|-+$/g, '')
      .toLowerCase();
    const blob = new Blob([buildClientConfigText(client, clientSecret)], { type: 'text/plain;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = fileName || 'vbio-integration.txt';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  };

  const removeClient = async (client: JwtClientRecord) => {
    if (!window.confirm(`Delete integration "${client.name}"?`)) return;
    setError(null);
    setVisibleSecret(null);
    try {
      if (!managementToken) throw new Error('Please sign in again.');
      await deleteJwtClient(managementToken, client.client_id);
      setClients((prev) => prev.filter((item) => item.client_id !== client.client_id));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete JWT client.');
    }
  };

  return (
    <div className="page-grid users-page">
      <section className="page-header">
        <div>
          <h1>External Integrations</h1>
        </div>
      </section>

      {!canManage ? (
        <div className="alert error">Sign out and sign in again to enable integration management.</div>
      ) : null}
      {loading ? <div className="muted">Loading integrations...</div> : null}

      <section className="panel">
        <div className="settings-panel-head">
          <h2><KeyRound size={18} /> Create Integration</h2>
        </div>
        <form className="form-grid integration-create" onSubmit={onCreateClient}>
          <label className="field">
            <span>Name</span>
            <input name="name" placeholder="External system" required />
          </label>
          <label className="field">
            <span>Issuer</span>
            <input name="issuer" defaultValue="navigation" required />
          </label>
          <label className="field">
            <span>Audience</span>
            <input name="audience" defaultValue="vbio" required />
          </label>
          <label className="field">
            <span>TTL seconds</span>
            <input name="max_ttl_seconds" type="number" min="60" max="3600" defaultValue="300" required />
          </label>
          <button className="btn btn-primary" type="submit" disabled={!canManage}>Create</button>
        </form>
        {visibleSecret ? (
          <div className="token-plain-block">
            <strong>Client secret</strong>
            <code>{visibleSecret.secret}</code>
          </div>
        ) : null}
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
                <th>Issuer</th>
                <th>Audience</th>
                <th>TTL</th>
                <th>Status</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {clients.map((client) => (
                <tr key={client.client_id}>
                  <td><code>{client.client_id}</code></td>
                  <td>{client.name}</td>
                  <td>{client.issuer}</td>
                  <td>{client.audience}</td>
                  <td>{client.max_ttl_seconds}s</td>
                  <td>{client.active ? 'Active' : 'Disabled'}</td>
                  <td>
                    <div className="integration-actions">
                      <button
                        className="icon-btn"
                        type="button"
                        title="Download integration config"
                        aria-label={`Download config for ${client.name}`}
                        onClick={() => downloadClientConfig(client)}
                      >
                        <Download size={14} />
                      </button>
                      <button
                        className="icon-btn"
                        type="button"
                        title={client.active ? 'Disable integration' : 'Enable integration'}
                        aria-label={`${client.active ? 'Disable' : 'Enable'} ${client.name}`}
                        onClick={() => void toggleClient(client)}
                      >
                        {client.active ? <PowerOff size={14} /> : <Power size={14} />}
                      </button>
                      <button
                        className="icon-btn"
                        type="button"
                        title="Rotate secret"
                        aria-label={`Rotate secret for ${client.name}`}
                        onClick={() => void rotateSecret(client)}
                      >
                        <RotateCcw size={14} />
                      </button>
                      <button
                        className="icon-btn danger"
                        type="button"
                        title="Delete integration"
                        aria-label={`Delete ${client.name}`}
                        onClick={() => void removeClient(client)}
                      >
                        <Trash2 size={14} />
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
