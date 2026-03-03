import { formatDuration } from '../../utils/date';

interface ProjectHeaderMetaProps {
  projectName: string;
  displayTaskState: string;
  workflowShortTitle: string;
  isActiveRuntime: boolean;
  progressPercent: number;
  waitingSeconds: number | null;
  totalRuntimeSeconds: number | null;
}

export function ProjectHeaderMeta({
  projectName,
  displayTaskState,
  workflowShortTitle,
  isActiveRuntime,
  progressPercent,
  waitingSeconds,
  totalRuntimeSeconds
}: ProjectHeaderMetaProps) {
  return (
    <div className="page-header-left">
      <h1>{projectName}</h1>
      <div className="project-compact-meta">
        <span className={`badge state-${displayTaskState.toLowerCase()}`}>{displayTaskState}</span>
        <span className="meta-chip">{workflowShortTitle}</span>
        {isActiveRuntime ? (
          <>
            <span
              className={`meta-chip meta-chip-live meta-chip-live-progress ${
                displayTaskState === 'RUNNING' ? 'meta-chip-live-running' : 'meta-chip-live-queued'
              }`}
            >
              {Math.round(progressPercent)}%
            </span>
            {waitingSeconds !== null && (
              <span
                className={`meta-chip meta-chip-live meta-chip-live-elapsed ${
                  displayTaskState === 'RUNNING' ? 'meta-chip-live-running' : 'meta-chip-live-queued'
                }`}
              >
                {formatDuration(waitingSeconds)} elapsed
              </span>
            )}
          </>
        ) : (
          displayTaskState === 'SUCCESS' &&
          totalRuntimeSeconds !== null && <span className="meta-chip">Completed in {formatDuration(totalRuntimeSeconds)}</span>
        )}
      </div>
    </div>
  );
}
