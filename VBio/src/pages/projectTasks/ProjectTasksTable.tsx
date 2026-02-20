import { Clock3 } from 'lucide-react';
import type { ProjectTask } from '../../types/models';
import { ProjectTaskRow } from './ProjectTaskRow';
import type { SortKey, TaskListRow } from './taskListTypes';

interface ProjectTasksTableProps {
  filteredCount: number;
  leadOptOnlyView: boolean;
  sortKey: SortKey;
  sortMark: (key: SortKey) => string;
  onSort: (key: SortKey) => void;
  pagedRows: TaskListRow[];
  editingTaskNameId: string | null;
  editingTaskNameValue: string;
  savingTaskNameId: string | null;
  openingTaskId: string | null;
  deletingTaskId: string | null;
  onOpenTask: (task: ProjectTask) => void;
  onRemoveTask: (task: ProjectTask) => void;
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
  filteredCount,
  leadOptOnlyView,
  sortKey,
  sortMark,
  onSort,
  pagedRows,
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
  onEditingTaskNameValueChange,
  currentPage,
  totalPages,
  pageSize,
  onPageSizeChange,
  onPageChange,
  onJumpToPage
}: ProjectTasksTableProps) {
  return (
    <>
      {filteredCount === 0 ? (
        <div className="empty-state">No task runs yet.</div>
      ) : (
        <div className="table-wrap project-table-wrap task-table-wrap">
          <table className="table project-table task-table">
            <thead>
              <tr>
                <th>
                  <span className="project-th">Ligand View</span>
                </th>
                {leadOptOnlyView ? (
                  <th>
                    <span className="project-th">MMP Stats</span>
                  </th>
                ) : (
                  <>
                    <th>
                      <button type="button" className={`task-th-sort ${sortKey === 'plddt' ? 'active' : ''}`} onClick={() => onSort('plddt')}>
                        <span className="project-th">pLDDT <span className="task-th-arrow">{sortMark('plddt')}</span></span>
                      </button>
                    </th>
                    <th>
                      <button type="button" className={`task-th-sort ${sortKey === 'iptm' ? 'active' : ''}`} onClick={() => onSort('iptm')}>
                        <span className="project-th">iPTM <span className="task-th-arrow">{sortMark('iptm')}</span></span>
                      </button>
                    </th>
                    <th>
                      <button type="button" className={`task-th-sort ${sortKey === 'pae' ? 'active' : ''}`} onClick={() => onSort('pae')}>
                        <span className="project-th">PAE <span className="task-th-arrow">{sortMark('pae')}</span></span>
                      </button>
                    </th>
                  </>
                )}
                <th>
                  <button type="button" className={`task-th-sort ${sortKey === 'submitted' ? 'active' : ''}`} onClick={() => onSort('submitted')}>
                    <span className="project-th"><Clock3 size={13} /> Submitted <span className="task-th-arrow">{sortMark('submitted')}</span></span>
                  </button>
                </th>
                {!leadOptOnlyView && (
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'backend' ? 'active' : ''}`} onClick={() => onSort('backend')}>
                      <span className="project-th">Backend <span className="task-th-arrow">{sortMark('backend')}</span></span>
                    </button>
                  </th>
                )}
                <th>
                  <button type="button" className={`task-th-sort ${sortKey === 'seed' ? 'active' : ''}`} onClick={() => onSort('seed')}>
                    <span className="project-th">Seed <span className="task-th-arrow">{sortMark('seed')}</span></span>
                  </button>
                </th>
                <th>
                  <button type="button" className={`task-th-sort ${sortKey === 'duration' ? 'active' : ''}`} onClick={() => onSort('duration')}>
                    <span className="project-th">Duration <span className="task-th-arrow">{sortMark('duration')}</span></span>
                  </button>
                </th>
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
                  mode={leadOptOnlyView ? 'lead_opt' : 'default'}
                  editingTaskNameId={editingTaskNameId}
                  editingTaskNameValue={editingTaskNameValue}
                  savingTaskNameId={savingTaskNameId}
                  openingTaskId={openingTaskId}
                  deletingTaskId={deletingTaskId}
                  onOpenTask={onOpenTask}
                  onRemoveTask={onRemoveTask}
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
