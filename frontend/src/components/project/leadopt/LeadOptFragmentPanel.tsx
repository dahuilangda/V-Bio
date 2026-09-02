import { LigandFragmentSketcher, type LigandFragmentItem } from '../LigandFragmentSketcher';

interface LeadOptFragmentPanelProps {
  sectionId?: string;
  effectiveLigandSmiles: string;
  fragments: LigandFragmentItem[];
  selectedFragmentIds: string[];
  activeFragmentId: string;
  onAtomClick: (atomIndex: number, options?: { additive?: boolean; preferredFragmentId?: string }) => void;
  onToggleFragmentSelection: (fragmentId: string, options?: { additive?: boolean }) => void;
  onClearFragmentSelection: () => void;
}

/**
 * Fragment selection on the reference ligand (click atoms / legend chips).
 * The picked fragments feed the halo run: fragment smiles → keep fragment,
 * atom indices → directed edit positions.
 */
export function LeadOptFragmentPanel({
  sectionId,
  effectiveLigandSmiles,
  fragments,
  selectedFragmentIds,
  activeFragmentId,
  onAtomClick,
  onToggleFragmentSelection,
  onClearFragmentSelection
}: LeadOptFragmentPanelProps) {
  return (
    <section id={sectionId} className="panel subtle lead-opt-panel">
      <div className="lead-opt-panel-title">Fragments</div>
      <LigandFragmentSketcher
        smiles={effectiveLigandSmiles}
        fragments={fragments}
        selectedFragmentIds={selectedFragmentIds}
        activeFragmentId={activeFragmentId}
        onAtomClick={onAtomClick}
        onFragmentClick={onToggleFragmentSelection}
        onBackgroundClick={onClearFragmentSelection}
        height={220}
      />
    </section>
  );
}
