import { useEffect, useState } from 'react';
import type { NavigateFunction } from 'react-router-dom';

interface EntryRoutingResolutionOptions {
  projectId: string;
  hasExplicitWorkspaceQuery: boolean;
  navigate: NavigateFunction;
}

export function useEntryRoutingResolution(options: EntryRoutingResolutionOptions): boolean {
  const { projectId, hasExplicitWorkspaceQuery, navigate } = options;
  const [entryRoutingResolved, setEntryRoutingResolved] = useState(false);

  useEffect(() => {
    const normalizedProjectId = String(projectId || '').trim();
    // With an explicit workspace intent (tab / task_row_id / new_task) the user lands in the
    // workspace editor directly.
    if (!normalizedProjectId || hasExplicitWorkspaceQuery) {
      setEntryRoutingResolved(true);
      return;
    }
    // No explicit intent: default to the task list regardless of task count. A project with zero
    // tasks used to drop the user into the workspace with an unsaved draft task open, which read
    // as "a task was auto-created"; the task list's "New Task" button is the explicit way to
    // start the first task.
    navigate(`/projects/${normalizedProjectId}/tasks`, { replace: true });
  }, [projectId, hasExplicitWorkspaceQuery, navigate]);

  return entryRoutingResolved;
}
