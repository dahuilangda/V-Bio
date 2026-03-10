import { useMemo } from 'react';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import type { ProteinTemplateUpload } from '../../types/models';
import type { ProjectWorkspaceDraft } from './workspaceTypes';

interface UseProjectDirtyStateInput {
  draft: ProjectWorkspaceDraft | null;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  affinityUploads: AffinityPersistedUploads;
  savedDraftFingerprint: string;
  savedComputationFingerprint: string;
  savedTemplateFingerprint: string;
  savedAffinityUploadsFingerprint: string;
  createDraftFingerprint: (draft: ProjectWorkspaceDraft) => string;
  createComputationFingerprint: (draft: ProjectWorkspaceDraft) => string;
  createProteinTemplatesFingerprint: (templates: Record<string, ProteinTemplateUpload>) => string;
  createAffinityUploadsFingerprint: (uploads: AffinityPersistedUploads) => string;
}

interface UseProjectDirtyStateResult {
  metadataOnlyDraftDirty: boolean;
  hasUnsavedChanges: boolean;
}

export function useProjectDirtyState({
  draft,
  proteinTemplates,
  affinityUploads,
  savedDraftFingerprint,
  savedComputationFingerprint,
  savedTemplateFingerprint,
  savedAffinityUploadsFingerprint,
  createDraftFingerprint,
  createComputationFingerprint,
  createProteinTemplatesFingerprint,
  createAffinityUploadsFingerprint
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
  const currentAffinityUploadsFingerprint = useMemo(
    () => createAffinityUploadsFingerprint(affinityUploads),
    [affinityUploads, createAffinityUploadsFingerprint]
  );
  const isDraftDirty = useMemo(
    () => Boolean(draft) && Boolean(savedDraftFingerprint) && currentDraftFingerprint !== savedDraftFingerprint,
    [draft, savedDraftFingerprint, currentDraftFingerprint]
  );
  const isTemplateDirty = useMemo(
    () => Boolean(draft) && Boolean(savedTemplateFingerprint) && currentTemplateFingerprint !== savedTemplateFingerprint,
    [draft, savedTemplateFingerprint, currentTemplateFingerprint]
  );
  const isAffinityUploadDirty = useMemo(
    () =>
      Boolean(draft) &&
      Boolean(savedAffinityUploadsFingerprint) &&
      currentAffinityUploadsFingerprint !== savedAffinityUploadsFingerprint,
    [draft, savedAffinityUploadsFingerprint, currentAffinityUploadsFingerprint]
  );
  const metadataOnlyDraftDirty = useMemo(
    () =>
      Boolean(draft) &&
      Boolean(savedDraftFingerprint) &&
      currentDraftFingerprint !== savedDraftFingerprint &&
      Boolean(savedComputationFingerprint) &&
      currentComputationFingerprint === savedComputationFingerprint &&
      !isTemplateDirty &&
      !isAffinityUploadDirty,
    [
      draft,
      savedDraftFingerprint,
      currentDraftFingerprint,
      savedComputationFingerprint,
      currentComputationFingerprint,
      isTemplateDirty,
      isAffinityUploadDirty
    ]
  );
  const hasUnsavedChanges = isDraftDirty || isTemplateDirty || isAffinityUploadDirty;

  return {
    metadataOnlyDraftDirty,
    hasUnsavedChanges
  };
}
