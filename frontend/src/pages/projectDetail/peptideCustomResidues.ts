import type { CustomCcdMoleculeInput, CustomResidueBackbone, ProjectInputConfig } from '../../types/models';

export interface NormalizedCustomResidueDefinition {
  ccd: string;
  smiles: string;
  baseResidue?: string;
  label?: string;
  backbone?: CustomResidueBackbone;
  cTerminalAmidated?: boolean;
}

export function normalizeCustomResidueCode(value: unknown): string {
  return String(value || '').replace(/[^A-Za-z0-9_-]/g, '').toUpperCase().slice(0, 12);
}

// Validate a manual backbone override: exactly the 5 slots, each a non-negative integer.
// Returns the cleaned override or null if malformed (callers then omit it → backend auto-detects).
const BACKBONE_SLOTS = ['n', 'ca', 'c', 'o', 'oxt'] as const;
export function normalizeCustomResidueBackbone(value: unknown): CustomResidueBackbone | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const raw = value as Record<string, unknown>;
  const backbone = {} as CustomResidueBackbone;
  for (const slot of BACKBONE_SLOTS) {
    const num = Number(raw[slot]);
    if (!Number.isFinite(num) || num < 0 || Math.floor(num) !== num) return null;
    backbone[slot] = num;
  }
  return backbone;
}

// Copy a drawn SMILES onto any selected custom pool entry that is missing one. This is a
// load-time migration, not a submit-time fallback: once the SMILES is on the pool entry it
// persists with the config and the submit path still reads the pool entry only. Pre-fix
// selections stored `{code, kind}`; their SMILES lives in the residue library (drawn by the
// user) so we backfill from there.
export function enrichPeptideResiduePoolFromLibrary(
  options: ProjectInputConfig['options'],
  library: CustomCcdMoleculeInput[]
): ProjectInputConfig['options'] {
  const pool = options.peptideResiduePool;
  if (!Array.isArray(pool) || pool.length === 0) return options;
  const libraryByCode = new Map<string, CustomCcdMoleculeInput>();
  for (const item of library) {
    const code = normalizeCustomResidueCode(item.ccd);
    if (code && String(item.smiles || '').trim()) libraryByCode.set(code, item);
  }
  if (libraryByCode.size === 0) return options;
  let changed = false;
  const nextPool = pool.map((entry) => {
    if (entry.kind !== 'custom' || String(entry.smiles || '').trim()) return entry;
    const lib = libraryByCode.get(normalizeCustomResidueCode(entry.code));
    if (!lib) return entry;
    changed = true;
    return { ...entry, smiles: lib.smiles, baseResidue: lib.baseResidue, label: lib.label, backbone: lib.backbone, cTerminalAmidated: lib.cTerminalAmidated };
  });
  if (!changed) return options;
  return { ...options, peptideResiduePool: nextPool };
}

// Single source of truth: a selected custom residue's CCD comes ONLY from its own
// peptideResiduePool entry, which carries the SMILES that was drawn for it and is
// persisted with the config. No fallback, no secondary store — if a custom selection
// has no SMILES on its pool entry it yields no definition (and the backend rejects it
// loudly) rather than silently substituting something else.
export function selectedCustomResidueDefinitions(
  options: ProjectInputConfig['options']
): NormalizedCustomResidueDefinition[] {
  const seen = new Set<string>();
  const definitions: NormalizedCustomResidueDefinition[] = [];
  for (const item of options.peptideResiduePool || []) {
    if (item.kind !== 'custom') continue;
    const ccd = normalizeCustomResidueCode(item.code);
    const smiles = String(item.smiles || '').trim();
    if (!ccd || !smiles || seen.has(ccd)) continue;
    seen.add(ccd);
    definitions.push({
      ccd,
      smiles,
      baseResidue: String(item.baseResidue || '').trim().toUpperCase().slice(0, 1) || undefined,
      label: String(item.label || '').trim().slice(0, 80) || undefined,
      backbone: normalizeCustomResidueBackbone(item.backbone) || undefined,
      cTerminalAmidated: Boolean(item.cTerminalAmidated) || undefined
    });
  }
  return definitions;
}
