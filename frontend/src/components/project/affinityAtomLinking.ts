import { readObjectPath } from '../../pages/projectDetail/projectMetrics';

export interface ExactLigandAtomLink {
  chainId: string;
  residue: number;
  atomName: string;
  displayAtomIndex: number;
}

export interface ExactLigandAtomLinkMap {
  chainId: string;
  residue: number;
  atoms: ExactLigandAtomLink[];
  displayAtomIndexByAtomName: Map<string, number>;
}

const WATER_COMP_IDS = new Set(['HOH', 'WAT', 'DOD', 'SOL']);
const POLYMER_COMP_IDS = new Set([
  'ACE',
  'NME',
  'NMA',
  'NH2',
  'ALA',
  'ARG',
  'ASN',
  'ASP',
  'CYS',
  'GLN',
  'GLU',
  'GLY',
  'HIS',
  'ILE',
  'LEU',
  'LYS',
  'MET',
  'PHE',
  'PRO',
  'SER',
  'THR',
  'TRP',
  'TYR',
  'VAL',
  'SEC',
  'PYL',
  'ASX',
  'GLX',
  'UNK',
  'A',
  'C',
  'G',
  'U',
  'I',
  'DA',
  'DC',
  'DG',
  'DT',
  'DI',
  'DU'
]);

function normalizeChainId(value: string): string {
  return String(value || '').trim().toUpperCase();
}

function normalizeAtomName(value: string): string {
  return String(value || '').trim();
}

function readMetricCandidate(confidence: Record<string, unknown>, path: string): unknown {
  return path.includes('.') ? readObjectPath(confidence, path) : confidence[path];
}

function readFirstStringMetric(confidence: Record<string, unknown>, candidates: string[]): string {
  for (const candidate of candidates) {
    const value = readMetricCandidate(confidence, candidate);
    if (typeof value !== 'string') continue;
    const trimmed = value.trim();
    if (trimmed) return trimmed;
  }
  return '';
}

function readAtomNameKeysByChain(
  confidence: Record<string, unknown>,
  candidates: string[],
  selectedLigandChainId: string
): string[] {
  const normalizedSelectedChainId = normalizeChainId(selectedLigandChainId);
  if (!normalizedSelectedChainId) return [];

  for (const candidate of candidates) {
    const value = readMetricCandidate(confidence, candidate);
    if (!value || typeof value !== 'object' || Array.isArray(value)) continue;
    const entries = Object.entries(value as Record<string, unknown>);
    const matched = entries.find(([chainId]) => normalizeChainId(chainId) === normalizedSelectedChainId);
    if (!matched || !Array.isArray(matched[1])) continue;
    const keys: string[] = [];
    const seen = new Set<string>();
    for (const item of matched[1]) {
      const atomName = normalizeAtomName(String(item || ''));
      if (!atomName || seen.has(atomName)) continue;
      seen.add(atomName);
      keys.push(atomName);
    }
    if (keys.length > 0) return keys;
  }

  return [];
}

function selectAtomNameKeysForRenderedSmiles(
  confidence: Record<string, unknown> | null,
  renderedSmiles: string,
  selectedLigandChainId: string
): string[] {
  if (!confidence) return [];
  const normalizedRenderedSmiles = String(renderedSmiles || '').trim();
  if (!normalizedRenderedSmiles) return [];

  const displaySmiles = readFirstStringMetric(confidence, [
    'ligand_display_smiles',
    'ligand.display_smiles',
    'ligandDisplaySmiles'
  ]);
  if (displaySmiles && displaySmiles === normalizedRenderedSmiles) {
    const displayKeys = readAtomNameKeysByChain(confidence, [
      'ligand_display_atom_name_keys_by_chain',
      'ligand.display_atom_name_keys_by_chain',
      'ligand_display.atom_name_keys_by_chain'
    ], selectedLigandChainId);
    if (displayKeys.length > 0) return displayKeys;
  }

  const alignedSmiles = readFirstStringMetric(confidence, [
    'ligand_smiles',
    'ligand.smiles',
    'ligandSmiles'
  ]);
  if (alignedSmiles && alignedSmiles === normalizedRenderedSmiles) {
    const alignedKeys = readAtomNameKeysByChain(confidence, [
      'ligand_atom_name_keys_by_chain',
      'ligand.atom_name_keys_by_chain',
      'ligand_confidence.atom_name_keys_by_chain'
    ], selectedLigandChainId);
    if (alignedKeys.length > 0) return alignedKeys;
  }

  return [];
}

function tokenizeCifRow(row: string): string[] {
  const matcher = /'(?:[^']*)'|"(?:[^"]*)"|[^\s]+/g;
  const tokens: string[] = [];
  let match = matcher.exec(row);
  while (match) {
    tokens.push(match[0]);
    match = matcher.exec(row);
  }
  return tokens;
}

function stripCifTokenQuotes(value: string): string {
  if (value.length >= 2) {
    if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
      return value.slice(1, -1);
    }
  }
  return value;
}

function findHeaderIndex(headers: string[], names: string[]): number {
  const lowered = headers.map((header) => header.toLowerCase());
  for (const name of names) {
    const idx = lowered.indexOf(name.toLowerCase());
    if (idx >= 0) return idx;
  }
  return -1;
}

function isHydrogenLikeElement(raw: string): boolean {
  const value = raw.trim().toUpperCase();
  if (!value) return false;
  const head = value.replace(/[^A-Z]/g, '').slice(0, 1);
  return head === 'H' || head === 'D' || head === 'T';
}

function isLikelyLigandCompId(compId: string): boolean {
  const normalized = compId.trim().toUpperCase();
  if (!normalized) return false;
  if (WATER_COMP_IDS.has(normalized)) return false;
  return !POLYMER_COMP_IDS.has(normalized);
}

function isLikelyLigandAtomRow(groupPdb: string, compId: string): boolean {
  if (!isLikelyLigandCompId(compId)) return false;
  const normalizedGroup = groupPdb.trim().toUpperCase();
  if (!normalizedGroup) return true;
  return normalizedGroup === 'HETATM' || normalizedGroup === 'ATOM';
}

interface StructureLigandAtomEntry {
  chainId: string;
  residue: number;
  atomName: string;
}

function parseSingleLigandResidueFromCif(
  structureText: string,
  selectedLigandChainId: string
): StructureLigandAtomEntry[] | null {
  const normalizedSelectedChainId = normalizeChainId(selectedLigandChainId);
  if (!structureText.trim() || !normalizedSelectedChainId) return null;
  const lines = structureText.split(/\r?\n/);

  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    if (!headers.some((header) => header.toLowerCase().startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const groupCol = findHeaderIndex(headers, ['_atom_site.group_pdb']);
    const chainCol = findHeaderIndex(headers, ['_atom_site.auth_asym_id', '_atom_site.label_asym_id']);
    const seqCol = findHeaderIndex(headers, ['_atom_site.auth_seq_id', '_atom_site.label_seq_id']);
    const compCol = findHeaderIndex(headers, ['_atom_site.auth_comp_id', '_atom_site.label_comp_id']);
    const atomIdCol = findHeaderIndex(headers, ['_atom_site.auth_atom_id', '_atom_site.label_atom_id']);
    const typeCol = findHeaderIndex(headers, ['_atom_site.type_symbol']);
    if (chainCol < 0 || seqCol < 0 || compCol < 0 || atomIdCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    const residueKeys = new Set<string>();
    const atomsByResidue = new Map<string, StructureLigandAtomEntry[]>();
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length < headers.length) {
        rowIndex += 1;
        continue;
      }

      const chainId = stripCifTokenQuotes(tokens[chainCol]).trim();
      if (normalizeChainId(chainId) !== normalizedSelectedChainId) {
        rowIndex += 1;
        continue;
      }
      const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
      const groupPdb = groupCol >= 0 ? stripCifTokenQuotes(tokens[groupCol]).trim().toUpperCase() : '';
      const atomName = normalizeAtomName(stripCifTokenQuotes(tokens[atomIdCol]));
      const element = typeCol >= 0 ? stripCifTokenQuotes(tokens[typeCol]).trim() : atomName;
      const residue = Number.parseInt(stripCifTokenQuotes(tokens[seqCol]).trim(), 10);
      if (!Number.isFinite(residue) || !atomName) {
        rowIndex += 1;
        continue;
      }
      if (!isLikelyLigandAtomRow(groupPdb, compId) || isHydrogenLikeElement(element)) {
        rowIndex += 1;
        continue;
      }
      const residueKey = `${normalizeChainId(chainId)}|${residue}|${compId}`;
      residueKeys.add(residueKey);
      if (residueKeys.size > 1) {
        return null;
      }
      const next = atomsByResidue.get(residueKey) || [];
      next.push({ chainId, residue, atomName });
      atomsByResidue.set(residueKey, next);
      rowIndex += 1;
    }

    if (atomsByResidue.size === 1) {
      return Array.from(atomsByResidue.values())[0] || null;
    }
    i = rowIndex - 1;
  }

  return null;
}

function parseSingleLigandResidueFromPdb(
  structureText: string,
  selectedLigandChainId: string
): StructureLigandAtomEntry[] | null {
  const normalizedSelectedChainId = normalizeChainId(selectedLigandChainId);
  if (!structureText.trim() || !normalizedSelectedChainId) return null;
  const lines = structureText.split(/\r?\n/);
  const residueKeys = new Set<string>();
  const atomsByResidue = new Map<string, StructureLigandAtomEntry[]>();

  for (const line of lines) {
    if (!line.startsWith('HETATM') && !line.startsWith('ATOM')) continue;
    const chainId = line.slice(21, 22).trim();
    if (normalizeChainId(chainId) !== normalizedSelectedChainId) continue;
    const compId = line.slice(17, 20).trim().toUpperCase();
    const atomName = normalizeAtomName(line.slice(12, 16));
    const element = (line.length >= 78 ? line.slice(76, 78) : atomName).trim();
    const residue = Number.parseInt(line.slice(22, 26).trim(), 10);
    if (!Number.isFinite(residue) || !atomName) continue;
    if (!isLikelyLigandCompId(compId) || isHydrogenLikeElement(element)) continue;
    const residueKey = `${normalizeChainId(chainId)}|${residue}|${compId}`;
    residueKeys.add(residueKey);
    if (residueKeys.size > 1) {
      return null;
    }
    const next = atomsByResidue.get(residueKey) || [];
    next.push({ chainId, residue, atomName });
    atomsByResidue.set(residueKey, next);
  }

  if (atomsByResidue.size !== 1) return null;
  return Array.from(atomsByResidue.values())[0] || null;
}

function parseSingleLigandResidueFromStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  selectedLigandChainId: string
): StructureLigandAtomEntry[] | null {
  return structureFormat === 'pdb'
    ? parseSingleLigandResidueFromPdb(structureText, selectedLigandChainId)
    : parseSingleLigandResidueFromCif(structureText, selectedLigandChainId);
}

export function resolveExactLigandAtomLinks(params: {
  confidence: Record<string, unknown> | null;
  renderedSmiles: string;
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  selectedLigandChainId: string | null;
}): ExactLigandAtomLinkMap | null {
  const { confidence, renderedSmiles, structureText, structureFormat, selectedLigandChainId } = params;
  const normalizedSelectedChainId = normalizeChainId(String(selectedLigandChainId || ''));
  if (!confidence || !normalizedSelectedChainId || !String(renderedSmiles || '').trim() || !String(structureText || '').trim()) {
    return null;
  }

  const atomNameKeys = selectAtomNameKeysForRenderedSmiles(confidence, renderedSmiles, normalizedSelectedChainId);
  if (atomNameKeys.length === 0) return null;

  const structureAtoms = parseSingleLigandResidueFromStructure(structureText, structureFormat, normalizedSelectedChainId);
  if (!structureAtoms || structureAtoms.length !== atomNameKeys.length) return null;

  const structureAtomByName = new Map<string, StructureLigandAtomEntry>();
  for (const atom of structureAtoms) {
    if (structureAtomByName.has(atom.atomName)) {
      return null;
    }
    structureAtomByName.set(atom.atomName, atom);
  }

  const atoms: ExactLigandAtomLink[] = [];
  const displayAtomIndexByAtomName = new Map<string, number>();
  for (let displayAtomIndex = 0; displayAtomIndex < atomNameKeys.length; displayAtomIndex += 1) {
    const atomName = atomNameKeys[displayAtomIndex];
    const structureAtom = structureAtomByName.get(atomName);
    if (!structureAtom) return null;
    if (displayAtomIndexByAtomName.has(atomName)) return null;
    displayAtomIndexByAtomName.set(atomName, displayAtomIndex);
    atoms.push({
      chainId: structureAtom.chainId,
      residue: structureAtom.residue,
      atomName,
      displayAtomIndex
    });
  }

  const activeAtom = atoms[0];
  if (!activeAtom) return null;

  return {
    chainId: activeAtom.chainId,
    residue: activeAtom.residue,
    atoms,
    displayAtomIndexByAtomName
  };
}
