import { useEffect, useState } from 'react';
import { formatDuration } from '../../utils/date';

export interface ProjectHeaderMetaProps {
  projectName: string;
  displayTaskState: string;
  workflowShortTitle: string;
  isActiveRuntime: boolean;
  progressPercent: number;
  /** Stage-level progress from the worker payload ("Generation 3/6 · 45/128 candidates"); null when unavailable. */
  stagedProgressText: string | null;
  submittedAt: string | null;
  totalRuntimeSeconds: number | null;
}

function ElapsedSecondsChip({ submittedAt, taskState }: { submittedAt: string; taskState: string }) {
  // self-held 1s tick: only this chip re-renders while a task runs, not the workspace
  const active = taskState === 'QUEUED' || taskState === 'RUNNING';
  const [nowTs, setNowTs] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNowTs(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [active]);
  if (!active) return null;
  const elapsedSeconds = Math.max(0, Math.floor((nowTs - new Date(submittedAt).getTime()) / 1000));
  return (
    <span
      className={`meta-chip meta-chip-live meta-chip-live-elapsed ${
        taskState === 'RUNNING' ? 'meta-chip-live-running' : 'meta-chip-live-queued'
      }`}
    >
      {formatDuration(elapsedSeconds)} elapsed
    </span>
  );
}

export function ProjectHeaderMeta({
  projectName,
  displayTaskState,
  workflowShortTitle,
  isActiveRuntime,
  progressPercent,
  stagedProgressText,
  submittedAt,
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
            {displayTaskState === 'RUNNING' && stagedProgressText !== null && (
              <span className="meta-chip meta-chip-live meta-chip-live-running">{stagedProgressText}</span>
            )}
            {submittedAt !== null && <ElapsedSecondsChip submittedAt={submittedAt} taskState={displayTaskState} />}
          </>
        ) : (
          displayTaskState === 'SUCCESS' &&
          totalRuntimeSeconds !== null && <span className="meta-chip">Completed in {formatDuration(totalRuntimeSeconds)}</span>
        )}
      </div>
    </div>
  );
}
