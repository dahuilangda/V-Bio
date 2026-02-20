import { LeadOptimizationWorkspace } from '../../components/project/LeadOptimizationWorkspace';
import type { LeadOptPersistedUploads } from '../../components/project/leadopt/hooks/useLeadOptReferenceFragment';
import type {
  LeadOptMmpPersistedSnapshot,
  LeadOptPredictionRecord
} from '../../components/project/leadopt/hooks/useLeadOptMmpQueryMachine';
import type { WorkspaceTab } from './workspaceTypes';

export interface LeadOptimizationWorkflowSectionProps {
  visible: boolean;
  workspaceTab: WorkspaceTab;
  canEdit: boolean;
  submitting: boolean;
  backend: string;
  onNavigateToResults?: () => void;
  onRegisterHeaderRunAction?: (action: (() => void) | null) => void;
  proteinSequence: string;
  ligandSmiles: string;
  targetChain: string;
  ligandChain: string;
  onLigandSmilesChange: (value: string) => void;
  referenceScopeKey?: string;
  persistedReferenceUploads?: LeadOptPersistedUploads;
  onReferenceUploadsChange?: (uploads: LeadOptPersistedUploads) => void;
  onMmpTaskQueued?: (payload: {
    taskId: string;
    requestPayload: Record<string, unknown>;
    querySmiles: string;
    referenceUploads: LeadOptPersistedUploads;
  }) => void | Promise<void>;
  onMmpTaskCompleted?: (payload: {
    taskId: string;
    queryId: string;
    transformCount: number;
    candidateCount: number;
    elapsedSeconds: number;
    resultSnapshot?: Record<string, unknown>;
  }) => void | Promise<void>;
  onMmpTaskFailed?: (payload: { taskId: string; error: string }) => void | Promise<void>;
  initialMmpSnapshot?: LeadOptMmpPersistedSnapshot | null;
  onPredictionQueued?: (payload: { taskId: string; backend: string; candidateSmiles: string }) => void | Promise<void>;
  onPredictionStateChange?: (payload: {
    records: Record<string, LeadOptPredictionRecord>;
    referenceRecords: Record<string, LeadOptPredictionRecord>;
    summary: {
      total: number;
      queued: number;
      running: number;
      success: number;
      failure: number;
      latestTaskId: string;
    };
  }) => void | Promise<void>;
}

export function LeadOptimizationWorkflowSection({
  visible,
  workspaceTab,
  canEdit,
  submitting,
  backend,
  onNavigateToResults,
  onRegisterHeaderRunAction,
  proteinSequence,
  ligandSmiles,
  targetChain,
  ligandChain,
  onLigandSmilesChange,
  referenceScopeKey,
  persistedReferenceUploads,
  onReferenceUploadsChange,
  onMmpTaskQueued,
  onMmpTaskCompleted,
  onMmpTaskFailed,
  initialMmpSnapshot,
  onPredictionQueued,
  onPredictionStateChange
}: LeadOptimizationWorkflowSectionProps) {
  if (!visible) return null;
  const viewMode = workspaceTab === 'results' ? 'design' : 'reference';

  return (
    <LeadOptimizationWorkspace
      viewMode={viewMode}
      canEdit={canEdit}
      submitting={submitting}
      backend={backend}
      onNavigateToResults={onNavigateToResults}
      onRegisterHeaderRunAction={onRegisterHeaderRunAction}
      proteinSequence={proteinSequence}
      ligandSmiles={ligandSmiles}
      targetChain={targetChain}
      ligandChain={ligandChain}
      onLigandSmilesChange={onLigandSmilesChange}
      referenceScopeKey={referenceScopeKey}
      persistedReferenceUploads={persistedReferenceUploads}
      onReferenceUploadsChange={onReferenceUploadsChange}
      onMmpTaskQueued={onMmpTaskQueued}
      onMmpTaskCompleted={onMmpTaskCompleted}
      onMmpTaskFailed={onMmpTaskFailed}
      initialMmpSnapshot={initialMmpSnapshot}
      onPredictionQueued={onPredictionQueued}
      onPredictionStateChange={onPredictionStateChange}
    />
  );
}
