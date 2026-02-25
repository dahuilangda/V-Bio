import { useCallback, useEffect, useMemo, useState } from 'react';
import { Check, ChevronLeft, ChevronRight, ChevronsLeft, ChevronsRight, Eye, EyeOff, Pencil, Search, Star, Trash2, X } from 'lucide-react';
import {
  deleteLeadOptimizationMmpDatabaseAdmin,
  fetchLeadOptimizationMmpDatabases,
  patchLeadOptimizationMmpDatabaseAdmin,
  type LeadOptMmpDatabaseItem
} from '../../api/backendApi';

interface MmpDatabaseAdminPanelProps {
  compact?: boolean;
}

const MMP_DB_CATALOG_CACHE_MS = 15_000;
const MMP_DB_PAGE_SIZE = 10;
let mmpDbCatalogCacheRows: LeadOptMmpDatabaseItem[] = [];
let mmpDbCatalogCacheAt = 0;

function readMmpDbCatalogCache(): LeadOptMmpDatabaseItem[] {
  if (Date.now() - mmpDbCatalogCacheAt > MMP_DB_CATALOG_CACHE_MS) return [];
  return Array.isArray(mmpDbCatalogCacheRows) ? mmpDbCatalogCacheRows : [];
}

function writeMmpDbCatalogCache(rows: LeadOptMmpDatabaseItem[]): void {
  mmpDbCatalogCacheRows = Array.isArray(rows) ? rows : [];
  mmpDbCatalogCacheAt = Date.now();
}

export function MmpDatabaseAdminPanel({ compact = false }: MmpDatabaseAdminPanelProps) {
  const [databases, setDatabases] = useState<LeadOptMmpDatabaseItem[]>(() => readMmpDbCatalogCache());
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [editingLabelId, setEditingLabelId] = useState('');
  const [editingLabelValue, setEditingLabelValue] = useState('');
  const [searchQuery, setSearchQuery] = useState('');
  const [page, setPage] = useState(1);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState<string | null>(null);

  const loadCatalog = useCallback(async (options?: { silent?: boolean }) => {
    const silent = options?.silent === true;
    if (!silent) setLoading(true);
    setError(null);
    try {
      const catalog = await fetchLeadOptimizationMmpDatabases({ includeHidden: true });
      const rows = Array.isArray(catalog.databases) ? catalog.databases : [];
      const nextRows = rows.filter((item) => String(item.backend || '').toLowerCase() === 'postgres');
      setDatabases(nextRows);
      writeMmpDbCatalogCache(nextRows);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load MMP databases.');
    } finally {
      if (!silent) setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadCatalog({ silent: databases.length > 0 });
  }, [loadCatalog, databases.length]);

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

  const beginEditLabel = (databaseId: string, currentLabel: string) => {
    setEditingLabelId(String(databaseId || '').trim());
    setEditingLabelValue(String(currentLabel || '').trim());
    setError(null);
    setSuccess(null);
  };

  const cancelEditLabel = () => {
    setEditingLabelId('');
    setEditingLabelValue('');
  };

  const saveLabel = async (databaseId: string) => {
    const normalizedId = String(databaseId || '').trim();
    if (!normalizedId) return;
    const nextLabel = String(editingLabelValue || '').trim();
    setSaving(true);
    setError(null);
    setSuccess(null);
    try {
      await patchLeadOptimizationMmpDatabaseAdmin(normalizedId, { label: nextLabel });
      await loadCatalog();
      setSuccess('Database label updated.');
      cancelEditLabel();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to update database label.');
    } finally {
      setSaving(false);
    }
  };

  const deleteDatabase = async (databaseId: string, schema: string, isDefault: boolean) => {
    if (!databaseId) return;
    if (schema.trim().toLowerCase() === 'public' || isDefault) return;
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

  const formatCount = (value: unknown): string => {
    if (typeof value === 'number' && Number.isFinite(value) && value >= 0) {
      return value.toLocaleString();
    }
    if (typeof value === 'string') {
      const num = Number(value);
      if (Number.isFinite(num) && num >= 0) {
        return Math.trunc(num).toLocaleString();
      }
    }
    return '-';
  };

  const formatProperties = (items: LeadOptMmpDatabaseItem['properties']): string => {
    if (!Array.isArray(items) || items.length === 0) return '-';
    const seen = new Set<string>();
    const rows: string[] = [];
    for (const item of items) {
      const name = String(item.name || item.label || item.display_name || '').trim();
      if (!name || seen.has(name)) continue;
      seen.add(name);
      rows.push(name);
    }
    return rows.length > 0 ? rows.join(', ') : '-';
  };

  const readDatabaseState = (db: LeadOptMmpDatabaseItem): 'ready' | 'building' | 'failed' => {
    const status = String(db.status || '').trim().toLowerCase();
    if (status === 'ready') return 'ready';
    if (status === 'failed') return 'failed';
    return 'building';
  };

  const filteredDatabases = useMemo(() => {
    const q = String(searchQuery || '').trim().toLowerCase();
    if (!q) return databases;
    return databases.filter((db) => {
      const status = readDatabaseState(db);
      const tokens = [
        String(db.id || ''),
        String(db.label || ''),
        String(db.schema || ''),
        String(db.source || ''),
        status,
        formatProperties(db.properties || []),
      ]
        .map((item) => item.trim().toLowerCase())
        .filter(Boolean);
      return tokens.some((item) => item.includes(q));
    });
  }, [databases, searchQuery]);

  const totalPages = Math.max(1, Math.ceil(filteredDatabases.length / MMP_DB_PAGE_SIZE));

  const pagedDatabases = useMemo(() => {
    const start = (page - 1) * MMP_DB_PAGE_SIZE;
    return filteredDatabases.slice(start, start + MMP_DB_PAGE_SIZE);
  }, [filteredDatabases, page]);

  const pageStart = filteredDatabases.length === 0 ? 0 : (page - 1) * MMP_DB_PAGE_SIZE + 1;
  const pageEnd = Math.min(filteredDatabases.length, page * MMP_DB_PAGE_SIZE);

  useEffect(() => {
    setPage(1);
  }, [searchQuery]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  return (
    <section className="panel settings-admin-panel">
      {!compact ? (
        <div className="settings-panel-head">
          <h2>Lead Opt Databases</h2>
          <p className="muted">Global catalog. Create/import from CLI only.</p>
        </div>
      ) : null}

      {error ? <div className="alert error">{error}</div> : null}
      {success ? <div className="alert success">{success}</div> : null}

      {loading ? (
        <div className="muted">Loading MMP databases...</div>
      ) : (
        <>
          <div className="toolbar project-toolbar mmp-life-list-toolbar leadopt-db-toolbar">
            <div className="project-toolbar-filters">
              <div className="project-filter-field project-filter-field-search">
                <div className="input-wrap search-input">
                  <Search size={16} />
                  <input
                    value={searchQuery}
                    onChange={(event) => setSearchQuery(event.target.value)}
                    placeholder="Search label / schema / status / property"
                    aria-label="Search lead opt databases"
                  />
                </div>
              </div>
            </div>
            <div className="project-toolbar-meta project-toolbar-meta-rich">
              <span className="meta-chip">Rows {filteredDatabases.length}</span>
              <span className="meta-chip">Page {page}/{totalPages}</span>
            </div>
          </div>
          <div className="table-wrap">
            <table className="table settings-admin-table">
              <thead>
                <tr>
                  <th>Label</th>
                  <th>Schema</th>
                  <th>Counts (Cmp/Rule/Pair)</th>
                  <th>Status</th>
                  <th>Properties</th>
                  <th>Visible</th>
                  <th>Default</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {pagedDatabases.length === 0 ? (
                  <tr>
                    <td colSpan={8} className="muted">No databases match the current search.</td>
                  </tr>
                ) : (
                  pagedDatabases.map((db) => {
                const id = String(db.id || '');
                const schema = String(db.schema || '');
                const isPublicSchema = schema.trim().toLowerCase() === 'public';
                const isDefault = db.is_default === true;
                const rawLabel = String(db.label || '').trim();
                const sourceLabel = String(db.source || '').trim();
                const normalizedSchema = schema.trim().toLowerCase();
                const labelFromSchema =
                  Boolean(rawLabel) && Boolean(schema) && rawLabel.trim().toLowerCase() === normalizedSchema;
                const displayLabel = (() => {
                  if (rawLabel) {
                    if (
                      labelFromSchema &&
                      sourceLabel &&
                      sourceLabel.trim().toLowerCase() !== normalizedSchema
                    ) {
                      return sourceLabel;
                    }
                    return rawLabel;
                  }
                  if (sourceLabel) return sourceLabel;
                  if (id) return id;
                  return '-';
                })();
                const counts = db.stats || {};
                const countsText = `${formatCount(counts.compounds)} / ${formatCount(counts.rules)} / ${formatCount(counts.pairs)}`;
                const state = readDatabaseState(db);
                const statusLabel = state === 'ready' ? 'Ready' : state === 'failed' ? 'Failed' : 'Syncing';
                const statusShort = state === 'ready' ? 'R' : state === 'failed' ? 'F' : 'S';
                const propertiesText = formatProperties(db.properties);
                const isEditingLabel = editingLabelId === id;
                const deleteDisabled = saving || isPublicSchema || isDefault;
                const deleteTitle = isPublicSchema
                  ? 'public schema cannot be deleted'
                  : isDefault
                    ? 'Default database cannot be deleted'
                    : 'Delete schema';
                return (
                  <tr key={id}>
                    <td>
                      {isEditingLabel ? (
                        <div className="leadopt-db-label-edit-row">
                          <input
                            className="leadopt-db-label-input"
                            value={editingLabelValue}
                            onChange={(event) => setEditingLabelValue(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter') {
                                event.preventDefault();
                                void saveLabel(id);
                              }
                              if (event.key === 'Escape') {
                                event.preventDefault();
                                cancelEditLabel();
                              }
                            }}
                            placeholder={schema || 'database label'}
                            disabled={saving}
                            autoFocus
                          />
                          <button
                            className="leadopt-db-label-action"
                            type="button"
                            onClick={() => void saveLabel(id)}
                            disabled={saving}
                            title="Save label"
                            aria-label="Save label"
                          >
                            <Check size={13} />
                          </button>
                          <button
                            className="leadopt-db-label-action"
                            type="button"
                            onClick={cancelEditLabel}
                            disabled={saving}
                            title="Cancel edit"
                            aria-label="Cancel edit"
                          >
                            <X size={13} />
                          </button>
                        </div>
                      ) : (
                        <div className="leadopt-db-label-cell">
                          <span className="leadopt-db-label-main">{displayLabel || '-'}</span>
                          {labelFromSchema ? <span className="leadopt-db-label-note">Alias not set (same as schema)</span> : null}
                          <button
                            className="leadopt-db-label-edit-btn"
                            type="button"
                            onClick={() => beginEditLabel(id, rawLabel || displayLabel)}
                            disabled={saving}
                            title="Edit label"
                            aria-label="Edit label"
                          >
                            <Pencil size={12} />
                          </button>
                        </div>
                      )}
                    </td>
                    <td>
                      <code className="leadopt-db-schema-code">{schema || '-'}</code>
                    </td>
                    <td title="Compounds / Rules / Pairs">{countsText}</td>
                    <td>
                      <span
                        className={`leadopt-db-status-dot ${state}`}
                        title={statusLabel}
                        aria-label={statusLabel}
                      >
                        {statusShort}
                      </span>
                    </td>
                    <td className="leadopt-db-props" title={propertiesText}>
                      {propertiesText}
                    </td>
                    <td>{db.visible === false ? 'No' : 'Yes'}</td>
                    <td>{db.is_default ? 'Yes' : 'No'}</td>
                    <td>
                      <div className="leadopt-db-actions">
                        <button
                          className="leadopt-db-action"
                          type="button"
                          onClick={() => void updateVisibility(id, !(db.visible === false))}
                          disabled={saving}
                          title={db.visible === false ? 'Show database' : 'Hide database'}
                          aria-label={db.visible === false ? 'Show database' : 'Hide database'}
                        >
                          {db.visible === false ? <Eye size={14} /> : <EyeOff size={14} />}
                        </button>
                        <button
                          className="leadopt-db-action"
                          type="button"
                          onClick={() => void setDefault(id)}
                          disabled={saving || isDefault}
                          title={isDefault ? 'Default database' : 'Set default database'}
                          aria-label={isDefault ? 'Default database' : 'Set default database'}
                        >
                          <Star size={14} />
                        </button>
                        <button
                          className="leadopt-db-action leadopt-db-action--danger"
                          type="button"
                          onClick={() => void deleteDatabase(id, schema, isDefault)}
                          disabled={deleteDisabled}
                          title={deleteTitle}
                          aria-label={deleteTitle}
                        >
                          <Trash2 size={14} />
                        </button>
                      </div>
                    </td>
                  </tr>
                  );
                })
              )}
              </tbody>
            </table>
          </div>
          <div className="project-pagination leadopt-db-pagination">
            <div className="project-pagination-controls">
              <button
                className="icon-btn"
                type="button"
                onClick={() => setPage(1)}
                disabled={page <= 1}
                title="First page"
                aria-label="First page"
              >
                <ChevronsLeft size={14} />
              </button>
              <button
                className="icon-btn"
                type="button"
                onClick={() => setPage((prev) => Math.max(1, prev - 1))}
                disabled={page <= 1}
                title="Previous page"
                aria-label="Previous page"
              >
                <ChevronLeft size={14} />
              </button>
              <button
                className="icon-btn"
                type="button"
                onClick={() => setPage((prev) => Math.min(totalPages, prev + 1))}
                disabled={page >= totalPages}
                title="Next page"
                aria-label="Next page"
              >
                <ChevronRight size={14} />
              </button>
              <button
                className="icon-btn"
                type="button"
                onClick={() => setPage(totalPages)}
                disabled={page >= totalPages}
                title="Last page"
                aria-label="Last page"
              >
                <ChevronsRight size={14} />
              </button>
            </div>
            <div className="project-pagination-info muted small">
              {filteredDatabases.length === 0 ? 'No rows' : `Showing ${pageStart}-${pageEnd} of ${filteredDatabases.length}`}
            </div>
          </div>
        </>
      )}
    </section>
  );
}
