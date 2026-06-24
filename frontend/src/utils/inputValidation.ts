import type { InputComponent } from '../types/models';
import { normalizeComponentSequence } from './projectInputs';


export const AMINO_ACID_BACKBONE_SMARTS = '[NX3;!$(NC=O)]-[C;X4]-C(=O)[O,N]';
export const AMINO_ACID_TERMINAL_BACKBONE_SMARTS = '[NX3,NX4,NX2]-[C;X4]-C(=O)[O,N]';

function hasSubstructureMatchPayload(value: unknown): boolean {
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text || text === '-1' || text === '[]' || text === 'null' || text === '{}') return false;
    try {
      return hasSubstructureMatchPayload(JSON.parse(text));
    } catch {
      return false;
    }
  }
  if (Array.isArray(value)) return value.length > 0 && value.some((item) => hasSubstructureMatchPayload(item) || Number(item) >= 0);
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    if (Array.isArray(obj.atoms)) return obj.atoms.length > 0;
    if (Array.isArray(obj.matches)) return obj.matches.length > 0;
  }
  return false;
}

export function rdkitMolHasAminoAcidBackbone(rdkit: any, smiles: string, allowTerminal = true): boolean {
  const text = String(smiles || '').trim();
  if (!text || !rdkit) return false;
  const mol = rdkit.get_mol(text);
  if (!mol) return false;
  try {
    const patterns = allowTerminal ? [AMINO_ACID_BACKBONE_SMARTS, AMINO_ACID_TERMINAL_BACKBONE_SMARTS] : [AMINO_ACID_BACKBONE_SMARTS];
    for (const pattern of patterns) {
      const query = (typeof rdkit.get_qmol === 'function' ? rdkit.get_qmol(pattern) : null) || rdkit.get_mol(pattern);
      if (!query) continue;
      try {
        const hasMatch = Boolean(
          (typeof mol.get_substruct_match === 'function' && hasSubstructureMatchPayload(mol.get_substruct_match(query))) ||
            (typeof mol.get_substruct_matches === 'function' && hasSubstructureMatchPayload(mol.get_substruct_matches(query)))
        );
        if (hasMatch) return true;
      } finally {
        query.delete();
      }
    }
    return false;
  } finally {
    mol.delete();
  }
}

export function looksLikeAminoAcidBackboneSmiles(smiles: string): boolean {
  const compact = String(smiles || '').replace(/\s+/g, '').toUpperCase();
  if (!compact) return false;
  return /N/.test(compact) && /C\(=O\)(O|\[O-\]|N)/.test(compact);
}

const PRINTABLE_ASCII_REGEX = /^[\x20-\x7E]+$/;
const PROTEIN_REGEX = /^[ACDEFGHIKLMNPQRSTVWY]+$/i;
const DNA_REGEX = /^[ATGC]+$/i;
const RNA_REGEX = /^[AUGC]+$/i;

interface ValidateComponentOptions {
  requireAtLeastOne?: boolean;
}

function normalizedNonEmptyComponents(components: InputComponent[]): InputComponent[] {
  return components
    .map((comp) => ({
      ...comp,
      sequence: normalizeComponentSequence(comp.type, comp.sequence)
    }))
    .filter((comp) => Boolean(comp.sequence));
}

export function validateComponents(
  components: InputComponent[],
  options: ValidateComponentOptions = {}
): string | null {
  const requireAtLeastOne = options.requireAtLeastOne ?? true;
  if (!components.length) {
    return 'Please add at least one component.';
  }

  const activeComponents = normalizedNonEmptyComponents(components);
  if (requireAtLeastOne && activeComponents.length === 0) {
    return 'Please provide at least one non-empty component sequence.';
  }

  for (let i = 0; i < activeComponents.length; i += 1) {
    const comp = activeComponents[i];
    const sequence = comp.sequence;

    if (comp.type === 'protein') {
      if (!PROTEIN_REGEX.test(sequence)) {
        return `Component ${i + 1} (protein): only standard amino acids are allowed (ACDEFGHIKLMNPQRSTVWY).`;
      }
      const modificationPositions = new Set<number>();
      for (const mod of comp.modifications || []) {
        const position = Math.floor(Number(mod.position));
        if (!Number.isFinite(position) || position < 1 || position > sequence.length) {
          return `Component ${i + 1} (protein): modification position must be within the protein sequence.`;
        }
        if (modificationPositions.has(position)) {
          return `Component ${i + 1} (protein): only one modification is allowed per residue position.`;
        }
        modificationPositions.add(position);
        if (!String(mod.ccd || '').trim()) {
          return `Component ${i + 1} (protein): modification at position ${position} needs a CCD code.`;
        }
        if (mod.inputMethod === 'jsme') {
          const smiles = String(mod.smiles || '').trim();
          if (!smiles) {
            return `Component ${i + 1} (protein): custom modification at position ${position} needs a drawn structure.`;
          }
          if (!looksLikeAminoAcidBackboneSmiles(smiles)) {
            return `Component ${i + 1} (protein): custom modification at position ${position} must include an amino-acid backbone.`;
          }
        }
      }
    } else if (comp.type === 'dna') {
      if (!DNA_REGEX.test(sequence)) {
        return `Component ${i + 1} (DNA): only A/T/G/C are allowed.`;
      }
    } else if (comp.type === 'rna') {
      if (!RNA_REGEX.test(sequence)) {
        return `Component ${i + 1} (RNA): only A/U/G/C are allowed.`;
      }
    } else if (comp.type === 'ligand') {
      if (comp.inputMethod !== 'ccd' && !PRINTABLE_ASCII_REGEX.test(sequence)) {
        return `Component ${i + 1} (ligand): SMILES contains invalid characters.`;
      }
    }
  }

  return null;
}
