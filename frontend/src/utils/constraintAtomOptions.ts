import { BUILT_IN_PROTEIN_MODIFICATIONS, NATURAL_AMINO_ACID_RESIDUES } from '../components/project/residueCatalog';
import type { CustomResidueBackbone, InputComponent, ProteinModification } from '../types/models';
import { buildChainInfos } from './chainAssignments';
import { AMINO_ACID_BACKBONE_SMARTS } from './inputValidation';
import type { RDKitModule } from './rdkit';
import type { StructureAtomOptionsByChain, StructureResidueAtomOption } from './structureParser';

const DEFAULT_PROTEIN_BACKBONE_ATOMS = ['N', 'CA', 'C', 'O', 'CB'];
const GLY_ATOMS = ['N', 'CA', 'C', 'O'];

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

// Atom names for a JSME-drawn custom residue, mirroring the backend CCD builder
// (run_single_prediction.py: _set_custom_ccd_atom_properties). Heavy atoms are numbered
// {ELEMENT}{count} in SMILES order (ligandAtomNamesFromSmilesByElementOrder); the alpha
// backbone — matched by AMINO_ACID_BACKBONE_SMARTS, the same pattern the backend
// validates against — is then overridden to N/CA/C/O/OXT. SMILES is the single source
// of truth: names are derived on demand, never persisted, so they cannot drift from the
// backend CCD.
const BACKBONE_ATOM_NAMES = ['N', 'CA', 'C', 'O', 'OXT'] as const;
// Element each backbone slot (in SMARTS query order) must sit on: N, alpha-C, carboxyl-C, carbonyl-O, terminal-O.
const BACKBONE_EXPECTED_ELEMENTS = ['N', 'C', 'C', 'O', 'O'] as const;
// Amidated variant: the 5th backbone atom is the C-terminal amide nitrogen (NXT) instead of the
// carboxyl hydroxyl oxygen. Mirrors the backend amide path in _find_residue_backbone_topology.
const BACKBONE_EXPECTED_ELEMENTS_AMIDATED = ['N', 'C', 'C', 'O', 'N'] as const;

// Parses an RDKit get_substruct_match payload into the ordered atom-index tuple of the
// first match (indices are in SMARTS query order).
function parseBackboneMatch(raw: unknown): number[] | null {
  let value: unknown = raw;
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text || text === '-1' || text === '[]' || text === 'null' || text === '{}') return null;
    try {
      value = JSON.parse(text);
    } catch {
      return null;
    }
  }
  if (value && typeof value === 'object' && !Array.isArray(value)) {
    const obj = value as Record<string, unknown>;
    if (Array.isArray(obj.atoms)) return parseBackboneMatch(obj.atoms);
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    if (typeof value[0] === 'number') return value.map((item) => Math.floor(Number(item)));
    if (Array.isArray(value[0])) return (value[0] as unknown[]).map((item) => Math.floor(Number(item)));
  }
  return null;
}

export function customResidueAtomNamesFromSmiles(
  rdkit: RDKitModule,
  smiles: string,
  backboneOverride?: CustomResidueBackbone,
  amidated: boolean = false
): string[] | null {
  const source = String(smiles || '').trim();
  if (!source) return null;
  const mol = rdkit.get_mol(source);
  if (!mol) return null;
  try {
    const baseNames = ligandAtomNamesFromSmilesByElementOrder(source);
    const numAtoms = typeof mol.get_num_atoms === 'function' ? mol.get_num_atoms() : 0;
    // baseNames (heavy atoms in SMILES order) align 1:1 with RDKit atom indices only when
    // the SMILES has no standalone explicit H; the count guard rejects the misaligned case.
    if (numAtoms <= 0 || numAtoms !== baseNames.length) return null;

    const names = [...baseNames];

    // Manual backbone (user-clicked atoms): stamp N/CA/C/O/OXT at the given indices and skip the
    // SMARTS heuristic, so the picker's atom names match the backend CCD (backbone letters +
    // {ELEMENT}{count} sidechain).
    if (backboneOverride) {
      const slots: Array<[keyof CustomResidueBackbone, string]> = [
        ['n', 'N'],
        ['ca', 'CA'],
        ['c', 'C'],
        ['o', 'O'],
        ['oxt', amidated ? 'NXT' : 'OXT']
      ];
      for (const [slot, label] of slots) {
        const atomIdx = backboneOverride[slot];
        if (!Number.isInteger(atomIdx) || atomIdx < 0 || atomIdx >= names.length) return null;
        names[atomIdx] = label;
      }
      return uniqueAtoms(names);
    }

    const detectSmarts = amidated ? AMINO_ACID_BACKBONE_SMARTS_AMIDATED : AMINO_ACID_BACKBONE_SMARTS;
    const expectedElements = amidated ? BACKBONE_EXPECTED_ELEMENTS_AMIDATED : BACKBONE_EXPECTED_ELEMENTS;
    const backboneNames = amidated ? (['N', 'CA', 'C', 'O', 'NXT'] as const) : BACKBONE_ATOM_NAMES;
    const query =
      (typeof rdkit.get_qmol === 'function' ? rdkit.get_qmol(detectSmarts) : null) ||
      rdkit.get_mol(detectSmarts);
    if (!query) return null;
    try {
      let raw: unknown = null;
      if (typeof mol.get_substruct_match === 'function') {
        try {
          raw = mol.get_substruct_match(query);
        } catch {
          raw = null;
        }
      }
      const backbone = parseBackboneMatch(raw);
      if (!backbone || backbone.length < backboneNames.length) return null;

      for (let slot = 0; slot < backboneNames.length; slot += 1) {
        const atomIdx = backbone[slot];
        if (!Number.isInteger(atomIdx) || atomIdx < 0 || atomIdx >= names.length) return null;
        // Guard against a match returned out of query order: the slot's element prefix
        // (read from its base name) must match the expected backbone element.
        if ((names[atomIdx].match(/^[A-Z]+/)?.[0] || '') !== expectedElements[slot]) return null;
        names[atomIdx] = backboneNames[slot];
      }
      return uniqueAtoms(names);
    } finally {
      try {
        query.delete();
      } catch {
        /* query already disposed */
      }
    }
  } finally {
    try {
      mol.delete();
    } catch {
      /* molecule already disposed */
    }
  }
}

// SMARTS for the 5 backbone atoms, in query order [N, CA, C, =O (carbonyl), O (hydroxyl)].
// Mirrors the backend carboxyl pattern so a correct detection lines up with the backend's pick.
// The α-nitrogen accepts any valence ([NH3+] protonated, [NH2] neutral, ring N …) to mirror the
// backend _find_residue_backbone_topology, which enumerates every N (atomicnum==7) regardless of
// charge. The old [NX3] made JSME-drawn zwitterions ([NH3+]…[O-]) silently un-detectable.
const CUSTOM_BACKBONE_DETECT_SMARTS = '[N;!$(NC=O)]-[C;X4]-[CX3](=[OX1])[OX1H0-,OX2H1]';
const CUSTOM_BACKBONE_DETECT_SMARTS_AMIDATED = '[N;!$(NC=O)]-[C;X4]-[CX3](=[OX1])[NX3H2,NX3H1,NX4H2]';
const AMINO_ACID_BACKBONE_SMARTS_AMIDATED = '[N;!$(NC=O)]-[C;X4]-C(=O)N';
const BACKBONE_SLOT_ORDER = ['n', 'ca', 'c', 'o', 'oxt'] as const;

// RDKit returns the list of matches in a few shapes across builds ({atoms: [[...]]}, a list of
// lists, ...). Normalize to one list of index lists.
function parseAllBackboneMatches(raw: unknown): number[][] {
  let value: unknown = raw;
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text || text === '-1' || text === '[]' || text === 'null' || text === '{}') return [];
    try {
      value = JSON.parse(text);
    } catch {
      return [];
    }
  }
  const normalizeRow = (row: unknown): number[] =>
    Array.isArray(row) ? row.map((v) => Number(v)).filter((v) => Number.isInteger(v) && v >= 0) : [];
  if (Array.isArray(value)) {
    if (value.length > 0 && Array.isArray(value[0])) return value.map(normalizeRow).filter((r) => r.length > 0);
    const single = normalizeRow(value);
    return single.length > 0 ? [single] : [];
  }
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    if (Array.isArray(obj.atoms) && obj.atoms.length > 0 && Array.isArray(obj.atoms[0])) {
      return (obj.atoms as unknown[]).map(normalizeRow).filter((r) => r.length > 0);
    }
    if (Array.isArray(obj.matches) && obj.matches.length > 0 && Array.isArray(obj.matches[0])) {
      return (obj.matches as unknown[]).map(normalizeRow).filter((r) => r.length > 0);
    }
  }
  return [];
}

// Detect the backbone atoms for a custom residue. When `anchors` is supplied (atoms the user
// already picked), the detection treats those as fixed: among all matches it prefers the one that
// agrees with the most anchors, then forces the anchored slots to the user's atoms, so only the
// unfilled slots are (re)detected. Returns null when no backbone is found, or when the SMILES has
// explicit H (its atom indices would not line up with the backend).
export function detectCustomResidueBackbone(
  rdkit: RDKitModule,
  smiles: string,
  anchors?: Partial<CustomResidueBackbone>,
  amidated: boolean = false
): CustomResidueBackbone | null {
  const source = String(smiles || '').trim();
  if (!source) return null;
  const mol = rdkit.get_mol(source);
  if (!mol) return null;
  try {
    const numAtoms = typeof mol.get_num_atoms === 'function' ? mol.get_num_atoms() : 0;
    if (numAtoms <= 0 || numAtoms !== ligandAtomNamesFromSmilesByElementOrder(source).length) return null;
    const detectSmarts = amidated ? CUSTOM_BACKBONE_DETECT_SMARTS_AMIDATED : CUSTOM_BACKBONE_DETECT_SMARTS;
    const query =
      (typeof rdkit.get_qmol === 'function' ? rdkit.get_qmol(detectSmarts) : null) ||
      rdkit.get_mol(detectSmarts);
    if (!query) return null;
    try {
      let matches: number[][] = [];
      if (typeof mol.get_substruct_matches === 'function') {
        try {
          matches = parseAllBackboneMatches(mol.get_substruct_matches(query));
        } catch {
          matches = [];
        }
      }
      if (matches.length === 0 && typeof mol.get_substruct_match === 'function') {
        try {
          const single = parseBackboneMatch(mol.get_substruct_match(query));
          if (single) matches = [single];
        } catch {
          matches = [];
        }
      }
      matches = matches.filter((m) => m.length >= 5);
      if (matches.length === 0) return null;

      const anchorEntries = anchors
        ? BACKBONE_SLOT_ORDER.map((slot, pos) => ({ pos, idx: anchors[slot] })).filter((e) => e.idx !== undefined)
        : [];
      let best: number[] | null = null;
      let bestScore = -1;
      for (const match of matches) {
        let score = 0;
        for (const entry of anchorEntries) {
          if (match[entry.pos] === entry.idx) score += 1;
        }
        if (score > bestScore) {
          bestScore = score;
          best = match;
        }
      }
      if (!best) return null;
      const result: CustomResidueBackbone = { n: best[0], ca: best[1], c: best[2], o: best[3], oxt: best[4] };
      // The user's picks take priority: force the anchored slots to their atoms.
      if (anchors) {
        BACKBONE_SLOT_ORDER.forEach((slot) => {
          if (anchors[slot] !== undefined) result[slot] = anchors[slot] as number;
        });
      }
      const indices = [result.n, result.ca, result.c, result.o, result.oxt];
      if (indices.some((idx) => !Number.isInteger(idx) || idx < 0 || idx >= numAtoms)) return null;
      if (new Set(indices).size !== 5) return null;
      return result;
    } finally {
      if (query) {
        try {
          query.delete();
        } catch {
          /* query already disposed */
        }
      }
    }
  } finally {
    try {
      mol.delete();
    } catch {
      /* molecule already disposed */
    }
  }
}

// Parse a V2000 molblock into a per-atom element array (indexed by RDKit atom idx). Used to check
// that a manual backbone pick still lands on the right element after a SMILES edit, since atom
// indices can shift when the structure changes.
function readMolblockElements(mol: { get_molblock?: () => string }): string[] | null {
  const mb = typeof mol.get_molblock === 'function' ? mol.get_molblock() : '';
  if (!mb) return null;
  const lines = mb.split('\n');
  let countsIdx = -1;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].includes('V2000')) {
      countsIdx = i;
      break;
    }
  }
  if (countsIdx < 0) return null;
  const nAtoms = Number.parseInt((lines[countsIdx].trim().split(/\s+/)[0] || ''), 10);
  if (!Number.isFinite(nAtoms) || nAtoms <= 0) return null;
  const elements: string[] = [];
  for (let i = 0; i < nAtoms; i += 1) {
    const line = lines[countsIdx + 1 + i];
    if (!line) return null;
    const symbol = line.trim().split(/\s+/)[3];
    if (!symbol) return null;
    elements.push(symbol.toUpperCase());
  }
  return elements;
}

// Parse a V2000 molblock into a bond list (0-based atom indices; order 1=SINGLE, 2=DOUBLE,
// 3=TRIPLE, 4=AROMATIC). Pairs with readMolblockElements so the frontend can mirror the backend's
// neighbor-set + bond-order checks in _residue_topology_from_backbone_override, which the RDKit JS
// binding does not otherwise expose per-atom.
function readMolblockBonds(mol: { get_molblock?: () => string }): Array<{ a: number; b: number; order: number }> | null {
  const mb = typeof mol.get_molblock === 'function' ? mol.get_molblock() : '';
  if (!mb) return null;
  const lines = mb.split('\n');
  let countsIdx = -1;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].includes('V2000')) {
      countsIdx = i;
      break;
    }
  }
  if (countsIdx < 0) return null;
  const countsTokens = lines[countsIdx].trim().split(/\s+/);
  const nAtoms = Number.parseInt(countsTokens[0] || '', 10);
  const nBonds = Number.parseInt(countsTokens[1] || '', 10);
  if (!Number.isFinite(nAtoms) || nAtoms <= 0 || !Number.isFinite(nBonds) || nBonds < 0) return null;
  const bonds: Array<{ a: number; b: number; order: number }> = [];
  for (let i = 0; i < nBonds; i += 1) {
    const line = lines[countsIdx + 1 + nAtoms + i];
    if (!line) return null;
    const tokens = line.trim().split(/\s+/);
    const a = Number.parseInt(tokens[0] || '', 10) - 1;
    const b = Number.parseInt(tokens[1] || '', 10) - 1;
    const order = Number.parseInt(tokens[2] || '', 10);
    if (!Number.isFinite(a) || !Number.isFinite(b) || !Number.isFinite(order) || a < 0 || b < 0) return null;
    bonds.push({ a, b, order });
  }
  return bonds;
}

// Per-slot backbone errors for a manual override. Empty object = all checks pass. Mirrors the
// backend _residue_topology_from_backbone_override (custom_ccd_builder.py:228-310) check-for-check:
// range, distinctness, per-slot element, C bonded to O+terminal, C=O double (amide: C-NXT single),
// CA bonded to N or C. Collects every independent slot error so the editor can mark each wrong
// pill; submit-time surfacing takes the first error in slot order (n→ca→c→o→oxt). Topology checks
// are skipped when an atom they depend on has the wrong element, so a wrong element is reported
// cleanly (and the first-error order matches the backend) instead of cascading into a bond error.
// A parse failure or a heavy-atom/SMILES-index mismatch yields _parseError rather than passing.
export type BackboneSlotErrors = Partial<Record<keyof CustomResidueBackbone, string>> & { _parseError?: string };

const BACKBONE_PARSE_ERROR = 'Could not parse the backbone structure. Check the SMILES and try again.';

export function firstBackboneSlotError(errors: BackboneSlotErrors): string | null {
  if (errors._parseError) return errors._parseError;
  for (const slot of BACKBONE_SLOT_ORDER) {
    const msg = errors[slot];
    if (msg) return msg;
  }
  return null;
}

export function validateCustomResidueBackbone(
  rdkit: RDKitModule,
  smiles: string,
  backbone: CustomResidueBackbone,
  amidated: boolean
): BackboneSlotErrors {
  const source = String(smiles || '').trim();
  if (!source) return { _parseError: BACKBONE_PARSE_ERROR };
  const mol = rdkit.get_mol(source);
  if (!mol) return { _parseError: BACKBONE_PARSE_ERROR };
  try {
    const numAtoms = typeof mol.get_num_atoms === 'function' ? mol.get_num_atoms() : 0;
    if (numAtoms <= 0) return { _parseError: BACKBONE_PARSE_ERROR };
    // Heavy-atom count must align with the SMILES-order index space the picker uses; a mismatch
    // (e.g. an explicit H) means indices are off and any verdict would be wrong.
    if (numAtoms !== ligandAtomNamesFromSmilesByElementOrder(source).length) {
      return { _parseError: BACKBONE_PARSE_ERROR };
    }
    const elements = readMolblockElements(mol);
    const bonds = readMolblockBonds(mol);
    if (!elements || !bonds) return { _parseError: BACKBONE_PARSE_ERROR };

    const errors: BackboneSlotErrors = {};
    const idx: Record<keyof CustomResidueBackbone, number> = {
      n: backbone.n,
      ca: backbone.ca,
      c: backbone.c,
      o: backbone.o,
      oxt: backbone.oxt
    };
    const labelOf = (slot: keyof CustomResidueBackbone) => (slot === 'oxt' && amidated ? 'NXT' : slot.toUpperCase());

    const inRange = (v: number) => Number.isInteger(v) && v >= 0 && v < numAtoms;
    const elementOf = (v: number) => (inRange(v) ? elements[v] : '');

    // 1) Range (backend _slot_idx, lines 236-245).
    for (const slot of BACKBONE_SLOT_ORDER) {
      if (!inRange(idx[slot])) {
        errors[slot] = `Backbone ${labelOf(slot)} atom index is out of range (0..${numAtoms - 1}).`;
      }
    }

    // 2) Distinctness (backend lines 252-253). Skip slots already flagged out of range.
    for (let i = 0; i < BACKBONE_SLOT_ORDER.length; i += 1) {
      for (let j = i + 1; j < BACKBONE_SLOT_ORDER.length; j += 1) {
        const s1 = BACKBONE_SLOT_ORDER[i];
        const s2 = BACKBONE_SLOT_ORDER[j];
        if (idx[s1] === idx[s2] && !errors[s1] && !errors[s2]) {
          errors[s1] = 'Each backbone slot must point to a different atom.';
        }
      }
    }

    // 3) Per-slot element (backend lines 255-272).
    if (!errors.n && elementOf(idx.n) !== 'N') errors.n = 'Backbone N must be a nitrogen atom.';
    if (!errors.ca && !['C', 'N'].includes(elementOf(idx.ca))) errors.ca = 'Backbone CA must be a carbon or nitrogen atom.';
    if (!errors.c && elementOf(idx.c) !== 'C') errors.c = 'Backbone C must be a carbon atom.';
    if (!errors.o && elementOf(idx.o) !== 'O') errors.o = 'Backbone O must be a carbonyl oxygen.';
    if (!errors.oxt) {
      const want = amidated ? 'N' : 'O';
      if (elementOf(idx.oxt) !== want) {
        errors.oxt = amidated
          ? 'With C-terminal amidation, the 5th backbone slot (NXT) must be a nitrogen atom.'
          : 'Backbone OXT must be an oxygen atom.';
      }
    }

    const neighborsOf = (v: number): Set<number> => {
      const set = new Set<number>();
      for (const bond of bonds) {
        if (bond.a === v) set.add(bond.b);
        else if (bond.b === v) set.add(bond.a);
      }
      return set;
    };
    const bondOrderBetween = (x: number, y: number): number | null => {
      for (const bond of bonds) {
        if ((bond.a === x && bond.b === y) || (bond.a === y && bond.b === x)) return bond.order;
      }
      return null;
    };

    // 4) C bonded to O and the terminal atom (backend lines 277-279).
    if (!errors.c && !errors.o && !errors.oxt) {
      const cNeighbors = neighborsOf(idx.c);
      if (!cNeighbors.has(idx.o) || !cNeighbors.has(idx.oxt)) {
        errors.c = 'Backbone C must bond to both O and the terminal atom (carboxyl/amide).';
      }
    }
    // 5) C=O must be a double bond (backend lines 281-288).
    if (!errors.o && !errors.c && bondOrderBetween(idx.c, idx.o) !== 2) {
      errors.o = 'Backbone O must be a carbonyl oxygen (C=O double bond).';
    }
    // 6) Under amidation, C-NXT must be a single bond (backend lines 290-294).
    if (amidated && !errors.oxt && !errors.c && bondOrderBetween(idx.c, idx.oxt) !== 1) {
      errors.oxt = 'With C-terminal amidation, C-NXT must be a single bond (amide C–N).';
    }
    // 7) CA bonded to N or C (backend lines 298-300).
    if (!errors.ca && !errors.n && !errors.c) {
      const caNeighbors = neighborsOf(idx.ca);
      if (!caNeighbors.has(idx.n) && !caNeighbors.has(idx.c)) {
        errors.ca = 'Backbone CA must bond to N or C.';
      }
    }

    return errors;
  } finally {
    try {
      mol.delete();
    } catch {
      /* molecule already disposed */
    }
  }
}

// Keep only the manual backbone picks whose atom index is still in range AND still points at the
// right element after a SMILES edit. Picks whose atom vanished or whose slot now sits on a
// different element are dropped — so the user can keep editing the structure without their
// selections being wiped or silently re-pointed at the wrong atom. Returns the validated subset.
export function validateBackboneSlots(
  rdkit: RDKitModule,
  smiles: string,
  slots: Partial<CustomResidueBackbone>,
  amidated: boolean
): Partial<CustomResidueBackbone> {
  const source = String(smiles || '').trim();
  if (!source) return {};
  const mol = rdkit.get_mol(source);
  if (!mol) return {};
  try {
    const numAtoms = typeof mol.get_num_atoms === 'function' ? mol.get_num_atoms() : 0;
    if (numAtoms <= 0) return {};
    const elements = readMolblockElements(mol);
    if (!elements) return {};
    const expected: Record<keyof CustomResidueBackbone, string> = {
      n: 'N',
      ca: 'C',
      c: 'C',
      o: 'O',
      oxt: amidated ? 'N' : 'O'
    };
    const result: Partial<CustomResidueBackbone> = {};
    (Object.keys(expected) as (keyof CustomResidueBackbone)[]).forEach((slot) => {
      const idx = slots[slot];
      if (
        idx !== undefined &&
        Number.isInteger(idx) &&
        idx >= 0 &&
        idx < numAtoms &&
        elements[idx] === expected[slot]
      ) {
        result[slot] = idx;
      }
    });
    return result;
  } finally {
    try {
      mol.delete();
    } catch {
      /* molecule already disposed */
    }
  }
}

function proteinModificationByPosition(modifications: ProteinModification[] | undefined): Map<number, ProteinModification> {
  const byPosition = new Map<number, ProteinModification>();
  for (const mod of modifications || []) {
    const position = Math.max(1, Math.floor(Number(mod.position || 1)));
    if (Number.isFinite(position) && !byPosition.has(position)) byPosition.set(position, mod);
  }
  return byPosition;
}

function atomOptionsForProteinResidue(
  residue: string,
  mod: ProteinModification | undefined,
  rdkit: RDKitModule | null
): { residueName: string; atoms: string[] } {
  if (!mod) {
    return { residueName: residue, atoms: NATURAL_ATOMS_BY_ONE_LETTER[residue] || DEFAULT_PROTEIN_BACKBONE_ATOMS };
  }

  const ccd = String(mod.ccd || '').trim().toUpperCase();
  const builtIn = BUILT_IN_MOD_BY_CCD.get(ccd);
  if (mod.inputMethod === 'jsme') {
    // Names come solely from the drawn SMILES via RDKit — no hardcoded fallback. While
    // RDKit loads (cached, already required by the 2D previews here) the residue exposes
    // no atoms rather than wrong ones; BondAtomSelect keeps any saved selection across
    // that transient empty state.
    const smiles = String(mod.smiles || '').trim();
    const atoms = rdkit && smiles ? customResidueAtomNamesFromSmiles(rdkit, smiles, mod.backbone, mod.cTerminalAmidated) : null;
    return { residueName: ccd || residue, atoms: atoms || [] };
  }
  return {
    residueName: ccd || residue,
    atoms: PRESET_MOD_ATOMS_BY_CCD[ccd] || NATURAL_ATOMS_BY_CCD[ccd] || (builtIn ? NATURAL_ATOMS_BY_ONE_LETTER[builtIn.baseResidue] : undefined) || DEFAULT_PROTEIN_BACKBONE_ATOMS
  };
}

export function buildComponentAtomOptionsByChain(components: InputComponent[], rdkit: RDKitModule | null = null): StructureAtomOptionsByChain {
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
      const effectiveCcd = inputMethod === 'ccd' ? sequence : null;
      const atoms = effectiveCcd ? [] : ligandAtomNamesFromSmilesByElementOrder(component.sequence);
      result[chain.id] = [
        {
          chainId: chain.id,
          residue: 1,
          residueName: effectiveCcd || 'LIG',
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
      const options = atomOptionsForProteinResidue(residueName, modifications.get(position), rdkit);
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
