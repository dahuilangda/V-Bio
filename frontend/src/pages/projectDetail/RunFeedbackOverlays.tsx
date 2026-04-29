import type { MouseEvent } from 'react';
import { CheckCircle2 } from 'lucide-react';

interface RunFeedbackOverlaysProps {
  runSuccessNotice: string | null;
  taskHistoryPath: string;
  onOpenTaskHistory: (event: MouseEvent<HTMLElement>) => void;
  isRunRedirecting: boolean;
  error: string | null;
  resultError: string | null;
  affinityPreviewError: string | null;
  resultChainConsistencyWarning: string | null;
}

export function RunFeedbackOverlays({
  runSuccessNotice,
  taskHistoryPath,
  onOpenTaskHistory,
  isRunRedirecting,
  error,
  resultError,
  affinityPreviewError,
  resultChainConsistencyWarning
}: RunFeedbackOverlaysProps) {
  return (
    <>
      {runSuccessNotice && (
        <div className="run-inline-toast" role="status" aria-live="polite" aria-label="New task started">
          <span className="run-inline-toast-icon" aria-hidden="true">
            <CheckCircle2 size={16} />
          </span>
          <div className="run-inline-toast-line">
            <div className="run-inline-toast-text">{runSuccessNotice}</div>
            <a className="run-inline-toast-link" href={taskHistoryPath} onClick={onOpenTaskHistory}>
              Tasks
            </a>
          </div>
        </div>
      )}

      {isRunRedirecting && (
        <div className="run-submit-transition" role="status" aria-live="polite" aria-label="Task submitted, opening task history">
          <div className="run-submit-transition-card">
            <span className="run-submit-transition-icon" aria-hidden="true">
              <CheckCircle2 size={15} />
            </span>
            <span className="run-submit-transition-title">Task queued. Opening Tasks...</span>
          </div>
        </div>
      )}

      {(error || resultError || affinityPreviewError) && (
        <div className="alert error">{error || resultError || affinityPreviewError}</div>
      )}
      {resultChainConsistencyWarning && <div className="alert warning">{resultChainConsistencyWarning}</div>}
    </>
  );
}
