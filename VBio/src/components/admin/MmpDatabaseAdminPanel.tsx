import { useCallback, useEffect, useState } from 'react';
import {
  deleteLeadOptimizationMmpDatabaseAdmin,
  fetchLeadOptimizationMmpDatabases,
  patchLeadOptimizationMmpDatabaseAdmin,
  type LeadOptMmpDatabaseItem
} from '../../api/backendApi';

interface MmpDatabaseAdminPanelProps {
  compact?: boolean;
}

export function MmpDatabaseAdminPanel({ compact = false }: MmpDatabaseAdminPanelProps) {
  const [databases, setDatabases] = useState<LeadOptMmpDatabaseItem[]>([]);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const loadCatalog = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const catalog = await fetchLeadOptimizationMmpDatabases({ includeHidden: true });
      const rows = Array.isArray(catalog.databases) ? catalog.databases : [];
      setDatabases(rows.filter((item) => String(item.backend || '').toLowerCase() === 'postgres'));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load MMP databases.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCatalog();
  }, [loadCatalog]);

  const updateVisibility = async (databaseId: string, visible: boolean) => {
    if (!databaseId) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await patchLeadOptimizationMmpDatabaseAdmin(databaseId, { visible });
      await loadCatalog();
      setSuccess('Database visibility updated.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update visibility.');
    } finally {
      setSaving(false);
    }
  };

  const setDefault = async (databaseId: string) => {
    if (!databaseId) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await patchLeadOptimizationMmpDatabaseAdmin(databaseId, { is_default: true });
      await loadCatalog();
      setSuccess('Default database updated.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to set default database.');
    } finally {
      setSaving(false);
    }
  };

  const deleteDatabase = async (databaseId: string, schema: string) => {
    if (!databaseId) return;
    if (schema.trim().toLowerCase() === 'public') return;
    const confirmed = window.confirm(`Delete schema "${schema}" permanently?`);
    if (!confirmed) return;
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await deleteLeadOptimizationMmpDatabaseAdmin(databaseId, { dropData: true });
      await loadCatalog();
      setSuccess('Database deleted.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete database.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <section className="panel settings-admin-panel">
      <div className="settings-panel-head">
        <h2>Lead Opt Databases</h2>
        {!compact ? <p className="muted">Global catalog. Create/import from CLI only.</p> : null}
      </div>

      <div className="settings-admin-hint muted">
        Recommended import source: `ChEMBL_CYP3A4_hERG`.
      </div>

      {error ? <div className="alert error">{error}</div> : null}
      {success ? <div className="alert success">{success}</div> : null}

      {loading ? (
        <div className="muted">Loading MMP databases...</div>
      ) : (
        <div className="table-wrap">
          <table className="table settings-admin-table">
            <thead>
              <tr>
                <th>Label</th>
                <th>Schema</th>
                <th>Visible</th>
                <th>Default</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {databases.map((db) => {
                const id = String(db.id || '');
                const schema = String(db.schema || '');
                const isPublicSchema = schema.trim().toLowerCase() === 'public';
                const displayLabel = String(db.label || id || '-').trim();
                return (
                  <tr key={id}>
                    <td>{displayLabel || '-'}</td>
                    <td>{schema || '-'}</td>
                    <td>{db.visible === false ? 'No' : 'Yes'}</td>
                    <td>{db.is_default ? 'Yes' : 'No'}</td>
                    <td>
                      <div className="row gap-6">
                        <button
                          className="btn btn-ghost btn-compact"
                          type="button"
                          onClick={() => void updateVisibility(id, !(db.visible === false))}
                          disabled={saving}
                        >
                          {db.visible === false ? 'Show' : 'Hide'}
                        </button>
                        <button
                          className="btn btn-ghost btn-compact"
                          type="button"
                          onClick={() => void setDefault(id)}
                          disabled={saving}
                        >
                          Set Default
                        </button>
                        <button
                          className="btn btn-danger btn-compact"
                          type="button"
                          onClick={() => void deleteDatabase(id, schema)}
                          disabled={saving || isPublicSchema}
                          title={isPublicSchema ? 'public schema cannot be deleted' : 'Delete schema'}
                        >
                          Delete
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
