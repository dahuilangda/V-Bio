import type { CustomCcdMoleculeInput } from '../../types/models';

export interface ResidueCatalogEntry {
  ccd: string;
  label: string;
  baseResidue: string;
  group: string;
  smiles?: string;
  custom?: boolean;
}

export const NATURAL_AMINO_ACID_RESIDUES: ResidueCatalogEntry[] = [
  { ccd: 'ALA', label: 'Alanine', baseResidue: 'A', group: 'Natural', smiles: 'N[C@@H](C)C(=O)O' },
  { ccd: 'ARG', label: 'Arginine', baseResidue: 'R', group: 'Natural', smiles: 'N[C@@H](CCCNC(=N)N)C(=O)O' },
  { ccd: 'ASN', label: 'Asparagine', baseResidue: 'N', group: 'Natural', smiles: 'N[C@@H](CC(N)=O)C(=O)O' },
  { ccd: 'ASP', label: 'Aspartic acid', baseResidue: 'D', group: 'Natural', smiles: 'N[C@@H](CC(=O)O)C(=O)O' },
  { ccd: 'CYS', label: 'Cysteine', baseResidue: 'C', group: 'Natural', smiles: 'N[C@@H](CS)C(=O)O' },
  { ccd: 'GLN', label: 'Glutamine', baseResidue: 'Q', group: 'Natural', smiles: 'N[C@@H](CCC(N)=O)C(=O)O' },
  { ccd: 'GLU', label: 'Glutamic acid', baseResidue: 'E', group: 'Natural', smiles: 'N[C@@H](CCC(=O)O)C(=O)O' },
  { ccd: 'GLY', label: 'Glycine', baseResidue: 'G', group: 'Natural', smiles: 'NCC(=O)O' },
  { ccd: 'HIS', label: 'Histidine', baseResidue: 'H', group: 'Natural', smiles: 'N[C@@H](Cc1c[nH]cn1)C(=O)O' },
  { ccd: 'ILE', label: 'Isoleucine', baseResidue: 'I', group: 'Natural', smiles: 'N[C@@H]([C@@H](C)CC)C(=O)O' },
  { ccd: 'LEU', label: 'Leucine', baseResidue: 'L', group: 'Natural', smiles: 'N[C@@H](CC(C)C)C(=O)O' },
  { ccd: 'LYS', label: 'Lysine', baseResidue: 'K', group: 'Natural', smiles: 'N[C@@H](CCCCN)C(=O)O' },
  { ccd: 'MET', label: 'Methionine', baseResidue: 'M', group: 'Natural', smiles: 'N[C@@H](CCSC)C(=O)O' },
  { ccd: 'PHE', label: 'Phenylalanine', baseResidue: 'F', group: 'Natural', smiles: 'N[C@@H](Cc1ccccc1)C(=O)O' },
  { ccd: 'PRO', label: 'Proline', baseResidue: 'P', group: 'Natural', smiles: 'N1CCC[C@H]1C(=O)O' },
  { ccd: 'SER', label: 'Serine', baseResidue: 'S', group: 'Natural', smiles: 'N[C@@H](CO)C(=O)O' },
  { ccd: 'THR', label: 'Threonine', baseResidue: 'T', group: 'Natural', smiles: 'N[C@@H]([C@H](O)C)C(=O)O' },
  { ccd: 'TRP', label: 'Tryptophan', baseResidue: 'W', group: 'Natural', smiles: 'N[C@@H](Cc1c[nH]c2ccccc12)C(=O)O' },
  { ccd: 'TYR', label: 'Tyrosine', baseResidue: 'Y', group: 'Natural', smiles: 'N[C@@H](Cc1ccc(O)cc1)C(=O)O' },
  { ccd: 'VAL', label: 'Valine', baseResidue: 'V', group: 'Natural', smiles: 'N[C@@H](C(C)C)C(=O)O' }
];

export const BUILT_IN_PROTEIN_MODIFICATIONS: ResidueCatalogEntry[] = [
  { ccd: 'AIB', label: 'alpha-aminoisobutyric acid', baseResidue: 'A', group: 'Common non-natural', smiles: 'NC(C)(C)C(=O)O' },
  { ccd: 'NLE', label: 'norleucine', baseResidue: 'L', group: 'Common non-natural', smiles: 'N[C@@H](CCCCC)C(=O)O' },
  { ccd: 'NVA', label: 'norvaline', baseResidue: 'V', group: 'Common non-natural', smiles: 'N[C@@H](CCC)C(=O)O' },
  { ccd: 'ORN', label: 'ornithine', baseResidue: 'K', group: 'Common non-natural', smiles: 'N[C@@H](CCCN)C(=O)O' },
  { ccd: 'CIT', label: 'citrulline', baseResidue: 'R', group: 'Common non-natural', smiles: 'N[C@@H](CCCNC(N)=O)C(=O)O' },
  { ccd: 'HSE', label: 'homoserine', baseResidue: 'S', group: 'Common non-natural', smiles: 'N[C@@H](CCO)C(=O)O' },
  { ccd: 'HCY', label: 'homocysteine', baseResidue: 'C', group: 'Common non-natural', smiles: 'N[C@@H](CCS)C(=O)O' },
  { ccd: 'MSE', label: 'selenomethionine', baseResidue: 'M', group: 'Common non-natural', smiles: 'N[C@@H](CC[Se]C)C(=O)O' },
  { ccd: 'SEC', label: 'selenocysteine', baseResidue: 'C', group: 'Common non-natural', smiles: 'N[C@@H](C[SeH])C(=O)O' },
  { ccd: 'HYP', label: 'hydroxyproline', baseResidue: 'P', group: 'Common non-natural', smiles: 'O=C(O)[C@@H]1CC(O)CN1' },
  { ccd: 'PCA', label: 'pyroglutamic acid', baseResidue: 'E', group: 'Common non-natural', smiles: 'O=C(O)[C@@H]1CCC(=O)N1' },
  { ccd: 'SEP', label: 'phosphoserine', baseResidue: 'S', group: 'PTM', smiles: 'N[C@@H](COP(=O)(O)O)C(=O)O' },
  { ccd: 'TPO', label: 'phosphothreonine', baseResidue: 'T', group: 'PTM', smiles: 'N[C@@H]([C@H](C)OP(=O)(O)O)C(=O)O' },
  { ccd: 'PTR', label: 'phosphotyrosine', baseResidue: 'Y', group: 'PTM', smiles: 'N[C@@H](Cc1ccc(OP(=O)(O)O)cc1)C(=O)O' },
  { ccd: 'CSO', label: 'S-hydroxycysteine', baseResidue: 'C', group: 'PTM', smiles: 'N[C@@H](CSO)C(=O)O' },
  { ccd: 'MLY', label: 'N6-methyllysine', baseResidue: 'K', group: 'PTM', smiles: 'N[C@@H](CCCCNC)C(=O)O' },
  { ccd: 'DAL', label: 'D-alanine', baseResidue: 'A', group: 'D-amino acid', smiles: 'N[C@H](C)C(=O)O' },
  { ccd: 'BALA', label: 'beta-alanine', baseResidue: 'A', group: 'Backbone variant', smiles: 'NCCC(=O)O' },
  { ccd: 'MANS', label: 'O-Man-Ser', baseResidue: 'S', group: 'Glycosylation', smiles: 'N[C@@H](CO[C@H]1O[C@@H](CO)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O' },
  { ccd: 'MANT', label: 'O-Man-Thr', baseResidue: 'T', group: 'Glycosylation', smiles: 'N[C@@H]([C@H](C)O[C@H]1O[C@@H](CO)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O' },
  { ccd: 'MANN', label: 'N-Man-Asn', baseResidue: 'N', group: 'Glycosylation', smiles: 'N[C@@H](CC(=O)N[C@H]1O[C@@H](CO)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O' },
  { ccd: 'NAGS', label: 'O-GlcNAc-Ser', baseResidue: 'S', group: 'Glycosylation', smiles: 'N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(C)=O)C(=O)O' },
  { ccd: 'NAGT', label: 'O-GlcNAc-Thr', baseResidue: 'T', group: 'Glycosylation', smiles: 'N[C@@H]([C@H](C)O[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(C)=O)C(=O)O' },
  { ccd: 'NAGN', label: 'N-GlcNAc-Asn', baseResidue: 'N', group: 'Glycosylation', smiles: 'N[C@@H](CC(=O)N[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1NC(C)=O)C(=O)O' },
  { ccd: 'GALS', label: 'O-Gal-Ser', baseResidue: 'S', group: 'Glycosylation', smiles: 'N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@@H](O)[C@H]1O)C(=O)O' },
  { ccd: 'GALT', label: 'O-Gal-Thr', baseResidue: 'T', group: 'Glycosylation', smiles: 'N[C@@H]([C@H](C)O[C@H]1O[C@H](CO)[C@@H](O)[C@@H](O)[C@H]1O)C(=O)O' },
  { ccd: 'FUCS', label: 'O-Fuc-Ser', baseResidue: 'S', group: 'Glycosylation', smiles: 'N[C@@H](CO[C@H]1O[C@@H](C)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O' },
  { ccd: 'GLCS', label: 'O-Glc-Ser', baseResidue: 'S', group: 'Glycosylation', smiles: 'N[C@@H](CO[C@H]1O[C@H](CO)[C@@H](O)[C@H](O)[C@@H]1O)C(=O)O' },
  { ccd: 'XYLS', label: 'O-Xyl-Ser', baseResidue: 'S', group: 'Glycosylation', smiles: 'N[C@@H](CO[C@H]1O[C@@H](O)[C@H](O)[C@@H](O)[C@@H]1O)C(=O)O' }
];

export function normalizeResidueCode(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, '').toUpperCase().slice(0, 12);
}

export function buildCustomResidueCatalog(library: CustomCcdMoleculeInput[] = []): ResidueCatalogEntry[] {
  const seen = new Set<string>();
  const rows: ResidueCatalogEntry[] = [];
  for (const item of library) {
    const ccd = normalizeResidueCode(item.ccd);
    const smiles = String(item.smiles || '').trim();
    if (!ccd || !smiles || seen.has(ccd)) continue;
    seen.add(ccd);
    rows.push({
      ccd,
      label: String(item.label || 'Custom residue').trim() || 'Custom residue',
      baseResidue: String(item.baseResidue || 'A').trim().toUpperCase().slice(0, 1) || 'A',
      group: 'Custom library',
      smiles,
      custom: true
    });
  }
  return rows;
}
