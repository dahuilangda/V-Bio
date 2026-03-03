import { useEffect, useState } from 'react';
import type { NavigateFunction } from 'react-router-dom';
import type { ProjectTask } from '../../types/models';

interface EntryRoutingResolutionOptions {
  projectId: string;
  hasExplicitWorkspaceQuery: boolean;
  navigate: NavigateFunction;
  listProjectTasksCompact: (projectId: string) => Promise<ProjectTask[]>;
}

export function useEntryRoutingResolution(options: EntryRoutingResolutionOptions): boolean {
  const { projectId, hasExplicitWorkspaceQuery, navigate, listProjectTasksCompact } = options;
  const [entryRoutingResolved, setEntryRoutingResolved] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const resolveEntryRoute = async () => {
      const normalizedProjectId = String(projectId || '').trim();
      if (!normalizedProjectId) {
        if (!cancelled) setEntryRoutingResolved(true);
        return;
      }
      if (hasExplicitWorkspaceQuery) {
        if (!cancelled) setEntryRoutingResolved(true);
        return;
      }
      try {
        const rows = await listProjectTasksCompact(normalizedProjectId);
        if (cancelled) return;
        if (rows.length > 0) {
          navigate(`/projects/${normalizedProjectId}/tasks`, { replace: true });
          return;
        }
      } catch {
        // If list lookup fails, keep current route and let page-level loadProject surface actual errors.
      }
      if (!cancelled) setEntryRoutingResolved(true);
    };

    setEntryRoutingResolved(false);
    void resolveEntryRoute();
    return () => {
      cancelled = true;
    };
  }, [projectId, hasExplicitWorkspaceQuery, navigate, listProjectTasksCompact]);

  return entryRoutingResolved;
}
