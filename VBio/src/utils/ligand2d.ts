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
  primaryMol: ReturnType<RDKitModule['get_mol']>,
  options?: {
    allowAlternateMol?: boolean;
  }
): { renderMol: ReturnType<RDKitModule['get_mol']>; atomCount: number; perAtomConfidence: number[] } {
  const allowAlternateMol = options?.allowAlternateMol !== false;
  const atomCount = readAtomCount(primaryMol || {});
  const exactFromPrimary = buildPerAtomConfidence(values, atomCount, confidenceHint);
  if (exactFromPrimary.length > 0 || !primaryMol) {
    return { renderMol: primaryMol, atomCount, perAtomConfidence: exactFromPrimary };
  }

  const normalized = normalizeConfidenceValues(values);
  if (normalized.length === 0) {
    return { renderMol: primaryMol, atomCount, perAtomConfidence: [] };
  }
  if (!allowAlternateMol) {
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

function normalizeHighlightAtomIndices(atomIndices: number[]): number[] {
  const normalized = Array.from(
    new Set(
      atomIndices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
    )
  );
  return normalized;
}

function injectAtomRingOverlay(svg: string, atomIndices: number[]): string {
  // Keep highlighted atoms clean (no extra gray stroke ring).
  const normalized = normalizeHighlightAtomIndices(atomIndices);
  if (normalized.length === 0) return svg;
  return svg;
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

function enforceSvgCanvasFit(svg: string): string {
  const svgTagStart = svg.indexOf('<svg');
  if (svgTagStart < 0) return svg;
  const svgTagEnd = svg.indexOf('>', svgTagStart);
  if (svgTagEnd < 0) return svg;
  const openTag = svg.slice(svgTagStart, svgTagEnd + 1);
  const hasPreserve = /preserveAspectRatio\s*=/.test(openTag);
  const nextTag = hasPreserve
    ? openTag.replace(/preserveAspectRatio\s*=\s*['"][^'"]*['"]/, 'preserveAspectRatio="xMidYMid meet"')
    : openTag.replace('<svg', '<svg preserveAspectRatio="xMidYMid meet"');
  return `${svg.slice(0, svgTagStart)}${nextTag}${svg.slice(svgTagEnd + 1)}`;
}

export interface Ligand2DRenderOptions {
  smiles: string;
  width: number;
  height: number;
  atomConfidences?: number[] | null;
  confidenceHint?: number | null;
  highlightQuery?: string | null;
  highlightAtomIndices?: number[] | null;
  templateSmiles?: string | null;
  strictTemplateAlignment?: boolean;
}

function tryAlignDepictionToTemplate(
  rdkit: RDKitModule,
  mol: ReturnType<RDKitModule['get_mol']>,
  templateSmiles: string | null | undefined
): boolean {
  const template = String(templateSmiles || '').trim();
  if (!template || !mol) return false;
  const templateMol = rdkit.get_mol(template);
  if (!templateMol) return false;
  let aligned = false;
  try {
    templateMol.set_new_coords?.();
    const anyMol = mol as unknown as Record<string, unknown>;
    const candidate = [
      'generate_aligned_coords',
      'generateDepictionMatching2DStructure',
      'generate_depiction_matching2d_structure',
      'generate_depiction_matching2D_structure',
      'generate_depiction_matching2DStructure',
    ]
      .map((name) => anyMol[name])
      .find((fn) => typeof fn === 'function') as ((...args: unknown[]) => unknown) | undefined;
    if (!candidate) return false;
    try {
      candidate.call(mol, templateMol);
      aligned = true;
      return aligned;
    } catch {
      // try alternative signatures below
    }
    try {
      candidate.call(mol, templateMol, JSON.stringify({ acceptFailure: false, allowRGroups: false }));
      aligned = true;
      return aligned;
    } catch {
      // try alternative signatures below
    }
    try {
      candidate.call(mol, template);
      aligned = true;
      return aligned;
    } catch {
      // try alternative signatures below
    }
    try {
      candidate.call(mol, template, JSON.stringify({ acceptFailure: false, allowRGroups: false }));
      aligned = true;
    } catch {
      // no-op
    }
  } finally {
    templateMol.delete();
  }
  return aligned;
}

function parseSubstructureMatchAtoms(value: unknown): number[] {
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text || text === '-1' || text === '[]' || text === 'null' || text === '{}') return [];
    try {
      return parseSubstructureMatchAtoms(JSON.parse(text));
    } catch {
      return [];
    }
  }
  if (Array.isArray(value)) {
    const flat: number[] = [];
    value.forEach((item) => {
      if (typeof item === 'number' && Number.isFinite(item) && item >= 0) {
        flat.push(Math.floor(item));
        return;
      }
      if (Array.isArray(item)) {
        item.forEach((sub) => {
          if (typeof sub === 'number' && Number.isFinite(sub) && sub >= 0) {
            flat.push(Math.floor(sub));
          }
        });
      }
    });
    return Array.from(new Set(flat));
  }
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    if (Array.isArray(obj.atoms)) {
      return parseSubstructureMatchAtoms(obj.atoms);
    }
    return [];
  }
  return [];
}

export function renderLigand2DSvg(
  rdkit: RDKitModule,
  {
    smiles,
    width,
    height,
    atomConfidences = null,
    confidenceHint = null,
    highlightQuery = null,
    highlightAtomIndices = null,
    templateSmiles = null,
    strictTemplateAlignment = false
  }: Ligand2DRenderOptions
): string {
  const value = smiles.trim();
  if (!value) {
    throw new Error('SMILES is empty.');
  }

  const sourceMol = rdkit.get_mol(value);
  if (!sourceMol) {
    throw new Error('Invalid SMILES for RDKit.');
  }
  const explicitHighlightAtoms = Array.isArray(highlightAtomIndices)
    ? Array.from(
        new Set(
          highlightAtomIndices
            .map((value) => Number(value))
            .filter((value) => Number.isFinite(value) && value >= 0)
            .map((value) => Math.floor(value))
        )
      )
    : [];
  const resolved = buildPerAtomConfidenceWithMol(rdkit, atomConfidences, confidenceHint, sourceMol, {
    // Keep backend-provided highlight indices on the exact same atom ordering.
    allowAlternateMol: explicitHighlightAtoms.length === 0
  });
  const mol = resolved.renderMol || sourceMol;
  const atomCount = resolved.atomCount;
  const perAtomConfidence = resolved.perAtomConfidence;
  const compactCanvas = Math.min(width, height) <= 140;
  const depictionPadding = compactCanvas ? 0.045 : 0.03;
  const fixedFontSize = compactCanvas
    ? atomCount > 80
      ? 9
      : atomCount > 52
        ? 10
        : 11
    : atomCount > 80
      ? 10
      : atomCount > 52
        ? 11
        : 13;

  try {
    const normalizedTemplate = String(templateSmiles || '').trim();
    const templateRequired = strictTemplateAlignment === true;
    if (templateRequired && !normalizedTemplate) {
      throw new Error('Template alignment requires a reference template SMILES.');
    }
    // Ensure deterministic base orientation before template matching.
    mol.set_new_coords?.();
    const alignedByTemplate = normalizedTemplate
      ? tryAlignDepictionToTemplate(rdkit, mol, normalizedTemplate)
      : false;
    if (normalizedTemplate && templateRequired && !alignedByTemplate) {
      throw new Error('Failed to align 2D depiction to the reference template.');
    }
    if (!templateRequired && !alignedByTemplate) {
      mol.normalize_depiction?.();
    }
    let rawSvg = '';
    if (typeof mol.get_svg_with_highlights === 'function') {
      try {
        const details: Record<string, unknown> = {
          width,
          height,
          padding: depictionPadding,
          clearBackground: false,
          minFontSize: 9,
          fixedFontSize,
          maxFontSize: 36,
          drawOptions: {
            padding: depictionPadding
          }
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

        if (explicitHighlightAtoms.length > 0) {
          if (perAtomConfidence.length === 0) {
            const highlightAtoms = Array.isArray(details.highlightAtoms)
              ? ([...details.highlightAtoms] as number[])
              : [];
            const highlightAtomColors = (details.highlightAtomColors || {}) as Record<number, [number, number, number]>;
            const highlightAtomRadii = (details.highlightAtomRadii || {}) as Record<number, number>;
            explicitHighlightAtoms.forEach((atomIdx) => {
              if (!highlightAtoms.includes(atomIdx)) highlightAtoms.push(atomIdx);
              highlightAtomColors[atomIdx] = [0.95, 0.79, 0.43];
              highlightAtomRadii[atomIdx] = 0.34;
            });
            details.atoms = highlightAtoms;
            details.highlightAtoms = highlightAtoms;
            details.highlightAtomColors = highlightAtomColors;
            details.highlightAtomRadii = highlightAtomRadii;
            details.highlightRadii = highlightAtomRadii;
            details.fillHighlights = false;
            details.atomHighlightsAreCircles = true;
            details.highlightBonds = [];
          }
        } else if (highlightQuery && highlightQuery.trim()) {
          const queryMol =
            (typeof rdkit.get_qmol === 'function' ? rdkit.get_qmol(highlightQuery.trim()) : null) ||
            rdkit.get_mol(highlightQuery.trim());
          if (queryMol) {
            try {
              let matchedAtoms: number[] = [];
              if (typeof mol.get_substruct_match === 'function') {
                matchedAtoms = parseSubstructureMatchAtoms(mol.get_substruct_match(queryMol));
              }
              if (matchedAtoms.length === 0 && typeof mol.get_substruct_matches === 'function') {
                matchedAtoms = parseSubstructureMatchAtoms(mol.get_substruct_matches(queryMol));
              }
              if (matchedAtoms.length > 0) {
                const highlightAtoms = Array.isArray(details.highlightAtoms)
                  ? ([...details.highlightAtoms] as number[])
                  : [];
                const highlightAtomColors = (details.highlightAtomColors || {}) as Record<number, [number, number, number]>;
                const highlightAtomRadii = (details.highlightAtomRadii || {}) as Record<number, number>;
                matchedAtoms.forEach((atomIdx) => {
                  if (!highlightAtoms.includes(atomIdx)) highlightAtoms.push(atomIdx);
                  highlightAtomColors[atomIdx] = [0.94, 0.55, 0.18];
                  highlightAtomRadii[atomIdx] = 0.34;
                });
                details.atoms = highlightAtoms;
                details.highlightAtoms = highlightAtoms;
                details.highlightAtomColors = highlightAtomColors;
                details.highlightAtomRadii = highlightAtomRadii;
                details.highlightRadii = highlightAtomRadii;
                details.fillHighlights = true;
                details.atomHighlightsAreCircles = true;
              }
            } finally {
              queryMol.delete();
            }
          }
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
    return enforceSvgCanvasFit(injectReadabilityStyle(injectAtomRingOverlay(rawSvg, explicitHighlightAtoms)));
  } finally {
    mol.delete();
    if (mol !== sourceMol) {
      sourceMol.delete();
    }
  }
}
