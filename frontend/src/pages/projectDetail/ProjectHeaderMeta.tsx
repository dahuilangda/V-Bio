import { useEffect, useState } from 'react';
import { formatDuration } from '../../utils/date';

interface ProjectHeaderMetaProps {
  projectName: string;
  displayTaskState: string;
  workflowShortTitle: string;
  isActiveRuntime: boolean;
  progressPercent: number;
  submittedAt: string | null;
  totalRuntimeSeconds: number | null;
}

function ElapsedSecondsChip({ submittedAt, taskState }: { submittedAt: string; taskState: string }) {
  // Self-held 1s tick. The historical implementation ticked a top-level nowTs state that
  // re-rendered the ENTIRE workspace tree every second while a task ran; this chip was its
  // only consumer, so the clock lives here now and nothing above re-renders.
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
