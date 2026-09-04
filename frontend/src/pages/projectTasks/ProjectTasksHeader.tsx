import { Download, KeyRound, Plus, ArrowLeft, X } from 'lucide-react';
import { Link } from 'react-router-dom';
import type { ExportProgressInfo } from './taskListTypes';

export type { ExportProgressInfo };

function ExportProgressRing({ percent }: { percent: number | null }) {
  const radius = 6;
  const circumference = 2 * Math.PI * radius;
  // null = indeterminate (submitting): a short arc that spins via the .spin class.
  const shownPercent = percent === null ? 30 : Math.max(0, Math.min(100, percent));
  return (
    <svg
      viewBox="0 0 16 16"
      width={14}
      height={14}
      className={`task-export-progress-ring${percent === null ? ' spin' : ''}`}
      aria-hidden="true"
    >
      <circle cx="8" cy="8" r={radius} fill="none" stroke="#e5eee8" strokeWidth="2.5" />
      <circle
        cx="8"
        cy="8"
        r={radius}
        fill="none"
        stroke="var(--primary)"
        strokeWidth="2.5"
        strokeLinecap="round"
        strokeDasharray={circumference}
        strokeDashoffset={circumference * (1 - shownPercent / 100)}
        transform="rotate(-90 8 8)"
      />
    </svg>
  );
}

interface ProjectTasksHeaderProps {
  projectName: string;
  taskCountText: string;
  refreshing: boolean;
  createTaskHref: string;
  backToCurrentTaskHref: string;
  canEdit: boolean;
  exportingExcel: boolean;
  exportProgress?: ExportProgressInfo | null;
  filteredCount: number;
  onDownloadExcel: () => void;
  onOpenApi: () => void;
  apiAccessDisabled?: boolean;
  apiAccessDisabledReason?: string;
}

export function ProjectTasksHeader({
  projectName,
  taskCountText,
  refreshing,
  createTaskHref,
  backToCurrentTaskHref,
  canEdit,
  exportingExcel,
  exportProgress = null,
  filteredCount,
  onDownloadExcel,
  onOpenApi,
  apiAccessDisabled = false,
  apiAccessDisabledReason = ''
}: ProjectTasksHeaderProps) {
  const exportPercent =
    exportProgress === null
      ? null
      : exportProgress.phase === 'submitting'
        ? null // indeterminate until the server job starts reporting
        : exportProgress.phase === 'downloading'
          ? 100
          : exportProgress.total > 0
            ? Math.round((exportProgress.done / exportProgress.total) * 100)
            : 0;
  const exportSubText =
    exportProgress === null
      ? ''
      : exportProgress.phase === 'collecting'
        ? `Loading tasks ${exportProgress.done.toLocaleString()} / ${exportProgress.total.toLocaleString()}`
        : exportProgress.phase === 'submitting'
          ? `Submitting ${exportProgress.total} tasks to the server queue…`
          : exportProgress.phase === 'downloading'
            ? 'Preparing download…'
            : `${exportProgress.done.toLocaleString()} / ${exportProgress.total.toLocaleString()} tasks`;
  const exportTitle =
    exportProgress?.phase === 'collecting' ? 'Loading tasks' : 'Exporting Excel';
  return (
    <section className="page-header">
      <div className="page-header-left">
        <h1>Tasks</h1>
        <p className="muted">
          {projectName} · {taskCountText}
          {refreshing ? ' · Syncing...' : ''}
        </p>
      </div>
      <div className="row gap-8 page-header-actions page-header-actions-minimal">
        <div className="task-header-inline-actions" role="toolbar" aria-label="Task actions">
          <Link
            className="task-row-action-btn task-row-action-btn-primary"
            to={createTaskHref}
            title={canEdit ? 'New task' : 'Shared projects are read-only'}
            aria-label="New task"
            onClick={(event) => {
              if (canEdit) return;
              event.preventDefault();
            }}
            aria-disabled={!canEdit}
            style={!canEdit ? { pointerEvents: 'none', opacity: 0.5 } : undefined}
          >
            <Plus size={14} />
          </Link>
          <Link className="task-row-action-btn" to={backToCurrentTaskHref} title="Open current task" aria-label="Open current task">
            <ArrowLeft size={14} />
          </Link>
          <span className="task-export-anchor">
            <button
              type="button"
              className="task-row-action-btn"
              onClick={onDownloadExcel}
              disabled={!exportingExcel && filteredCount === 0}
              title={exportingExcel ? 'Cancel export' : 'Export task list'}
              aria-label={exportingExcel ? 'Cancel export' : 'Export task list'}
            >
              {exportingExcel ? (
                <>
                  <ExportProgressRing percent={exportPercent} />
                  <X size={14} className="task-export-cancel-icon" aria-hidden="true" />
                </>
              ) : (
                <Download size={14} />
              )}
            </button>
            {exportingExcel && exportProgress ? (
              <div className="task-export-popover" role="status">
                <div className="task-export-popover-head">
                  <span className="task-export-popover-title">{exportTitle}</span>
                  <span className="task-export-popover-pct">{exportPercent === null ? '' : `${exportPercent}%`}</span>
                </div>
                <div className="task-export-popover-bar">
                  <div
                    className={`task-export-popover-fill${exportProgress.phase === 'submitting' ? ' task-export-popover-fill--pulse' : ''}`}
                    style={exportPercent === null ? undefined : { width: `${exportPercent}%` }}
                  />
                </div>
                <div className="task-export-popover-sub">{exportSubText}</div>
              </div>
            ) : null}
          </span>
          <button
            type="button"
            className="task-row-action-btn"
            onClick={onOpenApi}
            disabled={apiAccessDisabled}
            title={apiAccessDisabled ? (apiAccessDisabledReason || 'API access unavailable') : 'API access'}
            aria-label="API access"
          >
            <KeyRound size={14} />
          </button>
        </div>
      </div>
    </section>
  );
}
