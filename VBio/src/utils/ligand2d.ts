import type { RDKitModule } from './rdkit';

function normalizePlddt(value: number): number {
  if (!Number.isFinite(value)) return 0;
  const normalized = value >= 0 && value <= 1 ? value * 100 : value;
  return Math.max(0, Math.min(100, normalized));
}

function colorForConfidence(value: number): [number, number, number] {
  const v = normalizePlddt(value);
  // AlphaFold confidence colors:
  // very low: #FF7D45, low: #FFDB13, confident: #65CBF3, very high: #0053D6.
  if (v < 50) return [1.0, 0.49, 0.27];
  if (v < 70) return [1.0, 0.86, 0.07];
  if (v < 90) return [0.40, 0.80, 0.95];
  // Slightly lighter than raw AF deep-blue for better 2D readability.
  return [0.16, 0.47, 0.9];
}

function normalizeConfidenceValues(values: number[] | null | undefined): number[] {
  if (!Array.isArray(values)) return [];
  return values
    .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
    .map((value) => normalizePlddt(value));
}

function readAtomCount(mol: { get_num_atoms?: () => number }): number {
  return typeof mol.get_num_atoms === 'function' ? Math.max(0, Math.floor(mol.get_num_atoms())) : 0;
}

function tryReadSmilesVariants(mol: { get_smiles?: (details?: string) => string }): string[] {
  if (typeof mol.get_smiles !== 'function') return [];
  const candidates: string[] = [];
  const seen = new Set<string>();
  const options: Array<string | undefined> = [
    JSON.stringify({ canonical: false, allHsExplicit: false }),
    JSON.stringify({ canonical: false }),
    undefined
  ];

  for (const option of options) {
    try {
      const value = (mol.get_smiles(option) || '').trim();
      if (!value || seen.has(value)) continue;
      seen.add(value);
      candidates.push(value);
    } catch {
      // Skip unsupported option payloads.
    }
  }
  return candidates;
}

function tryBuildExactConfidenceMol(
  rdkit: RDKitModule,
  sourceMol: { get_smiles?: (details?: string) => string },
  expectedAtomCount: number
): { mol: ReturnType<RDKitModule['get_mol']>; atomCount: number } | null {
  if (expectedAtomCount <= 0) return null;
  const smilesCandidates = tryReadSmilesVariants(sourceMol);
  for (const smiles of smilesCandidates) {
    const candidateMol = rdkit.get_mol(smiles);
    if (!candidateMol) continue;
    const count = readAtomCount(candidateMol);
    if (count === expectedAtomCount) {
      return { mol: candidateMol, atomCount: count };
    }
    candidateMol.delete();
  }
  return null;
}

function buildPerAtomConfidenceWithMol(
  rdkit: RDKitModule,
  values: number[] | null | undefined,
  confidenceHint: number | null | undefined,
  primaryMol: ReturnType<RDKitModule['get_mol']>
): { renderMol: ReturnType<RDKitModule['get_mol']>; atomCount: number; perAtomConfidence: number[] } {
  const atomCount = readAtomCount(primaryMol || {});
  const exactFromPrimary = buildPerAtomConfidence(values, atomCount, confidenceHint);
  if (exactFromPrimary.length > 0 || !primaryMol) {
    return { renderMol: primaryMol, atomCount, perAtomConfidence: exactFromPrimary };
  }

  const normalized = normalizeConfidenceValues(values);
  if (normalized.length === 0) {
    return { renderMol: primaryMol, atomCount, perAtomConfidence: [] };
  }

  const exactMol = tryBuildExactConfidenceMol(rdkit, primaryMol, normalized.length);
  if (!exactMol || !exactMol.mol) {
    return { renderMol: primaryMol, atomCount, perAtomConfidence: [] };
  }

  return {
    renderMol: exactMol.mol,
    atomCount: exactMol.atomCount,
    perAtomConfidence: normalized
  };
}

function buildPerAtomConfidence(
  values: number[] | null | undefined,
  atomCount: number,
  _confidenceHint: number | null | undefined
): number[] {
  if (atomCount <= 0) return [];
  const normalized = normalizeConfidenceValues(values);
  if (normalized.length === atomCount) return normalized;
  return [];
}

function injectReadabilityStyle(svg: string): string {
  const svgTagStart = svg.indexOf('<svg');
  if (svgTagStart < 0) return svg;
  const svgTagEnd = svg.indexOf('>', svgTagStart);
  if (svgTagEnd < 0) return svg;
  const style = [
    '<style>',
    'text { paint-order: stroke !important; stroke: rgba(255,255,255,0.92) !important; stroke-width: 1.1px !important; stroke-linejoin: round !important; }',
    '</style>'
  ].join('');
  return `${svg.slice(0, svgTagEnd + 1)}${style}${svg.slice(svgTagEnd + 1)}`;
}

export interface Ligand2DRenderOptions {
  smiles: string;
  width: number;
  height: number;
  atomConfidences?: number[] | null;
  confidenceHint?: number | null;
}

export function renderLigand2DSvg(
  rdkit: RDKitModule,
  { smiles, width, height, atomConfidences = null, confidenceHint = null }: Ligand2DRenderOptions
): string {
  const value = smiles.trim();
  if (!value) {
    throw new Error('SMILES is empty.');
  }

  const sourceMol = rdkit.get_mol(value);
  if (!sourceMol) {
    throw new Error('Invalid SMILES for RDKit.');
  }
  const resolved = buildPerAtomConfidenceWithMol(rdkit, atomConfidences, confidenceHint, sourceMol);
  const mol = resolved.renderMol || sourceMol;
  const atomCount = resolved.atomCount;
  const perAtomConfidence = resolved.perAtomConfidence;

  try {
    mol.set_new_coords?.();
    mol.normalize_depiction?.();
    let rawSvg = '';
    if (typeof mol.get_svg_with_highlights === 'function') {
      try {
        const details: Record<string, unknown> = {
          width,
          height,
          minFontSize: 9,
          fixedFontSize: atomCount > 80 ? 10 : atomCount > 52 ? 11 : 13,
          maxFontSize: 36
        };

        if (perAtomConfidence.length > 0) {
          const highlightAtoms: number[] = [];
          const highlightAtomColors: Record<number, [number, number, number]> = {};
          const highlightAtomRadii: Record<number, number> = {};
          const highlightRadius = atomCount > 90 ? 0.21 : atomCount > 55 ? 0.24 : 0.28;
          for (let i = 0; i < perAtomConfidence.length; i += 1) {
            highlightAtoms.push(i);
            highlightAtomColors[i] = colorForConfidence(perAtomConfidence[i]);
            highlightAtomRadii[i] = highlightRadius;
          }
          details.atoms = highlightAtoms;
          details.highlightAtoms = highlightAtoms;
          details.highlightAtomColors = highlightAtomColors;
          details.highlightAtomRadii = highlightAtomRadii;
          details.highlightRadii = highlightAtomRadii;
          details.fillHighlights = true;
          details.atomHighlightsAreCircles = true;
          details.highlightBonds = [];
        }

        rawSvg = mol.get_svg_with_highlights(JSON.stringify(details));
      } catch {
        rawSvg = '';
      }
    }
    if (!rawSvg) {
      rawSvg = mol.get_svg(width, height);
    }
    if (!rawSvg) {
      throw new Error('RDKit returned empty SVG.');
    }
    return injectReadabilityStyle(rawSvg);
  } finally {
    mol.delete();
    if (mol !== sourceMol) {
      sourceMol.delete();
    }
  }
}
