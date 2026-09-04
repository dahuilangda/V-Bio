import { useEffect, useState } from 'react';
import { MemoLigand2DPreview } from '../Ligand2DPreview';
import { fetchLeadOptimizationHaloBackends, type LeadOptHaloBackend, type LeadOptHaloRoundEvent } from '../../../api/backendLeadOptimizationApi';
import type { LeadOptHaloCandidate } from './hooks/useLeadOptHaloRun';

/** Fallback while (or if) the backend listing is unavailable — must mirror
 * the backend's PredictOracle.SUPPORTED_BACKENDS ordering. */
const HALO_BACKEND_FALLBACK: Array<{ value: LeadOptHaloBackend; label: string }> = [
  { value: 'protenix2dock', label: 'Protenix2Dock' },
  { value: 'boltz2dock', label: 'Boltz2Dock' },
  { value: 'alphafold3', label: 'AlphaFold3' }
];

interface LeadOptHaloParamsPanelProps {
  canEdit: boolean;
  running: boolean;
  backend: LeadOptHaloBackend;
  rounds: number;
  budgetPerRound: number;
  pocketLabel: string;
  canRun: boolean;
  runDisabledReason: string;
  onBackendChange: (value: LeadOptHaloBackend) => void;
  onRoundsChange: (value: number) => void;
  onBudgetChange: (value: number) => void;
  onRun: () => void;
}

export function LeadOptHaloParamsPanel({
  canEdit,
  running,
  backend,
  rounds,
  budgetPerRound,
  pocketLabel,
  canRun,
  runDisabledReason,
  onBackendChange,
  onRoundsChange,
  onBudgetChange,
  onRun
}: LeadOptHaloParamsPanelProps) {
  const disabled = !canEdit || running || !canRun;
  const [backendOptions, setBackendOptions] = useState(HALO_BACKEND_FALLBACK);
  useEffect(() => {
    let cancelled = false;
    fetchLeadOptimizationHaloBackends()
      .then((payload) => {
        if (cancelled || !Array.isArray(payload.backends) || payload.backends.length === 0) return;
        setBackendOptions(payload.backends.map((entry) => ({ value: entry.id as LeadOptHaloBackend, label: entry.label || entry.id })));
      })
      .catch(() => {
        // Listing unavailable — the fallback mirrors the backend default.
      });
    return () => {
      cancelled = true;
    };
  }, []);
  return (
    <section className="panel subtle lead-opt-panel">
      <div className="lead-opt-panel-title">Optimization</div>
      <div className="lead-opt-halo-grid">
        <label className="field">
          <span>Scoring backend</span>
          <select value={backend} disabled={disabled} onChange={(e) => onBackendChange(e.target.value as LeadOptHaloBackend)}>
            {backendOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </label>
        <label className="field">
          <span>Iterations (rounds)</span>
          <input
            type="number"
            min={1}
            max={100}
            value={rounds}
            disabled={disabled}
            onChange={(e) => onRoundsChange(Math.max(1, Math.min(100, Math.floor(Number(e.target.value) || 1))))}
          />
        </label>
        <label className="field">
          <span>Oracle budget / round</span>
          <input
            type="number"
            min={1}
            max={512}
            value={budgetPerRound}
            disabled={disabled}
            onChange={(e) => onBudgetChange(Math.max(1, Math.min(512, Math.floor(Number(e.target.value) || 1))))}
          />
        </label>
        <div className="field field-span-2">
          <span>Pocket</span>
          <div className="muted small">{pocketLabel}</div>
        </div>
      </div>
      <div className="lead-opt-halo-run-row">
        <button
          type="button"
          className="btn btn-primary btn-compact"
          disabled={disabled}
          title={runDisabledReason || 'Run the iterative optimization'}
          onClick={onRun}
        >
          {running ? 'Optimizing…' : 'Run Optimization'}
        </button>
        {canRun || !runDisabledReason ? null : (
          <span className="lead-opt-error">{runDisabledReason}</span>
        )}
      </div>
    </section>
  );
}

export function LeadOptHaloProgressPanel({
  progress,
  roundsLog
}: {
  progress: LeadOptHaloRoundEvent | null;
  roundsLog: Array<Record<string, unknown>>;
}) {
  const stats = progress?.stats || {};
  const bestAffinity = Number(stats.best_affinity_oracle);
  return (
    <section className="panel subtle lead-opt-panel">
      <div className="lead-opt-panel-title">Progress</div>
      <div className="lead-opt-halo-progress">
        <span>
          {progress?.round
            ? `Round ${progress.round}${progress.total_rounds ? `/${progress.total_rounds}` : ''}`
            : progress?.message || 'Running…'}
        </span>
        {Number.isFinite(bestAffinity) ? (
          <span className="lead-opt-halo-best">best pIC50 {bestAffinity.toFixed(2)}</span>
        ) : null}
      </div>
      {Array.isArray(progress?.top_candidates) && progress.top_candidates.length > 0 ? (
        <div className="lead-opt-halo-topchips">
          {progress.top_candidates.slice(0, 6).map((candidate, index) => (
            <span key={`halo-top-${index}`} className="lead-opt-halo-topchip" title={String(candidate.smiles || '')}>
              #{index + 1} {String(candidate.smiles || '').slice(0, 24)}
              {candidate.affinity_pic50 !== undefined && candidate.affinity_pic50 !== null
                ? ` · ${Number(candidate.affinity_pic50).toFixed(1)}`
                : ''}
            </span>
          ))}
        </div>
      ) : null}
      {roundsLog.length > 0 ? (
        <div className="muted small">
          {roundsLog.length} round{roundsLog.length === 1 ? '' : 's'} logged
        </div>
      ) : null}
    </section>
  );
}

interface LeadOptHaloCandidatesPanelProps {
  candidates: LeadOptHaloCandidate[];
  mode: string;
  backend: string;
}

export function LeadOptHaloCandidatesPanel({ candidates, mode, backend }: LeadOptHaloCandidatesPanelProps) {
  if (candidates.length === 0) {
    return (
      <section className="panel subtle lead-opt-panel">
        <div className="lead-opt-panel-title">Candidates</div>
        <div className="ligand-preview-empty">No candidates yet — run the optimization.</div>
      </section>
    );
  }
  return (
    <section className="panel subtle lead-opt-panel lead-opt-halo-candidates">
      <div className="lead-opt-panel-title">
        Candidates
        <span className="muted small">
          {' '}· {candidates.length} molecules · {mode || 'halo'} · {backend || '-'}
        </span>
      </div>
      <div className="lead-opt-table-wrap">
        <table className="lead-opt-candidate-table">
          <thead>
            <tr>
              <th>#</th>
              <th>2D</th>
              <th>SMILES</th>
              <th>Round</th>
              <th>Source</th>
              <th>pIC50</th>
              <th>ipSAE</th>
              <th>pLDDT</th>
              <th>Reward</th>
            </tr>
          </thead>
          <tbody>
            {candidates.map((candidate, index) => (
              <tr key={`halo-cand-${index}`}>
                <td>{index + 1}</td>
                <td className="lead-opt-cand-2d">
                  {candidate.smiles ? <MemoLigand2DPreview smiles={candidate.smiles} /> : null}
                </td>
                <td className="lead-opt-cand-smiles" title={candidate.smiles}>{candidate.smiles}</td>
                <td>{candidate.round ?? '-'}</td>
                <td>{candidate.source || '-'}</td>
                <td>{candidate.affinity_pic50 !== undefined ? candidate.affinity_pic50.toFixed(2) : '-'}</td>
                <td>{candidate.ipsae !== undefined ? candidate.ipsae.toFixed(2) : '-'}</td>
                <td>{candidate.ligand_plddt_mean !== undefined ? candidate.ligand_plddt_mean.toFixed(1) : '-'}</td>
                <td>{candidate.final_reward !== undefined ? candidate.final_reward.toFixed(3) : '-'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
