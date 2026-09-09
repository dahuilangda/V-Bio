/**
 * The command column of the API access builder: the copyable command cards
 * (environment, YAML preview, submit, status, result, screening, task
 * action) and the command history list. All command strings and workflow
 * labeling are computed by ApiAccessPage; the ~21 values this column reads
 * arrive as three named prop groups — clipboard, commands, workflow.
 */
import type { Dispatch, SetStateAction } from 'react';
import { CommandItem } from './ApiCommandItem';
import { CommandHistory } from './ApiCommandHistory';
import type {
  AffinityBackend,
  BuilderWorkflowKey,
  CommandHistoryEntry,
  PredictionBackend
} from './apiAccessHelpers';
import type { WorkflowDefinition } from '../utils/workflows';
import './ApiCommandRightColumn.css';

export interface ApiCommandClipboardProps {
  copiedActionId: string;
  copyText: (text: string, okMessage: string, historyLabel?: string, copyId?: string) => Promise<void>;
  commandHistory: CommandHistoryEntry[];
  setCommandHistory: Dispatch<SetStateAction<CommandHistoryEntry[]>>;
  applyCommandHistory: (entry: CommandHistoryEntry) => void;
}

export interface ApiCommandStringsProps {
  commandEnv: string;
  yamlBuilderText: string;
  downloadGeneratedYaml: () => void;
  commandSubmitWithHints: string;
  commandStatus: string;
  commandResults: string;
  commandScreeningResults: string;
  commandTaskAction: string;
}

export interface ApiCommandWorkflowProps {
  isPredictionWorkflow: boolean;
  isVirtualScreeningWorkflow: boolean;
  isSupportedSubmitWorkflow: boolean;
  selectedWorkflow: WorkflowDefinition;
  builderWorkflowKey: BuilderWorkflowKey;
  effectivePredictionBackend: PredictionBackend;
  effectiveAffinityBackend: AffinityBackend;
  builderTaskOperation: 'cancel' | 'delete';
}

interface ApiCommandRightColumnProps {
  clipboard: ApiCommandClipboardProps;
  commands: ApiCommandStringsProps;
  workflow: ApiCommandWorkflowProps;
}

export function ApiCommandRightColumn({
  clipboard,
  commands,
  workflow
}: ApiCommandRightColumnProps) {
  const {
    copiedActionId,
    copyText,
    commandHistory,
    setCommandHistory,
    applyCommandHistory
  } = clipboard;
  const {
    commandEnv,
    yamlBuilderText,
    downloadGeneratedYaml,
    commandSubmitWithHints,
    commandStatus,
    commandResults,
    commandScreeningResults,
    commandTaskAction
  } = commands;
  const {
    isPredictionWorkflow,
    isVirtualScreeningWorkflow,
    isSupportedSubmitWorkflow,
    selectedWorkflow,
    builderWorkflowKey,
    effectivePredictionBackend,
    effectiveAffinityBackend,
    builderTaskOperation
  } = workflow;

  return (
    <div className="api-command-right">
      <div className="api-command-list">
        <CommandItem
          index={1}
          title="Environment"
          command={commandEnv}
          copied={copiedActionId === 'copy-env'}
          onCopy={() => { void copyText(commandEnv, 'Environment command copied.', 'Environment', 'copy-env'); }}
        />

        {(isPredictionWorkflow || isVirtualScreeningWorkflow) && (
          <CommandItem
            index=""
            title="YAML Preview"
            command={yamlBuilderText}
            copied={copiedActionId === 'copy-yaml-preview'}
            onCopy={() => { void copyText(yamlBuilderText, 'Generated YAML copied.', 'YAML Preview', 'copy-yaml-preview'); }}
            extraAction={{ label: 'Download generated YAML', onClick: downloadGeneratedYaml }}
          />
        )}

        <CommandItem
          index={2}
          title={`Submit (${!isSupportedSubmitWorkflow
            ? selectedWorkflow.shortTitle
            : builderWorkflowKey === 'prediction'
              ? `Prediction/${effectivePredictionBackend}`
              : builderWorkflowKey === 'virtual_screening'
                ? 'Virtual Screening/nesso'
                : `Affinity/${effectiveAffinityBackend}`})`}
          command={commandSubmitWithHints}
          copied={copiedActionId === 'copy-submit'}
          onCopy={() => { void copyText(commandSubmitWithHints, 'Submit command copied.', 'Submit', 'copy-submit'); }}
          disabled={!isSupportedSubmitWorkflow}
        >
          {!isSupportedSubmitWorkflow && (
            <p className="muted small">Select a Prediction, Virtual Screening, or Affinity project to generate submit command.</p>
          )}
        </CommandItem>

        <CommandItem
          index={3}
          title="Check Status"
          command={commandStatus}
          copied={copiedActionId === 'copy-status'}
          onCopy={() => { void copyText(commandStatus, 'Status command copied.', 'Status', 'copy-status'); }}
          titleTip="Uses the $TASK_ID captured from the submit response."
        />

        <CommandItem
          index={4}
          title="Download Result"
          command={commandResults}
          copied={copiedActionId === 'copy-result'}
          onCopy={() => { void copyText(commandResults, 'Result command copied.', 'Result', 'copy-result'); }}
        />

        {commandScreeningResults && (
          <CommandItem
            index={5}
            title="Screening Ranking"
            command={commandScreeningResults}
            copied={copiedActionId === 'copy-screening'}
            onCopy={() => { void copyText(commandScreeningResults, 'Screening ranking command copied.', 'Screening Ranking', 'copy-screening'); }}
            hint="compounds[0] is the strongest binder (lowest affinity_pred_value, log10 IC50 in µM)."
          />
        )}

        <CommandItem
          index={commandScreeningResults ? '6' : '5'}
          title={builderTaskOperation === 'delete' ? 'Delete Task' : 'Cancel Task'}
          command={commandTaskAction}
          copied={copiedActionId === 'copy-task-action'}
          onCopy={() => { void copyText(commandTaskAction, 'Task action command copied.', builderTaskOperation === 'delete' ? 'Delete Task' : 'Cancel Task', 'copy-task-action'); }}
          hint={`Operation mode: ${builderTaskOperation}.`}
        />
      </div>

      <CommandHistory
        history={commandHistory}
        copiedId={copiedActionId}
        onCopy={copyText}
        onApply={applyCommandHistory}
        onClear={() => setCommandHistory([])}
      />
    </div>
  );
}
