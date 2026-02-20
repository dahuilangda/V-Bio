import type { MutableRefObject } from 'react';
import { downloadResultBlob, getTaskStatus, parseResultBundle } from '../../api/backendApi';
import { updateProject, updateProjectTask } from '../../api/supabaseLite';
import type { Project, ProjectTask } from '../../types/models';
import {
  hasLeadOptPredictionRuntime,
  hasTaskLigandAtomPlddts,
  hasTaskSummaryMetrics,
  isProjectRow,
  isProjectTaskRow,
  mapTaskState,
  readStatusText,
  resolveTaskBackendValue,
  resolveTaskSelectionContext,
  sanitizeTaskRows,
  sortProjectTasks
} from './taskDataUtils';

function hasLeadOptMmpOnlySnapshot(task: ProjectTask): boolean {
  if (String(task.structure_name || '').trim().length > 0) return false;
  const confidence =
    task && task.confidence && typeof task.confidence === 'object'
      ? (task.confidence as Record<string, unknown>)
      : {};
  const leadOptMmp = confidence.lead_opt_mmp;
  if (leadOptMmp && typeof leadOptMmp === 'object') return true;
  return String(task.status_text || '').toUpperCase().includes('MMP');
}

export async function syncRuntimeTaskRows(projectRow: Project, taskRows: ProjectTask[]) {
  const safeTaskRows = sanitizeTaskRows(taskRows);
  const runtimeRows = safeTaskRows.filter(
    (row) =>
      Boolean(row.task_id) &&
      (row.task_state === 'QUEUED' || row.task_state === 'RUNNING') &&
      !hasLeadOptPredictionRuntime(row)
  );
  if (runtimeRows.length === 0) {
    return {
      project: projectRow,
      taskRows: sortProjectTasks(safeTaskRows)
    };
  }

  const checks = await Promise.allSettled(runtimeRows.map((row) => getTaskStatus(row.task_id)));
  let nextProject = projectRow;
  let nextTaskRows = [...safeTaskRows];

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
      const selection = resolveTaskSelectionContext(row);
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
