import { useEffect } from 'react';
import type { NavigateFunction, Location } from 'react-router-dom';
import type { ProjectTask } from '../../types/models';
import type { TaskWorkspaceView } from './taskListTypes';
import { normalizeTaskWorkspaceView } from './taskDataUtils';

interface UseProjectTasksApiContextSyncInput {
  workspaceView: TaskWorkspaceView;
  currentTaskRow: ProjectTask | null;
  location: Location;
  navigate: NavigateFunction;
}

export function useProjectTasksApiContextSync({
  workspaceView,
  currentTaskRow,
  location,
  navigate
}: UseProjectTasksApiContextSyncInput): void {
  useEffect(() => {
    if (workspaceView !== 'api') return;
    const query = new URLSearchParams(location.search);
    if (normalizeTaskWorkspaceView(query.get('view')) !== 'api') return;
    if (!currentTaskRow) return;
    if (query.get('task_row_id')) return;
    query.set('task_row_id', currentTaskRow.id);
    const taskId = String(currentTaskRow.task_id || '').trim();
    const taskName = String(currentTaskRow.name || '').trim();
    const taskSummary = String(currentTaskRow.summary || '').trim();
    if (taskId) query.set('task_id', taskId);
    if (taskName) query.set('task_name', taskName);
    if (taskSummary) query.set('task_summary', taskSummary);
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
  }, [workspaceView, currentTaskRow, location.pathname, location.search, navigate]);
}
