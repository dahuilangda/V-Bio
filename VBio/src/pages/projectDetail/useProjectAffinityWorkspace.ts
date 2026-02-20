import { useCallback, useEffect, useMemo } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import { useAffinityWorkflow } from '../../hooks/useAffinityWorkflow';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { InputComponent, ProjectTask } from '../../types/models';
import { applyAffinityChainsToDraftState, applyUseMsaToProteinComponents } from './projectAffinityDraft';
import { readTaskAffinityUploads, resolveAffinityUploadStorageTaskRowId } from './projectTaskSnapshot';

interface UseProjectAffinityWorkspaceInput {
  isAffinityWorkflow: boolean;
  workspaceTab: 'results' | 'basics' | 'components' | 'constraints';
  projectId: string | null;
  draft: any;
  setDraft: Dispatch<SetStateAction<any>>;
  affinityUploadScopeTaskRowId: string | null;
  taskAffinityUploads: Record<string, AffinityPersistedUploads>;
  statusContextTaskRow: ProjectTask | null;
  activeResultTask: ProjectTask | null;
  computeUseMsaFlag: (components: InputComponent[], fallback?: boolean) => boolean;
  rememberAffinityUploadsForTaskRow: (taskRowId: string | null, uploads: AffinityPersistedUploads) => void;
}

export function useProjectAffinityWorkspace({
  isAffinityWorkflow,
  workspaceTab,
  projectId,
  draft,
  setDraft,
  affinityUploadScopeTaskRowId,
  taskAffinityUploads,
  statusContextTaskRow,
  activeResultTask,
  computeUseMsaFlag,
  rememberAffinityUploadsForTaskRow
}: UseProjectAffinityWorkspaceInput) {
  const applyAffinityChainsToDraft = useCallback(
    (targetChainId: string, ligandChainId: string, forceEnable = false) => {
      setDraft((prev: any) => applyAffinityChainsToDraftState(prev, targetChainId, ligandChainId, forceEnable));
    },
    [setDraft]
  );

  const onAffinityChainsResolved = useCallback(
    (targetChainId: string, ligandChainId: string) => {
      applyAffinityChainsToDraft(
        targetChainId,
        ligandChainId,
        String(draft?.backend || 'boltz').trim().toLowerCase() === 'boltz'
      );
    },
    [applyAffinityChainsToDraft, draft?.backend]
  );

  const onAffinityUseMsaChange = useCallback(
    (checked: boolean) => {
      const nextChecked = Boolean(checked);
      setDraft((prev: any) => applyUseMsaToProteinComponents(prev, nextChecked, computeUseMsaFlag));
    },
    [setDraft, computeUseMsaFlag]
  );

  const affinityPersistedUploads = useMemo<AffinityPersistedUploads>(() => {
    if (!isAffinityWorkflow) {
      return { target: null, ligand: null };
    }
    const storageTaskRowId = resolveAffinityUploadStorageTaskRowId(affinityUploadScopeTaskRowId);
    const savedByScope = storageTaskRowId ? taskAffinityUploads[storageTaskRowId] || null : null;
    const sourceTask = statusContextTaskRow || activeResultTask || null;
    const fromTask = sourceTask ? readTaskAffinityUploads(sourceTask) : { target: null, ligand: null };
    return {
      target: savedByScope?.target || fromTask.target,
      ligand: savedByScope?.ligand || fromTask.ligand
    };
  }, [isAffinityWorkflow, taskAffinityUploads, affinityUploadScopeTaskRowId, statusContextTaskRow, activeResultTask]);

  const affinityWorkflow = useAffinityWorkflow({
    enabled: isAffinityWorkflow && workspaceTab === 'components',
    scopeKey: `${projectId || ''}:${affinityUploadScopeTaskRowId}`,
    preferredConfidenceOnly: !Boolean(draft?.inputConfig.properties.affinity),
    persistedLigandSmiles: statusContextTaskRow?.ligand_smiles || activeResultTask?.ligand_smiles || '',
    persistedUploads: affinityPersistedUploads,
    onChainsResolved: onAffinityChainsResolved
  });

  useEffect(() => {
    if (!isAffinityWorkflow || workspaceTab !== 'components') return;
    if (affinityWorkflow.uploadsHydrating) return;
    const storageTaskRowId = resolveAffinityUploadStorageTaskRowId(affinityUploadScopeTaskRowId);
    if (!storageTaskRowId) return;
    rememberAffinityUploadsForTaskRow(storageTaskRowId, affinityWorkflow.persistedUploads);
  }, [
    isAffinityWorkflow,
    workspaceTab,
    affinityUploadScopeTaskRowId,
    rememberAffinityUploadsForTaskRow,
    affinityWorkflow.persistedUploads,
    affinityWorkflow.uploadsHydrating
  ]);

  return {
    ...affinityWorkflow,
    onAffinityUseMsaChange
  };
}
