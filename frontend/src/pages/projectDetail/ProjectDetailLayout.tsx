import type { FormEvent, MouseEvent, RefObject } from 'react';
import type { AffinityWorkflowSectionProps } from './AffinityWorkflowSection';
import type { LeadOptimizationWorkflowSectionProps } from './LeadOptimizationWorkflowSection';
import type { PredictionWorkflowSectionProps } from './PredictionWorkflowSection';
import { ProjectHeaderActions } from './ProjectHeaderActions';
import { ProjectHeaderMeta } from './ProjectHeaderMeta';
import { ProjectWorkspaceContent } from './ProjectWorkspaceContent';
import type { WorkflowRuntimeSettingsSectionProps } from './WorkflowRuntimeSettingsSection';
import { RunFeedbackOverlays } from './RunFeedbackOverlays';
import { WorkspaceStepper } from './WorkspaceStepper';
import type { ProjectResultsSectionProps } from '../../components/project/ProjectResultsSection';
import type { WorkspaceTab } from './workspaceTypes';

interface WorkflowDescriptor {
  shortTitle: string;
  runLabel: string;
  description: string;
}

export interface ProjectDetailLayoutProps {
  projectName: string;
  canDownloadResult: boolean;
  workflow: WorkflowDescriptor;
  workspaceTab: WorkspaceTab;
  componentStepLabel: string;
  taskName: string;
  taskSummary: string;
  isPredictionWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  displayTaskState: string;
  isActiveRuntime: boolean;
  progressPercent: number;
  waitingSeconds: number | null;
  totalRuntimeSeconds: number | null;
  canEdit: boolean;
  loading: boolean;
  saving: boolean;
  submitting: boolean;
  runSubmitting: boolean;
  hasUnsavedChanges: boolean;
  runMenuOpen: boolean;
  runDisabled: boolean;
  runBlockedReason: string;
  isRunRedirecting: boolean;
  canOpenRunMenu: boolean;
  showHeaderRunAction: boolean;
  showQuickRunFab: boolean;
  taskHistoryPath: string;
  runSuccessNotice: string | null;
  error: string | null;
  resultError: string | null;
  affinityPreviewError: string | null;
  resultChainConsistencyWarning: string | null;
  projectResultsSectionProps: ProjectResultsSectionProps;
  affinitySectionProps: Omit<AffinityWorkflowSectionProps, 'visible'>;
  leadOptimizationSectionProps: Omit<LeadOptimizationWorkflowSectionProps, 'visible'>;
  predictionSectionProps: Omit<PredictionWorkflowSectionProps, 'visible'>;
  runtimeSettingsProps: Omit<WorkflowRuntimeSettingsSectionProps, 'visible'>;
  runActionRef: RefObject<HTMLDivElement>;
  topRunButtonRef: RefObject<HTMLButtonElement>;
  onOpenTaskHistory: (event: MouseEvent<HTMLElement>) => void;
  onDownloadResult: () => void;
  onSaveDraft: () => void;
  onReset: () => void;
  onRunAction: () => void;
  onRestoreSavedDraft: () => void;
  onRunCurrentDraft: () => void;
  showStopAction?: boolean;
  stopSubmitting?: boolean;
  stopDisabled?: boolean;
  stopTitle?: string;
  onStopAction?: () => void;
  onWorkspaceTabChange: (tab: WorkspaceTab) => void;
  onTaskNameChange: (value: string) => void;
  onTaskSummaryChange: (value: string) => void;
  onWorkspaceFormSubmit: (event: FormEvent<HTMLFormElement>) => void;
}

export function ProjectDetailLayout({
  projectName,
  canDownloadResult,
  workflow,
  workspaceTab,
  componentStepLabel,
  taskName,
  taskSummary,
  isPredictionWorkflow,
  isAffinityWorkflow,
  isLeadOptimizationWorkflow,
  displayTaskState,
  isActiveRuntime,
  progressPercent,
  waitingSeconds,
  totalRuntimeSeconds,
  canEdit,
  loading,
  saving,
  submitting,
  runSubmitting,
  hasUnsavedChanges,
  runMenuOpen,
  runDisabled,
  runBlockedReason,
  isRunRedirecting,
  canOpenRunMenu,
  showHeaderRunAction,
  showQuickRunFab,
  taskHistoryPath,
  runSuccessNotice,
  error,
  resultError,
  affinityPreviewError,
  resultChainConsistencyWarning,
  projectResultsSectionProps,
  affinitySectionProps,
  leadOptimizationSectionProps,
  predictionSectionProps,
  runtimeSettingsProps,
  runActionRef,
  topRunButtonRef,
  onOpenTaskHistory,
  onDownloadResult,
  onSaveDraft,
  onReset,
  onRunAction,
  onRestoreSavedDraft,
  onRunCurrentDraft,
  showStopAction = false,
  stopSubmitting = false,
  stopDisabled = false,
  stopTitle = '',
  onStopAction,
  onWorkspaceTabChange,
  onTaskNameChange,
  onTaskSummaryChange,
  onWorkspaceFormSubmit
}: ProjectDetailLayoutProps) {
  return (
    <div className="page-grid project-detail">
      <section className="page-header">
        <ProjectHeaderMeta
          projectName={projectName}
          displayTaskState={displayTaskState}
          workflowShortTitle={workflow.shortTitle}
          isActiveRuntime={isActiveRuntime}
          progressPercent={progressPercent}
          waitingSeconds={waitingSeconds}
          totalRuntimeSeconds={totalRuntimeSeconds}
        />

        <ProjectHeaderActions
          taskHistoryPath={taskHistoryPath}
          onOpenTaskHistory={onOpenTaskHistory}
          onDownloadResult={onDownloadResult}
          canDownloadResult={canDownloadResult}
          onSaveDraft={onSaveDraft}
          canEdit={canEdit}
          saving={saving}
          hasUnsavedChanges={hasUnsavedChanges}
          onReset={onReset}
          loading={loading}
          submitting={submitting}
          runSubmitting={runSubmitting}
          runActionRef={runActionRef}
          topRunButtonRef={topRunButtonRef}
          onRunAction={onRunAction}
          runDisabled={runDisabled}
          runBlockedReason={runBlockedReason}
          workflowRunLabel={workflow.runLabel}
          isRunRedirecting={isRunRedirecting}
          canOpenRunMenu={canOpenRunMenu}
          runMenuOpen={runMenuOpen}
          onRestoreSavedDraft={onRestoreSavedDraft}
          onRunCurrentDraft={onRunCurrentDraft}
          showRunAction={showHeaderRunAction}
          showStopAction={showStopAction}
          stopSubmitting={stopSubmitting}
          stopDisabled={stopDisabled}
          stopTitle={stopTitle}
          onStopAction={onStopAction}
        />
      </section>

      <RunFeedbackOverlays
        runSuccessNotice={runSuccessNotice}
        taskHistoryPath={taskHistoryPath}
        onOpenTaskHistory={onOpenTaskHistory}
        isRunRedirecting={isRunRedirecting}
        showQuickRunFab={showQuickRunFab}
        onRunAction={onRunAction}
        runDisabled={runDisabled}
        runBlockedReason={runBlockedReason}
        workflowRunLabel={workflow.runLabel}
        submitting={runSubmitting}
        error={error}
        resultError={resultError}
        affinityPreviewError={affinityPreviewError}
        resultChainConsistencyWarning={resultChainConsistencyWarning}
      />

      <div className="workspace-shell">
        <WorkspaceStepper
          workspaceTab={workspaceTab}
          onWorkspaceTabChange={onWorkspaceTabChange}
          isPredictionWorkflow={isPredictionWorkflow}
          isAffinityWorkflow={isAffinityWorkflow}
          isLeadOptimizationWorkflow={isLeadOptimizationWorkflow}
          componentStepLabel={componentStepLabel}
        />

        <ProjectWorkspaceContent
          workspaceTab={workspaceTab}
          componentStepLabel={componentStepLabel}
          projectResultsSectionProps={projectResultsSectionProps}
          onSaveDraft={onWorkspaceFormSubmit}
          canEdit={canEdit}
          taskName={taskName}
          taskSummary={taskSummary}
          onTaskNameChange={onTaskNameChange}
          onTaskSummaryChange={onTaskSummaryChange}
          affinitySectionProps={affinitySectionProps}
          leadOptimizationSectionProps={leadOptimizationSectionProps}
          isPredictionWorkflow={isPredictionWorkflow}
          isAffinityWorkflow={isAffinityWorkflow}
          isLeadOptimizationWorkflow={isLeadOptimizationWorkflow}
          predictionSectionProps={predictionSectionProps}
          workflowDescription={workflow.description}
          runtimeSettingsProps={runtimeSettingsProps}
        />
      </div>
    </div>
  );
}
