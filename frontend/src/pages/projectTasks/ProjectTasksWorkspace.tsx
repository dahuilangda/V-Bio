import { ProjectTasksFilters, type ProjectTasksFiltersProps } from './ProjectTasksFilters';
import { ProjectTasksTable, type ProjectTasksTableProps } from './ProjectTasksTable';
import { deriveTaskTableMode } from './taskListTypes';

/**
 * Composition root of the tasks tab: forwards the two children's own prop
 * groups; tableMode is the only derived value (from workflowFilter /
 * workflowOptions inside the filters group).
 */
export interface ProjectTasksWorkspaceProps {
  filtersProps: Omit<ProjectTasksFiltersProps, 'tableMode' | 'isCompactMetricsView'>;
  tableProps: Omit<ProjectTasksTableProps, 'tableMode'>;
}

export function ProjectTasksWorkspace({ filtersProps, tableProps }: ProjectTasksWorkspaceProps) {
  const tableMode = deriveTaskTableMode(filtersProps.workflowFilter, filtersProps.workflowOptions);
  const compactMetricsView = tableMode !== 'default';

  return (
    <section className="panel">
      <ProjectTasksFilters
        {...filtersProps}
        tableMode={tableMode}
        isCompactMetricsView={compactMetricsView}
      />

      <ProjectTasksTable {...tableProps} tableMode={tableMode} />
    </section>
  );
}
