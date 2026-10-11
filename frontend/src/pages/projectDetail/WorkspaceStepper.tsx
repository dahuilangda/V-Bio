import { Dna, Eye, SlidersHorizontal, Target } from 'lucide-react';
import type { WorkspaceTab } from './workspaceTypes';

export interface WorkspaceStepperProps {
  workspaceTab: WorkspaceTab;
  onWorkspaceTabChange: (tab: WorkspaceTab) => void;
  isPredictionWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  constraintsSupported: boolean;
  componentStepLabel: string;
}

export function WorkspaceStepper({
  workspaceTab,
  onWorkspaceTabChange,
  isPredictionWorkflow,
  isAffinityWorkflow,
  isLeadOptimizationWorkflow,
  constraintsSupported,
  componentStepLabel
}: WorkspaceStepperProps) {
  const componentsLabel = isLeadOptimizationWorkflow ? 'Build' : componentStepLabel;
  const constraintsLabel = isLeadOptimizationWorkflow ? 'Results' : 'Constraints';
  const showConstraintsStep = isPredictionWorkflow && constraintsSupported;

  return (
    <aside className="workspace-stepper" aria-label="Workspace sections">
      <div className="workspace-stepper-track" aria-hidden="true" />
      <button
        type="button"
        className={`workspace-step ${workspaceTab === 'basics' ? 'active' : ''}`}
          aria-current={workspaceTab === 'basics' ? 'step' : undefined}
        onClick={() => onWorkspaceTabChange('basics')}
        aria-label="Edit basics"
        data-label="Basics"
        title="Basics"
      >
        <span className="workspace-step-dot">
          <SlidersHorizontal size={13} />
        </span>
      </button>
      {(isPredictionWorkflow || isAffinityWorkflow || isLeadOptimizationWorkflow) && (
        <button
          type="button"
          className={`workspace-step ${workspaceTab === 'components' ? 'active' : ''}`}
          aria-current={workspaceTab === 'components' ? 'step' : undefined}
          onClick={() => onWorkspaceTabChange('components')}
          aria-label={`Edit ${componentsLabel.toLowerCase()}`}
          data-label={componentsLabel}
          title={componentsLabel}
        >
          <span className="workspace-step-dot">
            <Dna size={13} />
          </span>
        </button>
      )}
      {showConstraintsStep && (
        <button
          type="button"
          className={`workspace-step ${workspaceTab === 'constraints' ? 'active' : ''}`}
          aria-current={workspaceTab === 'constraints' ? 'step' : undefined}
          onClick={() => onWorkspaceTabChange('constraints')}
          aria-label={`Edit ${constraintsLabel.toLowerCase()}`}
          data-label={constraintsLabel}
          title={constraintsLabel}
        >
          <span className="workspace-step-dot">
            <Target size={13} />
          </span>
        </button>
      )}
      <button
        type="button"
        className={`workspace-step ${workspaceTab === 'results' ? 'active' : ''}`}
          aria-current={workspaceTab === 'results' ? 'step' : undefined}
        onClick={() => onWorkspaceTabChange('results')}
        aria-label="View current result"
          data-label="Results"
          title="Results"
      >
        <span className="workspace-step-dot">
          <Eye size={13} />
        </span>
      </button>
    </aside>
  );
}
