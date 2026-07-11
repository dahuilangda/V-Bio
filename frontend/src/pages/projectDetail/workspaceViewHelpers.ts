import type { InputComponent, PredictionConstraint, ProjectTask } from '../../types/models';
import { downloadResultBlob, downloadResultFile } from '../../api/backendApi';
import {
  normalizeLeadOptCandidatesUiState,
  type LeadOptCandidatesUiState
} from '../../components/project/leadopt/LeadOptCandidatesPanel';
import {
  buildLeadOptPredictionRecordKey,
  parseLeadOptPredictionRecordKey,
  type LeadOptPredictionRecord
} from '../../components/project/leadopt/hooks/useLeadOptMmpQueryMachine';
import { readLeadOptTaskSummary } from '../projectTasks/taskDataUtils';

export function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' ? (value as Record<string, unknown>) : {};
}

export function hasExplicitPeptideResiduePool(task: ProjectTask | null | undefined): boolean {
  const properties = asRecord(task?.properties);
  const options = asRecord(properties.__vbio_input_options_v1);
  return Array.isArray(options.peptideResiduePool) || Array.isArray(options.peptide_residue_pool);
}


export function readNestedObjectPath(payload: Record<string, unknown>, path: string): unknown {
  let current: unknown = payload;
  for (const token of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[token];
  }
  return current;
}

export function readFirstRecordArrayFromObjectPaths(payloads: Record<string, unknown>[], paths: string[]): Array<Record<string, unknown>> {
  for (const payload of payloads) {
    for (const path of paths) {
      const rows = asRecordArray(readNestedObjectPath(payload, path));
      if (rows.length > 0) return rows;
    }
  }
  return [];
}

export function summarizeCopilotComponents(components: InputComponent[] | null | undefined) {
  if (!Array.isArray(components)) return [];
  return components.map((component, index) => ({
    index,
    id: readText(component.id).trim(),
    type: readText(component.type).trim(),
    sequenceLength: readText(component.sequence).trim().length,
    numCopies: component.numCopies,
    inputMethod: component.inputMethod || undefined,
    useMsa: component.useMsa,
    cyclic: component.cyclic,
    modificationsCount: Array.isArray(component.modifications) ? component.modifications.length : 0
  }));
}

export function summarizeCopilotConstraints(constraints: PredictionConstraint[] | null | undefined) {
  if (!Array.isArray(constraints)) return [];
  return constraints.map((constraint, index) => ({
    index,
    id: readText((constraint as { id?: string }).id).trim(),
    type: readText((constraint as { type?: string }).type).trim()
  }));
}

export function summarizePeptideDesignCandidates(confidence: Record<string, unknown>) {
  const peptide = asRecord(confidence.peptide_design);
  const rows = readFirstRecordArrayFromObjectPaths(
    [confidence, peptide, asRecord(peptide.progress), asRecord(confidence.progress)],
    [
      'peptide_design.best_sequences',
      'best_sequences',
      'current_best_sequences',
      'progress.best_sequences',
      'progress.current_best_sequences',
      'candidates'
    ]
  );
  return {
    count: rows.length,
    top: rows.slice(0, 5).map((row, index) => ({
      index,
      rank: toFiniteNumber(row.rank ?? row.ranking ?? row.order),
      generation: toFiniteNumber(row.generation ?? row.iteration ?? row.iter),
      score: toFiniteNumber(row.score ?? row.composite_score ?? row.fitness ?? row.objective),
      plddt: normalizePlddtMetric(row.plddt ?? row.binder_avg_plddt ?? row.ligand_mean_plddt ?? row.mean_plddt),
      interfaceMetric: toFiniteNumber(row.ligand_ipsae_max ?? row.ipsae_dom ?? row.pair_iptm ?? row.pairIptm ?? row.iptm),
      sequenceLength: readText(row.peptide_sequence ?? row.binder_sequence ?? row.candidate_sequence ?? row.designed_sequence ?? row.sequence).trim().length,
      structureName: readText(row.structureName ?? row.structure_name ?? row.structure_file ?? row.structure_path).trim()
    }))
  };
}

export function summarizeCopilotTask(task: ProjectTask | null | undefined) {
  if (!task) return null;
  const confidence = asRecord(task.confidence);
  return {
    id: readText(task.id).trim(),
    project_id: readText(task.project_id).trim(),
    name: readText(task.name).trim(),
    summary: readText(task.summary).trim(),
    task_id: readText(task.task_id).trim(),
    task_state: readText(task.task_state).trim(),
    status_text: readText(task.status_text).trim(),
    error_text: readText(task.error_text).trim(),
    backend: readText(task.backend).trim(),
    seed: task.seed,
    structure_name: readText(task.structure_name).trim(),
    submitted_at: task.submitted_at,
    completed_at: task.completed_at,
    duration_seconds: task.duration_seconds,
    components: summarizeCopilotComponents(task.components),
    constraints: summarizeCopilotConstraints(task.constraints),
    properties: task.properties,
    affinitySummary: {
      keys: Object.keys(asRecord(task.affinity)).slice(0, 24)
    },
    confidenceSummary: {
      keys: Object.keys(confidence).slice(0, 24),
      peptideDesign: summarizePeptideDesignCandidates(confidence)
    }
  };
}

export function normalizeCopilotPrefillComponents(value: unknown): InputComponent[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item, index) => {
      const row = asRecord(item);
      let componentType = readText(row.type).trim().toLowerCase();
      componentType = componentType.replace(/[-\s]+/g, '_');
      if (componentType === 'peptide' || componentType === 'polypeptide') componentType = 'protein';
      if (
        componentType === 'small_molecule' ||
        componentType === 'smallmolecule' ||
        componentType === 'molecule' ||
        componentType === 'compound' ||
        componentType === 'drug' ||
        componentType === 'smiles' ||
        componentType === 'ccd'
      ) {
        componentType = 'ligand';
      }
      if (componentType !== 'protein' && componentType !== 'ligand' && componentType !== 'dna' && componentType !== 'rna') return null;
      const rawSequence =
        readText(row.sequence).trim() ||
        readText(row.value).trim() ||
        readText(row.input).trim() ||
        (componentType === 'ligand'
          ? readText(row.smiles).trim() || readText(row.ccd).trim() || readText(row.ligand).trim()
          : '');
      const sequence = componentType === 'ligand' ? rawSequence : rawSequence.replace(/\s+/g, '').toUpperCase();
      if (!sequence) return null;
      const component: InputComponent = {
        id: `copilot-${componentType}-${index + 1}`,
        type: componentType as InputComponent['type'],
        sequence,
        numCopies: Math.max(1, Math.floor(Number(row.numCopies) || 1)),
      };
      if (componentType === 'protein') {
        component.useMsa = row.useMsa === false ? false : true;
      }
      if (componentType === 'ligand') {
        component.inputMethod = readText(row.inputMethod).trim() === 'ccd' ? 'ccd' : 'smiles';
      }
      return component;
    })
    .filter((component): component is InputComponent => Boolean(component));
}

export function normalizeCopilotComponentPartial(value: unknown): Partial<InputComponent> {
  const row = asRecord(value);
  const patch: Partial<InputComponent> = {};
  let componentType = readText(row.type).trim().toLowerCase().replace(/[-\s]+/g, '_');
  if (componentType === 'peptide' || componentType === 'polypeptide') componentType = 'protein';
  if (
    componentType === 'small_molecule' ||
    componentType === 'smallmolecule' ||
    componentType === 'molecule' ||
    componentType === 'compound' ||
    componentType === 'drug' ||
    componentType === 'smiles' ||
    componentType === 'ccd'
  ) {
    componentType = 'ligand';
  }
  if (componentType === 'protein' || componentType === 'ligand' || componentType === 'dna' || componentType === 'rna') {
    patch.type = componentType;
  }
  const rawSequence =
    readText(row.sequence).trim() ||
    readText(row.value).trim() ||
    readText(row.input).trim() ||
    readText(row.smiles).trim() ||
    readText(row.ccd).trim() ||
    readText(row.ligand).trim();
  if (rawSequence) {
    patch.sequence = patch.type === 'ligand' ? rawSequence : rawSequence.replace(/\s+/g, '').toUpperCase();
  }
  const copies = Math.floor(Number(row.numCopies));
  if (Number.isFinite(copies) && copies >= 1) patch.numCopies = copies;
  if (typeof row.useMsa === 'boolean') patch.useMsa = row.useMsa;
  const inputMethod = readText(row.inputMethod).trim();
  if (inputMethod === 'smiles' || inputMethod === 'ccd') patch.inputMethod = inputMethod;
  return patch;
}

export function findCopilotComponentIndex(components: InputComponent[], selectorValue: unknown): number {
  const selector = asRecord(selectorValue);
  const index = Math.floor(Number(selector.index));
  if (Number.isFinite(index) && index >= 1 && index <= components.length) return index - 1;
  const id = readText(selector.id).trim();
  if (id) {
    const byId = components.findIndex((component) => component.id === id);
    if (byId >= 0) return byId;
  }
  const type = readText(selector.type).trim().toLowerCase();
  const sequenceContains = readText(selector.sequenceContains).trim();
  return components.findIndex((component) => {
    if (type && component.type !== type) return false;
    if (sequenceContains && !readText(component.sequence).includes(sequenceContains)) return false;
    return Boolean(type || sequenceContains);
  });
}

export function applyCopilotComponentPatchOperations(components: InputComponent[], operationsValue: unknown): InputComponent[] {
  if (!Array.isArray(operationsValue) || operationsValue.length === 0) return components;
  let next = [...components];
  let changed = false;
  operationsValue.slice(0, 16).forEach((operationValue) => {
    const operation = asRecord(operationValue);
    const op = readText(operation.op).trim().toLowerCase();
    if (op === 'append') {
      const [component] = normalizeCopilotPrefillComponents([operation.component]);
      if (component) {
        next = [...next, { ...component, id: component.id || `copilot-${component.type}-${next.length + 1}` }];
        changed = true;
      }
      return;
    }
    if (op === 'replace_all') {
      const componentsReplacement = normalizeCopilotPrefillComponents(operation.components);
      if (componentsReplacement.length > 0) {
        next = componentsReplacement;
        changed = true;
      }
      return;
    }
    const targetIndex = findCopilotComponentIndex(next, operation.selector);
    if (targetIndex < 0) return;
    if (op === 'remove') {
      next = next.filter((_, index) => index !== targetIndex);
      changed = true;
      return;
    }
    if (op === 'update') {
      const patch = normalizeCopilotComponentPartial(operation.component);
      if (Object.keys(patch).length === 0) return;
      next = next.map((component, index) => (index === targetIndex ? { ...component, ...patch } : component));
      changed = true;
    }
  });
  return changed && next.length > 0 ? next : components;
}

export function readFiniteNumber(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

export function asPredictionRecordMap(value: unknown): Record<string, LeadOptPredictionRecord> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return value as Record<string, LeadOptPredictionRecord>;
}

export function hasPersistedIpsaeMetric(value: unknown): boolean {
  const row = asRecord(value);
  return (
    toFiniteNumber(row.ligand_ipsae_max ?? row.ligandIpsaeMax) !== null ||
    toFiniteNumber(row.ipsae_dom ?? row.ipsaeDom) !== null
  );
}

export function normalizePredictionState(value: unknown): 'QUEUED' | 'RUNNING' | 'SUCCESS' | 'FAILURE' {
  const token = readText(value).trim().toUpperCase();
  if (token === 'RUNNING' || token === 'SUCCESS' || token === 'FAILURE') return token;
  return 'QUEUED';
}

export function predictionStatePriority(state: unknown): number {
  const normalized = normalizePredictionState(state);
  if (normalized === 'QUEUED') return 1;
  if (normalized === 'RUNNING') return 2;
  return 3;
}

export function hasPredictionRecordMetrics(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  if (toFiniteNumber(record.interfaceMetricValue) !== null) return true;
  if (toFiniteNumber(record.pairIptm) !== null) return true;
  if (toFiniteNumber(record.pairPae) !== null) return true;
  if (toFiniteNumber(record.ligandPlddt) !== null) return true;
  return Array.isArray(record.ligandAtomPlddts) && record.ligandAtomPlddts.length > 0;
}

export function normalizePredictionInterfaceSource(value: unknown): 'ipsae' | 'iptm' | 'none' {
  const token = readText(value).trim().toLowerCase();
  if (token === 'ipsae' || token === 'iptm') return token;
  return 'none';
}

export function mergePredictionInterfaceMetric(
  primary: LeadOptPredictionRecord,
  secondary: LeadOptPredictionRecord
): Pick<LeadOptPredictionRecord, 'interfaceMetricValue' | 'interfaceMetricLabel' | 'interfaceMetricSource'> {
  const primarySource = normalizePredictionInterfaceSource(primary.interfaceMetricSource);
  const secondarySource = normalizePredictionInterfaceSource(secondary.interfaceMetricSource);
  const preferred = primarySource === 'ipsae' || secondarySource !== 'ipsae' ? primary : secondary;
  const fallback = preferred === primary ? secondary : primary;
  const preferredValue = toFiniteNumber(preferred.interfaceMetricValue);
  const fallbackValue = toFiniteNumber(fallback.interfaceMetricValue);
  const preferredSource = normalizePredictionInterfaceSource(preferred.interfaceMetricSource);
  const fallbackSource = normalizePredictionInterfaceSource(fallback.interfaceMetricSource);
  const mergedSource = preferredSource !== 'none' ? preferredSource : fallbackSource;
  const mergedLabel = mergedSource === 'iptm' ? 'ipTM' : 'IPSAE';
  return {
    interfaceMetricValue: preferredValue ?? fallbackValue,
    interfaceMetricLabel: mergedLabel,
    interfaceMetricSource: mergedSource
  };
}

export function hasPredictionRecordStructure(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  return readText(record.structureText).trim().length > 0;
}

export function hasExactPredictionRenderContract(record: LeadOptPredictionRecord | null | undefined): boolean {
  if (!record) return false;
  const renderSmiles = readText(record.ligandRenderSmiles).trim();
  return renderSmiles.length > 0 && Array.isArray(record.ligandRenderAtomPlddts) && record.ligandRenderAtomPlddts.length > 0;
}

export function pickPredictionRenderContract(
  primary: LeadOptPredictionRecord | null | undefined,
  secondary: LeadOptPredictionRecord | null | undefined
): { ligandRenderSmiles: string; ligandRenderAtomPlddts: number[] } {
  if (hasExactPredictionRenderContract(primary)) {
    return {
      ligandRenderSmiles: readText(primary?.ligandRenderSmiles).trim(),
      ligandRenderAtomPlddts: Array.isArray(primary?.ligandRenderAtomPlddts) ? primary!.ligandRenderAtomPlddts : []
    };
  }
  if (hasExactPredictionRenderContract(secondary)) {
    return {
      ligandRenderSmiles: readText(secondary?.ligandRenderSmiles).trim(),
      ligandRenderAtomPlddts: Array.isArray(secondary?.ligandRenderAtomPlddts) ? secondary!.ligandRenderAtomPlddts : []
    };
  }
  return {
    ligandRenderSmiles: '',
    ligandRenderAtomPlddts: []
  };
}

export function choosePreferredPredictionRecord(
  left: LeadOptPredictionRecord,
  right: LeadOptPredictionRecord
): LeadOptPredictionRecord {
  const leftPriority = predictionStatePriority(left.state);
  const rightPriority = predictionStatePriority(right.state);
  if (leftPriority !== rightPriority) {
    return rightPriority > leftPriority ? right : left;
  }
  const leftMetrics = hasPredictionRecordMetrics(left) ? 1 : 0;
  const rightMetrics = hasPredictionRecordMetrics(right) ? 1 : 0;
  if (leftMetrics !== rightMetrics) {
    return rightMetrics > leftMetrics ? right : left;
  }
  const leftStructure = hasPredictionRecordStructure(left) ? 1 : 0;
  const rightStructure = hasPredictionRecordStructure(right) ? 1 : 0;
  if (leftStructure !== rightStructure) {
    return rightStructure > leftStructure ? right : left;
  }
  const leftTs = Number.isFinite(Number(left.updatedAt)) ? Number(left.updatedAt) : 0;
  const rightTs = Number.isFinite(Number(right.updatedAt)) ? Number(right.updatedAt) : 0;
  if (leftTs !== rightTs) {
    return rightTs > leftTs ? right : left;
  }
  return readText(right.error).trim() ? right : left;
}

export function mergePredictionRecordPair(
  fromConfidence: LeadOptPredictionRecord | null | undefined,
  fromProperties: LeadOptPredictionRecord | null | undefined
): LeadOptPredictionRecord | null {
  if (!fromConfidence && !fromProperties) return null;
  if (!fromConfidence) return fromProperties || null;
  if (!fromProperties) return fromConfidence;
  const primary = choosePreferredPredictionRecord(fromConfidence, fromProperties);
  const secondary = primary === fromConfidence ? fromProperties : fromConfidence;
  const renderContract = pickPredictionRenderContract(primary, secondary);
  const mergedInterfaceMetric = mergePredictionInterfaceMetric(primary, secondary);
  return {
    ...secondary,
    ...primary,
    taskId: readText(primary.taskId || secondary.taskId).trim(),
    state: normalizePredictionState(primary.state),
    backend: readText(primary.backend || secondary.backend).trim().toLowerCase(),
    pairIptm: toFiniteNumber(primary.pairIptm) ?? toFiniteNumber(secondary.pairIptm),
    interfaceMetricValue: mergedInterfaceMetric.interfaceMetricValue,
    interfaceMetricLabel: mergedInterfaceMetric.interfaceMetricLabel,
    interfaceMetricSource: mergedInterfaceMetric.interfaceMetricSource,
    pairPae: toFiniteNumber(primary.pairPae) ?? toFiniteNumber(secondary.pairPae),
    pairIptmResolved:
      primary.pairIptmResolved === true ||
      secondary.pairIptmResolved === true ||
      hasPredictionRecordMetrics(primary) ||
      hasPredictionRecordMetrics(secondary),
    ligandPlddt: toFiniteNumber(primary.ligandPlddt) ?? toFiniteNumber(secondary.ligandPlddt),
    ligandAtomPlddts: Array.isArray(primary.ligandAtomPlddts)
      ? primary.ligandAtomPlddts
      : Array.isArray(secondary.ligandAtomPlddts)
        ? secondary.ligandAtomPlddts
        : [],
    ligandRenderSmiles: renderContract.ligandRenderSmiles,
    ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts,
    resultBundleHydrated: primary.resultBundleHydrated === true || secondary.resultBundleHydrated === true,
    updatedAt: Math.max(
      Number.isFinite(Number(primary.updatedAt)) ? Number(primary.updatedAt) : 0,
      Number.isFinite(Number(secondary.updatedAt)) ? Number(secondary.updatedAt) : 0
    )
  };
}

export function mergePredictionRecordMaps(
  confidenceInput: unknown,
  propertiesInput: unknown,
  preferredBackendInput?: unknown
): Record<string, LeadOptPredictionRecord> {
  const confidence = compactLeadOptPredictionMap(asPredictionRecordMap(confidenceInput), preferredBackendInput);
  const properties = compactLeadOptPredictionMap(asPredictionRecordMap(propertiesInput), preferredBackendInput);
  const merged: Record<string, LeadOptPredictionRecord> = {};
  const keys = new Set<string>([...Object.keys(confidence), ...Object.keys(properties)]);
  for (const key of keys) {
    const normalizedKey = readText(key).trim();
    if (!normalizedKey) continue;
    const mergedRecord = mergePredictionRecordPair(confidence[normalizedKey], properties[normalizedKey]);
    if (!mergedRecord) continue;
    merged[normalizedKey] = mergedRecord;
  }
  return merged;
}

export function summarizeLeadOptPredictions(records: Record<string, LeadOptPredictionRecord>) {
  let queued = 0;
  let running = 0;
  let success = 0;
  let failure = 0;
  for (const record of Object.values(records)) {
    const token = String(record.state || '').toUpperCase();
    if (token === 'QUEUED') queued += 1;
    else if (token === 'RUNNING') running += 1;
    else if (token === 'SUCCESS') success += 1;
    else if (token === 'FAILURE') failure += 1;
  }
  return {
    total: Object.keys(records).length,
    queued,
    running,
    success,
    failure
  };
}

export function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) as Array<Record<string, unknown>>;
}

export function toFiniteNumber(value: unknown): number | null {
  const numeric = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(numeric)) return null;
  return numeric;
}

export function normalizePlddtMetric(value: unknown): number | null {
  const numeric = toFiniteNumber(value);
  if (numeric === null) return null;
  const scaled = numeric >= 0 && numeric <= 1 ? numeric * 100 : numeric;
  if (!Number.isFinite(scaled)) return null;
  return Math.max(0, Math.min(100, scaled));
}

export function compactLigandAtomPlddts(values: unknown): number[] {
  if (!Array.isArray(values)) return [];
  const out: number[] = [];
  for (const item of values) {
    const normalized = normalizePlddtMetric(item);
    if (normalized === null) continue;
    out.push(Math.round(normalized * 100) / 100);
    if (out.length >= 256) break;
  }
  return out;
}

export function hydratePredictionRecordMetricsFromHistory(
  current: LeadOptPredictionRecord | null | undefined,
  historical: LeadOptPredictionRecord | null | undefined
): LeadOptPredictionRecord | null {
  if (!current && !historical) return null;
  if (!current) return historical || null;
  if (!historical) return current;
  const renderContract = pickPredictionRenderContract(current, historical);
  const mergedInterfaceMetric = mergePredictionInterfaceMetric(current, historical);
  return {
    ...current,
    pairIptm: toFiniteNumber(current.pairIptm) ?? toFiniteNumber(historical.pairIptm),
    interfaceMetricValue: mergedInterfaceMetric.interfaceMetricValue,
    interfaceMetricLabel: mergedInterfaceMetric.interfaceMetricLabel,
    interfaceMetricSource: mergedInterfaceMetric.interfaceMetricSource,
    pairPae: toFiniteNumber(current.pairPae) ?? toFiniteNumber(historical.pairPae),
    pairIptmResolved:
      current.pairIptmResolved === true ||
      historical.pairIptmResolved === true ||
      hasPredictionRecordMetrics(current) ||
      hasPredictionRecordMetrics(historical),
    ligandPlddt: toFiniteNumber(current.ligandPlddt) ?? toFiniteNumber(historical.ligandPlddt),
    ligandAtomPlddts:
      Array.isArray(current.ligandAtomPlddts) && current.ligandAtomPlddts.length > 0
        ? current.ligandAtomPlddts
        : Array.isArray(historical.ligandAtomPlddts)
          ? historical.ligandAtomPlddts
          : [],
    ligandRenderSmiles: renderContract.ligandRenderSmiles,
    ligandRenderAtomPlddts: renderContract.ligandRenderAtomPlddts,
    structureText: readText(current.structureText).trim() || readText(historical.structureText).trim(),
    structureFormat:
      readText(current.structureText).trim()
        ? readText(current.structureFormat).toLowerCase() === 'pdb'
          ? 'pdb'
          : 'cif'
        : readText(historical.structureFormat).toLowerCase() === 'pdb'
          ? 'pdb'
          : readText(current.structureFormat).toLowerCase() === 'pdb'
            ? 'pdb'
            : 'cif',
    structureName: readText(current.structureName).trim() || readText(historical.structureName).trim(),
    resultBundleHydrated: current.resultBundleHydrated === true || historical.resultBundleHydrated === true,
    updatedAt: Math.max(
      Number.isFinite(Number(current.updatedAt)) ? Number(current.updatedAt) : 0,
      Number.isFinite(Number(historical.updatedAt)) ? Number(historical.updatedAt) : 0
    )
  };
}

export function hydratePredictionRecordMapFromHistory(
  currentInput: unknown,
  historicalInput: unknown
): Record<string, LeadOptPredictionRecord> {
  const current = asPredictionRecordMap(currentInput);
  const historical = asPredictionRecordMap(historicalInput);
  const out: Record<string, LeadOptPredictionRecord> = {};
  const keys = new Set([...Object.keys(historical), ...Object.keys(current)]);
  for (const key of keys) {
    const next = hydratePredictionRecordMetricsFromHistory(current[key], historical[key]);
    if (!next) continue;
    out[key] = next;
  }
  return out;
}

export function readBooleanToken(value: unknown): boolean | null {
  if (value === true) return true;
  if (value === false) return false;
  const token = readText(value).trim().toLowerCase();
  if (!token) return null;
  if (token === '1' || token === 'true' || token === 'yes' || token === 'on') return true;
  if (token === '0' || token === 'false' || token === 'no' || token === 'off') return false;
  return null;
}

export function normalizePredictionBackendStrict(value: unknown): string {
  const token = readText(value).trim().toLowerCase();
  if (token === 'boltz2') return 'boltz';
  if (token === 'boltz' || token === 'alphafold3' || token === 'protenix' || token === 'pocketxmol') return token;
  return '';
}

export const LEAD_OPT_UI_STATE_STORAGE_KEY = 'vbio:lead_opt:results_ui_state:v1';
export const SESSION_KEY = 'vbio_session';

export function readSessionIdentityFromLocalStorage(): string {
  if (typeof window === 'undefined') return '';
  try {
    const raw = window.localStorage.getItem(SESSION_KEY);
    if (!raw) return '';
    const payload = JSON.parse(raw) as Record<string, unknown>;
    const userId = readText(payload.userId).trim();
    const username = readText(payload.username).trim().toLowerCase();
    return userId || username;
  } catch {
    return '';
  }
}

export function buildLeadOptUiStateScopeKey(params: {
  sessionIdentity: string;
  projectId: string;
  taskRowId: string;
  queryId: string;
}): string {
  const sessionIdentity = readText(params.sessionIdentity).trim().toLowerCase();
  const projectId = readText(params.projectId).trim();
  const taskRowId = readText(params.taskRowId).trim();
  const queryId = readText(params.queryId).trim();
  if (!sessionIdentity || !projectId || !taskRowId) return '';
  return [sessionIdentity, projectId, taskRowId, queryId || '__query__'].join('|');
}

export function readLeadOptUiStateStoreFromLocal(): Record<string, unknown> {
  if (typeof window === 'undefined') return {};
  try {
    const raw = window.localStorage.getItem(LEAD_OPT_UI_STATE_STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return asRecord(parsed);
  } catch {
    return {};
  }
}

export function readLeadOptUiStateFromLocal(scopeKey: string): LeadOptCandidatesUiState | null {
  const normalizedScopeKey = readText(scopeKey).trim();
  if (!normalizedScopeKey) return null;
  const store = readLeadOptUiStateStoreFromLocal();
  const payload = store[normalizedScopeKey];
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  return compactLeadOptCandidatesUiState(normalizeLeadOptCandidatesUiState(payload, 'pocketxmol'));
}

export function writeLeadOptUiStateToLocal(scopeKey: string, uiState: LeadOptCandidatesUiState): void {
  if (typeof window === 'undefined') return;
  const normalizedScopeKey = readText(scopeKey).trim();
  if (!normalizedScopeKey) return;
  const store = readLeadOptUiStateStoreFromLocal();
  const compactUiState = compactLeadOptCandidatesUiState(uiState);
  const nextStore = {
    ...store,
    [normalizedScopeKey]: compactUiState
  };
  try {
    window.localStorage.setItem(LEAD_OPT_UI_STATE_STORAGE_KEY, JSON.stringify(nextStore));
  } catch {
    // Ignore local storage write failures (quota / privacy mode).
  }
}

export function compactLeadOptPredictionRecord(value: LeadOptPredictionRecord): LeadOptPredictionRecord {
  const backend = normalizePredictionBackendStrict(value.backend);
  const renderContract = pickPredictionRenderContract(value, null);
  return {
    taskId: readText(value.taskId).trim(),
    state: value.state,
    backend,
    pairIptm: toFiniteNumber(value.pairIptm),
    interfaceMetricValue: toFiniteNumber(value.interfaceMetricValue),
    interfaceMetricLabel: value.interfaceMetricLabel === 'ipTM' ? 'ipTM' : 'IPSAE',
    interfaceMetricSource:
      value.interfaceMetricSource === 'ipsae' ? 'ipsae' : value.interfaceMetricSource === 'iptm' ? 'iptm' : 'none',
    pairPae: toFiniteNumber(value.pairPae),
    pairIptmResolved: value.pairIptmResolved === true,
    ligandPlddt: normalizePlddtMetric(value.ligandPlddt),
    ligandAtomPlddts: compactLigandAtomPlddts(value.ligandAtomPlddts),
    ligandRenderSmiles: renderContract.ligandRenderSmiles,
    ligandRenderAtomPlddts: compactLigandAtomPlddts(renderContract.ligandRenderAtomPlddts),
    structureText: '',
    structureFormat: readText(value.structureFormat).toLowerCase() === 'pdb' ? 'pdb' : 'cif',
    structureName: readText(value.structureName).trim(),
    resultBundleHydrated: value.resultBundleHydrated === true,
    error: readText(value.error),
    updatedAt: Number.isFinite(Number(value.updatedAt)) ? Number(value.updatedAt) : 0
  };
}

export function compactLeadOptPredictionMap(
  value: Record<string, LeadOptPredictionRecord>,
  _preferredBackendInput?: unknown
): Record<string, LeadOptPredictionRecord> {
  const out: Record<string, LeadOptPredictionRecord> = {};
  for (const [rawKey, record] of Object.entries(value)) {
    const normalizedRawKey = readText(rawKey).trim();
    const parsedKey = parseLeadOptPredictionRecordKey(rawKey);
    const backendFromKey = normalizePredictionBackendStrict(parsedKey.backend);
    const backendFromRawKey = normalizePredictionBackendStrict(normalizedRawKey);
    const parsedSmiles = readText(parsedKey.smiles).trim();

    // Reference prediction map uses backend-only keys.
    if (!backendFromKey && backendFromRawKey && parsedSmiles.toLowerCase() === backendFromRawKey) {
      const compactRecord = compactLeadOptPredictionRecord({
        ...record,
        backend: backendFromRawKey
      });
      const merged = mergePredictionRecordPair(out[backendFromRawKey], compactRecord);
      if (!merged) continue;
      out[backendFromRawKey] = merged;
      continue;
    }

    if (!parsedSmiles) continue;
    // Candidate prediction map uses `backend::smiles` keys only.
    const backend = backendFromKey;
    if (!backend) continue;
    const key = buildLeadOptPredictionRecordKey(backend, parsedSmiles);
    if (!key) continue;
    const compactRecord = compactLeadOptPredictionRecord({
      ...record,
      backend
    });
    const merged = mergePredictionRecordPair(out[key], compactRecord);
    if (!merged) continue;
    out[key] = merged;
  }
  return out;
}

export function compactLeadOptEnumeratedCandidateRow(value: unknown): Record<string, unknown> | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const row = value as Record<string, unknown>;
  const smiles = readText(row.smiles || row.candidate_smiles || row.predicted_smiles).trim();
  if (!smiles) return null;
  const nPairs = toFiniteNumber(row.n_pairs);
  const medianDelta = toFiniteNumber(row.median_delta);
  const propertiesRaw = asRecord(row.properties);
  const propertyDeltasRaw = asRecord(row.property_deltas);
  const properties: Record<string, unknown> = {};
  const propertyDeltas: Record<string, unknown> = {};
  const mw = toFiniteNumber(propertiesRaw.molecular_weight);
  const logp = toFiniteNumber(propertiesRaw.logp);
  const tpsa = toFiniteNumber(propertiesRaw.tpsa);
  const deltaMw = toFiniteNumber(propertyDeltasRaw.mw);
  const deltaLogp = toFiniteNumber(propertyDeltasRaw.logp);
  const deltaTpsa = toFiniteNumber(propertyDeltasRaw.tpsa);
  if (mw !== null) properties.molecular_weight = mw;
  if (logp !== null) properties.logp = logp;
  if (tpsa !== null) properties.tpsa = tpsa;
  if (deltaMw !== null) propertyDeltas.mw = deltaMw;
  if (deltaLogp !== null) propertyDeltas.logp = deltaLogp;
  if (deltaTpsa !== null) propertyDeltas.tpsa = deltaTpsa;
  const highlightAtomIndices = Array.isArray(row.final_highlight_atom_indices)
    ? Array.from(
        new Set(
          row.final_highlight_atom_indices
            .map((item) => Number(item))
            .filter((item) => Number.isFinite(item) && item >= 0)
            .map((item) => Math.floor(item))
        )
      )
    : [];
  const constantSmiles = readText(row.constant_smiles).trim();
  return {
    smiles,
    ...(nPairs === null ? {} : { n_pairs: nPairs }),
    ...(medianDelta === null ? {} : { median_delta: medianDelta }),
    properties,
    property_deltas: propertyDeltas,
    final_highlight_atom_indices: highlightAtomIndices,
    ...(constantSmiles ? { constant_smiles: constantSmiles } : {})
  };
}

export function compactLeadOptEnumeratedCandidates(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  const rows: Array<Record<string, unknown>> = [];
  for (const item of value) {
    const compact = compactLeadOptEnumeratedCandidateRow(item);
    if (!compact) continue;
    rows.push(compact);
  }
  return rows;
}

export function compactLeadOptQueryResult(value: unknown): Record<string, unknown> {
  const queryResult = asRecord(value);
  if (Object.keys(queryResult).length === 0) return {};
  const transforms = asRecordArray(queryResult.transforms);
  const globalTransforms = asRecordArray(queryResult.global_transforms);
  const clusters = asRecordArray(queryResult.clusters);
  const count = Number.isFinite(Number(queryResult.count)) ? Number(queryResult.count) : transforms.length;
  const globalCount = Number.isFinite(Number(queryResult.global_count))
    ? Number(queryResult.global_count)
    : Math.max(count, globalTransforms.length);
  const groupedByEnvironment = readBooleanToken(queryResult.grouped_by_environment);
  return {
    query_id: readText(queryResult.query_id).trim(),
    task_id: readText(queryResult.task_id).trim(),
    query_mode: readText(queryResult.query_mode).trim() || 'one-to-many',
    aggregation_type: readText(queryResult.aggregation_type).trim(),
    property_targets: asRecord(queryResult.property_targets),
    rule_env_radius: Number.isFinite(Number(queryResult.rule_env_radius)) ? Number(queryResult.rule_env_radius) : 1,
    ...(groupedByEnvironment === null ? {} : { grouped_by_environment: groupedByEnvironment }),
    mmp_database_id: readText(queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(queryResult.mmp_database_schema).trim(),
    cluster_group_by: readText(queryResult.cluster_group_by).trim(),
    transforms,
    global_transforms: globalTransforms,
    clusters,
    stats: asRecord(queryResult.stats),
    count,
    global_count: globalCount,
    min_pairs: Number.isFinite(Number(queryResult.min_pairs)) ? Number(queryResult.min_pairs) : 1
  };
}

export function readLeadOptStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .map((item) => readText(item).trim())
        .filter(Boolean)
    )
  );
}

export function readLeadOptIntegerArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return Array.from(
    new Set(
      value
        .map((item) => Number(item))
        .filter((item) => Number.isFinite(item) && item >= 0)
        .map((item) => Math.floor(item))
    )
  );
}

export function compactLeadOptVariableItems(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      const record = asRecord(item);
      const query = readText(record.query).trim();
      const fragmentId = readText(record.fragment_id).trim();
      const atomIndices = readLeadOptIntegerArray(record.atom_indices);
      if (!query && !fragmentId && atomIndices.length === 0) return null;
      return {
        query,
        mode: readText(record.mode).trim() || 'substructure',
        fragment_id: fragmentId,
        atom_indices: atomIndices
      } as Record<string, unknown>;
    })
    .filter((item): item is Record<string, unknown> => Boolean(item));
}

export function compactLeadOptQueryPayload(value: unknown): Record<string, unknown> {
  const payload = asRecord(value);
  const variableSpec = asRecord(payload.variable_spec);
  return {
    query_mol: readText(payload.query_mol).trim(),
    variable_spec: {
      mode: readText(variableSpec.mode).trim() || 'substructure',
      items: compactLeadOptVariableItems(variableSpec.items)
    },
    selected_fragment_ids: readLeadOptStringArray(payload.selected_fragment_ids),
    selected_fragment_atom_indices: readLeadOptIntegerArray(payload.selected_fragment_atom_indices),
    constant_spec: asRecord(payload.constant_spec),
    property_targets: asRecord(payload.property_targets),
    mmp_database_id: readText(payload.mmp_database_id).trim(),
    mmp_database_label: readText(payload.mmp_database_label).trim(),
    mmp_database_schema: readText(payload.mmp_database_schema).trim(),
    query_mode: readText(payload.query_mode).trim(),
    aggregation_type: readText(payload.aggregation_type).trim(),
    grouped_by_environment: readBooleanToken(payload.grouped_by_environment),
    min_pairs: toFiniteNumber(payload.min_pairs),
    rule_env_radius: toFiniteNumber(payload.rule_env_radius),
    max_results: toFiniteNumber(payload.max_results)
  };
}

export function buildLeadOptListMeta(leadOptMmpInput: unknown): Record<string, unknown> {
  const leadOptMmp = asRecord(leadOptMmpInput);
  const queryResult = asRecord(leadOptMmp.query_result);
  const selection = asRecord(leadOptMmp.selection);
  const predictionSummary = asRecord(leadOptMmp.prediction_summary);
  const predictionMap = asRecord(leadOptMmp.prediction_by_smiles);
  const compactCandidates = compactLeadOptEnumeratedCandidates(
    leadOptMmp.enumerated_candidates ?? asRecord(leadOptMmp.result_snapshot).enumerated_candidates
  );
  const compactQueryResult = compactLeadOptQueryResult({
    ...queryResult,
    query_id: readText(leadOptMmp.query_id || queryResult.query_id).trim(),
    task_id: readText(leadOptMmp.task_id || queryResult.task_id).trim(),
    mmp_database_id: readText(leadOptMmp.mmp_database_id || queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(leadOptMmp.mmp_database_label || queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(leadOptMmp.mmp_database_schema || queryResult.mmp_database_schema).trim()
  });
  const predictionTotal = toFiniteNumber(predictionSummary.total);
  const selectedFragmentIds = readLeadOptStringArray(
    selection.selected_fragment_ids ?? leadOptMmp.selected_fragment_ids
  );
  const selectedFragmentAtomIndices = readLeadOptIntegerArray(
    selection.selected_fragment_atom_indices ?? leadOptMmp.selected_fragment_atom_indices
  );
  const variableItems = compactLeadOptVariableItems(selection.variable_items ?? leadOptMmp.variable_items);
  const selectedFragmentQuery =
    readLeadOptStringArray(selection.variable_queries ?? leadOptMmp.variable_queries)[0] ||
    readText(leadOptMmp.selected_fragment_query).trim();
  const directionToken = readText(selection.direction ?? leadOptMmp.direction).trim().toLowerCase();
  const direction = directionToken === 'increase' || directionToken === 'decrease' ? directionToken : '';
  return {
    stage: readText(leadOptMmp.stage).trim(),
    prediction_stage: readText(leadOptMmp.prediction_stage).trim(),
    query_id: readText(leadOptMmp.query_id || queryResult.query_id).trim(),
    task_id: readText(leadOptMmp.task_id || queryResult.task_id).trim(),
    transform_count: toFiniteNumber(leadOptMmp.transform_count),
    candidate_count: toFiniteNumber(leadOptMmp.candidate_count),
    bucket_count:
      toFiniteNumber(leadOptMmp.bucket_count) ??
      predictionTotal ??
      Object.keys(predictionMap).length,
    mmp_database_id: readText(leadOptMmp.mmp_database_id || queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(leadOptMmp.mmp_database_label || queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(leadOptMmp.mmp_database_schema || queryResult.mmp_database_schema).trim(),
    selection: {
      selected_fragment_ids: selectedFragmentIds,
      selected_fragment_atom_indices: selectedFragmentAtomIndices,
      variable_queries: selectedFragmentQuery ? [selectedFragmentQuery] : [],
      variable_items: variableItems,
      grouped_by_environment_mode: readText(selection.grouped_by_environment_mode).trim().toLowerCase(),
      query_property: readText(selection.query_property).trim(),
      direction
    },
    selected_fragment_ids: selectedFragmentIds,
    selected_fragment_atom_indices: selectedFragmentAtomIndices,
    selected_fragment_query: selectedFragmentQuery,
    query_payload: compactLeadOptQueryPayload(leadOptMmp.query_payload),
    prediction_summary: {
      total: predictionTotal,
      queued: toFiniteNumber(predictionSummary.queued),
      running: toFiniteNumber(predictionSummary.running),
      success: toFiniteNumber(predictionSummary.success),
      failure: toFiniteNumber(predictionSummary.failure)
    },
    query_result: compactQueryResult,
    enumerated_candidates: compactCandidates,
    target_chain: readText(leadOptMmp.target_chain).trim(),
    ligand_chain: readText(leadOptMmp.ligand_chain).trim()
  };
}

export function buildLeadOptStateMeta(leadOptInput: unknown): Record<string, unknown> {
  const leadOpt = asRecord(leadOptInput);
  const predictionSummary = asRecord(leadOpt.prediction_summary);
  const selectedBackend = normalizePredictionBackendStrict(leadOpt.selected_backend);
  return {
    stage: readText(leadOpt.stage).trim(),
    prediction_stage: readText(leadOpt.prediction_stage).trim(),
    query_id: readText(leadOpt.query_id || asRecord(leadOpt.query_result).query_id).trim(),
    task_id: readText(leadOpt.task_id || asRecord(leadOpt.query_result).task_id).trim(),
    prediction_task_id: readText(leadOpt.prediction_task_id).trim(),
    prediction_candidate_smiles: readText(leadOpt.prediction_candidate_smiles).trim(),
    prediction_summary: {
      total: toFiniteNumber(predictionSummary.total) ?? 0,
      queued: toFiniteNumber(predictionSummary.queued) ?? 0,
      running: toFiniteNumber(predictionSummary.running) ?? 0,
      success: toFiniteNumber(predictionSummary.success) ?? 0,
      failure: toFiniteNumber(predictionSummary.failure) ?? 0,
      latest_task_id: readText(predictionSummary.latest_task_id).trim()
    },
    prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.prediction_by_smiles)),
    reference_prediction_by_backend: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.reference_prediction_by_backend)),
    ...(selectedBackend ? { selected_backend: selectedBackend } : {}),
    target_chain: readText(leadOpt.target_chain).trim(),
    ligand_chain: readText(leadOpt.ligand_chain).trim()
  };
}

export function mergeLeadOptStateMetaIntoProperties(
  propertiesInput: unknown,
  leadOptInput: unknown
): Record<string, unknown> {
  const properties = asRecord(propertiesInput);
  return {
    ...properties,
    lead_opt_state: buildLeadOptStateMeta(leadOptInput)
  };
}

export function mergeLeadOptMetaIntoProperties(
  propertiesInput: unknown,
  leadOptInput: unknown
): Record<string, unknown> {
  const properties = asRecord(propertiesInput);
  return {
    ...properties,
    lead_opt_list: buildLeadOptListMeta(leadOptInput),
    lead_opt_state: buildLeadOptStateMeta(leadOptInput)
  };
}

export function compactLeadOptForConfidenceWrite(leadOptInput: unknown): Record<string, unknown> {
  const leadOpt = asRecord(leadOptInput);
  const queryResult = asRecord(leadOpt.query_result);
  const predictionSummary = asRecord(leadOpt.prediction_summary);
  const compactPredictionSummary = {
    total: toFiniteNumber(predictionSummary.total) ?? 0,
    queued: toFiniteNumber(predictionSummary.queued) ?? 0,
    running: toFiniteNumber(predictionSummary.running) ?? 0,
    success: toFiniteNumber(predictionSummary.success) ?? 0,
    failure: toFiniteNumber(predictionSummary.failure) ?? 0,
    latest_task_id: readText(predictionSummary.latest_task_id).trim()
  };
  return {
    stage: readText(leadOpt.stage).trim(),
    prediction_stage: readText(leadOpt.prediction_stage).trim(),
    query_id: readText(leadOpt.query_id || queryResult.query_id).trim(),
    task_id: readText(leadOpt.task_id || queryResult.task_id).trim(),
    transform_count: toFiniteNumber(leadOpt.transform_count),
    candidate_count: toFiniteNumber(leadOpt.candidate_count),
    bucket_count: toFiniteNumber(leadOpt.bucket_count),
    mmp_database_id: readText(leadOpt.mmp_database_id || queryResult.mmp_database_id).trim(),
    mmp_database_label: readText(leadOpt.mmp_database_label || queryResult.mmp_database_label).trim(),
    mmp_database_schema: readText(leadOpt.mmp_database_schema || queryResult.mmp_database_schema).trim(),
    target_chain: readText(leadOpt.target_chain).trim(),
    ligand_chain: readText(leadOpt.ligand_chain).trim(),
    prediction_summary: compactPredictionSummary,
    prediction_by_smiles: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.prediction_by_smiles)),
    reference_prediction_by_backend: compactLeadOptPredictionMap(asPredictionRecordMap(leadOpt.reference_prediction_by_backend))
  };
}

export function buildLeadOptPredictionPersistSignature(records: Record<string, LeadOptPredictionRecord>): string {
  return Object.entries(records)
    .map(([key, record]) => {
      const normalizedKey = readText(key).trim();
      const taskId = readText(record.taskId).trim();
      const state = readText(record.state).trim().toUpperCase();
      const backend = readText(record.backend).trim().toLowerCase();
      const pairIptm = toFiniteNumber(record.pairIptm);
      const pairPae = toFiniteNumber(record.pairPae);
      const ligandPlddt = normalizePlddtMetric(record.ligandPlddt);
      const atomPlddts = compactLigandAtomPlddts(record.ligandAtomPlddts);
      const atomPlddtSignature = atomPlddts.length > 0
        ? `${atomPlddts.length}:${atomPlddts[0]?.toFixed(2) || ''}:${atomPlddts[atomPlddts.length - 1]?.toFixed(2) || ''}`
        : '';
      const error = readText(record.error).trim();
      return [
        normalizedKey,
        taskId,
        state,
        backend,
        pairIptm === null ? '' : pairIptm.toFixed(4),
        pairPae === null ? '' : pairPae.toFixed(3),
        record.pairIptmResolved === true ? '1' : '0',
        ligandPlddt === null ? '' : ligandPlddt.toFixed(3),
        atomPlddtSignature,
        error
      ].join('~');
    })
    .sort((a, b) => a.localeCompare(b))
    .join('||');
}

export function readLeadOptPersistRecordUpdatedAt(value: unknown): number {
  const record = asRecord(value);
  const raw = record.updatedAt ?? record.updated_at;
  const numeric = typeof raw === 'number' ? raw : typeof raw === 'string' ? Number(raw) : Number.NaN;
  return Number.isFinite(numeric) ? numeric : 0;
}

export function mergeLeadOptPersistRecordMap(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  const merged: Record<string, unknown> = { ...prev };
  for (const [key, nextRecord] of Object.entries(next)) {
    const prevRecord = merged[key];
    if (!prevRecord) {
      merged[key] = nextRecord;
      continue;
    }
    const nextUpdatedAt = readLeadOptPersistRecordUpdatedAt(nextRecord);
    const prevUpdatedAt = readLeadOptPersistRecordUpdatedAt(prevRecord);
    merged[key] = nextUpdatedAt >= prevUpdatedAt ? nextRecord : prevRecord;
  }
  return merged;
}

export function mergeLeadOptStateForPersist(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  return {
    ...prev,
    ...next,
    prediction_by_smiles: mergeLeadOptPersistRecordMap(next.prediction_by_smiles, prev.prediction_by_smiles),
    reference_prediction_by_backend: mergeLeadOptPersistRecordMap(
      next.reference_prediction_by_backend,
      prev.reference_prediction_by_backend
    )
  };
}

export function mergeLeadOptSnapshotForPersist(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  const nextQueryResult = compactLeadOptQueryResult(next.query_result);
  const prevQueryResult = compactLeadOptQueryResult(prev.query_result);
  const nextSnapshotIdentity = readText(nextQueryResult.query_id || nextQueryResult.task_id).trim();
  const prevSnapshotIdentity = readText(prevQueryResult.query_id || prevQueryResult.task_id).trim();
  const mergedQueryResult =
    nextSnapshotIdentity && prevSnapshotIdentity && nextSnapshotIdentity !== prevSnapshotIdentity
      ? nextQueryResult
      : {
          ...prevQueryResult,
          ...nextQueryResult,
          transforms:
            Array.isArray(nextQueryResult.transforms) && nextQueryResult.transforms.length > 0
              ? nextQueryResult.transforms
              : Array.isArray(prevQueryResult.transforms)
                ? prevQueryResult.transforms
                : [],
          global_transforms:
            Array.isArray(nextQueryResult.global_transforms) && nextQueryResult.global_transforms.length > 0
              ? nextQueryResult.global_transforms
              : Array.isArray(prevQueryResult.global_transforms)
                ? prevQueryResult.global_transforms
                : [],
          clusters:
            Array.isArray(nextQueryResult.clusters) && nextQueryResult.clusters.length > 0
              ? nextQueryResult.clusters
              : Array.isArray(prevQueryResult.clusters)
                ? prevQueryResult.clusters
                : []
        };
  return {
    ...prev,
    ...next,
    query_result: mergedQueryResult,
    selection:
      Object.keys(asRecord(next.selection)).length > 0
        ? asRecord(next.selection)
        : asRecord(prev.selection),
    query_payload:
      Object.keys(asRecord(next.query_payload)).length > 0
        ? asRecord(next.query_payload)
        : asRecord(prev.query_payload),
    enumerated_candidates:
      Array.isArray(next.enumerated_candidates) && next.enumerated_candidates.length > 0
        ? compactLeadOptEnumeratedCandidates(next.enumerated_candidates)
        : Array.isArray(prev.enumerated_candidates)
          ? compactLeadOptEnumeratedCandidates(prev.enumerated_candidates)
          : [],
    prediction_by_smiles: mergeLeadOptPersistRecordMap(next.prediction_by_smiles, prev.prediction_by_smiles),
    reference_prediction_by_backend: mergeLeadOptPersistRecordMap(
      next.reference_prediction_by_backend,
      prev.reference_prediction_by_backend
    )
  };
}

export function mergeLeadOptPatchPayloadForPersist(nextValue: unknown, prevValue: unknown): Record<string, unknown> {
  const next = asRecord(nextValue);
  const prev = asRecord(prevValue);
  if (Object.keys(next).length === 0 && Object.keys(prev).length === 0) return {};
  const merged: Record<string, unknown> = {
    ...prev,
    ...next
  };
  const nextProperties = asRecord(next.properties);
  const prevProperties = asRecord(prev.properties);
  if (Object.keys(nextProperties).length > 0 || Object.keys(prevProperties).length > 0) {
    merged.properties = {
      ...prevProperties,
      ...nextProperties,
      lead_opt_state: mergeLeadOptStateForPersist(nextProperties.lead_opt_state, prevProperties.lead_opt_state)
    };
  }
  const nextConfidence = asRecord(next.confidence);
  const prevConfidence = asRecord(prev.confidence);
  if (Object.keys(nextConfidence).length > 0 || Object.keys(prevConfidence).length > 0) {
    merged.confidence = {
      ...prevConfidence,
      ...nextConfidence,
      lead_opt_mmp: mergeLeadOptStateForPersist(nextConfidence.lead_opt_mmp, prevConfidence.lead_opt_mmp)
    };
  }
  return merged;
}

export function compactLeadOptCandidatesUiState(value: LeadOptCandidatesUiState): LeadOptCandidatesUiState {
  return {
    selectedBackend: readText(value.selectedBackend).trim().toLowerCase() || 'pocketxmol',
    stateFilter: value.stateFilter,
    showAdvanced: value.showAdvanced === true,
    modelMetricColumns: Array.isArray(value.modelMetricColumns) ? value.modelMetricColumns.slice(0, 3) : ['plddt', 'ipsae', 'iptm'],
    mwMin: readText(value.mwMin).trim(),
    mwMax: readText(value.mwMax).trim(),
    logpMin: readText(value.logpMin).trim(),
    logpMax: readText(value.logpMax).trim(),
    tpsaMin: readText(value.tpsaMin).trim(),
    tpsaMax: readText(value.tpsaMax).trim(),
    plddtMin: readText(value.plddtMin).trim(),
    plddtMax: readText(value.plddtMax).trim(),
    iptmMin: readText(value.iptmMin).trim(),
    iptmMax: readText(value.iptmMax).trim(),
    paeMin: readText(value.paeMin).trim(),
    paeMax: readText(value.paeMax).trim(),
    structureSearchMode: value.structureSearchMode,
    structureSearchQuery: readText(value.structureSearchQuery).trim(),
    previewRenderMode: value.previewRenderMode
  };
}

export function resolveLeadOptSnapshotFromTask(taskInput: unknown): Record<string, unknown> {
  const task = asRecord(taskInput);
  const properties = asRecord(task.properties);
  const fromProperties = asRecord(properties.lead_opt_list);
  const fromPropertiesState = asRecord(properties.lead_opt_state);
  const confidence = asRecord(task.confidence);
  const fromConfidence = asRecord(confidence.lead_opt_mmp);
  if (
    Object.keys(fromProperties).length === 0 &&
    Object.keys(fromPropertiesState).length === 0 &&
    Object.keys(fromConfidence).length === 0
  ) {
    return {};
  }

  const propertiesQueryResult = asRecord(fromProperties.query_result);
  const confidenceQueryResult = asRecord(fromConfidence.query_result);
  const propertiesSelection = asRecord(fromProperties.selection);
  const confidenceSelection = asRecord(fromConfidence.selection);
  const stateSelectedBackend = normalizePredictionBackendStrict(
    fromPropertiesState.selected_backend ?? fromConfidence.selected_backend
  );
  const preferredPredictionBackend = stateSelectedBackend;
  const mergedPredictions = mergePredictionRecordMaps(
    mergePredictionRecordMaps(fromConfidence.prediction_by_smiles, fromProperties.prediction_by_smiles, preferredPredictionBackend),
    fromPropertiesState.prediction_by_smiles,
    preferredPredictionBackend
  );
  const mergedReferencePredictions = mergePredictionRecordMaps(
    mergePredictionRecordMaps(
      fromConfidence.reference_prediction_by_backend,
      fromProperties.reference_prediction_by_backend,
      preferredPredictionBackend
    ),
    fromPropertiesState.reference_prediction_by_backend,
    preferredPredictionBackend
  );

  return {
    ...fromConfidence,
    ...fromProperties,
    query_result:
      Object.keys(propertiesQueryResult).length > 0
        ? propertiesQueryResult
        : confidenceQueryResult,
    enumerated_candidates:
      Array.isArray(fromProperties.enumerated_candidates) && fromProperties.enumerated_candidates.length > 0
        ? fromProperties.enumerated_candidates
        : Array.isArray(fromConfidence.enumerated_candidates)
          ? fromConfidence.enumerated_candidates
          : [],
    prediction_by_smiles: mergedPredictions,
    reference_prediction_by_backend: mergedReferencePredictions,
    ...(stateSelectedBackend ? { selected_backend: stateSelectedBackend } : {}),
    stage: readText(fromPropertiesState.stage).trim() || readText(fromProperties.stage).trim() || readText(fromConfidence.stage).trim(),
    prediction_stage:
      readText(fromPropertiesState.prediction_stage).trim() ||
      readText(fromProperties.prediction_stage).trim() ||
      readText(fromConfidence.prediction_stage).trim(),
    prediction_summary:
      Object.keys(asRecord(fromPropertiesState.prediction_summary)).length > 0
        ? asRecord(fromPropertiesState.prediction_summary)
        : Object.keys(asRecord(fromProperties.prediction_summary)).length > 0
          ? asRecord(fromProperties.prediction_summary)
          : asRecord(fromConfidence.prediction_summary),
    selection:
      Object.keys(propertiesSelection).length > 0
        ? propertiesSelection
        : confidenceSelection,
    ui_state: {},
    query_id:
      readText(fromProperties.query_id || propertiesQueryResult.query_id).trim() ||
      readText(fromPropertiesState.query_id).trim() ||
      readText(fromConfidence.query_id || confidenceQueryResult.query_id).trim(),
    task_id:
      readText(fromProperties.task_id || propertiesQueryResult.task_id).trim() ||
      readText(fromPropertiesState.task_id).trim() ||
      readText(fromConfidence.task_id || confidenceQueryResult.task_id).trim(),
    target_chain: readText(fromPropertiesState.target_chain || fromProperties.target_chain || fromConfidence.target_chain).trim(),
    ligand_chain: readText(fromPropertiesState.ligand_chain || fromProperties.ligand_chain || fromConfidence.ligand_chain).trim(),
    prediction_task_id:
      readText(fromPropertiesState.prediction_task_id).trim() ||
      readText(fromProperties.prediction_task_id).trim() ||
      readText(fromConfidence.prediction_task_id).trim(),
    prediction_candidate_smiles:
      readText(fromPropertiesState.prediction_candidate_smiles).trim() ||
      readText(fromProperties.prediction_candidate_smiles).trim() ||
      readText(fromConfidence.prediction_candidate_smiles).trim()
  };
}

export function resolveLeadOptDownloadTaskId(taskInput: unknown, structureTaskIdInput: unknown): string {
  const viewerTaskId = readText(structureTaskIdInput).trim();
  if (viewerTaskId) return viewerTaskId;

  const task = asRecord(taskInput);
  if (Object.keys(task).length === 0) return '';

  const snapshot = resolveLeadOptSnapshotFromTask(task);
  const snapshotPredictionTaskId = readText(
    snapshot.prediction_task_id || asRecord(snapshot.prediction_summary).latest_task_id
  ).trim();
  if (snapshotPredictionTaskId) return snapshotPredictionTaskId;

  const taskId = readText(task.task_id).trim();
  const structureName = readText(task.structure_name).trim();
  if (taskId && structureName && Object.keys(snapshot).length === 0) return taskId;

  return '';
}

export function sanitizeArchiveNamePart(value: unknown, fallback = 'item'): string {
  const text = readText(value).trim();
  if (!text) return fallback;
  const normalized = text
    .replace(/[\\/:*?"<>|]/g, '_')
    .replace(/\s+/g, '_')
    .replace(/_+/g, '_')
    .replace(/^_+|_+$/g, '');
  if (!normalized) return fallback;
  return normalized.slice(0, 80);
}

export function collectLeadOptDownloadRecords(
  predictionMapInput: unknown,
  preferredBackendInput: unknown
): Array<{ key: string; backend: string; smiles: string; record: LeadOptPredictionRecord }> {
  const predictionMap = asPredictionRecordMap(predictionMapInput);
  const preferredBackend = normalizePredictionBackendStrict(preferredBackendInput);
  const allSuccess = Object.entries(predictionMap)
    .map(([key, record]) => {
      const parsed = parseLeadOptPredictionRecordKey(key);
      const backend = normalizePredictionBackendStrict(record.backend || parsed.backend);
      const smiles = readText(parsed.smiles).trim();
      return { key, backend, smiles, record };
    })
    .filter(({ backend, record }) => {
      const taskId = readText(record.taskId).trim();
      const state = readText(record.state).trim().toUpperCase();
      return Boolean(taskId && !taskId.startsWith('local:') && backend && state === 'SUCCESS');
    });
  if (!preferredBackend) return allSuccess;
  const preferred = allSuccess.filter(({ backend }) => backend === preferredBackend);
  return preferred.length > 0 ? preferred : allSuccess;
}

export async function downloadLeadOptCombinedArchive(params: {
  predictionMap: unknown;
  preferredBackend?: unknown;
  projectName: string;
  queryId?: string;
  fallbackTaskId?: string;
}): Promise<void> {
  const records = collectLeadOptDownloadRecords(params.predictionMap, params.preferredBackend);
  if (records.length === 0) {
    const fallbackTaskId = readText(params.fallbackTaskId).trim();
    if (!fallbackTaskId) {
      throw new Error('No successful lead-opt prediction results are available for download yet.');
    }
    await downloadResultFile(fallbackTaskId);
    return;
  }

  const { default: JSZipLib } = await import('jszip');
  const bundleZip = new JSZipLib();
  const manifest: Array<Record<string, unknown>> = [];
  const sortedRecords = [...records].sort((left, right) => {
    const leftBackend = readText(left.backend).trim();
    const rightBackend = readText(right.backend).trim();
    if (leftBackend !== rightBackend) return leftBackend.localeCompare(rightBackend);
    return left.smiles.localeCompare(right.smiles);
  });

  for (let index = 0; index < sortedRecords.length; index += 1) {
    const item = sortedRecords[index];
    const taskId = readText(item.record.taskId).trim();
    const sourceBlob = await downloadResultBlob(taskId, { mode: 'full' });
    const sourceZip = await JSZipLib.loadAsync(sourceBlob);
    const folderName = [
      String(index + 1).padStart(3, '0'),
      sanitizeArchiveNamePart(item.backend, 'backend'),
      sanitizeArchiveNamePart(item.smiles, 'compound'),
      sanitizeArchiveNamePart(taskId, 'task')
    ].join('_');
    for (const [path, entry] of Object.entries(sourceZip.files)) {
      if (entry.dir) continue;
      bundleZip.file(`${folderName}/${path}`, await entry.async('blob'));
    }
    manifest.push({
      index: index + 1,
      task_id: taskId,
      backend: item.backend,
      smiles: item.smiles,
      structure_name: readText(item.record.structureName).trim(),
      source_folder: folderName,
    });
  }

  bundleZip.file(
    'manifest.json',
    JSON.stringify(
      {
        project_name: readText(params.projectName).trim(),
        query_id: readText(params.queryId).trim(),
        compound_count: sortedRecords.length,
        generated_at: new Date().toISOString(),
        records: manifest,
      },
      null,
      2
    )
  );

  const archiveBlob = await bundleZip.generateAsync({ type: 'blob' });
  const href = URL.createObjectURL(archiveBlob);
  const anchor = document.createElement('a');
  const projectNamePart = sanitizeArchiveNamePart(params.projectName, 'lead_opt');
  const queryIdPart = sanitizeArchiveNamePart(params.queryId, 'query');
  anchor.href = href;
  anchor.download = `${projectNamePart}_${queryIdPart}_lead_opt_results.zip`;
  anchor.click();
  URL.revokeObjectURL(href);
}

export function readLeadOptTaskRowTimestamp(taskInput: unknown): number {
  const task = asRecord(taskInput);
  return new Date(
    readText(task.updated_at || task.completed_at || task.submitted_at || task.created_at).trim()
  ).getTime() || 0;
}

export function readLeadOptSnapshotPriority(taskInput: unknown): number {
  const task = taskInput as any;
  const snapshot = resolveLeadOptSnapshotFromTask(task);
  if (Object.keys(snapshot).length === 0) return -1;
  const summary = readLeadOptTaskSummary(task);
  if (!summary) return 0;
  const queued = Math.max(0, Number(summary.predictionQueued || 0));
  const running = Math.max(0, Number(summary.predictionRunning || 0));
  const success = Math.max(0, Number(summary.predictionSuccess || 0));
  const failure = Math.max(0, Number(summary.predictionFailure || 0));
  const stage = readText(summary.stage).trim().toLowerCase();
  const hasMaterializedQuery = Boolean(
    summary.transformCount !== null ||
    summary.candidateCount !== null ||
    summary.databaseId ||
    summary.databaseLabel ||
    summary.databaseSchema
  );
  if (success > 0 || failure > 0 || hasMaterializedQuery || stage === 'prediction_completed' || stage === 'completed') return 4;
  if (running > 0 || stage === 'prediction_running' || stage === 'running') return 3;
  if (queued > 0 || stage === 'prediction_queued' || stage === 'queued') return 2;
  return 1;
}

export function readLeadOptListPriority(taskInput: unknown): number {
  const task = taskInput as any;
  const snapshot = resolveLeadOptSnapshotFromTask(taskInput);
  if (Object.keys(snapshot).length === 0) return -1;
  const summary = readLeadOptTaskSummary(task);
  const queryResult = asRecord(snapshot.query_result);
  const queryId = readText(snapshot.query_id || queryResult.query_id).trim();
  const candidateCount = Math.max(
    Number(summary?.candidateCount ?? 0) || 0,
    Number(snapshot.candidate_count || 0) || 0,
    Array.isArray(snapshot.enumerated_candidates) ? snapshot.enumerated_candidates.length : 0
  );
  const transformCount = Math.max(
    Number(summary?.transformCount ?? 0) || 0,
    Number(snapshot.transform_count || 0) || 0,
    Array.isArray(queryResult.transforms) ? queryResult.transforms.length : 0
  );
  const bucketCount = Math.max(
    Number(summary?.bucketCount ?? 0) || 0,
    Number(snapshot.bucket_count || 0) || 0,
    Array.isArray(queryResult.clusters) ? queryResult.clusters.length : 0
  );
  if (queryId && candidateCount > 0) return 4;
  if (queryId && (transformCount > 0 || bucketCount > 0)) return 3;
  if (queryId) return 2;
  return 1;
}

export function pickPreferredLeadOptTask(projectTasks: ProjectTask[]): ProjectTask | null {
  let preferred: ProjectTask | null = null;
  for (const row of projectTasks) {
    const snapshot = resolveLeadOptSnapshotFromTask(row);
    if (Object.keys(snapshot).length === 0) continue;
    if (!preferred) {
      preferred = row;
      continue;
    }
    const preferredPriority = readLeadOptListPriority(preferred);
    const candidatePriority = readLeadOptListPriority(row);
    if (candidatePriority > preferredPriority) {
      preferred = row;
      continue;
    }
    if (candidatePriority < preferredPriority) continue;
    if (readLeadOptTaskRowTimestamp(row) > readLeadOptTaskRowTimestamp(preferred)) {
      preferred = row;
    }
  }
  return preferred;
}

export function readLeadOptQueryIdFromSnapshot(snapshotInput: unknown): string {
  const snapshot = asRecord(snapshotInput);
  const queryResult = asRecord(snapshot.query_result);
  return readText(snapshot.query_id || queryResult.query_id).trim();
}

export function buildLeadOptAggregatedSnapshot(params: {
  projectTasks: ProjectTask[];
  requestedTaskRow?: ProjectTask | null;
  preferRequestedQuery?: boolean;
  strictRequestedTaskRow?: boolean;
  preferredListTask?: ProjectTask | null;
  historicalReferenceRecords: Record<string, LeadOptPredictionRecord>;
}): Record<string, unknown> | null {
  const { projectTasks, requestedTaskRow, preferRequestedQuery, strictRequestedTaskRow, preferredListTask, historicalReferenceRecords } = params;
  const requestedSnapshot = resolveLeadOptSnapshotFromTask(requestedTaskRow);
  const requestedTaskRowId = readText((requestedTaskRow as any)?.id).trim();
  const requestedQueryId = readLeadOptQueryIdFromSnapshot(requestedSnapshot);
  const preferredListSnapshot = resolveLeadOptSnapshotFromTask(preferredListTask);
  const preferredListQueryId = readLeadOptQueryIdFromSnapshot(preferredListSnapshot);
  const hasMaterializedLeadOptSnapshot = (snapshotInput: unknown): boolean => {
    const snapshot = asRecord(snapshotInput);
    if (Object.keys(snapshot).length === 0) return false;
    const queryResult = asRecord(snapshot.query_result);
    const queryId = readText(snapshot.query_id || queryResult.query_id).trim();
    if (!queryId) return false;
    if (Array.isArray(snapshot.enumerated_candidates) && snapshot.enumerated_candidates.length > 0) return true;
    if (Object.keys(asPredictionRecordMap(snapshot.prediction_by_smiles)).length > 0) return true;
    if (Number(snapshot.candidate_count || 0) > 0) return true;
    if (Number(snapshot.transform_count || 0) > 0) return true;
    if (Number(snapshot.bucket_count || 0) > 0) return true;
    if (Array.isArray(queryResult.transforms) && queryResult.transforms.length > 0) return true;
    if (Array.isArray(queryResult.clusters) && queryResult.clusters.length > 0) return true;
    if (Number(queryResult.count || 0) > 0) return true;
    if (Number(queryResult.global_count || 0) > 0) return true;
    return false;
  };
  let anchorQueryId = preferredListQueryId || requestedQueryId;
  if (requestedQueryId) {
    const requestedRows = projectTasks.filter((row) => {
      const snapshot = resolveLeadOptSnapshotFromTask(row);
      return readLeadOptQueryIdFromSnapshot(snapshot) === requestedQueryId;
    });
    const requestedHasMaterialized = requestedRows.some((row) =>
      hasMaterializedLeadOptSnapshot(resolveLeadOptSnapshotFromTask(row))
    );
    if (preferRequestedQuery || requestedHasMaterialized || !preferredListQueryId) {
      anchorQueryId = requestedQueryId;
    }
  }
  let relevantRows: ProjectTask[] = [];
  if (strictRequestedTaskRow && requestedTaskRowId) {
    relevantRows = projectTasks.filter((row) => {
      if (readText((row as any)?.id).trim() !== requestedTaskRowId) return false;
      const snapshot = resolveLeadOptSnapshotFromTask(row);
      return Object.keys(snapshot).length > 0;
    });
    if (relevantRows.length === 0 && requestedTaskRow) {
      if (Object.keys(requestedSnapshot).length > 0) {
        relevantRows = [requestedTaskRow];
      }
    }
    if (requestedQueryId) {
      anchorQueryId = requestedQueryId;
    }
  } else {
    relevantRows = projectTasks.filter((row) => {
      const snapshot = resolveLeadOptSnapshotFromTask(row);
      if (Object.keys(snapshot).length === 0) return false;
      if (!anchorQueryId) return true;
      return readLeadOptQueryIdFromSnapshot(snapshot) === anchorQueryId;
    });
  }
  if (relevantRows.length === 0) return null;

  let listSource: ProjectTask | null = null;
  let stateSource: ProjectTask | null = null;
  let mergedPredictions: Record<string, LeadOptPredictionRecord> = {};
  let mergedReferencePredictions: Record<string, LeadOptPredictionRecord> = {};
  const mergedEnumeratedBySmiles: Record<string, Record<string, unknown>> = {};
  for (const row of relevantRows) {
    const snapshot = resolveLeadOptSnapshotFromTask(row);
    mergedPredictions = mergePredictionRecordMaps(mergedPredictions, snapshot.prediction_by_smiles);
    mergedReferencePredictions = mergePredictionRecordMaps(mergedReferencePredictions, snapshot.reference_prediction_by_backend);
    const enumerated = Array.isArray(snapshot.enumerated_candidates) ? snapshot.enumerated_candidates : [];
    for (const candidateRaw of enumerated) {
      const candidate = asRecord(candidateRaw);
      const smiles = readText(candidate.smiles).trim();
      if (!smiles) continue;
      const existing = mergedEnumeratedBySmiles[smiles];
      if (!existing) {
        mergedEnumeratedBySmiles[smiles] = candidate;
        continue;
      }
      const existingScore = Object.keys(existing).length;
      const candidateScore = Object.keys(candidate).length;
      if (candidateScore >= existingScore) {
        mergedEnumeratedBySmiles[smiles] = candidate;
      }
    }
    if (!listSource) {
      listSource = row;
    } else {
      const currentPriority = readLeadOptListPriority(listSource);
      const nextPriority = readLeadOptListPriority(row);
      if (nextPriority > currentPriority || (nextPriority === currentPriority && readLeadOptTaskRowTimestamp(row) > readLeadOptTaskRowTimestamp(listSource))) {
        listSource = row;
      }
    }
    if (!stateSource) {
      stateSource = row;
    } else {
      const currentPriority = readLeadOptSnapshotPriority(stateSource);
      const nextPriority = readLeadOptSnapshotPriority(row);
      if (nextPriority > currentPriority || (nextPriority === currentPriority && readLeadOptTaskRowTimestamp(row) > readLeadOptTaskRowTimestamp(stateSource))) {
        stateSource = row;
      }
    }
  }
  const listSnapshot = resolveLeadOptSnapshotFromTask(listSource);
  const stateSnapshot = resolveLeadOptSnapshotFromTask(stateSource);
  const mergedEnumeratedCandidates = Object.values(mergedEnumeratedBySmiles);
  const mergedSummary = summarizeLeadOptPredictions(mergedPredictions);
  const basePredictionSummary = asRecord(stateSnapshot.prediction_summary);
  const baseStage = readText(stateSnapshot.stage || stateSnapshot.prediction_stage || listSnapshot.stage || listSnapshot.prediction_stage).trim().toLowerCase();
  const derivedStage =
    mergedSummary.running > 0
      ? 'prediction_running'
      : mergedSummary.queued > 0
        ? 'prediction_queued'
        : mergedSummary.failure > 0 && mergedSummary.success === 0 && mergedSummary.total > 0
          ? 'prediction_failed'
          : mergedSummary.total > 0
            ? 'prediction_completed'
            : baseStage;
  const derivedPredictionStage =
    mergedSummary.running > 0
      ? 'running'
      : mergedSummary.queued > 0
        ? 'queued'
        : mergedSummary.total > 0
          ? 'completed'
          : readText(stateSnapshot.prediction_stage || listSnapshot.prediction_stage).trim();

  return {
    ...listSnapshot,
    ...stateSnapshot,
    query_id: anchorQueryId || readLeadOptQueryIdFromSnapshot(stateSnapshot) || readLeadOptQueryIdFromSnapshot(listSnapshot),
    task_id: readText(
      stateSnapshot.task_id ||
        asRecord(stateSnapshot.query_result).task_id ||
        listSnapshot.task_id ||
        asRecord(listSnapshot.query_result).task_id
    ).trim(),
    query_result:
      Object.keys(asRecord(listSnapshot.query_result)).length > 0
        ? asRecord(listSnapshot.query_result)
        : asRecord(stateSnapshot.query_result),
    enumerated_candidates:
      mergedEnumeratedCandidates.length > 0
        ? mergedEnumeratedCandidates
        : Array.isArray(listSnapshot.enumerated_candidates) && listSnapshot.enumerated_candidates.length > 0
          ? listSnapshot.enumerated_candidates
          : Array.isArray(stateSnapshot.enumerated_candidates)
            ? stateSnapshot.enumerated_candidates
            : [],
    selection:
      Object.keys(asRecord(listSnapshot.selection)).length > 0
        ? asRecord(listSnapshot.selection)
        : asRecord(stateSnapshot.selection),
    ui_state: {},
    query_payload:
      Object.keys(asRecord(listSnapshot.query_payload)).length > 0
        ? asRecord(listSnapshot.query_payload)
        : asRecord(stateSnapshot.query_payload),
    query_cache_state: readText(stateSnapshot.query_cache_state || listSnapshot.query_cache_state).trim().toLowerCase(),
    stage: derivedStage,
    prediction_stage: derivedPredictionStage,
    prediction_summary: {
      ...basePredictionSummary,
      total: Math.max(mergedSummary.total, Number(toFiniteNumber(basePredictionSummary.total) || 0)),
      queued: mergedSummary.queued,
      running: mergedSummary.running,
      success: mergedSummary.success,
      failure: mergedSummary.failure,
      latest_task_id: readText(basePredictionSummary.latest_task_id).trim()
    },
    prediction_by_smiles: mergedPredictions,
    reference_prediction_by_backend: hydratePredictionRecordMapFromHistory(
      mergedReferencePredictions,
      historicalReferenceRecords
    ),
    target_chain: readText(stateSnapshot.target_chain || listSnapshot.target_chain).trim(),
    ligand_chain: readText(stateSnapshot.ligand_chain || listSnapshot.ligand_chain).trim()
  };
}

export function buildLeadOptSelectionFromPayload(payload: Record<string, unknown>, context: {
  querySmiles: string;
  targetChain: string;
  ligandChain: string;
}) {
  const variableSpec = asRecord(payload.variable_spec);
  const variableItems = asRecordArray(variableSpec.items).map((item) => {
    const atomIndices = Array.isArray(item.atom_indices)
      ? item.atom_indices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
      : [];
    return {
      query: readText(item.query).trim(),
      mode: readText(item.mode).trim() || 'substructure',
      fragment_id: readText(item.fragment_id).trim(),
      atom_indices: atomIndices
    };
  });
  const selectedFragmentIdsFromPayload = Array.isArray(payload.selected_fragment_ids)
    ? payload.selected_fragment_ids
        .map((value) => readText(value).trim())
        .filter(Boolean)
    : [];
  const selectedFragmentAtomIndicesFromPayload = Array.isArray(payload.selected_fragment_atom_indices)
    ? payload.selected_fragment_atom_indices
        .map((value) => Number(value))
        .filter((value) => Number.isFinite(value) && value >= 0)
        .map((value) => Math.floor(value))
    : [];
  const selectedFragmentIds = Array.from(
    new Set(
      selectedFragmentIdsFromPayload.length > 0
        ? selectedFragmentIdsFromPayload
        : variableItems.map((item) => readText(item.fragment_id).trim()).filter(Boolean)
    )
  );
  const selectedFragmentAtomIndices = Array.from(
    new Set(
      selectedFragmentAtomIndicesFromPayload.length > 0
        ? selectedFragmentAtomIndicesFromPayload
        : variableItems.flatMap((item) => item.atom_indices || [])
    )
  );
  const variableQueries = Array.from(
    new Set(variableItems.map((item) => readText(item.query).trim()).filter(Boolean))
  );
  const groupedByEnvironmentValue = readBooleanToken(payload.grouped_by_environment);
  const groupedByEnvironmentMode =
    groupedByEnvironmentValue === true ? 'on' : groupedByEnvironmentValue === false ? 'off' : 'auto';
  const propertyTargets = asRecord(payload.property_targets);
  const queryProperty = readText(propertyTargets.property).trim();
  const directionToken = readText(propertyTargets.direction).trim().toLowerCase();
  const direction = directionToken === 'increase' || directionToken === 'decrease' ? directionToken : '';
  const minPairsRaw = Number(payload.min_pairs);
  const minPairs = Number.isFinite(minPairsRaw) ? Math.max(1, Math.floor(minPairsRaw)) : 1;
  const envRadiusRaw = Number(payload.rule_env_radius);
  const envRadius = Number.isFinite(envRadiusRaw) ? Math.max(0, Math.floor(envRadiusRaw)) : 1;
  return {
    query_smiles: readText(context.querySmiles).trim(),
    target_chain: readText(context.targetChain).trim(),
    ligand_chain: readText(context.ligandChain).trim(),
    selected_fragment_ids: selectedFragmentIds,
    selected_fragment_atom_indices: selectedFragmentAtomIndices,
    variable_queries: variableQueries,
    variable_items: variableItems,
    grouped_by_environment_mode: groupedByEnvironmentMode,
    query_property: queryProperty,
    direction,
    min_pairs: minPairs,
    env_radius: envRadius
  };
}
