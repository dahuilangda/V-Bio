/**
 * Peptide-design pocket helpers. The binding pocket can be defined two ways:
 * against an uploaded target structure — author-numbered "A:152" tokens from
 * 3D residue picks, or an "x,y,z" center plus a radius derived from the box
 * size — or, for sequence-only targets, by naming one or more positions on
 * the target sequence ("25,26,27"): the box equivalent when there is no
 * structure to draw one on.
 */
import type { AffinityDockPocket } from '../types/models';

export interface ParsedPocketResidueTokens {
  /** "A:152" style tokens — author numbering of the uploaded structure. */
  chainContacts: Array<{ chain: string; residue: number }>;
  /** Bare numbers — 1-based positions on the target chain sequence. */
  plainPositions: number[];
}

export function parsePocketResidueTokens(value: string | null | undefined): ParsedPocketResidueTokens {
  const parsed: ParsedPocketResidueTokens = { chainContacts: [], plainPositions: [] };
  for (const rawToken of String(value || '').split(',')) {
    const token = rawToken.trim();
    if (!token) continue;
    if (token.includes(':')) {
      const separatorIndex = token.indexOf(':');
      const chain = token.slice(0, separatorIndex).trim();
      const residue = Number.parseInt(token.slice(separatorIndex + 1), 10);
      if (!chain || !Number.isFinite(residue) || residue <= 0) continue;
      parsed.chainContacts.push({ chain, residue });
    } else {
      const position = Number.parseInt(token, 10);
      if (!Number.isFinite(position) || position <= 0) continue;
      parsed.plainPositions.push(position);
    }
  }
  return parsed;
}

const AMINO_ACID_THREE_LETTER: Record<string, string> = {
  A: 'ALA', R: 'ARG', N: 'ASN', D: 'ASP', C: 'CYS', Q: 'GLN', E: 'GLU', G: 'GLY',
  H: 'HIS', I: 'ILE', L: 'LEU', K: 'LYS', M: 'MET', F: 'PHE', P: 'PRO', S: 'SER',
  T: 'THR', W: 'TRP', Y: 'TYR', V: 'VAL', U: 'SEC', O: 'PYL'
};

/** "25 · GLU" — constraint-style residue label for sequence positions. */
export function aminoAcidOptionLabel(position: number, oneLetter: string): string {
  const letter = String(oneLetter || '').toUpperCase();
  const three = AMINO_ACID_THREE_LETTER[letter];
  return three ? `${position} · ${three}` : `${position} · ${letter || '?'}`;
}

/** Sorted, de-duplicated plain positions joined into "25,26,27" form. */
export function formatPlainPocketPositions(positions: Iterable<number>): string {
  const unique = Array.from(new Set(Array.from(positions).filter((p) => Number.isFinite(p) && p > 0)));
  return unique.sort((a, b) => a - b).join(',');
}

export function togglePocketPosition(positions: number[], position: number): number[] {
  return positions.includes(position) ? positions.filter((p) => p !== position) : [...positions, position];
}

/**
 * Pocket radius for the center-based backend path, derived from the interactive
 * box size the same way boltz2score converts a box to a radius: half the
 * longest edge, clamped to the peptidePocketBox range (4-40 A).
 */
export function pocketRadiusFromBox(box: { sizeX: number; sizeY: number; sizeZ: number }): number {
  const longest = Math.max(box.sizeX, box.sizeY, box.sizeZ);
  const radius = Math.round(longest / 2);
  return Math.max(4, Math.min(40, radius));
}

/** "x,y,z" center string (1 decimal) for the peptidePocketCenter option. */
export function pocketCenterString(box: { centerX: number; centerY: number; centerZ: number }): string {
  const round1 = (v: number) => (Number.isFinite(v) ? Math.round(v * 10) / 10 : 0);
  return `${round1(box.centerX)},${round1(box.centerY)},${round1(box.centerZ)}`;
}

/**
 * Author-numbered residue tokens ("A:152,A:153") from raw 3D picks on the
 * target template. The chain prefix is the YAML chain of the target component
 * (the numbering the backend resolves through the template alignment); the
 * residue numbers stay in the template's author numbering exactly as picked.
 */
export function formatPocketResiduePicks(
  targetChainId: string | null | undefined,
  picks: Array<{ chainId: string; residue: number }>
): string {
  const chain = String(targetChainId || '').trim() || 'A';
  const seen = new Set<string>();
  const tokens: string[] = [];
  for (const pick of picks) {
    const residue = Math.floor(Number(pick.residue));
    if (!Number.isFinite(residue) || residue <= 0) continue;
    const token = `${chain}:${residue}`;
    if (seen.has(token)) continue;
    seen.add(token);
    tokens.push(token);
  }
  return tokens.join(',');
}

/** One-line pocket state for the Binding sidebar: "3 residues", "center + radius" or "whole surface". */
export function peptidePocketSummaryLabel(
  pocketCenter: string | null | undefined,
  pocketResidues: string | null | undefined
): string {
  const tokens = parsePocketResidueTokens(pocketResidues);
  const residueCount = tokens.chainContacts.length + tokens.plainPositions.length;
  if (residueCount > 0) return `${residueCount} residue${residueCount === 1 ? '' : 's'}`;
  if (String(pocketCenter || '').trim()) return 'center + radius';
  return 'whole surface';
}

/**
 * Identity of what the pocket is defined against: the uploaded structure
 * (component + file + loaded content) or the target sequence for
 * sequence-only targets.
 */
export function peptidePocketTargetSignature(args: {
  componentId: string | null;
  hasStructure: boolean;
  fileName?: string;
  contentLength?: number;
  sequence?: string;
}): string {
  if (args.hasStructure) {
    return `tpl:${String(args.componentId || '')}:${String(args.fileName || '')}:${Number(args.contentLength || 0)}`;
  }
  return `seq:${String(args.componentId || '')}:${String(args.sequence || '').replace(/\s+/g, '').toUpperCase()}`;
}

/**
 * Whether a pocket defined against the previous signature must be
 * invalidated. The same component gaining its uploaded structure (restored
 * tasks hydrating template uploads) is NOT a change — plain positions stay
 * valid because the backend translates sequence → author through the
 * template map (the box submitted with a run must be remembered). Everything else — target component
 * switch, structure rebuild/removal, sequence edit — invalidates.
 */
export function peptidePocketTargetChanged(previous: string, next: string): boolean {
  if (!previous || previous === next) return false;
  const kindOf = (sig: string) => (sig.startsWith('tpl:') ? 'tpl' : 'seq');
  const componentOf = (sig: string) => sig.slice(sig.indexOf(':') + 1).split(':')[0];
  if (kindOf(previous) === 'seq' && kindOf(next) === 'tpl' && componentOf(previous) === componentOf(next)) {
    return false;
  }
  return true;
}

/**
 * Update the pocket option + the derived submission fields from the
 * interactive box. One source of truth: a residues-method box (with picks)
 * submits the residue list; anything else submits center + radius.
 */
/**
 * Pocket picks made on an uploaded structure (chain-prefixed residues or an
 * explicit center) resolve against that structure's numbering and frame.
 * When a snapshot is restored without the structure's content they cannot
 * be interpreted and are dropped rather than carried into a sequence-only
 * submission; plain sequence positions stay valid either way.
 */
export function pocketOptionsWithRestoredTemplate<T extends {
  peptidePocketResidues?: string | null;
  peptidePocketCenter?: string | null;
}>(options: T, hasTargetTemplate: boolean): T {
  if (hasTargetTemplate) return options;
  const { chainContacts, plainPositions } = parsePocketResidueTokens(
    options.peptidePocketResidues);
  const center = String(options.peptidePocketCenter || '').trim();
  if (chainContacts.length === 0 && !center) return options;
  return {
    ...options,
    peptidePocketResidues: plainPositions.length > 0
      ? formatPlainPocketPositions(plainPositions)
      : null,
    peptidePocketCenter: null
  };
}

export function pocketSubmissionFieldsFromBox(
  targetChainId: string | null | undefined,
  pocket: AffinityDockPocket | null,
  picks: Array<{ chainId: string; residue: number }>
): {
  peptidePocketCenter: string | null;
  peptidePocketResidues: string | null;
  peptidePocketBox: number | null;
} {
  if (!pocket) return { peptidePocketCenter: null, peptidePocketResidues: null, peptidePocketBox: null };
  if (pocket.method === 'residues' && picks.length > 0) {
    return {
      peptidePocketCenter: null,
      peptidePocketResidues: formatPocketResiduePicks(targetChainId, picks),
      peptidePocketBox: null
    };
  }
  return {
    peptidePocketCenter: pocketCenterString(pocket),
    peptidePocketResidues: null,
    peptidePocketBox: pocketRadiusFromBox(pocket)
  };
}
