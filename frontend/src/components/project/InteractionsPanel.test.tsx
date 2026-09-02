import { describe, it, expect } from 'vitest';
import { renderToString } from 'react-dom/server';
import {
  InteractionsPanel,
  INTERACTION_TYPE_META,
  interactionResidueHighlights,
  parseInteractionsFromAffinity,
  type LigandInteraction,
  type LigandInteractionsReport
} from './InteractionsPanel';

function interaction(overrides: Partial<LigandInteraction> = {}): LigandInteraction {
  return {
    type: 'hydrogen_bond',
    resid: 'Axp:ALA104',
    restype: 'ALA',
    resnr: 104,
    reschain: 'Axp',
    distance: 3.1,
    ligand_atoms: ['N001'],
    protein_atoms: ['N'],
    sidechain: false,
    ...overrides
  };
}

describe('parseInteractionsFromAffinity', () => {
  it('extracts the embedded interactions report', () => {
    const report: LigandInteractionsReport = {
      counts: { hydrogen_bond: 1 },
      interactions: [interaction()]
    };
    expect(parseInteractionsFromAffinity({ affinity_pic50: 6.2, interactions: report })).toBe(report);
  });

  it('returns null for missing or malformed payloads', () => {
    expect(parseInteractionsFromAffinity(null)).toBeNull();
    expect(parseInteractionsFromAffinity({})).toBeNull();
    expect(parseInteractionsFromAffinity({ interactions: [1, 2, 3] })).toBeNull();
    expect(parseInteractionsFromAffinity({ interactions: 'nope' })).toBeNull();
  });
});

describe('interactionResidueHighlights', () => {
  it('dedupes residues across interactions and marks the selected one active', () => {
    const report: LigandInteractionsReport = {
      interactions: [
        interaction(),
        interaction({ type: 'hydrophobic', resid: 'Axp:VAL31', restype: 'VAL', resnr: 31, reschain: 'Axp' }),
        interaction({ type: 'salt_bridge', resid: 'Axp:ALA104', restype: 'ALA', resnr: 104, reschain: 'Axp' })
      ]
    };
    const selected = interaction();
    const highlights = interactionResidueHighlights(report, selected);
    expect(highlights).toHaveLength(2);
    expect(highlights.find((h) => h.chainId === 'Axp' && h.residue === 104)?.emphasis).toBe('active');
    expect(highlights.find((h) => h.chainId === 'Axp' && h.residue === 31)?.emphasis).toBe('default');
  });

  it('skips entries with non-numeric residue numbers', () => {
    const report: LigandInteractionsReport = {
      interactions: [interaction({ resnr: null as unknown as number })]
    };
    expect(interactionResidueHighlights(report, null)).toHaveLength(0);
  });

  it('returns empty for null report', () => {
    expect(interactionResidueHighlights(null, null)).toHaveLength(0);
  });
});

describe('InteractionsPanel render', () => {
  const baseProps = {
    selectedInteraction: null,
    onSelectInteraction: () => {},
    ligandChainId: 'Lx1',
    ligandResidueNumber: 1
  };

  it('renders empty state without a report', () => {
    const html = renderToString(<InteractionsPanel {...baseProps} report={null} />);
    expect(html).toContain('No interaction analysis available');
  });

  it('renders count badges, groups, and interaction rows', () => {
    const report: LigandInteractionsReport = {
      counts: { hydrogen_bond: 1, hydrophobic: 2 },
      interactions: [
        interaction(),
        interaction({ type: 'hydrophobic', resid: 'Axp:VAL31', restype: 'VAL', resnr: 31, distance: 3.9, ligand_atoms: ['C00B'], protein_atoms: ['CG2'] }),
        interaction({ type: 'hydrophobic', resid: 'Axp:ILE83', restype: 'ILE', resnr: 83, distance: 3.8, ligand_atoms: ['C003'], protein_atoms: ['CG1'] })
      ]
    };
    const html = renderToString(<InteractionsPanel {...baseProps} report={report} />).replace(/<!-- -->/g, '');
    expect(html).toContain('H-bond · 1');
    expect(html).toContain('Hydrophobic · 2');
    expect(html).toContain('Axp:ALA104');
    expect(html).toContain('N001 ↔ N');
    expect(html).toContain('3.1 Å');
    expect((html.match(/interaction-row/g) || []).length).toBeGreaterThanOrEqual(3);
  });

  it('marks the selected interaction row', () => {
    const selected = interaction();
    const report: LigandInteractionsReport = { interactions: [selected] };
    const html = renderToString(
      <InteractionsPanel {...baseProps} report={report} selectedInteraction={selected} />
    );
    expect(html).toContain('interaction-row selected');
  });

  it('renders unknown interaction types with a plain label', () => {
    const report: LigandInteractionsReport = {
      counts: { metal_coordination: 1 },
      interactions: [interaction({ type: 'metal_coordination', resid: 'Axp:HIS100', restype: 'HIS', resnr: 100 })]
    };
    const html = renderToString(<InteractionsPanel {...baseProps} report={report} />);
    expect(html).toContain('metal_coordination');
  });

  it('exposes stable type metadata for every standard type', () => {
    for (const key of Object.keys(INTERACTION_TYPE_META)) {
      expect(INTERACTION_TYPE_META[key].label).toBeTruthy();
      expect(INTERACTION_TYPE_META[key].color).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });
});
