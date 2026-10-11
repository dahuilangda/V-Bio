import { Suspense, lazy, type FormEvent, type ReactNode } from 'react';
import { ProjectBasicsMetadataForm } from '../../components/project/ProjectBasicsMetadataForm';
import type { ProjectResultsSectionProps } from '../../components/project/ProjectResultsSection';
import type { AffinityWorkflowSectionProps } from './AffinityWorkflowSection';
import type { LeadOptimizationWorkflowSectionProps } from './LeadOptimizationWorkflowSection';
import type { PredictionWorkflowSectionProps } from './PredictionWorkflowSection';
import type { VirtualScreeningWorkflowSectionProps } from './VirtualScreeningWorkflowSection';
import type { WorkflowRuntimeSettingsSectionProps } from './WorkflowRuntimeSettingsSection';
import type { WorkspaceTab } from './workspaceTypes';

// A project renders exactly ONE workflow section, and the results section only
// mounts on the results tab — so each of these heavy subtrees loads on first
// render instead of weighing down the detail-page bundle for every project.
const ProjectResultsSection = lazy(() => import('../../components/project/ProjectResultsSection').then((m) => ({ default: m.ProjectResultsSection })));
const AffinityWorkflowSection = lazy(() => import('./AffinityWorkflowSection').then((m) => ({ default: m.AffinityWorkflowSection })));
const LeadOptimizationWorkflowSection = lazy(() => import('./LeadOptimizationWorkflowSection').then((m) => ({ default: m.LeadOptimizationWorkflowSection })));
const PredictionWorkflowSection = lazy(() => import('./PredictionWorkflowSection').then((m) => ({ default: m.PredictionWorkflowSection })));
const VirtualScreeningWorkflowSection = lazy(() => import('./VirtualScreeningWorkflowSection').then((m) => ({ default: m.VirtualScreeningWorkflowSection })));
const WorkflowRuntimeSettingsSection = lazy(() => import('./WorkflowRuntimeSettingsSection').then((m) => ({ default: m.WorkflowRuntimeSettingsSection })));

function SectionSuspense({ children }: { children: ReactNode }) {
  return <Suspense fallback={<div className="workflow-note" role="status" aria-live="polite">Loading workspace...</div>}>{children}</Suspense>;
}

export interface ProjectWorkspaceContentProps {
  workspaceTab: WorkspaceTab;
  componentStepLabel: string;
  projectResultsSectionProps: ProjectResultsSectionProps;
  saveDraftAction: (event: FormEvent<HTMLFormElement>) => void;
  isEditable: boolean;
  taskName: string;
  taskSummary: string;
  onTaskNameChange: (value: string) => void;
  onTaskSummaryChange: (value: string) => void;
  affinitySectionProps: Omit<AffinityWorkflowSectionProps, 'isVisible'>;
  leadOptimizationSectionProps: Omit<LeadOptimizationWorkflowSectionProps, 'isVisible'>;
  isPredictionWorkflow: boolean;
  isVirtualScreeningWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  predictionSectionProps: Omit<PredictionWorkflowSectionProps, 'isVisible'>;
  virtualScreeningSectionProps: Omit<VirtualScreeningWorkflowSectionProps, 'isVisible'>;
  workflowDescription: string;
  runtimeSettingsProps: Omit<WorkflowRuntimeSettingsSectionProps, 'isVisible'>;
}

export function ProjectWorkspaceContent({
  workspaceTab,
  componentStepLabel,
  projectResultsSectionProps,
  saveDraftAction,
  isEditable,
  taskName,
  taskSummary,
  onTaskNameChange,
  onTaskSummaryChange,
  affinitySectionProps,
  leadOptimizationSectionProps,
  isPredictionWorkflow,
  isVirtualScreeningWorkflow,
  isAffinityWorkflow,
  isLeadOptimizationWorkflow,
  predictionSectionProps,
  virtualScreeningSectionProps,
  workflowDescription,
  runtimeSettingsProps
}: ProjectWorkspaceContentProps) {
  const showLeadOptWorkspace = isLeadOptimizationWorkflow && (workspaceTab === 'components' || workspaceTab === 'results');
  const showNativeResults = workspaceTab === 'results' && !isLeadOptimizationWorkflow;
  const showFlatPredictionWorkspace =
    isPredictionWorkflow && !isVirtualScreeningWorkflow &&
    (workspaceTab === 'components' || workspaceTab === 'constraints');
  const showFlatAffinityWorkspace = isAffinityWorkflow && workspaceTab === 'components';
  const showFlatVirtualScreeningWorkspace = isVirtualScreeningWorkflow && workspaceTab === 'components';
  const showFlatWorkspace = showFlatPredictionWorkspace || showFlatVirtualScreeningWorkspace || showFlatAffinityWorkspace;
  const showRuntimeSettingsInComponents =
    workspaceTab === 'components' && !isLeadOptimizationWorkflow && !isAffinityWorkflow && !isVirtualScreeningWorkflow;
  const showPredictionSection =
    isPredictionWorkflow && !isVirtualScreeningWorkflow &&
    (workspaceTab === 'components' || workspaceTab === 'constraints');
  const showAffinitySection = workspaceTab === 'components' && isAffinityWorkflow;

  if (showLeadOptWorkspace) {
    return (
      <div className="workspace-content workspace-content--lead-opt">
        <SectionSuspense>
          <LeadOptimizationWorkflowSection
            isVisible
            {...leadOptimizationSectionProps}
          />
        </SectionSuspense>
      </div>
    );
  }

  const workspaceTitle =
    workspaceTab === 'constraints'
      ? 'Constraints'
      : workspaceTab === 'components'
        ? componentStepLabel
        : 'Basics';
  const showWorkspaceTitle = true;

  return (
    <div className="workspace-content">
      {showNativeResults && (
        <SectionSuspense>
          <ProjectResultsSection {...projectResultsSectionProps} />
        </SectionSuspense>
      )}

      {showFlatWorkspace && (
        <form className="form-grid" onSubmit={saveDraftAction}>
          {showFlatPredictionWorkspace && <SectionSuspense><PredictionWorkflowSection isVisible {...predictionSectionProps} /></SectionSuspense>}
          {showFlatVirtualScreeningWorkspace && <SectionSuspense><VirtualScreeningWorkflowSection isVisible {...virtualScreeningSectionProps} /></SectionSuspense>}
          {showFlatAffinityWorkspace && <SectionSuspense><AffinityWorkflowSection isVisible {...affinitySectionProps} /></SectionSuspense>}
          {showFlatPredictionWorkspace && showRuntimeSettingsInComponents ? (
            <SectionSuspense><WorkflowRuntimeSettingsSection isVisible {...runtimeSettingsProps} /></SectionSuspense>
          ) : null}
        </form>
      )}

      {!showFlatWorkspace && (workspaceTab !== 'results' || isLeadOptimizationWorkflow) && (
        <section className="panel inputs-panel">
          {showWorkspaceTitle ? <h2>{workspaceTitle}</h2> : null}

          <form className="form-grid" onSubmit={saveDraftAction}>
            {workspaceTab === 'basics' && (
              <ProjectBasicsMetadataForm
                isEditable={isEditable}
                taskName={taskName}
                taskSummary={taskSummary}
                onTaskNameChange={onTaskNameChange}
                onTaskSummaryChange={onTaskSummaryChange}
              />
            )}

            {showAffinitySection ? <SectionSuspense><AffinityWorkflowSection isVisible {...affinitySectionProps} /></SectionSuspense> : null}

            {showPredictionSection ? (
              <SectionSuspense><PredictionWorkflowSection isVisible {...predictionSectionProps} /></SectionSuspense>
            ) : isVirtualScreeningWorkflow && workspaceTab === 'components' ? (
              <SectionSuspense><VirtualScreeningWorkflowSection isVisible {...virtualScreeningSectionProps} /></SectionSuspense>
            ) : isAffinityWorkflow || isLeadOptimizationWorkflow ? null : (
              <div className="workflow-note">{workflowDescription}</div>
            )}

            {showRuntimeSettingsInComponents ? <SectionSuspense><WorkflowRuntimeSettingsSection isVisible {...runtimeSettingsProps} /></SectionSuspense> : null}
          </form>
        </section>
      )}
    </div>
  );
}
