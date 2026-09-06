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
  // .ent is the legacy PDB extension (accepted by the manual upload input too).
  if (lower.endsWith('.pdb') || lower.endsWith('.ent')) return 'pdb';
  if (lower.endsWith('.cif') || lower.endsWith('.mmcif')) return 'cif';
  return null;
}

// Shared atom-record predicates — one source of truth for both the format sniffer and the
// download validator, so a future tweak can never make the two diverge. The _atom_site check
// is line-anchored because the CIF grammar always starts the tag at column 0; an unanchored
// substring would let a PDB REMARK mentioning "_atom_site." flip a valid PDB to cif.
function hasCifAtomSite(structureText: string): boolean {
  return /^\s*_atom_site\./m.test(structureText);
}

function hasPdbAtomRow(structureText: string): boolean {
  return /^(ATOM|HETATM)/m.test(structureText);
}

// Content sniff for a file whose NAME carries no recognizable extension (e.g. a
// copilot-applied "KLK1"). Precedence matters: mmCIF atom_site rows ALSO start with
// "ATOM", but only mmCIF contains an _atom_site definition — so cif is checked first.
export function detectStructureTextFormat(structureText: string): 'pdb' | 'cif' | null {
  const text = String(structureText || '');
  if (!text.trim()) return null;
  if (hasCifAtomSite(text)) return 'cif';
  if (hasPdbAtomRow(text)) return 'pdb';
  return null;
}

// Guards copilot/harness structure downloads: a fetched URL can return an HTML
// error page (or an empty body) with HTTP 200, which would otherwise be stored
// as a "structure" that silently renders nothing in the viewer.
export function structureTextHasAtomRecords(structureText: string, format: 'pdb' | 'cif'): boolean {
  const text = String(structureText || '');
  if (!text.trim()) return false;
  return format === 'pdb' ? hasPdbAtomRow(text) : hasCifAtomSite(text);
}

// The run-chain policy for gates that hold both the file NAME and its TEXT: the declared
// name wins when it carries a recognized extension; otherwise sniff the content. One bad
// name must never block (or misroute) the preview / pocket-box / submit gates.
export function resolveStructureFormat(fileName: string, structureText: string): 'pdb' | 'cif' | null {
  return detectStructureFormat(fileName) ?? detectStructureTextFormat(structureText);
}

// Stamp the content-detected format onto a name that carries no recognized extension
// ("KLK1" → "KLK1.cif"). The single storage-name policy: every file entering an affinity
// target slot — copilot download, draft restore, manual upload — passes through here, so a
// legacy extension-less PERSISTED draft self-heals on restore instead of re-raising
// "Target file must be .pdb/.ent/.cif/.mmcif" forever. Unsniffable content returns the name
// unchanged (the gates report it honestly).
export function normalizeStructureFileName(fileName: string, structureText: string): string {
  if (detectStructureFormat(fileName)) return fileName;
  const sniffed = detectStructureTextFormat(structureText);
  if (!sniffed) return fileName;
  return `${fileName.replace(/[.\s]+$/, '')}.${sniffed}`;
}

// Download a structure file from a copilot-provided URL and validate that it actually
// contains atom records — an HTML error page or empty body with HTTP 200 would otherwise
// be stored as a "structure" that silently renders nothing.
export async function fetchValidatedStructure(
  structureUrl: string,
  rawFileName: unknown
): Promise<{ fileName: string; format: 'pdb' | 'cif'; contentText: string }> {
  // Planner-controlled names must not carry control characters into File names, receipts,
  // or persisted snapshots.
  let fileName = String(rawFileName || '').trim().replace(/[\r\n\t\x00-\x1f]/g, '');
  const urlSegment = (structureUrl.split('/').pop() || '').split('?')[0].split('#')[0];
  if (!fileName) fileName = urlSegment;
  // The name the planner passes may lack an extension (e.g. "9VO8"); the URL is what is
  // actually fetched, so fall back to detecting the format from it.
  let format = detectStructureFormat(fileName) ?? detectStructureFormat(urlSegment);
  if (!format) throw new Error('The structure URL must point to a .pdb, .ent, .cif, or .mmcif file — pass the entry\'s pdbId instead and the host will build the correct file URL.');
  const response = await fetch(structureUrl);
  if (!response.ok) throw new Error(`Could not download the structure (HTTP ${response.status}).`);
  const contentText = await response.text();
  // The CONTENT wins over the declared name on conflict: a .pdb-named mmCIF passes the pdb
  // atom-record gate (mmCIF rows start with ATOM) yet breaks every column-based parser
  // downstream while Mol* (which tries both formats) hides the damage — the same poisoned
  // run chain the stamping below exists to prevent, so re-resolve format AND restamp.
  const sniffed = detectStructureTextFormat(contentText);
  if (sniffed && sniffed !== format) {
    format = sniffed;
    fileName = `${fileName.replace(/\.(pdb|ent|cif|mmcif)$/i, '')}.${format}`;
  }
  // Every downstream gate (preview parse, pocket-box detection, submit validation) keys the
  // format off the STORED file's name. A planner-supplied name without a recognized extension
  // (e.g. "KLK1") downloads fine and then silently poisons the whole run chain — the exact
  // "target loaded but nothing can run" failure — so stamp the detected format onto the name
  // (shared policy with the draft-restore and upload paths).
  fileName = normalizeStructureFileName(fileName, contentText);
  if (!structureTextHasAtomRecords(contentText, format)) {
    throw new Error('The downloaded file does not contain structural atom records (ATOM/HETATM or _atom_site) — the URL likely returned an error page.');
  }
  return { fileName, format, contentText };
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


export interface StructureResidueAtomOption {
  chainId: string;
  residue: number;
  residueName: string;
  atoms: string[];
}

export type StructureAtomOptionsByChain = Record<string, StructureResidueAtomOption[]>;

function normalizeAtomName(value: string): string {
  return value.replace(/\s+/g, '').trim().toUpperCase();
}

function appendStructureAtomOption(
  buckets: Map<string, Map<number, { chainId: string; residue: number; residueName: string; atoms: Set<string> }>>,
  chainId: string,
  residueRaw: string,
  residueName: string,
  atomName: string
) {
  const residue = parseSeqNumber(residueRaw);
  const atom = normalizeAtomName(atomName);
  if (residue === null || residue <= 0 || !atom) return;
  const chainBucket = buckets.get(chainId) || new Map<number, { chainId: string; residue: number; residueName: string; atoms: Set<string> }>();
  const residueBucket = chainBucket.get(residue) || { chainId, residue, residueName: residueName.trim().toUpperCase(), atoms: new Set<string>() };
  residueBucket.atoms.add(atom);
  chainBucket.set(residue, residueBucket);
  buckets.set(chainId, chainBucket);
}

function parsePdbStructureResidueAtomOptions(text: string): StructureAtomOptionsByChain {
  const buckets = new Map<string, Map<number, { chainId: string; residue: number; residueName: string; atoms: Set<string> }>>();
  for (const line of text.split(/\r?\n/)) {
    if (!line.startsWith('ATOM') && !line.startsWith('HETATM')) continue;
    appendStructureAtomOption(
      buckets,
      normalizeChainId(line.slice(21, 22)),
      line.slice(22, 26).trim(),
      line.slice(17, 20).trim(),
      line.slice(12, 16).trim()
    );
  }
  return serializeStructureAtomOptions(buckets);
}

function parseCifStructureResidueAtomOptions(text: string): StructureAtomOptionsByChain {
  const lines = text.split(/\r?\n/);
  const buckets = new Map<string, Map<number, { chainId: string; residue: number; residueName: string; atoms: Set<string> }>>();

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
    const residueNameIdx = col(['_atom_site.auth_comp_id', '_atom_site.label_comp_id']);
    const authSeqIdx = col(['_atom_site.auth_seq_id']);
    const labelSeqIdx = col(['_atom_site.label_seq_id']);
    const atomIdx = col(['_atom_site.auth_atom_id', '_atom_site.label_atom_id']);

    if (chainIdx < 0 || residueNameIdx < 0 || atomIdx < 0 || (authSeqIdx < 0 && labelSeqIdx < 0)) {
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

      appendStructureAtomOption(
        buckets,
        normalizeChainId(tokens[chainIdx] || ''),
        (authSeqIdx >= 0 ? tokens[authSeqIdx] : tokens[labelSeqIdx]) || '',
        tokens[residueNameIdx] || '',
        tokens[atomIdx] || ''
      );
      j += 1;
    }

    i = j - 1;
  }

  return serializeStructureAtomOptions(buckets);
}

function serializeStructureAtomOptions(
  buckets: Map<string, Map<number, { chainId: string; residue: number; residueName: string; atoms: Set<string> }>>
): StructureAtomOptionsByChain {
  const result: StructureAtomOptionsByChain = {};
  for (const [chainId, residueMap] of buckets.entries()) {
    const rows = Array.from(residueMap.values())
      .sort((a, b) => a.residue - b.residue)
      .map((item) => ({
        chainId: item.chainId,
        residue: item.residue,
        residueName: item.residueName,
        atoms: Array.from(item.atoms).sort((a, b) => a.localeCompare(b))
      }));
    if (rows.length > 0) result[chainId] = rows;
  }
  return result;
}

export function extractStructureResidueAtomOptions(
  structureText: string,
  format: 'pdb' | 'cif'
): StructureAtomOptionsByChain {
  return format === 'pdb' ? parsePdbStructureResidueAtomOptions(structureText) : parseCifStructureResidueAtomOptions(structureText);
}

/**
 * The guaranteed-valid mmCIF download URL for an RCSB entry id. mmCIF is RCSB's master
 * archive format and exists for EVERY entry, so the host builds the URL from the identifier
 * itself — the planner passes a pdbId, never a hand-copied URL (the record's sourceUrl is
 * the human entry page, and .pdb-format files exist only for some entries).
 */
export function rcsbCifUrl(pdbId: string): string {
  const id = pdbId.trim().toUpperCase();
  return `https://files.rcsb.org/download/${encodeURIComponent(id)}.cif`;
}

/** True when the string looks like a 4-character RCSB entry id (e.g. 8YGY, 1UBQ). */
export function isRcsbEntryId(value: string): boolean {
  return /^[0-9A-Za-z]{4}$/.test(value.trim());
}

/** Strip surrounding single/double quotes from a CIF field token. Identical to the
 *  local copies that used to live in cifConfidenceColoring, resultBundleParser and
 *  affinityAtomLinking. */
export function stripCifTokenQuotes(value: string): string {
  if (value.length >= 2) {
    if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
      return value.slice(1, -1);
    }
  }
  return value;
}
