import { Dna, Eye, SlidersHorizontal, Target } from 'lucide-react';
import type { WorkspaceTab } from './workspaceTypes';

interface WorkspaceStepperProps {
  workspaceTab: WorkspaceTab;
  onWorkspaceTabChange: (tab: WorkspaceTab) => void;
  isPredictionWorkflow: boolean;
  isAffinityWorkflow: boolean;
  isLeadOptimizationWorkflow: boolean;
  componentStepLabel: string;
}

export function WorkspaceStepper({
  workspaceTab,
  onWorkspaceTabChange,
  isPredictionWorkflow,
  isAffinityWorkflow,
  isLeadOptimizationWorkflow,
  componentStepLabel
}: WorkspaceStepperProps) {
  const componentsLabel = isLeadOptimizationWorkflow ? 'Build' : componentStepLabel;
  const constraintsLabel = isLeadOptimizationWorkflow ? 'Results' : 'Constraints';
  const showConstraintsStep = isPredictionWorkflow;

  return (
    <aside className="workspace-stepper" aria-label="Workspace sections">
      <div className="workspace-stepper-track" aria-hidden="true" />
      <button
        type="button"
        className={`workspace-step ${workspaceTab === 'basics' ? 'active' : ''}`}
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
