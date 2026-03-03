import { ArrowLeft, Download, KeyRound, LoaderCircle, Plus } from 'lucide-react';
import { Link } from 'react-router-dom';

interface ProjectTasksHeaderProps {
  projectName: string;
  taskCountText: string;
  refreshing: boolean;
  createTaskHref: string;
  backToCurrentTaskHref: string;
  exportingExcel: boolean;
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
  exportingExcel,
  filteredCount,
  onDownloadExcel,
  onOpenApi,
  apiAccessDisabled = false,
  apiAccessDisabledReason = ''
}: ProjectTasksHeaderProps) {
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
          <Link className="task-row-action-btn task-row-action-btn-primary" to={createTaskHref} title="New task" aria-label="New task">
            <Plus size={14} />
          </Link>
          <Link className="task-row-action-btn" to={backToCurrentTaskHref} title="Open current task" aria-label="Open current task">
            <ArrowLeft size={14} />
          </Link>
          <button
            type="button"
            className="task-row-action-btn"
            onClick={onDownloadExcel}
            disabled={exportingExcel || filteredCount === 0}
            title="Export task list"
            aria-label="Export task list"
          >
            {exportingExcel ? <LoaderCircle size={14} className="spin" /> : <Download size={14} />}
          </button>
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
