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

function buildPerAtomConfidence(
  values: number[] | null | undefined,
  atomCount: number,
  confidenceHint: number | null | undefined
): number[] {
  if (atomCount <= 0) return [];
  const normalized = values
    ?.filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
    .map((value) => normalizePlddt(value));
  if (normalized && normalized.length > 0) {
    if (normalized.length === atomCount) return normalized;
    if (normalized.length > atomCount) return normalized.slice(0, atomCount);
    const avg = normalized.reduce((sum, value) => sum + value, 0) / normalized.length;
    return [...normalized, ...Array.from({ length: atomCount - normalized.length }, () => avg)];
  }
  if (typeof confidenceHint === 'number' && Number.isFinite(confidenceHint)) {
    const hinted = normalizePlddt(confidenceHint);
    return Array.from({ length: atomCount }, () => hinted);
  }
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

  const mol = rdkit.get_mol(value);
  if (!mol) {
    throw new Error('Invalid SMILES for RDKit.');
  }

  try {
    mol.set_new_coords?.();
    mol.normalize_depiction?.();
    let rawSvg = '';
    const atomCount = typeof mol.get_num_atoms === 'function' ? Math.max(0, Math.floor(mol.get_num_atoms())) : 0;
    const perAtomConfidence = buildPerAtomConfidence(atomConfidences, atomCount, confidenceHint);
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
  }
}
