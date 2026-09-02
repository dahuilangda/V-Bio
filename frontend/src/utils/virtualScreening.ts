import yaml from 'js-yaml';
import type { InputComponent } from '../types/models';
import { assignChainIdsForComponents } from './chainAssignments';
import { loadRDKitModule } from './rdkit';

export interface VirtualScreeningCompound {
  id: string;
  name: string;
  smiles: string;
  sourceIndex: number;
  canonicalSmiles?: string;
}

export interface VirtualScreeningParseResult {
  compounds: VirtualScreeningCompound[];
  warnings: string[];
  errors: string[];
  format: 'fasta' | 'smi' | 'csv' | 'tsv' | 'lines' | 'empty';
}

export interface VirtualScreeningValidationResult {
  compounds: VirtualScreeningCompound[];
  invalid: Array<{ index: number; message: string }>;
  warnings: string[];
}

function slugToken(value: string, fallback: string): string {
  const token = value
    .normalize('NFKD')
    .replace(/[\u0300-\u036f]/g, '')
    .replace(/[^a-zA-Z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .toLowerCase()
    .slice(0, 56);
  return token || fallback;
}

function uniqueId(base: string, used: Set<string>): string {
  let candidate = base;
  let suffix = 2;
  while (used.has(candidate)) {
    candidate = base.slice(0, Math.max(1, 60 - String(suffix).length - 1)) + '-' + suffix;
    suffix += 1;
  }
  used.add(candidate);
  return candidate;
}

function addCompound(
  compounds: VirtualScreeningCompound[],
  usedIds: Set<string>,
  smiles: string,
  name: string,
  sourceIndex: number,
  errors: string[]
): void {
  const normalizedSmiles = smiles.trim();
  if (!normalizedSmiles) return;
  if (normalizedSmiles.length > 4096) {
    errors.push('Compound ' + sourceIndex + ': SMILES is longer than 4096 characters.');
    return;
  }
  const displayName = name.trim().slice(0, 160) || 'Compound ' + sourceIndex;
  const id = uniqueId(slugToken(displayName, 'compound-' + String(sourceIndex).padStart(3, '0')), usedIds);
  compounds.push({ id, name: displayName, smiles: normalizedSmiles, sourceIndex });
}

function splitDelimitedLine(line: string, delimiter: ',' | '\t'): string[] | null {
  const fields: string[] = [];
  let current = '';
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const char = line[index];
    if (char === '"') {
      if (quoted && line[index + 1] === '"') {
        current += '"';
        index += 1;
      } else {
        quoted = !quoted;
      }
      continue;
    }
    if (char === delimiter && !quoted) {
      fields.push(current.trim());
      current = '';
      continue;
    }
    current += char;
  }
  if (quoted) return null;
  fields.push(current.trim());
  return fields;
}

function normalizedColumnName(value: string): string {
  return value.trim().toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '');
}

function findColumnIndex(columns: string[], aliases: string[]): number {
  return columns.findIndex((column) => aliases.includes(normalizedColumnName(column)));
}

function parseDelimitedCompoundInput(params: {
  source: string;
  delimiter: ',' | '\t';
  format: 'csv' | 'tsv';
}): VirtualScreeningParseResult | null {
  const rows: Array<{ fields: string[]; sourceIndex: number }> = [];
  const errors: string[] = [];
  params.source.split('\n').forEach((rawLine, index) => {
    const line = rawLine.trim();
    if (!line || line.startsWith('#')) return;
    const fields = splitDelimitedLine(rawLine, params.delimiter);
    if (!fields) {
      errors.push(`Line ${index + 1}: unterminated quoted field.`);
      return;
    }
    rows.push({ fields, sourceIndex: index + 1 });
  });
  if (errors.length > 0 || rows.length === 0) {
    return errors.length > 0
      ? { compounds: [], warnings: [], errors, format: params.format }
      : null;
  }

  const firstRow = rows[0];
  const smilesColumn = findColumnIndex(firstRow.fields, [
    'smiles',
    'canonical_smiles',
    'isomeric_smiles',
    'molecule_smiles'
  ]);
  const hasRecognizedHeader = smilesColumn >= 0;
  const compoundRows = hasRecognizedHeader ? rows.slice(1) : rows;
  if (!hasRecognizedHeader && rows.every((row) => row.fields.length < 2)) {
    return null;
  }

  const nameColumn = hasRecognizedHeader
    ? findColumnIndex(firstRow.fields, ['name', 'compound', 'compound_name', 'id', 'identifier', 'title'])
    : 1;
  const effectiveSmilesColumn = hasRecognizedHeader ? smilesColumn : 0;
  const compounds: VirtualScreeningCompound[] = [];
  const usedIds = new Set<string>();
  const warnings: string[] = [];
  if (hasRecognizedHeader) {
    warnings.push(`Parsed ${params.format.toUpperCase()} columns: SMILES${nameColumn >= 0 ? ' and compound name' : ''}.`);
  } else {
    warnings.push(`Parsed ${params.format.toUpperCase()} rows as SMILES in column 1 and names in column 2.`);
  }
  for (const row of compoundRows) {
    const smiles = row.fields[effectiveSmilesColumn] || '';
    const name = nameColumn >= 0 ? row.fields[nameColumn] || '' : '';
    if (!smiles.trim() && !name.trim()) continue;
    if (!smiles.trim()) {
      errors.push(`Line ${row.sourceIndex}: SMILES is missing.`);
      continue;
    }
    addCompound(compounds, usedIds, smiles, name || `Compound ${compounds.length + 1}`, row.sourceIndex, errors);
  }
  return { compounds, warnings, errors, format: params.format };
}

export function parseVirtualScreeningInput(rawInput: string): VirtualScreeningParseResult {
  const source = String(rawInput || '').replace(/\r\n?/g, '\n');
  const lines = source.split('\n');
  const nonEmpty = lines
    .map((line, index) => ({ line: line.trim(), index: index + 1 }))
    .filter(({ line }) => line && !line.startsWith('#'));
  if (!nonEmpty.length) {
    return { compounds: [], warnings: [], errors: [], format: 'empty' };
  }

  const compounds: VirtualScreeningCompound[] = [];
  const warnings: string[] = [];
  const errors: string[] = [];
  const usedIds = new Set<string>();
  const hasHeaders = nonEmpty.some(({ line }) => line.startsWith('>'));
  if (hasHeaders) {
    let currentName = '';
    let currentLine = 0;
    let currentSmiles: string[] = [];
    const flush = () => {
      if (!currentName && currentSmiles.length === 0) return;
      if (!currentName) {
        errors.push('Line ' + currentLine + ': FASTA/NCBI record is missing a name.');
      } else if (!currentSmiles.length) {
        errors.push('Compound ' + currentName + ': SMILES is missing.');
      } else {
        addCompound(compounds, usedIds, currentSmiles.join('').trim(), currentName, currentLine, errors);
      }
      currentSmiles = [];
    };
    for (const { line, index } of nonEmpty) {
      if (line.startsWith('>')) {
        flush();
        currentName = line.slice(1).trim();
        currentLine = index;
      } else if (currentName) {
        currentSmiles.push(line.split(/\s+/)[0]);
      } else {
        errors.push('Line ' + index + ': expected a record header beginning with ">".');
      }
    }
    flush();
    return { compounds, warnings, errors, format: 'fasta' };
  }

  const firstDataLine = nonEmpty[0]?.line || '';
  const delimiter: ',' | '\t' | null = firstDataLine.includes('\t')
    ? '\t'
    : firstDataLine.includes(',')
      ? ','
      : null;
  if (delimiter) {
    const parsedDelimited = parseDelimitedCompoundInput({
      source,
      delimiter,
      format: delimiter === '\t' ? 'tsv' : 'csv'
    });
    if (parsedDelimited) return parsedDelimited;
  }

  const looksLikeSmi = nonEmpty.some(({ line }) => /\s/.test(line));
  for (const { line, index } of nonEmpty) {
    const fields = line.split(/\s+/);
    const smiles = fields[0];
    const name = fields.slice(1).join(' ') || 'Compound ' + (compounds.length + 1);
    addCompound(compounds, usedIds, smiles, name, index, errors);
  }
  if (looksLikeSmi) {
    warnings.push('Whitespace-separated names were parsed as .smi records; the first token is the SMILES.');
  }
  return { compounds, warnings, errors, format: looksLikeSmi ? 'smi' : 'lines' };
}

export async function validateVirtualScreeningSmiles(
  compounds: VirtualScreeningCompound[]
): Promise<VirtualScreeningValidationResult> {
  const rdkit = await loadRDKitModule();
  const invalid: Array<{ index: number; message: string }> = [];
  const warnings: string[] = [];
  const canonicalOwners = new Map<string, string>();
  const next = compounds.map((compound) => ({ ...compound }));
  for (let index = 0; index < next.length; index += 1) {
    const compound = next[index];
    let mol: ReturnType<typeof rdkit.get_mol> = null;
    try {
      mol = rdkit.get_mol(compound.smiles);
    } catch {
      mol = null;
    }
    if (!mol) {
      invalid.push({ index, message: 'Invalid SMILES: ' + compound.smiles });
      continue;
    }
    try {
      const canonical = String(mol.get_smiles?.() || compound.smiles).trim();
      const canonicalSmiles = canonical || compound.smiles;
      next[index].canonicalSmiles = canonicalSmiles;
      const owner = canonicalOwners.get(canonicalSmiles);
      if (owner) {
        warnings.push(compound.name + ' duplicates ' + owner + '; duplicate molecules will be skipped.');
      } else {
        canonicalOwners.set(canonicalSmiles, compound.name);
      }
    } finally {
      mol.delete();
    }
  }
  return { compounds: next, invalid, warnings };
}

const NESSO_BINDER_CHAIN_POOL = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789';

export interface VirtualScreeningChainPlan {
  assignments: string[][];
  targetChainIds: string[];
  contextLigandChainIds: string[];
  binderChainId: string;
}

export function chooseVirtualScreeningBinderChainId(occupiedInput: Iterable<string>): string {
  const occupied = new Set(Array.from(occupiedInput, (value) => String(value || '').trim()).filter(Boolean));
  for (const candidate of NESSO_BINDER_CHAIN_POOL) {
    if (!occupied.has(candidate)) return candidate;
  }
  for (let index = 1; index <= 999; index += 1) {
    const candidate = `V${index}`;
    if (!occupied.has(candidate)) return candidate;
  }
  throw new Error('Unable to allocate a chain id for the screening binder.');
}

export function buildVirtualScreeningChainPlan(components: InputComponent[]): VirtualScreeningChainPlan {
  const assignments = assignChainIdsForComponents(components);
  const targetChainIds: string[] = [];
  const contextLigandChainIds: string[] = [];
  components.forEach((component, index) => {
    const chainIds = assignments[index] || [];
    if (component.type === 'protein') targetChainIds.push(...chainIds);
    if (component.type === 'ligand') contextLigandChainIds.push(...chainIds);
  });
  return {
    assignments,
    targetChainIds,
    contextLigandChainIds,
    binderChainId: chooseVirtualScreeningBinderChainId(assignments.flat())
  };
}

export function buildVirtualScreeningYaml(params: {
  components?: InputComponent[];
  proteinSequence?: string;
  rawInput: string;
  batchName?: string;
  /** The library will be uploaded as a compounds_file part instead of inline YAML. */
  libraryFromFile?: boolean;
}): {
  yaml: string;
  compounds: VirtualScreeningCompound[];
  warnings: string[];
  chainPlan: VirtualScreeningChainPlan;
} {
  const parsed = params.libraryFromFile
    ? { compounds: [] as VirtualScreeningCompound[], warnings: [] as string[], errors: [] as string[] }
    : parseVirtualScreeningInput(params.rawInput);
  if (parsed.errors.length) throw new Error(parsed.errors.join(' '));
  if (!params.libraryFromFile && !parsed.compounds.length) throw new Error('Add at least one compound SMILES before running.');
  const compatibilityProtein = String(params.proteinSequence || '').replace(/\s+/g, '').toUpperCase();
  const components = Array.isArray(params.components) && params.components.length > 0
    ? params.components
    : compatibilityProtein
      ? [{
          id: 'virtual-screening-target',
          type: 'protein' as const,
          numCopies: 1,
          sequence: compatibilityProtein,
          useMsa: false,
          cyclic: false
        }]
      : [];
  if (components.some((component) => component.type !== 'protein' && component.type !== 'ligand')) {
    throw new Error('Nesso-1 Virtual Screening accepts protein and ligand components only; DNA/RNA are not supported.');
  }
  const proteinComponents = components.filter((component) => component.type === 'protein');
  if (!proteinComponents.length) throw new Error('Add at least one target protein component before running.');
  const chainPlan = buildVirtualScreeningChainPlan(components);
  const sequences = components.map((component, index) => {
    const chainIds = chainPlan.assignments[index] || [];
    const id = chainIds.length === 1 ? chainIds[0] : chainIds;
    if (component.type === 'protein') {
      const sequence = component.sequence.replace(/\s+/g, '').toUpperCase();
      if (!sequence) throw new Error(`Protein ${index + 1} is missing its sequence.`);
      if (!/^[ACDEFGHIKLMNPQRSTVWY]+$/.test(sequence)) {
        throw new Error(`Protein ${index + 1} contains residues unsupported by Nesso-1.`);
      }
      if (component.cyclic || (component.modifications || []).length > 0) {
        throw new Error('Nesso-1 does not support cyclic or modified protein components.');
      }
      return { protein: { id, sequence } };
    }
    const value = component.sequence.trim();
    if (!value) throw new Error(`Context ligand ${index + 1} is missing its input.`);
    return component.inputMethod === 'ccd'
      ? { ligand: { id, ccd: value.toUpperCase() } }
      : { ligand: { id, smiles: value } };
  });
  const payload: Record<string, unknown> = {
    version: 1,
    sequences
  };
  if (!params.libraryFromFile) {
    payload.virtual_screening = {
      name: params.batchName || 'Virtual screening',
      compounds: parsed.compounds.map((compound) => ({
        id: compound.id,
        name: compound.name,
        smiles: compound.smiles
      }))
    };
  }
  return {
    yaml: yaml.dump(payload, { lineWidth: -1, noRefs: true, sortKeys: false }),
    compounds: parsed.compounds,
    warnings: parsed.warnings,
    chainPlan
  };
}

export const VIRTUAL_SCREENING_EXAMPLE =
  '>aspirin\nCC(=O)OC1=CC=CC=C1C(=O)O\n>caffeine\nCn1c(=O)c2c(ncn2C)n(C)c1=O';
