/**
 * The two candidate presentation views (table row + card) of the peptide
 * design results workspace. Extracted as pure memo components — all data
 * arrives via props, no workspace closures.
 */
import { memo, useCallback, useMemo, type KeyboardEvent, type MouseEvent } from 'react';
import {
  buildPeptideLigandViewTokens,
  confidenceTone,
  formatInterfaceMetric,
  formatPlddt,
  formatScore,
  scoreConfidencePercent,
  toneForPlddtValue,
  type PeptideDesignCandidate
} from './parseHelpers';
import './PeptideCandidateViews.css';

interface PeptideCandidateTableRowProps {
  candidate: PeptideDesignCandidate;
  selected: boolean;
  cardMode: boolean;
  scoreMin: number | null;
  scoreMax: number | null;
  onOpen: (candidateId: string) => void;
  onSelect: (candidateId: string) => void;
}

export const PeptideCandidateTableRow = memo(function PeptideCandidateTableRow({
  candidate,
  selected,
  cardMode,
  scoreMin,
  scoreMax,
  onOpen,
  onSelect
}: PeptideCandidateTableRowProps) {
  const scoreTone = confidenceTone(scoreConfidencePercent(candidate.score, scoreMin, scoreMax));
  const plddtTone = confidenceTone(candidate.plddt);
  const interfaceTone = confidenceTone(
    candidate.interfaceMetric === null ? null : candidate.interfaceMetric * 100
  );
  const sequenceRows = useMemo(() => {
    const sequenceTokens = buildPeptideLigandViewTokens(candidate.sequence, candidate.modifications);
    return Array.from(
      { length: Math.ceil(sequenceTokens.length / 10) },
      (_, rowIdx) => sequenceTokens.slice(rowIdx * 10, rowIdx * 10 + 10)
    );
  }, [candidate.modifications, candidate.sequence]);
  const handleRowClick = useCallback(() => {
    if (cardMode) onSelect(candidate.id);
  }, [candidate.id, cardMode, onSelect]);
  const handleOpen = useCallback((event: MouseEvent<HTMLButtonElement>) => {
    event.stopPropagation();
    onOpen(candidate.id);
  }, [candidate.id, onOpen]);

  return (
    <tr
      className={selected ? 'selected' : ''}
      onClick={handleRowClick}
    >
      <td className="col-rank">{candidate.rank}</td>
      <td className="col-actions peptide-col-open">
        <button
          type="button"
          className="peptide-ligand-preview-btn"
          title="Open in 3D card view"
          aria-label="Open in 3D card view"
          onClick={handleOpen}
        >
          <span className="peptide-ligand-preview-track">
            {sequenceRows.length > 0 ? (
              sequenceRows.map((rowTokens, rowIdx) => (
                <span className="peptide-ligand-preview-row" key={`${candidate.id}-ligand-view-row-${rowIdx}`}>
                  {rowTokens.map((token, idx) => {
                    const residueIdx = rowIdx * 10 + idx;
                    const residuePlddt = candidate.residuePlddts[residueIdx] ?? null;
                    const residueTone = toneForPlddtValue(residuePlddt);
                    const isModified = Boolean(token.modifiedLabel);
                    const title = isModified
                      ? `#${residueIdx + 1} ${token.residue} -> ${token.modifiedLabel} | pLDDT ${residuePlddt === null ? '-' : residuePlddt.toFixed(1)}`
                      : `#${residueIdx + 1} ${token.residue} | pLDDT ${residuePlddt === null ? '-' : residuePlddt.toFixed(1)}`;
                    return (
                      <span className="peptide-ligand-preview-node-wrap" key={`${candidate.id}-ligand-view-${residueIdx}`}>
                        {idx > 0 ? (
                          <span className={`peptide-ligand-preview-link tone-${residueTone}`} aria-hidden="true" />
                        ) : null}
                        <span
                          className={`peptide-ligand-preview-node tone-${residueTone}${isModified ? ' is-modified' : ''}`}
                          title={title}
                        >
                          {token.displayResidue}
                        </span>
                      </span>
                    );
                  })}
                </span>
              ))
            ) : (
              <span className="peptide-ligand-preview-empty">-</span>
            )}
          </span>
        </button>
      </td>
      <td className="col-n">{candidate.generation !== null ? candidate.generation : '-'}</td>
      <td className="col-delta">
        <span className={`peptide-table-value conf-tone-${scoreTone}`}>{formatScore(candidate.score)}</span>
      </td>
      <td className="col-insights peptide-col-metric">
        <span className={`peptide-table-value conf-tone-${plddtTone}`}>{formatPlddt(candidate.plddt)}</span>
      </td>
      <td className="col-insights peptide-col-metric">
        <span className={`peptide-table-value conf-tone-${interfaceTone}`}>
          {formatInterfaceMetric(candidate.interfaceMetric)}
        </span>
      </td>
    </tr>
  );
});

interface PeptideCandidateCardProps {
  candidate: PeptideDesignCandidate;
  selected: boolean;
  scoreMin: number | null;
  scoreMax: number | null;
  /** Whether any candidate this run carries an ipSAE value — the pill hides entirely on runs
   *  whose scoring backend produced none, instead of a dead "IPSAE -" chip. */
  showIpsae: boolean;
  onSelect: (candidateId: string) => void;
}

export const PeptideCandidateCard = memo(function PeptideCandidateCard({
  candidate,
  selected,
  scoreMin,
  scoreMax,
  showIpsae,
  onSelect
}: PeptideCandidateCardProps) {
  const scoreTone = confidenceTone(scoreConfidencePercent(candidate.score, scoreMin, scoreMax));
  const plddtTone = confidenceTone(candidate.plddt);
  const iptmTone = confidenceTone(candidate.iptm === null ? null : candidate.iptm * 100);
  const ipsaeTone = confidenceTone(candidate.ipsae === null ? null : candidate.ipsae * 100);
  const sequenceRows = useMemo(() => {
    const sequenceTokens = buildPeptideLigandViewTokens(candidate.sequence, candidate.modifications);
    return Array.from(
      { length: Math.ceil(sequenceTokens.length / 5) },
      (_, rowIdx) => sequenceTokens.slice(rowIdx * 5, rowIdx * 5 + 5)
    );
  }, [candidate.modifications, candidate.sequence]);
  const handleSelect = useCallback(() => {
    onSelect(candidate.id);
  }, [candidate.id, onSelect]);
  const handleKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    onSelect(candidate.id);
  }, [candidate.id, onSelect]);

  return (
    <article
      className={`lead-opt-result-card peptide-result-card${selected ? ' selected' : ''}`}
      onClick={handleSelect}
      onKeyDown={handleKeyDown}
      role="button"
      tabIndex={0}
      aria-label={`Open peptide card ${candidate.rank}`}
    >
      <div className="lead-opt-result-card-head">
        <strong>#{candidate.rank}</strong>
        <span className="muted small">Gen {candidate.generation !== null ? candidate.generation : '-'}</span>
      </div>
      <div className="lead-opt-result-card-media peptide-result-card-media">
        <span className="peptide-ligand-preview-track peptide-ligand-preview-track--card">
          {sequenceRows.length > 0 ? (
            sequenceRows.map((rowTokens, rowIdx) => (
              <span className="peptide-ligand-preview-row peptide-ligand-preview-row--card" key={`${candidate.id}-card-row-${rowIdx}`}>
                {rowTokens.map((token, idx) => {
                  const residueIdx = rowIdx * 5 + idx;
                  const residuePlddt = candidate.residuePlddts[residueIdx] ?? null;
                  const residueTone = toneForPlddtValue(residuePlddt);
                  const isModified = Boolean(token.modifiedLabel);
                  const title = isModified
                    ? `#${residueIdx + 1} ${token.residue} -> ${token.modifiedLabel} | pLDDT ${residuePlddt === null ? '-' : residuePlddt.toFixed(1)}`
                    : `#${residueIdx + 1} ${token.residue} | pLDDT ${residuePlddt === null ? '-' : residuePlddt.toFixed(1)}`;
                  return (
                    <span className="peptide-ligand-preview-node-wrap" key={`${candidate.id}-card-${residueIdx}`}>
                      {idx > 0 ? (
                        <span className={`peptide-ligand-preview-link tone-${residueTone}`} aria-hidden="true" />
                      ) : null}
                      <span
                        className={`peptide-ligand-preview-node peptide-ligand-preview-node--card tone-${residueTone}${isModified ? ' is-modified' : ''}`}
                        title={title}
                      >
                        {token.displayResidue}
                      </span>
                    </span>
                  );
                })}
              </span>
            ))
          ) : (
            <span className="peptide-ligand-preview-empty">-</span>
          )}
        </span>
      </div>
      <div className="lead-opt-card-metric-strip peptide-card-metric-strip">
        <span className={`lead-opt-card-pill conf-tone-${scoreTone}`}>
          <span className="lead-opt-card-pill-key">Score</span>
          <strong>{formatScore(candidate.score)}</strong>
        </span>
        <span className={`lead-opt-card-pill conf-tone-${plddtTone}`}>
          <span className="lead-opt-card-pill-key">pLDDT</span>
          <strong>{formatPlddt(candidate.plddt)}</strong>
        </span>
        <span className={`lead-opt-card-pill conf-tone-${iptmTone}`}>
          <span className="lead-opt-card-pill-key">ipTM</span>
          <strong>{formatInterfaceMetric(candidate.iptm)}</strong>
        </span>
        {showIpsae ? (
          <span className={`lead-opt-card-pill conf-tone-${ipsaeTone}`}>
            <span className="lead-opt-card-pill-key">IPSAE</span>
            <strong>{formatInterfaceMetric(candidate.ipsae)}</strong>
          </span>
        ) : null}
      </div>
    </article>
  );
});
