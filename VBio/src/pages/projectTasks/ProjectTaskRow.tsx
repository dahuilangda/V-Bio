import { ExternalLink, LoaderCircle, Trash2 } from 'lucide-react';
import { Ligand2DPreview } from '../../components/project/Ligand2DPreview';
import type { InputComponent, ProjectTask } from '../../types/models';
import { formatDateTime, formatDuration } from '../../utils/date';
import { TaskLigandSequencePreview } from './TaskLigandSequencePreview';
import type { TaskListRow } from './taskListTypes';
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
  mode: 'default' | 'lead_opt';
  editingTaskNameId: string | null;
  editingTaskNameValue: string;
  savingTaskNameId: string | null;
  openingTaskId: string | null;
  deletingTaskId: string | null;
  onOpenTask: (task: ProjectTask) => Promise<void> | void;
  onRemoveTask: (task: ProjectTask) => Promise<void> | void;
  onBeginTaskNameEdit: (task: ProjectTask, displayName: string) => void;
  onCancelTaskNameEdit: () => void;
  onSaveTaskNameEdit: (task: ProjectTask, displayName: string) => Promise<void> | void;
  onEditingTaskNameValueChange: (value: string) => void;
}

export function ProjectTaskRow({
  row,
  mode,
  editingTaskNameId,
  editingTaskNameValue,
  savingTaskNameId,
  openingTaskId,
  deletingTaskId,
  onOpenTask,
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
  const actionTitle = hasRuntimeTaskId ? 'Open this task result' : 'Open this draft snapshot for editing';
  const stateTone = taskStateTone(task.task_state);
  const plddtTone = toneForPlddt(metrics.plddt);
  const iptmTone = toneForIptm(metrics.iptm);
  const paeTone = toneForPae(metrics.pae);
  const workflowClass = row.workflowKey.replace(/_/g, '-');
  const isLeadOptMode = mode === 'lead_opt';
  const hasCompletedMmp = isLeadOptMode && String(task.task_state || '').toUpperCase() === 'SUCCESS';
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
                width={184}
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
      {!isLeadOptMode ? (
        <td className="task-col-backend">
          <span className="badge task-backend-badge">{backendLabel(row.backendValue)}</span>
        </td>
      ) : null}
      {!isLeadOptMode ? <td className="task-col-seed">{task.seed ?? '-'}</td> : null}
      {!isLeadOptMode ? <td className="project-col-time">{formatDuration(task.duration_seconds)}</td> : null}
      <td className="project-col-actions">
        <div className="row gap-6 project-action-row">
          <button
            type="button"
            className="task-row-action-btn"
            onClick={() => void onOpenTask(task)}
            disabled={openingTaskId === task.id}
            title={actionTitle}
            aria-label={actionTitle}
          >
            {openingTaskId === task.id ? <LoaderCircle size={13} className="spin" /> : <ExternalLink size={14} />}
          </button>
          <button
            type="button"
            className="task-row-action-btn danger"
            onClick={() => void onRemoveTask(task)}
            disabled={deletingTaskId === task.id}
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
