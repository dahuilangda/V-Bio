import type { InputComponent, Project, ProjectTask } from '../../types/models';
import { assignChainIdsForComponents } from '../../utils/chainAssignments';
import { readPeptidePreviewFromProperties } from '../../utils/peptideTaskPreview';
import { normalizeComponentSequence, normalizeInputComponents } from '../../utils/projectInputs';
import type {
  SeedFilterOption,
  SortDirection,
  SortKey,
  StructureSearchMode,
  SubmittedWithinDaysOption,
  TaskConfidenceMetrics,
  TaskMetricContext,
  TaskSelectionContext,
  TaskWorkspaceView,
  WorkspacePairPreference
} from './taskListTypes';

function normalizeTaskComponents(components: InputComponent[]): InputComponent[] {
  return normalizeInputComponents(components);
}

const AFFINITY_TARGET_UPLOAD_COMPONENT_ID = '__affinity_target_upload__';
const AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = '__affinity_ligand_upload__';
const LEADOPT_TARGET_UPLOAD_COMPONENT_ID = '__leadopt_target_upload__';
const LEADOPT_LIGAND_UPLOAD_COMPONENT_ID = '__leadopt_ligand_upload__';

type TaskLigandSourceWorkflow = 'prediction' | 'peptide_design' | 'affinity' | 'lead_optimization' | 'auto';

function normalizeTaskLigandSourceWorkflow(value: string | null | undefined): TaskLigandSourceWorkflow {
  const normalized = String(value || '')
    .trim()
    .toLowerCase();
  if (normalized === 'prediction' || normalized === 'peptide_design' || normalized === 'affinity' || normalized === 'lead_optimization') {
    return normalized;
  }
  return 'auto';
}

function resolveAffinityUploadRole(component: Record<string, unknown>): 'target' | 'ligand' | null {
  const id = typeof component.id === 'string' ? component.id.trim() : '';
  if (id === AFFINITY_TARGET_UPLOAD_COMPONENT_ID || id === AFFINITY_LIGAND_UPLOAD_COMPONENT_ID) {
    return id === AFFINITY_TARGET_UPLOAD_COMPONENT_ID ? 'target' : 'ligand';
  }
  const uploadMeta =
    component.affinityUpload && typeof component.affinityUpload === 'object'
      ? (component.affinityUpload as Record<string, unknown>)
      : component.affinity_upload && typeof component.affinity_upload === 'object'
        ? (component.affinity_upload as Record<string, unknown>)
        : null;
  const role = typeof uploadMeta?.role === 'string' ? uploadMeta.role.trim().toLowerCase() : '';
  if (role === 'target' || role === 'ligand') return role;
  return null;
}

function resolveLeadOptUploadRole(component: Record<string, unknown>): 'target' | 'ligand' | null {
  const id = typeof component.id === 'string' ? component.id.trim() : '';
  if (id === LEADOPT_TARGET_UPLOAD_COMPONENT_ID || id === LEADOPT_LIGAND_UPLOAD_COMPONENT_ID) {
    return id === LEADOPT_TARGET_UPLOAD_COMPONENT_ID ? 'target' : 'ligand';
  }
  const uploadMeta =
    component.leadOptUpload && typeof component.leadOptUpload === 'object'
      ? (component.leadOptUpload as Record<string, unknown>)
      : component.lead_opt_upload && typeof component.lead_opt_upload === 'object'
        ? (component.lead_opt_upload as Record<string, unknown>)
        : null;
  const role = typeof uploadMeta?.role === 'string' ? uploadMeta.role.trim().toLowerCase() : '';
  if (role === 'target' || role === 'ligand') return role;
  return null;
}

function normalizeTaskRawComponent(component: Record<string, unknown>): Record<string, unknown> | null {
  const affinityUploadRole = resolveAffinityUploadRole(component);
  if (affinityUploadRole === 'target') return null;
  if (affinityUploadRole === 'ligand') {
    const sequence = normalizeComponentSequence('ligand', typeof component.sequence === 'string' ? component.sequence : '');
    if (!sequence) return null;
    return {
      ...component,
      type: 'ligand',
      inputMethod: 'jsme',
      sequence,
      affinityUpload: undefined,
      affinity_upload: undefined
    };
  }

  const leadOptUploadRole = resolveLeadOptUploadRole(component);
  if (leadOptUploadRole === 'target') return null;
  if (leadOptUploadRole === 'ligand') {
    const sequence = normalizeComponentSequence('ligand', typeof component.sequence === 'string' ? component.sequence : '');
    if (!sequence) return null;
    return {
      ...component,
      type: 'ligand',
      inputMethod: 'jsme',
      sequence,
      leadOptUpload: undefined,
      lead_opt_upload: undefined
    };
  }
  return component;
}

function readTaskComponents(task: ProjectTask): InputComponent[] {
  const rawComponents = Array.isArray(task.components)
    ? (task.components as unknown[])
        .filter((component): component is Record<string, unknown> => Boolean(component && typeof component === 'object'))
        .map((component) => normalizeTaskRawComponent(component))
        .filter((component): component is Record<string, unknown> => component !== null)
    : [];
  const components = rawComponents.length > 0 ? normalizeTaskComponents(rawComponents as unknown as InputComponent[]) : [];
  return components;
}

function readTaskPrimaryLigand(
  components: InputComponent[],
  preferredComponentId: string | null,
  preferredLigandChainId: string | null = null,
  strictPreferredLigand = false,
  workflow: TaskLigandSourceWorkflow = 'auto'
): { smiles: string; isSmiles: boolean } {
  const normalizedWorkflow = normalizeTaskLigandSourceWorkflow(workflow);

  // Respect explicit ligand component selection first (e.g. binding ligand = Comp2).
  // Ligand View must reflect the exact user-selected component.
  if (preferredComponentId) {
    const selected = components.find((item) => item.id === preferredComponentId);
    const selectedValue =
      selected && selected.type === 'ligand' ? normalizeComponentSequence('ligand', selected.sequence) : '';
    if (selected && selected.type === 'ligand' && selectedValue) {
      return {
        smiles: selectedValue,
        isSmiles: selected.inputMethod !== 'ccd'
      };
    }
  }

  if (normalizedWorkflow === 'affinity' || normalizedWorkflow === 'lead_optimization') {
    const uploadedLigand = components.find((item) => {
      if (item.type !== 'ligand') return false;
      if (!normalizeComponentSequence('ligand', item.sequence)) return false;
      const raw = item as unknown as Record<string, unknown>;
      if (normalizedWorkflow === 'affinity') {
        return resolveAffinityUploadRole(raw) === 'ligand';
      }
      return resolveLeadOptUploadRole(raw) === 'ligand';
    });
    if (uploadedLigand) {
      const value = normalizeComponentSequence('ligand', uploadedLigand.sequence);
      return {
        smiles: value,
        isSmiles: uploadedLigand.inputMethod !== 'ccd'
      };
    }
    return {
      smiles: '',
      isSmiles: false
    };
  }

  if (strictPreferredLigand && preferredLigandChainId) {
    // Strict mode means the binding ligand must resolve from component selection.
    return {
      smiles: '',
      isSmiles: false
    };
  }

  return {
    smiles: '',
    isSmiles: false
  };
}

function sortProjectTasks(rows: ProjectTask[]): ProjectTask[] {
  return [...rows].sort((a, b) => {
    const at = new Date(a.submitted_at || a.created_at).getTime();
    const bt = new Date(b.submitted_at || b.created_at).getTime();
    return bt - at;
  });
}

function isProjectTaskRow(value: ProjectTask | null | undefined): value is ProjectTask {
  return Boolean(value && typeof value === 'object' && typeof value.id === 'string' && value.id.trim());
}

function sanitizeTaskRows(rows: Array<ProjectTask | null | undefined>): ProjectTask[] {
  return rows.filter((row): row is ProjectTask => isProjectTaskRow(row));
}

function isProjectRow(value: Project | null | undefined): value is Project {
  return Boolean(value && typeof value === 'object' && typeof value.id === 'string' && value.id.trim());
}

function readObjectPath(data: Record<string, unknown>, path: string): unknown {
  let current: unknown = data;
  for (const key of path.split('.')) {
    if (!current || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[key];
  }
  return current;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function readFirstFiniteMetric(data: Record<string, unknown>, paths: string[]): number | null {
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function readFirstNonEmptyStringMetric(data: Record<string, unknown> | null, paths: string[]): string {
  if (!data) return '';
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'string' && value.trim()) {
      return value.trim();
    }
  }
  return '';
}

function readStringListMetric(data: Record<string, unknown> | null, paths: string[]): string[] {
  if (!data) return [];
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (!Array.isArray(value)) continue;
    const rows = value
      .filter((item): item is string => typeof item === 'string')
      .map((item) => item.trim())
      .filter(Boolean);
    if (rows.length > 0) return rows;
  }
  return [];
}

function readLigandSmilesFromMap(data: Record<string, unknown> | null, preferredLigandChainId: string | null): string {
  if (!data) return '';
  const mapCandidates: unknown[] = [
    readObjectPath(data, 'ligand_smiles_map'),
    readObjectPath(data, 'ligand.smiles_map'),
    readObjectPath(data, 'ligand.smilesMap')
  ];
  const preferredChain = normalizeChainKey(String(preferredLigandChainId || ''));

  for (const mapValue of mapCandidates) {
    if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) continue;
    const entries = Object.entries(mapValue as Record<string, unknown>)
      .map(([key, value]) => {
        if (typeof value !== 'string') return null;
        const normalizedValue = normalizeComponentSequence('ligand', value);
        if (!normalizedValue) return null;
        const keyText = String(key || '').trim();
        return {
          key: keyText,
          keyChain: normalizeChainKey(keyText.includes(':') ? keyText.slice(0, keyText.indexOf(':')) : keyText),
          value: normalizedValue
        };
      })
      .filter((item): item is { key: string; keyChain: string; value: string } => item !== null);
    if (entries.length === 0) continue;

    if (preferredChain) {
      const matched = entries.find((item) => item.keyChain === preferredChain);
      if (matched) return matched.value;
    }

    if (entries.length === 1) return entries[0].value;
    return entries[0].value;
  }

  return '';
}

function readTaskLigandSmilesHint(task: ProjectTask, preferredLigandChainId: string | null = null): string {
  const taskLigandSmiles = normalizeComponentSequence('ligand', String(task.ligand_smiles || ''));
  if (taskLigandSmiles) return taskLigandSmiles;

  const confidenceData =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const affinityData =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : null;

  const directConfidenceSmiles = normalizeComponentSequence(
    'ligand',
    readFirstNonEmptyStringMetric(confidenceData, [
      'ligand_smiles',
      'ligand.smiles',
      'ligandSmiles',
      'request.ligand_smiles',
      'inputs.ligand_smiles'
    ])
  );
  if (directConfidenceSmiles) return directConfidenceSmiles;

  const mappedConfidenceSmiles = normalizeComponentSequence(
    'ligand',
    readLigandSmilesFromMap(confidenceData, preferredLigandChainId)
  );
  if (mappedConfidenceSmiles) return mappedConfidenceSmiles;

  const directAffinitySmiles = normalizeComponentSequence(
    'ligand',
    readFirstNonEmptyStringMetric(affinityData, [
      'ligand_smiles',
      'ligand.smiles',
      'ligandSmiles',
      'request.ligand_smiles',
      'inputs.ligand_smiles'
    ])
  );
  if (directAffinitySmiles) return directAffinitySmiles;

  return normalizeComponentSequence('ligand', readLigandSmilesFromMap(affinityData, preferredLigandChainId));
}

function splitChainTokens(value: string): string[] {
  return String(value || '')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function isLikelyChainToken(value: string): boolean {
  const raw = String(value || '').trim();
  if (!raw) return false;
  if (raw.length > 12) return false;
  return /^[A-Za-z0-9._:-]+$/.test(raw);
}

function collectCoverageChainBuckets(confidence: Record<string, unknown> | null): {
  all: string[];
  ligand: string[];
  polymer: string[];
} {
  const all: string[] = [];
  const ligand: string[] = [];
  const polymer: string[] = [];
  if (!confidence) {
    return { all, ligand, polymer };
  }
  const pushUnique = (bucket: string[], chainIdRaw: unknown) => {
    const chainId = String(chainIdRaw || '').trim();
    if (!chainId) return;
    if (!bucket.some((item) => chainKeysMatch(item, chainId) || chainKeysMatch(chainId, item))) {
      bucket.push(chainId);
    }
  };
  const pushAll = (chainIdRaw: unknown) => pushUnique(all, chainIdRaw);

  const ligandCoverage = confidence.ligand_atom_coverage;
  if (Array.isArray(ligandCoverage)) {
    for (const row of ligandCoverage) {
      if (!row || typeof row !== 'object') continue;
      const chainId = (row as Record<string, unknown>).chain;
      pushUnique(ligand, chainId);
      pushAll(chainId);
    }
  }

  const chainCoverage = confidence.chain_atom_coverage;
  if (Array.isArray(chainCoverage)) {
    for (const row of chainCoverage) {
      if (!row || typeof row !== 'object') continue;
      const entry = row as Record<string, unknown>;
      const chainId = entry.chain;
      pushAll(chainId);
      const molType = String(entry.mol_type || '').trim().toLowerCase();
      if (molType.includes('nonpolymer') || molType.includes('ligand')) {
        pushUnique(ligand, chainId);
      } else if (molType.includes('protein') || molType.includes('dna') || molType.includes('rna') || molType.includes('polymer')) {
        pushUnique(polymer, chainId);
      }
    }
  }

  return { all, ligand, polymer };
}

function resolveChainFromPool(candidate: string, pool: string[]): string | null {
  const tokens = splitChainTokens(candidate);
  if (tokens.length === 0) return null;
  for (const token of tokens) {
    if (!isLikelyChainToken(token)) continue;
    const matched = pool.find((chainId) => chainKeysMatch(chainId, token) || chainKeysMatch(token, chainId));
    if (matched) return matched;
  }
  return null;
}

function normalizeProbability(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

const TASKS_PAGE_FILTERS_STORAGE_KEY = 'vbio:tasks-page-filters:v1';
const TASK_SORT_KEYS: SortKey[] = ['plddt', 'iptm', 'pae', 'submitted', 'backend', 'seed', 'duration'];
const TASK_SORT_DIRECTIONS: SortDirection[] = ['asc', 'desc'];
const TASK_SUBMITTED_WINDOW_OPTIONS: SubmittedWithinDaysOption[] = ['all', '1', '7', '30', '90'];
const TASK_SEED_FILTER_OPTIONS: SeedFilterOption[] = ['all', 'with_seed', 'without_seed'];
const TASK_STRUCTURE_SEARCH_MODES: StructureSearchMode[] = ['exact', 'substructure'];
const TASK_PAGE_SIZE_OPTIONS = [8, 12, 20, 50];

function normalizeTaskWorkspaceView(value: string | null): TaskWorkspaceView {
  return value === 'api' ? 'api' : 'tasks';
}

function isSequenceLigandType(type: InputComponent['type'] | null): boolean {
  return type === 'protein' || type === 'dna' || type === 'rna';
}

function clampPlddtValue(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value >= 0 && value <= 1) return Math.max(0, Math.min(100, value * 100));
  return Math.max(0, Math.min(100, value));
}

function alignConfidenceSeriesToLength(
  values: number[] | null,
  sequenceLength: number,
  fallbackValue: number | null
): number[] | null {
  if (sequenceLength <= 0) return null;
  const series = (values || []).filter((value) => Number.isFinite(value)).map((value) => clampPlddtValue(value));
  if (series.length === 0) {
    if (fallbackValue === null || !Number.isFinite(fallbackValue)) return null;
    const normalizedFallback = clampPlddtValue(fallbackValue);
    return Array.from({ length: sequenceLength }, () => normalizedFallback);
  }

  if (series.length === sequenceLength) return series;

  if (series.length > sequenceLength) {
    const reduced: number[] = [];
    for (let i = 0; i < sequenceLength; i += 1) {
      const start = Math.floor((i * series.length) / sequenceLength);
      const end = Math.max(start + 1, Math.floor(((i + 1) * series.length) / sequenceLength));
      const chunk = series.slice(start, end);
      const avg = chunk.reduce((sum, value) => sum + value, 0) / chunk.length;
      reduced.push(clampPlddtValue(avg));
    }
    return reduced;
  }

  // Treat very short series as under-specified summaries (e.g. mean-only) rather than
  // broadcasting one/few values across all residues.
  if (series.length < Math.min(sequenceLength, 4)) {
    return null;
  }

  const expanded: number[] = [];
  for (let i = 0; i < sequenceLength; i += 1) {
    const mapped = Math.floor((i * series.length) / sequenceLength);
    expanded.push(series[Math.min(series.length - 1, Math.max(0, mapped))]);
  }
  return expanded;
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}

function isNumericToken(value: string): boolean {
  return /^\d+$/.test(String(value || '').trim());
}

function readPairValueFromNestedMap(
  mapValue: unknown,
  chainA: string,
  chainB: string
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const rowA =
    byChain[chainA] && typeof byChain[chainA] === 'object' && !Array.isArray(byChain[chainA])
      ? (byChain[chainA] as Record<string, unknown>)
      : (() => {
          for (const [key, value] of Object.entries(byChain)) {
            if (!chainKeysMatch(key, chainA) && !chainKeysMatch(chainA, key)) continue;
            if (value && typeof value === 'object' && !Array.isArray(value)) {
              return value as Record<string, unknown>;
            }
          }
          return null;
        })();
  if (!rowA) return null;
  const directValue = rowA[chainB];
  if (directValue !== undefined) {
    return normalizeProbability(toFiniteNumber(directValue));
  }
  for (const [key, value] of Object.entries(rowA)) {
    if (!chainKeysMatch(key, chainB) && !chainKeysMatch(chainB, key)) continue;
    return normalizeProbability(toFiniteNumber(value));
  }
  return null;
}

function readPairValueFromNumericMap(
  mapValue: unknown,
  chainA: string,
  chainB: string,
  chainOrderHints: string[],
  preferredDirectionalIptm: number | null
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length === 0 || !keys.every((item) => isNumericToken(item))) return null;

  const idxA = chainOrderHints.findIndex(
    (hint) => chainKeysMatch(hint, chainA) || chainKeysMatch(chainA, hint)
  );
  const idxB = chainOrderHints.findIndex(
    (hint) => chainKeysMatch(hint, chainB) || chainKeysMatch(chainB, hint)
  );
  if (idxA >= 0 && idxB >= 0 && idxA !== idxB) {
    const ligandToTarget = readPairValueFromNestedMap(byChain, String(idxB), String(idxA));
    const targetToLigand = readPairValueFromNestedMap(byChain, String(idxA), String(idxB));
    if (ligandToTarget !== null && targetToLigand !== null && preferredDirectionalIptm !== null) {
      const ligandDelta = Math.abs(ligandToTarget - preferredDirectionalIptm);
      const targetDelta = Math.abs(targetToLigand - preferredDirectionalIptm);
      return ligandDelta <= targetDelta ? ligandToTarget : targetToLigand;
    }
    if (ligandToTarget !== null) return ligandToTarget;
    if (targetToLigand !== null) return targetToLigand;
  }

  if (keys.length === 2 && preferredDirectionalIptm !== null) {
    const [first, second] = keys.sort((a, b) => Number(a) - Number(b));
    const forward = readPairValueFromNestedMap(byChain, first, second);
    const backward = readPairValueFromNestedMap(byChain, second, first);
    if (forward !== null && backward !== null) {
      const forwardDelta = Math.abs(forward - preferredDirectionalIptm);
      const backwardDelta = Math.abs(backward - preferredDirectionalIptm);
      return forwardDelta <= backwardDelta ? forward : backward;
    }
    if (forward !== null) return forward;
    if (backward !== null) return backward;
  }
  return null;
}

function readPairValueFromAnyTwoKeyMap(
  mapValue: unknown,
  preferredDirectionalIptm: number | null
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  if (preferredDirectionalIptm === null) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length !== 2) return null;
  const [first, second] = keys;
  const forward = readPairValueFromNestedMap(byChain, first, second);
  const backward = readPairValueFromNestedMap(byChain, second, first);
  if (forward === null && backward === null) return null;
  if (forward !== null && backward !== null) {
    const forwardDelta = Math.abs(forward - preferredDirectionalIptm);
    const backwardDelta = Math.abs(backward - preferredDirectionalIptm);
    return forwardDelta <= backwardDelta ? forward : backward;
  }
  return forward ?? backward;
}

function readPairIptmForChains(
  confidence: Record<string, unknown>,
  chainA: string | null,
  chainB: string | null,
  fallbackChainIds: string[]
): number | null {
  if (!chainA || !chainB) return null;
  const sameChain = chainKeysMatch(chainA, chainB);

  const chainIdsRaw = readObjectPath(confidence, 'chain_ids');
  const chainIds =
    Array.isArray(chainIdsRaw) && chainIdsRaw.every((item) => typeof item === 'string')
      ? (chainIdsRaw as string[])
      : fallbackChainIds;
  const preferredDirectionalIptm = normalizeProbability(
    readFirstFiniteMetric(confidence, ['ligand_iptm', 'iptm'])
  );

  const pairMap = readObjectPath(confidence, 'pair_chains_iptm');
  if (!sameChain) {
    // Drug-design view: prefer ligand->target directional ipTM when available.
    const ligandToTarget = readPairValueFromNestedMap(pairMap, chainB, chainA);
    if (ligandToTarget !== null) return ligandToTarget;
    const targetToLigand = readPairValueFromNestedMap(pairMap, chainA, chainB);
    if (targetToLigand !== null) return targetToLigand;
  }
  const numericMapped = readPairValueFromNumericMap(
    pairMap,
    chainA,
    chainB,
    chainIds,
    preferredDirectionalIptm
  );
  if (numericMapped !== null) return numericMapped;
  const twoKeyMapped = readPairValueFromAnyTwoKeyMap(pairMap, preferredDirectionalIptm);
  if (twoKeyMapped !== null) return twoKeyMapped;

  const pairMatrixRaw =
    readObjectPath(confidence, 'chain_pair_iptm') ??
    readObjectPath(confidence, 'chain_pair_iptm_global');
  if (!Array.isArray(pairMatrixRaw)) return null;
  const pairMatrix = pairMatrixRaw;
  const i = chainIds.findIndex((item) => chainKeysMatch(item, chainA) || chainKeysMatch(chainA, item));
  const j = chainIds.findIndex((item) => chainKeysMatch(item, chainB) || chainKeysMatch(chainB, item));
  if (i >= 0 && j >= 0 && i !== j) {
    const rowI = pairMatrix[i];
    const rowJ = pairMatrix[j];
    const ligandToTarget = Array.isArray(rowJ) ? normalizeProbability(toFiniteNumber(rowJ[i])) : null;
    const targetToLigand = Array.isArray(rowI) ? normalizeProbability(toFiniteNumber(rowI[j])) : null;
    if (ligandToTarget !== null && targetToLigand !== null && preferredDirectionalIptm !== null) {
      const ligandDelta = Math.abs(ligandToTarget - preferredDirectionalIptm);
      const targetDelta = Math.abs(targetToLigand - preferredDirectionalIptm);
      return ligandDelta <= targetDelta ? ligandToTarget : targetToLigand;
    }
    if (ligandToTarget !== null) return ligandToTarget;
    if (targetToLigand !== null) return targetToLigand;
  }
  return null;
}

function readChainMeanPlddtForChain(confidence: Record<string, unknown>, chainId: string | null): number | null {
  if (!chainId) return null;
  const map = readObjectPath(confidence, 'chain_mean_plddt');
  if (!map || typeof map !== 'object' || Array.isArray(map)) return null;
  const value = toFiniteNumber((map as Record<string, unknown>)[chainId]);
  if (value === null) return null;
  return value >= 0 && value <= 1 ? value * 100 : value;
}

function resolveTaskSelectionContext(
  task: ProjectTask,
  workspacePreference?: WorkspacePairPreference,
  workflowHint: TaskLigandSourceWorkflow | string = 'auto'
): TaskSelectionContext {
  const workflow = normalizeTaskLigandSourceWorkflow(workflowHint);
  const preferTaskSmilesLigand = workflow === 'affinity' || workflow === 'lead_optimization';
  const taskLigandSmiles = readTaskLigandSmilesHint(task, workspacePreference?.ligandChainId || null);
  const useBindingPreference = workflow === 'prediction' || workflow === 'peptide_design' || workflow === 'auto';
  const taskProperties = task.properties && typeof task.properties === 'object' ? task.properties : null;
  const rawTarget = taskProperties && typeof taskProperties.target === 'string' ? taskProperties.target.trim() : '';
  const rawLigand = taskProperties && typeof taskProperties.ligand === 'string' ? taskProperties.ligand.trim() : '';
  const rawBinder = taskProperties && typeof taskProperties.binder === 'string' ? taskProperties.binder.trim() : '';
  const affinityData =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : null;
  const affinityTargetHint = readFirstNonEmptyStringMetric(affinityData, [
    'requested_target_chain',
    'target_chain',
    'binder_chain'
  ]);
  const confidenceData =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const confidenceTargetHint = readFirstNonEmptyStringMetric(confidenceData, [
    'requested_target_chain_id',
    'target_chain_id',
    'target_chain',
    'protein_chain_id'
  ]);
  const confidenceLigandHint = readFirstNonEmptyStringMetric(confidenceData, [
    'requested_ligand_chain_id',
    'ligand_chain_id',
    'model_ligand_chain_id',
    'binder_chain_id'
  ]);
  const confidenceChainIds = readStringListMetric(confidenceData, ['chain_ids']);
  const coverageChains = collectCoverageChainBuckets(confidenceData);
  const preferredTarget = String(workspacePreference?.targetChainId || '')
    .trim();
  const targetCandidate = rawTarget || affinityTargetHint || confidenceTargetHint || preferredTarget;
  const activeComponents = readTaskComponents(task).filter((item) => Boolean(item.sequence.trim()));
  const ligandComponentCount = activeComponents.filter((item) => item.type === 'ligand').length;

  if (activeComponents.length === 0) {
    const fallbackChainIds = Array.from(
      new Set([
        ...splitChainTokens(targetCandidate).filter((item) => isLikelyChainToken(item)),
        ...confidenceChainIds.filter((item) => isLikelyChainToken(item)),
        ...coverageChains.all.filter((item) => isLikelyChainToken(item))
      ])
    );
    const resolvedLigandChainId =
      resolveChainFromPool(confidenceLigandHint, fallbackChainIds) ||
      coverageChains.ligand[0] ||
      null;
    let resolvedTargetChainId =
      resolveChainFromPool(targetCandidate, fallbackChainIds) ||
      resolveChainFromPool(confidenceTargetHint, fallbackChainIds) ||
      coverageChains.polymer.find((chainId) => !resolvedLigandChainId || !chainKeysMatch(chainId, resolvedLigandChainId)) ||
      fallbackChainIds.find((chainId) => !resolvedLigandChainId || !chainKeysMatch(chainId, resolvedLigandChainId)) ||
      null;
    if (
      resolvedTargetChainId &&
      resolvedLigandChainId &&
      chainKeysMatch(resolvedTargetChainId, resolvedLigandChainId)
    ) {
      resolvedTargetChainId =
        coverageChains.polymer.find((chainId) => !chainKeysMatch(chainId, resolvedLigandChainId)) ||
        fallbackChainIds.find((chainId) => !chainKeysMatch(chainId, resolvedLigandChainId)) ||
        resolvedTargetChainId;
    }
    return {
      chainIds: fallbackChainIds,
      targetChainId: resolvedTargetChainId || targetCandidate || null,
      ligandChainId: resolvedLigandChainId,
      ligandSmiles: preferTaskSmilesLigand ? taskLigandSmiles : '',
      ligandIsSmiles: preferTaskSmilesLigand ? Boolean(taskLigandSmiles) : false,
      ligandComponentCount,
      ligandSequence: '',
      ligandSequenceType: null
    };
  }

  const chainAssignments = assignChainIdsForComponents(activeComponents);
  const chainIdsByConfig = chainAssignments.flat().map((value) => value.toUpperCase());
  const chainIdByKey = new Map<string, string>();
  for (const chainId of [...chainIdsByConfig, ...confidenceChainIds, ...coverageChains.all]) {
    const key = normalizeChainKey(chainId);
    if (!key || chainIdByKey.has(key)) continue;
    chainIdByKey.set(key, chainId);
  }
  const chainIds = Array.from(chainIdByKey.values());

  const chainToComponent = new Map<string, InputComponent>();
  chainAssignments.forEach((chainGroup, index) => {
    chainGroup.forEach((chainId) => {
      chainToComponent.set(normalizeChainKey(chainId), activeComponents[index]);
    });
  });
  const componentOptions = activeComponents.map((component, index) => {
    const firstChain = chainAssignments[index]?.[0] || null;
    return {
      componentId: component.id,
      chainId: firstChain ? firstChain.toUpperCase() : null,
      type: component.type,
      isSmiles: component.type === 'ligand' && component.inputMethod !== 'ccd'
    };
  });
  const resolveChainFromCandidate = (candidate: string): string | null => {
    const raw = String(candidate || '').trim();
    if (!raw) return null;
    const normalized = raw.toUpperCase();
    const byOrdinalMatch = raw.match(/^comp(?:onent)?\s*#?(\d+)$/i) || raw.match(/^#?(\d+)$/);
    if (byOrdinalMatch) {
      const ordinal = Number.parseInt(byOrdinalMatch[1], 10);
      if (Number.isFinite(ordinal) && ordinal > 0) {
        const byOrdinal = componentOptions[ordinal - 1];
        if (byOrdinal?.chainId) return byOrdinal.chainId;
      }
    }
    const byKnownChain = chainIdByKey.get(normalizeChainKey(raw));
    if (byKnownChain) return byKnownChain;
    const byComponent = componentOptions.find((item) => item.componentId === raw);
    if (byComponent?.chainId) return byComponent.chainId;
    const byNormalizedChain = componentOptions.find(
      (item) => normalizeChainKey(String(item.chainId || '')) === normalizeChainKey(normalized)
    );
    if (byNormalizedChain?.chainId) return byNormalizedChain.chainId;
    for (const chainId of chainIds) {
      if (chainKeysMatch(chainId, normalized) || chainKeysMatch(normalized, chainId)) {
        return chainId;
      }
    }
    return null;
  };
  const resolveComponentFromCandidate = (
    candidate: string,
    options?: { allowAnyType?: boolean }
  ): { componentId: string; chainId: string | null; type: InputComponent['type']; isSmiles: boolean } | null => {
    const allowAnyType = Boolean(options?.allowAnyType);
    const raw = String(candidate || '').trim();
    if (!raw) return null;
    const chainId = resolveChainFromCandidate(raw);
    if (chainId) {
      const byChain = componentOptions.find((item) => item.chainId === chainId);
      if (byChain && (allowAnyType || byChain.type === 'ligand')) return byChain;
    }
    const byComponentId = componentOptions.find((item) => item.componentId === raw);
    if (byComponentId && (allowAnyType || byComponentId.type === 'ligand')) return byComponentId;
    return null;
  };
  const resolveWorkflowUploadLigandComponent = (
    workflowType: 'affinity' | 'lead_optimization'
  ): { componentId: string; chainId: string | null; type: InputComponent['type']; isSmiles: boolean } | null => {
    for (const component of activeComponents) {
      if (component.type !== 'ligand') continue;
      if (!normalizeComponentSequence('ligand', component.sequence)) continue;
      const raw = component as unknown as Record<string, unknown>;
      const role =
        workflowType === 'affinity' ? resolveAffinityUploadRole(raw) : resolveLeadOptUploadRole(raw);
      if (role !== 'ligand') continue;
      const option = componentOptions.find((item) => item.componentId === component.id) || null;
      if (option) return option;
    }
    return null;
  };
  const resolvedConfidenceTargetChain = resolveChainFromCandidate(confidenceTargetHint);
  const resolvedConfidenceLigandChain = resolveChainFromCandidate(confidenceLigandHint);
  let targetChainId =
    resolveChainFromCandidate(targetCandidate) ||
    resolvedConfidenceTargetChain ||
    componentOptions.find((item) => item.type !== 'ligand' && item.chainId)?.chainId ||
    chainAssignments[0]?.[0] ||
    chainIds[0] ||
    null;
  const predictionBindingCandidates = [
    ...splitChainTokens(rawBinder),
    ...splitChainTokens(rawLigand)
  ];
  const selectedLigandOption = (() => {
    if (useBindingPreference) {
      for (const candidate of predictionBindingCandidates) {
        const resolved = resolveComponentFromCandidate(candidate, { allowAnyType: true });
        if (resolved) return resolved;
      }
      return null;
    }
    if (workflow === 'affinity') return resolveWorkflowUploadLigandComponent('affinity');
    if (workflow === 'lead_optimization') return resolveWorkflowUploadLigandComponent('lead_optimization');
    return null;
  })();
  let ligandChainId = selectedLigandOption?.chainId || resolvedConfidenceLigandChain || null;
  if (ligandChainId && !targetChainId) {
    const ligandChainKey = ligandChainId;
    targetChainId =
      chainIds.find((chainId) => !chainKeysMatch(chainId, ligandChainKey)) ||
      resolvedConfidenceTargetChain ||
      targetChainId;
  }
  if (targetChainId && !ligandChainId) {
    const targetChainKey = targetChainId;
    ligandChainId =
      resolvedConfidenceLigandChain ||
      chainIds.find((chainId) => !chainKeysMatch(chainId, targetChainKey)) ||
      null;
  }
  if (
    targetChainId &&
    ligandChainId &&
    chainKeysMatch(targetChainId, ligandChainId) &&
    (workflow === 'affinity' || workflow === 'lead_optimization')
  ) {
    const preferredDifferentTarget =
      resolvedConfidenceTargetChain && !chainKeysMatch(resolvedConfidenceTargetChain, ligandChainId)
        ? resolvedConfidenceTargetChain
        : null;
    const firstDifferentChain = chainIds.find((chainId) => !chainKeysMatch(chainId, ligandChainId)) || null;
    targetChainId = preferredDifferentTarget || firstDifferentChain || targetChainId;
  }
  const selectedLigandComponent = selectedLigandOption
    ? chainToComponent.get(normalizeChainKey(String(selectedLigandOption.chainId || ''))) ||
      activeComponents.find((item) => item.id === selectedLigandOption.componentId) ||
      null
    : null;
  let ligand =
    selectedLigandComponent?.type === 'ligand'
      ? readTaskPrimaryLigand(activeComponents, selectedLigandComponent.id || null, ligandChainId, true, workflow)
      : {
          smiles: '',
          isSmiles: false
        };
  if (!selectedLigandComponent && (workflow === 'affinity' || workflow === 'lead_optimization')) {
    ligand = readTaskPrimaryLigand(activeComponents, null, null, true, workflow);
  }
  const resolvedTaskLigandSmiles = preferTaskSmilesLigand
    ? readTaskLigandSmilesHint(task, ligandChainId || workspacePreference?.ligandChainId || null) || taskLigandSmiles
    : '';
  if (preferTaskSmilesLigand && resolvedTaskLigandSmiles) {
    ligand = {
      smiles: resolvedTaskLigandSmiles,
      isSmiles: true
    };
  }
  const ligandSequence =
    !preferTaskSmilesLigand && selectedLigandComponent && isSequenceLigandType(selectedLigandComponent.type)
      ? normalizeComponentSequence(selectedLigandComponent.type, selectedLigandComponent.sequence || '')
      : '';
  const ligandSequenceType = preferTaskSmilesLigand ? null : selectedLigandComponent?.type || null;

  return {
    chainIds,
    targetChainId,
    ligandChainId,
    ligandSmiles: ligand.smiles,
    ligandIsSmiles: ligand.isSmiles,
    ligandComponentCount,
    ligandSequence,
    ligandSequenceType
  };
}

function readTaskConfidenceMetrics(task: ProjectTask, context?: TaskMetricContext): TaskConfidenceMetrics {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const strictPairIptm = context ? context.strictPairIptm !== false : false;
  const selectedLigandPlddt = context
    ? readChainMeanPlddtForChain(confidence, context.ligandChainId)
    : null;
  const selectedPairIptm = context
    ? readPairIptmForChains(confidence, context.targetChainId, context.ligandChainId, context.chainIds)
    : null;
  const plddtRaw = readFirstFiniteMetric(confidence, [
    'ligand_plddt',
    'ligand_mean_plddt',
    'complex_iplddt',
    'complex_plddt_protein',
    'complex_plddt',
    'plddt'
  ]);
  const iptmRaw = strictPairIptm
    ? selectedPairIptm
    : selectedPairIptm ?? readFirstFiniteMetric(confidence, ['iptm', 'ligand_iptm', 'protein_iptm']);
  const paeRaw = readFirstFiniteMetric(confidence, ['complex_pde', 'complex_pae', 'gpde', 'pae']);
  const mergedPlddt = selectedLigandPlddt ?? plddtRaw;
  return {
    plddt: mergedPlddt === null ? null : mergedPlddt <= 1 ? mergedPlddt * 100 : mergedPlddt,
    iptm: normalizeProbability(iptmRaw),
    pae: paeRaw
  };
}

interface PeptideTaskSummary {
  designMode: 'linear' | 'cyclic' | 'bicyclic' | null;
  binderLength: number | null;
  iterations: number | null;
  populationSize: number | null;
  eliteSize: number | null;
  mutationRate: number | null;
  currentGeneration: number | null;
  totalGenerations: number | null;
  bestScore: number | null;
  candidateCount: number | null;
  completedTasks: number | null;
  pendingTasks: number | null;
  totalTasks: number | null;
  stage: string;
  statusMessage: string;
}

interface LeadOptTaskSummary {
  stage: string;
  summary: string;
  databaseId: string;
  databaseLabel: string;
  databaseSchema: string;
  transformCount: number | null;
  candidateCount: number | null;
  bucketCount: number | null;
  predictionTotal: number | null;
  predictionQueued: number | null;
  predictionRunning: number | null;
  predictionSuccess: number | null;
  predictionFailure: number | null;
  selectedFragmentIds: string[];
  selectedAtomIndices: number[];
  selectedFragmentQuery: string;
}

interface PeptideBestCandidatePreview {
  sequence: string;
  plddt: number | null;
  iptm: number | null;
  residuePlddts: number[] | null;
  binderChainId: string | null;
}

function readFiniteNumber(value: unknown): number | null {
  const parsed = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : Number.NaN;
  if (!Number.isFinite(parsed)) return null;
  return parsed;
}

function readStringArray(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => (typeof item === 'string' ? item.trim() : ''))
    .filter(Boolean);
}

function readFirstFiniteFromPayloadPaths(payloads: Record<string, unknown>[], paths: string[]): number | null {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readFiniteNumber(readObjectPath(payload, path));
      if (value !== null) return value;
    }
  }
  return null;
}

function readFirstTextFromPayloadPaths(payloads: Record<string, unknown>[], paths: string[]): string {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = String(readObjectPath(payload, path) || '').trim();
      if (value) return value;
    }
  }
  return '';
}

function readFirstRecordArrayFromPayloadPaths(
  payloads: Record<string, unknown>[],
  paths: string[]
): Array<Record<string, unknown>> {
  for (const payload of payloads) {
    for (const path of paths) {
      const rows = readObjectPath(payload, path);
      if (!Array.isArray(rows)) continue;
      const records = rows.filter(
        (item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item))
      );
      if (records.length > 0) return records;
    }
  }
  return [];
}

function normalizePeptideDesignMode(value: string): 'linear' | 'cyclic' | 'bicyclic' | null {
  const token = value.trim().toLowerCase().replace(/[\s_-]+/g, '');
  if (!token) return null;
  if (token === 'linear') return 'linear';
  if (token === 'cyclic' || token === 'cycle' || token === 'monocyclic') return 'cyclic';
  if (token === 'bicyclic' || token === 'bicycle' || token === 'doublecyclic') return 'bicyclic';
  return null;
}

function normalizeNonNegativeInteger(value: number | null): number | null {
  if (value === null) return null;
  return Math.max(0, Math.floor(value));
}

function readPeptideTaskCandidateCount(payloads: Record<string, unknown>[]): number | null {
  const direct = readFirstFiniteFromPayloadPaths(payloads, [
    'candidate_count',
    'num_candidates',
    'best_sequence_count',
    'peptide_design.candidate_count'
  ]);
  if (direct !== null) return Math.max(0, Math.floor(direct));
  const arrayPaths = [
    'best_sequences',
    'current_best_sequences',
    'candidates',
    'peptide_candidates',
    'peptide_design.best_sequences',
    'peptide_design.current_best_sequences',
    'peptide_design.candidates'
  ];
  for (const payload of payloads) {
    for (const path of arrayPaths) {
      const rows = readObjectPath(payload, path);
      if (Array.isArray(rows) && rows.length > 0) return rows.length;
    }
  }
  return null;
}

function readPeptideTaskSummary(task: ProjectTask): PeptideTaskSummary | null {
  const peptidePreview = readPeptidePreviewFromProperties(task.properties);
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const peptideDesign =
    confidence?.peptide_design && typeof confidence.peptide_design === 'object' && !Array.isArray(confidence.peptide_design)
      ? (confidence.peptide_design as Record<string, unknown>)
      : {};
  const peptideProgress =
    peptideDesign.progress && typeof peptideDesign.progress === 'object' && !Array.isArray(peptideDesign.progress)
      ? (peptideDesign.progress as Record<string, unknown>)
      : {};
  const topProgress =
    confidence?.progress && typeof confidence.progress === 'object' && !Array.isArray(confidence.progress)
      ? (confidence.progress as Record<string, unknown>)
      : {};
  const requestPayload =
    confidence?.request && typeof confidence.request === 'object' && !Array.isArray(confidence.request)
      ? (confidence.request as Record<string, unknown>)
      : {};
  const requestOptions =
    requestPayload.options && typeof requestPayload.options === 'object' && !Array.isArray(requestPayload.options)
      ? (requestPayload.options as Record<string, unknown>)
      : {};
  const inputPayload =
    confidence?.inputs && typeof confidence.inputs === 'object' && !Array.isArray(confidence.inputs)
      ? (confidence.inputs as Record<string, unknown>)
      : {};
  const inputOptions =
    inputPayload.options && typeof inputPayload.options === 'object' && !Array.isArray(inputPayload.options)
      ? (inputPayload.options as Record<string, unknown>)
      : {};
  const taskRecord = task as unknown as Record<string, unknown>;
  const taskOptions =
    taskRecord.options && typeof taskRecord.options === 'object' && !Array.isArray(taskRecord.options)
      ? (taskRecord.options as Record<string, unknown>)
      : {};
  const payloads = [
    peptidePreview || {},
    confidence || {},
    topProgress,
    peptideDesign,
    peptideProgress,
    requestPayload,
    requestOptions,
    inputPayload,
    inputOptions,
    taskOptions
  ];

  const designMode = normalizePeptideDesignMode(
    readFirstTextFromPayloadPaths(payloads, [
      'design_mode',
      'mode',
      'peptide_design_mode',
      'peptideDesignMode',
      'peptide_design.design_mode',
      'peptide_design.mode',
      'request.options.peptide_design_mode',
      'request.options.peptideDesignMode',
      'inputs.options.peptide_design_mode',
      'inputs.options.peptideDesignMode'
    ])
  );

  const binderLength = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'binder_length',
      'length',
      'peptide_binder_length',
      'peptideBinderLength',
      'peptide_design.binder_length',
      'peptide_design.length',
      'request.options.peptide_binder_length',
      'request.options.peptideBinderLength',
      'inputs.options.peptide_binder_length',
      'inputs.options.peptideBinderLength'
    ])
  );

  const iterations = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'peptide_iterations',
      'peptideIterations',
      'generations',
      'total_generations',
      'peptide_design.iterations',
      'peptide_design.generations',
      'peptide_design.total_generations',
      'request.options.peptide_iterations',
      'request.options.peptideIterations',
      'inputs.options.peptide_iterations',
      'inputs.options.peptideIterations'
    ])
  );

  const populationSize = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'population_size',
      'peptide_population_size',
      'peptidePopulationSize',
      'peptide_design.population_size',
      'request.options.peptide_population_size',
      'request.options.peptidePopulationSize',
      'inputs.options.peptide_population_size',
      'inputs.options.peptidePopulationSize'
    ])
  );

  const eliteSize = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'elite_size',
      'num_elites',
      'peptide_elite_size',
      'peptideEliteSize',
      'peptide_design.elite_size',
      'request.options.peptide_elite_size',
      'request.options.peptideEliteSize',
      'inputs.options.peptide_elite_size',
      'inputs.options.peptideEliteSize'
    ])
  );

  const mutationRateRaw = readFirstFiniteFromPayloadPaths(payloads, [
    'mutation_rate',
    'peptide_mutation_rate',
    'peptideMutationRate',
    'peptide_design.mutation_rate',
    'request.options.peptide_mutation_rate',
    'request.options.peptideMutationRate',
    'inputs.options.peptide_mutation_rate',
    'inputs.options.peptideMutationRate'
  ]);
  const mutationRate = mutationRateRaw === null ? null : mutationRateRaw > 1 && mutationRateRaw <= 100 ? mutationRateRaw / 100 : mutationRateRaw;

  const currentGeneration = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'current_generation',
      'generation',
      'iter',
      'progress.current_generation',
      'peptide_design.current_generation',
      'peptide_design.progress.current_generation'
    ])
  );
  const totalGenerations = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'total_generations',
      'generations',
      'max_generation',
      'progress.total_generations',
      'peptide_design.total_generations',
      'peptide_design.progress.total_generations'
    ])
  );
  const bestScore = readFirstFiniteFromPayloadPaths(payloads, [
    'best_score',
    'current_best_score',
    'score',
    'peptide_design.best_score',
    'peptide_design.current_best_score'
  ]);
  const completedTasks = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'completed_tasks',
      'done_tasks',
      'finished_tasks',
      'peptide_design.completed_tasks',
      'peptide_design.progress.completed_tasks'
    ])
  );
  const pendingTasks = normalizeNonNegativeInteger(
    readFirstFiniteFromPayloadPaths(payloads, [
      'pending_tasks',
      'queued_tasks',
      'peptide_design.pending_tasks',
      'peptide_design.progress.pending_tasks'
    ])
  );
  const totalTasksRaw = readFirstFiniteFromPayloadPaths(payloads, [
    'total_tasks',
    'task_total',
    'peptide_design.total_tasks',
    'peptide_design.progress.total_tasks'
  ]);
  const totalTasks = normalizeNonNegativeInteger(
    totalTasksRaw !== null ? totalTasksRaw : completedTasks !== null && pendingTasks !== null ? completedTasks + pendingTasks : null
  );
  const candidateCount = readPeptideTaskCandidateCount(payloads);
  const stage = readFirstTextFromPayloadPaths(payloads, [
    'current_status',
    'status_stage',
    'stage',
    'progress.current_status',
    'peptide_design.current_status',
    'peptide_design.status_stage',
    'peptide_design.stage',
    'peptide_design.progress.current_status'
  ]);
  const statusMessage = readFirstTextFromPayloadPaths(payloads, [
    'status_message',
    'message',
    'status',
    'progress.status_message',
    'peptide_design.status_message',
    'peptide_design.progress.status_message'
  ]);

  if (
    !peptidePreview &&
    !confidence &&
    designMode === null &&
    binderLength === null &&
    iterations === null &&
    populationSize === null &&
    eliteSize === null &&
    mutationRate === null &&
    currentGeneration === null &&
    totalGenerations === null &&
    bestScore === null &&
    candidateCount === null &&
    completedTasks === null &&
    pendingTasks === null &&
    totalTasks === null &&
    !stage &&
    !statusMessage
  ) {
    return null;
  }

  return {
    designMode,
    binderLength,
    iterations,
    populationSize,
    eliteSize,
    mutationRate,
    currentGeneration,
    totalGenerations,
    bestScore,
    candidateCount,
    completedTasks,
    pendingTasks,
    totalTasks,
    stage,
    statusMessage
  };
}

function normalizePeptideCandidateSequence(value: string): string {
  return value.replace(/\s+/g, '').trim().toUpperCase();
}

function normalizePlddtScalar(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  if (value >= 0 && value <= 1) return value * 100;
  return value;
}

function readPeptideCandidateSequence(row: Record<string, unknown>): string {
  const sequence = readFirstTextFromPayloadPaths([row], [
    'peptide_sequence',
    'binder_sequence',
    'candidate_sequence',
    'designed_sequence',
    'sequence'
  ]);
  return normalizePeptideCandidateSequence(sequence);
}

function comparePeptideCandidateRows(a: Record<string, unknown>, b: Record<string, unknown>, aIndex: number, bIndex: number): number {
  const aRank = readFirstFiniteFromPayloadPaths([a], ['rank', 'ranking', 'order']);
  const bRank = readFirstFiniteFromPayloadPaths([b], ['rank', 'ranking', 'order']);
  const aRankValue = aRank !== null ? Math.max(1, Math.floor(aRank)) : null;
  const bRankValue = bRank !== null ? Math.max(1, Math.floor(bRank)) : null;
  if (aRankValue !== null && bRankValue !== null && aRankValue !== bRankValue) {
    return aRankValue - bRankValue;
  }
  if (aRankValue !== null && bRankValue === null) return -1;
  if (aRankValue === null && bRankValue !== null) return 1;

  const aScore = readFirstFiniteFromPayloadPaths([a], ['composite_score', 'score', 'fitness', 'objective']);
  const bScore = readFirstFiniteFromPayloadPaths([b], ['composite_score', 'score', 'fitness', 'objective']);
  if (aScore !== null && bScore !== null && aScore !== bScore) {
    return bScore - aScore;
  }
  if (aScore !== null && bScore === null) return -1;
  if (aScore === null && bScore !== null) return 1;

  const aPlddt = normalizePlddtScalar(
    readFirstFiniteFromPayloadPaths([a], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt'])
  );
  const bPlddt = normalizePlddtScalar(
    readFirstFiniteFromPayloadPaths([b], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt'])
  );
  if (aPlddt !== null && bPlddt !== null && aPlddt !== bPlddt) {
    return bPlddt - aPlddt;
  }
  if (aPlddt !== null && bPlddt === null) return -1;
  if (aPlddt === null && bPlddt !== null) return 1;

  return aIndex - bIndex;
}

function alignResidueSeriesToSequence(values: number[], sequenceLength: number): number[] {
  const normalized = normalizeAtomPlddts(values);
  if (normalized.length === 0) return [];
  if (sequenceLength <= 0) return normalized;
  if (normalized.length === sequenceLength) return normalized;
  if (normalized.length < Math.min(sequenceLength, 4)) return [];
  if (normalized.length > sequenceLength) {
    const reduced: number[] = [];
    for (let i = 0; i < sequenceLength; i += 1) {
      const start = Math.floor((i * normalized.length) / sequenceLength);
      const end = Math.max(start + 1, Math.floor(((i + 1) * normalized.length) / sequenceLength));
      const chunk = normalized.slice(start, end);
      reduced.push(chunk.reduce((sum, value) => sum + value, 0) / chunk.length);
    }
    return reduced;
  }
  const expanded: number[] = [];
  for (let i = 0; i < sequenceLength; i += 1) {
    const mapped = Math.floor((i * normalized.length) / sequenceLength);
    expanded.push(normalized[Math.min(normalized.length - 1, Math.max(0, mapped))]);
  }
  return expanded;
}

function readResidueSeriesByChain(
  value: unknown,
  sequenceLength: number,
  preferredChainId: string | null | undefined
): number[] {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
  const record = value as Record<string, unknown>;
  const preferred = String(preferredChainId || '').trim();
  let best: { values: number[]; score: number } | null = null;
  for (const [chainId, raw] of Object.entries(record)) {
    const values = normalizeAtomPlddts(toFiniteNumberArray(raw));
    if (values.length === 0) continue;
    let score = 0;
    if (preferred && chainKeysMatch(chainId, preferred)) score += 48;
    score -= Math.abs(values.length - sequenceLength) * 4;
    if (values.length === sequenceLength) score += 30;
    if (values.length >= Math.max(1, sequenceLength - 2) && values.length <= sequenceLength + 2) score += 16;
    if (!best || score > best.score) {
      best = { values, score };
    }
  }
  if (!best) return [];
  return alignResidueSeriesToSequence(best.values, sequenceLength);
}

function readCandidateResiduePlddts(
  row: Record<string, unknown>,
  sequenceLength: number,
  preferredChainId: string | null | undefined
): number[] | null {
  const direct = alignResidueSeriesToSequence(
    toFiniteNumberArray(
      readObjectPath(row, 'residue_plddts') ??
        readObjectPath(row, 'residue_plddt') ??
        readObjectPath(row, 'per_residue_plddt') ??
        readObjectPath(row, 'plddts')
    ),
    sequenceLength
  );
  if (direct.length >= Math.min(sequenceLength, 4)) return direct;

  const byChainCandidates = [
    readObjectPath(row, 'residue_plddt_by_chain'),
    readObjectPath(row, 'residuePlddtByChain'),
    readObjectPath(row, 'residue_plddts_by_chain'),
    readObjectPath(row, 'chain_residue_plddt'),
    readObjectPath(row, 'chain_plddt'),
    readObjectPath(row, 'chain_plddts')
  ];
  for (const candidate of byChainCandidates) {
    const values = readResidueSeriesByChain(candidate, sequenceLength, preferredChainId);
    if (values.length >= Math.min(sequenceLength, 4)) return values;
  }
  return null;
}

function readPeptideBestCandidatePreview(task: ProjectTask): PeptideBestCandidatePreview | null {
  const peptidePreview = readPeptidePreviewFromProperties(task.properties);
  const previewBest = peptidePreview
    ? (() => {
        const best = readObjectPath(peptidePreview, 'best_candidate');
        return best && typeof best === 'object' && !Array.isArray(best) ? (best as Record<string, unknown>) : null;
      })()
    : null;
  if (previewBest) {
    const sequence = readPeptideCandidateSequence(previewBest);
    if (sequence) {
      const plddt = normalizePlddtScalar(
        readFirstFiniteFromPayloadPaths([previewBest], ['plddt', 'binder_avg_plddt', 'ligand_mean_plddt', 'mean_plddt'])
      );
      const iptm = normalizeProbability(
        readFirstFiniteFromPayloadPaths([previewBest], ['pair_iptm_target_binder', 'pair_iptm', 'iptm'])
      );
      const binderChainId = readFirstTextFromPayloadPaths(
        [previewBest, peptidePreview || {}],
        ['binder_chain_id', 'model_ligand_chain_id', 'requested_ligand_chain_id', 'ligand_chain_id']
      );
      const residuePlddts = readCandidateResiduePlddts(previewBest, sequence.length, binderChainId);
      return {
        sequence,
        plddt,
        iptm,
        residuePlddts,
        binderChainId: binderChainId || null
      };
    }
  }

  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  if (!confidence) return null;
  const peptideDesign =
    confidence.peptide_design && typeof confidence.peptide_design === 'object' && !Array.isArray(confidence.peptide_design)
      ? (confidence.peptide_design as Record<string, unknown>)
      : {};
  const peptideProgress =
    peptideDesign.progress && typeof peptideDesign.progress === 'object' && !Array.isArray(peptideDesign.progress)
      ? (peptideDesign.progress as Record<string, unknown>)
      : {};
  const topProgress =
    confidence.progress && typeof confidence.progress === 'object' && !Array.isArray(confidence.progress)
      ? (confidence.progress as Record<string, unknown>)
      : {};

  const candidateRows = readFirstRecordArrayFromPayloadPaths(
    [confidence, peptideDesign, peptideProgress, topProgress],
    [
      'peptide_design.best_sequences',
      'peptide_design.current_best_sequences',
      'peptide_design.candidates',
      'best_sequences',
      'current_best_sequences',
      'candidates',
      'progress.best_sequences',
      'progress.current_best_sequences'
    ]
  );
  if (candidateRows.length === 0) return null;

  const rowOrder = new Map(candidateRows.map((row, index) => [row, index] as const));
  const sorted = [...candidateRows].sort((a, b) =>
    comparePeptideCandidateRows(a, b, rowOrder.get(a) ?? 0, rowOrder.get(b) ?? 0)
  );
  const best = sorted.find((row) => Boolean(readPeptideCandidateSequence(row))) || sorted[0];
  if (!best) return null;

  const sequence = readPeptideCandidateSequence(best);
  if (!sequence) return null;

  const plddt = normalizePlddtScalar(
    readFirstFiniteFromPayloadPaths([best], ['binder_avg_plddt', 'plddt', 'ligand_mean_plddt', 'mean_plddt'])
  );
  const iptm = normalizeProbability(
    readFirstFiniteFromPayloadPaths([best], ['pair_iptm_target_binder', 'pair_iptm', 'iptm'])
  );
  const binderChainId = readFirstTextFromPayloadPaths(
    [best, confidence, peptideDesign, peptideProgress, topProgress],
    ['binder_chain_id', 'model_ligand_chain_id', 'requested_ligand_chain_id', 'ligand_chain_id']
  );
  const residuePlddts = readCandidateResiduePlddts(best, sequence.length, binderChainId);

  return {
    sequence,
    plddt,
    iptm,
    residuePlddts,
    binderChainId: binderChainId || null
  };
}

function readLeadOptVariableItems(leadOptMmp: Record<string, unknown>): Array<Record<string, unknown>> {
  const selection = readObjectPath(leadOptMmp, 'selection');
  if (selection && typeof selection === 'object' && !Array.isArray(selection)) {
    const selectedItems = readObjectPath(selection as Record<string, unknown>, 'variable_items');
    if (Array.isArray(selectedItems)) {
      return selectedItems
        .filter((item) => item && typeof item === 'object' && !Array.isArray(item))
        .map((item) => item as Record<string, unknown>);
    }
  }
  const queryPayload = readObjectPath(leadOptMmp, 'query_payload');
  if (queryPayload && typeof queryPayload === 'object' && !Array.isArray(queryPayload)) {
    const variableSpec = readObjectPath(queryPayload as Record<string, unknown>, 'variable_spec');
    if (variableSpec && typeof variableSpec === 'object' && !Array.isArray(variableSpec)) {
      const items = readObjectPath(variableSpec as Record<string, unknown>, 'items');
      if (Array.isArray(items)) {
        return items
          .filter((item) => item && typeof item === 'object' && !Array.isArray(item))
          .map((item) => item as Record<string, unknown>);
      }
    }
  }
  return [];
}

function normalizeLeadOptTaskStage(value: string): string {
  const token = value.trim().toLowerCase();
  if (!token) return 'unknown';
  return token;
}

function readLeadOptTaskListMetaFromProperties(task: ProjectTask): Record<string, unknown> | null {
  const properties =
    task.properties && typeof task.properties === 'object' && !Array.isArray(task.properties)
      ? (task.properties as unknown as Record<string, unknown>)
      : null;
  if (!properties) return null;
  const meta = properties.lead_opt_list;
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    return meta as Record<string, unknown>;
  }
  return null;
}

function readLeadOptTaskStateMetaFromProperties(task: ProjectTask): Record<string, unknown> | null {
  const properties =
    task.properties && typeof task.properties === 'object' && !Array.isArray(task.properties)
      ? (task.properties as unknown as Record<string, unknown>)
      : null;
  if (!properties) return null;
  const meta = properties.lead_opt_state;
  if (meta && typeof meta === 'object' && !Array.isArray(meta)) {
    return meta as Record<string, unknown>;
  }
  return null;
}

function readLeadOptTaskSummary(task: ProjectTask): LeadOptTaskSummary | null {
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const leadOptMmpFromConfidence =
    confidence?.lead_opt_mmp && typeof confidence.lead_opt_mmp === 'object' && !Array.isArray(confidence.lead_opt_mmp)
      ? (confidence.lead_opt_mmp as Record<string, unknown>)
      : null;
  const leadOptListMeta = readLeadOptTaskListMetaFromProperties(task);
  const leadOptStateMeta = readLeadOptTaskStateMetaFromProperties(task);
  const mergedLeadOptFromProperties: Record<string, unknown> | null =
    leadOptListMeta || leadOptStateMeta
      ? {
          ...(leadOptListMeta || {}),
          ...(leadOptStateMeta || {}),
          prediction_summary: {
            ...asRecord((leadOptListMeta || {}).prediction_summary),
            ...asRecord((leadOptStateMeta || {}).prediction_summary),
          },
          prediction_by_smiles: {
            ...asRecord((leadOptListMeta || {}).prediction_by_smiles),
            ...asRecord((leadOptStateMeta || {}).prediction_by_smiles),
          },
          reference_prediction_by_backend: {
            ...asRecord((leadOptListMeta || {}).reference_prediction_by_backend),
            ...asRecord((leadOptStateMeta || {}).reference_prediction_by_backend),
          },
        } as Record<string, unknown>
      : null;
  const leadOptMmp = leadOptMmpFromConfidence || mergedLeadOptFromProperties;
  if (!leadOptMmp) return null;
  const queryResultRaw = readObjectPath(leadOptMmp, 'query_result');
  const queryResult =
    queryResultRaw && typeof queryResultRaw === 'object' && !Array.isArray(queryResultRaw)
      ? (queryResultRaw as Record<string, unknown>)
      : {};

  const stage = normalizeLeadOptTaskStage(
    String(
      leadOptMmp.prediction_stage ||
      leadOptMmp.stage ||
      leadOptMmp.prediction_state ||
      task.task_state ||
      ''
    )
  );
  const predictionSummary =
    leadOptMmp.prediction_summary && typeof leadOptMmp.prediction_summary === 'object' && !Array.isArray(leadOptMmp.prediction_summary)
      ? (leadOptMmp.prediction_summary as Record<string, unknown>)
      : {};
  const predictionMap =
    leadOptMmp.prediction_by_smiles && typeof leadOptMmp.prediction_by_smiles === 'object' && !Array.isArray(leadOptMmp.prediction_by_smiles)
      ? (leadOptMmp.prediction_by_smiles as Record<string, unknown>)
      : {};
  const bucketCountFromSummary = readFiniteNumber(predictionSummary.total);
  const bucketCount = bucketCountFromSummary !== null ? Math.max(0, Math.floor(bucketCountFromSummary)) : Object.keys(predictionMap).length;
  const predictionQueued = (() => {
    const value = readFiniteNumber(predictionSummary.queued);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionRunning = (() => {
    const value = readFiniteNumber(predictionSummary.running);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionSuccess = (() => {
    const value = readFiniteNumber(predictionSummary.success);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionFailure = (() => {
    const value = readFiniteNumber(predictionSummary.failure);
    return value === null ? null : Math.max(0, Math.floor(value));
  })();
  const predictionTotal = (() => {
    const value = readFiniteNumber(predictionSummary.total);
    if (value !== null) return Math.max(0, Math.floor(value));
    if (Object.keys(predictionMap).length > 0) return Object.keys(predictionMap).length;
    if (
      predictionQueued !== null ||
      predictionRunning !== null ||
      predictionSuccess !== null ||
      predictionFailure !== null
    ) {
      return (predictionQueued || 0) + (predictionRunning || 0) + (predictionSuccess || 0) + (predictionFailure || 0);
    }
    return null;
  })();
  const transformCount = readFiniteNumber(leadOptMmp.transform_count);
  const candidateCount = readFiniteNumber(leadOptMmp.candidate_count);
  const selectedFragmentIds = (() => {
    const selectionIds = readStringArray(
      readObjectPath(leadOptMmp, 'selection.selected_fragment_ids') ?? leadOptMmp.selected_fragment_ids
    );
    if (selectionIds.length > 0) return Array.from(new Set(selectionIds));
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    return Array.from(
      new Set(
        variableItems
          .map((item) => String(item.fragment_id || '').trim())
          .filter(Boolean)
      )
    );
  })();
  const selectedAtomIndices = (() => {
    const selectionAtomsRaw =
      readObjectPath(leadOptMmp, 'selection.selected_fragment_atom_indices') ?? leadOptMmp.selected_fragment_atom_indices;
    if (Array.isArray(selectionAtomsRaw)) {
      return Array.from(
        new Set(
          selectionAtomsRaw
            .map((value) => Number(value))
            .filter((value) => Number.isFinite(value) && value >= 0)
            .map((value) => Math.floor(value))
        )
      );
    }
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    return Array.from(
      new Set(
        variableItems.flatMap((item) => {
          if (!Array.isArray(item.atom_indices)) return [];
          return item.atom_indices
            .map((value) => Number(value))
            .filter((value) => Number.isFinite(value) && value >= 0)
            .map((value) => Math.floor(value));
        })
      )
    );
  })();
  const selectedFragmentQuery = (() => {
    const selectionQueries = readStringArray(
      readObjectPath(leadOptMmp, 'selection.variable_queries') ?? leadOptMmp.variable_queries
    );
    if (selectionQueries.length > 0) return selectionQueries[0];
    const variableItems = readLeadOptVariableItems(leadOptMmp);
    for (const item of variableItems) {
      const query = String(item.query || '').trim();
      if (query) return query;
    }
    return String(leadOptMmp.selected_fragment_query || '').trim();
  })();

  const stageLabel = (() => {
    if (stage === 'prediction_running' || stage === 'running') return 'Running';
    if (stage === 'prediction_completed' || stage === 'completed') return 'Completed';
    if (stage === 'prediction_failed' || stage === 'failed') return 'Failed';
    if (stage === 'prediction_queued' || stage === 'queued') return 'Queued';
    if (stage === 'idle') return 'Idle';
    return stage ? stage.replace(/_/g, ' ') : 'Unknown';
  })();

  const summary = (() => {
    const parts: string[] = [stageLabel];
    if (transformCount !== null) parts.push(`${Math.max(0, Math.floor(transformCount))} transforms`);
    if (candidateCount !== null) parts.push(`${Math.max(0, Math.floor(candidateCount))} candidates`);
    if (bucketCount > 0) parts.push(`${bucketCount} buckets`);
    if (predictionTotal !== null && predictionTotal > 0) {
      parts.push(
        `q${predictionQueued || 0}/r${predictionRunning || 0}/s${predictionSuccess || 0}/f${predictionFailure || 0}`
      );
    }
    return parts.join(' · ');
  })();
  const databaseId = String(
    leadOptMmp.mmp_database_id || queryResult.mmp_database_id || ''
  ).trim();
  const databaseSchema = String(
    leadOptMmp.mmp_database_schema || queryResult.mmp_database_schema || ''
  ).trim();
  const databaseLabel = String(
    leadOptMmp.mmp_database_label || queryResult.mmp_database_label || databaseSchema || databaseId || ''
  ).trim();

  return {
    stage,
    summary,
    databaseId,
    databaseLabel,
    databaseSchema,
    transformCount: transformCount === null ? null : Math.max(0, Math.floor(transformCount)),
    candidateCount: candidateCount === null ? null : Math.max(0, Math.floor(candidateCount)),
    bucketCount,
    predictionTotal,
    predictionQueued,
    predictionRunning,
    predictionSuccess,
    predictionFailure,
    selectedFragmentIds,
    selectedAtomIndices,
    selectedFragmentQuery
  };
}

function hasLeadOptPredictionRuntime(task: ProjectTask): boolean {
  const summary = readLeadOptTaskSummary(task);
  if (!summary) return false;
  const stage = summary.stage;
  if (stage === 'prediction_running' || stage === 'running' || stage === 'prediction_queued' || stage === 'queued') {
    return true;
  }
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : null;
  const leadOptMmp =
    confidence?.lead_opt_mmp && typeof confidence.lead_opt_mmp === 'object' && !Array.isArray(confidence.lead_opt_mmp)
      ? (confidence.lead_opt_mmp as Record<string, unknown>)
      : null;
  if (!leadOptMmp) return false;
  const predictionSummary =
    leadOptMmp.prediction_summary && typeof leadOptMmp.prediction_summary === 'object' && !Array.isArray(leadOptMmp.prediction_summary)
      ? (leadOptMmp.prediction_summary as Record<string, unknown>)
      : {};
  const queued = readFiniteNumber(predictionSummary.queued) || 0;
  const running = readFiniteNumber(predictionSummary.running) || 0;
  return queued + running > 0;
}

function toFiniteNumberArray(value: unknown): number[] {
  const normalizeItems = (items: unknown[]): number[] =>
    items
      .map((item) => {
        if (typeof item === 'number') return Number.isFinite(item) ? item : null;
        if (typeof item === 'string') {
          const parsed = Number(item.trim());
          return Number.isFinite(parsed) ? parsed : null;
        }
        return null;
      })
      .filter((item): item is number => item !== null);

  if (Array.isArray(value)) {
    return normalizeItems(value);
  }
  if (value && typeof value === 'object') {
    const obj = value as Record<string, unknown>;
    const scalarEntries = Object.entries(obj)
      .map(([key, item]) => ({
        key,
        keyNumber: Number(key),
        value:
          typeof item === 'number'
            ? (Number.isFinite(item) ? item : null)
            : typeof item === 'string'
              ? (() => {
                  const parsed = Number(item.trim());
                  return Number.isFinite(parsed) ? parsed : null;
                })()
              : null
      }))
      .filter((entry) => entry.value !== null);
    const numericKeyEntries = scalarEntries.filter((entry) => Number.isFinite(entry.keyNumber));
    if (numericKeyEntries.length >= 3 && numericKeyEntries.length >= Math.floor(scalarEntries.length * 0.6)) {
      numericKeyEntries.sort((a, b) => a.keyNumber - b.keyNumber);
      return numericKeyEntries.map((entry) => entry.value as number);
    }
    const nestedCandidates: unknown[] = [
      obj.values,
      obj.value,
      obj.plddt,
      obj.plddts,
      obj.residue_plddt,
      obj.residue_plddts,
      obj.token_plddt,
      obj.token_plddts,
      obj.scores
    ];
    for (const candidate of nestedCandidates) {
      if (Array.isArray(candidate)) {
        const parsed = normalizeItems(candidate);
        if (parsed.length > 0) return parsed;
      }
    }
  }
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value) as unknown;
      if (Array.isArray(parsed)) {
        return normalizeItems(parsed);
      }
      if (parsed && typeof parsed === 'object') {
        return toFiniteNumberArray(parsed);
      }
    } catch {
      return [];
    }
  }
  return [];
}

function normalizeAtomPlddts(values: number[]): number[] {
  const normalized = values
    .filter((value) => Number.isFinite(value))
    .map((value) => {
      if (value >= 0 && value <= 1) return value * 100;
      return value;
    })
    .map((value) => Math.max(0, Math.min(100, value)));
  if (normalized.length === 0) return [];
  return normalized;
}

function mean(values: number[] | null): number | null {
  if (!values || values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function normalizeChainKey(value: string): string {
  return value.trim().toUpperCase();
}

function chainKeysMatch(candidate: string, preferred: string): boolean {
  const normalizedCandidate = normalizeChainKey(candidate);
  const normalizedPreferred = normalizeChainKey(preferred);
  if (!normalizedCandidate || !normalizedPreferred) return false;
  if (normalizedCandidate === normalizedPreferred) return true;

  const compactCandidate = normalizedCandidate.replace(/[^A-Z0-9]/g, '');
  const compactPreferred = normalizedPreferred.replace(/[^A-Z0-9]/g, '');
  if (compactCandidate && compactPreferred && compactCandidate === compactPreferred) return true;

  const candidateTokens = normalizedCandidate.split(/[^A-Z0-9]+/).filter(Boolean);
  if (candidateTokens.includes(normalizedPreferred) || (compactPreferred && candidateTokens.includes(compactPreferred))) {
    return true;
  }

  if (compactCandidate && compactPreferred) {
    if (compactCandidate.startsWith(compactPreferred) || compactCandidate.endsWith(compactPreferred)) {
      return true;
    }
    if (compactPreferred.startsWith(compactCandidate) || compactPreferred.endsWith(compactCandidate)) {
      return true;
    }
    return compactCandidate.endsWith(compactPreferred);
  }
  return false;
}

function readTaskLigandAtomPlddtsFromChainMap(
  value: unknown,
  preferredChainKeys: Set<string>,
  allowFallbackToAnyChain: boolean
): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const parsedEntries = Object.entries(value as Record<string, unknown>)
    .map(([key, chainValues]) => {
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length === 0) return null;
      return {
        chainId: normalizeChainKey(key),
        values: parsed
      };
    })
    .filter((entry): entry is { chainId: string; values: number[] } => entry !== null);
  if (parsedEntries.length === 0) return null;

  const pickLongest = (values: number[][]): number[] | null => {
    if (values.length === 0) return null;
    return values.reduce((best, current) => (current.length > best.length ? current : best), values[0]);
  };

  if (preferredChainKeys.size > 0) {
    const matched = parsedEntries
      .filter((entry) =>
        Array.from(preferredChainKeys).some(
          (preferred) => chainKeysMatch(entry.chainId, preferred) || chainKeysMatch(preferred, entry.chainId)
        )
      )
      .map((entry) => entry.values);
    const selected = pickLongest(matched);
    if (selected) return selected;
  }

  if (allowFallbackToAnyChain) {
    return pickLongest(parsedEntries.map((entry) => entry.values));
  }
  return null;
}

function readTaskResiduePlddtsFromChainMap(
  value: unknown,
  preferredChainKeys: Set<string>
): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const map = value as Record<string, unknown>;
  const entries = Object.entries(map);
  if (entries.length === 0) return null;

  if (preferredChainKeys.size > 0) {
    for (const [key, chainValues] of entries) {
      const matched = Array.from(preferredChainKeys).some((preferred) => chainKeysMatch(key, preferred));
      if (!matched) continue;
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length > 0) return parsed;
    }
  }
  return null;
}

function collectTaskPreferredLigandChainKeys(
  confidence: Record<string, unknown>,
  preferredLigandChainId: string | null
): Set<string> {
  const keys = new Set<string>();
  if (preferredLigandChainId) {
    keys.add(normalizeChainKey(preferredLigandChainId));
  }
  const modelLigandChain = readFirstNonEmptyStringMetric(confidence, ['model_ligand_chain_id']);
  if (modelLigandChain) {
    keys.add(normalizeChainKey(modelLigandChain));
  }
  const requestedLigandChain = readFirstNonEmptyStringMetric(confidence, ['requested_ligand_chain_id', 'ligand_chain_id']);
  if (requestedLigandChain) {
    keys.add(normalizeChainKey(requestedLigandChain));
  }
  return keys;
}

function readTaskTokenPlddtsForChain(
  confidence: Record<string, unknown>,
  preferredChainKeys: Set<string>
): number[] | null {
  const tokenPlddtCandidates: unknown[] = [
    confidence.token_plddts,
    confidence.token_plddt,
    readObjectPath(confidence, 'token_plddts'),
    readObjectPath(confidence, 'token_plddt'),
    readObjectPath(confidence, 'plddt_by_token')
  ];
  const tokenChainCandidates: unknown[] = [
    confidence.token_chain_ids,
    confidence.token_chain_id,
    readObjectPath(confidence, 'token_chain_ids'),
    readObjectPath(confidence, 'token_chain_id'),
    readObjectPath(confidence, 'chain_ids_by_token')
  ];

  if (preferredChainKeys.size === 0) return null;
  for (const plddtCandidate of tokenPlddtCandidates) {
    const tokenPlddts = normalizeAtomPlddts(toFiniteNumberArray(plddtCandidate));
    if (tokenPlddts.length === 0) continue;

    for (const chainCandidate of tokenChainCandidates) {
      if (!Array.isArray(chainCandidate)) continue;
      if (chainCandidate.length !== tokenPlddts.length) continue;
      const tokenChains = chainCandidate.map((value) => normalizeChainKey(String(value || '')));
      if (tokenChains.some((value) => !value)) continue;

      const byChain = tokenPlddts.filter((_, index) => {
        return Array.from(preferredChainKeys).some((preferred) => chainKeysMatch(tokenChains[index], preferred));
      });
      if (byChain.length > 0) return byChain;
    }
  }
  return null;
}

function readTaskLigandResiduePlddts(task: ProjectTask, preferredLigandChainId: string | null): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const preferredChainKeys = collectTaskPreferredLigandChainKeys(confidence, preferredLigandChainId);
  if (preferredChainKeys.size === 0) return null;
  const chainMapCandidates: unknown[] = [
    confidence.residue_plddt_by_chain,
    confidence.chain_residue_plddt,
    confidence.plddt_by_chain,
    readObjectPath(confidence, 'residue_plddt_by_chain'),
    readObjectPath(confidence, 'chain_residue_plddt'),
    readObjectPath(confidence, 'plddt.by_chain')
  ];
  for (const candidate of chainMapCandidates) {
    const parsed = readTaskResiduePlddtsFromChainMap(candidate, preferredChainKeys);
    if (parsed && parsed.length > 0) return parsed;
  }

  const tokenPlddts = readTaskTokenPlddtsForChain(confidence, preferredChainKeys);
  if (tokenPlddts && tokenPlddts.length > 0) return tokenPlddts;

  return null;
}

function readTaskLigandAtomPlddts(
  task: ProjectTask,
  preferredLigandChainId: string | null = null,
  allowFlatFallback = true
): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const preferredChainKeys = collectTaskPreferredLigandChainKeys(confidence, preferredLigandChainId);
  const byChainCandidates: unknown[] = [
    confidence.ligand_atom_plddts_by_chain,
    readObjectPath(confidence, 'ligand.atom_plddts_by_chain'),
    readObjectPath(confidence, 'ligand_confidence.atom_plddts_by_chain')
  ];
  let hasChainMap = false;
  for (const candidate of byChainCandidates) {
    if (candidate && typeof candidate === 'object' && !Array.isArray(candidate)) {
      hasChainMap = true;
    }
    const parsed = readTaskLigandAtomPlddtsFromChainMap(
      candidate,
      preferredChainKeys,
      preferredChainKeys.size === 0
    );
    if (parsed && parsed.length > 0) {
      return parsed;
    }
  }
  if (preferredChainKeys.size > 0 && hasChainMap && !allowFlatFallback) return null;
  if (!allowFlatFallback) return null;

  const candidates: unknown[] = [
    confidence.ligand_atom_plddts,
    confidence.ligand_atom_plddt,
    readObjectPath(confidence, 'ligand.atom_plddts'),
    readObjectPath(confidence, 'ligand.atom_plddt'),
    readObjectPath(confidence, 'ligand_confidence.atom_plddts')
  ];
  for (const candidate of candidates) {
    const parsed = normalizeAtomPlddts(toFiniteNumberArray(candidate));
    if (parsed.length > 0) {
      return parsed;
    }
  }
  return null;
}

function hasTaskLigandAtomPlddts(
  task: ProjectTask,
  preferredLigandChainId: string | null = null,
  allowFlatFallback = true
): boolean {
  return Boolean(readTaskLigandAtomPlddts(task, preferredLigandChainId, allowFlatFallback)?.length);
}

function hasTaskSummaryMetrics(task: ProjectTask): boolean {
  const context = resolveTaskSelectionContext(task);
  const metrics = readTaskConfidenceMetrics(task, context);
  return metrics.plddt !== null || metrics.iptm !== null || metrics.pae !== null;
}

export {
  TASKS_PAGE_FILTERS_STORAGE_KEY,
  TASK_SORT_DIRECTIONS,
  TASK_SORT_KEYS,
  TASK_SUBMITTED_WINDOW_OPTIONS,
  TASK_SEED_FILTER_OPTIONS,
  TASK_STRUCTURE_SEARCH_MODES,
  TASK_PAGE_SIZE_OPTIONS,
  normalizeTaskWorkspaceView,
  sanitizeTaskRows,
  sortProjectTasks,
  isProjectTaskRow,
  isProjectRow,
  resolveTaskSelectionContext,
  hasTaskSummaryMetrics,
  hasTaskLigandAtomPlddts,
  readTaskLigandAtomPlddts,
  readTaskLigandResiduePlddts,
  readTaskConfidenceMetrics,
  readPeptideTaskSummary,
  readPeptideBestCandidatePreview,
  readLeadOptTaskSummary,
  hasLeadOptPredictionRuntime,
  isSequenceLigandType,
  alignConfidenceSeriesToLength,
  mean
};

export {
  SILENT_CACHE_SYNC_WINDOW_MS,
  mapTaskState,
  inferTaskStateFromStatusPayload,
  readStatusText,
  resolveTaskBackendValue,
  compareNullableNumber,
  defaultSortDirection,
  nextSortDirection,
  parseNumberOrNull,
  normalizePlddtThreshold,
  normalizeIptmThreshold,
  normalizeSmilesForSearch,
  hasSubstructureMatchPayload,
  sanitizeFileName,
  toBase64FromBytes,
  waitForRuntimeTaskToStop
} from './taskRuntimeUiUtils';

export type { LoadTaskDataOptions } from './taskRuntimeUiUtils';
