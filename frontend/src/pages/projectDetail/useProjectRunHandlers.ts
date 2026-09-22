import type { FormEvent, MouseEvent } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { NavigateFunction } from 'react-router-dom';
import {
  handleResetFromHeader as handleResetFromHeaderAction,
  handleRestoreSavedDraft as handleRestoreSavedDraftAction,
  handleRunAction as handleRunActionControl,
  handleRunCurrentDraft as handleRunCurrentDraftControl,
} from './runControls';
import { handleOpenTaskHistoryAction } from './editorActions';

interface UseProjectRunHandlersOptions {
  runDisabled: boolean;
  submitTask: () => Promise<void>;
  setRunMenuOpen: Dispatch<SetStateAction<boolean>>;
  loadProject: () => Promise<void>;
  saving: boolean;
  submitting: boolean;
  loading: boolean;
  hasUnsavedChanges: boolean;
  saveDraft: (event?: FormEvent<HTMLFormElement>) => Promise<void>;
  taskHistoryPath: string;
  setRunRedirectTaskId: Dispatch<SetStateAction<string | null>>;
  navigate: NavigateFunction;
}

interface UseProjectRunHandlersResult {
  handleRunAction: () => void;
  handleConfirmedRunAction: (notifyEmailOverride?: string) => void;
  handleRunCurrentDraft: () => void;
  handleRestoreSavedDraft: () => void;
  handleResetFromHeader: () => void;
  handleWorkspaceFormSubmit: (event: FormEvent<HTMLFormElement>) => void;
  handleOpenTaskHistory: (event: MouseEvent<HTMLElement>) => void;
}

export function useProjectRunHandlers({
  runDisabled,
  submitTask,
  setRunMenuOpen,
  loadProject,
  saving,
  submitting,
  loading,
  hasUnsavedChanges,
  saveDraft,
  taskHistoryPath,
  setRunRedirectTaskId,
  navigate,
}: UseProjectRunHandlersOptions): UseProjectRunHandlersResult {
  const handleRunAction = () => {
    // unsaved edits: let the user choose which version to run before
    // consuming compute (the "(has unsaved changes)" hint advertises this menu)
    if (hasUnsavedChanges && !runDisabled && !submitting && !saving && !loading) {
      setRunMenuOpen(true);
      return;
    }
    handleRunActionControl({ runDisabled, submitTask });
  };

  // the confirm dialog IS the run confirmation: submit the current draft
  // directly — handleRunAction would open the unsaved-changes menu under the
  // modal and lock the dialog
  const handleConfirmedRunAction = (notifyEmailOverride?: string) => {
    handleRunActionControl({ runDisabled, submitTask, notifyEmailOverride });
  };

  const handleRunCurrentDraft = () => {
    handleRunCurrentDraftControl({
      setRunMenuOpen,
      submitTask
    });
  };

  const handleRestoreSavedDraft = () => {
    handleRestoreSavedDraftAction({
      setRunMenuOpen,
      loadProject
    });
  };

  const handleResetFromHeader = () => {
    handleResetFromHeaderAction({
      saving,
      submitting,
      loading,
      hasUnsavedChanges,
      onRestore: handleRestoreSavedDraft
    });
  };

  const handleWorkspaceFormSubmit = (event: FormEvent<HTMLFormElement>) => {
    void saveDraft(event);
  };

  const handleOpenTaskHistory = (event: MouseEvent<HTMLElement>) => {
    handleOpenTaskHistoryAction({
      event,
      taskHistoryPath,
      setRunRedirectTaskId,
      navigate
    });
  };

  return {
    handleRunAction,
    handleConfirmedRunAction,
    handleRunCurrentDraft,
    handleRestoreSavedDraft,
    handleResetFromHeader,
    handleWorkspaceFormSubmit,
    handleOpenTaskHistory,
  };
}
