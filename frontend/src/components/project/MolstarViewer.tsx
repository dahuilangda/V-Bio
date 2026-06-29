import { memo, useEffect, useRef } from 'react';
import { alphafoldLegend } from '../../utils/alphafold';
import { applyMolstarHighlights } from './molstar/highlight';
import { useMolstarBootstrap } from './molstar/hooks/useMolstarBootstrap';
import { useMolstarFocus } from './molstar/hooks/useMolstarFocus';
import { useMolstarStructureTheme } from './molstar/hooks/useMolstarStructureTheme';
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
  autoFocusLigand?: boolean;
  suppressResidueSelection?: boolean;
}

export type { MolstarResiduePick, MolstarResidueHighlight, MolstarAtomHighlight };

export const MolstarViewer = memo(function MolstarViewer({
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
  autoFocusLigand,
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

  const shouldAutoFocusLigand = autoFocusLigand ?? scenePreset === 'lead_opt';

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
    autoFocusLigand: shouldAutoFocusLigand,
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
    // Deferred ligand focus after the appearance pipeline settles. We intentionally do NOT
    // re-apply color themes here — that is the pipeline's job. This effect re-runs on every
    // colorMode / structureReadyVersion change, and the previous version called
    // tryApplyElementSymbolThemeToCurrentScene (a 6s poll + viewer.setStyle rebuild) 2-4x per
    // AF<->Std toggle, which froze the browser on large structures.
    let cancelled = false;
    const timer = window.setTimeout(() => {
      if (cancelled || !viewerRef.current) return;
      focusLigandAnchor(viewerRef.current);
    }, 120);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [
    focusLigandAnchor,
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
});
