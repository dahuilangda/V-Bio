import { useEffect } from 'react';
import type { Dispatch, MutableRefObject, SetStateAction } from 'react';
import type {
  InputComponent,
  PredictionConstraint,
  ProjectInputConfig,
  ProteinTemplateUpload,
} from '../../types/models';
import { PEPTIDE_DESIGNED_LIGAND_TOKEN } from '../../utils/projectInputs';
import { isPredictionLikeWorkflowKey } from '../../utils/workflows';
import { filterConstraintsByBackend } from './projectDraftUtils';

interface DraftLike {
  backend: string;
  inputConfig: {
    components: InputComponent[];
    constraints: PredictionConstraint[];
    properties: ProjectInputConfig['properties'];
  };
}

interface UseProjectDraftSynchronizersOptions<TDraft extends DraftLike> {
  draft: TDraft | null;
  setDraft: Dispatch<SetStateAction<TDraft | null>>;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  setProteinTemplates: Dispatch<SetStateAction<Record<string, ProteinTemplateUpload>>>;
  activeConstraintId: string | null;
  setActiveConstraintId: Dispatch<SetStateAction<string | null>>;
  selectedContactConstraintIds: string[];
  setSelectedContactConstraintIds: Dispatch<SetStateAction<string[]>>;
  constraintSelectionAnchorRef: MutableRefObject<string | null>;
  constraintPickModeEnabled: boolean;
  constraintPickSlotRef: MutableRefObject<Record<string, 'first' | 'second'>>;
  activeComponentId: string | null;
  setActiveComponentId: Dispatch<SetStateAction<string | null>>;
  workflowKey: string;
  isPeptideDesignWorkflow: boolean;
  selectedWorkspaceLigandChainId: string | null;
  selectedWorkspaceTargetChainId: string | null;
  canEnableAffinityFromWorkspace: boolean;
}

export function useProjectDraftSynchronizers<TDraft extends DraftLike>({
  draft,
  setDraft,
  proteinTemplates,
  setProteinTemplates,
  activeConstraintId,
  setActiveConstraintId,
  selectedContactConstraintIds,
  setSelectedContactConstraintIds,
  constraintSelectionAnchorRef,
  constraintPickModeEnabled,
  constraintPickSlotRef,
  activeComponentId,
  setActiveComponentId,
  workflowKey,
  isPeptideDesignWorkflow,
  selectedWorkspaceLigandChainId,
  selectedWorkspaceTargetChainId,
  canEnableAffinityFromWorkspace,
}: UseProjectDraftSynchronizersOptions<TDraft>): void {
  useEffect(() => {
    if (!draft) return;
    const validProteinIds = new Set(
      draft.inputConfig.components.filter((component) => component.type === 'protein').map((component) => component.id)
    );
    const hasInvalidTemplate = Object.keys(proteinTemplates).some((componentId) => !validProteinIds.has(componentId));
    if (!hasInvalidTemplate) return;

    setProteinTemplates((prev) => {
      let changed = false;
      const next: Record<string, ProteinTemplateUpload> = {};
      for (const [componentId, upload] of Object.entries(prev)) {
        if (validProteinIds.has(componentId)) {
          next[componentId] = upload;
        } else {
          changed = true;
        }
      }
      return changed ? next : prev;
    });
  }, [draft, proteinTemplates, setProteinTemplates]);

  useEffect(() => {
    if (!draft) return;
    const nextConstraints = filterConstraintsByBackend(draft.inputConfig.constraints, draft.backend);
    if (nextConstraints.length === draft.inputConfig.constraints.length) return;

    const keptIds = new Set(nextConstraints.map((item) => item.id));
    const filteredSelectedContactIds = selectedContactConstraintIds.filter((id) => keptIds.has(id));
    setDraft((prev) =>
      prev
        ? {
            ...prev,
            inputConfig: {
              ...prev.inputConfig,
              constraints: nextConstraints
            }
          }
        : prev
    );
    if (
      filteredSelectedContactIds.length !== selectedContactConstraintIds.length ||
      filteredSelectedContactIds.some((id, index) => id !== selectedContactConstraintIds[index])
    ) {
      setSelectedContactConstraintIds(filteredSelectedContactIds);
    }
    if (activeConstraintId && !keptIds.has(activeConstraintId)) {
      setActiveConstraintId(null);
    }
    if (constraintSelectionAnchorRef.current && !keptIds.has(constraintSelectionAnchorRef.current)) {
      constraintSelectionAnchorRef.current = null;
    }
  }, [
    draft,
    activeConstraintId,
    selectedContactConstraintIds,
    setDraft,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
  ]);

  useEffect(() => {
    if (!draft) return;
    const ids = draft.inputConfig.constraints.map((item) => item.id);
    if (ids.length === 0) {
      if (activeConstraintId !== null) setActiveConstraintId(null);
      if (selectedContactConstraintIds.length > 0) {
        setSelectedContactConstraintIds([]);
      }
      constraintSelectionAnchorRef.current = null;
      return;
    }
    if (activeConstraintId && !ids.includes(activeConstraintId)) {
      setActiveConstraintId(ids[0]);
    }
    const validContactIds = new Set(
      draft.inputConfig.constraints.filter((item) => item.type === 'contact').map((item) => item.id)
    );
    const filtered = selectedContactConstraintIds.filter((id) => validContactIds.has(id));
    if (
      filtered.length !== selectedContactConstraintIds.length ||
      filtered.some((id, index) => id !== selectedContactConstraintIds[index])
    ) {
      setSelectedContactConstraintIds(filtered);
    }
    if (constraintSelectionAnchorRef.current && !validContactIds.has(constraintSelectionAnchorRef.current)) {
      constraintSelectionAnchorRef.current = null;
    }
  }, [
    draft,
    activeConstraintId,
    selectedContactConstraintIds,
    setActiveConstraintId,
    setSelectedContactConstraintIds,
    constraintSelectionAnchorRef,
  ]);

  useEffect(() => {
    if (!constraintPickModeEnabled || !activeConstraintId) return;
    constraintPickSlotRef.current[activeConstraintId] = 'first';
  }, [constraintPickModeEnabled, activeConstraintId, constraintPickSlotRef]);

  useEffect(() => {
    if (!draft) return;
    const ids = draft.inputConfig.components.map((item) => item.id);
    if (ids.length === 0) {
      if (activeComponentId !== null) setActiveComponentId(null);
      return;
    }
    if (!activeComponentId || !ids.includes(activeComponentId)) {
      setActiveComponentId(ids[0]);
    }
  }, [draft, activeComponentId, setActiveComponentId]);

  useEffect(() => {
    if (!isPredictionLikeWorkflowKey(workflowKey)) return;
    if (!draft) return;
    const props = draft.inputConfig.properties;
    const nextLigand = isPeptideDesignWorkflow
      ? PEPTIDE_DESIGNED_LIGAND_TOKEN
      : selectedWorkspaceLigandChainId || null;
    const nextTarget = selectedWorkspaceTargetChainId;
    const nextAffinity = isPeptideDesignWorkflow ? false : canEnableAffinityFromWorkspace ? props.affinity : false;
    const nextProps = {
      ...props,
      affinity: nextAffinity,
      target: nextTarget,
      ligand: nextLigand,
      binder: nextLigand
    };

    if (
      nextProps.affinity !== props.affinity ||
      nextProps.target !== props.target ||
      nextProps.ligand !== props.ligand ||
      nextProps.binder !== props.binder
    ) {
      setDraft((prev) =>
        prev
          ? {
              ...prev,
              inputConfig: {
                ...prev.inputConfig,
                properties: nextProps
              }
            }
          : prev
      );
    }
  }, [
    draft,
    selectedWorkspaceLigandChainId,
    selectedWorkspaceTargetChainId,
    canEnableAffinityFromWorkspace,
    isPeptideDesignWorkflow,
    workflowKey,
    setDraft,
  ]);
}
