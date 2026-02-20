import {
  MolstarViewer,
  type MolstarAtomHighlight,
  type MolstarResidueHighlight,
  type MolstarResiduePick
} from '../MolstarViewer';

interface LeadOptReferencePanelProps {
  sectionId?: string;
  canEdit: boolean;
  loading: boolean;
  submitting: boolean;
  referenceReady: boolean;
  previewStructureText: string;
  previewStructureFormat: 'cif' | 'pdb';
  previewOverlayStructureText: string;
  previewOverlayStructureFormat: 'cif' | 'pdb';
  ligandChain: string;
  highlightedLigandAtoms: MolstarAtomHighlight[];
  highlightedPocketResidues: MolstarResidueHighlight[];
  activeMolstarAtom: MolstarAtomHighlight | null;
  onResiduePick: (pick: MolstarResiduePick) => void;
  onTargetFileChange: (file: File | null) => Promise<void>;
  onLigandFileChange: (file: File | null) => Promise<void>;
}

export function LeadOptReferencePanel({
  sectionId,
  canEdit,
  loading,
  submitting,
  referenceReady,
  previewStructureText,
  previewStructureFormat,
  previewOverlayStructureText,
  previewOverlayStructureFormat,
  ligandChain,
  highlightedLigandAtoms,
  highlightedPocketResidues,
  activeMolstarAtom,
  onResiduePick,
  onTargetFileChange,
  onLigandFileChange
}: LeadOptReferencePanelProps) {
  return (
    <section id={sectionId} className="panel subtle lead-opt-panel">
      <div className="lead-opt-reference-grid">
        <label className="field">
          <span>Target (PDB/CIF)</span>
          <input
            type="file"
            accept=".pdb,.cif,.mmcif,.ent"
            onChange={async (event) => {
              const nextTarget = event.target.files?.[0] || null;
              await onTargetFileChange(nextTarget);
            }}
            disabled={!canEdit || loading || submitting}
          />
        </label>
        <label className="field">
          <span>Ligand (SDF/MOL2/PDB/CIF)</span>
          <input
            type="file"
            accept=".sdf,.sd,.mol2,.mol,.pdb,.ent,.cif,.mmcif"
            onChange={async (event) => {
              const nextLigand = event.target.files?.[0] || null;
              await onLigandFileChange(nextLigand);
            }}
            disabled={!canEdit || loading || submitting}
          />
        </label>
      </div>
      <p className="small muted">
        {referenceReady
          ? 'Reference ready. Fragment and 3D stay synced.'
          : 'Upload target + ligand to start.'}
      </p>
      <div className="lead-opt-structure-panel">
        {previewStructureText ? (
          <MolstarViewer
            structureText={previewStructureText}
            format={previewStructureFormat}
            overlayStructureText={previewOverlayStructureText}
            overlayFormat={previewOverlayStructureFormat}
            colorMode="white"
            scenePreset="lead_opt"
            ligandFocusChainId={ligandChain}
            onResiduePick={onResiduePick}
            highlightResidues={highlightedPocketResidues}
            suppressResidueSelection
            highlightAtoms={highlightedLigandAtoms}
            activeAtom={activeMolstarAtom}
            interactionGranularity="element"
            suppressAutoFocus={false}
          />
        ) : (
          <div className="ligand-preview-empty">Upload reference target+ligand to view 3D.</div>
        )}
      </div>
    </section>
  );
}
