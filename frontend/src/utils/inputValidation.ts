import type { InputComponent } from '../types/models';
import { normalizeComponentSequence } from './projectInputs';

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
