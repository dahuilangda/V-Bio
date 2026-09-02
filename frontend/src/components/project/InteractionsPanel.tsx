import { useMemo } from 'react';
import type { MolstarAtomHighlight, MolstarResidueHighlight } from './MolstarViewer';

export interface LigandInteraction {
  type: string;
  resid: string;
  restype: string;
  resnr: number | null;
  reschain: string;
  distance: number | null;
  ligand_atoms: string[];
  protein_atoms: string[];
  sidechain: boolean;
}

export interface LigandInteractionsReport {
  ligand_chain_id?: string;
  ligand_resname?: string;
  counts?: Record<string, number>;
  interactions?: LigandInteraction[];
  pocket_residues?: Array<{ resid: string; restype?: string; resnr?: number | null; distance?: number | null }>;
}

export const INTERACTION_TYPE_META: Record<string, { label: string; color: string }> = {
  hydrogen_bond: { label: 'H-bond', color: '#3b82c4' },
  hydrophobic: { label: 'Hydrophobic', color: '#d9a441' },
  salt_bridge: { label: 'Salt bridge', color: '#c05050' },
  pi_stacking: { label: 'π-stacking', color: '#7a5bb5' },
  pi_cation: { label: 'π-cation', color: '#9a6fb0' },
  halogen_bond: { label: 'Halogen', color: '#4a9e8f' },
  water_bridge: { label: 'Water bridge', color: '#5b8fb0' }
};

export function parseInteractionsFromAffinity(affinity: Record<string, unknown> | null | undefined): LigandInteractionsReport | null {
  if (!affinity) return null;
  const raw = (affinity as Record<string, unknown>).interactions;
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  return raw as LigandInteractionsReport;
}

interface InteractionsPanelProps {
  report: LigandInteractionsReport | null;
  selectedInteraction: LigandInteraction | null;
  onSelectInteraction: (interaction: LigandInteraction | null) => void;
  onAtomHighlight?: (atoms: MolstarAtomHighlight[]) => void;
  ligandChainId?: string;
  ligandResidueNumber?: number | null;
}

export function InteractionsPanel({
  report,
  selectedInteraction,
  onSelectInteraction,
  onAtomHighlight,
  ligandChainId,
  ligandResidueNumber
}: InteractionsPanelProps) {
  const grouped = useMemo(() => {
    const interactions = report?.interactions || [];
    const byType = new Map<string, LigandInteraction[]>();
    for (const item of interactions) {
      const list = byType.get(item.type) || [];
      list.push(item);
      byType.set(item.type, list);
    }
    const order = Object.keys(INTERACTION_TYPE_META)
      .filter((t) => byType.has(t))
      .concat([...byType.keys()].filter((t) => !(t in INTERACTION_TYPE_META)));
    return order.map((type) => ({ type, items: byType.get(type)! }));
  }, [report]);

  if (!report || grouped.length === 0) {
    return <div className="ligand-preview-empty">No interaction analysis available for this result.</div>;
  }

  const counts = report.counts || {};

  const handleSelect = (interaction: LigandInteraction) => {
    const isSelected =
      selectedInteraction?.type === interaction.type &&
      selectedInteraction?.resid === interaction.resid &&
      selectedInteraction?.distance === interaction.distance;
    const next = isSelected ? null : interaction;
    onSelectInteraction(next);
    if (onAtomHighlight && ligandChainId && ligandResidueNumber != null) {
      onAtomHighlight(
        next && next.ligand_atoms.length > 0
          ? next.ligand_atoms.map((atomName) => ({
              chainId: ligandChainId,
              residue: ligandResidueNumber,
              atomName,
              emphasis: 'active' as const
            }))
          : []
      );
    }
  };

  return (
    <div className="interactions-panel">
      <div className="interactions-count-row">
        {Object.entries(counts)
          .sort((a, b) => b[1] - a[1])
          .map(([type, count]) => {
            const meta = INTERACTION_TYPE_META[type];
            return (
              <span
                key={type}
                className="interaction-count-badge"
                title={meta?.label || type}
                style={{
                  borderColor: meta ? `${meta.color}66` : '#c3d3e0',
                  color: meta?.color || '#33506a'
                }}
              >
                <span className="dot" style={{ background: meta?.color || '#8aa' }} />
                {meta?.label || type} · {count}
              </span>
            );
          })}
      </div>
      {grouped.map(({ type, items }) => {
        const meta = INTERACTION_TYPE_META[type];
        return (
          <div key={type} className="interaction-group">
            <div className="interaction-group-title" style={{ color: meta?.color || undefined }}>
              {meta?.label || type}
            </div>
            {items.map((item, idx) => {
              const isSelected =
                selectedInteraction?.type === item.type &&
                selectedInteraction?.resid === item.resid &&
                selectedInteraction?.distance === item.distance;
              return (
                <button
                  key={`${item.resid}-${type}-${idx}`}
                  type="button"
                  className={`interaction-row ${isSelected ? 'selected' : ''}`}
                  onClick={() => handleSelect(item)}
                  title="Highlight this residue and its ligand atoms"
                >
                  <span className="interaction-resid">{item.resid}</span>
                  <span className="interaction-detail">
                    {item.ligand_atoms.join(', ') || '—'} ↔ {item.protein_atoms.join(', ') || '—'}
                    {item.sidechain ? ' · sc' : ''}
                  </span>
                  <span className="interaction-dist">{item.distance != null ? `${item.distance.toFixed(1)} Å` : ''}</span>
                </button>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}


export function interactionResidueHighlights(
  report: LigandInteractionsReport | null,
  selected: LigandInteraction | null
): MolstarResidueHighlight[] {
  if (!report?.interactions || report.interactions.length === 0) return [];
  const unique = new Map<string, MolstarResidueHighlight>();
  for (const item of report.interactions) {
    const chain = String(item.reschain || item.resid.split(':')[0] || '').trim();
    const residue = Number(item.resnr);
    // Number(null) === 0 would silently produce residue 0 — reject it.
    if (!chain || item.resnr == null || !Number.isFinite(residue) || residue <= 0) continue;
    const key = `${chain}:${residue}`;
    if (!unique.has(key)) {
      unique.set(key, { chainId: chain, residue, emphasis: 'default' });
    }
  }
  if (selected) {
    const chain = String(selected.reschain || selected.resid.split(':')[0] || '').trim();
    const residue = Number(selected.resnr);
    if (chain && selected.resnr != null && Number.isFinite(residue) && residue > 0) {
      unique.set(`${chain}:${residue}`, { chainId: chain, residue, emphasis: 'active' });
    }
  }
  return [...unique.values()];
}
