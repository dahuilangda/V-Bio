import type { FormEvent } from 'react';
import { ProjectBasicsMetadataForm } from '../../components/project/ProjectBasicsMetadataForm';
import { ProjectResultsSection, type ProjectResultsSectionProps } from '../../components/project/ProjectResultsSection';
import { AffinityWorkflowSection, type AffinityWorkflowSectionProps } from './AffinityWorkflowSection';
import { LeadOptimizationWorkflowSection, type LeadOptimizationWorkflowSectionProps } from './LeadOptimizationWorkflowSection';
import { PredictionWorkflowSection, type PredictionWorkflowSectionProps } from './PredictionWorkflowSection';
import { VirtualScreeningWorkflowSection, type VirtualScreeningWorkflowSectionProps } from './VirtualScreeningWorkflowSection';
import { WorkflowRuntimeSettingsSection, type WorkflowRuntimeSettingsSectionProps } from './WorkflowRuntimeSettingsSection';
import type { WorkspaceTab } from './workspaceTypes';

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
        <LeadOptimizationWorkflowSection
          isVisible
          {...leadOptimizationSectionProps}
        />
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
      {showNativeResults && <ProjectResultsSection {...projectResultsSectionProps} />}

      {showFlatWorkspace && (
        <form className="form-grid" onSubmit={saveDraftAction}>
          {showFlatPredictionWorkspace && <PredictionWorkflowSection isVisible {...predictionSectionProps} />}
          {showFlatVirtualScreeningWorkspace && <VirtualScreeningWorkflowSection isVisible {...virtualScreeningSectionProps} />}
          {showFlatAffinityWorkspace && <AffinityWorkflowSection isVisible {...affinitySectionProps} />}
          {showFlatPredictionWorkspace && showRuntimeSettingsInComponents ? (
            <WorkflowRuntimeSettingsSection isVisible {...runtimeSettingsProps} />
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

            {showAffinitySection ? <AffinityWorkflowSection isVisible {...affinitySectionProps} /> : null}

            {showPredictionSection ? (
              <PredictionWorkflowSection isVisible {...predictionSectionProps} />
            ) : isVirtualScreeningWorkflow && workspaceTab === 'components' ? (
              <VirtualScreeningWorkflowSection isVisible {...virtualScreeningSectionProps} />
            ) : isAffinityWorkflow || isLeadOptimizationWorkflow ? null : (
              <div className="workflow-note">{workflowDescription}</div>
            )}

            {showRuntimeSettingsInComponents ? <WorkflowRuntimeSettingsSection isVisible {...runtimeSettingsProps} /> : null}
          </form>
        </section>
      )}
    </div>
  );
}
