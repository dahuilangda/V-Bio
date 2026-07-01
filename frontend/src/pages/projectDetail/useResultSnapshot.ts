import { useMemo } from 'react';
import type { InputComponent, Project, ProjectInputConfig, ProjectTask, ProteinModification } from '../../types/models';
import { componentTypeLabel, normalizeComponentSequence } from '../../utils/projectInputs';
import { buildChainInfos } from '../../utils/chainAssignments';
import { isSequenceLigandType } from './OverviewLigandSequencePreview';
import {
  readChainMeanPlddtForChain,
  readFirstFiniteMetric,
  readFiniteMetricSeries,
  readPairIptmForChains,
  std,
  toneForIc50,
  toneForIptm,
  toneForPlddt,
  toneForProbability,
  mean,
  type MetricTone,
} from './projectMetrics';
import {
  alignConfidenceSeriesToLength,
  readLigandAtomPlddtsFromConfidence,
  readResiduePlddtsForChain,
} from './projectConfidence';
import { buildResultChainConsistencyWarning, resolveSelectedResultLigandChainId, resolveSelectedResultTargetChainId } from './projectResultChains';
import { AFFINITY_UPLOAD_SCOPE_PREFIX, readTaskComponents } from './projectTaskSnapshot';
import { nonEmptyComponents } from './projectDraftUtils';
import { readLeadOptTaskSummary } from '../projectTasks/taskDataUtils';

type ChainInfo = ReturnType<typeof buildChainInfos>[number];

export interface UseResultSnapshotParams {
  project: Project | null;
  projectTasks: ProjectTask[];
  draftProperties: ProjectInputConfig['properties'] | null | undefined;
  statusContextTaskRow: ProjectTask | null;
  requestedStatusTaskRow: ProjectTask | null;
  viewerTaskId?: string | null;
  normalizedDraftComponents: InputComponent[];
  workflowKey: string;
  shouldComputeResultMetrics: boolean;
  isDraftTaskSnapshot: (task: ProjectTask | null | undefined) => boolean;
}

export interface UseResultSnapshotResult {
  runtimeResultTask: ProjectTask | null;
  activeResultTask: ProjectTask | null;
  affinityUploadScopeTaskRowId: string;
  resultOverviewComponents: InputComponent[];
  resultOverviewActiveComponents: InputComponent[];
  resultChainInfos: ChainInfo[];
  resultChainIds: string[];
  resultComponentOptions: Array<{
    id: string;
    type: InputComponent['type'];
    sequence: string;
    chainId: string | null;
    isSmiles: boolean;
    label: string;
  }>;
  resultChainInfoById: Map<string, ChainInfo>;
  resultComponentById: Map<string, InputComponent>;
  resultChainShortLabelById: Map<string, string>;
  resultPairPreference: Record<string, unknown> | null;
  selectedResultTargetChainId: string | null;
  selectedResultLigandChainId: string | null;
  selectedResultLigandComponent: InputComponent | null;
  selectedResultLigandSequence: string;
  overviewPrimaryLigand: {
    smiles: string;
    isSmiles: boolean;
    selectedComponentType: InputComponent['type'] | null;
  };
  snapshotConfidence: Record<string, unknown> | null;
  resultChainConsistencyWarning: string | null;
  snapshotAffinity: Record<string, unknown> | null;
  snapshotLigandAtomPlddts: number[];
  snapshotLigandResiduePlddts: number[] | null;
  snapshotLigandMeanPlddt: number | null;
  snapshotSelectedLigandChainPlddt: number | null;
  snapshotPlddt: number | null;
  snapshotSelectedPairIptm: number | null;
  snapshotIptm: number | null;
  snapshotBindingValues: number[] | null;
  snapshotBindingProbability: number | null;
  snapshotBindingStd: number | null;
  snapshotLogIc50Values: number[] | null;
  snapshotIc50Um: number | null;
  snapshotIc50Error: { plus: number; minus: number } | null;
  snapshotPlddtTone: MetricTone;
  snapshotIptmTone: MetricTone;
  snapshotIc50Tone: MetricTone;
  snapshotBindingTone: MetricTone;
}

function hasLeadOptSnapshotPayload(task: ProjectTask | null | undefined): boolean {
  if (!task) return false;
  const properties =
    task.properties && typeof task.properties === 'object' && !Array.isArray(task.properties)
      ? (task.properties as unknown as Record<string, unknown>)
      : null;
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as unknown as Record<string, unknown>)
      : null;
  const leadOptList =
    properties?.lead_opt_list && typeof properties.lead_opt_list === 'object' && !Array.isArray(properties.lead_opt_list)
      ? (properties.lead_opt_list as Record<string, unknown>)
      : null;
  const leadOptState =
    properties?.lead_opt_state && typeof properties.lead_opt_state === 'object' && !Array.isArray(properties.lead_opt_state)
      ? (properties.lead_opt_state as Record<string, unknown>)
      : null;
  const leadOptConfidence =
    confidence?.lead_opt_mmp && typeof confidence.lead_opt_mmp === 'object' && !Array.isArray(confidence.lead_opt_mmp)
      ? (confidence.lead_opt_mmp as Record<string, unknown>)
      : null;
  return Boolean(
    (leadOptList && Object.keys(leadOptList).length > 0) ||
      (leadOptState && Object.keys(leadOptState).length > 0) ||
      (leadOptConfidence && Object.keys(leadOptConfidence).length > 0)
  );
}

function isRuntimeActiveTask(task: ProjectTask | null | undefined): boolean {
  const state = String(task?.task_state || '').trim().toUpperCase();
  return state === 'QUEUED' || state === 'RUNNING';
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readObjectPath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const key of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

function readRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
}

function readFirstRecordArray(payloads: Record<string, unknown>[], paths: string[]): Array<Record<string, unknown>> {
  for (const payload of payloads) {
    for (const path of paths) {
      const rows = readRecordArray(readObjectPath(payload, path));
      if (rows.length > 0) return rows;
    }
  }
  return [];
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

function readFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value.trim());
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function readFirstFinite(payloads: Record<string, unknown>[], paths: string[]): number | null {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readFiniteNumber(readObjectPath(payload, path));
      if (value !== null) return value;
    }
  }
  return null;
}

function readFirstText(payloads: Record<string, unknown>[], paths: string[]): string {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readText(readObjectPath(payload, path));
      if (value) return value;
    }
  }
  return '';
}

function readPeptideCandidateSequence(row: Record<string, unknown>): string {
  return readFirstText(row ? [row] : [], ['peptide_sequence', 'binder_sequence', 'candidate_sequence', 'designed_sequence', 'sequence'])
    .replace(/\s+/g, '')
    .trim()
    .toUpperCase();
}

function readPeptideCandidateModifications(row: Record<string, unknown>, sequenceLength: number): ProteinModification[] {
  const raw =
    readObjectPath(row, 'modifications') ??
    readObjectPath(row, 'protein_modifications') ??
    readObjectPath(row, 'residue_modifications') ??
    readObjectPath(row, 'residueMods') ??
    readObjectPath(row, 'residue_mods') ??
    readObjectPath(row, 'mods') ??
    readObjectPath(row, 'result.modifications') ??
    readObjectPath(row, 'prediction.modifications') ??
    readObjectPath(row, 'metadata.modifications') ??
    readObjectPath(row, 'structure_payload.modifications');
  if (!Array.isArray(raw)) return [];
  const rows: ProteinModification[] = [];
  const seen = new Set<number>();
  raw.forEach((item, index) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return;
    const record = item as Record<string, unknown>;
    const position = Math.floor(Number(record.position ?? record.residue_index ?? record.residue ?? record.pos));
    const ccd = readText(record.ccd ?? record.code ?? record.residue_name).toUpperCase();
    if (!Number.isFinite(position) || position < 1 || position > sequenceLength || !ccd || seen.has(position)) return;
    seen.add(position);
    rows.push({
      id: readText(record.id) || `peptide-mod-${position}-${ccd}-${index}`,
      position,
      baseResidue: readText(record.baseResidue ?? record.base_residue).toUpperCase().slice(0, 1),
      ccd,
      inputMethod: readText(record.inputMethod ?? record.input_method).toLowerCase() === 'jsme' ? 'jsme' : 'ccd',
      smiles: typeof record.smiles === 'string' ? record.smiles : undefined,
      label: typeof record.label === 'string' ? record.label : undefined
    });
  });
  return rows.sort((a, b) => a.position - b.position);
}

function comparePeptideCandidateRows(a: Record<string, unknown>, b: Record<string, unknown>, aIndex: number, bIndex: number): number {
  const aRank = readFirstFinite([a], ['rank', 'ranking', 'order']);
  const bRank = readFirstFinite([b], ['rank', 'ranking', 'order']);
  const aRankValue = aRank === null ? null : Math.max(1, Math.floor(aRank));
  const bRankValue = bRank === null ? null : Math.max(1, Math.floor(bRank));
  if (aRankValue !== null && bRankValue !== null && aRankValue !== bRankValue) return aRankValue - bRankValue;
  if (aRankValue !== null && bRankValue === null) return -1;
  if (aRankValue === null && bRankValue !== null) return 1;

  const aScore = readFirstFinite([a], ['composite_score', 'score', 'fitness', 'objective']);
  const bScore = readFirstFinite([b], ['composite_score', 'score', 'fitness', 'objective']);
  if (aScore !== null && bScore !== null && aScore !== bScore) return bScore - aScore;
  if (aScore !== null && bScore === null) return -1;
  if (aScore === null && bScore !== null) return 1;

  const aIptm = readFirstFinite([a], ['pair_iptm_target_binder', 'pair_iptm', 'iptm']);
  const bIptm = readFirstFinite([b], ['pair_iptm_target_binder', 'pair_iptm', 'iptm']);
  if (aIptm !== null && bIptm !== null && aIptm !== bIptm) return bIptm - aIptm;
  if (aIptm !== null && bIptm === null) return -1;
  if (aIptm === null && bIptm !== null) return 1;
  return aIndex - bIndex;
}

function readPeptideBestCandidateFromTask(task: ProjectTask | null | undefined): {
  sequence: string;
  modifications: ProteinModification[];
  binderChainId: string | null;
} | null {
  const confidence = asRecord(task?.confidence);
  if (Object.keys(confidence).length === 0) return null;
  const peptideDesign = asRecord(confidence.peptide_design);
  const peptideProgress = asRecord(peptideDesign.progress);
  const topProgress = asRecord(confidence.progress);
  const candidateRows = readFirstRecordArray(
    [confidence, peptideDesign, peptideProgress, topProgress],
    [
      'progress.current_best_sequences',
      'progress.best_sequences',
      'peptide_design.progress.current_best_sequences',
      'peptide_design.progress.best_sequences',
      'peptide_design.current_best_sequences',
      'current_best_sequences',
      'peptide_design.best_sequences',
      'best_sequences',
      'peptide_design.candidates',
      'candidates'
    ]
  );
  if (candidateRows.length === 0) return null;
  const order = new Map(candidateRows.map((row, index) => [row, index] as const));
  const sorted = [...candidateRows].sort((a, b) => comparePeptideCandidateRows(a, b, order.get(a) ?? 0, order.get(b) ?? 0));
  const best = sorted.find((row) => Boolean(readPeptideCandidateSequence(row))) || sorted[0];
  const sequence = best ? readPeptideCandidateSequence(best) : '';
  if (!sequence) return null;
  const binderChainId = readFirstText(
    [best, confidence, peptideDesign, peptideProgress, topProgress],
    ['binder_chain_id', 'model_ligand_chain_id', 'requested_ligand_chain_id', 'ligand_chain_id']
  );
  return {
    sequence,
    modifications: readPeptideCandidateModifications(best, sequence.length),
    binderChainId: binderChainId || null
  };
}

function readLeadOptTaskRowTimestamp(task: ProjectTask | null | undefined): number {
  return new Date(
    String(task?.updated_at || task?.completed_at || task?.submitted_at || task?.created_at || '')
  ).getTime() || 0;
}

function readLeadOptSnapshotPriority(task: ProjectTask | null | undefined): number {
  if (!task || !hasLeadOptSnapshotPayload(task)) return -1;
  const properties =
    task.properties && typeof task.properties === 'object' && !Array.isArray(task.properties)
      ? (task.properties as unknown as Record<string, unknown>)
      : {};
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as unknown as Record<string, unknown>)
      : {};
  const leadOptList = properties.lead_opt_list && typeof properties.lead_opt_list === 'object' ? (properties.lead_opt_list as Record<string, unknown>) : {};
  const leadOptState = properties.lead_opt_state && typeof properties.lead_opt_state === 'object' ? (properties.lead_opt_state as Record<string, unknown>) : {};
  const leadOptConfidence = confidence.lead_opt_mmp && typeof confidence.lead_opt_mmp === 'object' ? (confidence.lead_opt_mmp as Record<string, unknown>) : {};
  const queryResult =
    leadOptList.query_result && typeof leadOptList.query_result === 'object' ? (leadOptList.query_result as Record<string, unknown>) :
    leadOptConfidence.query_result && typeof leadOptConfidence.query_result === 'object' ? (leadOptConfidence.query_result as Record<string, unknown>) :
    {};
  const queryId = String(leadOptList.query_id || queryResult.query_id || leadOptState.query_id || leadOptConfidence.query_id || '').trim();
  const summary = readLeadOptTaskSummary(task);
  const candidateCount = Math.max(
    Number(summary?.candidateCount ?? 0) || 0,
    Array.isArray(leadOptList.enumerated_candidates)
      ? leadOptList.enumerated_candidates.length
      : Array.isArray(leadOptConfidence.enumerated_candidates)
        ? leadOptConfidence.enumerated_candidates.length
        : 0
  );
  const transformCount = Math.max(
    Number(summary?.transformCount ?? 0) || 0,
    Array.isArray(queryResult.transforms) ? queryResult.transforms.length : 0
  );
  const bucketCount = Math.max(Number(summary?.bucketCount ?? 0) || 0, Number(leadOptList.bucket_count || 0) || 0);
  if (queryId && candidateCount > 0) return 4;
  if (queryId && (transformCount > 0 || bucketCount > 0)) return 3;
  if (queryId) return 2;
  if (!summary) return 1;
  const success = Math.max(0, Number(summary.predictionSuccess || 0));
  const running = Math.max(0, Number(summary.predictionRunning || 0));
  const queued = Math.max(0, Number(summary.predictionQueued || 0));
  if (success > 0) return 2;
  if (running > 0) return 1;
  if (queued > 0) return 0;
  return 1;
}

function pickPreferredLeadOptSnapshotTask(tasks: ProjectTask[]): ProjectTask | null {
  let preferred: ProjectTask | null = null;
  for (const task of tasks) {
    if (!hasLeadOptSnapshotPayload(task)) continue;
    if (!preferred) {
      preferred = task;
      continue;
    }
    const preferredPriority = readLeadOptSnapshotPriority(preferred);
    const candidatePriority = readLeadOptSnapshotPriority(task);
    if (candidatePriority > preferredPriority) {
      preferred = task;
      continue;
    }
    if (candidatePriority < preferredPriority) continue;
    if (readLeadOptTaskRowTimestamp(task) > readLeadOptTaskRowTimestamp(preferred)) {
      preferred = task;
    }
  }
  return preferred;
}

export function useResultSnapshot(params: UseResultSnapshotParams): UseResultSnapshotResult {
  const {
    project,
    projectTasks,
    draftProperties,
    statusContextTaskRow,
    requestedStatusTaskRow,
    viewerTaskId,
    normalizedDraftComponents,
    workflowKey,
    shouldComputeResultMetrics,
    isDraftTaskSnapshot,
  } = params;

  const preferredLeadOptSnapshotTask = useMemo(
    () => (workflowKey === 'lead_optimization' ? pickPreferredLeadOptSnapshotTask(projectTasks) : null),
    [projectTasks, workflowKey]
  );

  const runtimeResultTask = useMemo(() => {
    const activeTaskId = String(project?.task_id || '').trim();
    const taskByProjectTaskId =
      activeTaskId
        ? projectTasks.find((item) => String(item.task_id || '').trim() === activeTaskId) || null
        : null;
    if (workflowKey !== 'lead_optimization') {
      return taskByProjectTaskId;
    }
    if (isRuntimeActiveTask(taskByProjectTaskId)) {
      return taskByProjectTaskId;
    }
    return preferredLeadOptSnapshotTask || taskByProjectTaskId;
  }, [preferredLeadOptSnapshotTask, project?.task_id, projectTasks, workflowKey]);

  const viewerResultTask = useMemo(() => {
    const normalizedViewerTaskId = String(viewerTaskId || '').trim();
    if (!normalizedViewerTaskId) return null;
    return projectTasks.find((item) => String(item.task_id || '').trim() === normalizedViewerTaskId) || null;
  }, [projectTasks, viewerTaskId]);

  const activeResultTask = useMemo(() => {
    if (viewerResultTask) {
      return viewerResultTask;
    }
    if (workflowKey !== 'lead_optimization') {
      // Keep the results panel pinned to an explicitly selected task row.
      // Status badges/polling may still follow the active runtime task separately.
      if (requestedStatusTaskRow?.id) {
        return requestedStatusTaskRow;
      }
      return statusContextTaskRow || runtimeResultTask;
    }
    if (requestedStatusTaskRow?.id && hasLeadOptSnapshotPayload(requestedStatusTaskRow)) {
      return requestedStatusTaskRow;
    }
    if (preferredLeadOptSnapshotTask) {
      return preferredLeadOptSnapshotTask;
    }
    if (isRuntimeActiveTask(statusContextTaskRow)) {
      return statusContextTaskRow || runtimeResultTask;
    }
    return runtimeResultTask || statusContextTaskRow || requestedStatusTaskRow;
  }, [preferredLeadOptSnapshotTask, requestedStatusTaskRow, runtimeResultTask, statusContextTaskRow, viewerResultTask, workflowKey]);

  const affinityUploadScopeTaskRowId = useMemo(() => {
    if (requestedStatusTaskRow?.id) {
      if (isDraftTaskSnapshot(requestedStatusTaskRow)) return requestedStatusTaskRow.id;
      return `${AFFINITY_UPLOAD_SCOPE_PREFIX}${requestedStatusTaskRow.id}`;
    }
    if (statusContextTaskRow?.id) {
      if (isDraftTaskSnapshot(statusContextTaskRow)) return statusContextTaskRow.id;
      return `${AFFINITY_UPLOAD_SCOPE_PREFIX}${statusContextTaskRow.id}`;
    }
    if (runtimeResultTask?.id && isDraftTaskSnapshot(runtimeResultTask)) return runtimeResultTask.id;
    return '__new__';
  }, [requestedStatusTaskRow, statusContextTaskRow, runtimeResultTask, isDraftTaskSnapshot]);

  const peptideBestCandidate = useMemo(() => {
    if (workflowKey !== 'peptide_design') return null;
    return readPeptideBestCandidateFromTask(activeResultTask);
  }, [activeResultTask, workflowKey]);

  const resultOverviewComponents = useMemo(() => {
    const taskComponents = readTaskComponents(activeResultTask);
    const baseComponents = taskComponents.length > 0 ? taskComponents : normalizedDraftComponents;
    if (workflowKey !== 'peptide_design' || !peptideBestCandidate?.sequence) return baseComponents;
    const withoutSyntheticPeptide = baseComponents.filter((component) => component.id !== '__peptide_design_best_candidate__');
    return [
      ...withoutSyntheticPeptide,
      {
        id: '__peptide_design_best_candidate__',
        type: 'protein',
        sequence: peptideBestCandidate.sequence,
        modifications: peptideBestCandidate.modifications,
        useMsa: false
      } as InputComponent
    ];
  }, [activeResultTask, normalizedDraftComponents, peptideBestCandidate, workflowKey]);

  const resultOverviewActiveComponents = useMemo(() => {
    return nonEmptyComponents(resultOverviewComponents);
  }, [resultOverviewComponents]);

  const resultChainInfos = useMemo(() => {
    return buildChainInfos(resultOverviewActiveComponents);
  }, [resultOverviewActiveComponents]);

  const resultChainIds = useMemo(() => {
    if (workflowKey === 'peptide_design' && peptideBestCandidate?.binderChainId) {
      const syntheticInfo = resultChainInfos.find((item) => item.componentId === '__peptide_design_best_candidate__') || null;
      return resultChainInfos.map((item) =>
        syntheticInfo && item.id === syntheticInfo.id ? peptideBestCandidate.binderChainId || item.id : item.id
      );
    }
    return resultChainInfos.map((item) => item.id);
  }, [peptideBestCandidate, resultChainInfos, workflowKey]);

  const resultComponentOptions = useMemo(() => {
    const chainByComponentId = new Map<string, string>();
    for (const info of resultChainInfos) {
      if (!chainByComponentId.has(info.componentId)) {
        const chainId =
          workflowKey === 'peptide_design' &&
          info.componentId === '__peptide_design_best_candidate__' &&
          peptideBestCandidate?.binderChainId
            ? peptideBestCandidate.binderChainId
            : info.id;
        chainByComponentId.set(info.componentId, chainId);
      }
    }
    return resultOverviewActiveComponents.map((component, index) => ({
      id: component.id,
      type: component.type,
      sequence: component.sequence,
      chainId: chainByComponentId.get(component.id) || null,
      isSmiles: component.type === 'ligand' && component.inputMethod !== 'ccd',
      label:
        component.id === '__peptide_design_best_candidate__'
          ? 'Designed peptide'
          : `Component ${index + 1} · ${componentTypeLabel(component.type)}`
    }));
  }, [peptideBestCandidate, resultOverviewActiveComponents, resultChainInfos, workflowKey]);

  const resultChainInfoById = useMemo(() => {
    const byId = new Map<string, ChainInfo>();
    for (const info of resultChainInfos) {
      const chainId =
        workflowKey === 'peptide_design' &&
        info.componentId === '__peptide_design_best_candidate__' &&
        peptideBestCandidate?.binderChainId
          ? peptideBestCandidate.binderChainId
          : info.id;
      byId.set(chainId, info);
    }
    return byId;
  }, [peptideBestCandidate, resultChainInfos, workflowKey]);

  const resultComponentById = useMemo(() => {
    const byId = new Map<string, InputComponent>();
    for (const component of resultOverviewActiveComponents) {
      byId.set(component.id, component);
    }
    return byId;
  }, [resultOverviewActiveComponents]);

  const resultChainShortLabelById = useMemo(() => {
    const byId = new Map<string, string>();
    const typeShort = (type: InputComponent['type']) => {
      if (type === 'protein') return 'Prot';
      if (type === 'ligand') return 'Lig';
      if (type === 'dna') return 'DNA';
      return 'RNA';
    };
    for (const info of resultChainInfos) {
      const chainId =
        workflowKey === 'peptide_design' &&
        info.componentId === '__peptide_design_best_candidate__' &&
        peptideBestCandidate?.binderChainId
          ? peptideBestCandidate.binderChainId
          : info.id;
      if (info.componentId === '__peptide_design_best_candidate__') {
        byId.set(chainId, 'Designed peptide');
        continue;
      }
      const compToken = `Comp ${info.componentIndex + 1}`;
      const copySuffix = info.copyIndex > 0 ? `.${info.copyIndex + 1}` : '';
      byId.set(chainId, `${compToken}${copySuffix} ${typeShort(info.type)}`);
    }
    return byId;
  }, [peptideBestCandidate, resultChainInfos, workflowKey]);

  const resultPairPreference = useMemo(() => {
    if (draftProperties && typeof draftProperties === 'object') {
      return draftProperties as unknown as Record<string, unknown>;
    }
    if (activeResultTask?.properties && typeof activeResultTask.properties === 'object') {
      return activeResultTask.properties as unknown as Record<string, unknown>;
    }
    return null;
  }, [draftProperties, activeResultTask?.properties]);

  const selectedResultTargetChainId = useMemo(() => {
    const affinityData =
      activeResultTask?.affinity && typeof activeResultTask.affinity === 'object' && !Array.isArray(activeResultTask.affinity)
        ? (activeResultTask.affinity as Record<string, unknown>)
        : project?.affinity && typeof project.affinity === 'object' && !Array.isArray(project.affinity)
          ? (project.affinity as Record<string, unknown>)
          : null;
    const confidenceData =
      activeResultTask?.confidence && typeof activeResultTask.confidence === 'object' && !Array.isArray(activeResultTask.confidence)
        ? (activeResultTask.confidence as Record<string, unknown>)
        : project?.confidence && typeof project.confidence === 'object' && !Array.isArray(project.confidence)
          ? (project.confidence as Record<string, unknown>)
          : null;
    return resolveSelectedResultTargetChainId({
      resultPairPreference,
      resultChainInfoById,
      resultComponentOptions,
      resultChainIds,
      affinityData,
      confidenceData
    });
  }, [
    resultPairPreference,
    resultChainInfoById,
    resultComponentOptions,
    resultChainIds,
    activeResultTask?.affinity,
    project?.affinity,
    activeResultTask?.confidence,
    project?.confidence
  ]);

  const selectedResultLigandChainId = useMemo(() => {
    if (workflowKey === 'peptide_design' && peptideBestCandidate?.binderChainId) {
      return peptideBestCandidate.binderChainId;
    }
    const shouldUseProjectFallback = workflowKey !== 'peptide_design';
    const affinityData =
      activeResultTask?.affinity && typeof activeResultTask.affinity === 'object' && !Array.isArray(activeResultTask.affinity)
        ? (activeResultTask.affinity as Record<string, unknown>)
        : shouldUseProjectFallback && project?.affinity && typeof project.affinity === 'object' && !Array.isArray(project.affinity)
          ? (project.affinity as Record<string, unknown>)
          : null;
    const confidenceData =
      activeResultTask?.confidence && typeof activeResultTask.confidence === 'object' && !Array.isArray(activeResultTask.confidence)
        ? (activeResultTask.confidence as Record<string, unknown>)
        : shouldUseProjectFallback && project?.confidence && typeof project.confidence === 'object' && !Array.isArray(project.confidence)
          ? (project.confidence as Record<string, unknown>)
          : null;
    return resolveSelectedResultLigandChainId({
      resultPairPreference,
      resultChainInfoById,
      resultComponentOptions,
      resultChainIds,
      selectedResultTargetChainId,
      affinityData,
      confidenceData,
      preferSequenceLigand: workflowKey === 'peptide_design'
    });
  }, [
    resultPairPreference,
    resultChainInfoById,
    resultComponentOptions,
    selectedResultTargetChainId,
    resultChainIds,
    activeResultTask?.affinity,
    project?.affinity,
    activeResultTask?.confidence,
    project?.confidence,
    workflowKey,
    peptideBestCandidate
  ]);

  const selectedResultLigandComponent = useMemo(() => {
    if (!selectedResultLigandChainId) return null;
    const info = resultChainInfoById.get(selectedResultLigandChainId);
    if (!info) return null;
    return resultComponentById.get(info.componentId) || null;
  }, [selectedResultLigandChainId, resultChainInfoById, resultComponentById]);

  const selectedResultLigandSequence = useMemo(() => {
    if (!selectedResultLigandComponent || !isSequenceLigandType(selectedResultLigandComponent.type)) return '';
    return normalizeComponentSequence(selectedResultLigandComponent.type, selectedResultLigandComponent.sequence || '');
  }, [selectedResultLigandComponent]);

  const overviewPrimaryLigand = useMemo(() => {
    if (selectedResultLigandComponent) {
      const selectedSequence = normalizeComponentSequence(
        selectedResultLigandComponent.type,
        selectedResultLigandComponent.sequence || ''
      );
      if (
        selectedResultLigandComponent.type === 'ligand' &&
        selectedResultLigandComponent.inputMethod !== 'ccd' &&
        selectedSequence
      ) {
        return {
          smiles: selectedSequence,
          isSmiles: true,
          selectedComponentType: selectedResultLigandComponent.type as InputComponent['type'] | null
        };
      }
      return {
        smiles: '',
        isSmiles: false,
        selectedComponentType: selectedResultLigandComponent.type as InputComponent['type'] | null
      };
    }
    return {
      smiles: '',
      isSmiles: false,
      selectedComponentType: null as InputComponent['type'] | null
    };
  }, [selectedResultLigandComponent]);

  const snapshotConfidence = useMemo(() => {
    if (activeResultTask?.confidence && Object.keys(activeResultTask.confidence).length > 0) {
      return activeResultTask.confidence as Record<string, unknown>;
    }
    if (workflowKey === 'peptide_design') {
      return null;
    }
    if (project?.confidence && Object.keys(project.confidence).length > 0) {
      return project.confidence as Record<string, unknown>;
    }
    return null;
  }, [activeResultTask?.confidence, project?.confidence, workflowKey]);

  const resultChainConsistencyWarning = useMemo(() => {
    return buildResultChainConsistencyWarning({
      workflowKey,
      snapshotConfidence,
      resultChainIds,
      resultOverviewActiveComponents
    });
  }, [workflowKey, snapshotConfidence, resultChainIds, resultOverviewActiveComponents]);

  const snapshotAffinity = useMemo(() => {
    if (activeResultTask?.affinity && Object.keys(activeResultTask.affinity).length > 0) {
      return activeResultTask.affinity as Record<string, unknown>;
    }
    if (workflowKey === 'peptide_design') {
      return null;
    }
    if (project?.affinity && Object.keys(project.affinity).length > 0) {
      return project.affinity as Record<string, unknown>;
    }
    return null;
  }, [activeResultTask?.affinity, project?.affinity, workflowKey]);

  const snapshotLigandAtomPlddts = useMemo(() => {
    if (!shouldComputeResultMetrics) return [];
    return readLigandAtomPlddtsFromConfidence(snapshotConfidence, selectedResultLigandChainId);
  }, [shouldComputeResultMetrics, snapshotConfidence, selectedResultLigandChainId]);

  const snapshotLigandResiduePlddts = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (!selectedResultLigandSequence || !selectedResultLigandChainId) return null;
    const raw = readResiduePlddtsForChain(snapshotConfidence, selectedResultLigandChainId);
    return alignConfidenceSeriesToLength(raw, selectedResultLigandSequence.length);
  }, [shouldComputeResultMetrics, snapshotConfidence, selectedResultLigandChainId, selectedResultLigandSequence]);

  const snapshotLigandMeanPlddt = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (!snapshotLigandAtomPlddts.length) return null;
    return mean(snapshotLigandAtomPlddts);
  }, [shouldComputeResultMetrics, snapshotLigandAtomPlddts]);

  const snapshotSelectedLigandChainPlddt = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    return readChainMeanPlddtForChain(snapshotConfidence, selectedResultLigandChainId);
  }, [shouldComputeResultMetrics, snapshotConfidence, selectedResultLigandChainId]);

  const snapshotPlddt = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (snapshotSelectedLigandChainPlddt !== null) {
      return snapshotSelectedLigandChainPlddt;
    }
    if (snapshotLigandMeanPlddt !== null) {
      return snapshotLigandMeanPlddt;
    }
    if (!snapshotConfidence) return null;
    const raw = readFirstFiniteMetric(snapshotConfidence, [
      'ligand_plddt',
      'ligand_mean_plddt',
      'complex_iplddt',
      'complex_plddt_protein',
      'complex_plddt',
      'plddt'
    ]);
    if (raw === null) return null;
    return raw <= 1 ? raw * 100 : raw;
  }, [shouldComputeResultMetrics, snapshotConfidence, snapshotLigandMeanPlddt, snapshotSelectedLigandChainPlddt]);

  const snapshotSelectedPairIptm = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    return readPairIptmForChains(
      snapshotConfidence,
      selectedResultTargetChainId,
      selectedResultLigandChainId,
      resultChainIds
    );
  }, [shouldComputeResultMetrics, snapshotConfidence, selectedResultTargetChainId, selectedResultLigandChainId, resultChainIds]);

  const snapshotIptm = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (snapshotSelectedPairIptm !== null) return snapshotSelectedPairIptm;
    if (!snapshotConfidence) return null;
    const raw = readFirstFiniteMetric(snapshotConfidence, ['iptm', 'ligand_iptm', 'protein_iptm']);
    if (raw === null) return null;
    return raw > 1 && raw <= 100 ? raw / 100 : raw;
  }, [shouldComputeResultMetrics, snapshotConfidence, snapshotSelectedPairIptm]);

  const snapshotBindingValues = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (!snapshotAffinity) return null;
    const values = readFiniteMetricSeries(snapshotAffinity, [
      'affinity_probability_binary',
      'affinity_probability_binary1',
      'affinity_probability_binary2'
    ]).map((value) => (value > 1 && value <= 100 ? value / 100 : value));
    const normalized = values.filter((value) => Number.isFinite(value) && value >= 0 && value <= 1);
    if (normalized.length === 0) return null;
    return normalized;
  }, [shouldComputeResultMetrics, snapshotAffinity]);

  const snapshotBindingProbability = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (!snapshotBindingValues?.length) return null;
    return Math.max(0, Math.min(1, mean(snapshotBindingValues)));
  }, [shouldComputeResultMetrics, snapshotBindingValues]);

  const snapshotBindingStd = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (!snapshotBindingValues?.length) return null;
    return std(snapshotBindingValues);
  }, [shouldComputeResultMetrics, snapshotBindingValues]);

  const snapshotLogIc50Values = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (!snapshotAffinity) return null;
    const logValues = readFiniteMetricSeries(snapshotAffinity, [
      'affinity_pred_value',
      'affinity_pred_value1',
      'affinity_pred_value2'
    ]);
    if (logValues.length === 0) return null;
    return logValues;
  }, [shouldComputeResultMetrics, snapshotAffinity]);

  const snapshotIc50Um = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (!snapshotLogIc50Values?.length) return null;
    return 10 ** mean(snapshotLogIc50Values);
  }, [shouldComputeResultMetrics, snapshotLogIc50Values]);

  const snapshotIc50Error = useMemo(() => {
    if (!shouldComputeResultMetrics) return null;
    if (!snapshotLogIc50Values?.length || snapshotLogIc50Values.length <= 1) return null;
    const meanLog = mean(snapshotLogIc50Values);
    const stdLog = std(snapshotLogIc50Values);
    const center = 10 ** meanLog;
    const lower = 10 ** (meanLog - stdLog);
    const upper = 10 ** (meanLog + stdLog);
    return {
      plus: Math.max(0, upper - center),
      minus: Math.max(0, center - lower)
    };
  }, [shouldComputeResultMetrics, snapshotLogIc50Values]);

  const snapshotPlddtTone = useMemo(() => toneForPlddt(snapshotPlddt), [snapshotPlddt]);
  const snapshotIptmTone = useMemo(() => toneForIptm(snapshotIptm), [snapshotIptm]);
  const snapshotIc50Tone = useMemo(() => toneForIc50(snapshotIc50Um), [snapshotIc50Um]);
  const snapshotBindingTone = useMemo(() => toneForProbability(snapshotBindingProbability), [snapshotBindingProbability]);

  return {
    runtimeResultTask,
    activeResultTask,
    affinityUploadScopeTaskRowId,
    resultOverviewComponents,
    resultOverviewActiveComponents,
    resultChainInfos,
    resultChainIds,
    resultComponentOptions,
    resultChainInfoById,
    resultComponentById,
    resultChainShortLabelById,
    resultPairPreference,
    selectedResultTargetChainId,
    selectedResultLigandChainId,
    selectedResultLigandComponent,
    selectedResultLigandSequence,
    overviewPrimaryLigand,
    snapshotConfidence,
    resultChainConsistencyWarning,
    snapshotAffinity,
    snapshotLigandAtomPlddts,
    snapshotLigandResiduePlddts,
    snapshotLigandMeanPlddt,
    snapshotSelectedLigandChainPlddt,
    snapshotPlddt,
    snapshotSelectedPairIptm,
    snapshotIptm,
    snapshotBindingValues,
    snapshotBindingProbability,
    snapshotBindingStd,
    snapshotLogIc50Values,
    snapshotIc50Um,
    snapshotIc50Error,
    snapshotPlddtTone,
    snapshotIptmTone,
    snapshotIc50Tone,
    snapshotBindingTone,
  };
}
