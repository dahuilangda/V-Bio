import { useMemo } from 'react';
import type { ProteinTemplateUpload } from '../../types/models';
import type { ProjectWorkspaceDraft } from './workspaceTypes';

interface UseProjectDirtyStateInput {
  draft: ProjectWorkspaceDraft | null;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  savedDraftFingerprint: string;
  savedComputationFingerprint: string;
  savedTemplateFingerprint: string;
  createDraftFingerprint: (draft: ProjectWorkspaceDraft) => string;
  createComputationFingerprint: (draft: ProjectWorkspaceDraft) => string;
  createProteinTemplatesFingerprint: (templates: Record<string, ProteinTemplateUpload>) => string;
}

interface UseProjectDirtyStateResult {
  metadataOnlyDraftDirty: boolean;
  hasUnsavedChanges: boolean;
}

export function useProjectDirtyState({
  draft,
  proteinTemplates,
  savedDraftFingerprint,
  savedComputationFingerprint,
  savedTemplateFingerprint,
  createDraftFingerprint,
  createComputationFingerprint,
  createProteinTemplatesFingerprint
}: UseProjectDirtyStateInput): UseProjectDirtyStateResult {
  const currentDraftFingerprint = useMemo(() => (draft ? createDraftFingerprint(draft) : ''), [draft, createDraftFingerprint]);
  const currentComputationFingerprint = useMemo(
    () => (draft ? createComputationFingerprint(draft) : ''),
    [draft, createComputationFingerprint]
  );
  const currentTemplateFingerprint = useMemo(
    () => createProteinTemplatesFingerprint(proteinTemplates),
    [proteinTemplates, createProteinTemplatesFingerprint]
  );
  const isDraftDirty = useMemo(
    () => Boolean(draft) && Boolean(savedDraftFingerprint) && currentDraftFingerprint !== savedDraftFingerprint,
    [draft, savedDraftFingerprint, currentDraftFingerprint]
  );
  const isTemplateDirty = useMemo(
    () => Boolean(draft) && Boolean(savedTemplateFingerprint) && currentTemplateFingerprint !== savedTemplateFingerprint,
    [draft, savedTemplateFingerprint, currentTemplateFingerprint]
  );
  const metadataOnlyDraftDirty = useMemo(
    () =>
      Boolean(draft) &&
      Boolean(savedDraftFingerprint) &&
      currentDraftFingerprint !== savedDraftFingerprint &&
      Boolean(savedComputationFingerprint) &&
      currentComputationFingerprint === savedComputationFingerprint &&
      !isTemplateDirty,
    [
      draft,
      savedDraftFingerprint,
      currentDraftFingerprint,
      savedComputationFingerprint,
      currentComputationFingerprint,
      isTemplateDirty
    ]
  );
  const hasUnsavedChanges = isDraftDirty || isTemplateDirty;

  return {
    metadataOnlyDraftDirty,
    hasUnsavedChanges
  };
}
