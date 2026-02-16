const AMINO_THREE_TO_ONE: Record<string, string> = {
  ALA: 'A',
  ARG: 'R',
  ASN: 'N',
  ASP: 'D',
  CYS: 'C',
  GLN: 'Q',
  GLU: 'E',
  GLY: 'G',
  HIS: 'H',
  ILE: 'I',
  LEU: 'L',
  LYS: 'K',
  MET: 'M',
  PHE: 'F',
  PRO: 'P',
  SER: 'S',
  THR: 'T',
  TRP: 'W',
  TYR: 'Y',
  VAL: 'V',
  SEC: 'U',
  PYL: 'O',
  MSE: 'M'
};

export function detectStructureFormat(fileName: string): 'pdb' | 'cif' | null {
  const lower = fileName.trim().toLowerCase();
  if (lower.endsWith('.pdb')) return 'pdb';
  if (lower.endsWith('.cif') || lower.endsWith('.mmcif')) return 'cif';
  return null;
}

function cleanToken(token: string): string {
  const trimmed = token.trim();
  if (
    (trimmed.startsWith("'") && trimmed.endsWith("'")) ||
    (trimmed.startsWith('"') && trimmed.endsWith('"'))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function normalizeChainId(value: string): string {
  const v = value.trim();
  if (!v || v === '.' || v === '?') return '_';
  return v;
}

function tokenizeCifRow(row: string): string[] {
  const tokens: string[] = [];
  const matcher = /'(?:[^']*)'|"(?:[^"]*)"|[^\s]+/g;
  let match: RegExpExecArray | null = matcher.exec(row);
  while (match) {
    tokens.push(cleanToken(match[0]));
    match = matcher.exec(row);
  }
  return tokens;
}

function appendResidue(
  chainResidues: Map<string, { seen: Set<string>; residues: string[] }>,
  chainId: string,
  residueKey: string,
  residueName: string
) {
  const oneLetter = AMINO_THREE_TO_ONE[residueName.toUpperCase()];
  if (!oneLetter) return;
  const bucket = chainResidues.get(chainId) || { seen: new Set<string>(), residues: [] };
  if (!bucket.seen.has(residueKey)) {
    bucket.seen.add(residueKey);
    bucket.residues.push(oneLetter);
  }
  chainResidues.set(chainId, bucket);
}

function parsePdbProteinChains(text: string): Record<string, string> {
  const chainResidues = new Map<string, { seen: Set<string>; residues: string[] }>();
  const lines = text.split(/\r?\n/);
  for (const line of lines) {
    if (!line.startsWith('ATOM')) continue;
    const residueName = line.slice(17, 20).trim();
    const chainId = normalizeChainId(line.slice(21, 22));
    const residueSeq = line.slice(22, 26).trim();
    const insertionCode = line.slice(26, 27).trim();
    const residueKey = `${residueSeq}:${insertionCode || '.'}`;
    appendResidue(chainResidues, chainId, residueKey, residueName);
  }
  const result: Record<string, string> = {};
  for (const [chainId, bucket] of chainResidues.entries()) {
    const seq = bucket.residues.join('');
    if (seq) result[chainId] = seq;
  }
  return result;
}

function parseSeqNumber(raw: string): number | null {
  const parsed = Number.parseInt(String(raw || '').trim(), 10);
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

interface ChainResidueIndexBucket {
  seen: Set<string>;
  residueIndexByAuth: Record<number, number>;
  nextIndex: number;
}

function parsePdbProteinResidueIndexMap(text: string): Record<string, Record<number, number>> {
  const chainBuckets = new Map<string, ChainResidueIndexBucket>();
  const lines = text.split(/\r?\n/);

  for (const line of lines) {
    if (!line.startsWith('ATOM')) continue;
    const residueName = line.slice(17, 20).trim();
    const oneLetter = AMINO_THREE_TO_ONE[residueName.toUpperCase()];
    if (!oneLetter) continue;

    const chainId = normalizeChainId(line.slice(21, 22));
    const residueSeqRaw = line.slice(22, 26).trim();
    const insertionCode = line.slice(26, 27).trim();
    const residueKey = `${residueSeqRaw}:${insertionCode || '.'}`;

    const bucket =
      chainBuckets.get(chainId) || {
        seen: new Set<string>(),
        residueIndexByAuth: {},
        nextIndex: 0
      };

    if (bucket.seen.has(residueKey)) {
      chainBuckets.set(chainId, bucket);
      continue;
    }

    bucket.seen.add(residueKey);
    bucket.nextIndex += 1;
    const authSeq = parseSeqNumber(residueSeqRaw);
    if (authSeq !== null && bucket.residueIndexByAuth[authSeq] === undefined) {
      bucket.residueIndexByAuth[authSeq] = bucket.nextIndex;
    }
    chainBuckets.set(chainId, bucket);
  }

  const result: Record<string, Record<number, number>> = {};
  for (const [chainId, bucket] of chainBuckets.entries()) {
    if (bucket.nextIndex > 0) {
      result[chainId] = bucket.residueIndexByAuth;
    }
  }
  return result;
}

function parseCifProteinChains(text: string): Record<string, string> {
  const lines = text.split(/\r?\n/);
  const chainResidues = new Map<string, { seen: Set<string>; residues: string[] }>();

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (line !== 'loop_') continue;

    let j = i + 1;
    const headers: string[] = [];
    while (j < lines.length) {
      const header = lines[j].trim();
      if (!header.startsWith('_')) break;
      headers.push(header);
      j += 1;
    }
    if (!headers.some((h) => h.startsWith('_atom_site.'))) {
      i = j - 1;
      continue;
    }

    const col = (names: string[]) => {
      for (const name of names) {
        const idx = headers.indexOf(name);
        if (idx >= 0) return idx;
      }
      return -1;
    };

    const chainIdx = col(['_atom_site.auth_asym_id', '_atom_site.label_asym_id']);
    const residueNameIdx = col(['_atom_site.label_comp_id', '_atom_site.auth_comp_id']);
    const residueSeqIdx = col(['_atom_site.auth_seq_id', '_atom_site.label_seq_id']);
    const insertionIdx = col(['_atom_site.pdbx_PDB_ins_code']);

    if (chainIdx < 0 || residueNameIdx < 0 || residueSeqIdx < 0) {
      i = j - 1;
      continue;
    }

    while (j < lines.length) {
      const rowRaw = lines[j];
      const row = rowRaw.trim();
      if (!row || row === '#') {
        j += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rowRaw);
      if (tokens.length < headers.length) {
        j += 1;
        continue;
      }

      const chainId = normalizeChainId(tokens[chainIdx] || '');
      const residueName = (tokens[residueNameIdx] || '').toUpperCase();
      const residueSeq = tokens[residueSeqIdx] || '';
      const insertion = insertionIdx >= 0 ? tokens[insertionIdx] || '' : '';
      const residueKey = `${residueSeq}:${insertion && insertion !== '?' && insertion !== '.' ? insertion : '.'}`;
      appendResidue(chainResidues, chainId, residueKey, residueName);
      j += 1;
    }

    i = j - 1;
  }

  const result: Record<string, string> = {};
  for (const [chainId, bucket] of chainResidues.entries()) {
    const seq = bucket.residues.join('');
    if (seq) result[chainId] = seq;
  }
  return result;
}

function parseCifProteinResidueIndexMap(text: string): Record<string, Record<number, number>> {
  const lines = text.split(/\r?\n/);
  const chainBuckets = new Map<string, ChainResidueIndexBucket>();

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (line !== 'loop_') continue;

    let j = i + 1;
    const headers: string[] = [];
    while (j < lines.length) {
      const header = lines[j].trim();
      if (!header.startsWith('_')) break;
      headers.push(header);
      j += 1;
    }
    if (!headers.some((h) => h.startsWith('_atom_site.'))) {
      i = j - 1;
      continue;
    }

    const col = (names: string[]) => {
      for (const name of names) {
        const idx = headers.indexOf(name);
        if (idx >= 0) return idx;
      }
      return -1;
    };

    const chainIdx = col(['_atom_site.auth_asym_id', '_atom_site.label_asym_id']);
    const residueNameIdx = col(['_atom_site.label_comp_id', '_atom_site.auth_comp_id']);
    const authSeqIdx = col(['_atom_site.auth_seq_id']);
    const labelSeqIdx = col(['_atom_site.label_seq_id']);
    const insertionIdx = col(['_atom_site.pdbx_PDB_ins_code']);

    if (chainIdx < 0 || residueNameIdx < 0 || (authSeqIdx < 0 && labelSeqIdx < 0)) {
      i = j - 1;
      continue;
    }

    while (j < lines.length) {
      const rowRaw = lines[j];
      const row = rowRaw.trim();
      if (!row || row === '#') {
        j += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rowRaw);
      if (tokens.length < headers.length) {
        j += 1;
        continue;
      }

      const chainId = normalizeChainId(tokens[chainIdx] || '');
      const residueName = (tokens[residueNameIdx] || '').toUpperCase();
      const oneLetter = AMINO_THREE_TO_ONE[residueName];
      if (!oneLetter) {
        j += 1;
        continue;
      }

      const authSeqRaw = authSeqIdx >= 0 ? tokens[authSeqIdx] || '' : '';
      const labelSeqRaw = labelSeqIdx >= 0 ? tokens[labelSeqIdx] || '' : '';
      const fallbackSeqRaw = authSeqRaw || labelSeqRaw;
      const insertion = insertionIdx >= 0 ? tokens[insertionIdx] || '' : '';
      const residueKey = `${fallbackSeqRaw}:${insertion && insertion !== '?' && insertion !== '.' ? insertion : '.'}`;

      const bucket =
        chainBuckets.get(chainId) || {
          seen: new Set<string>(),
          residueIndexByAuth: {},
          nextIndex: 0
        };

      if (!bucket.seen.has(residueKey)) {
        bucket.seen.add(residueKey);
        bucket.nextIndex += 1;

        const authSeq = parseSeqNumber(authSeqRaw);
        const labelSeq = parseSeqNumber(labelSeqRaw);
        const fallbackSeq = parseSeqNumber(fallbackSeqRaw);
        const mappedIndex = labelSeq ?? bucket.nextIndex;

        if (authSeq !== null && bucket.residueIndexByAuth[authSeq] === undefined) {
          bucket.residueIndexByAuth[authSeq] = mappedIndex;
        }
        if (labelSeq !== null && bucket.residueIndexByAuth[labelSeq] === undefined) {
          bucket.residueIndexByAuth[labelSeq] = labelSeq;
        }
        if (fallbackSeq !== null && bucket.residueIndexByAuth[fallbackSeq] === undefined) {
          bucket.residueIndexByAuth[fallbackSeq] = mappedIndex;
        }
      }

      chainBuckets.set(chainId, bucket);
      j += 1;
    }

    i = j - 1;
  }

  const result: Record<string, Record<number, number>> = {};
  for (const [chainId, bucket] of chainBuckets.entries()) {
    if (bucket.nextIndex > 0) {
      result[chainId] = bucket.residueIndexByAuth;
    }
  }
  return result;
}

function parsePdbChainIds(text: string): string[] {
  const chains = new Set<string>();
  const lines = text.split(/\r?\n/);
  for (const line of lines) {
    if (!line.startsWith('ATOM') && !line.startsWith('HETATM')) continue;
    const chainId = normalizeChainId(line.slice(21, 22));
    if (chainId) chains.add(chainId);
  }
  return Array.from(chains).sort((a, b) => a.localeCompare(b));
}

function parseCifChainIds(text: string): string[] {
  const lines = text.split(/\r?\n/);
  const chains = new Set<string>();

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (line !== 'loop_') continue;

    let j = i + 1;
    const headers: string[] = [];
    while (j < lines.length) {
      const header = lines[j].trim();
      if (!header.startsWith('_')) break;
      headers.push(header);
      j += 1;
    }
    if (!headers.some((h) => h.startsWith('_atom_site.'))) {
      i = j - 1;
      continue;
    }

    const col = (names: string[]) => {
      for (const name of names) {
        const idx = headers.indexOf(name);
        if (idx >= 0) return idx;
      }
      return -1;
    };

    const chainIdx = col(['_atom_site.auth_asym_id', '_atom_site.label_asym_id']);
    if (chainIdx < 0) {
      i = j - 1;
      continue;
    }

    while (j < lines.length) {
      const rowRaw = lines[j];
      const row = rowRaw.trim();
      if (!row || row === '#') {
        j += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rowRaw);
      if (tokens.length < headers.length) {
        j += 1;
        continue;
      }

      const chainId = normalizeChainId(tokens[chainIdx] || '');
      if (chainId) chains.add(chainId);
      j += 1;
    }

    i = j - 1;
  }

  return Array.from(chains).sort((a, b) => a.localeCompare(b));
}

export function extractProteinChainSequences(structureText: string, format: 'pdb' | 'cif'): Record<string, string> {
  return format === 'pdb' ? parsePdbProteinChains(structureText) : parseCifProteinChains(structureText);
}

export function extractProteinChainResidueIndexMap(
  structureText: string,
  format: 'pdb' | 'cif'
): Record<string, Record<number, number>> {
  return format === 'pdb' ? parsePdbProteinResidueIndexMap(structureText) : parseCifProteinResidueIndexMap(structureText);
}

export function extractStructureChainIds(structureText: string, format: 'pdb' | 'cif'): string[] {
  return format === 'pdb' ? parsePdbChainIds(structureText) : parseCifChainIds(structureText);
}
