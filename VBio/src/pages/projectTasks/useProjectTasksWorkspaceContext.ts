import { useMemo } from 'react';
import type { Project, ProjectTask } from '../../types/models';
import { loadProjectInputConfig } from '../../utils/projectInputs';
import { getWorkflowDefinition } from '../../utils/workflows';
import type { TaskListRow, TaskWorkflowFilter, WorkspacePairPreference } from './taskListTypes';
import { resolveTaskWorkflowKey } from './taskPresentation';
import {
  alignConfidenceSeriesToLength,
  isProjectTaskRow,
  isSequenceLigandType,
  mean,
  readPeptideBestCandidatePreview,
  readPeptideTaskSummary,
  readLeadOptTaskSummary,
  readTaskConfidenceMetrics,
  readTaskLigandAtomPlddts,
  readTaskLigandResiduePlddts,
  resolveTaskBackendValue,
  resolveTaskSelectionContext,
  sanitizeTaskRows
} from './taskDataUtils';

interface UseProjectTasksWorkspaceContextInput {
  project: Project | null;
  tasks: ProjectTask[];
}

interface UseProjectTasksWorkspaceContextResult {
  taskCountText: string;
  currentTaskRow: ProjectTask | null;
  backToCurrentTaskHref: string;
  createTaskHref: string;
  workspacePairPreference: WorkspacePairPreference;
  taskRows: TaskListRow[];
  workflowOptions: TaskWorkflowFilter[];
  backendOptions: string[];
}

export function useProjectTasksWorkspaceContext({
  project,
  tasks
}: UseProjectTasksWorkspaceContextInput): UseProjectTasksWorkspaceContextResult {
  const taskCountText = useMemo(() => `${sanitizeTaskRows(tasks).length} tasks`, [tasks]);

  const currentTaskRow = useMemo(() => {
    if (!project) return null;
    const currentRuntimeTaskId = String(project.task_id || '').trim();
    if (currentRuntimeTaskId) {
      const matchedRuntime = tasks.find(
        (row) => isProjectTaskRow(row) && String(row.task_id || '').trim() === currentRuntimeTaskId
      );
      if (matchedRuntime) return matchedRuntime;
    }
    const latestDraft = tasks.find(
      (row) => isProjectTaskRow(row) && row.task_state === 'DRAFT' && !String(row.task_id || '').trim()
    );
    if (latestDraft) return latestDraft;
    return tasks.find((row) => isProjectTaskRow(row)) || null;
  }, [project, tasks]);

  const backToCurrentTaskHref = useMemo(() => {
    if (!project) return '/projects';
    const params = new URLSearchParams();
    const currentTaskId = String(currentTaskRow?.task_id || project.task_id || '').trim();
    params.set('tab', currentTaskId ? 'results' : 'components');
    if (currentTaskRow?.id) {
      params.set('task_row_id', currentTaskRow.id);
    }
    return `/projects/${project.id}?${params.toString()}`;
  }, [project, currentTaskRow]);

  const createTaskHref = useMemo(() => {
    if (!project) return '/projects';
    const params = new URLSearchParams();
    params.set('tab', 'components');
    params.set('new_task', '1');
    return `/projects/${project.id}?${params.toString()}`;
  }, [project]);

  const workspacePairPreference = useMemo<WorkspacePairPreference>(() => {
    if (!project) {
      return {
        targetChainId: null,
        ligandChainId: null
      };
    }

    const savedConfig = loadProjectInputConfig(project.id);
    const savedTarget = String(savedConfig?.properties?.target || '')
      .trim();
    const savedLigand = String(savedConfig?.properties?.binder || savedConfig?.properties?.ligand || '')
      .trim();
    const currentProps =
      currentTaskRow?.properties && typeof currentTaskRow.properties === 'object' ? currentTaskRow.properties : null;
    const currentTarget = String(currentProps?.target || '')
      .trim();
    const currentLigand = String(currentProps?.binder || currentProps?.ligand || '')
      .trim();

    return {
      targetChainId: currentTarget || savedTarget || null,
      ligandChainId: currentLigand || savedLigand || null
    };
  }, [project, currentTaskRow]);

  const taskRows = useMemo<TaskListRow[]>(() => {
    return sanitizeTaskRows(tasks).map((task) => {
      const submittedTs = new Date(task.submitted_at || task.created_at).getTime();
      const durationValue =
        typeof task.duration_seconds === 'number' && Number.isFinite(task.duration_seconds)
          ? task.duration_seconds
          : null;
      const resolvedWorkflow = resolveTaskWorkflowKey(task, project?.task_type || '');
      const workflowKey =
        resolvedWorkflow === 'affinity' ||
        resolvedWorkflow === 'lead_optimization' ||
        resolvedWorkflow === 'peptide_design'
          ? resolvedWorkflow
          : 'prediction';
      const selection = resolveTaskSelectionContext(task, workspacePairPreference, workflowKey);
      const ligandAtomPlddts = readTaskLigandAtomPlddts(task, selection.ligandChainId, selection.ligandComponentCount <= 1);
      const peptideBest = workflowKey === 'peptide_design' ? readPeptideBestCandidatePreview(task) : null;
      const resolvedLigandSequence =
        workflowKey === 'peptide_design' && peptideBest?.sequence
          ? peptideBest.sequence
          : selection.ligandSequence;
      const resolvedLigandSequenceType =
        workflowKey === 'peptide_design' && peptideBest?.sequence
          ? 'protein'
          : selection.ligandSequenceType;
      const ligandResiduePlddtsRaw =
        workflowKey === 'peptide_design' && peptideBest?.sequence
          ? peptideBest.residuePlddts
          : selection.ligandSequence && isSequenceLigandType(selection.ligandSequenceType)
            ? readTaskLigandResiduePlddts(task, selection.ligandChainId)
            : null;
      const ligandResiduePlddts = alignConfidenceSeriesToLength(ligandResiduePlddtsRaw, resolvedLigandSequence.length, null);
      const metrics = readTaskConfidenceMetrics(task, selection);
      const ligandMeanPlddt = mean(ligandAtomPlddts);
      const ligandSequenceMeanPlddt = mean(ligandResiduePlddts);
      const plddt =
        metrics.plddt !== null
          ? metrics.plddt
          : workflowKey === 'peptide_design'
            ? peptideBest?.plddt ?? ligandMeanPlddt ?? ligandSequenceMeanPlddt
            : ligandMeanPlddt ?? ligandSequenceMeanPlddt;
      const leadOpt = readLeadOptTaskSummary(task);
      const peptide = workflowKey === 'peptide_design' ? readPeptideTaskSummary(task) : null;
      const resolvedBucketCount =
        workflowKey === 'lead_optimization' && leadOpt
          ? leadOpt.bucketCount
          : null;
      return {
        task,
        metrics: {
          ...metrics,
          plddt
        },
        submittedTs,
        backendValue: resolveTaskBackendValue(task, project?.backend || ''),
        durationValue,
        ligandSmiles: workflowKey === 'peptide_design' ? '' : selection.ligandSmiles,
        ligandIsSmiles: workflowKey === 'peptide_design' ? false : selection.ligandIsSmiles,
        ligandAtomPlddts,
        ligandSequence: resolvedLigandSequence,
        ligandSequenceType: resolvedLigandSequenceType,
        ligandResiduePlddts,
        workflowKey,
        workflowLabel: getWorkflowDefinition(workflowKey).shortTitle,
        leadOptMmpSummary: leadOpt?.summary || '',
        leadOptMmpStage: leadOpt?.stage || '',
        leadOptDatabaseId: leadOpt?.databaseId || '',
        leadOptDatabaseLabel: leadOpt?.databaseLabel || '',
        leadOptDatabaseSchema: leadOpt?.databaseSchema || '',
        leadOptTransformCount: leadOpt?.transformCount ?? null,
        leadOptCandidateCount: leadOpt?.candidateCount ?? null,
        leadOptBucketCount: resolvedBucketCount,
        leadOptPredictionTotal: leadOpt?.predictionTotal ?? null,
        leadOptPredictionQueued: leadOpt?.predictionQueued ?? null,
        leadOptPredictionRunning: leadOpt?.predictionRunning ?? null,
        leadOptPredictionSuccess: leadOpt?.predictionSuccess ?? null,
        leadOptPredictionFailure: leadOpt?.predictionFailure ?? null,
        leadOptSelectedFragmentIds: leadOpt?.selectedFragmentIds || [],
        leadOptSelectedAtomIndices: leadOpt?.selectedAtomIndices || [],
        leadOptSelectedFragmentQuery: leadOpt?.selectedFragmentQuery || '',
        peptideDesignMode: peptide?.designMode ?? null,
        peptideBinderLength: peptide?.binderLength ?? null,
        peptideIterations: peptide?.iterations ?? null,
        peptidePopulationSize: peptide?.populationSize ?? null,
        peptideEliteSize: peptide?.eliteSize ?? null,
        peptideMutationRate: peptide?.mutationRate ?? null,
        peptideCurrentGeneration: peptide?.currentGeneration ?? null,
        peptideTotalGenerations: peptide?.totalGenerations ?? null,
        peptideBestScore: peptide?.bestScore ?? null,
        peptideCandidateCount: peptide?.candidateCount ?? null,
        peptideCompletedTasks: peptide?.completedTasks ?? null,
        peptidePendingTasks: peptide?.pendingTasks ?? null,
        peptideTotalTasks: peptide?.totalTasks ?? null,
        peptideStage: peptide?.stage || '',
        peptideStatusMessage: peptide?.statusMessage || ''
      };
    });
  }, [tasks, workspacePairPreference, project?.backend, project?.task_type]);

  const workflowOptions = useMemo<TaskWorkflowFilter[]>(
    () => Array.from(new Set(taskRows.map((row) => row.workflowKey))).sort((a, b) => a.localeCompare(b)),
    [taskRows]
  );

  const backendOptions = useMemo(
    () =>
      Array.from(new Set(taskRows.map((row) => row.backendValue).filter(Boolean))).sort((a, b) =>
        a.localeCompare(b)
      ),
    [taskRows]
  );

  return {
    taskCountText,
    currentTaskRow,
    backToCurrentTaskHref,
    createTaskHref,
    workspacePairPreference,
    taskRows,
    workflowOptions,
    backendOptions
  };
}
