import { useEffect, type Dispatch, type SetStateAction } from 'react';
import { readFirstFiniteMetric, readObjectPath } from './projectMetrics';
import type { ProjectWorkspaceDraft } from './workspaceTypes';

interface UseProjectConfidenceSignalsInput {
  snapshotConfidence: Record<string, unknown> | null;
  projectBackendValue: string | null | undefined;
  draft: ProjectWorkspaceDraft | null;
  setDraft: Dispatch<SetStateAction<ProjectWorkspaceDraft | null>>;
}

interface UseProjectConfidenceSignalsResult {
  confidenceBackend: string;
  projectBackend: string;
  hasProtenixConfidenceSignals: boolean;
  hasAf3ConfidenceSignals: boolean;
}

export function useProjectConfidenceSignals({
  snapshotConfidence,
  projectBackendValue,
  draft,
  setDraft
}: UseProjectConfidenceSignalsInput): UseProjectConfidenceSignalsResult {
  const confidenceBackend =
    snapshotConfidence && typeof snapshotConfidence.backend === 'string'
      ? String(snapshotConfidence.backend).toLowerCase()
      : '';
  const projectBackend = String(projectBackendValue || '').trim().toLowerCase();
  const hasProtenixConfidenceSignals = Boolean(
    confidenceBackend === 'protenix' ||
      readFirstFiniteMetric(snapshotConfidence || {}, [
        'complex_plddt',
        'complex_plddt_protein',
        'complex_iplddt',
        'plddt',
        'ligand_mean_plddt'
      ]) !== null ||
      readObjectPath(snapshotConfidence || {}, 'chain_mean_plddt') !== undefined ||
      readObjectPath(snapshotConfidence || {}, 'residue_plddt_by_chain') !== undefined
  );
  const hasAf3ConfidenceSignals = Boolean(
    readFirstFiniteMetric(snapshotConfidence || {}, ['ranking_score', 'fraction_disordered']) !== null ||
      readObjectPath(snapshotConfidence || {}, 'chain_pair_iptm') !== undefined
  );

  useEffect(() => {
    if (!draft) return;
    if (projectBackend !== 'protenix') return;
    if (!hasProtenixConfidenceSignals) return;
    if (draft.color_mode === 'alphafold') return;
    setDraft((prev) => (prev ? { ...prev, color_mode: 'alphafold' } : prev));
  }, [draft, projectBackend, hasProtenixConfidenceSignals, setDraft]);

  return {
    confidenceBackend,
    projectBackend,
    hasProtenixConfidenceSignals,
    hasAf3ConfidenceSignals
  };
}
