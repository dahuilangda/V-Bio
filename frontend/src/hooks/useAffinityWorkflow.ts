import type { Dispatch, SetStateAction } from 'react';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { previewAffinityComplex } from '../api/backendApi';
import type { AffinityPreviewPayload } from '../types/models';
import { normalizeStructureFileName, resolveStructureFormat, extractProteinChainSequences, extractStructureChainIds } from '../utils/structureParser';

interface UseAffinityWorkflowOptions {
  enabled: boolean;
  scopeKey?: string | null;
  preferredConfidenceOnly?: boolean;
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
  const { enabled, scopeKey, preferredConfidenceOnly = true, persistedLigandSmiles, persistedUploads, onChainsResolved } = options;

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
  const preferredConfidenceOnlyRef = useRef(Boolean(preferredConfidenceOnly));
  const hydratedUploadKeyRef = useRef('');
  const hydratedPersistedSmilesKeyRef = useRef('');
  const targetUploadReadSeqRef = useRef(0);
  const ligandUploadReadSeqRef = useRef(0);
  // True once a file lands in memory for the current scope (user upload or copilot
  // apply). Persisted-upload hydration must never clobber it — without this, an
  // apply done while the workflow is disabled (other tab) gets silently replaced
  // by the previously saved snapshot the moment the Components tab enables.
  const hasInMemoryUploadsRef = useRef(false);

  const currentPairKey = useMemo(() => buildPairKey(targetFile, ligandFile), [targetFile, ligandFile]);
  const isPreviewCurrent = Boolean(currentPairKey) && previewPairKey === currentPairKey;
  const hasLigand = Boolean(preview?.hasLigand);
  const supportsActivity = Boolean(preview?.supportsActivity);
  const ligandIsSmallMolecule = Boolean(preview?.ligandIsSmallMolecule);
  const previewTargetStructureText = preview?.targetStructureText || preview?.structureText || '';
  const previewTargetStructureFormat: 'cif' | 'pdb' = preview?.targetStructureFormat || preview?.structureFormat || 'cif';
  const previewLigandStructureText = preview?.ligandStructureText || '';
  const previewLigandStructureFormat: 'cif' | 'pdb' = preview?.ligandStructureFormat || 'cif';
  const confidenceOnlyLocked = !hasLigand;

  useEffect(() => {
    preferredConfidenceOnlyRef.current = Boolean(preferredConfidenceOnly);
    if (!confidenceOnlyTouchedRef.current) {
      setConfidenceOnly(Boolean(preferredConfidenceOnly));
    }
  }, [preferredConfidenceOnly]);

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
    setConfidenceOnly(Boolean(preferredConfidenceOnlyRef.current));
    setPersistedTargetUpload(null);
    setPersistedLigandUpload(null);
    confidenceOnlyTouchedRef.current = false;
    hasInMemoryUploadsRef.current = false;
  }, []);

  useEffect(() => {
    requestSeqRef.current += 1;
    targetUploadReadSeqRef.current += 1;
    ligandUploadReadSeqRef.current += 1;
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
    if (hasInMemoryUploadsRef.current) {
      // In-memory uploads (user or copilot applied during this scope) win over
      // the persisted snapshot; hydration is only an initial restore.
      setUploadsHydrating(false);
      return;
    }
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

    targetUploadReadSeqRef.current += 1;
    ligandUploadReadSeqRef.current += 1;
    hydratedUploadKeyRef.current = persistedUploadKey;
    // Self-heal legacy drafts: a snapshot saved before the name-extension policy may carry an
    // extension-less fileName ("KLK1") whose content is a valid structure — restoring it
    // verbatim would re-raise the target-format error on every page load, forever.
    const restoredTargetName = normalizeStructureFileName(targetName, targetContent);
    const restoredTarget = new File([targetContent], restoredTargetName, { type: 'text/plain' });
    const ligandName = String(persistedUploads?.ligand?.fileName || '').trim();
    const ligandContent = String(persistedUploads?.ligand?.content || '').trim();
    const restoredLigand = ligandName && ligandContent ? new File([ligandContent], ligandName, { type: 'text/plain' }) : null;

    setTargetFile(restoredTarget);
    setLigandFile(restoredLigand);
    setPersistedTargetUpload({ fileName: restoredTargetName, content: targetContent });
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
      const targetReadSeq = targetUploadReadSeqRef.current + 1;
      targetUploadReadSeqRef.current = targetReadSeq;
      ligandUploadReadSeqRef.current += 1;
      hasInMemoryUploadsRef.current = true;
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
      // Force overwrite semantics: clear previous cached uploads immediately.
      setPersistedTargetUpload(null);
      setPersistedLigandUpload(null);
      if (!file) {
        return;
      }
      void file
        .text()
        .then((content) => {
          if (targetUploadReadSeqRef.current !== targetReadSeq) return;
          // Same name-extension policy as the restore path: a file whose name carries no
          // recognized extension gets the content-sniffed format stamped on BEFORE the
          // preview gate sees it — whatever source produced the File.
          const normalized = normalizeStructureFileName(file.name, content);
          if (normalized !== file.name) {
            const healed = new File([content], normalized, { type: file.type || 'text/plain' });
            setTargetFile(healed);
            setPersistedTargetUpload({ fileName: normalized, content });
            return;
          }
          setPersistedTargetUpload({ fileName: file.name, content });
        })
        .catch(() => {
          if (targetUploadReadSeqRef.current !== targetReadSeq) return;
          setPersistedTargetUpload({ fileName: file.name, content: '' });
        });
    },
    []
  );

  const onLigandFileChange = useCallback(
    (file: File | null) => {
      const ligandReadSeq = ligandUploadReadSeqRef.current + 1;
      ligandUploadReadSeqRef.current = ligandReadSeq;
      hasInMemoryUploadsRef.current = true;
      setLigandFile(file);
      setPreviewError(null);
      setPreviewPairKey('');
      setTargetChainIds([]);
      setLigandChainId('');
      setLigandSmiles('');
      confidenceOnlyTouchedRef.current = false;
      setPreview(null);
      setPreviewVersion((prev) => prev + 1);
      // Clear stale ligand upload content before applying new upload result.
      setPersistedLigandUpload(null);
      if (!file) {
        return;
      }
      void file
        .text()
        .then((content) => {
          if (ligandUploadReadSeqRef.current !== ligandReadSeq) return;
          setPersistedLigandUpload({ fileName: file.name, content });
        })
        .catch(() => {
          if (ligandUploadReadSeqRef.current !== ligandReadSeq) return;
          setPersistedLigandUpload({ fileName: file.name, content: '' });
        });
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
          // Name first, content sniff second (shared policy): a file applied without a
          // recognizable extension (e.g. a copilot-applied "KLK1") must still parse
          // instead of blocking the preview.
          const targetText = await targetFile.text();
          const targetFormat = resolveStructureFormat(targetFile.name, targetText);
          if (!targetFormat) {
            throw new Error('Target file must be .pdb, .ent, .cif or .mmcif.');
          }
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
        const nextLocked = !nextPreview.hasLigand;
        setConfidenceOnly((prev) => {
          if (nextLocked) return true;
          if (!confidenceOnlyTouchedRef.current) return Boolean(preferredConfidenceOnlyRef.current);
          return prev;
        });
      } catch (error) {
        if (requestSeqRef.current !== requestSeq) return;
        setPreviewPairKey('');
        setPreview(null);
        setPreviewVersion((prev) => prev + 1);
        setTargetChainIds([]);
        setLigandChainId('');
        const message = error instanceof Error ? error.message : 'Failed to build affinity preview.';
        setPreviewError(
          message.includes('Backend request timeout')
            ? 'Affinity preview timed out while preparing the uploaded target and ligand. The files were selected, but preview preparation did not finish in time; try smaller files or run after the backend is less busy.'
            : message
        );
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
