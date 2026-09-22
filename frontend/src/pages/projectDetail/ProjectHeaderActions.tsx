import type { MouseEvent, RefObject } from 'react';
import { ArrowLeft, Download, LoaderCircle, RefreshCcw, Save, Square } from 'lucide-react';
import { RunPlayIcon } from './RunPlayIcon';

export interface ProjectHeaderActionsProps {
  taskHistoryPath: string;
  onOpenTaskHistory: (event: MouseEvent<HTMLElement>) => void;
  onDownloadResult: () => void;
  canDownloadResult: boolean;
  isDownloadingResult?: boolean;
  saveDraftAction: () => void;
  isEditable: boolean;
  isSaving: boolean;
  hasUnsavedChanges: boolean;
  onReset: () => void;
  isLoading: boolean;
  isSubmitting: boolean;
  isRunSubmitting: boolean;
  runActionRef: RefObject<HTMLDivElement>;
  topRunButtonRef: RefObject<HTMLButtonElement>;
  runAction: () => void;
  isRunDisabled: boolean;
  runBlockedReason: string;
  workflowRunLabel: string;
  isRunRedirecting: boolean;
  isRunMenuAllowed: boolean;
  isRunMenuOpen: boolean;
  onRestoreSavedDraft: () => void;
  onRunCurrentDraft: () => void;
  isRunActionVisible?: boolean;
  isStopActionVisible?: boolean;
  isStopSubmitting?: boolean;
  isStopDisabled?: boolean;
  stopTitle?: string;
  stopAction?: () => void;
}

export function ProjectHeaderActions({
  onOpenTaskHistory,
  onDownloadResult,
  isDownloadingResult = false,
  canDownloadResult,
  saveDraftAction,
  isEditable,
  isSaving,
  hasUnsavedChanges,
  onReset,
  isLoading,
  isSubmitting,
  isRunSubmitting,
  runActionRef,
  topRunButtonRef,
  runAction,
  isRunDisabled,
  runBlockedReason,
  workflowRunLabel,
  isRunRedirecting,
  isRunMenuAllowed,
  isRunMenuOpen,
  onRestoreSavedDraft,
  onRunCurrentDraft,
  isRunActionVisible = true,
  isStopActionVisible = false,
  isStopSubmitting = false,
  isStopDisabled = false,
  stopTitle = '',
  stopAction
}: ProjectHeaderActionsProps) {
  const runTitle =
    isRunSubmitting
      ? 'Submitting'
      : isRunRedirecting
        ? 'Opening task history'
        : runBlockedReason
          ? runBlockedReason
          : hasUnsavedChanges
            ? `${workflowRunLabel} (has unsaved changes)`
            : workflowRunLabel;

  const runAriaLabel =
    isRunSubmitting
      ? 'Submitting'
      : isRunRedirecting
        ? 'Opening task history'
        : runBlockedReason
          ? runBlockedReason
          : workflowRunLabel;

  return (
    <div className="row gap-8 page-header-actions">
      <button
        type="button"
        className="task-row-action-btn"
        onClick={onOpenTaskHistory}
        title="Back to task list"
        aria-label="Back to task list"
      >
        <ArrowLeft size={14} />
      </button>
      <button type="button"
        className="task-row-action-btn"
        onClick={onDownloadResult}
        disabled={!canDownloadResult || isDownloadingResult}
        title={isDownloadingResult ? 'Downloading result' : 'Download result'}
        aria-label={isDownloadingResult ? 'Downloading result' : 'Download result'}
        aria-busy={isDownloadingResult}
      >
        {isDownloadingResult ? <LoaderCircle size={14} className="spin" /> : <Download size={14} />}
      </button>
      <button
        className="task-row-action-btn"
        type="button"
        onClick={saveDraftAction}
        disabled={!isEditable || isSaving || !hasUnsavedChanges}
        title={isSaving ? 'Saving draft' : hasUnsavedChanges ? 'Save draft' : 'Draft saved'}
        aria-label={isSaving ? 'Saving draft' : hasUnsavedChanges ? 'Save draft' : 'Draft saved'}
      >
        {isSaving ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
      </button>
      <button
        type="button"
        className="task-row-action-btn"
        onClick={onReset}
        disabled={isLoading || isSaving || isSubmitting || !hasUnsavedChanges}
        title={hasUnsavedChanges ? 'Discard unsaved edits' : 'No unsaved edits'}
        aria-label={hasUnsavedChanges ? 'Discard unsaved edits' : 'No unsaved edits'}
      >
        <RefreshCcw size={14} />
      </button>
      {isStopActionVisible ? (
        <button
          type="button"
          className="task-row-action-btn"
          onClick={stopAction}
          disabled={isStopDisabled || !stopAction}
          title={stopTitle}
          aria-label={stopTitle || 'Stop run'}
        >
          {isStopSubmitting ? <LoaderCircle size={14} className="spin" /> : <Square size={14} />}
        </button>
      ) : null}
      {isRunActionVisible ? (
        <div className="run-action" ref={runActionRef}>
          <button
            className="task-row-action-btn task-row-action-btn-primary"
            type="button"
            ref={topRunButtonRef}
            onClick={runAction}
            disabled={isRunDisabled}
            title={runTitle}
            aria-label={runAriaLabel}
            aria-haspopup={isRunMenuAllowed ? 'menu' : undefined}
            aria-expanded={isRunMenuAllowed ? isRunMenuOpen : undefined}
          >
            {isRunSubmitting || isRunRedirecting ? <LoaderCircle size={14} className="spin" /> : <RunPlayIcon size={14} />}
          </button>
          {isRunMenuOpen && hasUnsavedChanges && (
            <div className="run-action-menu" role="menu" aria-label="Run options">
              <button
                type="button"
                className="run-action-item"
                onClick={onRestoreSavedDraft}
                disabled={isLoading || isSaving || isSubmitting}
              >
                Restore Saved
              </button>
              <button
                type="button"
                className="run-action-item primary"
                onClick={onRunCurrentDraft}
                disabled={isLoading || isSaving || isSubmitting}
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
