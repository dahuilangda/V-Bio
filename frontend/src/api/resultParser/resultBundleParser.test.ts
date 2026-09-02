import { describe, it, expect } from 'vitest';
import JSZip from 'jszip';
import { parseResultBundle } from './resultBundleParser';

const MINIMAL_CIF = [
  'data_test',
  'loop_',
  '_atom_site.group_PDB',
  '_atom_site.id',
  '_atom_site.type_symbol',
  '_atom_site.label_atom_id',
  '_atom_site.label_comp_id',
  '_atom_site.label_asym_id',
  '_atom_site.auth_seq_id',
  '_atom_site.Cartn_x',
  '_atom_site.Cartn_y',
  '_atom_site.Cartn_z',
  '_atom_site.B_iso_or_equiv',
  'ATOM 1 C CA ALA A 1 0.000 0.000 0.000 85.0',
  'HETATM 2 C C1 LIG L 1 2.000 2.000 2.000 90.0',
  '#'
].join('\n');

async function buildResultZip(files: Record<string, string>): Promise<Blob> {
  const zip = new JSZip();
  for (const [name, content] of Object.entries(files)) {
    zip.file(name, content);
  }
  // JSZip in the node test environment cannot consume a Blob directly; hand it
  // the ArrayBuffer (accepted at runtime, cast only for the Blob-typed API).
  const buffer = await zip.generateAsync({ type: 'arraybuffer' });
  return buffer as unknown as Blob;
}

const BASE_FILES = {
  'rec1/best_model.cif': MINIMAL_CIF,
  'rec1/best_confidence.json': JSON.stringify({
    confidence_score: 0.88,
    iptm: 0.91,
    complex_plddt: 0.87
  }),
  'rec1/best_ipsae.json': JSON.stringify({
    ipsae_dom: 0.72,
    ligand_ipsae_max: 0.81
  }),
  'rec1/affinity_rec1.json': JSON.stringify({
    affinity_pred_value: -0.4,
    affinity_pic50: 6.4,
    affinity_pic50_mw: 6.7,
    affinity_probability_binary: 0.62
  })
};

const INTERACTIONS_PAYLOAD = {
  record_id: 'rec1',
  ligand_chain_id: 'Lx1',
  ligand_resname: 'LIG',
  counts: { hydrogen_bond: 1, hydrophobic: 2 },
  interactions: [
    {
      type: 'hydrogen_bond',
      resid: 'Axp:ALA104',
      restype: 'ALA',
      resnr: 104,
      reschain: 'Axp',
      distance: 3.08,
      ligand_atoms: ['N001'],
      protein_atoms: ['N'],
      sidechain: false
    }
  ],
  pocket_residues: [{ resid: 'Axp:ALA104', restype: 'ALA', resnr: 104, distance: 3.08 }]
};

describe('parseResultBundle — boltz2score affinity/interactions flow', () => {
  it('merges best_interactions.json into the affinity record', async () => {
    const blob = await buildResultZip({
      ...BASE_FILES,
      'rec1/best_interactions.json': JSON.stringify(INTERACTIONS_PAYLOAD)
    });
    const parsed = await parseResultBundle(blob);
    expect(parsed).not.toBeNull();
    const affinity = parsed!.affinity as Record<string, unknown>;
    expect(affinity.affinity_pic50).toBe(6.4);
    expect(affinity.interactions).toEqual(INTERACTIONS_PAYLOAD);
    expect(parsed!.structureText).toContain('LIG');
  });

  it('keeps affinity intact when no interactions file exists (legacy bundles)', async () => {
    const blob = await buildResultZip(BASE_FILES);
    const parsed = await parseResultBundle(blob);
    expect(parsed).not.toBeNull();
    const affinity = parsed!.affinity as Record<string, unknown>;
    expect(affinity.affinity_pic50).toBe(6.4);
    expect(affinity.interactions).toBeUndefined();
  });

  it('merges best_ipsae.json into confidence', async () => {
    const blob = await buildResultZip(BASE_FILES);
    const parsed = await parseResultBundle(blob);
    expect((parsed!.confidence as Record<string, unknown>).ipsae_dom).toBeCloseTo(0.72);
  });

  it('returns null when no structure file is present', async () => {
    const blob = await buildResultZip({
      'rec1/best_confidence.json': BASE_FILES['rec1/best_confidence.json']
    });
    expect(await parseResultBundle(blob)).toBeNull();
  });

  it('ignores per-record interactions files when only the alias is canonical', async () => {
    // Old worker output style: interactions_<record>.json only, no alias yet.
    const blob = await buildResultZip({
      ...BASE_FILES,
      'rec1/interactions_rec1.json': JSON.stringify(INTERACTIONS_PAYLOAD)
    });
    const parsed = await parseResultBundle(blob);
    const affinity = parsed!.affinity as Record<string, unknown>;
    // Current contract reads the best_interactions alias; per-record files alone
    // are not merged (documented behavior, not a bug).
    expect(affinity.interactions).toBeUndefined();
  });
});
