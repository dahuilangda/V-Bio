import { useMemo, type ReactNode } from 'react';
import { type MolstarAtomHighlight, type MolstarResidueHighlight, type MolstarResiduePick } from '../MolstarViewer';
import { LeadOptMolstarViewer } from './LeadOptMolstarViewer';
import { combineLigandAndBoxOverlay } from '../../../utils/pocketBox';
import { InfoTip } from '../../../components/common/InfoTip';
import { Field } from '../../common/Field';

interface LeadOptReferencePanelProps {
  sectionId?: string;
  isEditable: boolean;
  isLoading: boolean;
  isSubmitting: boolean;
  isReferenceReady: boolean;
  previewStructureText: string;
  previewStructureFormat: 'cif' | 'pdb';
  previewOverlayStructureText: string;
  previewOverlayStructureFormat: 'cif' | 'pdb';
  /** Docking-style pocket box wireframe; takes priority over the ligand overlay. */
  boxOverlayText?: string;
  /** Toolbar (Box / Clear) rendered next to the status line once the reference is ready. */
  pocketToolbar?: ReactNode;
  /** PocketBoxControls drawer rendered inside the viewer while open. */
  pocketControls?: ReactNode;
  ligandChain: string;
  highlightedLigandAtoms: MolstarAtomHighlight[];
  highlightedPocketResidues: MolstarResidueHighlight[];
  activeMolstarAtom: MolstarAtomHighlight | null;
  onResiduePick: (pick: MolstarResiduePick) => void;
  targetFileChangeAction: (file: File | null) => Promise<void>;
  ligandFileChangeAction: (file: File | null) => Promise<void>;
}

export function LeadOptReferencePanel({
  sectionId,
  isEditable,
  isLoading,
  isSubmitting,
  isReferenceReady,
  previewStructureText,
  previewStructureFormat,
  previewOverlayStructureText,
  previewOverlayStructureFormat,
  boxOverlayText = '',
  pocketToolbar,
  pocketControls,
  ligandChain,
  highlightedLigandAtoms,
  highlightedPocketResidues,
  activeMolstarAtom,
  onResiduePick,
  targetFileChangeAction,
  ligandFileChangeAction
}: LeadOptReferencePanelProps) {
  const activeLigandResidue = useMemo<MolstarResidueHighlight | null>(() => {
    const anchor = activeMolstarAtom || highlightedLigandAtoms[0] || null;
    if (!anchor) return null;
    const chainId = String(anchor.chainId || '').trim();
    const residue = Math.floor(Number(anchor.residue) || 0);
    if (!chainId || residue <= 0) return null;
    return { chainId, residue, emphasis: 'active' };
  }, [activeMolstarAtom, highlightedLigandAtoms]);

  const displayLigandAtoms = useMemo<MolstarAtomHighlight[]>(
    () =>
      highlightedLigandAtoms.map((item) => ({
        ...item,
        emphasis: 'default'
      })),
    [highlightedLigandAtoms]
  );

  const hasStructure = Boolean(previewStructureText.trim());
  // One overlay slot carries both the ligand and the box wireframe so the
  // ligand stays visible once the box is drawn.
  const combinedOverlay = useMemo(() => {
    if (!hasStructure) return { text: '', format: 'pdb' as const };
    const ligandOverlay = previewOverlayStructureText.trim();
    const box = boxOverlayText.trim();
    if (box) return combineLigandAndBoxOverlay(ligandOverlay, previewOverlayStructureFormat, box);
    return { text: ligandOverlay, format: previewOverlayStructureFormat };
  }, [boxOverlayText, hasStructure, previewOverlayStructureFormat, previewOverlayStructureText]);

  return (
    <section id={sectionId} className="panel subtle lead-opt-panel lead-opt-panel--reference">
      <div className="lead-opt-reference-grid">
        <Field label="Target (PDB/CIF)">
          <input
            type="file"
            className="file-input-unified"
            accept=".pdb,.cif,.mmcif,.ent"
            onChange={async (event) => {
              const input = event.currentTarget;
              const nextTarget = event.target.files?.[0] || null;
              await targetFileChangeAction(nextTarget);
              input.value = '';
            }}
            disabled={!isEditable || isLoading || isSubmitting}
          />
        </Field>
        <Field label="Ligand (SDF/MOL2/PDB/CIF)">
          <input
            type="file"
            className="file-input-unified"
            accept=".sdf,.sd,.mol2,.mol,.pdb,.ent,.cif,.mmcif"
            onChange={async (event) => {
              const input = event.currentTarget;
              const nextLigand = event.target.files?.[0] || null;
              await ligandFileChangeAction(nextLigand);
              input.value = '';
            }}
            disabled={!isEditable || isLoading || isSubmitting}
          />
        </Field>
      </div>
      <div className="lead-opt-reference-status">
        <p className="small muted">
          {isLoading
            ? 'Parsing reference…'
            : isReferenceReady
              ? (
                <>
                  Reference ready
                  <InfoTip text="Pocket box sits on the ligand; fragments and the 3D view stay in sync." align="start" />
                </>
              )
              : 'Upload target + ligand to start.'}
        </p>
        {isReferenceReady ? pocketToolbar : null}
      </div>
      <div className="lead-opt-structure-panel">
        {hasStructure ? (
          <LeadOptMolstarViewer
            structureText={previewStructureText}
            format={previewStructureFormat}
            overlayStructureText={combinedOverlay.text || undefined}
            overlayFormat={combinedOverlay.format}
            colorMode="default"
            ligandFocusChainId={ligandChain}
            onResiduePick={onResiduePick}
            highlightResidues={highlightedPocketResidues}
            isResidueSelectionSuppressed
            highlightAtoms={displayLigandAtoms}
            activeResidue={activeLigandResidue}
            activeAtom={null}
            interactionGranularity="element"
            isAutoFocusSuppressed={false}
          />
        ) : (
          <div className="ligand-preview-empty">
            {isLoading ? 'Rendering reference…' : 'Upload reference target+ligand to view 3D.'}
          </div>
        )}
        {hasStructure ? pocketControls : null}
      </div>
    </section>
  );
}
