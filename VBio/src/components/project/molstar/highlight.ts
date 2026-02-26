import { applyLigandSpotlight } from './spotlight';
import type { MolstarAtomHighlight, MolstarResidueHighlight } from './types';
import { buildAtomLoci, buildResidueLoci } from './loci';

const EMPTY_HIGHLIGHTS: MolstarResidueHighlight[] = [];
const EMPTY_ATOM_HIGHLIGHTS: MolstarAtomHighlight[] = [];

interface ApplyMolstarHighlightsArgs {
  viewer: any;
  structureText: string;
  highlightResidues?: MolstarResidueHighlight[];
  activeResidue?: MolstarResidueHighlight | null;
  highlightAtoms?: MolstarAtomHighlight[];
  activeAtom?: MolstarAtomHighlight | null;
  suppressAutoFocus: boolean;
  disableExternalFocus?: boolean;
  hadExternalHighlightsRef: { current: boolean };
  suppressPickEventsRef: { current: boolean };
}

export function applyMolstarHighlights({
  viewer,
  structureText,
  highlightResidues,
  activeResidue,
  highlightAtoms,
  activeAtom,
  suppressAutoFocus,
  disableExternalFocus,
  hadExternalHighlightsRef,
  suppressPickEventsRef
}: ApplyMolstarHighlightsArgs) {
  if (!viewer || !structureText.trim()) return;

  const selectionManager = viewer?.plugin?.managers?.structure?.selection;
  const focusManager = viewer?.plugin?.managers?.structure?.focus;
  if (!selectionManager?.fromLoci || !selectionManager?.clear) return;

  const highlightSource = highlightResidues ?? EMPTY_HIGHLIGHTS;
  const atomHighlightSource = highlightAtoms ?? EMPTY_ATOM_HIGHLIGHTS;

  const normalized = Array.from(
    new Map(
      highlightSource
        .filter((item) => Boolean(item?.chainId) && Number.isFinite(item?.residue) && Math.floor(Number(item.residue)) > 0)
        .map((item) => [
          `${item.chainId}:${Math.floor(Number(item.residue))}`,
          { ...item, residue: Math.floor(Number(item.residue)) }
        ])
    ).values()
  ) as MolstarResidueHighlight[];

  const normalizedAtoms = Array.from(
    new Map(
      atomHighlightSource
        .filter(
          (item) =>
            Boolean(item?.chainId) &&
            Number.isFinite(item?.residue) &&
            Math.floor(Number(item.residue)) > 0 &&
            (Boolean(String(item?.atomName || '').trim()) ||
              (Number.isFinite(item?.atomIndex) && Math.floor(Number(item.atomIndex)) >= 0))
        )
        .map((item) => {
          const residueNumber = Math.floor(Number(item.residue));
          const atomName = String(item.atomName || '').trim();
          const normalizedAtomIndex =
            Number.isFinite(item?.atomIndex) && Number(item.atomIndex) >= 0 ? Math.floor(Number(item.atomIndex)) : undefined;
          const key =
            atomName || normalizedAtomIndex === undefined
              ? `${item.chainId}:${residueNumber}:${atomName}`
              : `${item.chainId}:${residueNumber}:idx-${normalizedAtomIndex}`;
          return [key, { ...item, residue: residueNumber, atomName, atomIndex: normalizedAtomIndex }];
        })
    ).values()
  ) as MolstarAtomHighlight[];

  const hasExternalHighlights = normalized.length > 0 || normalizedAtoms.length > 0 || Boolean(activeResidue) || Boolean(activeAtom);

  if (!hasExternalHighlights) {
    if (!hadExternalHighlightsRef.current) return;
    hadExternalHighlightsRef.current = false;
    suppressPickEventsRef.current = true;
    try {
      selectionManager.clear();
      focusManager?.clear?.();
      void applyLigandSpotlight(viewer, false);
    } catch {
      // no-op
    } finally {
      window.setTimeout(() => {
        suppressPickEventsRef.current = false;
      }, 0);
    }
    return;
  }

  hadExternalHighlightsRef.current = true;
  suppressPickEventsRef.current = true;

  try {
    selectionManager.clear();
    for (const item of normalized) {
      const loci = buildResidueLoci(viewer, item.chainId, item.residue);
      if (!loci) continue;
      selectionManager.fromLoci('add', loci);
    }
    for (const item of normalizedAtoms) {
      const loci = buildAtomLoci(viewer, item.chainId, item.residue, item.atomName, item.atomIndex);
      if (!loci) continue;
      selectionManager.fromLoci('add', loci);
    }

    const focusAtomTarget = activeAtom || normalizedAtoms.find((item) => item.emphasis === 'active') || null;
    const focusResidueTarget = activeResidue || normalized.find((item) => item.emphasis === 'active') || null;
    const lociHighlights = viewer?.plugin?.managers?.interactivity?.lociHighlights;

    if (!disableExternalFocus && !suppressAutoFocus && focusManager?.setFromLoci) {
      if (focusAtomTarget) {
        const atomFocusLoci = buildAtomLoci(
          viewer,
          focusAtomTarget.chainId,
          focusAtomTarget.residue,
          focusAtomTarget.atomName,
          focusAtomTarget.atomIndex
        );
        if (atomFocusLoci) {
          focusManager.setFromLoci(atomFocusLoci);
          if (lociHighlights?.highlightOnly) {
            try {
              lociHighlights.highlightOnly({ loci: atomFocusLoci });
            } catch {
              // no-op
            }
          }
        }
      } else if (focusResidueTarget) {
        const residueFocusLoci = buildResidueLoci(viewer, focusResidueTarget.chainId, focusResidueTarget.residue);
        if (residueFocusLoci) {
          focusManager.setFromLoci(residueFocusLoci);
          if (lociHighlights?.clearHighlights) {
            try {
              lociHighlights.clearHighlights();
            } catch {
              // no-op
            }
          }
        }
      }
    }

    void applyLigandSpotlight(viewer, normalizedAtoms.length > 0 || normalized.length > 0, 0.28);
  } catch {
    // no-op
  } finally {
    window.setTimeout(() => {
      suppressPickEventsRef.current = false;
    }, 0);
  }
}
