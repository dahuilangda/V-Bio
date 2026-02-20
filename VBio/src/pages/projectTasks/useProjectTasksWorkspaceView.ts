import { useCallback, useEffect, useState } from 'react';
import type { NavigateFunction, Location } from 'react-router-dom';
import type { TaskWorkspaceView } from './taskListTypes';
import { normalizeTaskWorkspaceView } from './taskDataUtils';

interface UseProjectTasksWorkspaceViewInput {
  location: Location;
  navigate: NavigateFunction;
}

interface UseProjectTasksWorkspaceViewResult {
  workspaceView: TaskWorkspaceView;
  setWorkspaceViewWithUrl: (nextView: TaskWorkspaceView) => void;
}

export function useProjectTasksWorkspaceView({
  location,
  navigate
}: UseProjectTasksWorkspaceViewInput): UseProjectTasksWorkspaceViewResult {
  const [workspaceView, setWorkspaceView] = useState<TaskWorkspaceView>(() => {
    if (typeof window === 'undefined') return 'tasks';
    return normalizeTaskWorkspaceView(new URLSearchParams(window.location.search).get('view'));
  });

  useEffect(() => {
    const nextView = normalizeTaskWorkspaceView(new URLSearchParams(location.search).get('view'));
    setWorkspaceView((prev) => (prev === nextView ? prev : nextView));
  }, [location.search]);

  const setWorkspaceViewWithUrl = useCallback(
    (nextView: TaskWorkspaceView) => {
      setWorkspaceView(nextView);
      const query = new URLSearchParams(location.search);
      if (nextView === 'api') {
        query.set('view', 'api');
      } else {
        query.delete('view');
        query.delete('open_builder');
        query.delete('task_row_id');
        query.delete('task_id');
        query.delete('task_name');
        query.delete('task_summary');
        query.delete('from');
      }
      const nextSearch = query.toString();
      const currentSearch = new URLSearchParams(location.search).toString();
      if (nextSearch === currentSearch) return;
      navigate(
        {
          pathname: location.pathname,
          search: nextSearch ? `?${nextSearch}` : ''
        },
        { replace: true }
      );
    },
    [location.pathname, location.search, navigate]
  );

  return {
    workspaceView,
    setWorkspaceViewWithUrl
  };
}
