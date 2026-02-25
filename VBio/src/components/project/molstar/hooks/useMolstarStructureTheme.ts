import { useEffect, useRef, useState, type RefObject } from 'react';
import { getStructureSignature, loadStructure } from '../bootstrap';
import { applyStructureAppearancePipeline } from './structureAppearancePipeline';

interface UseMolstarStructureThemeArgs {
  viewerRef: RefObject<any>;
  ready: boolean;
  structureText: string;
  format: 'cif' | 'pdb';
  overlayStructureText?: string;
  overlayFormat?: 'cif' | 'pdb';
  colorMode: string;
  confidenceBackend?: string;
  scenePreset: 'default' | 'lead_opt';
  leadOptStyleVariant: 'default' | 'results';
  suppressAutoFocus: boolean;
  autoFocusLigand: boolean;
  focusLigandAnchor: (viewer: any) => boolean;
  setError: (value: string | null) => void;
}

export function useMolstarStructureTheme({
  viewerRef,
  ready,
  structureText,
  format,
  overlayStructureText,
  overlayFormat,
  colorMode,
  confidenceBackend,
  scenePreset,
  leadOptStyleVariant,
  suppressAutoFocus,
  autoFocusLigand,
  focusLigandAnchor,
  setError
}: UseMolstarStructureThemeArgs) {
  const [structureReadyVersion, setStructureReadyVersion] = useState(0);
  const [structureContentVersion, setStructureContentVersion] = useState(0);
  const structureLoadQueueRef = useRef<Promise<void>>(Promise.resolve());
  const styleApplyQueueRef = useRef<Promise<void>>(Promise.resolve());
  const structureRequestIdRef = useRef(0);
  const styleRequestIdRef = useRef(0);
  const loadedPrimarySignatureRef = useRef('');
  const loadedOverlaySignatureRef = useRef('');

  useEffect(() => {
    if (ready) return;
    loadedPrimarySignatureRef.current = '';
    loadedOverlaySignatureRef.current = '';
    structureRequestIdRef.current += 1;
    styleRequestIdRef.current += 1;
  }, [ready]);

  useEffect(() => {
    if (!ready || !viewerRef.current) return;
    const requestId = structureRequestIdRef.current + 1;
    structureRequestIdRef.current = requestId;

    const run = async () => {
      if (requestId !== structureRequestIdRef.current) return;
      try {
        setError(null);
        const viewer = viewerRef.current;
        if (!viewer) return;
        const primaryText = structureText.trim();
        const overlayText = String(overlayStructureText || '').trim();
        const resolvedOverlayFormat: 'cif' | 'pdb' = overlayFormat === 'pdb' ? 'pdb' : 'cif';

        if (!primaryText) {
          if (typeof viewer.clear === 'function') {
            await viewer.clear();
          }
          loadedPrimarySignatureRef.current = '';
          loadedOverlaySignatureRef.current = '';
          if (requestId === structureRequestIdRef.current) {
            setStructureContentVersion((prev) => prev + 1);
          }
          return;
        }

        const nextPrimarySignature = getStructureSignature(primaryText, format);
        const nextOverlaySignature = overlayText ? getStructureSignature(overlayText, resolvedOverlayFormat) : '';
        const previousPrimarySignature = loadedPrimarySignatureRef.current;
        const previousOverlaySignature = loadedOverlaySignatureRef.current;
        const primaryChanged = nextPrimarySignature !== previousPrimarySignature;
        const overlayChanged = nextOverlaySignature !== previousOverlaySignature;

        if (primaryChanged) {
          await loadStructure(viewer, primaryText, format, { clearBefore: true });
          loadedPrimarySignatureRef.current = nextPrimarySignature;
          loadedOverlaySignatureRef.current = '';
          if (requestId !== structureRequestIdRef.current) return;
          if (overlayText) {
            await loadStructure(viewer, overlayText, resolvedOverlayFormat, { clearBefore: false });
            loadedOverlaySignatureRef.current = nextOverlaySignature;
            if (requestId !== structureRequestIdRef.current) return;
          }
        } else if (overlayChanged) {
          if (!overlayText) {
            await loadStructure(viewer, primaryText, format, { clearBefore: true });
            loadedOverlaySignatureRef.current = '';
          } else if (!previousOverlaySignature) {
            await loadStructure(viewer, overlayText, resolvedOverlayFormat, { clearBefore: false });
            loadedOverlaySignatureRef.current = nextOverlaySignature;
          } else {
            await loadStructure(viewer, primaryText, format, { clearBefore: true });
            if (requestId !== structureRequestIdRef.current) return;
            await loadStructure(viewer, overlayText, resolvedOverlayFormat, { clearBefore: false });
            loadedOverlaySignatureRef.current = nextOverlaySignature;
          }
          if (requestId !== structureRequestIdRef.current) return;
        }

        if (requestId === structureRequestIdRef.current) {
          setStructureContentVersion((prev) => prev + 1);
        }
      } catch (e) {
        if (requestId !== structureRequestIdRef.current) return;
        setError(e instanceof Error ? e.message : 'Unable to update Mol* viewer.');
      }
    };

    structureLoadQueueRef.current = structureLoadQueueRef.current.then(run);

    return () => {
      if (structureRequestIdRef.current === requestId) {
        structureRequestIdRef.current += 1;
      }
    };
  }, [
    ready,
    structureText,
    format,
    overlayStructureText,
    overlayFormat,
    viewerRef,
    setError
  ]);

  useEffect(() => {
    if (!ready || !viewerRef.current || !structureText.trim()) return;
    const requestId = styleRequestIdRef.current + 1;
    styleRequestIdRef.current = requestId;

    const run = async () => {
      if (requestId !== styleRequestIdRef.current) return;
      try {
        setError(null);
        const viewer = viewerRef.current;
        if (!viewer) return;
        await applyStructureAppearancePipeline({
          viewer,
          colorMode,
          confidenceBackend,
          scenePreset,
          leadOptStyleVariant,
          suppressAutoFocus,
          autoFocusLigand,
          focusLigandAnchor,
          isRequestCurrent: () => requestId === styleRequestIdRef.current
        });
        if (requestId !== styleRequestIdRef.current) return;
        setStructureReadyVersion((prev) => prev + 1);
      } catch (e) {
        if (requestId !== styleRequestIdRef.current) return;
        setError(e instanceof Error ? e.message : 'Unable to update Mol* viewer.');
      }
    };

    styleApplyQueueRef.current = styleApplyQueueRef.current.then(run);

    return () => {
      if (styleRequestIdRef.current === requestId) {
        styleRequestIdRef.current += 1;
      }
    };
  }, [
    ready,
    structureText,
    structureContentVersion,
    colorMode,
    confidenceBackend,
    scenePreset,
    leadOptStyleVariant,
    suppressAutoFocus,
    autoFocusLigand,
    focusLigandAnchor,
    setError
  ]);

  return { structureReadyVersion };
}
