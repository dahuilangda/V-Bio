import { BUILT_IN_PROTEIN_MODIFICATIONS, NATURAL_AMINO_ACID_RESIDUES } from '../components/project/residueCatalog';
import type { InputComponent, ProteinModification } from '../types/models';
import { buildChainInfos } from './chainAssignments';
import type { StructureAtomOptionsByChain, StructureResidueAtomOption } from './structureParser';

const DEFAULT_PROTEIN_BACKBONE_ATOMS = ['N', 'CA', 'C', 'O', 'CB'];
const GLY_ATOMS = ['N', 'CA', 'C', 'O'];
const CUSTOM_RESIDUE_BACKBONE_ATOMS = ['N', 'CA', 'C', 'O', 'OXT'];

const NATURAL_ATOMS_BY_ONE_LETTER: Record<string, string[]> = {
  A: ['N', 'CA', 'C', 'O', 'CB'],
  R: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'NE', 'CZ', 'NH1', 'NH2'],
  N: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'OD1', 'ND2'],
  D: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'OD1', 'OD2'],
  C: ['N', 'CA', 'C', 'O', 'CB', 'SG'],
  Q: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'OE1', 'NE2'],
  E: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'OE1', 'OE2'],
  G: GLY_ATOMS,
  H: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'ND1', 'CD2', 'CE1', 'NE2'],
  I: ['N', 'CA', 'C', 'O', 'CB', 'CG1', 'CG2', 'CD1'],
  L: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2'],
  K: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'CE', 'NZ'],
  M: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'SD', 'CE'],
  F: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2', 'CE1', 'CE2', 'CZ'],
  P: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD'],
  S: ['N', 'CA', 'C', 'O', 'CB', 'OG'],
  T: ['N', 'CA', 'C', 'O', 'CB', 'OG1', 'CG2'],
  W: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2', 'NE1', 'CE2', 'CE3', 'CZ2', 'CZ3', 'CH2'],
  Y: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2', 'CE1', 'CE2', 'CZ', 'OH'],
  V: ['N', 'CA', 'C', 'O', 'CB', 'CG1', 'CG2']
};

const PRESET_MOD_ATOMS_BY_CCD: Record<string, string[]> = {
  AIB: ['N', 'CA', 'C', 'O', 'CB1', 'CB2'],
  BALA: ['N', 'CA', 'CB', 'C', 'O'],
  CIT: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'NE', 'CZ', 'OXT', 'NH2'],
  CSO: ['N', 'CA', 'C', 'O', 'CB', 'SG', 'OD'],
  DAL: NATURAL_ATOMS_BY_ONE_LETTER.A,
  GALS: ['N', 'CA', 'C', 'O', 'CB', 'OG', 'C1', 'C2', 'C3', 'C4', 'C5', 'O5', 'O1', 'O2', 'O3', 'O4', 'C6', 'O6'],
  GALT: ['N', 'CA', 'C', 'O', 'CB', 'OG1', 'CG2', 'C1', 'C2', 'C3', 'C4', 'C5', 'O5', 'O1', 'O2', 'O3', 'O4', 'C6', 'O6'],
  FUCS: ['N', 'CA', 'C', 'O', 'CB', 'OG', 'C1', 'C2', 'C3', 'C4', 'C5', 'O5', 'O1', 'O2', 'O3', 'O4', 'C6'],
  GLCS: ['N', 'CA', 'C', 'O', 'CB', 'OG', 'C1', 'C2', 'C3', 'C4', 'C5', 'O5', 'O1', 'O2', 'O3', 'O4', 'C6', 'O6'],
  HCY: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'SG'],
  HSE: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'OG'],
  HYP: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'OD1'],
  MANN: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'OD1', 'ND2', 'C1', 'C2', 'C3', 'C4', 'C5', 'O5', 'O1', 'O2', 'O3', 'O4', 'C6', 'O6'],
  MANS: ['N', 'CA', 'C', 'O', 'CB', 'OG', 'C1', 'C2', 'C3', 'C4', 'C5', 'O5', 'O1', 'O2', 'O3', 'O4', 'C6', 'O6'],
  MANT: ['N', 'CA', 'C', 'O', 'CB', 'OG1', 'CG2', 'C1', 'C2', 'C3', 'C4', 'C5', 'O5', 'O1', 'O2', 'O3', 'O4', 'C6', 'O6'],
  MLY: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'CE', 'NZ', 'CM'],
  MSE: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'SE', 'CE'],
  NAGS: ['N', 'CA', 'C', 'O', 'CB', 'OG', 'C1', 'C2', 'N2', 'C7', 'O7', 'C8', 'C3', 'C4', 'C5', 'O5', 'O1', 'O3', 'O4', 'C6', 'O6'],
  NAGT: ['N', 'CA', 'C', 'O', 'CB', 'OG1', 'CG2', 'C1', 'C2', 'N2', 'C7', 'O7', 'C8', 'C3', 'C4', 'C5', 'O5', 'O1', 'O3', 'O4', 'C6', 'O6'],
  NAGN: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'OD1', 'ND2', 'C1', 'C2', 'N2', 'C7', 'O7', 'C8', 'C3', 'C4', 'C5', 'O5', 'O1', 'O3', 'O4', 'C6', 'O6'],
  NLE: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'CE', 'CZ'],
  NVA: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD'],
  ORN: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'NE'],
  PCA: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD', 'OE'],
  PTR: ['N', 'CA', 'C', 'O', 'CB', 'CG', 'CD1', 'CD2', 'CE1', 'CE2', 'CZ', 'OH', 'P', 'O1P', 'O2P', 'O3P'],
  SEC: ['N', 'CA', 'C', 'O', 'CB', 'SEG'],
  SEP: ['N', 'CA', 'C', 'O', 'CB', 'OG', 'P', 'O1P', 'O2P', 'O3P'],
  TPO: ['N', 'CA', 'C', 'O', 'CB', 'OG1', 'CG2', 'P', 'O1P', 'O2P', 'O3P'],
  XYLS: ['N', 'CA', 'C', 'O', 'CB', 'OG', 'C1', 'C2', 'C3', 'C4', 'C5', 'O5', 'O1', 'O2', 'O3', 'O4']
};

const NATURAL_ATOMS_BY_CCD = Object.fromEntries(NATURAL_AMINO_ACID_RESIDUES.map((entry) => [entry.ccd, NATURAL_ATOMS_BY_ONE_LETTER[entry.baseResidue] || DEFAULT_PROTEIN_BACKBONE_ATOMS]));
const BUILT_IN_MOD_BY_CCD = new Map(BUILT_IN_PROTEIN_MODIFICATIONS.map((entry) => [entry.ccd, entry]));

function cleanSequence(value: string): string {
  return String(value || '').replace(/\s+/g, '').toUpperCase();
}

function uniqueAtoms(values: string[]): string[] {
  const seen = new Set<string>();
  const atoms: string[] = [];
  for (const value of values) {
    const atom = String(value || '').replace(/\s+/g, '').trim().toUpperCase();
    if (!atom || seen.has(atom)) continue;
    seen.add(atom);
    atoms.push(atom);
  }
  return atoms;
}


export function ligandAtomNamesFromSmilesByElementOrder(smiles: string): string[] {
  const elementCounts = new Map<string, number>();
  const atoms: string[] = [];
  const source = String(smiles || '');
  const pattern = /\[([^\]]+)\]|Br|Cl|Si|Se|Na|Li|Mg|Ca|Zn|Fe|[BCNOFPSIKbcnops]/g;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(source))) {
    const bracket = match[1];
    let symbol = match[0];
    if (bracket) {
      const bracketMatch = bracket.match(/^[0-9]*([A-Z][a-z]?|[cnops])/);
      if (!bracketMatch) continue;
      symbol = bracketMatch[1];
    }
    const normalized = symbol.length === 1 ? symbol.toUpperCase() : `${symbol[0].toUpperCase()}${symbol.slice(1).toLowerCase()}`;
    const upper = normalized.toUpperCase();
    if (upper === 'H') continue;
    const next = (elementCounts.get(upper) || 0) + 1;
    elementCounts.set(upper, next);
    atoms.push(`${upper}${next}`);
  }
  return uniqueAtoms(atoms);
}

function proteinModificationByPosition(modifications: ProteinModification[] | undefined): Map<number, ProteinModification> {
  const byPosition = new Map<number, ProteinModification>();
  for (const mod of modifications || []) {
    const position = Math.max(1, Math.floor(Number(mod.position || 1)));
    if (Number.isFinite(position) && !byPosition.has(position)) byPosition.set(position, mod);
  }
  return byPosition;
}

function atomOptionsForProteinResidue(residue: string, mod: ProteinModification | undefined): { residueName: string; atoms: string[] } {
  if (!mod) {
    return { residueName: residue, atoms: NATURAL_ATOMS_BY_ONE_LETTER[residue] || DEFAULT_PROTEIN_BACKBONE_ATOMS };
  }

  const ccd = String(mod.ccd || '').trim().toUpperCase();
  const builtIn = BUILT_IN_MOD_BY_CCD.get(ccd);
  if (mod.inputMethod === 'jsme') {
    return { residueName: ccd || residue, atoms: CUSTOM_RESIDUE_BACKBONE_ATOMS };
  }
  return {
    residueName: ccd || residue,
    atoms: PRESET_MOD_ATOMS_BY_CCD[ccd] || NATURAL_ATOMS_BY_CCD[ccd] || (builtIn ? NATURAL_ATOMS_BY_ONE_LETTER[builtIn.baseResidue] : undefined) || DEFAULT_PROTEIN_BACKBONE_ATOMS
  };
}

export function buildComponentAtomOptionsByChain(components: InputComponent[]): StructureAtomOptionsByChain {
  const activeComponents = components.filter((item) => cleanSequence(item.sequence));
  const chainInfos = buildChainInfos(activeComponents);
  const componentById = new Map(activeComponents.map((item) => [item.id, item] as const));
  const result: StructureAtomOptionsByChain = {};

  for (const chain of chainInfos) {
    const component = componentById.get(chain.componentId);
    if (!component) continue;
    const sequence = cleanSequence(component.sequence);
    if (!sequence) continue;

    if (chain.type === 'ligand') {
      const inputMethod = component.inputMethod || 'smiles';
      const atoms = inputMethod === 'ccd' ? [] : ligandAtomNamesFromSmilesByElementOrder(component.sequence);
      result[chain.id] = [
        {
          chainId: chain.id,
          residue: 1,
          residueName: inputMethod === 'ccd' ? sequence : 'LIG',
          atoms
        }
      ];
      continue;
    }

    if (chain.type !== 'protein') {
      result[chain.id] = sequence.split('').map((residueName, index) => ({
        chainId: chain.id,
        residue: index + 1,
        residueName,
        atoms: []
      }));
      continue;
    }

    const modifications = proteinModificationByPosition(component.modifications);
    result[chain.id] = sequence.split('').map((residueName, index): StructureResidueAtomOption => {
      const position = index + 1;
      const options = atomOptionsForProteinResidue(residueName, modifications.get(position));
      return {
        chainId: chain.id,
        residue: position,
        residueName: options.residueName,
        atoms: uniqueAtoms(options.atoms)
      };
    });
  }

  return result;
}
