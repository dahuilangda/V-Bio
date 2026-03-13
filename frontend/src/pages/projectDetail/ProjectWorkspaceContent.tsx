import type { FormEvent } from 'react';
import { ProjectBasicsMetadataForm } from '../../components/project/ProjectBasicsMetadataForm';
import { ProjectResultsSection, type ProjectResultsSectionProps } from '../../components/project/ProjectResultsSection';
import { AffinityWorkflowSection, type AffinityWorkflowSectionProps } from './AffinityWorkflowSection';
import { LeadOptimizationWorkflowSection, type LeadOptimizationWorkflowSectionProps } from './LeadOptimizationWorkflowSection';
import { PredictionWorkflowSection, type PredictionWorkflowSectionProps } from './PredictionWorkflowSection';
import { WorkflowRuntimeSettingsSection, type WorkflowRuntimeSettingsSectionProps } from './WorkflowRuntimeSettingsSection';
import type { WorkspaceTab } from './workspaceTypes';

interface ProjectWorkspaceContentProps {
  workspaceTab: WorkspaceTab;
  componentStepLabel: string;
  projectResultsSectionProps: ProjectResultsSectionProps;
  onSaveDraft: (event: FormEvent<HTMLFormElement>) => void;
  canEdit: boolean;
  taskName: string;
  taskSummary: string;
  onTaskNameChange: (value: string) => void;
  onTaskSummaryChange: (value: string) => void;
  affinitySectionProps: Omit<AffinityWorkflowSectionProps, 'visible'>;
  leadOptimizationSectionProps: Omit<LeadOptimizationWorkflowSectionProps, 'visible'>;
  isPredictionWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  predictionSectionProps: Omit<PredictionWorkflowSectionProps, 'visible'>;
  workflowDescription: string;
  runtimeSettingsProps: Omit<WorkflowRuntimeSettingsSectionProps, 'visible'>;
}

export function ProjectWorkspaceContent({
  workspaceTab,
  componentStepLabel,
  projectResultsSectionProps,
  onSaveDraft,
  canEdit,
  taskName,
  taskSummary,
  onTaskNameChange,
  onTaskSummaryChange,
  affinitySectionProps,
  leadOptimizationSectionProps,
  isPredictionWorkflow,
  isAffinityWorkflow,
  isLeadOptimizationWorkflow,
  predictionSectionProps,
  workflowDescription,
  runtimeSettingsProps
}: ProjectWorkspaceContentProps) {
  const showLeadOptWorkspace = isLeadOptimizationWorkflow && (workspaceTab === 'components' || workspaceTab === 'results');
  const showNativeResults = workspaceTab === 'results' && !isLeadOptimizationWorkflow;
  const showFlatPredictionWorkspace =
    isPredictionWorkflow && (workspaceTab === 'components' || workspaceTab === 'constraints');
  const showFlatAffinityWorkspace = isAffinityWorkflow && workspaceTab === 'components';
  const showFlatWorkspace = showFlatPredictionWorkspace || showFlatAffinityWorkspace;
  const isPeptideDesignWorkflow = runtimeSettingsProps.isPeptideDesignWorkflow;
  const showRuntimeSettingsInComponents = workspaceTab === 'components' && !isLeadOptimizationWorkflow && !isPeptideDesignWorkflow;
  const showPeptideModeInBasics = workspaceTab === 'basics' && isPeptideDesignWorkflow;
  const showPredictionSection = isPredictionWorkflow && (workspaceTab === 'components' || workspaceTab === 'constraints');
  const showAffinitySection = workspaceTab === 'components' && isAffinityWorkflow;

  if (showLeadOptWorkspace) {
    return (
      <div className="workspace-content workspace-content--lead-opt">
        <LeadOptimizationWorkflowSection
          visible
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
        <form className="form-grid" onSubmit={onSaveDraft}>
          {showFlatPredictionWorkspace && <PredictionWorkflowSection visible {...predictionSectionProps} />}
          {showFlatAffinityWorkspace && <AffinityWorkflowSection visible {...affinitySectionProps} />}
          {showFlatPredictionWorkspace && showRuntimeSettingsInComponents ? (
            <WorkflowRuntimeSettingsSection visible {...runtimeSettingsProps} />
          ) : null}
        </form>
      )}

      {!showFlatWorkspace && (workspaceTab !== 'results' || isLeadOptimizationWorkflow) && (
        <section className="panel inputs-panel">
          {showWorkspaceTitle ? <h2>{workspaceTitle}</h2> : null}

          <form className="form-grid" onSubmit={onSaveDraft}>
            {workspaceTab === 'basics' && (
              <ProjectBasicsMetadataForm
                canEdit={canEdit}
                taskName={taskName}
                taskSummary={taskSummary}
                onTaskNameChange={onTaskNameChange}
                onTaskSummaryChange={onTaskSummaryChange}
              />
            )}

            {showAffinitySection ? <AffinityWorkflowSection visible {...affinitySectionProps} /> : null}

            {showPredictionSection ? (
              <PredictionWorkflowSection visible {...predictionSectionProps} />
            ) : isAffinityWorkflow || isLeadOptimizationWorkflow ? null : (
              <div className="workflow-note">{workflowDescription}</div>
            )}

            {showRuntimeSettingsInComponents ? <WorkflowRuntimeSettingsSection visible {...runtimeSettingsProps} /> : null}

            {showPeptideModeInBasics ? (
              <WorkflowRuntimeSettingsSection
                visible
                displayMode="peptide_mode_only"
                {...runtimeSettingsProps}
              />
            ) : null}
          </form>
        </section>
      )}
    </div>
  );
}
