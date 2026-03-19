import type { InputComponent, ProjectTask } from '../../types/models';
import type { WorkflowKey } from '../../utils/workflows';

export type MetricTone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';
export type SortKey = 'plddt' | 'ipsae' | 'iptm' | 'pae' | 'submitted' | 'backend' | 'seed' | 'duration';
export type SortDirection = 'asc' | 'desc';
export type TaskTableMode = 'default' | 'lead_opt' | 'peptide';
export type TaskMetricColumnKey = 'plddt' | 'ipsae' | 'iptm' | 'pae';
export type SubmittedWithinDaysOption = 'all' | '1' | '7' | '30' | '90';
export type SeedFilterOption = 'all' | 'with_seed' | 'without_seed';
export type StructureSearchMode = 'exact' | 'substructure';
export type TaskWorkspaceView = 'tasks' | 'api';
export type TaskWorkflowFilter = 'all' | WorkflowKey;

export interface TaskConfidenceMetrics {
  plddt: number | null;
  ipsae: number | null;
  iptm: number | null;
  interfaceMetricValue: number | null;
  interfaceMetricLabel: 'IPSAE' | 'ipTM';
  interfaceMetricSource: 'ipsae' | 'iptm' | 'none';
  pae: number | null;
}

export interface TaskMetricContext {
  chainIds: string[];
  targetChainId: string | null;
  ligandChainId: string | null;
  strictPairIptm?: boolean;
}

export interface WorkspacePairPreference {
  targetChainId: string | null;
  ligandChainId: string | null;
}

export interface TaskSelectionContext extends TaskMetricContext {
  ligandSmiles: string;
  ligandIsSmiles: boolean;
  ligandComponentCount: number;
  ligandSequence: string;
  ligandSequenceType: InputComponent['type'] | null;
}

export interface TaskListRow {
  task: ProjectTask;
  metrics: TaskConfidenceMetrics;
  submittedTs: number;
  backendValue: string;
  durationValue: number | null;
  ligandSmiles: string;
  ligandRenderSmiles: string;
  ligandIsSmiles: boolean;
  ligandAtomPlddts: number[] | null;
  ligandRenderAtomPlddts: number[] | null;
  ligandSequence: string;
  ligandSequenceType: InputComponent['type'] | null;
  ligandResiduePlddts: number[] | null;
  workflowKey: WorkflowKey;
  workflowLabel: string;
  leadOptMmpSummary: string;
  leadOptMmpStage: string;
  leadOptDatabaseId: string;
  leadOptDatabaseLabel: string;
  leadOptDatabaseSchema: string;
  leadOptTransformCount: number | null;
  leadOptCandidateCount: number | null;
  leadOptBucketCount: number | null;
  leadOptPredictionTotal: number | null;
  leadOptPredictionQueued: number | null;
  leadOptPredictionRunning: number | null;
  leadOptPredictionSuccess: number | null;
  leadOptPredictionFailure: number | null;
  leadOptSelectedFragmentIds: string[];
  leadOptSelectedAtomIndices: number[];
  leadOptSelectedFragmentQuery: string;
  peptideDesignMode: 'linear' | 'cyclic' | 'bicyclic' | null;
  peptideBinderLength: number | null;
  peptideIterations: number | null;
  peptidePopulationSize: number | null;
  peptideEliteSize: number | null;
  peptideMutationRate: number | null;
  peptideCurrentGeneration: number | null;
  peptideTotalGenerations: number | null;
  peptideBestScore: number | null;
  peptideCandidateCount: number | null;
  peptideCompletedTasks: number | null;
  peptidePendingTasks: number | null;
  peptideTotalTasks: number | null;
  peptideStage: string;
  peptideStatusMessage: string;
}
