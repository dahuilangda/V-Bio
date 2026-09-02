import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  detectStructureFormat,
  detectStructureTextFormat,
  fetchValidatedStructure,
  isRcsbEntryId,
  normalizeStructureFileName,
  resolveStructureFormat,
  rcsbCifUrl,
  structureTextHasAtomRecords,
} from './structureParser';

describe('detectStructureFormat', () => {
  it('recognizes pdb, legacy ent, and cif/mmcif names (case-insensitive)', () => {
    expect(detectStructureFormat('9VO8.pdb')).toBe('pdb');
    expect(detectStructureFormat('1UBQ.ent')).toBe('pdb');
    expect(detectStructureFormat('AF-Q9Y631-F1.cif')).toBe('cif');
    expect(detectStructureFormat('model.mmcif')).toBe('cif');
    expect(detectStructureFormat('X.PDB')).toBe('pdb');
  });

  it('returns null for names with no structure extension', () => {
    // The copilot layer hands out ONLY guaranteed-valid links (RCSB records carry cifUrl —
    // mmCIF exists for every entry), so an extension-less or wrong-extension name is a
    // contract error to surface, not something to guess or fall back from.
    expect(detectStructureFormat('9VO8')).toBeNull();
    expect(detectStructureFormat('notes.txt')).toBeNull();
    expect(detectStructureFormat('')).toBeNull();
  });
});

describe('rcsbCifUrl', () => {
  it('builds the mmCIF URL from an entry id, normalizing case and whitespace', () => {
    expect(rcsbCifUrl('8YGY')).toBe('https://files.rcsb.org/download/8YGY.cif');
    expect(rcsbCifUrl(' 8ygy ')).toBe('https://files.rcsb.org/download/8YGY.cif');
  });

  it('identifies well-formed entry ids and rejects other shapes', () => {
    expect(isRcsbEntryId('8YGY')).toBe(true);
    expect(isRcsbEntryId('1UBQ')).toBe(true);
    expect(isRcsbEntryId('KLK')).toBe(false);
    expect(isRcsbEntryId('https://files.rcsb.org/download/8YGY.cif')).toBe(false);
    expect(isRcsbEntryId('')).toBe(false);
  });
});

describe('detectStructureTextFormat', () => {
  it('sniffs PDB from ATOM/HETATM rows and mmCIF from an _atom_site loop', () => {
    expect(detectStructureTextFormat('HEADER TEST\nATOM      1  N   MET A   1      11.104  6.134  6.504  1.00  0.00           N\n')).toBe('pdb');
    expect(detectStructureTextFormat('HETATM 123  C  LIG A 301      0.000  0.000  0.000  1.00  0.00           C\n')).toBe('pdb');
    expect(detectStructureTextFormat('data_test\n_atom_site.group_PDB\nATOM\n')).toBe('cif');
  });

  it('classifies cif FIRST when both markers are present — mmCIF atom rows also start with ATOM', () => {
    const mmCifWithAtomRows = 'data_x\n_atom_site.group_PDB\nATOM 1 N MET A 1 1.0 2.0 3.0\n';
    expect(detectStructureTextFormat(mmCifWithAtomRows)).toBe('cif');
    // and that is exactly why the pdb-format atom-record gate alone cannot catch a mismatch:
    expect(structureTextHasAtomRecords(mmCifWithAtomRows, 'pdb')).toBe(true);
  });

  it('ignores _atom_site mentioned mid-line (e.g. inside a PDB REMARK)', () => {
    expect(detectStructureTextFormat('REMARK 999 generated from _atom_site. loop\nATOM      1  N   MET A   1      0.0 0.0 0.0  1.00  0.00           N\n')).toBe('pdb');
  });

  it('parses PDB files with leading blank lines and REMARKs before the first ATOM', () => {
    expect(detectStructureTextFormat('\n\nHEADER T\nREMARK 1\nATOM      1  N   MET A   1      0.0 0.0 0.0  1.00  0.00           N\n')).toBe('pdb');
  });

  it('returns null for empty or non-structure content', () => {
    expect(detectStructureTextFormat('')).toBeNull();
    expect(detectStructureTextFormat('   \n  ')).toBeNull();
    expect(detectStructureTextFormat('<html><body>404 not found</body></html>')).toBeNull();
  });
});

describe('structureTextHasAtomRecords', () => {
  it('gates downloads per declared format', () => {
    expect(structureTextHasAtomRecords('ATOM      1  N   MET A   1      0.0 0.0 0.0  1.00  0.00           N\n', 'pdb')).toBe(true);
    expect(structureTextHasAtomRecords('data_x\n_atom_site.group_PDB\n', 'cif')).toBe(true);
    expect(structureTextHasAtomRecords('<html>error page</html>', 'cif')).toBe(false);
    expect(structureTextHasAtomRecords('', 'pdb')).toBe(false);
  });
});

describe('resolveStructureFormat', () => {
  it('prefers the declared name and falls back to sniffing the content', () => {
    expect(resolveStructureFormat('1FBJ.cif', '')).toBe('cif');
    expect(resolveStructureFormat('KLK1', 'ATOM      1  N   MET A   1      0.0 0.0 0.0  1.00  0.00           N\n')).toBe('pdb');
    expect(resolveStructureFormat('KLK1', '<html/>')).toBeNull();
  });
});

describe('normalizeStructureFileName', () => {
  const CIF_BODY = 'data_x\n_atom_site.group_PDB\nATOM\n';
  const PDB_BODY = 'ATOM      1  N   MET A   1      0.0 0.0 0.0  1.00  0.00           N\n';

  it('stamps the sniffed format onto extension-less names (legacy persisted drafts self-heal)', () => {
    expect(normalizeStructureFileName('KLK1', CIF_BODY)).toBe('KLK1.cif');
    expect(normalizeStructureFileName('KLK1', PDB_BODY)).toBe('KLK1.pdb');
    expect(normalizeStructureFileName('KLK1.  ', CIF_BODY)).toBe('KLK1.cif');
  });

  it('keeps names that already carry a recognized extension', () => {
    expect(normalizeStructureFileName('2BDG.cif', PDB_BODY)).toBe('2BDG.cif');
    expect(normalizeStructureFileName('x.ent', PDB_BODY)).toBe('x.ent');
  });

  it('returns the name unchanged when the content is not a recognizable structure', () => {
    expect(normalizeStructureFileName('notes', '<html/>')).toBe('notes');
    expect(normalizeStructureFileName('notes', '')).toBe('notes');
  });
});

describe('fetchValidatedStructure', () => {
  const CIF_BODY = 'data_x\n_atom_site.group_PDB\nATOM\n';
  const PDB_BODY = 'ATOM      1  N   MET A   1      0.0 0.0 0.0  1.00  0.00           N\n';

  function stubFetch(body: string, ok = true, status = 200) {
    return vi.fn(async () => ({ ok, status, text: async () => body } as unknown as Response));
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('stamps the detected format onto a planner-supplied extension-less name (root cause of the dead docking chain)', async () => {
    vi.stubGlobal('fetch', stubFetch(CIF_BODY));
    // "KLK1" as fileName + an RCSB .cif URL: the download succeeds, and the STORED name must
    // carry the extension every downstream gate (preview, pocket box, submit) keys off.
    const result = await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', 'KLK1');
    expect(result.format).toBe('cif');
    expect(result.fileName).toBe('KLK1.cif');
    expect(detectStructureFormat(result.fileName)).toBe('cif');
  });

  it('stamps through trailing dots and spaces in the supplied name', async () => {
    vi.stubGlobal('fetch', stubFetch(CIF_BODY));
    expect((await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', 'KLK1.')).fileName).toBe('KLK1.cif');
    expect((await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', 'KLK1  ')).fileName).toBe('KLK1.cif');
  });

  it('maps a .ent URL onto the pdb format when stamping', async () => {
    vi.stubGlobal('fetch', stubFetch(PDB_BODY));
    const result = await fetchValidatedStructure('https://example.com/1FBJ.ent', 'KLK1');
    expect(result.format).toBe('pdb');
    expect(result.fileName).toBe('KLK1.pdb');
  });

  it('lets the CONTENT win over a contradictory name — a .pdb-named mmCIF is restamped (the silent misparse trap)', async () => {
    vi.stubGlobal('fetch', stubFetch(CIF_BODY));
    // mmCIF atom rows satisfy the pdb atom-record gate, so the name-only path would store an
    // mmCIF body as X.pdb and break every column-based parser while Mol* hides the damage.
    const result = await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', '1FBJ.pdb');
    expect(result.format).toBe('cif');
    expect(result.fileName).toBe('1FBJ.cif');
  });

  it('keeps a name that already carries a recognized extension matching the content', async () => {
    vi.stubGlobal('fetch', stubFetch(CIF_BODY));
    const result = await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', '1FBJ.cif');
    expect(result.fileName).toBe('1FBJ.cif');
  });

  it('derives the name (with extension) from the URL when none is passed, ignoring query and fragment', async () => {
    vi.stubGlobal('fetch', stubFetch(CIF_BODY));
    expect((await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', '')).fileName).toBe('1FBJ.cif');
    expect((await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif?download=1', '')).fileName).toBe('1FBJ.cif');
    // extension-less name + query-carrying URL: the URL segment still resolves the format
    expect((await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif?download=1', 'KLK1')).fileName).toBe('KLK1.cif');
  });

  it('strips control characters from planner-supplied names', async () => {
    vi.stubGlobal('fetch', stubFetch(CIF_BODY));
    const result = await fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', 'KL\nK1\t');
    expect(result.fileName).toBe('KLK1.cif');
  });

  it('rejects a source whose format cannot be determined from name or URL', async () => {
    vi.stubGlobal('fetch', stubFetch(CIF_BODY));
    await expect(
      fetchValidatedStructure('https://example.com/structure', 'notes')
    ).rejects.toThrow(/pdbId/);
    await expect(
      fetchValidatedStructure('https://example.com/download/', '')
    ).rejects.toThrow(/pdbId/);
  });

  it('rejects a 200 response that is not a structure (HTML error page or empty body)', async () => {
    vi.stubGlobal('fetch', stubFetch('<html>not a structure</html>'));
    await expect(
      fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', '1FBJ.cif')
    ).rejects.toThrow(/atom records/);
    vi.stubGlobal('fetch', stubFetch('   \n'));
    await expect(
      fetchValidatedStructure('https://files.rcsb.org/download/1FBJ.cif', '1FBJ.cif')
    ).rejects.toThrow(/atom records/);
  });
});
