import type { Dispatch, SetStateAction } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { previewAffinityComplex } from '../api/backendApi';
import type { AffinityPreviewPayload } from '../types/models';
import { detectStructureFormat, extractProteinChainSequences, extractStructureChainIds } from '../utils/structureParser';

interface UseAffinityWorkflowOptions {
  enabled: boolean;
  scopeKey?: string | null;
  persistedLigandSmiles?: string;
  persistedUploads?: {
    target?: { fileName: string; content: string } | null;
    ligand?: { fileName: string; content: string } | null;
  };
  onChainsResolved?: (targetChainId: string, ligandChainId: string) => void;
}

export interface AffinityPersistedUpload {
  fileName: string;
  content: string;
}

export interface AffinityPersistedUploads {
  target: AffinityPersistedUpload | null;
  ligand: AffinityPersistedUpload | null;
}

export interface AffinityWorkflowState {
  targetFile: File | null;
  ligandFile: File | null;
  ligandSmiles: string;
  targetChainIds: string[];
  ligandChainId: string;
  preview: AffinityPreviewPayload | null;
  previewVersion: number;
  previewTargetStructureText: string;
  previewTargetStructureFormat: 'cif' | 'pdb';
  previewLigandStructureText: string;
  previewLigandStructureFormat: 'cif' | 'pdb';
  previewLoading: boolean;
  previewError: string | null;
  isPreviewCurrent: boolean;
  hasLigand: boolean;
  ligandIsSmallMolecule: boolean;
  supportsActivity: boolean;
  confidenceOnly: boolean;
  confidenceOnlyLocked: boolean;
  uploadsHydrating: boolean;
  persistedUploads: AffinityPersistedUploads;
  onTargetFileChange: (file: File | null) => void;
  onLigandFileChange: (file: File | null) => void;
  onConfidenceOnlyChange: (value: boolean) => void;
  setLigandSmiles: Dispatch<SetStateAction<string>>;
}

function fileKey(file: File | null): string {
  if (!file) return '';
  return `${file.name}:${file.size}:${file.lastModified}`;
}

function buildPairKey(targetFile: File | null, ligandFile: File | null): string {
  if (!targetFile) return '';
  return `${fileKey(targetFile)}__${fileKey(ligandFile) || 'none'}`;
}

export function useAffinityWorkflow(options: UseAffinityWorkflowOptions): AffinityWorkflowState {
  const { enabled, scopeKey, persistedLigandSmiles, persistedUploads, onChainsResolved } = options;

  const [targetFile, setTargetFile] = useState<File | null>(null);
  const [ligandFile, setLigandFile] = useState<File | null>(null);
  const [ligandSmiles, setLigandSmiles] = useState('');
  const [targetChainIds, setTargetChainIds] = useState<string[]>([]);
  const [ligandChainId, setLigandChainId] = useState('');
  const [preview, setPreview] = useState<AffinityPreviewPayload | null>(null);
  const [previewVersion, setPreviewVersion] = useState(0);
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState<string | null>(null);
  const [previewPairKey, setPreviewPairKey] = useState('');
  const [confidenceOnly, setConfidenceOnly] = useState(true);
  const [uploadsHydrating, setUploadsHydrating] = useState(false);
  const [persistedTargetUpload, setPersistedTargetUpload] = useState<AffinityPersistedUpload | null>(null);
  const [persistedLigandUpload, setPersistedLigandUpload] = useState<AffinityPersistedUpload | null>(null);
  const requestSeqRef = useRef(0);
  const confidenceOnlyTouchedRef = useRef(false);
  const hydratedUploadKeyRef = useRef('');
  const hydratedPersistedSmilesKeyRef = useRef('');

  const currentPairKey = useMemo(() => buildPairKey(targetFile, ligandFile), [targetFile, ligandFile]);
  const isPreviewCurrent = Boolean(currentPairKey) && previewPairKey === currentPairKey;
  const hasLigand = Boolean(preview?.hasLigand);
  const supportsActivity = Boolean(preview?.supportsActivity);
  const ligandIsSmallMolecule = Boolean(preview?.ligandIsSmallMolecule);
  const previewTargetStructureText = preview?.targetStructureText || preview?.structureText || '';
  const previewTargetStructureFormat: 'cif' | 'pdb' = preview?.targetStructureFormat || preview?.structureFormat || 'cif';
  const previewLigandStructureText = preview?.ligandStructureText || '';
  const previewLigandStructureFormat: 'cif' | 'pdb' = preview?.ligandStructureFormat || 'cif';
  const confidenceOnlyLocked = !hasLigand || !supportsActivity;

  const resetAll = useCallback(() => {
    setTargetFile(null);
    setLigandFile(null);
    setLigandSmiles('');
    setTargetChainIds([]);
    setLigandChainId('');
    setPreview(null);
    setPreviewVersion(0);
    setPreviewLoading(false);
    setPreviewError(null);
    setPreviewPairKey('');
    setConfidenceOnly(true);
    setPersistedTargetUpload(null);
    setPersistedLigandUpload(null);
    confidenceOnlyTouchedRef.current = false;
  }, []);

  useEffect(() => {
    requestSeqRef.current += 1;
    setUploadsHydrating(true);
    resetAll();
    hydratedUploadKeyRef.current = '';
    hydratedPersistedSmilesKeyRef.current = '';
  }, [scopeKey, resetAll]);

  const persistedUploadKey = useMemo(() => {
    const targetName = String(persistedUploads?.target?.fileName || '').trim();
    const targetContent = String(persistedUploads?.target?.content || '');
    const ligandName = String(persistedUploads?.ligand?.fileName || '').trim();
    const ligandContent = String(persistedUploads?.ligand?.content || '');
    const scopeToken = String(scopeKey || '');
    return `${scopeToken}|${targetName}:${targetContent.length}|${ligandName}:${ligandContent.length}`;
  }, [scopeKey, persistedUploads]);

  useEffect(() => {
    if (!enabled) return;
    const targetName = String(persistedUploads?.target?.fileName || '').trim();
    const targetContent = String(persistedUploads?.target?.content || '').trim();
    if (!targetName || !targetContent) {
      setUploadsHydrating(false);
      return;
    }
    if (hydratedUploadKeyRef.current === persistedUploadKey) {
      setUploadsHydrating(false);
      return;
    }

    hydratedUploadKeyRef.current = persistedUploadKey;
    const restoredTarget = new File([targetContent], targetName, { type: 'text/plain' });
    const ligandName = String(persistedUploads?.ligand?.fileName || '').trim();
    const ligandContent = String(persistedUploads?.ligand?.content || '').trim();
    const restoredLigand = ligandName && ligandContent ? new File([ligandContent], ligandName, { type: 'text/plain' }) : null;

    setTargetFile(restoredTarget);
    setLigandFile(restoredLigand);
    setPersistedTargetUpload({ fileName: targetName, content: targetContent });
    setPersistedLigandUpload(restoredLigand ? { fileName: ligandName, content: ligandContent } : null);
    setPreviewError(null);
    setPreviewPairKey('');
    setTargetChainIds([]);
    setLigandChainId('');
    setPreview(null);
    setPreviewVersion((prev) => prev + 1);
    confidenceOnlyTouchedRef.current = false;
    setUploadsHydrating(false);
  }, [enabled, persistedUploads, persistedUploadKey]);

  const onTargetFileChange = useCallback(
    (file: File | null) => {
      setTargetFile(file);
      // Re-uploading target should reset ligand-dependent preview state.
      setLigandFile(null);
      setLigandSmiles('');
      setPreviewError(null);
      setPreviewPairKey('');
      setTargetChainIds([]);
      setLigandChainId('');
      confidenceOnlyTouchedRef.current = false;
      setPreview(null);
      setPreviewVersion((prev) => prev + 1);
      if (!file) {
        setPersistedTargetUpload(null);
        setPersistedLigandUpload(null);
      } else {
        void file
          .text()
          .then((content) => {
            setPersistedTargetUpload({ fileName: file.name, content });
          })
          .catch(() => {
            setPersistedTargetUpload({ fileName: file.name, content: '' });
          });
      }
      if (!file) {
        return;
      }
    },
    []
  );

  const onLigandFileChange = useCallback(
    (file: File | null) => {
      setLigandFile(file);
      setPreviewError(null);
      setPreviewPairKey('');
      setTargetChainIds([]);
      setLigandChainId('');
      setLigandSmiles('');
      confidenceOnlyTouchedRef.current = false;
      setPreview(null);
      setPreviewVersion((prev) => prev + 1);
      if (!file) {
        setPersistedLigandUpload(null);
      } else {
        void file
          .text()
          .then((content) => {
            setPersistedLigandUpload({ fileName: file.name, content });
          })
          .catch(() => {
            setPersistedLigandUpload({ fileName: file.name, content: '' });
          });
      }
      if (!file) {
        return;
      }
    },
    []
  );

  useEffect(() => {
    if (!enabled) return;
    if (!targetFile) return;

    const expectedPairKey = buildPairKey(targetFile, ligandFile);
    const requestSeq = requestSeqRef.current + 1;
    requestSeqRef.current = requestSeq;
    setPreviewLoading(true);
    setPreviewError(null);

    void (async () => {
      try {
        let nextPreview: AffinityPreviewPayload;
        if (!ligandFile) {
          const targetFormat = detectStructureFormat(targetFile.name);
          if (!targetFormat) {
            throw new Error('Target file must be .pdb, .cif or .mmcif.');
          }
          const targetText = await targetFile.text();
          const chainIds = extractStructureChainIds(targetText, targetFormat);
          const proteinChainIds = Object.keys(extractProteinChainSequences(targetText, targetFormat));
          const targetChainIds = proteinChainIds.length > 0 ? proteinChainIds : chainIds;

          nextPreview = {
            structureText: targetText,
            structureFormat: targetFormat,
            structureName: targetFile.name,
            targetStructureText: targetText,
            targetStructureFormat: targetFormat,
            ligandStructureText: '',
            ligandStructureFormat: targetFormat,
            ligandSmiles: '',
            targetChainIds,
            ligandChainId: '',
            hasLigand: false,
            ligandIsSmallMolecule: false,
            supportsActivity: false,
            proteinFileName: targetFile.name,
            ligandFileName: ''
          };
        } else {
          nextPreview = await previewAffinityComplex({ targetFile, ligandFile });
        }

        if (requestSeqRef.current !== requestSeq) return;
        setPreview(nextPreview);
        setPreviewVersion((prev) => prev + 1);
        setPreviewPairKey(expectedPairKey);
        setTargetChainIds(nextPreview.targetChainIds);
        setLigandChainId(nextPreview.ligandChainId || '');
        setLigandSmiles((prev) => {
          const nextSmiles = String(nextPreview.ligandSmiles || '').trim();
          // When ligand file changes, preview is the source of truth.
          if (ligandFile) return nextSmiles;
          if (!nextSmiles) return prev;
          return prev.trim() ? prev : nextSmiles;
        });
        const nextLocked = !nextPreview.hasLigand || !nextPreview.supportsActivity;
        setConfidenceOnly((prev) => {
          if (nextLocked) return true;
          if (!confidenceOnlyTouchedRef.current) return true;
          return prev;
        });
      } catch (error) {
        if (requestSeqRef.current !== requestSeq) return;
        setPreviewPairKey('');
        setPreview(null);
        setPreviewVersion((prev) => prev + 1);
        setTargetChainIds([]);
        setLigandChainId('');
        setPreviewError(error instanceof Error ? error.message : 'Failed to build affinity preview.');
      } finally {
        if (requestSeqRef.current === requestSeq) {
          setPreviewLoading(false);
        }
      }
    })();
  }, [enabled, targetFile, ligandFile]);

  useEffect(() => {
    if (!enabled) return;
    if (targetFile || ligandFile || preview || previewLoading) return;
    const persisted = String(persistedLigandSmiles || '').trim();
    if (!persisted) return;
    const persistedSmilesKey = `${String(scopeKey || '')}|${persisted}`;
    if (hydratedPersistedSmilesKeyRef.current === persistedSmilesKey) return;
    hydratedPersistedSmilesKeyRef.current = persistedSmilesKey;
    setLigandSmiles((prev) => (prev.trim() ? prev : persisted));
  }, [enabled, persistedLigandSmiles, targetFile, ligandFile, preview, previewLoading, scopeKey]);

  useEffect(() => {
    if (!enabled) return;
    if (!isPreviewCurrent) return;
    if (!targetChainIds.length) return;
    const resolvedLigandChain = String(ligandChainId || '').trim();
    if (!resolvedLigandChain) return;
    onChainsResolved?.(targetChainIds[0], resolvedLigandChain);
  }, [enabled, isPreviewCurrent, targetChainIds, ligandChainId, onChainsResolved]);

  const onConfidenceOnlyChange = useCallback((value: boolean) => {
    confidenceOnlyTouchedRef.current = true;
    setConfidenceOnly(value);
  }, []);

  return {
    targetFile,
    ligandFile,
    ligandSmiles,
    targetChainIds,
    ligandChainId,
    preview,
    previewVersion,
    previewTargetStructureText,
    previewTargetStructureFormat,
    previewLigandStructureText,
    previewLigandStructureFormat,
    previewLoading,
    previewError,
    isPreviewCurrent,
    hasLigand,
    ligandIsSmallMolecule,
    supportsActivity,
    confidenceOnly,
    confidenceOnlyLocked,
    uploadsHydrating,
    persistedUploads: {
      target: persistedTargetUpload,
      ligand: persistedLigandUpload
    },
    onTargetFileChange,
    onLigandFileChange,
    onConfidenceOnlyChange,
    setLigandSmiles
  };
}
