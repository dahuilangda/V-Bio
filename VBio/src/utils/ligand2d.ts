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
  alignmentQuerySmiles?: string | null;
  alignmentAtomMap?: Array<[number, number]> | null;
  templateSmiles?: string | null;
  templateMolblock?: string | null;
  strictTemplateAlignment?: boolean;
}

function isLikelyMolblockInput(value: string): boolean {
  const text = String(value || '').trim();
  if (!text) return false;
  if (!text.includes('\n')) return false;
  return /\bV2000\b|\bV3000\b/.test(text);
}

function tryAlignDepictionToTemplate(
  rdkit: RDKitModule,
  mol: NonNullable<ReturnType<RDKitModule['get_mol']>>,
  templateInput: string | null | undefined,
  alignmentQueryInput?: string | null,
  preferredAtomMapInput?: Array<[number, number]> | null
): boolean {
  function classifyAlignmentResult(value: unknown): 'success' | 'failure' | 'unknown' {
    if (value === null || value === undefined) return 'unknown';
    if (typeof value === 'boolean') return value ? 'success' : 'failure';
    if (typeof value === 'number') return Number.isFinite(value) && value >= 0 ? 'success' : 'failure';
    if (typeof value === 'string') {
      const token = value.trim();
      if (!token) return 'unknown';
      const lowered = token.toLowerCase();
      if (lowered === '-1' || lowered === 'false' || lowered === 'null' || lowered === 'none' || lowered === 'undefined') {
        return 'failure';
      }
      if (lowered === 'true' || lowered === 'ok' || lowered === 'success') {
        return 'success';
      }
      const atoms = parseSubstructureMatchAtoms(token);
      if (atoms.length > 0) return 'success';
      if (token.startsWith('[') || token.startsWith('{')) return 'failure';
      return 'unknown';
    }
    if (typeof value === 'object') {
      const atoms = parseSubstructureMatchAtoms(value);
      return atoms.length > 0 ? 'success' : 'failure';
    }
    return 'unknown';
  }

  const template = String(templateInput || '').trim();
  void alignmentQueryInput;
  if (!template || !mol) return false;
  const templateMol = rdkit.get_mol(template);
  if (!templateMol) return false;
  try {
    // For SMILES templates we need deterministic 2D coordinates.
    // For supplied molblocks, keep original coordinates as the absolute reference frame.
    if (!isLikelyMolblockInput(template)) {
      templateMol.set_new_coords?.();
    }
    const anyMol = mol as unknown as Record<string, unknown>;
    const alignCoords = anyMol.generate_aligned_coords as ((...args: unknown[]) => unknown) | undefined;
    if (typeof alignCoords !== 'function') return false;

    const normalizedPreferredAtomMap = Array.isArray(preferredAtomMapInput)
      ? preferredAtomMapInput
          .map((item) => {
            if (!Array.isArray(item) || item.length < 2) return null;
            const left = Number(item[0]);
            const right = Number(item[1]);
            if (!Number.isFinite(left) || !Number.isFinite(right)) return null;
            if (left < 0 || right < 0) return null;
            return [Math.floor(left), Math.floor(right)] as [number, number];
          })
          .filter((item): item is [number, number] => Boolean(item))
      : [];
    // Enforce explicit backend-provided atom map to keep orientation deterministic.
    if (normalizedPreferredAtomMap.length === 0) return false;
    const constrainedOptions: Record<string, unknown> = {
      allowRGroups: true,
      acceptFailure: false,
      alignOnly: false
    };
    // RDKit expects atomMap entries as (templateAtomIdx, targetMolAtomIdx).
    constrainedOptions.atomMap = normalizedPreferredAtomMap.map(([molAtomIdx, referenceAtomIdx]) => [
      referenceAtomIdx,
      molAtomIdx
    ]);

    try {
      const result = alignCoords.call(mol, templateMol, JSON.stringify(constrainedOptions));
      const verdict = classifyAlignmentResult(result);
      if (verdict === 'success') return true;
      if (verdict === 'failure') return false;
      // Some rdkit.js builds return undefined for success.
      return true;
    } catch {
      return false;
    }
  } finally {
    templateMol.delete();
  }
  return false;
}

function normalizeSubstructureMatch(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => Number(item))
    .filter((item) => Number.isFinite(item) && item >= 0)
    .map((item) => Math.floor(item));
}

function parseSubstructureMatches(value: unknown): number[][] {
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text || text === '-1' || text === '[]' || text === 'null' || text === '{}') return [];
    try {
      return parseSubstructureMatches(JSON.parse(text));
    } catch {
      return [];
    }
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return [];
    const first = value[0];
    if (typeof first === 'number') {
      const normalized = normalizeSubstructureMatch(value);
      return normalized.length > 0 ? [normalized] : [];
    }
    const rows = value
      .map((item) => normalizeSubstructureMatch(item))
      .filter((item) => item.length > 0);
    return rows;
  }
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    if (Array.isArray(obj.atoms)) {
      return parseSubstructureMatches(obj.atoms);
    }
    if (Array.isArray(obj.matches)) {
      return parseSubstructureMatches(obj.matches);
    }
    return [];
  }
  return [];
}

function parseSubstructureMatchAtoms(value: unknown): number[] {
  const matches = parseSubstructureMatches(value);
  const flat = new Set<number>();
  matches.forEach((match) => {
    match.forEach((idx) => {
      if (Number.isFinite(idx) && idx >= 0) {
        flat.add(Math.floor(idx));
      }
    });
  });
  return Array.from(flat);
}

function compareNumberArrays(a: number[], b: number[]): number {
  const n = Math.min(a.length, b.length);
  for (let i = 0; i < n; i += 1) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}

function normalizeMatchRows(rows: number[][]): number[][] {
  return rows
    .map((row) =>
      row
        .map((item) => Number(item))
        .filter((item) => Number.isFinite(item) && item >= 0)
        .map((item) => Math.floor(item))
    )
    .filter((row) => row.length >= 2)
    .sort(compareNumberArrays);
}

const ALIGNMENT_MAP_CACHE = new Map<string, Array<[number, number]>>();

function buildFrontEndAlignmentAtomMap(
  rdkit: RDKitModule,
  candidateMol: NonNullable<ReturnType<RDKitModule['get_mol']>>,
  candidateSmiles: string,
  templateSmiles: string,
  alignmentQuerySmiles: string
): Array<[number, number]> {
  const candidate = String(candidateSmiles || '').trim();
  const template = String(templateSmiles || '').trim();
  const query = String(alignmentQuerySmiles || '').trim();
  if (!candidate || !template || !query) return [];
  const cacheKey = `${candidate}||${template}||${query}`;
  if (ALIGNMENT_MAP_CACHE.has(cacheKey)) {
    return ALIGNMENT_MAP_CACHE.get(cacheKey) || [];
  }

  const templateMol = rdkit.get_mol(template);
  const queryMol =
    (typeof rdkit.get_qmol === 'function' ? rdkit.get_qmol(query) : null) ||
    rdkit.get_mol(query);
  if (!templateMol || !queryMol) {
    templateMol?.delete();
    queryMol?.delete();
    ALIGNMENT_MAP_CACHE.set(cacheKey, []);
    return [];
  }

  try {
    const candidateMatchesRaw =
      (typeof candidateMol.get_substruct_matches === 'function'
        ? candidateMol.get_substruct_matches(queryMol)
        : typeof candidateMol.get_substruct_match === 'function'
          ? candidateMol.get_substruct_match(queryMol)
          : []) || [];
    const templateMatchesRaw =
      (typeof templateMol.get_substruct_matches === 'function'
        ? templateMol.get_substruct_matches(queryMol)
        : typeof templateMol.get_substruct_match === 'function'
          ? templateMol.get_substruct_match(queryMol)
          : []) || [];

    const candidateMatches = normalizeMatchRows(parseSubstructureMatches(candidateMatchesRaw));
    const templateMatches = normalizeMatchRows(parseSubstructureMatches(templateMatchesRaw));
    if (candidateMatches.length === 0 || templateMatches.length === 0) {
      ALIGNMENT_MAP_CACHE.set(cacheKey, []);
      return [];
    }

    for (const templateMatch of templateMatches) {
      for (const candidateMatch of candidateMatches) {
        if (candidateMatch.length !== templateMatch.length) continue;
        const pairs: Array<[number, number]> = [];
        const usedCandidate = new Set<number>();
        const usedTemplate = new Set<number>();
        for (let i = 0; i < candidateMatch.length; i += 1) {
          const candidateAtomIdx = candidateMatch[i];
          const templateAtomIdx = templateMatch[i];
          if (usedCandidate.has(candidateAtomIdx) || usedTemplate.has(templateAtomIdx)) continue;
          usedCandidate.add(candidateAtomIdx);
          usedTemplate.add(templateAtomIdx);
          pairs.push([candidateAtomIdx, templateAtomIdx]);
        }
        if (pairs.length >= 2) {
          ALIGNMENT_MAP_CACHE.set(cacheKey, pairs);
          return pairs;
        }
      }
    }

    ALIGNMENT_MAP_CACHE.set(cacheKey, []);
    return [];
  } finally {
    queryMol.delete();
    templateMol.delete();
  }
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
    alignmentQuerySmiles = null,
    alignmentAtomMap = null,
    templateSmiles = null,
    templateMolblock = null,
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
    void alignmentAtomMap;
    const normalizedTemplateSmiles = String(templateSmiles || '').trim();
    void templateMolblock;
    const normalizedTemplate = normalizedTemplateSmiles;
    const normalizedAlignmentQuery = String(alignmentQuerySmiles || '').trim();
    let renderMol: NonNullable<ReturnType<RDKitModule['get_mol']>> = mol;
    // Ensure deterministic base orientation before template matching.
    mol.set_new_coords?.();
    if (normalizedTemplate) {
      const frontEndAlignmentAtomMap = buildFrontEndAlignmentAtomMap(
        rdkit,
        mol,
        value,
        normalizedTemplate,
        normalizedAlignmentQuery
      );
      const effectiveAlignmentMap = frontEndAlignmentAtomMap;
      if (strictTemplateAlignment && effectiveAlignmentMap.length < 2) {
        throw new Error('Strict template alignment requires valid front-end atom mapping.');
      }
      const alignedByRdkit = tryAlignDepictionToTemplate(
        rdkit,
        mol,
        normalizedTemplate,
        normalizedAlignmentQuery,
        effectiveAlignmentMap
      );
      if (strictTemplateAlignment && effectiveAlignmentMap.length >= 2 && !alignedByRdkit) {
        throw new Error('Template alignment failed.');
      }
    }
    let rawSvg = '';
    if (typeof renderMol.get_svg_with_highlights === 'function') {
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
              if (typeof renderMol.get_substruct_match === 'function') {
                matchedAtoms = parseSubstructureMatchAtoms(renderMol.get_substruct_match(queryMol));
              }
              if (matchedAtoms.length === 0 && typeof renderMol.get_substruct_matches === 'function') {
                matchedAtoms = parseSubstructureMatchAtoms(renderMol.get_substruct_matches(queryMol));
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

        rawSvg = renderMol.get_svg_with_highlights(JSON.stringify(details));
      } catch {
        rawSvg = '';
      }
    }
    if (!rawSvg) {
      rawSvg = renderMol.get_svg(width, height);
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
