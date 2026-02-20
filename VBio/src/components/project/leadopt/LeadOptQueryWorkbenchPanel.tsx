import type { MouseEvent } from 'react';

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function readNumber(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function formatMetric(value: unknown, digits = 2): string {
  const numeric = readNumber(value);
  if (!Number.isFinite(numeric)) return '-';
  return numeric.toFixed(digits);
}

interface LeadOptQueryWorkbenchPanelProps {
  loading: boolean;
  queryNotice: string;
  queryMode: 'one-to-many' | 'many-to-many';
  transforms: Array<Record<string, unknown>>;
  activeTransformId: string;
  selectedTransformIds: string[];
  evidenceLoading: boolean;
  evidencePairs: Array<Record<string, unknown>>;
  activeTransformSummary: {
    nPairs: string;
    medianDelta: string;
    iqr: string;
    std: string;
    percentImproved: string;
    directionality: string;
    evidenceStrength: string;
  };
  hasSelection: boolean;
  onTransformOpen: (row: Record<string, unknown>) => void;
  onToggleTransformSelection: (transformId: string) => void;
  onSelectTopTransforms: (limit?: number) => void;
  onClearSelection: () => void;
  onRunEnumerate: () => void;
}

export function LeadOptQueryWorkbenchPanel({
  loading,
  queryNotice,
  queryMode,
  transforms,
  activeTransformId,
  selectedTransformIds,
  evidenceLoading,
  evidencePairs,
  activeTransformSummary,
  hasSelection,
  onTransformOpen,
  onToggleTransformSelection,
  onSelectTopTransforms,
  onClearSelection,
  onRunEnumerate
}: LeadOptQueryWorkbenchPanelProps) {
  const handleSelectStop = (event: MouseEvent) => {
    event.stopPropagation();
  };

  return (
    <section className="panel subtle lead-opt-panel lead-opt-workbench-panel">
      <div className="lead-opt-panel-head">
        <h3>查询</h3>
        <div className="lead-opt-workbench-meta">
          <span className="badge">{queryMode === 'many-to-many' ? 'Multi' : 'Single'}</span>
          <span className="badge">{transforms.length} transforms</span>
          <span className="badge">{selectedTransformIds.length} selected</span>
          <div className="row">
            <button
              type="button"
              className="btn btn-ghost btn-compact"
              onClick={() => onSelectTopTransforms(12)}
              disabled={loading || transforms.length === 0}
            >
              Select Top
            </button>
            <button type="button" className="btn btn-ghost btn-compact" onClick={onClearSelection} disabled={loading || !hasSelection}>
              Clear
            </button>
            <button
              type="button"
              className="btn btn-primary btn-compact"
              onClick={onRunEnumerate}
              disabled={loading || (!hasSelection && transforms.length === 0)}
            >
              Enumerate
            </button>
          </div>
        </div>
      </div>

      {queryNotice ? <p className="small muted">{queryNotice}</p> : null}

      <div className="lead-opt-workbench-group">
        <div className="lead-opt-workbench-group-head">
          <strong>Transforms</strong>
          <span className="small muted">Click row to inspect evidence</span>
        </div>
        {transforms.length === 0 ? (
          <p className="small muted">Run query to generate transforms.</p>
        ) : (
          <div className="lead-opt-result-table-wrap">
            <table className="lead-opt-result-table">
              <thead>
                <tr>
                  <th>#</th>
                  <th>From</th>
                  <th>To</th>
                  <th>n</th>
                  <th>Δ</th>
                  <th>IQR</th>
                  <th>Select</th>
                </tr>
              </thead>
              <tbody>
                {transforms.slice(0, 24).map((row, index) => {
                  const transformId = readText(row.transform_id);
                  const selected = selectedTransformIds.includes(transformId);
                  const active = activeTransformId === transformId;
                  return (
                    <tr
                      key={transformId || `${readText(row.from_smiles)}>>${readText(row.to_smiles)}`}
                      className={active ? 'selected' : ''}
                      onClick={() => onTransformOpen(row)}
                    >
                      <td>{index + 1}</td>
                      <td className="col-smiles">{readText(row.from_smiles) || '-'}</td>
                      <td className="col-smiles">{readText(row.to_smiles) || '-'}</td>
                      <td>{readText(row.n_pairs) || '-'}</td>
                      <td>{formatMetric(row.median_delta)}</td>
                      <td>{formatMetric(row.iqr)}</td>
                      <td onClick={handleSelectStop}>
                        <input type="checkbox" checked={selected} onChange={() => onToggleTransformSelection(transformId)} />
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </div>

      <div className="lead-opt-workbench-group lead-opt-evidence-wrap">
        <div className="lead-opt-workbench-group-head">
          <strong>Evidence</strong>
          {evidenceLoading ? <span className="small muted">Loading...</span> : null}
        </div>
        {activeTransformId ? (
          <>
            <p className="small muted">
              n={activeTransformSummary.nPairs || '-'} · Δ={activeTransformSummary.medianDelta || '-'} · evidence=
              {activeTransformSummary.evidenceStrength || '-'}
            </p>
            <div className="lead-opt-result-table-wrap">
              {evidencePairs.length === 0 ? (
                <p className="small muted">No cached pair rows for this transform.</p>
              ) : (
                <table className="lead-opt-result-table">
                  <thead>
                    <tr>
                      <th>Env</th>
                      <th>n</th>
                      <th>Δ</th>
                      <th>From</th>
                      <th>To</th>
                    </tr>
                  </thead>
                  <tbody>
                    {evidencePairs.slice(0, 24).map((row, idx) => (
                      <tr key={`${readText(row.transform_id)}-${idx}`}>
                        <td>{readText(row.rule_environment_id) || '-'}</td>
                        <td>{readText(row.n_pairs) || '-'}</td>
                        <td>{formatMetric(row.median_delta)}</td>
                        <td className="col-smiles">{readText(row.from_smiles) || '-'}</td>
                        <td className="col-smiles">{readText(row.to_smiles) || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </>
        ) : (
          <p className="small muted">Select a transform row to inspect evidence rows.</p>
        )}
      </div>
    </section>
  );
}
