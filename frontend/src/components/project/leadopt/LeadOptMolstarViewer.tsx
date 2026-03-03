import {
  MolstarViewer,
  type MolstarAtomHighlight,
  type MolstarResidueHighlight,
  type MolstarResiduePick
} from '../MolstarViewer';

interface LeadOptMolstarViewerProps {
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
  ligandFocusChainId?: string;
  suppressResidueSelection?: boolean;
  styleVariant?: 'default' | 'results';
}

export function LeadOptMolstarViewer({ styleVariant = 'default', ...props }: LeadOptMolstarViewerProps) {
  return (
    <MolstarViewer
      {...props}
      scenePreset="lead_opt"
      leadOptStyleVariant={styleVariant === 'results' ? 'results' : 'default'}
    />
  );
}

