import { ExternalLink, LoaderCircle, Square, Trash2 } from 'lucide-react';
import { Ligand2DPreview } from '../../components/project/Ligand2DPreview';
import type { InputComponent, ProjectTask } from '../../types/models';
import { formatDateTime, formatDuration } from '../../utils/date';
import { TaskLigandSequencePreview } from './TaskLigandSequencePreview';
import type { TaskListRow } from './taskListTypes';
import { inferTaskStateFromStatusPayload } from './taskRuntimeUiUtils';
import {
  backendLabel,
  formatMetric,
  shouldShowRunNote,
  taskStateLabel,
  taskStateTone,
  toneForIptm,
  toneForPae,
  toneForPlddt
} from './taskPresentation';

function isSequenceLigandType(type: InputComponent['type'] | null): boolean {
  return type === 'protein' || type === 'dna' || type === 'rna';
}

interface ProjectTaskRowProps {
  row: TaskListRow;
  mode: 'default' | 'lead_opt' | 'peptide';
  editingTaskNameId: string | null;
  editingTaskNameValue: string;
  savingTaskNameId: string | null;
  openingTaskId: string | null;
  deletingTaskId: string | null;
  terminatingTaskId: string | null;
  onOpenTask: (task: ProjectTask) => Promise<void> | void;
  onTerminateTask: (task: ProjectTask) => Promise<void> | void;
  onRemoveTask: (task: ProjectTask) => Promise<void> | void;
  onBeginTaskNameEdit: (task: ProjectTask, displayName: string) => void;
  onCancelTaskNameEdit: () => void;
  onSaveTaskNameEdit: (task: ProjectTask, displayName: string) => Promise<void> | void;
  onEditingTaskNameValueChange: (value: string) => void;
}

function formatPeptideBestScore(value: number | null): string {
  if (value === null) return '-';
  return value.toFixed(3);
}

function formatPeptideMutationRate(value: number | null): string {
  if (value === null) return '-';
  const normalized = value > 1 && value <= 100 ? value / 100 : value;
  return `${Math.max(0, normalized * 100).toFixed(0)}%`;
}

export function ProjectTaskRow({
  row,
  mode,
  editingTaskNameId,
  editingTaskNameValue,
  savingTaskNameId,
  openingTaskId,
  deletingTaskId,
  terminatingTaskId,
  onOpenTask,
  onTerminateTask,
  onRemoveTask,
  onBeginTaskNameEdit,
  onCancelTaskNameEdit,
  onSaveTaskNameEdit,
  onEditingTaskNameValueChange
}: ProjectTaskRowProps) {
  const { task, metrics } = row;
  const runNote = (task.status_text || '').trim();
  const taskName = String(task.name || '').trim() || `Task ${String(task.id || '').slice(0, 8)}`;
  const isEditingTaskName = editingTaskNameId === task.id;
  const isSavingTaskName = savingTaskNameId === task.id;
  const taskSummary = String(task.summary || '').trim();
  const showRunNote = shouldShowRunNote(task.task_state, runNote);
  const submittedTs = task.submitted_at || task.created_at;
  const hasRuntimeTaskId = Boolean(String(task.task_id || '').trim());
  const runtimeActionState = inferTaskStateFromStatusPayload(
    { state: String(task.task_state || ''), info: { status: String(task.status_text || '') } },
    task.task_state
  );
  const normalizedTaskState = String(task.task_state || '').trim().toUpperCase();
  const isTerminalState =
    normalizedTaskState === 'SUCCESS' || normalizedTaskState === 'FAILURE' || normalizedTaskState === 'REVOKED';
  const showTerminateHint = !hasRuntimeTaskId && (runtimeActionState === 'QUEUED' || runtimeActionState === 'RUNNING');
  const canTerminateTask = hasRuntimeTaskId && !isTerminalState;
  const terminatingThisTask = terminatingTaskId === task.id;
  const actionTitle = hasRuntimeTaskId ? 'Open this task result' : 'Open this draft snapshot for editing';
  const stateTone = taskStateTone(task.task_state);
  const plddtTone = toneForPlddt(metrics.plddt);
  const iptmTone = toneForIptm(metrics.iptm);
  const paeTone = toneForPae(metrics.pae);
  const workflowClass = row.workflowKey.replace(/_/g, '-');
  const isLeadOptMode = mode === 'lead_opt';
  const isPeptideMode = mode === 'peptide';
  const ligandPreviewWidth = isPeptideMode ? 248 : isLeadOptMode ? 184 : 312;
  const hasCompletedMmp =
    isLeadOptMode &&
    (row.leadOptTransformCount !== null || row.leadOptCandidateCount !== null || row.leadOptBucketCount !== null);
  const mmpTransforms = hasCompletedMmp && row.leadOptTransformCount !== null ? row.leadOptTransformCount : null;
  const mmpCandidates = hasCompletedMmp && row.leadOptCandidateCount !== null ? row.leadOptCandidateCount : null;
  const mmpBuckets = hasCompletedMmp && row.leadOptBucketCount !== null ? row.leadOptBucketCount : null;
  const mmpStats = (() => {
    if (!hasCompletedMmp) return [] as Array<{ key: string; label: string; value: number }>;
    const items: Array<{ key: string; label: string; value: number }> = [];
    if (mmpTransforms !== null) {
      items.push({ key: 'transforms', label: 'Transforms', value: mmpTransforms });
    }
    if (mmpCandidates !== null && mmpCandidates !== mmpTransforms) {
      items.push({ key: 'candidates', label: 'Candidates', value: mmpCandidates });
    }
    if (mmpBuckets !== null) {
      items.push({ key: 'buckets', label: 'Buckets', value: mmpBuckets });
    }
    return items;
  })();
  const peptideDesignItems = [
    { key: 'iter', label: 'Iter', value: row.peptideIterations !== null ? String(row.peptideIterations) : '-' },
    { key: 'pop', label: 'Pop', value: row.peptidePopulationSize !== null ? String(row.peptidePopulationSize) : '-' },
    { key: 'elite', label: 'Elite', value: row.peptideEliteSize !== null ? String(row.peptideEliteSize) : '-' },
    { key: 'mut', label: 'Mut', value: formatPeptideMutationRate(row.peptideMutationRate) }
  ];
  const peptideProgressItems = [
    {
      key: 'gen',
      label: 'Gen',
      value:
        row.peptideCurrentGeneration !== null
          ? row.peptideTotalGenerations !== null
            ? `${row.peptideCurrentGeneration}/${row.peptideTotalGenerations}`
            : String(row.peptideCurrentGeneration)
          : '-'
    },
    { key: 'best', label: 'Best', value: formatPeptideBestScore(row.peptideBestScore) },
    { key: 'cand', label: 'Cand', value: row.peptideCandidateCount !== null ? String(row.peptideCandidateCount) : '-' },
    {
      key: 'tasks',
      label: 'Tasks',
      value:
        row.peptideTotalTasks !== null
          ? `${row.peptideCompletedTasks ?? 0}/${row.peptideTotalTasks}`
          : row.peptideCompletedTasks !== null || row.peptidePendingTasks !== null
            ? `${row.peptideCompletedTasks ?? 0}/${(row.peptideCompletedTasks ?? 0) + (row.peptidePendingTasks ?? 0)}`
            : '-'
    }
  ];

  return (
    <tr key={task.id}>
      <td className="task-col-ligand">
        <button
          type="button"
          className="task-ligand-open-btn"
          onClick={() => void onOpenTask(task)}
          disabled={openingTaskId === task.id}
          title={actionTitle}
          aria-label={actionTitle}
        >
          {row.ligandSmiles && row.ligandIsSmiles ? (
            <div className="task-ligand-thumb">
              <Ligand2DPreview
                smiles={row.ligandSmiles}
                width={ligandPreviewWidth}
                height={120}
                atomConfidences={row.ligandAtomPlddts}
                confidenceHint={metrics.plddt}
                highlightQuery={isLeadOptMode ? row.leadOptSelectedFragmentQuery || null : null}
                highlightAtomIndices={isLeadOptMode ? row.leadOptSelectedAtomIndices : null}
              />
            </div>
          ) : row.ligandSequence && isSequenceLigandType(row.ligandSequenceType) ? (
            <TaskLigandSequencePreview sequence={row.ligandSequence} residuePlddts={row.ligandResiduePlddts} />
          ) : (
            <div className="task-ligand-thumb task-ligand-thumb-empty">
              <span className="muted small">No ligand</span>
            </div>
          )}
        </button>
      </td>
      {isLeadOptMode ? (
        <td className="task-col-mmp">
          <div className="task-mmp-cell">
            <div className="task-mmp-inline" aria-label="MMP statistics">
              {mmpStats.length > 0 ? (
                mmpStats.map((item) => (
                  <span key={item.key} className="task-mmp-inline-item">
                    <span className="task-mmp-inline-key">{item.label}</span>
                    <span className="task-mmp-inline-value">{item.value}</span>
                  </span>
                ))
              ) : (
                <span className="task-mmp-empty">-</span>
              )}
            </div>
          </div>
        </td>
      ) : null}
      {isLeadOptMode ? (
        <td className="task-col-leadopt-db">
          {row.leadOptDatabaseLabel || row.leadOptDatabaseSchema || row.leadOptDatabaseId ? (
            <div className="task-leadopt-db-cell">
              <span className="task-leadopt-db-name">
                {row.leadOptDatabaseLabel || row.leadOptDatabaseSchema || row.leadOptDatabaseId}
              </span>
              {row.leadOptDatabaseSchema &&
              row.leadOptDatabaseSchema !== (row.leadOptDatabaseLabel || row.leadOptDatabaseSchema) ? (
                <span className="task-leadopt-db-schema">{row.leadOptDatabaseSchema}</span>
              ) : null}
            </div>
          ) : (
            <span className="task-mmp-empty">-</span>
          )}
        </td>
      ) : isPeptideMode ? (
        <>
          <td className="task-col-peptide-setup">
            <div className="task-peptide-cell">
              <div className="task-peptide-inline" aria-label="Peptide design setup">
                {peptideDesignItems.map((item) => (
                  <span key={item.key} className="task-peptide-inline-item">
                    <span className="task-peptide-inline-key">{item.label}</span>
                    <span className="task-peptide-inline-value">{item.value}</span>
                  </span>
                ))}
              </div>
            </div>
          </td>
          <td className="task-col-peptide-progress">
            <div className="task-peptide-cell">
              <div className="task-peptide-inline" aria-label="Peptide iteration progress">
                {peptideProgressItems.map((item) => (
                  <span key={item.key} className="task-peptide-inline-item">
                    <span className="task-peptide-inline-key">{item.label}</span>
                    <span className="task-peptide-inline-value">{item.value}</span>
                  </span>
                ))}
              </div>
              {row.peptideStatusMessage ? <div className="task-peptide-note">{row.peptideStatusMessage}</div> : null}
            </div>
          </td>
        </>
      ) : (
        <>
          <td className="task-col-metric">
            <span className={`task-metric-value metric-value-${plddtTone}`}>{formatMetric(metrics.plddt, 1)}</span>
          </td>
          <td className="task-col-metric">
            <span className={`task-metric-value metric-value-${iptmTone}`}>{formatMetric(metrics.iptm, 3)}</span>
          </td>
          <td className="task-col-metric">
            <span className={`task-metric-value metric-value-${paeTone}`}>{formatMetric(metrics.pae, 2)}</span>
          </td>
        </>
      )}
      <td className="project-col-time task-col-submitted">
        <div className="task-submitted-cell">
          {isEditingTaskName ? (
            <input
              className="task-submitted-title-input"
              value={editingTaskNameValue}
              onChange={(event) => onEditingTaskNameValueChange(event.target.value)}
              onBlur={() => void onSaveTaskNameEdit(task, taskName)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault();
                  onCancelTaskNameEdit();
                  return;
                }
                if (event.key === 'Enter') {
                  event.preventDefault();
                  void onSaveTaskNameEdit(task, taskName);
                }
              }}
              placeholder={`Task ${String(task.id || '').slice(0, 8)}`}
              disabled={isSavingTaskName}
              autoFocus
            />
          ) : (
            <button
              type="button"
              className="task-submitted-title task-submitted-title-btn"
              onClick={() => onBeginTaskNameEdit(task, taskName)}
              disabled={Boolean(savingTaskNameId)}
              title="Edit task name"
            >
              {taskName}
              {isSavingTaskName ? <LoaderCircle size={11} className="spin" /> : null}
            </button>
          )}
          {taskSummary ? <div className="task-submitted-summary">{taskSummary}</div> : null}
          <div className="task-submitted-main">
            <span className={`task-state-chip ${stateTone}`}>{taskStateLabel(task.task_state)}</span>
            <span className={`task-workflow-chip workflow-${workflowClass}`}>{row.workflowLabel}</span>
            <span className="task-submitted-time">{formatDateTime(submittedTs)}</span>
          </div>
          {showRunNote ? <div className={`task-run-note is-${stateTone}`}>{runNote}</div> : null}
        </div>
      </td>
      {mode === 'default' ? (
        <td className="task-col-backend">
          <span className="badge task-backend-badge">{backendLabel(row.backendValue)}</span>
        </td>
      ) : null}
      {mode === 'default' ? <td className="task-col-seed">{task.seed ?? '-'}</td> : null}
      {mode === 'default' ? <td className="project-col-time task-col-duration">{formatDuration(task.duration_seconds)}</td> : null}
      <td className="project-col-actions">
        <div className="row gap-6 project-action-row">
          <button
            type="button"
            className="task-row-action-btn"
            onClick={() => void onOpenTask(task)}
            disabled={openingTaskId === task.id || terminatingThisTask}
            title={actionTitle}
            aria-label={actionTitle}
          >
            {openingTaskId === task.id ? <LoaderCircle size={13} className="spin" /> : <ExternalLink size={14} />}
          </button>
          {canTerminateTask || showTerminateHint ? (
            <button
              type="button"
              className="task-row-action-btn"
              onClick={() => void onTerminateTask(task)}
              disabled={!canTerminateTask || terminatingThisTask || deletingTaskId === task.id}
              title={
                !canTerminateTask
                  ? 'Task is active but runtime task ID is missing'
                  : runtimeActionState === 'RUNNING'
                    ? 'Stop running task'
                    : runtimeActionState === 'QUEUED'
                      ? 'Cancel queued task'
                      : 'Cancel active task'
              }
              aria-label={
                runtimeActionState === 'RUNNING'
                  ? 'Stop running task'
                  : runtimeActionState === 'QUEUED'
                    ? 'Cancel queued task'
                    : 'Cancel active task'
              }
            >
              {terminatingThisTask ? <LoaderCircle size={13} className="spin" /> : <Square size={13} />}
            </button>
          ) : null}
          <button
            type="button"
            className="task-row-action-btn danger"
            onClick={() => void onRemoveTask(task)}
            disabled={deletingTaskId === task.id || terminatingThisTask}
            title="Delete task"
            aria-label="Delete task"
          >
            {deletingTaskId === task.id ? <LoaderCircle size={13} className="spin" /> : <Trash2 size={14} />}
          </button>
        </div>
      </td>
    </tr>
  );
}
