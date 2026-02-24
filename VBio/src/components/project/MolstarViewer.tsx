import { useEffect, useRef } from 'react';
import { alphafoldLegend } from '../../utils/alphafold';
import { applyMolstarHighlights } from './molstar/highlight';
import { useMolstarBootstrap } from './molstar/hooks/useMolstarBootstrap';
import { useMolstarFocus } from './molstar/hooks/useMolstarFocus';
import { useMolstarStructureTheme } from './molstar/hooks/useMolstarStructureTheme';
import { tryApplyElementSymbolThemeToCurrentScene } from './molstar/theme';
import type { MolstarAtomHighlight, MolstarResidueHighlight, MolstarResiduePick } from './molstar/types';

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
  leadOptStyleVariant?: 'default' | 'results';
  ligandFocusChainId?: string;
  suppressResidueSelection?: boolean;
}

export type { MolstarResiduePick, MolstarResidueHighlight, MolstarAtomHighlight };

export function MolstarViewer({
  structureText,
  format,
  overlayStructureText,
  overlayFormat,
  colorMode = 'default',
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
  leadOptStyleVariant = 'default',
  ligandFocusChainId = '',
  suppressResidueSelection = false
}: MolstarViewerProps) {
  const hadExternalHighlightsRef = useRef(false);
  const {
    hostRef,
    viewerRef,
    ready,
    error,
    setError,
    suppressPickEventsRef
  } = useMolstarBootstrap({
    showSequence,
    interactionGranularity,
    onResiduePick,
    pickMode
  });

  const focusLigandAnchor = useMolstarFocus({
    format,
    structureText,
    overlayFormat,
    overlayStructureText,
    ligandFocusChainId
  });

  const { structureReadyVersion } = useMolstarStructureTheme({
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
    focusLigandAnchor,
    setError
  });

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
      disableExternalFocus: scenePreset === 'lead_opt' && leadOptStyleVariant === 'results',
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
    suppressResidueSelection,
    scenePreset,
    viewerRef,
    suppressPickEventsRef
  ]);

  useEffect(() => {
    if (!ready || !viewerRef.current || !structureText.trim()) return;
    if (scenePreset !== 'lead_opt' || suppressAutoFocus) return;
    let cancelled = false;
    const timer = window.setTimeout(() => {
      const viewer = viewerRef.current;
      if (!viewer || cancelled) return;
      focusLigandAnchor(viewer);
      const shouldForceElementTheme = leadOptStyleVariant === 'results' && colorMode !== 'alphafold';
      if (!shouldForceElementTheme) return;
      // Focus may create transient interaction sticks after theme pipeline.
      // Re-apply element-symbol so fragment mode never falls back to AF colors.
      void tryApplyElementSymbolThemeToCurrentScene(viewer).catch(() => null);
      const recolorTimer = window.setTimeout(() => {
        if (cancelled || !viewerRef.current) return;
        void tryApplyElementSymbolThemeToCurrentScene(viewerRef.current).catch(() => null);
      }, 180);
      if (cancelled) {
        window.clearTimeout(recolorTimer);
      }
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [
    colorMode,
    focusLigandAnchor,
    leadOptStyleVariant,
    ready,
    scenePreset,
    structureReadyVersion,
    structureText,
    suppressAutoFocus,
    viewerRef
  ]);

  if (error) {
    return <div className="alert error">{error}</div>;
  }

  return (
    <>
      <div ref={hostRef} className={`molstar-host ${lockView ? 'molstar-host-locked' : ''}`} />
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
