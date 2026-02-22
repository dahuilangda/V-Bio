import type { MutableRefObject } from 'react';
import { downloadResultBlob, getTaskStatus, parseResultBundle } from '../../api/backendApi';
import { updateProject, updateProjectTask } from '../../api/supabaseLite';
import type { Project, ProjectTask } from '../../types/models';
import {
  readLeadOptTaskSummary,
  readTaskConfidenceMetrics,
  readTaskLigandAtomPlddts,
  hasTaskLigandAtomPlddts,
  hasTaskSummaryMetrics,
  isProjectRow,
  isProjectTaskRow,
  mapTaskState,
  mean,
  readStatusText,
  resolveTaskBackendValue,
  resolveTaskSelectionContext,
  sanitizeTaskRows,
  sortProjectTasks
} from './taskDataUtils';
import { resolveTaskWorkflowKey } from './taskPresentation';

function hasLeadOptMmpOnlySnapshot(task: ProjectTask): boolean {
  const confidence =
    task && task.confidence && typeof task.confidence === 'object'
      ? (task.confidence as Record<string, unknown>)
      : {};
  const leadOptMmp = confidence.lead_opt_mmp;
  if (leadOptMmp && typeof leadOptMmp === 'object') return true;
  return String(task.status_text || '').toUpperCase().includes('MMP');
}

type LeadOptTaskSummary = NonNullable<ReturnType<typeof readLeadOptTaskSummary>>;

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readFiniteNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value.trim()) : Number.NaN;
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

function readFiniteNumberArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => readFiniteNumber(item))
    .filter((item): item is number => item !== null);
}

function hasFiniteMetric(value: unknown): boolean {
  return typeof value === 'number' && Number.isFinite(value);
}

function deriveLeadOptRuntimeState(summary: LeadOptTaskSummary): {
  taskState: ProjectTask['task_state'];
  statusText: string;
  errorText: string;
} | null {
  const queued = Math.max(0, summary.predictionQueued || 0);
  const running = Math.max(0, summary.predictionRunning || 0);
  const success = Math.max(0, summary.predictionSuccess || 0);
  const failure = Math.max(0, summary.predictionFailure || 0);
  const unresolved = queued + running;
  const total = summary.predictionTotal !== null ? Math.max(0, summary.predictionTotal) : Math.max(0, unresolved + success + failure);

  if (unresolved > 0) {
    const taskState: ProjectTask['task_state'] = running > 0 ? 'RUNNING' : 'QUEUED';
    const done = success;
    const denom = total > 0 ? total : unresolved + done;
    const statusText =
      taskState === 'RUNNING'
        ? `Scoring ${unresolved} running (${done}/${Math.max(1, denom)} done)`
        : `Scoring ${unresolved} queued (${done}/${Math.max(1, denom)} done)`;
    return {
      taskState,
      statusText,
      errorText: ''
    };
  }

  if (success > 0 || failure > 0 || total > 0) {
    const allFailed = success === 0 && failure > 0;
    return {
      taskState: allFailed ? 'FAILURE' : 'SUCCESS',
      statusText: allFailed ? `Scoring complete (0/${Math.max(1, total || failure)})` : `Scoring complete (${success}/${Math.max(1, total || success)})`,
      errorText: allFailed ? 'All candidate scoring jobs failed.' : ''
    };
  }

  const stage = String(summary.stage || '').trim().toLowerCase();
  if (stage === 'prediction_running' || stage === 'running') {
    return { taskState: 'RUNNING', statusText: 'Scoring running', errorText: '' };
  }
  if (stage === 'prediction_queued' || stage === 'queued') {
    return { taskState: 'QUEUED', statusText: 'Scoring queued', errorText: '' };
  }
  if (stage === 'prediction_failed' || stage === 'failed') {
    return { taskState: 'FAILURE', statusText: 'Scoring failed', errorText: 'Scoring failed.' };
  }
  if (stage === 'prediction_completed' || stage === 'completed') {
    return { taskState: 'SUCCESS', statusText: 'Scoring complete', errorText: '' };
  }

  return null;
}

function promoteLeadOptPredictionMetrics(task: ProjectTask): {
  confidencePatch: Record<string, unknown> | null;
  structureNamePatch: string;
} {
  const confidence = asRecord(task.confidence);
  const leadOptMmp = asRecord(confidence.lead_opt_mmp);
  const predictionMap = asRecord(leadOptMmp.prediction_by_smiles);
  const records = Object.values(predictionMap)
    .map((value) => asRecord(value))
    .map((record) => {
      const state = String(record.state || '').trim().toUpperCase();
      const pairIptm = readFiniteNumber(record.pairIptm ?? record.pair_iptm);
      const pairPae = readFiniteNumber(record.pairPae ?? record.pair_pae);
      const ligandPlddt = readFiniteNumber(record.ligandPlddt ?? record.ligand_plddt);
      const ligandAtomPlddts = readFiniteNumberArray(record.ligandAtomPlddts ?? record.ligand_atom_plddts);
      const structureName = String(record.structureName ?? record.structure_name ?? '').trim();
      const updatedAt = readFiniteNumber(record.updatedAt ?? record.updated_at) || 0;
      const hasMetrics = pairIptm !== null || pairPae !== null || ligandPlddt !== null || ligandAtomPlddts.length > 0 || Boolean(structureName);
      return {
        state,
        pairIptm,
        pairPae,
        ligandPlddt,
        ligandAtomPlddts,
        structureName,
        updatedAt,
        hasMetrics
      };
    })
    .filter((record) => record.hasMetrics);
  if (records.length === 0) {
    return {
      confidencePatch: null,
      structureNamePatch: ''
    };
  }

  const sorted = [...records].sort((a, b) => b.updatedAt - a.updatedAt);
  const bestSuccess = sorted.find((record) => record.state === 'SUCCESS');
  const best = bestSuccess || sorted[0];
  const nextConfidence: Record<string, unknown> = { ...confidence };
  let changed = false;

  if (best.pairIptm !== null && !hasFiniteMetric(nextConfidence.iptm)) {
    nextConfidence.iptm = best.pairIptm;
    changed = true;
  }

  const hasPae =
    hasFiniteMetric(nextConfidence.complex_pde) || hasFiniteMetric(nextConfidence.complex_pae) || hasFiniteMetric(nextConfidence.pae);
  if (best.pairPae !== null && !hasPae) {
    nextConfidence.complex_pde = best.pairPae;
    nextConfidence.complex_pae = best.pairPae;
    nextConfidence.pae = best.pairPae;
    changed = true;
  }

  const hasLigandPlddt =
    hasFiniteMetric(nextConfidence.ligand_plddt) ||
    hasFiniteMetric(nextConfidence.ligand_mean_plddt) ||
    hasFiniteMetric(nextConfidence.complex_plddt) ||
    hasFiniteMetric(nextConfidence.plddt);
  if (best.ligandPlddt !== null && !hasLigandPlddt) {
    nextConfidence.ligand_plddt = best.ligandPlddt;
    nextConfidence.ligand_mean_plddt = best.ligandPlddt;
    nextConfidence.complex_plddt = best.ligandPlddt;
    changed = true;
  }

  const currentLigandAtomPlddts = readFiniteNumberArray(nextConfidence.ligand_atom_plddts);
  if (best.ligandAtomPlddts.length > 0 && currentLigandAtomPlddts.length === 0) {
    nextConfidence.ligand_atom_plddts = best.ligandAtomPlddts;
    changed = true;
  }

  return {
    confidencePatch: changed ? nextConfidence : null,
    structureNamePatch: String(task.structure_name || '').trim() ? '' : best.structureName
  };
}

function summarizeLeadOptPredictionMap(predictionMap: Record<string, unknown>): {
  total: number;
  queued: number;
  running: number;
  success: number;
  failure: number;
} {
  let queued = 0;
  let running = 0;
  let success = 0;
  let failure = 0;
  for (const value of Object.values(predictionMap)) {
    const record = asRecord(value);
    const state = String(record.state || '').trim().toUpperCase();
    if (state === 'QUEUED') queued += 1;
    else if (state === 'RUNNING') running += 1;
    else if (state === 'SUCCESS') success += 1;
    else if (state === 'FAILURE') failure += 1;
  }
  return {
    total: Object.keys(predictionMap).length,
    queued,
    running,
    success,
    failure
  };
}

async function reconcileLeadOptPredictionMapStates(
  predictionMap: Record<string, unknown>
): Promise<{ nextMap: Record<string, unknown>; changed: boolean }> {
  const nextMap: Record<string, unknown> = { ...predictionMap };
  let changed = false;
  const pendingEntries = Object.entries(predictionMap)
    .map(([smiles, value]) => ({ smiles, record: asRecord(value) }))
    .filter(({ record }) => {
      const state = String(record.state || '').trim().toUpperCase();
      const taskId = String(record.taskId || record.task_id || '').trim();
      return Boolean(taskId) && (state === 'QUEUED' || state === 'RUNNING');
    })
    .slice(0, 4);

  for (const entry of pendingEntries) {
    const taskId = String(entry.record.taskId || entry.record.task_id || '').trim();
    if (!taskId) continue;
    try {
      const status = await getTaskStatus(taskId);
      const runtimeState = mapTaskState(status.state);
      const state =
        runtimeState === 'SUCCESS'
          ? 'SUCCESS'
          : runtimeState === 'FAILURE' || runtimeState === 'REVOKED'
            ? 'FAILURE'
            : runtimeState === 'RUNNING'
              ? 'RUNNING'
              : 'QUEUED';
      const errorText =
        state === 'FAILURE'
          ? String(
              (status.info && typeof status.info === 'object'
                ? (status.info as Record<string, unknown>).error || (status.info as Record<string, unknown>).message
                : '') || status.state || 'Prediction failed.'
            ).trim()
          : '';
      const currentState = String(entry.record.state || '').trim().toUpperCase();
      const currentError = String(entry.record.error || '').trim();
      if (currentState === state && currentError === errorText) continue;
      nextMap[entry.smiles] = {
        ...entry.record,
        state,
        error: errorText,
        updatedAt: Date.now()
      };
      changed = true;
    } catch {
      // Keep existing state on transient status errors.
    }
  }

  return { nextMap, changed };
}

async function hydrateLeadOptPredictionMetricsFromResult(
  task: ProjectTask,
  predictionMap: Record<string, unknown>
): Promise<{
  nextMap: Record<string, unknown>;
  confidencePatch: Record<string, unknown> | null;
  structureNamePatch: string;
  changed: boolean;
}> {
  const successEntries = Object.entries(predictionMap)
    .map(([smiles, value]) => ({ smiles, record: asRecord(value) }))
    .filter(({ record }) => {
      const state = String(record.state || '').trim().toUpperCase();
      if (state !== 'SUCCESS') return false;
      const taskId = String(record.taskId || record.task_id || '').trim();
      if (!taskId) return false;
      const pairResolved = record.pairIptmResolved === true || record.pair_iptm_resolved === true;
      const hasPairIptm = readFiniteNumber(record.pairIptm ?? record.pair_iptm) !== null;
      const hasPairPae = readFiniteNumber(record.pairPae ?? record.pair_pae) !== null;
      const hasLigandPlddt = readFiniteNumber(record.ligandPlddt ?? record.ligand_plddt) !== null;
      const hasLigandAtomSeries = readFiniteNumberArray(record.ligandAtomPlddts ?? record.ligand_atom_plddts).length > 0;
      return !(pairResolved && (hasPairIptm || hasPairPae || hasLigandPlddt || hasLigandAtomSeries));
    })
    .slice(0, 1);
  if (successEntries.length === 0) {
    return {
      nextMap: predictionMap,
      confidencePatch: null,
      structureNamePatch: '',
      changed: false
    };
  }

  const nextMap: Record<string, unknown> = { ...predictionMap };
  let changed = false;
  let structureNamePatch = '';
  for (const entry of successEntries) {
    const taskId = String(entry.record.taskId || entry.record.task_id || '').trim();
    if (!taskId) continue;
    try {
      const resultBlob = await downloadResultBlob(taskId, { mode: 'view' });
      const parsed = await parseResultBundle(resultBlob);
      if (!parsed) continue;

      const leadOptMmp = asRecord(asRecord(task.confidence).lead_opt_mmp);
      const targetChain = String(leadOptMmp.target_chain || '').trim();
      const ligandChain = String(leadOptMmp.ligand_chain || '').trim();
      const baseProperties = asRecord(task.properties);
      const syntheticTask: ProjectTask = {
        ...task,
        confidence: asRecord(parsed.confidence),
        affinity: asRecord(parsed.affinity),
        properties: {
          ...baseProperties,
          target: targetChain || (typeof baseProperties.target === 'string' ? baseProperties.target : null),
          ligand: ligandChain || (typeof baseProperties.ligand === 'string' ? baseProperties.ligand : null),
          binder: ligandChain || (typeof baseProperties.binder === 'string' ? baseProperties.binder : null)
        } as ProjectTask['properties']
      };
      const selection = resolveTaskSelectionContext(
        syntheticTask,
        {
          targetChainId: targetChain || null,
          ligandChainId: ligandChain || null
        },
        'lead_optimization'
      );
      const metrics = readTaskConfidenceMetrics(syntheticTask, selection);
      const ligandAtomPlddts =
        readTaskLigandAtomPlddts(syntheticTask, selection.ligandChainId, selection.ligandComponentCount <= 1) || [];
      const ligandPlddt = metrics.plddt !== null ? metrics.plddt : mean(ligandAtomPlddts);
      const updatedRecord = {
        ...entry.record,
        pairIptm: metrics.iptm,
        pairPae: metrics.pae,
        pairIptmResolved: true,
        ligandPlddt,
        ligandAtomPlddts,
        structureName: String(parsed.structureName || entry.record.structureName || entry.record.structure_name || '').trim(),
        error: '',
        updatedAt: Date.now()
      };
      nextMap[entry.smiles] = updatedRecord;
      changed = true;
      if (!String(task.structure_name || '').trim()) {
        structureNamePatch = String(parsed.structureName || '').trim();
      }
    } catch {
      // Keep retrying on later sync cycles if result artifact is not ready yet.
    }
  }

  if (!changed) {
    return {
      nextMap,
      confidencePatch: null,
      structureNamePatch: '',
      changed: false
    };
  }

  const nextConfidence = { ...asRecord(task.confidence) };
  const leadOptMmp = { ...asRecord(nextConfidence.lead_opt_mmp) };
  leadOptMmp.prediction_by_smiles = nextMap;
  nextConfidence.lead_opt_mmp = leadOptMmp;
  return {
    nextMap,
    confidencePatch: nextConfidence,
    structureNamePatch,
    changed: true
  };
}

export async function syncRuntimeTaskRows(projectRow: Project, taskRows: ProjectTask[]) {
  const safeTaskRows = sanitizeTaskRows(taskRows);
  let nextProject = projectRow;
  let nextTaskRows = [...safeTaskRows];

  const leadOptRows = safeTaskRows.filter((row) => {
    if (!Boolean(row.task_id)) return false;
    if (!readLeadOptTaskSummary(row)) return false;
    return row.task_state === 'QUEUED' || row.task_state === 'RUNNING' || !hasTaskSummaryMetrics(row);
  });
  for (const row of leadOptRows) {
    const baseSummary = readLeadOptTaskSummary(row);
    if (!baseSummary) continue;

    let workingRow: ProjectTask = row;
    let workingConfidence = asRecord(workingRow.confidence);
    const leadOptMmp = asRecord(workingConfidence.lead_opt_mmp);
    let predictionMap = asRecord(leadOptMmp.prediction_by_smiles);
    let leadOptChanged = false;
    let hydrationStructureNamePatch = '';

    if (Object.keys(predictionMap).length > 0) {
      const reconciled = await reconcileLeadOptPredictionMapStates(predictionMap);
      if (reconciled.changed) {
        predictionMap = reconciled.nextMap;
        leadOptChanged = true;
      }

      if (!hasTaskSummaryMetrics(workingRow)) {
        const hydrated = await hydrateLeadOptPredictionMetricsFromResult(workingRow, predictionMap);
        if (hydrated.changed) {
          predictionMap = hydrated.nextMap;
          leadOptChanged = true;
          if (hydrated.structureNamePatch) {
            hydrationStructureNamePatch = hydrated.structureNamePatch;
          }
        }
      }

      if (leadOptChanged) {
        const summaryCounts = summarizeLeadOptPredictionMap(predictionMap);
        const unresolved = summaryCounts.queued + summaryCounts.running;
        const nextStage =
          unresolved > 0
            ? summaryCounts.running > 0
              ? 'prediction_running'
              : 'prediction_queued'
            : summaryCounts.failure > 0 && summaryCounts.success === 0
              ? 'prediction_failed'
              : 'prediction_completed';
        const nextLeadOptMmp: Record<string, unknown> = {
          ...leadOptMmp,
          stage: nextStage,
          prediction_stage: unresolved > 0 ? (summaryCounts.running > 0 ? 'running' : 'queued') : 'completed',
          prediction_summary: {
            ...asRecord(leadOptMmp.prediction_summary),
            total: summaryCounts.total,
            queued: summaryCounts.queued,
            running: summaryCounts.running,
            success: summaryCounts.success,
            failure: summaryCounts.failure
          },
          bucket_count: summaryCounts.total,
          prediction_by_smiles: predictionMap
        };
        workingConfidence = {
          ...workingConfidence,
          lead_opt_mmp: nextLeadOptMmp
        };
        workingRow = {
          ...workingRow,
          confidence: workingConfidence
        };
      }
    }

    const summary = readLeadOptTaskSummary(workingRow) || baseSummary;
    const derived = deriveLeadOptRuntimeState(summary);
    if (!derived) continue;

    const terminal = derived.taskState === 'SUCCESS' || derived.taskState === 'FAILURE' || derived.taskState === 'REVOKED';
    const completedAt = terminal ? workingRow.completed_at || new Date().toISOString() : null;
    const submittedAt = workingRow.submitted_at || (nextProject.task_id === workingRow.task_id ? nextProject.submitted_at : null);
    const durationSeconds =
      terminal && submittedAt
        ? (() => {
            const duration = (new Date(completedAt || Date.now()).getTime() - new Date(submittedAt).getTime()) / 1000;
            return Number.isFinite(duration) && duration >= 0 ? duration : null;
          })()
        : null;
    const promoted = promoteLeadOptPredictionMetrics(workingRow);
    const mergedConfidencePatch = promoted.confidencePatch || (leadOptChanged ? workingConfidence : null);
    const mergedStructureNamePatch =
      promoted.structureNamePatch || hydrationStructureNamePatch || '';
    const taskPatch: Partial<ProjectTask> = {
      task_state: derived.taskState,
      status_text: derived.statusText,
      error_text: derived.errorText,
      completed_at: completedAt,
      duration_seconds: durationSeconds
    };
    if (mergedConfidencePatch) {
      taskPatch.confidence = mergedConfidencePatch;
    }
    if (mergedStructureNamePatch) {
      taskPatch.structure_name = mergedStructureNamePatch;
    }

    const taskNeedsPatch =
      workingRow.task_state !== derived.taskState ||
      (workingRow.status_text || '') !== derived.statusText ||
      (workingRow.error_text || '') !== derived.errorText ||
      workingRow.completed_at !== completedAt ||
      workingRow.duration_seconds !== durationSeconds ||
      Boolean(mergedConfidencePatch) ||
      Boolean(mergedStructureNamePatch);
    if (!taskNeedsPatch) continue;

    const fallbackTask: ProjectTask = {
      ...workingRow,
      ...taskPatch
    };
    const patchedTask = await updateProjectTask(workingRow.id, taskPatch).catch(() => fallbackTask);
    const nextTask = isProjectTaskRow(patchedTask) ? patchedTask : fallbackTask;
    nextTaskRows = nextTaskRows.map((item) => (item.id === workingRow.id ? nextTask : item));

    if (nextProject.task_id === workingRow.task_id) {
      const projectPatch: Partial<Project> = {
        task_state: derived.taskState,
        status_text: derived.statusText,
        error_text: derived.errorText,
        completed_at: completedAt,
        duration_seconds: durationSeconds
      };
      if (mergedConfidencePatch) {
        projectPatch.confidence = mergedConfidencePatch;
      }
      if (mergedStructureNamePatch) {
        projectPatch.structure_name = mergedStructureNamePatch;
      }
      const fallbackProject: Project = {
        ...nextProject,
        ...projectPatch
      };
      const patchedProject = await updateProject(nextProject.id, projectPatch).catch(() => fallbackProject);
      nextProject = isProjectRow(patchedProject) ? patchedProject : fallbackProject;
    }
  }

  const runtimeRows = nextTaskRows.filter(
    (row) =>
      Boolean(row.task_id) &&
      (row.task_state === 'QUEUED' || row.task_state === 'RUNNING') &&
      !readLeadOptTaskSummary(row)
  );
  if (runtimeRows.length === 0) {
    return {
      project: nextProject,
      taskRows: sortProjectTasks(sanitizeTaskRows(nextTaskRows))
    };
  }

  const checks = await Promise.allSettled(runtimeRows.map((row) => getTaskStatus(row.task_id)));

  for (let i = 0; i < checks.length; i += 1) {
    const result = checks[i];
    if (result.status !== 'fulfilled') continue;

    const runtimeTask = runtimeRows[i];
    const taskState = mapTaskState(result.value.state);
    const statusText = readStatusText(result.value);
    const errorText = taskState === 'FAILURE' ? statusText : '';
    const terminal = taskState === 'SUCCESS' || taskState === 'FAILURE' || taskState === 'REVOKED';
    const completedAt = terminal ? runtimeTask.completed_at || new Date().toISOString() : null;
    const submittedAt = runtimeTask.submitted_at || (nextProject.task_id === runtimeTask.task_id ? nextProject.submitted_at : null);
    const durationSeconds =
      terminal && submittedAt
        ? (() => {
            const duration = (new Date(completedAt || Date.now()).getTime() - new Date(submittedAt).getTime()) / 1000;
            return Number.isFinite(duration) && duration >= 0 ? duration : null;
          })()
        : null;

    const taskNeedsPatch =
      runtimeTask.task_state !== taskState ||
      (runtimeTask.status_text || '') !== statusText ||
      (runtimeTask.error_text || '') !== errorText ||
      runtimeTask.completed_at !== completedAt ||
      runtimeTask.duration_seconds !== durationSeconds;

    if (taskNeedsPatch) {
      const taskPatch: Partial<ProjectTask> = {
        task_state: taskState,
        status_text: statusText,
        error_text: errorText,
        completed_at: completedAt,
        duration_seconds: durationSeconds
      };
      const fallbackTask: ProjectTask = {
        ...runtimeTask,
        ...taskPatch
      };
      const patchedTask = await updateProjectTask(runtimeTask.id, taskPatch).catch(() => fallbackTask);
      const nextTask = isProjectTaskRow(patchedTask) ? patchedTask : fallbackTask;
      nextTaskRows = nextTaskRows.map((row) => (row.id === runtimeTask.id ? nextTask : row));
    }

    if (nextProject.task_id === runtimeTask.task_id) {
      const projectNeedsPatch =
        nextProject.task_state !== taskState ||
        (nextProject.status_text || '') !== statusText ||
        (nextProject.error_text || '') !== errorText ||
        nextProject.completed_at !== completedAt ||
        nextProject.duration_seconds !== durationSeconds;
      if (projectNeedsPatch) {
        const projectPatch: Partial<Project> = {
          task_state: taskState,
          status_text: statusText,
          error_text: errorText,
          completed_at: completedAt,
          duration_seconds: durationSeconds
        };
        const fallbackProject: Project = {
          ...nextProject,
          ...projectPatch
        };
        const patchedProject = await updateProject(nextProject.id, projectPatch).catch(() => fallbackProject);
        nextProject = isProjectRow(patchedProject) ? patchedProject : fallbackProject;
      }
    }
  }

  return {
    project: nextProject,
    taskRows: sortProjectTasks(sanitizeTaskRows(nextTaskRows))
  };
}

interface HydrationRefs {
  resultHydrationInFlightRef: MutableRefObject<Set<string>>;
  resultHydrationDoneRef: MutableRefObject<Set<string>>;
  resultHydrationAttemptsRef: MutableRefObject<Map<string, number>>;
}

export async function hydrateTaskMetricsFromResultRows(
  projectRow: Project,
  taskRows: ProjectTask[],
  refs: HydrationRefs
) {
  const { resultHydrationInFlightRef, resultHydrationDoneRef, resultHydrationAttemptsRef } = refs;
  const safeTaskRows = sanitizeTaskRows(taskRows);
  const candidates = safeTaskRows
    .filter((row) => {
      const taskId = String(row.task_id || '').trim();
      if (!taskId || row.task_state !== 'SUCCESS') return false;
      if (hasLeadOptMmpOnlySnapshot(row)) return false;
      const selection = resolveTaskSelectionContext(row, undefined, resolveTaskWorkflowKey(row, projectRow.task_type || ''));
      const confidence =
        row.confidence && typeof row.confidence === 'object' && !Array.isArray(row.confidence)
          ? (row.confidence as Record<string, unknown>)
          : null;
      const backendValue = resolveTaskBackendValue(row);
      const ligandByChain =
        confidence?.ligand_atom_plddts_by_chain &&
        typeof confidence.ligand_atom_plddts_by_chain === 'object' &&
        !Array.isArray(confidence.ligand_atom_plddts_by_chain)
          ? (confidence.ligand_atom_plddts_by_chain as Record<string, unknown>)
          : null;
      const hasLigandByChain = Boolean(ligandByChain && Object.keys(ligandByChain).length > 0);
      const residueByChain =
        confidence?.residue_plddt_by_chain &&
        typeof confidence.residue_plddt_by_chain === 'object' &&
        !Array.isArray(confidence.residue_plddt_by_chain)
          ? (confidence.residue_plddt_by_chain as Record<string, unknown>)
          : null;
      const hasResidueByChain = Boolean(residueByChain && Object.keys(residueByChain).length > 0);
      const needsSummaryHydration = !hasTaskSummaryMetrics(row);
      const needsLigandAtomHydration =
        Boolean(
          selection.ligandSmiles &&
            selection.ligandIsSmiles &&
            !hasTaskLigandAtomPlddts(row, selection.ligandChainId, selection.ligandComponentCount <= 1)
        );
      const needsProtenixDetailHydration =
        backendValue === 'protenix' && (!hasLigandByChain || !hasResidueByChain);
      if (!needsSummaryHydration && !needsLigandAtomHydration && !needsProtenixDetailHydration) {
        resultHydrationDoneRef.current.add(taskId);
        return false;
      }
      if (resultHydrationDoneRef.current.has(taskId)) return false;
      if (resultHydrationInFlightRef.current.has(taskId)) return false;
      const attempts = resultHydrationAttemptsRef.current.get(taskId) || 0;
      return attempts < 2;
    })
    .slice(0, 2);

  if (candidates.length === 0) {
    return {
      project: projectRow,
      taskRows: safeTaskRows
    };
  }

  let nextProject = projectRow;
  let nextTaskRows = [...safeTaskRows];

  for (const task of candidates) {
    const taskId = String(task.task_id || '').trim();
    if (!taskId) continue;
    const attempts = resultHydrationAttemptsRef.current.get(taskId) || 0;
    resultHydrationAttemptsRef.current.set(taskId, attempts + 1);
    resultHydrationInFlightRef.current.add(taskId);

    try {
      const resultBlob = await downloadResultBlob(taskId, { mode: 'view' });
      const parsed = await parseResultBundle(resultBlob);
      if (!parsed) continue;

      const taskPatch: Partial<ProjectTask> = {
        confidence: parsed.confidence || {},
        affinity: parsed.affinity || {},
        structure_name: parsed.structureName || task.structure_name || ''
      };
      const fallbackTask: ProjectTask = {
        ...task,
        ...taskPatch
      };
      const patchedTask = await updateProjectTask(task.id, taskPatch).catch(() => fallbackTask);
      const nextTask = isProjectTaskRow(patchedTask) ? patchedTask : fallbackTask;
      nextTaskRows = nextTaskRows.map((row) => (row.id === task.id ? nextTask : row));

      if (nextProject.task_id === taskId) {
        const projectPatch: Partial<Project> = {
          confidence: taskPatch.confidence || {},
          affinity: taskPatch.affinity || {},
          structure_name: taskPatch.structure_name || ''
        };
        const fallbackProject: Project = {
          ...nextProject,
          ...projectPatch
        };
        const patchedProject = await updateProject(nextProject.id, projectPatch).catch(() => fallbackProject);
        nextProject = isProjectRow(patchedProject) ? patchedProject : fallbackProject;
      }

      resultHydrationDoneRef.current.add(taskId);
    } catch {
      // Ignore transient parse/download failures; retry is bounded by attempt count.
    } finally {
      resultHydrationInFlightRef.current.delete(taskId);
    }
  }

  return {
    project: nextProject,
    taskRows: sortProjectTasks(sanitizeTaskRows(nextTaskRows))
  };
}
