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

type ChainInfo = ReturnType<typeof buildChainInfos>[number];

export interface UseResultSnapshotParams {
  project: Project | null;
  projectTasks: ProjectTask[];
  draftProperties: ProjectInputConfig['properties'] | null | undefined;
  statusContextTaskRow: ProjectTask | null;
  requestedStatusTaskRow: ProjectTask | null;
  normalizedDraftComponents: InputComponent[];
  workflowKey: string;
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

export function useResultSnapshot(params: UseResultSnapshotParams): UseResultSnapshotResult {
  const {
    project,
    projectTasks,
    draftProperties,
    statusContextTaskRow,
    requestedStatusTaskRow,
    normalizedDraftComponents,
    workflowKey,
    isDraftTaskSnapshot,
  } = params;

  const runtimeResultTask = useMemo(() => {
    const activeTaskId = String(project?.task_id || '').trim();
    if (!activeTaskId) return null;
    return projectTasks.find((item) => String(item.task_id || '').trim() === activeTaskId) || null;
  }, [project?.task_id, projectTasks]);

  const activeResultTask = statusContextTaskRow || runtimeResultTask;

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
    const latestDraftTask =
      projectTasks.find((item) => item.task_state === 'DRAFT' && !String(item.task_id || '').trim()) || null;
    if (latestDraftTask?.id) return latestDraftTask.id;
    return '__new__';
  }, [requestedStatusTaskRow, statusContextTaskRow, runtimeResultTask, projectTasks, isDraftTaskSnapshot]);

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
    if (project?.confidence && Object.keys(project.confidence).length > 0) {
      return project.confidence as Record<string, unknown>;
    }
    return null;
  }, [activeResultTask?.confidence, project?.confidence]);

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
    if (project?.affinity && Object.keys(project.affinity).length > 0) {
      return project.affinity as Record<string, unknown>;
    }
    return null;
  }, [activeResultTask?.affinity, project?.affinity]);

  const snapshotLigandAtomPlddts = useMemo(() => {
    return readLigandAtomPlddtsFromConfidence(snapshotConfidence, selectedResultLigandChainId);
  }, [snapshotConfidence, selectedResultLigandChainId]);

  const snapshotLigandResiduePlddts = useMemo(() => {
    if (!selectedResultLigandSequence || !selectedResultLigandChainId) return null;
    const raw = readResiduePlddtsForChain(snapshotConfidence, selectedResultLigandChainId);
    return alignConfidenceSeriesToLength(raw, selectedResultLigandSequence.length);
  }, [snapshotConfidence, selectedResultLigandChainId, selectedResultLigandSequence]);

  const snapshotLigandMeanPlddt = useMemo(() => {
    if (!snapshotLigandAtomPlddts.length) return null;
    return mean(snapshotLigandAtomPlddts);
  }, [snapshotLigandAtomPlddts]);

  const snapshotSelectedLigandChainPlddt = useMemo(() => {
    return readChainMeanPlddtForChain(snapshotConfidence, selectedResultLigandChainId);
  }, [snapshotConfidence, selectedResultLigandChainId]);

  const snapshotPlddt = useMemo(() => {
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
  }, [snapshotConfidence, snapshotLigandMeanPlddt, snapshotSelectedLigandChainPlddt]);

  const snapshotSelectedPairIptm = useMemo(() => {
    return readPairIptmForChains(
      snapshotConfidence,
      selectedResultTargetChainId,
      selectedResultLigandChainId,
      resultChainIds
    );
  }, [snapshotConfidence, selectedResultTargetChainId, selectedResultLigandChainId, resultChainIds]);

  const snapshotIptm = useMemo(() => {
    return snapshotSelectedPairIptm;
  }, [snapshotSelectedPairIptm]);

  const snapshotBindingValues = useMemo(() => {
    if (!snapshotAffinity) return null;
    const values = readFiniteMetricSeries(snapshotAffinity, [
      'affinity_probability_binary',
      'affinity_probability_binary1',
      'affinity_probability_binary2'
    ]).map((value) => (value > 1 && value <= 100 ? value / 100 : value));
    const normalized = values.filter((value) => Number.isFinite(value) && value >= 0 && value <= 1);
    if (normalized.length === 0) return null;
    return normalized;
  }, [snapshotAffinity]);

  const snapshotBindingProbability = useMemo(() => {
    if (!snapshotBindingValues?.length) return null;
    return Math.max(0, Math.min(1, mean(snapshotBindingValues)));
  }, [snapshotBindingValues]);

  const snapshotBindingStd = useMemo(() => {
    if (!snapshotBindingValues?.length) return null;
    return std(snapshotBindingValues);
  }, [snapshotBindingValues]);

  const snapshotLogIc50Values = useMemo(() => {
    if (!snapshotAffinity) return null;
    const logValues = readFiniteMetricSeries(snapshotAffinity, [
      'affinity_pred_value',
      'affinity_pred_value1',
      'affinity_pred_value2'
    ]);
    if (logValues.length === 0) return null;
    return logValues;
  }, [snapshotAffinity]);

  const snapshotIc50Um = useMemo(() => {
    if (!snapshotLogIc50Values?.length) return null;
    return 10 ** mean(snapshotLogIc50Values);
  }, [snapshotLogIc50Values]);

  const snapshotIc50Error = useMemo(() => {
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
  }, [snapshotLogIc50Values]);

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
