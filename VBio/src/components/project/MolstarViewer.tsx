import { useCallback, useEffect, useRef, useState } from 'react';
import { alphafoldLegend } from '../../utils/alphafold';
import { bootstrapViewerHost, getStructureSignature, loadStructure } from './molstar/bootstrap';
import { applyMolstarHighlights } from './molstar/highlight';
import { subscribePickEvents } from './molstar/pick';
import {
  clearStructureComponents,
  tryApplyLeadOptSceneStyle,
  tryApplyAlphaFoldTheme,
  tryApplyCartoonPreset,
  tryFocusLikelyLigand,
  tryApplyWhiteTheme,
  tryBuildRepresentationsFromStructures,
  waitForStructureEntries
} from './molstar/theme';
import { buildAtomLoci } from './molstar/loci';
import type { MolstarAtomHighlight, MolstarResidueHighlight, MolstarResiduePick } from './molstar/types';

declare global {
  interface Window {
    molstar?: {
      Viewer?: {
        create?: (target: HTMLElement, options: Record<string, unknown>) => Promise<any>;
      };
    };
  }
}

interface MolstarViewerProps {
  structureText: string;
  format: 'cif' | 'pdb';
  overlayStructureText?: string;
  overlayFormat?: 'cif' | 'pdb';
  colorMode?: string;
  confidenceBackend?: string;
  onResiduePick?: (pick: MolstarResiduePick) => void;
  pickMode?: 'click' | 'alt-left';
  highlightResidues?: MolstarResidueHighlight[];
  activeResidue?: MolstarResidueHighlight | null;
  highlightAtoms?: MolstarAtomHighlight[];
  activeAtom?: MolstarAtomHighlight | null;
  interactionGranularity?: 'residue' | 'element';
  lockView?: boolean;
  suppressAutoFocus?: boolean;
  showSequence?: boolean;
  scenePreset?: 'default' | 'lead_opt';
  ligandFocusChainId?: string;
  suppressResidueSelection?: boolean;
}

export type { MolstarResiduePick, MolstarResidueHighlight, MolstarAtomHighlight };

export function MolstarViewer({
  structureText,
  format,
  overlayStructureText,
  overlayFormat,
  colorMode = 'white',
  confidenceBackend,
  onResiduePick,
  pickMode = 'click',
  highlightResidues,
  activeResidue,
  highlightAtoms,
  activeAtom,
  interactionGranularity = 'residue',
  lockView = false,
  suppressAutoFocus = false,
  showSequence = true,
  scenePreset = 'default',
  ligandFocusChainId = '',
  suppressResidueSelection = false
}: MolstarViewerProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const viewerRef = useRef<any>(null);
  const pickUnsubscribeRef = useRef<(() => void) | null>(null);
  const onResiduePickRef = useRef<typeof onResiduePick>(onResiduePick);
  const suppressPickEventsRef = useRef(false);
  const hadExternalHighlightsRef = useRef(false);
  const structureApplyQueueRef = useRef<Promise<void>>(Promise.resolve());
  const structureRequestIdRef = useRef(0);
  const loadedPrimarySignatureRef = useRef('');
  const loadedOverlaySignatureRef = useRef('');
  const altPressedRef = useRef(false);
  const shiftPressedRef = useRef(false);
  const ctrlPressedRef = useRef(false);
  const recentModifiedPrimaryDownRef = useRef(0);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [structureReadyVersion, setStructureReadyVersion] = useState(0);
  const hasResiduePickHandler = Boolean(onResiduePick);

  useEffect(() => {
    onResiduePickRef.current = onResiduePick;
  }, [onResiduePick]);

  const emitResiduePick = useCallback((pick: MolstarResiduePick) => {
    onResiduePickRef.current?.(pick);
  }, []);

  const focusLigandAnchor = useCallback(
    (viewer: any): boolean => {
      const focusManager = viewer?.plugin?.managers?.structure?.focus;
      if (!focusManager?.setFromLoci) return false;
      if (tryFocusLikelyLigand(viewer)) return true;
      if (format !== 'pdb') return false;
      const lines = String(structureText || '').split(/\r?\n/);
      const preferredChain = String(ligandFocusChainId || '').trim();
      const candidates: Array<{ chain: string; residue: number; atom: string }> = [];
      const fallback: Array<{ chain: string; residue: number; atom: string }> = [];
      for (const line of lines) {
        if (!line.startsWith('HETATM')) continue;
        const residueName = line.slice(17, 20).trim().toUpperCase();
        if (!residueName || residueName === 'HOH' || residueName === 'WAT') continue;
        const chain = line.slice(21, 22).trim();
        const residue = Number.parseInt(line.slice(22, 26).trim(), 10);
        const atom = line.slice(12, 16).trim();
        if (!chain || !Number.isFinite(residue) || residue <= 0 || !atom) continue;
        const item = { chain, residue, atom };
        if (preferredChain && chain === preferredChain) candidates.push(item);
        else fallback.push(item);
      }
      const target = candidates[0] || fallback[0];
      if (!target) return false;
      const loci = buildAtomLoci(viewer, target.chain, target.residue, target.atom);
      if (!loci) return false;
      try {
        focusManager.setFromLoci(loci);
        return true;
      } catch {
        return false;
      }
    },
    [format, ligandFocusChainId, structureText]
  );

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.altKey || event.key === 'Alt') {
        altPressedRef.current = true;
      }
      if (event.shiftKey || event.key === 'Shift') {
        shiftPressedRef.current = true;
      }
      if (event.ctrlKey || event.metaKey || event.key === 'Control' || event.key === 'Meta') {
        ctrlPressedRef.current = true;
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (!event.altKey || event.key === 'Alt') {
        altPressedRef.current = false;
      }
      if (!event.shiftKey || event.key === 'Shift') {
        shiftPressedRef.current = false;
      }
      if ((!event.ctrlKey && !event.metaKey) || event.key === 'Control' || event.key === 'Meta') {
        ctrlPressedRef.current = false;
      }
    };
    const onPointerDown = (event: MouseEvent | PointerEvent) => {
      const isPrimary =
        event.button === 0 ||
        event.which === 1 ||
        (typeof event.buttons === 'number' && (event.buttons & 1) === 1);
      if (isPrimary && (event.altKey || event.shiftKey || event.ctrlKey || event.metaKey)) {
        recentModifiedPrimaryDownRef.current = Date.now();
      }
    };
    const onBlur = () => {
      altPressedRef.current = false;
      shiftPressedRef.current = false;
      ctrlPressedRef.current = false;
      recentModifiedPrimaryDownRef.current = 0;
    };

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('pointerdown', onPointerDown, { capture: true, passive: true });
    window.addEventListener('blur', onBlur);
    document.addEventListener('visibilitychange', onBlur);

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('pointerdown', onPointerDown, { capture: true });
      window.removeEventListener('blur', onBlur);
      document.removeEventListener('visibilitychange', onBlur);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const bootstrap = async () => {
      try {
        if (cancelled || !ref.current) return;

        viewerRef.current = await bootstrapViewerHost(ref.current, showSequence);
        try {
          viewerRef.current?.plugin?.managers?.interactivity?.setProps?.({ granularity: interactionGranularity });
          if (typeof viewerRef.current?.setSelectionMode === 'function') {
            viewerRef.current.setSelectionMode(true);
          } else if ('selectionMode' in (viewerRef.current || {})) {
            viewerRef.current.selectionMode = true;
          } else {
            viewerRef.current?.plugin?.behaviors?.interaction?.selectionMode?.next?.(true);
          }
        } catch {
          // no-op
        }
        pickUnsubscribeRef.current = subscribePickEvents(
          viewerRef.current,
          hasResiduePickHandler ? emitResiduePick : undefined,
          pickMode,
          () =>
            altPressedRef.current ||
            shiftPressedRef.current ||
            ctrlPressedRef.current ||
            Date.now() - recentModifiedPrimaryDownRef.current < 450,
          () => suppressPickEventsRef.current
        );
        setReady(true);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Unable to load Mol* viewer.');
      }
    };

    void bootstrap();

    return () => {
      cancelled = true;
      const viewer = viewerRef.current;
      if (pickUnsubscribeRef.current) {
        pickUnsubscribeRef.current();
        pickUnsubscribeRef.current = null;
      }
      if (viewer?.plugin?.dispose) {
        viewer.plugin.dispose();
      }
      viewerRef.current = null;
      loadedPrimarySignatureRef.current = '';
      loadedOverlaySignatureRef.current = '';
      setReady(false);
    };
  }, [showSequence, interactionGranularity]);

  useEffect(() => {
    if (!viewerRef.current) return;
    try {
      viewerRef.current?.plugin?.managers?.interactivity?.setProps?.({ granularity: interactionGranularity });
    } catch {
      // no-op
    }
  }, [interactionGranularity]);

  useEffect(() => {
    if (!viewerRef.current) return;
    try {
      const shouldEnableSelection = true;
      if (typeof viewerRef.current?.setSelectionMode === 'function') {
        viewerRef.current.setSelectionMode(shouldEnableSelection);
      } else if ('selectionMode' in (viewerRef.current || {})) {
        viewerRef.current.selectionMode = shouldEnableSelection;
      } else {
        viewerRef.current?.plugin?.behaviors?.interaction?.selectionMode?.next?.(shouldEnableSelection);
      }
    } catch {
      // no-op
    }
    if (pickUnsubscribeRef.current) {
      pickUnsubscribeRef.current();
      pickUnsubscribeRef.current = null;
    }
    pickUnsubscribeRef.current = subscribePickEvents(
      viewerRef.current,
      hasResiduePickHandler ? emitResiduePick : undefined,
      pickMode,
      () => {
        return (
          altPressedRef.current ||
          shiftPressedRef.current ||
          ctrlPressedRef.current ||
          Date.now() - recentModifiedPrimaryDownRef.current < 450
        );
      },
      () => suppressPickEventsRef.current
    );
    return () => {
      if (pickUnsubscribeRef.current) {
        pickUnsubscribeRef.current();
        pickUnsubscribeRef.current = null;
      }
    };
  }, [emitResiduePick, hasResiduePickHandler, pickMode]);

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
            setStructureReadyVersion((prev) => prev + 1);
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

        const structureEntries = await waitForStructureEntries(viewer);
        if (requestId !== structureRequestIdRef.current) return;
        const presetApplied = await tryApplyCartoonPreset(viewer, structureEntries);
        if (requestId !== structureRequestIdRef.current) return;
        if (scenePreset === 'lead_opt') {
          const supportsStaticBuilder =
            typeof viewer?.plugin?.builders?.structure?.tryCreateComponentStatic === 'function' &&
            typeof viewer?.plugin?.builders?.structure?.representation?.addRepresentation === 'function';
          if (supportsStaticBuilder) {
            await clearStructureComponents(viewer);
            await tryBuildRepresentationsFromStructures(
              viewer,
              colorMode === 'alphafold' ? 'plddt-confidence' : 'uniform',
              structureEntries,
              [
                { kind: 'polymer', type: 'ball-and-stick' },
                { kind: 'ligand', type: 'ball-and-stick' },
                { kind: 'branched', type: 'ball-and-stick' },
                { kind: 'ion', type: 'ball-and-stick' }
              ]
            );
            if (requestId !== structureRequestIdRef.current) return;
          }
        }
        if (!presetApplied && colorMode === 'alphafold') {
          const supportsStaticBuilder =
            typeof viewer?.plugin?.builders?.structure?.tryCreateComponentStatic === 'function' &&
            typeof viewer?.plugin?.builders?.structure?.representation?.addRepresentation === 'function';
          if (supportsStaticBuilder) {
            await clearStructureComponents(viewer);
            await tryBuildRepresentationsFromStructures(viewer, 'plddt-confidence', structureEntries);
            if (requestId !== structureRequestIdRef.current) return;
          }
        }

        if (colorMode === 'alphafold') {
          await tryApplyAlphaFoldTheme(viewer, confidenceBackend);
        } else {
          await tryApplyWhiteTheme(viewer);
        }
        if (scenePreset === 'lead_opt') {
          await tryApplyLeadOptSceneStyle(viewer, { polymerAlpha: 0.26, ligandAlpha: 1.0 });
          if (!suppressAutoFocus) {
            focusLigandAnchor(viewer);
          }
        }
        if (requestId === structureRequestIdRef.current) {
          setStructureReadyVersion((prev) => prev + 1);
        }
      } catch (e) {
        if (requestId !== structureRequestIdRef.current) return;
        setError(e instanceof Error ? e.message : 'Unable to update Mol* viewer.');
      }
    };

    structureApplyQueueRef.current = structureApplyQueueRef.current.then(run);

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
    colorMode,
    confidenceBackend,
    scenePreset,
    suppressAutoFocus,
    focusLigandAnchor
  ]);

  useEffect(() => {
    if (!ready || !viewerRef.current || !structureText.trim()) return;
    applyMolstarHighlights({
      viewer: viewerRef.current,
      structureText,
      highlightResidues: suppressResidueSelection ? [] : highlightResidues,
      activeResidue,
      highlightAtoms,
      activeAtom,
      suppressAutoFocus,
      hadExternalHighlightsRef,
      suppressPickEventsRef
    });
  }, [
    ready,
    structureText,
    structureReadyVersion,
    highlightResidues,
    activeResidue,
    highlightAtoms,
    activeAtom,
    suppressAutoFocus,
    suppressResidueSelection
  ]);

  if (error) {
    return <div className="alert error">{error}</div>;
  }

  return (
    <>
      <div ref={ref} className={`molstar-host ${lockView ? 'molstar-host-locked' : ''}`} />
      {!structureText.trim() && (
        <div className="muted small top-margin">No structure loaded yet. Run prediction and refresh status after completion.</div>
      )}
      {structureText.trim() && colorMode === 'alphafold' && (
        <div className="legend-row">
          {alphafoldLegend.map((item) => (
            <div key={item.key} className="legend-item">
              <span className="legend-dot" style={{ backgroundColor: item.color }} />
              <span>{item.label}</span>
            </div>
          ))}
        </div>
      )}
    </>
  );
}
