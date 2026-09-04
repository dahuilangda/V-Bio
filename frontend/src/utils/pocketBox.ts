/**
 * Dock-mode pocket box helpers: derive center/size from picked residues or a
 * reference ligand, and build a wireframe PDB for MolStar overlay display.
 */

export interface PocketBox {
  centerX: number;
  centerY: number;
  centerZ: number;
  sizeX: number;
  sizeY: number;
  sizeZ: number;
}

export interface ResiduePickKey {
  chainId: string;
  residue: number;
}

interface AtomCoord {
  chainId: string;
  residue: number;
  residueName: string;
  atomName: string;
  x: number;
  y: number;
  z: number;
}

function parsePdbAtomCoords(structureText: string): AtomCoord[] {
  const atoms: AtomCoord[] = [];
  for (const line of structureText.split('\n')) {
    if (!line.startsWith('ATOM') && !line.startsWith('HETATM')) continue;
    if (line.length < 54) continue;
    const atomName = line.slice(12, 16).trim();
    const residueRaw = line.slice(22, 26).trim();
    const residue = Number.parseInt(residueRaw, 10);
    if (!Number.isFinite(residue)) continue;
    const x = Number.parseFloat(line.slice(30, 38));
    const y = Number.parseFloat(line.slice(38, 46));
    const z = Number.parseFloat(line.slice(46, 54));
    if (!Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) continue;
    atoms.push({
      chainId: line.slice(21, 22).trim() || '_',
      residue,
      residueName: line.slice(17, 20).trim(),
      atomName,
      x,
      y,
      z
    });
  }
  return atoms;
}

function parseCifAtomCoords(structureText: string): AtomCoord[] {
  const atoms: AtomCoord[] = [];
  const lines = structureText.split('\n');
  let fields: string[] = [];
  let inAtomSite = false;
  const tokenize = (row: string): string[] => row.trim().split(/\s+/);
  for (const line of lines) {
    if (line.startsWith('#')) continue;
    if (line.trimStart().startsWith('_atom_site.')) {
      if (!inAtomSite) {
        inAtomSite = true;
        fields = [];
      }
      fields.push(line.trim().split('.')[1]);
      continue;
    }
    if (inAtomSite) {
      if (line.startsWith('ATOM') || line.startsWith('HETATM')) {
        const parts = tokenize(line);
        if (parts.length < fields.length) continue;
        const get = (name: string): string => {
          const idx = fields.indexOf(name);
          return idx >= 0 ? parts[idx] : '';
        };
        const chainField = get('auth_asym_id') || get('label_asym_id');
        const residueRaw = get('auth_seq_id') || get('label_seq_id');
        const residue = Number.parseInt(residueRaw, 10);
        const x = Number.parseFloat(get('Cartn_x'));
        const y = Number.parseFloat(get('Cartn_y'));
        const z = Number.parseFloat(get('Cartn_z'));
        if (!Number.isFinite(residue) || !Number.isFinite(x) || !Number.isFinite(y) || !Number.isFinite(z)) continue;
        atoms.push({
          chainId: (chainField || '_').trim(),
          residue,
          residueName: get('label_comp_id'),
          atomName: get('label_atom_id'),
          x,
          y,
          z
        });
      } else if (line.trim() === '' || line.trimStart().startsWith('loop_') || line.trimStart().startsWith('_')) {
        if (atoms.length > 0) inAtomSite = false;
      }
    }
  }
  return atoms;
}

export function parseStructureAtomCoords(structureText: string, format: 'pdb' | 'cif'): AtomCoord[] {
  return format === 'pdb' ? parsePdbAtomCoords(structureText) : parseCifAtomCoords(structureText);
}

/** Map of "chain:resnum" -> Cα coordinate (first CA atom per residue). */
export function extractResidueCAPoints(structureText: string, format: 'pdb' | 'cif'): Map<string, AtomCoord> {
  const points = new Map<string, AtomCoord>();
  for (const atom of parseStructureAtomCoords(structureText, format)) {
    if (atom.atomName.toUpperCase() !== 'CA') continue;
    const key = `${atom.chainId}:${atom.residue}`;
    if (!points.has(key)) points.set(key, atom);
  }
  return points;
}

function boundsOf(coords: { x: number; y: number; z: number }[]): PocketBox | null {
  if (coords.length === 0) return null;
  let minX = Infinity;
  let minY = Infinity;
  let minZ = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  let maxZ = -Infinity;
  for (const c of coords) {
    minX = Math.min(minX, c.x);
    minY = Math.min(minY, c.y);
    minZ = Math.min(minZ, c.z);
    maxX = Math.max(maxX, c.x);
    maxY = Math.max(maxY, c.y);
    maxZ = Math.max(maxZ, c.z);
  }
  const pad = 3.0;
  return {
    centerX: (minX + maxX) / 2,
    centerY: (minY + maxY) / 2,
    centerZ: (minZ + maxZ) / 2,
    sizeX: Math.max(maxX - minX + pad * 2, 18),
    sizeY: Math.max(maxY - minY + pad * 2, 18),
    sizeZ: Math.max(maxZ - minZ + pad * 2, 18)
  };
}


export interface DetectedLigand {
  chainId: string;
  resName: string;
  resNum: number;
  coords: Array<[number, number, number]>;
  atomCount: number;
  label: string;
}

const STANDARD_AA = new Set([
  'ALA','ARG','ASN','ASP','CYS','GLN','GLU','GLY','HIS','ILE','LEU','LYS','MET',
  'PHE','PRO','SER','THR','TRP','TYR','VAL','MSE','SEC','PYL'
]);
const STANDARD_NUC = new Set(['A','G','C','U','DA','DG','DC','DT']);
const WATER_IONS = new Set(['HOH','WAT','H2O','NA','CL','MG','CA','K','ZN','FE','MN','CU','SO4','PO4','GOL','EDO','DMS']);

/** Detect non-polymer small molecules in a structure — candidate binding pockets. */
export function detectLigands(
  structureText: string,
  format: 'pdb' | 'cif'
): DetectedLigand[] {
  const atoms = parseStructureAtomCoords(structureText, format);
  const byResidue = new Map<string, DetectedLigand>();
  
  for (const atom of atoms) {
    const resName = atom.residueName.toUpperCase();
    if (STANDARD_AA.has(resName) || STANDARD_NUC.has(resName) || WATER_IONS.has(resName)) {
      continue;
    }
    // Must have ≥6 heavy atoms to be a "small molecule" (filters out single ions)
    const key = `${atom.chainId}:${atom.residueName}:${atom.residue}`;
    let entry = byResidue.get(key);
    if (!entry) {
      entry = {
        chainId: atom.chainId,
        resName: atom.residueName,
        resNum: atom.residue,
        coords: [],
        atomCount: 0,
        label: `${atom.residueName}`
      };
      byResidue.set(key, entry);
    }
    entry.coords.push([atom.x, atom.y, atom.z]);
    entry.atomCount++;
  }
  
  return [...byResidue.values()]
    .filter(l => l.atomCount >= 6)
    .sort((a, b) => b.atomCount - a.atomCount)
    .slice(0, 5);  // top 5 by size
}

/** Compute a pocket box centered on a detected ligand. */
export function computePocketBoxFromLigandAtoms(
  ligand: DetectedLigand
): PocketBox | null {
  return boundsOf(ligand.coords.map(c => ({ x: c[0], y: c[1], z: c[2] })));
}

/** Pocket box from picked residues (Cα centroid + span + padding). */
export function computePocketBoxFromResiduePicks(
  structureText: string,
  format: 'pdb' | 'cif',
  picks: ResiduePickKey[]
): PocketBox | null {
  if (picks.length === 0) return null;
  const caPoints = extractResidueCAPoints(structureText, format);
  const coords = picks
    .map((pick) => caPoints.get(`${pick.chainId}:${pick.residue}`))
    .filter((p): p is AtomCoord => Boolean(p));
  return boundsOf(coords);
}

/** Pocket box from a reference ligand structure (bounding box + padding). */
export function computePocketBoxFromLigandStructure(
  ligandStructureText: string,
  format: 'pdb' | 'cif'
): PocketBox | null {
  return boundsOf(parseStructureAtomCoords(ligandStructureText, format));
}

/** Observed identity of the dock target: file name plus loaded preview text length. */
export interface PocketTargetSignature {
  name: string;
  length: number;
}

/**
 * Whether a pocket defined against the previous target must be invalidated. A
 * structure only counts as seen once its preview text actually loaded
 * (length > 0): restoring a saved task walks through {name, 0} while the 3D
 * preview is in flight, and treating that step as a change would wipe the box
 * persisted with the submission (the box submitted with a run must be remembered).
 */
export function pocketTargetChanged(
  previous: PocketTargetSignature | null,
  next: PocketTargetSignature
): boolean {
  if (!previous) return false;
  const renamed = previous.name !== '' && next.name !== '' && previous.name !== next.name;
  const rebuilt =
    previous.length > 0 &&
    next.length > 0 &&
    (previous.name !== next.name || previous.length !== next.length);
  return renamed || rebuilt;
}

/**
 * Wireframe box as a PDB string: 8 corner pseudo-atoms joined by 12 CONECT
 * edges. Loaded as a MolStar overlay structure, ball-and-stick rendering of
 * the bonds draws the box edges.
 */export function buildPocketBoxPdb(box: PocketBox): string {
  const hx = box.sizeX / 2;
  const hy = box.sizeY / 2;
  const hz = box.sizeZ / 2;
  const corners: Array<[number, number, number]> = [
    [box.centerX - hx, box.centerY - hy, box.centerZ - hz],
    [box.centerX + hx, box.centerY - hy, box.centerZ - hz],
    [box.centerX + hx, box.centerY + hy, box.centerZ - hz],
    [box.centerX - hx, box.centerY + hy, box.centerZ - hz],
    [box.centerX - hx, box.centerY - hy, box.centerZ + hz],
    [box.centerX + hx, box.centerY - hy, box.centerZ + hz],
    [box.centerX + hx, box.centerY + hy, box.centerZ + hz],
    [box.centerX - hx, box.centerY + hy, box.centerZ + hz]
  ];
  const edges: Array<[number, number]> = [
    [1, 2], [2, 3], [3, 4], [4, 1],
    [5, 6], [6, 7], [7, 8], [8, 5],
    [1, 5], [2, 6], [3, 7], [4, 8]
  ];

  // CONECT records: pair each atom with its neighbors.
  // Build a map of atom serial -> list of bonded serials.
  const conectMap = new Map<number, number[]>();
  for (const [a, b] of edges) {
    if (!conectMap.has(a)) conectMap.set(a, []);
    if (!conectMap.has(b)) conectMap.set(b, []);
    conectMap.get(a)!.push(b);
    conectMap.get(b)!.push(a);
  }

  const fmt = (v: number): string => (Number.isFinite(v) ? v.toFixed(3) : '0.000');
  const lines: string[] = [
    'HEADER    POCKET BOX WIREFRAME',
    'COMPND    BOX'
  ];

  // PDB fixed columns (1-based):
  // record 1-6, serial 7-11, name 13-16, resName 18-20, chainID 22,
  // resSeq 23-26, x 31-38, y 39-46, z 47-54
  corners.forEach(([x, y, z], i) => {
    const serial = i + 1;
    const name = `B${String(serial).padStart(3, '0')}`;
    lines.push(
      'HETATM' + String(serial).padStart(5) + ' ' + name.padEnd(4) + ' BOX' + ' X' + '   1' + '   ' + fmt(x).padStart(8) + fmt(y).padStart(8) + fmt(z).padStart(8) + '  1.00  0.00           C'
    );
  });

  // CONECT records — each line: CONECT + serial + bonded serials (max 4 per line)
  for (const [serial, bonded] of [...conectMap.entries()].sort((a, b) => a[0] - b[0])) {
    // Split into chunks of 4 (PDB CONECT limit)
    for (let i = 0; i < bonded.length; i += 4) {
      const chunk = bonded.slice(i, i + 4);
      let conect = `CONECT${String(serial).padStart(5)}`;
      for (const b of chunk) {
        conect += String(b).padStart(5);
      }
      lines.push(conect);
    }
  }

  // Fingerprint for change detection
  lines.push(`REMARK   2 BOX ${box.centerX.toFixed(2)} ${box.centerY.toFixed(2)} ${box.centerZ.toFixed(2)} ${box.sizeX.toFixed(1)} ${box.sizeY.toFixed(1)} ${box.sizeZ.toFixed(1)}`);
  lines.push('END');
  return lines.join('\n');
}

/**
 * Auto pocket strategy for copilot-driven docking: box the co-crystallized ligand's site when
 * the target structure has one (that IS the binding pocket), else the whole protein (large
 * box, blind docking). Sizes are generous on purpose — a too-small box misses the site; a
 * large one only costs search time.
 */
export function computeAutoPocketBox(
  structureText: string,
  format: 'pdb' | 'cif'
): { box: PocketBox; method: 'ligand' | 'manual'; ligandLabel: string | null } | null {
  const ligands = detectLigands(structureText, format);
  if (ligands.length > 0) {
    const box = computePocketBoxFromLigandAtoms(ligands[0]);
    if (box) {
      return {
        box: {
          ...box,
          sizeX: Math.max(box.sizeX, 22),
          sizeY: Math.max(box.sizeY, 22),
          sizeZ: Math.max(box.sizeZ, 22)
        },
        method: 'ligand',
        ligandLabel: ligands[0].label
      };
    }
  }
  const atoms = parseStructureAtomCoords(structureText, format);
  const caBox = boundsOf(atoms);
  if (!caBox) return null;
  return { box: caBox, method: 'manual', ligandLabel: null };
}

function stripPdbBoundaryRecords(text: string): string {
  return text
    .split('\n')
    .filter((line) => {
      const record = line.slice(0, 6).trim().toUpperCase();
      return record !== 'END' && record !== 'ENDMDL' && record !== 'TER' && record !== 'HEADER' && record !== 'COMPND';
    })
    .join('\n')
    .trim();
}

function inferElementFromAtomName(atomName: string): string {
  const letters = String(atomName || '').replace(/[^A-Za-z]/g, '');
  if (!letters) return 'C';
  if (
    letters.length >= 2 &&
    letters[0] === letters[0].toUpperCase() &&
    letters[1] === letters[1].toLowerCase()
  ) {
    return letters.slice(0, 2);
  }
  return letters.slice(0, 1).toUpperCase();
}

/** Rebuild HETATM records (no CONECT — Mol* infers bonds by distance) from a parsed structure. */
function ligandToPdbRecords(structureText: string, format: 'pdb' | 'cif'): string {
  const fmt = (v: number): string => (Number.isFinite(v) ? v.toFixed(3) : '0.000');
  return parseStructureAtomCoords(structureText, format)
    .map((atom, index) => {
      const serial = index + 1;
      const name = String(atom.atomName || 'C').slice(0, 4).padEnd(4);
      const resName = String(atom.residueName || 'LIG').slice(0, 3).padEnd(3);
      const chain = (String(atom.chainId || '').replace(/\s+/g, '') || 'L').slice(0, 1);
      const residue = Number.isFinite(atom.residue) ? Math.floor(atom.residue) : 1;
      const element = inferElementFromAtomName(atom.atomName).padStart(2);
      return (
        'HETATM' + String(serial).padStart(5) + ' ' + name + ' ' + resName + ' ' + chain +
        String(residue).padStart(4) + '    ' +
        fmt(atom.x).padStart(8) + fmt(atom.y).padStart(8) + fmt(atom.z).padStart(8) +
        '  1.00  0.00          ' + element
      );
    })
    .join('\n');
}

/**
 * One MolStar overlay slot must carry BOTH the reference ligand and the pocket
 * box wireframe, so the ligand stays visible once the box is drawn. Merged into
 * a single PDB text (box is always PDB); a cif ligand is rebuilt as HETATM
 * records. Returns '' unchanged inputs guard: empty ligand falls back to the
 * box alone.
 */
export function combineLigandAndBoxOverlay(
  ligandText: string,
  ligandFormat: 'pdb' | 'cif',
  boxPdb: string
): { text: string; format: 'pdb' } {
  const box = stripPdbBoundaryRecords(boxPdb || '');
  if (!String(ligandText || '').trim()) return { text: boxPdb || '', format: 'pdb' };
  const ligand = ligandFormat === 'pdb'
    ? stripPdbBoundaryRecords(ligandText)
    : ligandToPdbRecords(ligandText, 'cif');
  if (!ligand.trim()) return { text: boxPdb || '', format: 'pdb' };
  return { text: `${ligand}\n${box}\nEND`, format: 'pdb' };
}
