import { useMemo } from 'react';
import type { InputComponent, Project, ProjectInputConfig, ProjectTask } from '../../types/models';
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

  const activeResultTask = useMemo(() => {
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
  }, [preferredLeadOptSnapshotTask, requestedStatusTaskRow, runtimeResultTask, statusContextTaskRow, workflowKey]);

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

  const resultOverviewComponents = useMemo(() => {
    const taskComponents = readTaskComponents(activeResultTask);
    if (taskComponents.length > 0) return taskComponents;
    return normalizedDraftComponents;
  }, [activeResultTask, normalizedDraftComponents]);

  const resultOverviewActiveComponents = useMemo(() => {
    return nonEmptyComponents(resultOverviewComponents);
  }, [resultOverviewComponents]);

  const resultChainInfos = useMemo(() => {
    return buildChainInfos(resultOverviewActiveComponents);
  }, [resultOverviewActiveComponents]);

  const resultChainIds = useMemo(() => {
    return resultChainInfos.map((item) => item.id);
  }, [resultChainInfos]);

  const resultComponentOptions = useMemo(() => {
    const chainByComponentId = new Map<string, string>();
    for (const info of resultChainInfos) {
      if (!chainByComponentId.has(info.componentId)) {
        chainByComponentId.set(info.componentId, info.id);
      }
    }
    return resultOverviewActiveComponents.map((component, index) => ({
      id: component.id,
      type: component.type,
      sequence: component.sequence,
      chainId: chainByComponentId.get(component.id) || null,
      isSmiles: component.type === 'ligand' && component.inputMethod !== 'ccd',
      label: `Component ${index + 1} · ${componentTypeLabel(component.type)}`
    }));
  }, [resultOverviewActiveComponents, resultChainInfos]);

  const resultChainInfoById = useMemo(() => {
    const byId = new Map<string, ChainInfo>();
    for (const info of resultChainInfos) {
      byId.set(info.id, info);
    }
    return byId;
  }, [resultChainInfos]);

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
      const compToken = `Comp ${info.componentIndex + 1}`;
      const copySuffix = info.copyIndex > 0 ? `.${info.copyIndex + 1}` : '';
      byId.set(info.id, `${compToken}${copySuffix} ${typeShort(info.type)}`);
    }
    return byId;
  }, [resultChainInfos]);

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
    workflowKey
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
