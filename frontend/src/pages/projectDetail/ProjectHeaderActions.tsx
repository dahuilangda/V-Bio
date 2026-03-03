import type { MouseEvent, RefObject } from 'react';
import { ArrowLeft, Download, LoaderCircle, RefreshCcw, Save, Square } from 'lucide-react';
import { RunPlayIcon } from './RunPlayIcon';

interface ProjectHeaderActionsProps {
  taskHistoryPath: string;
  onOpenTaskHistory: (event: MouseEvent<HTMLElement>) => void;
  onDownloadResult: () => void;
  canDownloadResult: boolean;
  onSaveDraft: () => void;
  canEdit: boolean;
  saving: boolean;
  hasUnsavedChanges: boolean;
  onReset: () => void;
  loading: boolean;
  submitting: boolean;
  runSubmitting: boolean;
  runActionRef: RefObject<HTMLDivElement>;
  topRunButtonRef: RefObject<HTMLButtonElement>;
  onRunAction: () => void;
  runDisabled: boolean;
  runBlockedReason: string;
  workflowRunLabel: string;
  isRunRedirecting: boolean;
  canOpenRunMenu: boolean;
  runMenuOpen: boolean;
  onRestoreSavedDraft: () => void;
  onRunCurrentDraft: () => void;
  showRunAction?: boolean;
  showStopAction?: boolean;
  stopSubmitting?: boolean;
  stopDisabled?: boolean;
  stopTitle?: string;
  onStopAction?: () => void;
}

export function ProjectHeaderActions({
  taskHistoryPath,
  onOpenTaskHistory,
  onDownloadResult,
  canDownloadResult,
  onSaveDraft,
  canEdit,
  saving,
  hasUnsavedChanges,
  onReset,
  loading,
  submitting,
  runSubmitting,
  runActionRef,
  topRunButtonRef,
  onRunAction,
  runDisabled,
  runBlockedReason,
  workflowRunLabel,
  isRunRedirecting,
  canOpenRunMenu,
  runMenuOpen,
  onRestoreSavedDraft,
  onRunCurrentDraft,
  showRunAction = true,
  showStopAction = false,
  stopSubmitting = false,
  stopDisabled = false,
  stopTitle = '',
  onStopAction
}: ProjectHeaderActionsProps) {
  const runTitle =
    runSubmitting
      ? 'Submitting'
      : isRunRedirecting
        ? 'Opening task history'
        : runBlockedReason
          ? runBlockedReason
          : hasUnsavedChanges
            ? `${workflowRunLabel} (has unsaved changes)`
            : workflowRunLabel;

  const runAriaLabel =
    runSubmitting
      ? 'Submitting'
      : isRunRedirecting
        ? 'Opening task history'
        : runBlockedReason
          ? runBlockedReason
          : workflowRunLabel;

  return (
    <div className="row gap-8 page-header-actions">
      <a
        className="task-row-action-btn"
        href={taskHistoryPath}
        onClick={onOpenTaskHistory}
        title="Back to task list"
        aria-label="Back to task list"
      >
        <ArrowLeft size={14} />
      </a>
      <button
        className="task-row-action-btn"
        onClick={onDownloadResult}
        disabled={!canDownloadResult}
        title="Download result"
        aria-label="Download result"
      >
        <Download size={15} />
      </button>
      <button
        className="task-row-action-btn"
        type="button"
        onClick={onSaveDraft}
        disabled={!canEdit || saving || !hasUnsavedChanges}
        title={saving ? 'Saving draft' : hasUnsavedChanges ? 'Save draft' : 'Draft saved'}
        aria-label={saving ? 'Saving draft' : hasUnsavedChanges ? 'Save draft' : 'Draft saved'}
      >
        {saving ? <LoaderCircle size={15} className="spin" /> : <Save size={15} />}
      </button>
      <button
        type="button"
        className="task-row-action-btn"
        onClick={onReset}
        disabled={loading || saving || submitting || !hasUnsavedChanges}
        title={hasUnsavedChanges ? 'Discard unsaved edits' : 'No unsaved edits'}
        aria-label={hasUnsavedChanges ? 'Discard unsaved edits' : 'No unsaved edits'}
      >
        <RefreshCcw size={14} />
      </button>
      {showStopAction ? (
        <button
          type="button"
          className="task-row-action-btn"
          onClick={onStopAction}
          disabled={stopDisabled || !onStopAction}
          title={stopTitle}
          aria-label={stopTitle || 'Stop run'}
        >
          {stopSubmitting ? <LoaderCircle size={14} className="spin" /> : <Square size={14} />}
        </button>
      ) : null}
      {showRunAction ? (
        <div className="run-action" ref={runActionRef}>
          <button
            className="task-row-action-btn task-row-action-btn-primary"
            type="button"
            ref={topRunButtonRef}
            onClick={onRunAction}
            disabled={runDisabled}
            title={runTitle}
            aria-label={runAriaLabel}
            aria-haspopup={canOpenRunMenu ? 'menu' : undefined}
            aria-expanded={canOpenRunMenu ? runMenuOpen : undefined}
          >
            {runSubmitting || isRunRedirecting ? <LoaderCircle size={15} className="spin" /> : <RunPlayIcon size={15} />}
          </button>
          {runMenuOpen && hasUnsavedChanges && (
            <div className="run-action-menu" role="menu" aria-label="Run options">
              <button
                type="button"
                className="run-action-item"
                onClick={onRestoreSavedDraft}
                disabled={loading || saving || submitting}
              >
                Restore Saved
              </button>
              <button
                type="button"
                className="run-action-item primary"
                onClick={onRunCurrentDraft}
                disabled={loading || saving || submitting}
              >
                Run Current
              </button>
            </div>
          )}
        </div>
      ) : null}
    </div>
  );
}
