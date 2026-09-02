import { Clock3, ExternalLink, Square, Trash2 } from 'lucide-react';
import type { ProjectTask } from '../../types/models';
import { formatDateTime } from '../../utils/date';
import { canEditTask } from '../../utils/accessControl';
import { ProjectTaskRow } from './ProjectTaskRow';
import { taskStateLabel, taskStateTone } from './taskPresentation';
import type { SortKey, TaskListRow, TaskMetricColumnKey, TaskTableMode } from './taskListTypes';

const METRIC_COLUMN_LABELS: Record<TaskMetricColumnKey, string> = {
  plddt: 'pLDDT',
  ipsae: 'IPSAE',
  iptm: 'ipTM',
  pae: 'PAE'
};

interface ProjectTasksTableProps {
  totalRowCount: number;
  canManageShares: boolean;
  filteredCount: number;
  tableMode: TaskTableMode;
  visibleMetricColumns: TaskMetricColumnKey[];
  sortKey: SortKey;
  sortMark: (key: SortKey) => string;
  onSort: (key: SortKey) => void;
  pagedRows: TaskListRow[];
  editingTaskNameId: string | null;
  editingTaskNameValue: string;
  savingTaskNameId: string | null;
  openingTaskId: string | null;
  deletingTaskId: string | null;
  terminatingTaskId: string | null;
  onOpenTask: (task: ProjectTask) => void;
  onTerminateTask: (task: ProjectTask) => void;
  onRemoveTask: (task: ProjectTask) => void;
  onOpenShareTask: (task: ProjectTask) => void;
  onBeginTaskNameEdit: (task: ProjectTask, displayName: string) => void;
  onCancelTaskNameEdit: () => void;
  onSaveTaskNameEdit: (task: ProjectTask, displayName: string) => void;
  onEditingTaskNameValueChange: (value: string) => void;
  currentPage: number;
  totalPages: number;
  pageSize: number;
  onPageSizeChange: (value: number) => void;
  onPageChange: (updater: number | ((prev: number) => number)) => void;
  onJumpToPage: (value: string) => void;
}

export function ProjectTasksTable({
  totalRowCount,
  canManageShares,
  filteredCount,
  tableMode,
  visibleMetricColumns,
  sortKey,
  sortMark,
  onSort,
  pagedRows,
  editingTaskNameId,
  editingTaskNameValue,
  savingTaskNameId,
  openingTaskId,
  deletingTaskId,
  terminatingTaskId,
  onOpenTask,
  onTerminateTask,
  onRemoveTask,
  onOpenShareTask,
  onBeginTaskNameEdit,
  onCancelTaskNameEdit,
  onSaveTaskNameEdit,
  onEditingTaskNameValueChange,
  currentPage,
  totalPages,
  pageSize,
  onPageSizeChange,
  onPageChange,
  onJumpToPage
}: ProjectTasksTableProps) {
  const isLeadOptMode = tableMode === 'lead_opt';
  const isPeptideMode = tableMode === 'peptide';
  const tableClass = `table project-table task-table${
    isLeadOptMode ? ' task-table--leadopt' : isPeptideMode ? ' task-table--peptide' : ''
  }`;

  return (
    <>
      {filteredCount === 0 ? (
        <div className="empty-state">{totalRowCount > 0 ? 'No tasks match current filters.' : 'No task runs yet.'}</div>
      ) : (
        <div className="table-wrap project-table-wrap task-table-wrap">
          <table className={tableClass}>
            <thead>
              <tr>
                <th>
                  <span className="project-th">Ligand View</span>
                </th>
                {isLeadOptMode ? (
                  <>
                    <th>
                      <span className="project-th">MMP Stats</span>
                    </th>
                    <th>
                      <span className="project-th">Database</span>
                    </th>
                  </>
                ) : isPeptideMode ? (
                  <>
                    <th>
                      <span className="project-th">Design Setup</span>
                    </th>
                    <th className="task-th-metric task-th-metric-plddt">
                      <button
                        type="button"
                        className={`task-th-sort ${sortKey === 'plddt' ? 'active' : ''}`}
                        onClick={() => onSort('plddt')}
                      >
                        <span className="project-th">
                          pLDDT <span className="task-th-arrow">{sortMark('plddt')}</span>
                        </span>
                      </button>
                    </th>
                    <th className="task-th-metric task-th-metric-ipsae">
                      <button
                        type="button"
                        className={`task-th-sort ${sortKey === 'ipsae' ? 'active' : ''}`}
                        onClick={() => onSort('ipsae')}
                      >
                        <span className="project-th">
                          IPSAE <span className="task-th-arrow">{sortMark('ipsae')}</span>
                        </span>
                      </button>
                    </th>
                  </>
                ) : (
                  <>
                    {visibleMetricColumns.map((metricKey) => (
                      <th key={metricKey} className={`task-th-metric task-th-metric-${metricKey}`}>
                        <button
                          type="button"
                          className={`task-th-sort ${sortKey === metricKey ? 'active' : ''}`}
                          onClick={() => onSort(metricKey)}
                        >
                          <span className="project-th">
                            {METRIC_COLUMN_LABELS[metricKey]} <span className="task-th-arrow">{sortMark(metricKey)}</span>
                          </span>
                        </button>
                      </th>
                    ))}
                  </>
                )}
                <th className="task-th-submitted">
                  <button type="button" className={`task-th-sort ${sortKey === 'submitted' ? 'active' : ''}`} onClick={() => onSort('submitted')}>
                    <span className="project-th"><Clock3 size={13} /> Submitted <span className="task-th-arrow">{sortMark('submitted')}</span></span>
                  </button>
                </th>
                {!isLeadOptMode && !isPeptideMode && (
                  <th className="task-th-backend">
                    <button type="button" className={`task-th-sort ${sortKey === 'backend' ? 'active' : ''}`} onClick={() => onSort('backend')}>
                      <span className="project-th">Backend <span className="task-th-arrow">{sortMark('backend')}</span></span>
                    </button>
                  </th>
                )}
                {!isLeadOptMode && !isPeptideMode && (
                  <th className="task-th-seed">
                    <button type="button" className={`task-th-sort ${sortKey === 'seed' ? 'active' : ''}`} onClick={() => onSort('seed')}>
                      <span className="project-th">Seed <span className="task-th-arrow">{sortMark('seed')}</span></span>
                    </button>
                  </th>
                )}
                {!isLeadOptMode && !isPeptideMode && (
                  <th className="task-th-mode">
                    <button type="button" className={`task-th-sort ${sortKey === 'mode' ? 'active' : ''}`} onClick={() => onSort('mode')}>
                      <span className="project-th">Mode <span className="task-th-arrow">{sortMark('mode')}</span></span>
                    </button>
                  </th>
                )}
                <th>
                  <span className="project-th">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {pagedRows.map((row) => (
                <ProjectTaskRow
                  key={row.task.id}
                  row={row}
                  mode={tableMode}
                  visibleMetricColumns={visibleMetricColumns}
                  canManageShares={canManageShares}
                  editingTaskNameId={editingTaskNameId}
                  editingTaskNameValue={editingTaskNameValue}
                  savingTaskNameId={savingTaskNameId}
                  openingTaskId={openingTaskId}
                  deletingTaskId={deletingTaskId}
                  terminatingTaskId={terminatingTaskId}
                  onOpenTask={onOpenTask}
                  onTerminateTask={onTerminateTask}
                  onRemoveTask={onRemoveTask}
                  onOpenShareTask={onOpenShareTask}
                  onBeginTaskNameEdit={onBeginTaskNameEdit}
                  onCancelTaskNameEdit={onCancelTaskNameEdit}
                  onSaveTaskNameEdit={onSaveTaskNameEdit}
                  onEditingTaskNameValueChange={onEditingTaskNameValueChange}
                />
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Mobile task card list — same data, card layout for small screens (CSS toggles visibility). */}
      {filteredCount > 0 ? (
        <div className="task-card-list" aria-label="Tasks">
          {pagedRows.map((row) => {
            const { task } = row;
            const canEdit = canEditTask(task);
            const taskName = String(task.name || '').trim() || `Task ${String(task.task_id || task.id || '').slice(0, 8)}`;
            const stateLabel = taskStateLabel(task.task_state);
            const stateTone = taskStateTone(task.task_state);
            const submittedTs = task.submitted_at || task.created_at;
            const normalizedTaskState = String(task.task_state || '').trim().toUpperCase();
            const isTerminalState = normalizedTaskState === 'SUCCESS' || normalizedTaskState === 'FAILURE' || normalizedTaskState === 'REVOKED';
            const hasRuntimeTaskId = Boolean(String(task.task_id || '').trim());
            const canTerminateTask = hasRuntimeTaskId && !isTerminalState;
            return (
              <div className="task-card" key={task.id}>
                <div className="task-card-head">
                  <button
                    type="button"
                    className="task-card-name"
                    onClick={() => void onOpenTask(task)}
                    title="Open task"
                  >
                    {taskName}
                  </button>
                  <span className={`task-card-state tone-${stateTone}`}>{stateLabel}</span>
                </div>
                {task.summary ? <p className="task-card-summary muted">{task.summary}</p> : null}
                <div className="task-card-meta">
                  {submittedTs ? <span className="muted"><Clock3 size={11} /> {formatDateTime(submittedTs)}</span> : null}
                </div>
                <div className="task-card-actions">
                  <button type="button" className="btn btn-ghost btn-compact" onClick={() => void onOpenTask(task)}>
                    <ExternalLink size={14} /> Open
                  </button>
                  {canTerminateTask ? (
                    <button
                      type="button"
                      className="btn btn-ghost btn-compact"
                      onClick={() => void onTerminateTask(task)}
                      disabled={Boolean(terminatingTaskId)}
                    >
                      <Square size={13} /> Cancel
                    </button>
                  ) : null}
                  {canEdit ? (
                    <button
                      type="button"
                      className="btn btn-ghost btn-compact danger"
                      onClick={() => void onRemoveTask(task)}
                      disabled={Boolean(deletingTaskId)}
                    >
                      <Trash2 size={14} /> Delete
                    </button>
                  ) : null}
                </div>
              </div>
            );
          })}
        </div>
      ) : null}

      {filteredCount > 0 && (
        <div className="project-pagination">
          <div className="project-pagination-info muted small">
            Page {currentPage} / {totalPages}
          </div>
          <div className="project-pagination-controls">
            <label className="project-page-size">
              <span className="muted small">Per page</span>
              <select value={String(pageSize)} onChange={(e) => onPageSizeChange(Math.max(1, Number(e.target.value) || 12))}>
                <option value="8">8</option>
                <option value="12">12</option>
                <option value="20">20</option>
                <option value="50">50</option>
              </select>
            </label>
            <button className="btn btn-ghost btn-compact" disabled={currentPage <= 1} onClick={() => onPageChange(1)}>
              First
            </button>
            <button className="btn btn-ghost btn-compact" disabled={currentPage <= 1} onClick={() => onPageChange((p) => Math.max(1, p - 1))}>
              Prev
            </button>
            <button
              className="btn btn-ghost btn-compact"
              disabled={currentPage >= totalPages}
              onClick={() => onPageChange((p) => Math.min(totalPages, p + 1))}
            >
              Next
            </button>
            <button className="btn btn-ghost btn-compact" disabled={currentPage >= totalPages} onClick={() => onPageChange(totalPages)}>
              Last
            </button>
            <label className="project-page-size">
              <span className="muted small">Go to</span>
              <input
                type="number"
                min={1}
                max={totalPages}
                value={String(currentPage)}
                onChange={(e) => onJumpToPage(e.target.value)}
                aria-label="Go to tasks page"
              />
            </label>
          </div>
        </div>
      )}
    </>
  );
}
