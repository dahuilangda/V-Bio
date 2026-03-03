import { useCallback, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { ProteinTemplateUpload } from '../../types/models';
import type { AffinityPersistedUploads } from '../../hooks/useAffinityWorkflow';
import { createProteinTemplatesFingerprint, hasProteinTemplates } from './projectDraftUtils';

interface UseTaskAttachmentCacheResult {
  taskProteinTemplates: Record<string, Record<string, ProteinTemplateUpload>>;
  setTaskProteinTemplates: Dispatch<SetStateAction<Record<string, Record<string, ProteinTemplateUpload>>>>;
  taskAffinityUploads: Record<string, AffinityPersistedUploads>;
  setTaskAffinityUploads: Dispatch<SetStateAction<Record<string, AffinityPersistedUploads>>>;
  rememberTemplatesForTaskRow: (taskRowId: string | null, templates: Record<string, ProteinTemplateUpload>) => void;
  rememberAffinityUploadsForTaskRow: (taskRowId: string | null, uploads: AffinityPersistedUploads) => void;
}

export function useTaskAttachmentCache(): UseTaskAttachmentCacheResult {
  const [taskProteinTemplates, setTaskProteinTemplates] = useState<
    Record<string, Record<string, ProteinTemplateUpload>>
  >({});
  const [taskAffinityUploads, setTaskAffinityUploads] = useState<Record<string, AffinityPersistedUploads>>({});

  const rememberTemplatesForTaskRow = useCallback((taskRowId: string | null, templates: Record<string, ProteinTemplateUpload>) => {
    const normalizedTaskRowId = String(taskRowId || '').trim();
    if (!normalizedTaskRowId) return;
    setTaskProteinTemplates((prev) => {
      const current = prev[normalizedTaskRowId] || {};
      const sameFingerprint =
        createProteinTemplatesFingerprint(current) === createProteinTemplatesFingerprint(templates || {});
      if (sameFingerprint) return prev;
      const next = { ...prev };
      if (!hasProteinTemplates(templates)) {
        delete next[normalizedTaskRowId];
      } else {
        next[normalizedTaskRowId] = templates;
      }
      return next;
    });
  }, []);

  const rememberAffinityUploadsForTaskRow = useCallback(
    (taskRowId: string | null, uploads: AffinityPersistedUploads) => {
      const normalizedTaskRowId = String(taskRowId || '').trim();
      if (!normalizedTaskRowId) return;
      setTaskAffinityUploads((prev) => {
        const current = prev[normalizedTaskRowId] || { target: null, ligand: null };
        const sameTarget =
          String(current.target?.fileName || '') === String(uploads.target?.fileName || '') &&
          String(current.target?.content || '') === String(uploads.target?.content || '');
        const sameLigand =
          String(current.ligand?.fileName || '') === String(uploads.ligand?.fileName || '') &&
          String(current.ligand?.content || '') === String(uploads.ligand?.content || '');
        if (sameTarget && sameLigand) return prev;

        const next = { ...prev };
        if (!uploads.target && !uploads.ligand) {
          delete next[normalizedTaskRowId];
        } else {
          next[normalizedTaskRowId] = {
            target: uploads.target ? { ...uploads.target } : null,
            ligand: uploads.ligand ? { ...uploads.ligand } : null
          };
        }
        return next;
      });
    },
    []
  );

  return {
    taskProteinTemplates,
    setTaskProteinTemplates,
    taskAffinityUploads,
    setTaskAffinityUploads,
    rememberTemplatesForTaskRow,
    rememberAffinityUploadsForTaskRow
  };
}
