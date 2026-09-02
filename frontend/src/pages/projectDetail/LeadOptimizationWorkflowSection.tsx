import { LeadOptimizationWorkspace, type LeadOptHaloSnapshot } from '../../components/project/LeadOptimizationWorkspace';
import type { LeadOptHaloCandidate } from '../../components/project/leadopt/hooks/useLeadOptHaloRun';
import type { LeadOptPersistedUploads } from '../../components/project/leadopt/hooks/useLeadOptReferenceFragment';
import type { AffinityDockPocket, PredictionOptions } from '../../types/models';

export interface LeadOptimizationWorkflowSectionProps {
  visible: boolean;
  workspaceTab: 'results' | 'basics' | 'components' | 'constraints';
  canEdit: boolean;
  submitting: boolean;
  proteinSequence: string;
  ligandSmiles: string;
  targetChain: string;
  ligandChain: string;
  onLigandSmilesChange: (value: string) => void;
  referenceScopeKey?: string;
  persistedReferenceUploads?: LeadOptPersistedUploads | null;
  onReferenceUploadsChange?: (uploads: LeadOptPersistedUploads) => void;
  options: PredictionOptions;
  onOptionChange: (
    key: 'leadOptMode' | 'leadOptBackend' | 'leadOptRounds' | 'leadOptBudgetPerRound' | 'leadOptScaffoldHopRatio'
      | 'leadOptPocketCenter' | 'leadOptReferenceSmiles' | 'leadOptKeepFragmentSmiles' | 'leadOptEditAtomIndices',
    value: string | number | null
  ) => void;
  onDockPocketChange: (pocket: AffinityDockPocket | null) => void;
  haloSnapshot: LeadOptHaloSnapshot | null;
  onHaloTaskQueued: (payload: { taskId: string; requestPayload: Record<string, unknown> }) => Promise<void> | void;
  onHaloTaskCompleted: (payload: {
    taskId: string;
    candidates: LeadOptHaloCandidate[];
    roundsLog: Array<Record<string, unknown>>;
    roundsCompleted: number | null;
    totalRounds: number | null;
    mode: string;
    backend: string;
  }) => Promise<void> | void;
  onHaloTaskFailed: (payload: { taskId: string; error: string }) => Promise<void> | void;
  onNavigateToResults?: () => void;
}

export function LeadOptimizationWorkflowSection({
  visible,
  workspaceTab,
  canEdit,
  submitting,
  proteinSequence,
  ligandSmiles,
  targetChain,
  ligandChain,
  onLigandSmilesChange,
  referenceScopeKey,
  persistedReferenceUploads,
  onReferenceUploadsChange,
  options,
  onOptionChange,
  onDockPocketChange,
  haloSnapshot,
  onHaloTaskQueued,
  onHaloTaskCompleted,
  onHaloTaskFailed,
  onNavigateToResults
}: LeadOptimizationWorkflowSectionProps) {
  if (!visible) return null;
  const viewMode = workspaceTab === 'results' ? 'design' : 'reference';
  return (
    <LeadOptimizationWorkspace
      viewMode={viewMode}
      canEdit={canEdit}
      submitting={submitting}
      proteinSequence={proteinSequence}
      ligandSmiles={ligandSmiles}
      targetChain={targetChain}
      ligandChain={ligandChain}
      onLigandSmilesChange={onLigandSmilesChange}
      referenceScopeKey={referenceScopeKey}
      persistedReferenceUploads={persistedReferenceUploads}
      onReferenceUploadsChange={onReferenceUploadsChange}
      options={options}
      onOptionChange={onOptionChange}
      onDockPocketChange={onDockPocketChange}
      haloSnapshot={haloSnapshot}
      onHaloTaskQueued={onHaloTaskQueued}
      onHaloTaskCompleted={onHaloTaskCompleted}
      onHaloTaskFailed={onHaloTaskFailed}
      onNavigateToResults={onNavigateToResults}
    />
  );
}
