/**
 * The command column of the API access builder: the copyable command cards
 * (environment, YAML preview, submit, status, result, screening, task
 * action) and the command history list. All command strings and workflow
 * labeling are computed by ApiAccessPage; props arrive as three named
 * groups — clipboard, commands, workflow.
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
  copyTextAction: (text: string, okMessage: string, historyLabel?: string, copyId?: string) => Promise<void>;
  commandHistory: CommandHistoryEntry[];
  setCommandHistory: Dispatch<SetStateAction<CommandHistoryEntry[]>>;
  onApplyCommandHistory: (entry: CommandHistoryEntry) => void;
}

export interface ApiCommandStringsProps {
  commandEnv: string;
  yamlBuilderText: string;
  onDownloadGeneratedYaml: () => void;
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
    copyTextAction,
    commandHistory,
    setCommandHistory,
    onApplyCommandHistory
  } = clipboard;
  const {
    commandEnv,
    yamlBuilderText,
    onDownloadGeneratedYaml,
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
          isCopied={copiedActionId === 'copy-env'}
          onCopy={() => { void copyTextAction(commandEnv, 'Environment command copied.', 'Environment', 'copy-env'); }}
        />

        {(isPredictionWorkflow || isVirtualScreeningWorkflow) && (
          <CommandItem
            index=""
            title="YAML Preview"
            command={yamlBuilderText}
            isCopied={copiedActionId === 'copy-yaml-preview'}
            onCopy={() => { void copyTextAction(yamlBuilderText, 'Generated YAML copied.', 'YAML Preview', 'copy-yaml-preview'); }}
            extraAction={{ label: 'Download generated YAML', onClick: onDownloadGeneratedYaml }}
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
          isCopied={copiedActionId === 'copy-submit'}
          onCopy={() => { void copyTextAction(commandSubmitWithHints, 'Submit command copied.', 'Submit', 'copy-submit'); }}
          isDisabled={!isSupportedSubmitWorkflow}
        >
          {!isSupportedSubmitWorkflow && (
            <p className="muted small">Select a Prediction, Virtual Screening, or Affinity project to generate submit command.</p>
          )}
        </CommandItem>

        <CommandItem
          index={3}
          title="Check Status"
          command={commandStatus}
          isCopied={copiedActionId === 'copy-status'}
          onCopy={() => { void copyTextAction(commandStatus, 'Status command copied.', 'Status', 'copy-status'); }}
          titleTip="Uses the $TASK_ID captured from the submit response."
        />

        <CommandItem
          index={4}
          title="Download Result"
          command={commandResults}
          isCopied={copiedActionId === 'copy-result'}
          onCopy={() => { void copyTextAction(commandResults, 'Result command copied.', 'Result', 'copy-result'); }}
        />

        {commandScreeningResults && (
          <CommandItem
            index={5}
            title="Screening Ranking"
            command={commandScreeningResults}
            isCopied={copiedActionId === 'copy-screening'}
            onCopy={() => { void copyTextAction(commandScreeningResults, 'Screening ranking command copied.', 'Screening Ranking', 'copy-screening'); }}
            hint="compounds[0] is the strongest binder (lowest affinity_pred_value, log10 IC50 in µM)."
          />
        )}

        <CommandItem
          index={commandScreeningResults ? '6' : '5'}
          title={builderTaskOperation === 'delete' ? 'Delete Task' : 'Cancel Task'}
          command={commandTaskAction}
          isCopied={copiedActionId === 'copy-task-action'}
          onCopy={() => { void copyTextAction(commandTaskAction, 'Task action command copied.', builderTaskOperation === 'delete' ? 'Delete Task' : 'Cancel Task', 'copy-task-action'); }}
          hint={`Operation mode: ${builderTaskOperation}.`}
        />
      </div>

      <CommandHistory
        history={commandHistory}
        copiedId={copiedActionId}
        onCopy={copyTextAction}
        onApply={onApplyCommandHistory}
        onClear={() => setCommandHistory([])}
      />
    </div>
  );
}
