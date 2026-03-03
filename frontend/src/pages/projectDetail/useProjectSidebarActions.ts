import type { Dispatch, SetStateAction } from 'react';
import type { InputComponent } from '../../types/models';
import {
  addComponentToDraftAction,
  addConstraintFromSidebarAction,
  jumpToComponentAction,
  setAffinityComponentFromWorkspaceAction,
  setAffinityEnabledFromWorkspaceAction
} from './editorActions';
import { buildDefaultConstraint } from './constraintWorkspaceUtils';
import type { ProjectWorkspaceDraft, WorkspaceTab } from './workspaceTypes';

interface UseProjectSidebarActionsInput {
  draft: ProjectWorkspaceDraft | null;
  setDraft: Dispatch<SetStateAction<ProjectWorkspaceDraft | null>>;
  setWorkspaceTab: Dispatch<SetStateAction<WorkspaceTab>>;
  setActiveComponentId: Dispatch<SetStateAction<string | null>>;
  setSidebarTypeOpen: Dispatch<SetStateAction<Record<InputComponent['type'], boolean>>>;
  normalizedDraftComponents: InputComponent[];
  setSidebarConstraintsOpen: Dispatch<SetStateAction<boolean>>;
  setActiveConstraintId: Dispatch<SetStateAction<string | null>>;
  setSelectedContactConstraintIds: Dispatch<SetStateAction<string[]>>;
  constraintSelectionAnchorRef: { current: string | null };
  activeChainInfos: Array<{ id: string }>;
  ligandChainOptions: Array<{ id: string }>;
  isBondOnlyBackend: boolean;
  canEnableAffinityFromWorkspace: boolean;
  workspaceTargetOptions: Array<{ componentId: string; chainId: string }>;
  workspaceLigandSelectableOptions: Array<{ componentId: string; chainId: string }>;
  createInputComponent: (type: InputComponent['type']) => InputComponent;
}

interface UseProjectSidebarActionsResult {
  addComponentToDraft: (type: InputComponent['type']) => void;
  addConstraintFromSidebar: () => void;
  setAffinityEnabledFromWorkspace: (enabled: boolean) => void;
  setAffinityComponentFromWorkspace: (field: 'target' | 'ligand', componentId: string | null) => void;
  jumpToComponent: (componentId: string) => void;
}

export function useProjectSidebarActions({
  draft,
  setDraft,
  setWorkspaceTab,
  setActiveComponentId,
  setSidebarTypeOpen,
  normalizedDraftComponents,
  setSidebarConstraintsOpen,
  setActiveConstraintId,
  setSelectedContactConstraintIds,
  constraintSelectionAnchorRef,
  activeChainInfos,
  ligandChainOptions,
  isBondOnlyBackend,
  canEnableAffinityFromWorkspace,
  workspaceTargetOptions,
  workspaceLigandSelectableOptions,
  createInputComponent
}: UseProjectSidebarActionsInput): UseProjectSidebarActionsResult {
  const addComponentToDraft = (type: InputComponent['type']) => {
    addComponentToDraftAction({
      type,
      createInputComponent,
      setWorkspaceTab,
      setActiveComponentId,
      setSidebarTypeOpen,
      setDraft
    });
  };

  const addConstraintFromSidebar = () => {
    addConstraintFromSidebarAction({
      draft,
      buildDefaultConstraint: (preferredType) =>
        buildDefaultConstraint({
          preferredType,
          activeChainInfos,
          ligandChainOptions,
          isBondOnlyBackend
        }),
      isBondOnlyBackend,
      setWorkspaceTab,
      setSidebarConstraintsOpen,
      setActiveConstraintId,
      setSelectedContactConstraintIds,
      constraintSelectionAnchorRef,
      setDraft
    });
  };

  const setAffinityEnabledFromWorkspace = (enabled: boolean) => {
    setAffinityEnabledFromWorkspaceAction({
      enabled,
      canEnableAffinityFromWorkspace,
      setDraft
    });
  };

  const setAffinityComponentFromWorkspace = (field: 'target' | 'ligand', componentId: string | null) => {
    setAffinityComponentFromWorkspaceAction({
      field,
      componentId,
      workspaceTargetOptions,
      workspaceLigandSelectableOptions,
      setDraft
    });
  };

  const jumpToComponent = (componentId: string) => {
    jumpToComponentAction({
      componentId,
      setWorkspaceTab,
      setActiveComponentId,
      normalizedDraftComponents,
      setSidebarTypeOpen
    });
  };

  return {
    addComponentToDraft,
    addConstraintFromSidebar,
    setAffinityEnabledFromWorkspace,
    setAffinityComponentFromWorkspace,
    jumpToComponent
  };
}
