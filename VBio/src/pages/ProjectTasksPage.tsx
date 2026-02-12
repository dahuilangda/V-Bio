import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Activity,
  ArrowLeft,
  Clock3,
  Download,
  ExternalLink,
  Filter,
  LoaderCircle,
  RefreshCcw,
  Search,
  SlidersHorizontal,
  Trash2
} from 'lucide-react';
import { downloadResultBlob, getTaskStatus, parseResultBundle, terminateTask as terminateBackendTask } from '../api/backendApi';
import { deleteProjectTask, getProjectById, listProjectTasks, listProjectTasksForList, updateProject, updateProjectTask } from '../api/supabaseLite';
import { JSMEEditor } from '../components/project/JSMEEditor';
import { Ligand2DPreview } from '../components/project/Ligand2DPreview';
import { useAuth } from '../hooks/useAuth';
import type { InputComponent, Project, ProjectTask } from '../types/models';
import { assignChainIdsForComponents } from '../utils/chainAssignments';
import { formatDateTime, formatDuration } from '../utils/date';
import { renderLigand2DSvg } from '../utils/ligand2d';
import { loadProjectInputConfig, normalizeComponentSequence, normalizeInputComponents } from '../utils/projectInputs';
import { loadRDKitModule } from '../utils/rdkit';

function normalizeTaskComponents(components: InputComponent[]): InputComponent[] {
  return normalizeInputComponents(components);
}

function readTaskComponents(task: ProjectTask): InputComponent[] {
  const components = Array.isArray(task.components) ? normalizeTaskComponents(task.components) : [];
  if (components.length > 0) return components;

  const fallback: InputComponent[] = [];
  const proteinSequence = normalizeComponentSequence('protein', task.protein_sequence || '');
  const ligandSmiles = normalizeComponentSequence('ligand', task.ligand_smiles || '');
  if (proteinSequence) {
    fallback.push({
      id: 'task-protein-1',
      type: 'protein',
      numCopies: 1,
      sequence: proteinSequence,
      useMsa: true,
      cyclic: false
    });
  }
  if (ligandSmiles) {
    fallback.push({
      id: 'task-ligand-1',
      type: 'ligand',
      numCopies: 1,
      sequence: ligandSmiles,
      inputMethod: 'jsme'
    });
  }
  return fallback;
}

function readTaskPrimaryLigand(
  task: ProjectTask,
  components: InputComponent[],
  preferredComponentId: string | null
): { smiles: string; isSmiles: boolean } {
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

  const ligand = components.find((item) => item.type === 'ligand' && normalizeComponentSequence('ligand', item.sequence));
  if (ligand) {
    const value = normalizeComponentSequence('ligand', ligand.sequence);
    return {
      smiles: value,
      isSmiles: ligand.inputMethod !== 'ccd'
    };
  }

  const directLigand = normalizeComponentSequence('ligand', task.ligand_smiles || '');
  if (directLigand) {
    return { smiles: directLigand, isSmiles: true };
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

function readFirstFiniteMetric(data: Record<string, unknown>, paths: string[]): number | null {
  for (const path of paths) {
    const value = readObjectPath(data, path);
    if (typeof value === 'number' && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function normalizeProbability(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

type MetricTone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';
type SortKey = 'plddt' | 'iptm' | 'pae' | 'submitted' | 'backend' | 'seed' | 'duration';
type SortDirection = 'asc' | 'desc';
type SubmittedWithinDaysOption = 'all' | '1' | '7' | '30' | '90';
type SeedFilterOption = 'all' | 'with_seed' | 'without_seed';
type StructureSearchMode = 'exact' | 'substructure';

const TASKS_PAGE_FILTERS_STORAGE_KEY = 'vbio:tasks-page-filters:v1';
const TASK_SORT_KEYS: SortKey[] = ['plddt', 'iptm', 'pae', 'submitted', 'backend', 'seed', 'duration'];
const TASK_SORT_DIRECTIONS: SortDirection[] = ['asc', 'desc'];
const TASK_SUBMITTED_WINDOW_OPTIONS: SubmittedWithinDaysOption[] = ['all', '1', '7', '30', '90'];
const TASK_SEED_FILTER_OPTIONS: SeedFilterOption[] = ['all', 'with_seed', 'without_seed'];
const TASK_STRUCTURE_SEARCH_MODES: StructureSearchMode[] = ['exact', 'substructure'];
const TASK_PAGE_SIZE_OPTIONS = [8, 12, 20, 50];

interface TaskConfidenceMetrics {
  plddt: number | null;
  iptm: number | null;
  pae: number | null;
}

interface TaskMetricContext {
  chainIds: string[];
  targetChainId: string | null;
  ligandChainId: string | null;
}

interface WorkspacePairPreference {
  targetChainId: string | null;
  ligandChainId: string | null;
}

interface TaskSelectionContext extends TaskMetricContext {
  ligandSmiles: string;
  ligandIsSmiles: boolean;
  ligandComponentCount: number;
  ligandSequence: string;
  ligandSequenceType: InputComponent['type'] | null;
}

interface TaskListRow {
  task: ProjectTask;
  metrics: TaskConfidenceMetrics;
  submittedTs: number;
  backendValue: string;
  durationValue: number | null;
  ligandSmiles: string;
  ligandIsSmiles: boolean;
  ligandAtomPlddts: number[] | null;
  ligandSequence: string;
  ligandSequenceType: InputComponent['type'] | null;
  ligandResiduePlddts: number[] | null;
}

function toneForPlddt(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value >= 90) return 'excellent';
  if (value >= 70) return 'good';
  if (value >= 50) return 'medium';
  return 'low';
}

function ligandSequenceDensityClass(length: number): 'is-short' | 'is-medium' | 'is-long' | 'is-xlong' {
  if (length <= 26) return 'is-short';
  if (length <= 56) return 'is-medium';
  if (length <= 110) return 'is-long';
  return 'is-xlong';
}

function residuesPerLineForLength(length: number): number {
  if (length <= 30) return 10;
  if (length <= 90) return 11;
  if (length <= 180) return 12;
  return 13;
}

function splitSequenceNodesIntoBalancedLines<T>(nodes: T[]): T[][] {
  if (nodes.length === 0) return [];
  const preferredPerLine = residuesPerLineForLength(nodes.length);
  const lineCount = Math.max(1, Math.ceil(nodes.length / preferredPerLine));
  const baseSize = Math.floor(nodes.length / lineCount);
  const remainder = nodes.length % lineCount;
  const lines: T[][] = [];
  let cursor = 0;
  for (let i = 0; i < lineCount; i += 1) {
    const size = baseSize + (i < remainder ? 1 : 0);
    lines.push(nodes.slice(cursor, cursor + size));
    cursor += size;
  }
  return lines;
}

function ligandSequenceHeightClass(lineCount: number): 'is-height-normal' | 'is-height-tall' | 'is-height-xtall' {
  if (lineCount <= 5) return 'is-height-normal';
  if (lineCount <= 10) return 'is-height-tall';
  return 'is-height-xtall';
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

  const expanded: number[] = [];
  for (let i = 0; i < sequenceLength; i += 1) {
    const mapped = Math.floor((i * series.length) / sequenceLength);
    expanded.push(series[Math.min(series.length - 1, Math.max(0, mapped))]);
  }
  return expanded;
}

function toneForIptm(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  const normalized = value <= 1 ? value : value / 100;
  if (normalized >= 0.8) return 'excellent';
  if (normalized >= 0.6) return 'good';
  if (normalized >= 0.4) return 'medium';
  return 'low';
}

function toneForPae(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value <= 5) return 'excellent';
  if (value <= 10) return 'good';
  if (value <= 20) return 'medium';
  return 'low';
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}

function readPairIptmForChains(
  confidence: Record<string, unknown>,
  chainA: string | null,
  chainB: string | null,
  fallbackChainIds: string[]
): number | null {
  if (!chainA || !chainB) return null;

  const pairMap = readObjectPath(confidence, 'pair_chains_iptm');
  if (pairMap && typeof pairMap === 'object' && !Array.isArray(pairMap)) {
    const byChain = pairMap as Record<string, unknown>;
    const rowA = byChain[chainA];
    const rowB = byChain[chainB];
    const v1 =
      rowA && typeof rowA === 'object' && !Array.isArray(rowA)
        ? normalizeProbability(toFiniteNumber((rowA as Record<string, unknown>)[chainB]))
        : null;
    const v2 =
      rowB && typeof rowB === 'object' && !Array.isArray(rowB)
        ? normalizeProbability(toFiniteNumber((rowB as Record<string, unknown>)[chainA]))
        : null;
    if (v1 !== null || v2 !== null) {
      return Math.max(v1 ?? Number.NEGATIVE_INFINITY, v2 ?? Number.NEGATIVE_INFINITY);
    }
  }

  const pairMatrix = readObjectPath(confidence, 'chain_pair_iptm');
  if (!Array.isArray(pairMatrix)) return null;
  const chainIdsRaw = readObjectPath(confidence, 'chain_ids');
  const chainIds =
    Array.isArray(chainIdsRaw) && chainIdsRaw.every((item) => typeof item === 'string')
      ? (chainIdsRaw as string[])
      : fallbackChainIds;
  const i = chainIds.findIndex((item) => item === chainA);
  const j = chainIds.findIndex((item) => item === chainB);
  if (i < 0 || j < 0) return null;
  const rowI = pairMatrix[i];
  const rowJ = pairMatrix[j];
  const m1 = Array.isArray(rowI) ? normalizeProbability(toFiniteNumber(rowI[j])) : null;
  const m2 = Array.isArray(rowJ) ? normalizeProbability(toFiniteNumber(rowJ[i])) : null;
  if (m1 !== null || m2 !== null) {
    return Math.max(m1 ?? Number.NEGATIVE_INFINITY, m2 ?? Number.NEGATIVE_INFINITY);
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

function resolveTaskSelectionContext(task: ProjectTask, workspacePreference?: WorkspacePairPreference): TaskSelectionContext {
  const taskProperties = task.properties && typeof task.properties === 'object' ? task.properties : null;
  const rawTarget = taskProperties && typeof taskProperties.target === 'string' ? taskProperties.target.trim().toUpperCase() : '';
  const rawLigand = taskProperties && typeof taskProperties.ligand === 'string' ? taskProperties.ligand.trim().toUpperCase() : '';
  const preferredTarget = String(workspacePreference?.targetChainId || '')
    .trim()
    .toUpperCase();
  const preferredLigand = String(workspacePreference?.ligandChainId || '')
    .trim()
    .toUpperCase();
  const targetCandidate = preferredTarget || rawTarget;
  const ligandCandidate = preferredLigand || rawLigand;
  const activeComponents = readTaskComponents(task).filter((item) => Boolean(item.sequence.trim()));
  const ligandComponentCount = activeComponents.filter((item) => item.type === 'ligand').length;

  if (activeComponents.length === 0) {
    const ligand = readTaskPrimaryLigand(task, [], null);
    const fallbackChainIds = Array.from(new Set([targetCandidate, ligandCandidate].filter(Boolean)));
    return {
      chainIds: fallbackChainIds,
      targetChainId: targetCandidate || null,
      ligandChainId: ligandCandidate || null,
      ligandSmiles: ligand.smiles,
      ligandIsSmiles: ligand.isSmiles,
      ligandComponentCount: ligand.smiles ? 1 : 0,
      ligandSequence: '',
      ligandSequenceType: null
    };
  }

  const chainAssignments = assignChainIdsForComponents(activeComponents);
  const chainIds = chainAssignments.flat().map((value) => value.toUpperCase());
  const validChainIds = new Set(chainIds);
  const fallbackTargetChainId = chainAssignments[0]?.[0] || null;
  const fallbackLigandChainId = chainAssignments[chainAssignments.length - 1]?.[0] || null;
  const normalizedFallbackTarget = fallbackTargetChainId ? fallbackTargetChainId.toUpperCase() : null;
  const normalizedFallbackLigand = fallbackLigandChainId ? fallbackLigandChainId.toUpperCase() : null;
  const targetChainId = targetCandidate && validChainIds.has(targetCandidate) ? targetCandidate : normalizedFallbackTarget;
  const ligandChainId = ligandCandidate && validChainIds.has(ligandCandidate) ? ligandCandidate : normalizedFallbackLigand;

  const chainToComponent = new Map<string, InputComponent>();
  chainAssignments.forEach((chainGroup, index) => {
    chainGroup.forEach((chainId) => {
      chainToComponent.set(chainId.toUpperCase(), activeComponents[index]);
    });
  });
  const selectedLigandComponent = ligandChainId ? chainToComponent.get(ligandChainId) || null : null;
  const ligand =
    selectedLigandComponent?.type === 'ligand'
      ? readTaskPrimaryLigand(task, activeComponents, selectedLigandComponent.id || null)
      : {
          smiles: '',
          isSmiles: false
        };
  const ligandSequence =
    selectedLigandComponent && isSequenceLigandType(selectedLigandComponent.type)
      ? normalizeComponentSequence(selectedLigandComponent.type, selectedLigandComponent.sequence || '')
      : '';
  const ligandSequenceType = selectedLigandComponent?.type || null;

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
  const iptmRaw = selectedPairIptm ?? readFirstFiniteMetric(confidence, ['iptm', 'ligand_iptm', 'protein_iptm']);
  const paeRaw = readFirstFiniteMetric(confidence, ['complex_pde', 'complex_pae', 'pae']);
  const mergedPlddt = selectedLigandPlddt ?? plddtRaw;
  return {
    plddt: mergedPlddt === null ? null : mergedPlddt <= 1 ? mergedPlddt * 100 : mergedPlddt,
    iptm: normalizeProbability(iptmRaw),
    pae: paeRaw
  };
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
    return compactCandidate.endsWith(compactPreferred);
  }
  return false;
}

function readTaskLigandAtomPlddtsFromChainMap(
  value: unknown,
  preferredLigandChainId: string | null,
  allowFallbackToAnyChain: boolean
): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const map = value as Record<string, unknown>;
  const entries = Object.entries(map);
  if (entries.length === 0) return null;

  const preferred = preferredLigandChainId ? normalizeChainKey(preferredLigandChainId) : '';
  if (preferred) {
    for (const [key, chainValues] of entries) {
      if (!chainKeysMatch(key, preferred)) continue;
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length > 0) {
        return parsed;
      }
    }
  }

  if (allowFallbackToAnyChain) {
    for (const [, chainValues] of entries) {
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length > 0) {
        return parsed;
      }
    }
  }
  return null;
}

function readTaskResiduePlddtsFromChainMap(
  value: unknown,
  preferredLigandChainId: string | null
): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const map = value as Record<string, unknown>;
  const entries = Object.entries(map);
  if (entries.length === 0) return null;

  const preferred = preferredLigandChainId ? normalizeChainKey(preferredLigandChainId) : '';
  if (preferred) {
    for (const [key, chainValues] of entries) {
      if (!chainKeysMatch(key, preferred)) continue;
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length > 0) return parsed;
    }
  }
  return null;
}

function readTaskTokenPlddtsForChain(
  confidence: Record<string, unknown>,
  preferredLigandChainId: string | null
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

  const preferred = preferredLigandChainId ? normalizeChainKey(preferredLigandChainId) : '';
  if (!preferred) return null;
  for (const plddtCandidate of tokenPlddtCandidates) {
    const tokenPlddts = normalizeAtomPlddts(toFiniteNumberArray(plddtCandidate));
    if (tokenPlddts.length === 0) continue;

    for (const chainCandidate of tokenChainCandidates) {
      if (!Array.isArray(chainCandidate)) continue;
      if (chainCandidate.length !== tokenPlddts.length) continue;
      const tokenChains = chainCandidate.map((value) => normalizeChainKey(String(value || '')));
      if (tokenChains.some((value) => !value)) continue;

      const byChain = tokenPlddts.filter((_, index) => chainKeysMatch(tokenChains[index], preferred));
      if (byChain.length > 0) return byChain;
    }
  }
  return null;
}

function readTaskLigandResiduePlddts(task: ProjectTask, preferredLigandChainId: string | null): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  if (!preferredLigandChainId) return null;
  const chainMapCandidates: unknown[] = [
    confidence.residue_plddt_by_chain,
    confidence.chain_residue_plddt,
    confidence.chain_plddt,
    confidence.chain_plddts,
    confidence.plddt_by_chain,
    readObjectPath(confidence, 'residue_plddt_by_chain'),
    readObjectPath(confidence, 'chain_residue_plddt'),
    readObjectPath(confidence, 'chain_plddt'),
    readObjectPath(confidence, 'chain_plddts'),
    readObjectPath(confidence, 'plddt.by_chain')
  ];
  for (const candidate of chainMapCandidates) {
    const parsed = readTaskResiduePlddtsFromChainMap(candidate, preferredLigandChainId);
    if (parsed && parsed.length > 0) return parsed;
  }

  const tokenPlddts = readTaskTokenPlddtsForChain(confidence, preferredLigandChainId);
  if (tokenPlddts && tokenPlddts.length > 0) return tokenPlddts;

  return null;
}

function readTaskLigandAtomPlddts(
  task: ProjectTask,
  preferredLigandChainId: string | null = null,
  allowFlatFallback = true
): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
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
      preferredLigandChainId,
      !preferredLigandChainId
    );
    if (parsed && parsed.length > 0) {
      return parsed;
    }
  }
  if (preferredLigandChainId && hasChainMap && !allowFlatFallback) return null;
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

function formatMetric(value: number | null, fractionDigits: number): string {
  if (value === null) return '-';
  return value.toFixed(fractionDigits);
}

function compareNullableNumber(a: number | null, b: number | null, ascending: boolean): number {
  if (a === null && b === null) return 0;
  if (a === null) return 1;
  if (b === null) return -1;
  return ascending ? a - b : b - a;
}

function defaultSortDirection(key: SortKey): SortDirection {
  if (key === 'pae' || key === 'backend' || key === 'seed') return 'asc';
  return 'desc';
}

function nextSortDirection(current: SortDirection): SortDirection {
  return current === 'asc' ? 'desc' : 'asc';
}

function mapTaskState(raw: string): ProjectTask['task_state'] {
  const normalized = raw.toUpperCase();
  if (normalized === 'SUCCESS') return 'SUCCESS';
  if (normalized === 'FAILURE') return 'FAILURE';
  if (normalized === 'REVOKED') return 'REVOKED';
  if (normalized === 'PENDING') return 'QUEUED';
  return 'RUNNING';
}

async function waitForRuntimeTaskToStop(taskId: string, timeoutMs = 12000, intervalMs = 900): Promise<ProjectTask['task_state'] | null> {
  const normalizedTaskId = String(taskId || '').trim();
  if (!normalizedTaskId) return null;
  const deadline = Date.now() + Math.max(1000, timeoutMs);
  let lastState: ProjectTask['task_state'] | null = null;

  while (Date.now() < deadline) {
    try {
      const status = await getTaskStatus(normalizedTaskId);
      const mapped = mapTaskState(String(status.state || ''));
      lastState = mapped;
      if (mapped !== 'QUEUED' && mapped !== 'RUNNING') {
        return mapped;
      }
    } catch {
      // Status endpoint can be briefly unavailable while workers update state.
    }
    await new Promise<void>((resolve) => {
      window.setTimeout(() => resolve(), intervalMs);
    });
  }

  return lastState;
}

function readStatusText(status: { info?: Record<string, unknown>; state: string }): string {
  if (!status.info) return status.state;
  const s1 = status.info.status;
  const s2 = status.info.message;
  if (typeof s1 === 'string' && s1.trim()) return s1;
  if (typeof s2 === 'string' && s2.trim()) return s2;
  return status.state;
}

function taskStateLabel(state: ProjectTask['task_state']): string {
  if (state === 'QUEUED') return 'Queued';
  if (state === 'RUNNING') return 'Running';
  if (state === 'SUCCESS') return 'Success';
  if (state === 'FAILURE') return 'Failed';
  if (state === 'REVOKED') return 'Revoked';
  return 'Draft';
}

function taskStateTone(state: ProjectTask['task_state']): 'draft' | 'queued' | 'running' | 'success' | 'failure' | 'revoked' {
  if (state === 'QUEUED') return 'queued';
  if (state === 'RUNNING') return 'running';
  if (state === 'SUCCESS') return 'success';
  if (state === 'FAILURE') return 'failure';
  if (state === 'REVOKED') return 'revoked';
  return 'draft';
}

function normalizeStatusToken(value: string): string {
  return value.trim().toLowerCase().replace(/[_\s-]+/g, '');
}

function backendLabel(value: string): string {
  if (value === 'alphafold3') return 'AlphaFold3';
  if (value === 'boltz') return 'Boltz-2';
  return value ? value.toUpperCase() : 'Unknown';
}

function parseNumberOrNull(value: string): number | null {
  const trimmed = value.trim();
  if (!trimmed) return null;
  const parsed = Number(trimmed);
  return Number.isFinite(parsed) ? parsed : null;
}

function normalizePlddtThreshold(value: number | null): number | null {
  if (value === null) return null;
  if (value >= 0 && value <= 1) return value * 100;
  return value;
}

function normalizeIptmThreshold(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

function normalizeSmilesForSearch(value: string): string {
  return value.trim().replace(/\s+/g, '');
}

function hasSubstructureMatchPayload(value: unknown): boolean {
  if (typeof value === 'string') {
    const trimmed = value.trim();
    if (!trimmed || trimmed === '[]' || trimmed === '{}' || trimmed === 'null') return false;
    try {
      const parsed = JSON.parse(trimmed) as unknown;
      if (Array.isArray(parsed)) return parsed.length > 0;
      if (parsed && typeof parsed === 'object') return Object.keys(parsed as Record<string, unknown>).length > 0;
      return Boolean(parsed);
    } catch {
      return trimmed !== '-1';
    }
  }
  if (Array.isArray(value)) return value.length > 0;
  if (value && typeof value === 'object') return Object.keys(value as Record<string, unknown>).length > 0;
  return Boolean(value);
}

function shouldShowRunNote(state: ProjectTask['task_state'], note: string): boolean {
  const trimmed = note.trim();
  if (!trimmed) return false;
  const normalizedNote = normalizeStatusToken(trimmed);
  const normalizedState = normalizeStatusToken(state);
  if (normalizedNote === normalizedState) return false;
  const label = taskStateLabel(state);
  if (normalizedNote === normalizeStatusToken(label)) return false;
  return true;
}

interface TaskLigandSequencePreviewProps {
  sequence: string;
  residuePlddts: number[] | null;
}

function TaskLigandSequencePreview({ sequence, residuePlddts }: TaskLigandSequencePreviewProps) {
  const residues = sequence.trim().toUpperCase().split('');
  const densityClass = ligandSequenceDensityClass(residues.length);
  const nodes = useMemo(() => {
    return residues.map((rawResidue, index) => {
      const residue = /^[A-Z]$/.test(rawResidue) ? rawResidue : '?';
      const confidence = residuePlddts?.[index] ?? null;
      const tone = toneForPlddt(confidence);
      return {
        index,
        residue,
        confidence,
        tone
      };
    });
  }, [residues, residuePlddts]);
  const lines = useMemo(() => splitSequenceNodesIntoBalancedLines(nodes), [nodes]);
  const heightClass = ligandSequenceHeightClass(lines.length);

  if (nodes.length === 0) {
    return (
      <div className={`task-ligand-sequence ${densityClass}`}>
        <div className="task-ligand-thumb task-ligand-thumb-empty">
          <span className="muted small">No sequence</span>
        </div>
      </div>
    );
  }

  return (
    <div className={`task-ligand-sequence ${densityClass} ${heightClass}`}>
      <div className="task-ligand-sequence-lines">
        {lines.map((line, rowIndex) => (
          <div className="task-ligand-sequence-line" key={`ligand-seq-row-${rowIndex}`}>
            <span className="task-ligand-sequence-line-index left" aria-hidden="true">
              {line[0]?.index + 1}
            </span>
            <span className="task-ligand-sequence-line-track">
              {line.map((item, colIndex) => {
                const confidenceText = item.confidence === null ? '-' : item.confidence.toFixed(1);
                const linkTone = colIndex > 0 ? line[colIndex - 1].tone : item.tone;
                const position = item.index + 1;
                return (
                  <span className="task-ligand-sequence-node" key={`ligand-seq-residue-${item.index}`}>
                    {colIndex > 0 && <span className={`task-ligand-sequence-link tone-${linkTone}`} aria-hidden="true" />}
                    <span
                      className={`task-ligand-sequence-residue tone-${item.tone}`}
                      title={`#${position} ${item.residue} | pLDDT ${confidenceText}`}
                    >
                      {item.residue}
                    </span>
                  </span>
                );
              })}
            </span>
            <span className="task-ligand-sequence-line-index right" aria-hidden="true">
              {line[line.length - 1]?.index + 1}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function sanitizeFileName(value: string): string {
  const trimmed = value.trim();
  const safe = trimmed.replace(/[\\/:*?"<>|]/g, '_');
  return safe || 'project';
}

function toBase64FromBytes(bytes: Uint8Array): string {
  let binary = '';
  const chunkSize = 0x8000;
  for (let i = 0; i < bytes.length; i += chunkSize) {
    const chunk = bytes.subarray(i, i + chunkSize);
    binary += String.fromCharCode(...chunk);
  }
  return btoa(binary);
}

interface LoadTaskDataOptions {
  silent?: boolean;
  showRefreshing?: boolean;
  preferBackendStatus?: boolean;
  forceRefetch?: boolean;
}

const SILENT_CACHE_SYNC_WINDOW_MS = 30000;

export function ProjectTasksPage() {
  const { projectId = '' } = useParams();
  const navigate = useNavigate();
  const { session } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<ProjectTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [exportingExcel, setExportingExcel] = useState(false);
  const [openingTaskId, setOpeningTaskId] = useState<string | null>(null);
  const [deletingTaskId, setDeletingTaskId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [sortKey, setSortKey] = useState<SortKey>('submitted');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');
  const [taskSearch, setTaskSearch] = useState('');
  const [stateFilter, setStateFilter] = useState<'all' | ProjectTask['task_state']>('all');
  const [backendFilter, setBackendFilter] = useState<'all' | string>('all');
  const [showAdvancedFilters, setShowAdvancedFilters] = useState(false);
  const [submittedWithinDays, setSubmittedWithinDays] = useState<SubmittedWithinDaysOption>('all');
  const [seedFilter, setSeedFilter] = useState<SeedFilterOption>('all');
  const [failureOnly, setFailureOnly] = useState(false);
  const [minPlddt, setMinPlddt] = useState('');
  const [minIptm, setMinIptm] = useState('');
  const [maxPae, setMaxPae] = useState('');
  const [structureSearchMode, setStructureSearchMode] = useState<StructureSearchMode>('exact');
  const [structureSearchQuery, setStructureSearchQuery] = useState('');
  const [structureSearchMatches, setStructureSearchMatches] = useState<Record<string, boolean>>({});
  const [structureSearchLoading, setStructureSearchLoading] = useState(false);
  const [structureSearchError, setStructureSearchError] = useState<string | null>(null);
  const [pageSize, setPageSize] = useState<number>(12);
  const [page, setPage] = useState<number>(1);
  const [filtersHydrated, setFiltersHydrated] = useState(false);
  const loadSeqRef = useRef(0);
  const loadInFlightRef = useRef(false);
  const lastFullFetchTsRef = useRef(0);
  const projectRef = useRef<Project | null>(null);
  const tasksRef = useRef<ProjectTask[]>([]);
  const resultHydrationInFlightRef = useRef<Set<string>>(new Set());
  const resultHydrationDoneRef = useRef<Set<string>>(new Set());
  const resultHydrationAttemptsRef = useRef<Map<string, number>>(new Map());

  useEffect(() => {
    projectRef.current = project;
  }, [project]);

  useEffect(() => {
    tasksRef.current = sanitizeTaskRows(tasks);
  }, [tasks]);

  const syncRuntimeTasks = useCallback(async (projectRow: Project, taskRows: ProjectTask[]) => {
    const safeTaskRows = sanitizeTaskRows(taskRows);
    const runtimeRows = safeTaskRows.filter(
      (row) => Boolean(row.task_id) && (row.task_state === 'QUEUED' || row.task_state === 'RUNNING')
    );
    if (runtimeRows.length === 0) {
      return {
        project: projectRow,
        taskRows: sortProjectTasks(safeTaskRows)
      };
    }

    const checks = await Promise.allSettled(runtimeRows.map((row) => getTaskStatus(row.task_id)));
    let nextProject = projectRow;
    let nextTaskRows = [...safeTaskRows];

    for (let i = 0; i < checks.length; i += 1) {
      const result = checks[i];
      if (result.status !== 'fulfilled') continue;

      const runtimeTask = runtimeRows[i];
      const taskState = mapTaskState(result.value.state);
      const statusText = readStatusText(result.value);
      const errorText = taskState === 'FAILURE' ? statusText : '';
      const terminal = taskState === 'SUCCESS' || taskState === 'FAILURE' || taskState === 'REVOKED';
      const completedAt = terminal ? runtimeTask.completed_at || new Date().toISOString() : null;
      const submittedAt = runtimeTask.submitted_at || (nextProject.task_id === runtimeTask.task_id ? nextProject.submitted_at : null);
      const durationSeconds =
        terminal && submittedAt
          ? (() => {
              const duration = (new Date(completedAt || Date.now()).getTime() - new Date(submittedAt).getTime()) / 1000;
              return Number.isFinite(duration) && duration >= 0 ? duration : null;
            })()
          : null;

      const taskNeedsPatch =
        runtimeTask.task_state !== taskState ||
        (runtimeTask.status_text || '') !== statusText ||
        (runtimeTask.error_text || '') !== errorText ||
        runtimeTask.completed_at !== completedAt ||
        runtimeTask.duration_seconds !== durationSeconds;

      if (taskNeedsPatch) {
        const taskPatch: Partial<ProjectTask> = {
          task_state: taskState,
          status_text: statusText,
          error_text: errorText,
          completed_at: completedAt,
          duration_seconds: durationSeconds
        };
        const fallbackTask: ProjectTask = {
          ...runtimeTask,
          ...taskPatch
        };
        const patchedTask = await updateProjectTask(runtimeTask.id, taskPatch).catch(() => fallbackTask);
        const nextTask = isProjectTaskRow(patchedTask) ? patchedTask : fallbackTask;
        nextTaskRows = nextTaskRows.map((row) => (row.id === runtimeTask.id ? nextTask : row));
      }

      if (nextProject.task_id === runtimeTask.task_id) {
        const projectNeedsPatch =
          nextProject.task_state !== taskState ||
          (nextProject.status_text || '') !== statusText ||
          (nextProject.error_text || '') !== errorText ||
          nextProject.completed_at !== completedAt ||
          nextProject.duration_seconds !== durationSeconds;
        if (projectNeedsPatch) {
          const projectPatch: Partial<Project> = {
            task_state: taskState,
            status_text: statusText,
            error_text: errorText,
            completed_at: completedAt,
            duration_seconds: durationSeconds
          };
          const fallbackProject: Project = {
            ...nextProject,
            ...projectPatch
          };
          const patchedProject = await updateProject(nextProject.id, projectPatch).catch(() => fallbackProject);
          nextProject = isProjectRow(patchedProject) ? patchedProject : fallbackProject;
        }
      }
    }

    return {
      project: nextProject,
      taskRows: sortProjectTasks(sanitizeTaskRows(nextTaskRows))
    };
  }, []);

  const hydrateTaskMetricsFromResults = useCallback(async (projectRow: Project, taskRows: ProjectTask[]) => {
    const safeTaskRows = sanitizeTaskRows(taskRows);
    const candidates = safeTaskRows
      .filter((row) => {
        const taskId = String(row.task_id || '').trim();
        if (!taskId || row.task_state !== 'SUCCESS') return false;
        const selection = resolveTaskSelectionContext(row);
        const needsSummaryHydration = !hasTaskSummaryMetrics(row);
        const needsLigandAtomHydration =
          Boolean(
            selection.ligandSmiles &&
              selection.ligandIsSmiles &&
              !hasTaskLigandAtomPlddts(row, selection.ligandChainId, selection.ligandComponentCount === 1)
          );
        if (!needsSummaryHydration && !needsLigandAtomHydration) {
          resultHydrationDoneRef.current.add(taskId);
          return false;
        }
        if (resultHydrationDoneRef.current.has(taskId)) return false;
        if (resultHydrationInFlightRef.current.has(taskId)) return false;
        const attempts = resultHydrationAttemptsRef.current.get(taskId) || 0;
        return attempts < 2;
      })
      .slice(0, 2);

    if (candidates.length === 0) {
      return {
        project: projectRow,
        taskRows: safeTaskRows
      };
    }

    let nextProject = projectRow;
    let nextTaskRows = [...safeTaskRows];

    for (const task of candidates) {
      const taskId = String(task.task_id || '').trim();
      if (!taskId) continue;
      const attempts = resultHydrationAttemptsRef.current.get(taskId) || 0;
      resultHydrationAttemptsRef.current.set(taskId, attempts + 1);
      resultHydrationInFlightRef.current.add(taskId);

      try {
        const resultBlob = await downloadResultBlob(taskId);
        const parsed = await parseResultBundle(resultBlob);
        if (!parsed) continue;

        const taskPatch: Partial<ProjectTask> = {
          confidence: parsed.confidence || {},
          affinity: parsed.affinity || {},
          structure_name: parsed.structureName || task.structure_name || ''
        };
        const fallbackTask: ProjectTask = {
          ...task,
          ...taskPatch
        };
        const patchedTask = await updateProjectTask(task.id, taskPatch).catch(() => fallbackTask);
        const nextTask = isProjectTaskRow(patchedTask) ? patchedTask : fallbackTask;
        nextTaskRows = nextTaskRows.map((row) => (row.id === task.id ? nextTask : row));

        if (nextProject.task_id === taskId) {
          const projectPatch: Partial<Project> = {
            confidence: taskPatch.confidence || {},
            affinity: taskPatch.affinity || {},
            structure_name: taskPatch.structure_name || ''
          };
          const fallbackProject: Project = {
            ...nextProject,
            ...projectPatch
          };
          const patchedProject = await updateProject(nextProject.id, projectPatch).catch(() => fallbackProject);
          nextProject = isProjectRow(patchedProject) ? patchedProject : fallbackProject;
        }

        resultHydrationDoneRef.current.add(taskId);
      } catch {
        // Ignore transient parse/download failures; retry is bounded by attempt count.
      } finally {
        resultHydrationInFlightRef.current.delete(taskId);
      }
    }

    return {
      project: nextProject,
      taskRows: sortProjectTasks(sanitizeTaskRows(nextTaskRows))
    };
  }, []);

  const loadData = useCallback(
    async (options?: LoadTaskDataOptions) => {
      if (loadInFlightRef.current) return;
      const loadSeq = ++loadSeqRef.current;
      loadInFlightRef.current = true;
      const silent = Boolean(options?.silent);
      const showRefreshing = silent && options?.showRefreshing !== false;
      const preferBackendStatus = options?.preferBackendStatus !== false;
      const forceRefetch = Boolean(options?.forceRefetch);
      if (showRefreshing) {
        setRefreshing(true);
      } else if (!silent) {
        setLoading(true);
      }
      if (!silent) {
        setError(null);
      }
      try {
        const now = Date.now();
        const cachedProject = projectRef.current;
        const cachedTasks = sanitizeTaskRows(tasksRef.current);
        const withinCacheWindow = now - lastFullFetchTsRef.current <= SILENT_CACHE_SYNC_WINDOW_MS;
        const canUseCachedSync =
          silent && !forceRefetch && preferBackendStatus && withinCacheWindow && cachedProject && cachedTasks.length > 0;

        if (canUseCachedSync) {
          const synced = await syncRuntimeTasks(cachedProject, cachedTasks);
          if (loadSeqRef.current !== loadSeq) return;
          setProject(synced.project);
          setTasks(sanitizeTaskRows(synced.taskRows));
          void (async () => {
            try {
              const hydrated = await hydrateTaskMetricsFromResults(synced.project, synced.taskRows);
              if (loadSeqRef.current !== loadSeq) return;
              setProject(hydrated.project);
              setTasks(sanitizeTaskRows(hydrated.taskRows));
            } catch {
              // Keep cached sync state if result hydration fails.
            }
          })();
          return;
        }

        const [projectRow, taskRows] = await Promise.all([getProjectById(projectId), listProjectTasksForList(projectId)]);
        if (!projectRow || projectRow.deleted_at) {
          throw new Error('Project not found or already deleted.');
        }
        if (session && projectRow.user_id !== session.userId) {
          throw new Error('You do not have permission to access this project.');
        }

        lastFullFetchTsRef.current = Date.now();
        const sortedTaskRows = sortProjectTasks(sanitizeTaskRows(taskRows));
        if (loadSeqRef.current !== loadSeq) return;
        setProject(projectRow);
        setTasks(sanitizeTaskRows(sortedTaskRows));

        if (!preferBackendStatus) {
          return;
        }

        void (async () => {
          try {
            const synced = await syncRuntimeTasks(projectRow, sortedTaskRows);
            if (loadSeqRef.current !== loadSeq) return;
            setProject(synced.project);
            setTasks(sanitizeTaskRows(synced.taskRows));

            const hydrated = await hydrateTaskMetricsFromResults(synced.project, synced.taskRows);
            if (loadSeqRef.current !== loadSeq) return;
            setProject(hydrated.project);
            setTasks(sanitizeTaskRows(hydrated.taskRows));
          } catch {
            // Keep base rows rendered; background refinement is best-effort.
          }
        })();
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to load task history.');
      } finally {
        if (showRefreshing) {
          setRefreshing(false);
        } else if (!silent) {
          setLoading(false);
        }
        loadInFlightRef.current = false;
      }
    },
    [projectId, session, syncRuntimeTasks, hydrateTaskMetricsFromResults]
  );

  useEffect(() => {
    void loadData();
  }, [loadData]);

  useEffect(() => {
    const onFocus = () => {
      void loadData({ silent: true, showRefreshing: false, forceRefetch: true });
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void loadData({ silent: true, showRefreshing: false, forceRefetch: true });
      }
    };
    window.addEventListener('focus', onFocus);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.removeEventListener('focus', onFocus);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [loadData]);

  const hasActiveRuntime = useMemo(
    () =>
      tasks.some(
        (row) => isProjectTaskRow(row) && Boolean(row.task_id) && (row.task_state === 'QUEUED' || row.task_state === 'RUNNING')
      ),
    [tasks]
  );

  useEffect(() => {
    if (!hasActiveRuntime) return;
    const timer = window.setInterval(() => {
      void loadData({ silent: true, showRefreshing: false });
    }, 4000);
    return () => window.clearInterval(timer);
  }, [hasActiveRuntime, loadData]);

  const taskCountText = useMemo(() => `${sanitizeTaskRows(tasks).length} tasks`, [tasks]);
  const currentTaskRow = useMemo(() => {
    if (!project) return null;
    const currentRuntimeTaskId = String(project.task_id || '').trim();
    if (currentRuntimeTaskId) {
      const matchedRuntime = tasks.find(
        (row) => isProjectTaskRow(row) && String(row.task_id || '').trim() === currentRuntimeTaskId
      );
      if (matchedRuntime) return matchedRuntime;
    }
    const latestDraft = tasks.find(
      (row) => isProjectTaskRow(row) && row.task_state === 'DRAFT' && !String(row.task_id || '').trim()
    );
    if (latestDraft) return latestDraft;
    return tasks.find((row) => isProjectTaskRow(row)) || null;
  }, [project, tasks]);
  const backToCurrentTaskHref = useMemo(() => {
    if (!project) return '/projects';
    const params = new URLSearchParams();
    const currentTaskId = String(currentTaskRow?.task_id || project.task_id || '').trim();
    params.set('tab', currentTaskId ? 'results' : 'components');
    if (currentTaskRow?.id) {
      params.set('task_row_id', currentTaskRow.id);
    }
    return `/projects/${project.id}?${params.toString()}`;
  }, [project, currentTaskRow]);
  const workspacePairPreference = useMemo<WorkspacePairPreference>(() => {
    if (!project) {
      return {
        targetChainId: null,
        ligandChainId: null
      };
    }

    const savedConfig = loadProjectInputConfig(project.id);
    const savedTarget = String(savedConfig?.properties?.target || '')
      .trim()
      .toUpperCase();
    const savedLigand = String(savedConfig?.properties?.ligand || '')
      .trim()
      .toUpperCase();
    const currentProps =
      currentTaskRow?.properties && typeof currentTaskRow.properties === 'object' ? currentTaskRow.properties : null;
    const currentTarget = String(currentProps?.target || '')
      .trim()
      .toUpperCase();
    const currentLigand = String(currentProps?.ligand || '')
      .trim()
      .toUpperCase();

    return {
      targetChainId: savedTarget || currentTarget || null,
      ligandChainId: savedLigand || currentLigand || null
    };
  }, [project, currentTaskRow]);

  const taskRows = useMemo<TaskListRow[]>(() => {
    return sanitizeTaskRows(tasks).map((task) => {
      const submittedTs = new Date(task.submitted_at || task.created_at).getTime();
      const durationValue = typeof task.duration_seconds === 'number' && Number.isFinite(task.duration_seconds) ? task.duration_seconds : null;
      const selection = resolveTaskSelectionContext(task, workspacePairPreference);
      const ligandAtomPlddts = readTaskLigandAtomPlddts(
        task,
        selection.ligandChainId,
        selection.ligandComponentCount === 1
      );
      const ligandResiduePlddtsRaw =
        selection.ligandSequence && isSequenceLigandType(selection.ligandSequenceType)
          ? readTaskLigandResiduePlddts(task, selection.ligandChainId)
          : null;
      const ligandResiduePlddts = alignConfidenceSeriesToLength(
        ligandResiduePlddtsRaw,
        selection.ligandSequence.length,
        null
      );
      const metrics = readTaskConfidenceMetrics(task, selection);
      const ligandMeanPlddt = mean(ligandAtomPlddts);
      const ligandSequenceMeanPlddt = mean(ligandResiduePlddts);
      const plddt = metrics.plddt !== null ? metrics.plddt : ligandMeanPlddt ?? ligandSequenceMeanPlddt;
      return {
        task,
        metrics: {
          ...metrics,
          plddt
        },
        submittedTs,
        backendValue: String(task.backend || '').trim().toLowerCase(),
        durationValue,
        ligandSmiles: selection.ligandSmiles,
        ligandIsSmiles: selection.ligandIsSmiles,
        ligandAtomPlddts,
        ligandSequence: selection.ligandSequence,
        ligandSequenceType: selection.ligandSequenceType,
        ligandResiduePlddts
      };
    });
  }, [tasks, workspacePairPreference]);
  const backendOptions = useMemo(
    () =>
      Array.from(new Set(taskRows.map((row) => row.backendValue).filter(Boolean))).sort((a, b) =>
        a.localeCompare(b)
      ),
    [taskRows]
  );
  const advancedFilterCount = useMemo(() => {
    let count = 0;
    if (submittedWithinDays !== 'all') count += 1;
    if (seedFilter !== 'all') count += 1;
    if (failureOnly) count += 1;
    if (minPlddt.trim()) count += 1;
    if (minIptm.trim()) count += 1;
    if (maxPae.trim()) count += 1;
    if (structureSearchQuery.trim()) count += 1;
    return count;
  }, [submittedWithinDays, seedFilter, failureOnly, minPlddt, minIptm, maxPae, structureSearchQuery]);
  const clearAdvancedFilters = () => {
    setSubmittedWithinDays('all');
    setSeedFilter('all');
    setFailureOnly(false);
    setMinPlddt('');
    setMinIptm('');
    setMaxPae('');
    setStructureSearchMode('exact');
    setStructureSearchQuery('');
    setStructureSearchMatches({});
    setStructureSearchError(null);
  };

  useEffect(() => {
    if (typeof window === 'undefined') {
      setFiltersHydrated(true);
      return;
    }
    try {
      const raw = window.localStorage.getItem(TASKS_PAGE_FILTERS_STORAGE_KEY);
      if (!raw) return;
      const saved = JSON.parse(raw) as Record<string, unknown>;
      if (typeof saved.taskSearch === 'string') setTaskSearch(saved.taskSearch);
      if (
        typeof saved.stateFilter === 'string' &&
        ['all', 'DRAFT', 'QUEUED', 'RUNNING', 'SUCCESS', 'FAILURE', 'REVOKED'].includes(saved.stateFilter)
      ) {
        setStateFilter(saved.stateFilter as 'all' | ProjectTask['task_state']);
      }
      if (typeof saved.backendFilter === 'string' && saved.backendFilter.trim()) {
        setBackendFilter(saved.backendFilter.trim().toLowerCase());
      }
      if (typeof saved.sortKey === 'string' && TASK_SORT_KEYS.includes(saved.sortKey as SortKey)) {
        setSortKey(saved.sortKey as SortKey);
      }
      if (typeof saved.sortDirection === 'string' && TASK_SORT_DIRECTIONS.includes(saved.sortDirection as SortDirection)) {
        setSortDirection(saved.sortDirection as SortDirection);
      }
      if (typeof saved.showAdvancedFilters === 'boolean') {
        setShowAdvancedFilters(saved.showAdvancedFilters);
      }
      if (
        typeof saved.submittedWithinDays === 'string' &&
        TASK_SUBMITTED_WINDOW_OPTIONS.includes(saved.submittedWithinDays as SubmittedWithinDaysOption)
      ) {
        setSubmittedWithinDays(saved.submittedWithinDays as SubmittedWithinDaysOption);
      }
      if (typeof saved.seedFilter === 'string' && TASK_SEED_FILTER_OPTIONS.includes(saved.seedFilter as SeedFilterOption)) {
        setSeedFilter(saved.seedFilter as SeedFilterOption);
      }
      if (typeof saved.failureOnly === 'boolean') {
        setFailureOnly(saved.failureOnly);
      }
      if (typeof saved.minPlddt === 'string') setMinPlddt(saved.minPlddt);
      if (typeof saved.minIptm === 'string') setMinIptm(saved.minIptm);
      if (typeof saved.maxPae === 'string') setMaxPae(saved.maxPae);
      if (
        typeof saved.structureSearchMode === 'string' &&
        TASK_STRUCTURE_SEARCH_MODES.includes(saved.structureSearchMode as StructureSearchMode)
      ) {
        setStructureSearchMode(saved.structureSearchMode as StructureSearchMode);
      } else if (saved.structureSearchMode === 'off') {
        setStructureSearchMode('exact');
      }
      if (typeof saved.structureSearchQuery === 'string') {
        setStructureSearchQuery(saved.structureSearchQuery);
      }
      if (typeof saved.pageSize === 'number' && TASK_PAGE_SIZE_OPTIONS.includes(saved.pageSize)) {
        setPageSize(saved.pageSize);
      }
    } catch {
      // ignore malformed storage
    } finally {
      setFiltersHydrated(true);
    }
  }, []);

  useEffect(() => {
    if (!filtersHydrated || typeof window === 'undefined') return;
    const snapshot = {
      taskSearch,
      stateFilter,
      backendFilter,
      sortKey,
      sortDirection,
      showAdvancedFilters,
      submittedWithinDays,
      seedFilter,
      failureOnly,
      minPlddt,
      minIptm,
      maxPae,
      structureSearchMode,
      structureSearchQuery,
      pageSize
    };
    try {
      window.localStorage.setItem(TASKS_PAGE_FILTERS_STORAGE_KEY, JSON.stringify(snapshot));
    } catch {
      // ignore storage quota errors
    }
  }, [
    filtersHydrated,
    taskSearch,
    stateFilter,
    backendFilter,
    sortKey,
    sortDirection,
    showAdvancedFilters,
    submittedWithinDays,
    seedFilter,
    failureOnly,
    minPlddt,
    minIptm,
    maxPae,
    structureSearchMode,
    structureSearchQuery,
    pageSize
  ]);

  useEffect(() => {
    const mode = structureSearchMode;
    const query = structureSearchQuery.trim();
    if (!query) {
      setStructureSearchLoading(false);
      setStructureSearchError(null);
      setStructureSearchMatches({});
      return;
    }

    let cancelled = false;
    const run = async () => {
      setStructureSearchLoading(true);
      setStructureSearchError(null);

      try {
        if (mode === 'exact') {
          const normalizedQuery = normalizeSmilesForSearch(query);
          const nextMatches: Record<string, boolean> = {};
          taskRows.forEach((row) => {
            const candidate = normalizeSmilesForSearch(row.ligandSmiles);
            nextMatches[row.task.id] = Boolean(row.ligandIsSmiles && candidate && candidate === normalizedQuery);
          });
          if (!cancelled) {
            setStructureSearchMatches(nextMatches);
          }
          return;
        }

        const rdkit = await loadRDKitModule();
        if (cancelled) return;

        const queryMol = (typeof rdkit.get_qmol === 'function' ? rdkit.get_qmol(query) : null) || rdkit.get_mol(query);
        if (!queryMol) {
          throw new Error('Invalid SMARTS/SMILES query.');
        }

        const nextMatches: Record<string, boolean> = {};
        try {
          for (const row of taskRows) {
            if (!row.ligandIsSmiles || !row.ligandSmiles.trim()) {
              nextMatches[row.task.id] = false;
              continue;
            }
            const mol = rdkit.get_mol(row.ligandSmiles);
            if (!mol) {
              nextMatches[row.task.id] = false;
              continue;
            }
            try {
              let matched = false;
              if (typeof mol.get_substruct_match === 'function') {
                matched = hasSubstructureMatchPayload(mol.get_substruct_match(queryMol));
              }
              if (!matched && typeof mol.get_substruct_matches === 'function') {
                matched = hasSubstructureMatchPayload(mol.get_substruct_matches(queryMol));
              }
              nextMatches[row.task.id] = matched;
            } finally {
              mol.delete();
            }
          }
        } finally {
          queryMol.delete();
        }

        if (!cancelled) {
          setStructureSearchMatches(nextMatches);
        }
      } catch (err) {
        if (!cancelled) {
          setStructureSearchError(err instanceof Error ? err.message : 'Structure search failed.');
          setStructureSearchMatches({});
        }
      } finally {
        if (!cancelled) {
          setStructureSearchLoading(false);
        }
      }
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [taskRows, structureSearchMode, structureSearchQuery]);

  const filteredRows = useMemo(() => {
    const query = taskSearch.trim().toLowerCase();
    const structureSearchActive = Boolean(structureSearchQuery.trim());
    const applyStructureFilter = structureSearchActive && !structureSearchLoading && !structureSearchError;
    const submittedWindowMs = submittedWithinDays === 'all' ? null : Number(submittedWithinDays) * 24 * 60 * 60 * 1000;
    const submittedCutoff = submittedWindowMs === null ? null : Date.now() - submittedWindowMs;
    const minPlddtThreshold = normalizePlddtThreshold(parseNumberOrNull(minPlddt));
    const minIptmThreshold = normalizeIptmThreshold(parseNumberOrNull(minIptm));
    const maxPaeThreshold = parseNumberOrNull(maxPae);

  const filtered = taskRows.filter((row) => {
      const { task, metrics } = row;
      if (stateFilter !== 'all' && task.task_state !== stateFilter) return false;
      if (backendFilter !== 'all' && row.backendValue !== backendFilter) return false;
      if (seedFilter === 'with_seed' && (task.seed === null || task.seed === undefined)) return false;
      if (seedFilter === 'without_seed' && task.seed !== null && task.seed !== undefined) return false;
      if (failureOnly && task.task_state !== 'FAILURE' && !(task.error_text || '').trim()) return false;
      if (submittedCutoff !== null && (!Number.isFinite(row.submittedTs) || row.submittedTs < submittedCutoff)) return false;
      if (minPlddtThreshold !== null && (metrics.plddt === null || metrics.plddt < minPlddtThreshold)) return false;
      if (minIptmThreshold !== null && (metrics.iptm === null || metrics.iptm < minIptmThreshold)) return false;
      if (maxPaeThreshold !== null && (metrics.pae === null || metrics.pae > maxPaeThreshold)) return false;
      if (applyStructureFilter && !structureSearchMatches[task.id]) return false;
      if (!query) return true;

      const haystack = [
        task.task_state,
        task.backend,
        task.status_text,
        task.error_text,
        task.structure_name,
        task.seed ?? '',
        metrics.plddt ?? '',
        metrics.iptm ?? '',
        metrics.pae ?? ''
      ]
        .join(' ')
        .toLowerCase();
      return haystack.includes(query);
    });

    filtered.sort((a, b) => {
      if (sortKey === 'submitted') {
        return sortDirection === 'asc' ? a.submittedTs - b.submittedTs : b.submittedTs - a.submittedTs;
      }
      if (sortKey === 'plddt') {
        return compareNullableNumber(a.metrics.plddt, b.metrics.plddt, sortDirection === 'asc');
      }
      if (sortKey === 'iptm') {
        return compareNullableNumber(a.metrics.iptm, b.metrics.iptm, sortDirection === 'asc');
      }
      if (sortKey === 'pae') {
        return compareNullableNumber(a.metrics.pae, b.metrics.pae, sortDirection === 'asc');
      }
      if (sortKey === 'duration') {
        return compareNullableNumber(a.durationValue, b.durationValue, sortDirection === 'asc');
      }
      if (sortKey === 'seed') {
        return compareNullableNumber(a.task.seed, b.task.seed, sortDirection === 'asc');
      }
      const result = a.backendValue.localeCompare(b.backendValue);
      return sortDirection === 'asc' ? result : -result;
    });

    return filtered;
  }, [
    taskRows,
    sortKey,
    sortDirection,
    taskSearch,
    stateFilter,
    backendFilter,
    submittedWithinDays,
    seedFilter,
    failureOnly,
    minPlddt,
    minIptm,
    maxPae,
    structureSearchMode,
    structureSearchQuery,
    structureSearchLoading,
    structureSearchError,
    structureSearchMatches
  ]);

  const totalPages = useMemo(() => Math.max(1, Math.ceil(filteredRows.length / pageSize)), [filteredRows.length, pageSize]);
  const currentPage = Math.min(page, totalPages);
  const jumpToPage = useCallback(
    (rawValue: string) => {
      const parsed = Number(rawValue);
      if (!Number.isFinite(parsed)) return;
      setPage(Math.min(totalPages, Math.max(1, Math.floor(parsed))));
    },
    [totalPages]
  );
  const pagedRows = useMemo(() => {
    const start = (currentPage - 1) * pageSize;
    return filteredRows.slice(start, start + pageSize);
  }, [filteredRows, currentPage, pageSize]);

  useEffect(() => {
    setPage(1);
  }, [
    sortKey,
    sortDirection,
    pageSize,
    taskSearch,
    stateFilter,
    backendFilter,
    submittedWithinDays,
    seedFilter,
    failureOnly,
    minPlddt,
    minIptm,
    maxPae,
    structureSearchMode,
    structureSearchQuery
  ]);

  useEffect(() => {
    if (page > totalPages) setPage(totalPages);
  }, [page, totalPages]);

  const downloadExcel = useCallback(async () => {
    if (!project || filteredRows.length === 0) return;
    setExportingExcel(true);
    setError(null);
    try {
      const [{ default: ExcelJS }, rdkit] = await Promise.all([import('exceljs'), loadRDKitModule()]);
      const authoritativeTasks = await listProjectTasks(project.id);
      const authoritativeTaskMap = new Map(authoritativeTasks.map((task) => [task.id, task] as const));
      const imageWidthPx = 220;
      const imageHeightPx = 132;
      const rowHeightPt = Math.round((imageHeightPx * 72) / 96);
      const sheetName = (() => {
        const normalized = String(project.name || 'Tasks')
          .replace(/[\\/*?:[\]]/g, ' ')
          .trim();
        const truncated = normalized.slice(0, 31);
        return truncated || 'Tasks';
      })();

      const toPngBytesFromSvg = async (svg: string): Promise<Uint8Array> => {
        const svgBlob = new Blob([svg], { type: 'image/svg+xml;charset=utf-8' });
        const imageUrl = URL.createObjectURL(svgBlob);
        const image = await new Promise<HTMLImageElement>((resolve, reject) => {
          const img = new Image();
          img.onload = () => resolve(img);
          img.onerror = () => reject(new Error('Failed to decode SVG image.'));
          img.src = imageUrl;
        });
        try {
          const canvas = document.createElement('canvas');
          canvas.width = imageWidthPx;
          canvas.height = imageHeightPx;
          const ctx = canvas.getContext('2d');
          if (!ctx) {
            throw new Error('Canvas 2D context is unavailable.');
          }
          ctx.fillStyle = '#ffffff';
          ctx.fillRect(0, 0, imageWidthPx, imageHeightPx);
          ctx.drawImage(image, 0, 0, imageWidthPx, imageHeightPx);
          const pngBlob = await new Promise<Blob>((resolve, reject) => {
            canvas.toBlob((blob) => {
              if (!blob) {
                reject(new Error('Failed to encode PNG image.'));
                return;
              }
              resolve(blob);
            }, 'image/png');
          });
          return new Uint8Array(await pngBlob.arrayBuffer());
        } finally {
          URL.revokeObjectURL(imageUrl);
        }
      };

      const workbook = new ExcelJS.Workbook();
      const worksheet = workbook.addWorksheet(sheetName);
      worksheet.columns = [
        { header: '#', key: 'index', width: 6 },
        { header: 'Task Row ID', key: 'taskRowId', width: 38 },
        { header: 'Runtime Task ID', key: 'runtimeTaskId', width: 24 },
        { header: 'State', key: 'state', width: 12 },
        { header: 'Backend', key: 'backend', width: 12 },
        { header: 'Submitted', key: 'submitted', width: 22 },
        { header: 'Duration', key: 'duration', width: 12 },
        { header: 'pLDDT', key: 'plddt', width: 10 },
        { header: 'iPTM', key: 'iptm', width: 10 },
        { header: 'PAE', key: 'pae', width: 10 },
        { header: 'SMILES', key: 'smiles', width: 42 },
        { header: 'Ligand 2D (Confidence Color)', key: 'ligand2d', width: 36 }
      ];
      worksheet.getRow(1).font = { bold: true };

      let ligandSmilesRowCount = 0;
      let renderedImageCount = 0;
      let firstRenderError: string | null = null;

      for (let i = 0; i < filteredRows.length; i += 1) {
        const row = filteredRows[i];
        const { metrics } = row;
        const task = authoritativeTaskMap.get(row.task.id) || row.task;
        const selection = resolveTaskSelectionContext(task, workspacePairPreference);
        const ligandSmiles = selection.ligandSmiles;
        const ligandIsSmiles = selection.ligandIsSmiles;
        const ligandAtomPlddts =
          readTaskLigandAtomPlddts(task, selection.ligandChainId, selection.ligandComponentCount === 1) ??
          row.ligandAtomPlddts;
        const submittedText = formatDateTime(task.submitted_at || task.created_at);
        const runtimeTaskId = String(task.task_id || '').trim();
        let imageBytes: Uint8Array | null = null;
        if (ligandSmiles && ligandIsSmiles) {
          ligandSmilesRowCount += 1;
          try {
            const svg = renderLigand2DSvg(rdkit, {
              smiles: ligandSmiles,
              width: imageWidthPx,
              height: imageHeightPx,
              atomConfidences: ligandAtomPlddts,
              confidenceHint: metrics.plddt
            });
            imageBytes = await toPngBytesFromSvg(svg);
            renderedImageCount += 1;
          } catch (err) {
            imageBytes = null;
            if (!firstRenderError) {
              const reason = err instanceof Error && err.message ? err.message : 'unknown error';
              firstRenderError = `task ${task.id} (${reason})`;
            }
          }
        }
        const excelRow = worksheet.addRow({
          index: i + 1,
          taskRowId: task.id,
          runtimeTaskId,
          state: taskStateLabel(task.task_state),
          backend: backendLabel(task.backend || ''),
          submitted: submittedText,
          duration: formatDuration(task.duration_seconds),
          plddt: formatMetric(metrics.plddt, 1),
          iptm: formatMetric(metrics.iptm, 3),
          pae: formatMetric(metrics.pae, 2),
          smiles: ligandSmiles || '',
          ligand2d: imageBytes ? '' : '-'
        });
        if (imageBytes) {
          excelRow.height = rowHeightPt;
          const imageId = workbook.addImage({
            base64: `data:image/png;base64,${toBase64FromBytes(imageBytes)}`,
            extension: 'png'
          });
          worksheet.addImage(imageId, {
            tl: { col: 11, row: excelRow.number - 1 },
            ext: { width: imageWidthPx, height: imageHeightPx },
            editAs: 'oneCell'
          });
        }
      }
      if (ligandSmilesRowCount > 0 && renderedImageCount === 0) {
        throw new Error(
          firstRenderError
            ? `Ligand 2D rendering failed for all SMILES rows (${ligandSmilesRowCount}). First failure: ${firstRenderError}.`
            : `Ligand 2D rendering failed for all SMILES rows (${ligandSmilesRowCount}).`
        );
      }

      const workbookBuffer = await workbook.xlsx.writeBuffer();
      const blob = new Blob([workbookBuffer], {
        type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
      });
      const now = new Date();
      const timestamp = [
        now.getFullYear(),
        String(now.getMonth() + 1).padStart(2, '0'),
        String(now.getDate()).padStart(2, '0'),
        '_',
        String(now.getHours()).padStart(2, '0'),
        String(now.getMinutes()).padStart(2, '0'),
        String(now.getSeconds()).padStart(2, '0')
      ].join('');
      const fileName = `${sanitizeFileName(project.name)}_tasks_${timestamp}.xlsx`;
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = fileName;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      setError(err instanceof Error ? `Failed to export Excel: ${err.message}` : 'Failed to export Excel.');
    } finally {
      setExportingExcel(false);
    }
  }, [project, filteredRows, workspacePairPreference]);

  const openTask = async (task: ProjectTask) => {
    if (!project) return;
    setOpeningTaskId(task.id);
    setError(null);
    try {
      if (!String(task.task_id || '').trim()) {
        const query = new URLSearchParams({
          tab: 'components',
          task_row_id: task.id
        }).toString();
        navigate(`/projects/${project.id}?${query}`);
        return;
      }
      const nextAffinity =
        task.affinity && typeof task.affinity === 'object' && Object.keys(task.affinity).length > 0
          ? task.affinity
          : project.affinity || {};
      await updateProject(project.id, {
        task_id: task.task_id,
        task_state: task.task_state,
        status_text: task.status_text || '',
        error_text: task.error_text || '',
        submitted_at: task.submitted_at,
        completed_at: task.completed_at,
        duration_seconds: task.duration_seconds,
        confidence: task.confidence || {},
        affinity: nextAffinity,
        structure_name: task.structure_name || '',
        backend: task.backend || project.backend
      });
      navigate(`/projects/${project.id}?tab=results`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to open selected task.');
    } finally {
      setOpeningTaskId(null);
    }
  };

  const removeTask = async (task: ProjectTask) => {
    const runtimeTaskId = String(task.task_id || '').trim();
    const runtimeState = task.task_state;
    const isActiveRuntime = Boolean(runtimeTaskId) && (runtimeState === 'QUEUED' || runtimeState === 'RUNNING');
    const confirmText = isActiveRuntime
      ? runtimeState === 'QUEUED'
        ? `Task "${runtimeTaskId}" is queued. Delete will first cancel it on backend. Continue?`
        : `Task "${runtimeTaskId}" is running. Delete will first stop it on backend. Continue?`
      : `Delete task "${task.task_id || task.id}" from this project?`;
    if (!window.confirm(confirmText)) return;
    setDeletingTaskId(task.id);
    setError(null);
    try {
      if (runtimeState === 'QUEUED' || runtimeState === 'RUNNING') {
        if (!runtimeTaskId) {
          throw new Error(
            runtimeState === 'RUNNING'
              ? 'Task is running but task_id is missing; cannot stop backend task, deletion is blocked.'
              : 'Task is queued but task_id is missing; cannot cancel backend task, deletion is blocked.'
          );
        }
        const terminateResult = await terminateBackendTask(runtimeTaskId);
        if (terminateResult.terminated !== true) {
          throw new Error(
            runtimeState === 'RUNNING'
              ? `Backend did not confirm stop for task "${runtimeTaskId}", deletion is blocked.`
              : `Backend did not confirm cancellation for task "${runtimeTaskId}", deletion is blocked.`
          );
        }
        const terminalState = await waitForRuntimeTaskToStop(runtimeTaskId, runtimeState === 'RUNNING' ? 14000 : 9000);
        if (terminalState === null || terminalState === 'QUEUED' || terminalState === 'RUNNING') {
          throw new Error(
            runtimeState === 'RUNNING'
              ? `Failed to stop backend task "${runtimeTaskId}". Task is still active, so deletion is blocked.`
              : `Failed to cancel backend task "${runtimeTaskId}". Task is still active, so deletion is blocked.`
          );
        }
      }
      await deleteProjectTask(task.id);
      setTasks((prev) => sanitizeTaskRows(prev).filter((row) => row.id !== task.id));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete task.');
    } finally {
      setDeletingTaskId(null);
    }
  };

  const handleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDirection((prev) => nextSortDirection(prev));
      return;
    }
    setSortKey(key);
    setSortDirection(defaultSortDirection(key));
  };

  if (loading && !project) {
    return <div className="centered-page">Loading tasks...</div>;
  }

  if (!project) {
    return (
      <div className="page-grid">
        {error && <div className="alert error">{error}</div>}
        <section className="panel">
          <Link className="btn btn-ghost btn-compact" to="/projects">
            <ArrowLeft size={14} />
            Back to projects
          </Link>
        </section>
      </div>
    );
  }

  const sortMark = (key: SortKey) => {
    if (sortKey !== key) return '↕';
    return sortDirection === 'asc' ? '↑' : '↓';
  };

  return (
    <div className="page-grid">
      <section className="page-header">
        <div className="page-header-left">
          <h1>Tasks</h1>
          <p className="muted">
            {project.name} · {taskCountText}
            {refreshing ? ' · Syncing...' : ''}
          </p>
        </div>
        <div className="row gap-8 page-header-actions">
          <Link className="btn btn-ghost btn-compact" to={backToCurrentTaskHref}>
            <ArrowLeft size={14} />
            Back to Current Task
          </Link>
        </div>
      </section>

      {error && <div className="alert error">{error}</div>}

      <section className="panel">
        <div className="toolbar project-toolbar">
          <div className="project-toolbar-filters">
            <div className="project-filter-field project-filter-field-search">
              <div className="input-wrap search-input">
                <Search size={16} />
                <input
                  value={taskSearch}
                  onChange={(e) => setTaskSearch(e.target.value)}
                  placeholder="Search status/backend/metrics..."
                  aria-label="Search tasks"
                />
              </div>
            </div>
            <label className="project-filter-field">
              <Activity size={14} />
              <select
                className="project-filter-select"
                value={stateFilter}
                onChange={(e) => setStateFilter(e.target.value as 'all' | ProjectTask['task_state'])}
                aria-label="Filter tasks by state"
              >
                <option value="all">All States</option>
                <option value="DRAFT">Draft</option>
                <option value="QUEUED">Queued</option>
                <option value="RUNNING">Running</option>
                <option value="SUCCESS">Success</option>
                <option value="FAILURE">Failed</option>
                <option value="REVOKED">Revoked</option>
              </select>
            </label>
            <label className="project-filter-field">
              <Filter size={14} />
              <select
                className="project-filter-select"
                value={backendFilter}
                onChange={(e) => setBackendFilter(e.target.value)}
                aria-label="Filter tasks by backend"
              >
                <option value="all">All Backends</option>
                {backendOptions.map((value) => (
                  <option key={`task-backend-filter-${value}`} value={value}>
                    {backendLabel(value)}
                  </option>
                ))}
              </select>
            </label>
          </div>
          <div className="project-toolbar-meta project-toolbar-meta-rich">
            <span className="muted small">{filteredRows.length} matched</span>
            <button
              type="button"
              className="btn btn-ghost btn-compact btn-square"
              onClick={() => void downloadExcel()}
              disabled={exportingExcel || filteredRows.length === 0}
              title={exportingExcel ? 'Exporting Excel...' : 'Download Excel'}
              aria-label="Download tasks Excel"
            >
              {exportingExcel ? <LoaderCircle size={14} className="spin" /> : <Download size={14} />}
            </button>
            <button
              type="button"
              className={`btn btn-ghost btn-compact advanced-filter-toggle ${showAdvancedFilters ? 'active' : ''}`}
              onClick={() => setShowAdvancedFilters((prev) => !prev)}
              title="Toggle advanced filters"
              aria-label="Toggle advanced filters"
            >
              <SlidersHorizontal size={14} />
              Advanced
              {advancedFilterCount > 0 && <span className="advanced-filter-badge">{advancedFilterCount}</span>}
            </button>
          </div>
        </div>
        {showAdvancedFilters && (
          <div className="advanced-filter-panel">
            <div className="advanced-filter-grid">
              <label className="advanced-filter-field">
                <span>Submitted</span>
                <select
                  value={submittedWithinDays}
                  onChange={(e) => setSubmittedWithinDays(e.target.value as SubmittedWithinDaysOption)}
                  aria-label="Advanced filter by submitted time"
                >
                  <option value="all">Any time</option>
                  <option value="1">Last 24 hours</option>
                  <option value="7">Last 7 days</option>
                  <option value="30">Last 30 days</option>
                  <option value="90">Last 90 days</option>
                </select>
              </label>
              <label className="advanced-filter-field">
                <span>Seed</span>
                <select
                  value={seedFilter}
                  onChange={(e) => setSeedFilter(e.target.value as SeedFilterOption)}
                  aria-label="Advanced filter by seed"
                >
                  <option value="all">Any seed</option>
                  <option value="with_seed">With seed</option>
                  <option value="without_seed">Without seed</option>
                </select>
              </label>
              <label className="advanced-filter-field">
                <span>Min pLDDT</span>
                <input
                  type="number"
                  min={0}
                  max={100}
                  step={0.1}
                  value={minPlddt}
                  onChange={(e) => setMinPlddt(e.target.value)}
                  placeholder="e.g. 70"
                  aria-label="Minimum pLDDT"
                />
              </label>
              <label className="advanced-filter-field">
                <span>Min iPTM</span>
                <input
                  type="number"
                  min={0}
                  max={1}
                  step={0.001}
                  value={minIptm}
                  onChange={(e) => setMinIptm(e.target.value)}
                  placeholder="e.g. 0.70"
                  aria-label="Minimum iPTM"
                />
              </label>
              <label className="advanced-filter-field">
                <span>Max PAE</span>
                <input
                  type="number"
                  min={0}
                  step={0.1}
                  value={maxPae}
                  onChange={(e) => setMaxPae(e.target.value)}
                  placeholder="e.g. 12"
                  aria-label="Maximum PAE"
                />
              </label>
              <div className="advanced-filter-field advanced-filter-check">
                <span>Failure Scope</span>
                <label className="advanced-filter-checkbox-row">
                  <input
                    type="checkbox"
                    checked={failureOnly}
                    onChange={(e) => setFailureOnly(e.target.checked)}
                  />
                  <span>Failures / errors only</span>
                </label>
              </div>
              <div className="advanced-filter-field advanced-filter-field-wide task-structure-query-row">
                <div className="task-structure-query-head">
                  <span>Structure Query</span>
                  <div className="task-structure-mode-switch" role="tablist" aria-label="Structure search mode">
                    <button
                      type="button"
                      role="tab"
                      aria-selected={structureSearchMode === 'exact'}
                      className={`task-structure-mode-btn ${structureSearchMode === 'exact' ? 'active' : ''}`}
                      onClick={() => setStructureSearchMode('exact')}
                    >
                      Exact
                    </button>
                    <button
                      type="button"
                      role="tab"
                      aria-selected={structureSearchMode === 'substructure'}
                      className={`task-structure-mode-btn ${structureSearchMode === 'substructure' ? 'active' : ''}`}
                      onClick={() => setStructureSearchMode('substructure')}
                    >
                      Substructure
                    </button>
                  </div>
                </div>
                <div className="jsme-editor-container task-structure-jsme-shell">
                  <JSMEEditor smiles={structureSearchQuery} height={300} onSmilesChange={(value) => setStructureSearchQuery(value)} />
                </div>
                <div className={`task-structure-query-status ${structureSearchError ? 'is-error' : ''}`}>
                  {structureSearchLoading
                    ? 'Searching...'
                    : structureSearchError
                      ? 'Invalid query'
                      : structureSearchQuery.trim()
                        ? `Matched ${Object.values(structureSearchMatches).filter(Boolean).length}`
                        : 'Draw query'}
                </div>
              </div>
            </div>
            <div className="advanced-filter-actions">
              <button
                type="button"
                className="btn btn-ghost btn-compact"
                onClick={clearAdvancedFilters}
                disabled={advancedFilterCount === 0}
              >
                <RefreshCcw size={14} />
                Reset Advanced
              </button>
            </div>
          </div>
        )}

        {filteredRows.length === 0 ? (
          <div className="empty-state">No task runs yet.</div>
        ) : (
          <div className="table-wrap project-table-wrap task-table-wrap">
            <table className="table project-table task-table">
              <thead>
                <tr>
                  <th>
                    <span className="project-th">Ligand View</span>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'plddt' ? 'active' : ''}`} onClick={() => handleSort('plddt')}>
                      <span className="project-th">pLDDT <span className="task-th-arrow">{sortMark('plddt')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'iptm' ? 'active' : ''}`} onClick={() => handleSort('iptm')}>
                      <span className="project-th">iPTM <span className="task-th-arrow">{sortMark('iptm')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'pae' ? 'active' : ''}`} onClick={() => handleSort('pae')}>
                      <span className="project-th">PAE <span className="task-th-arrow">{sortMark('pae')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'submitted' ? 'active' : ''}`} onClick={() => handleSort('submitted')}>
                      <span className="project-th"><Clock3 size={13} /> Submitted <span className="task-th-arrow">{sortMark('submitted')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'backend' ? 'active' : ''}`} onClick={() => handleSort('backend')}>
                      <span className="project-th">Backend <span className="task-th-arrow">{sortMark('backend')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'seed' ? 'active' : ''}`} onClick={() => handleSort('seed')}>
                      <span className="project-th">Seed <span className="task-th-arrow">{sortMark('seed')}</span></span>
                    </button>
                  </th>
                  <th>
                    <button type="button" className={`task-th-sort ${sortKey === 'duration' ? 'active' : ''}`} onClick={() => handleSort('duration')}>
                      <span className="project-th">Duration <span className="task-th-arrow">{sortMark('duration')}</span></span>
                    </button>
                  </th>
                  <th>
                    <span className="project-th">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {pagedRows.map((row) => {
                  const { task, metrics } = row;
                  const runNote = (task.status_text || '').trim();
                  const showRunNote = shouldShowRunNote(task.task_state, runNote);
                  const submittedTs = task.submitted_at || task.created_at;
                  const hasRuntimeTaskId = Boolean(String(task.task_id || '').trim());
                  const actionTitle = hasRuntimeTaskId ? 'Open this task result' : 'Open this draft snapshot for editing';
                  const stateTone = taskStateTone(task.task_state);
                  const plddtTone = toneForPlddt(metrics.plddt);
                  const iptmTone = toneForIptm(metrics.iptm);
                  const paeTone = toneForPae(metrics.pae);
                  return (
                    <tr key={task.id}>
                      <td className="task-col-ligand">
                        {row.ligandSmiles && row.ligandIsSmiles ? (
                          <div className="task-ligand-thumb">
                            <Ligand2DPreview
                              smiles={row.ligandSmiles}
                              width={220}
                              height={132}
                              atomConfidences={row.ligandAtomPlddts}
                              confidenceHint={metrics.plddt}
                            />
                          </div>
                        ) : row.ligandSequence && isSequenceLigandType(row.ligandSequenceType) ? (
                          <TaskLigandSequencePreview
                            sequence={row.ligandSequence}
                            residuePlddts={row.ligandResiduePlddts}
                          />
                        ) : (
                          <div className="task-ligand-thumb task-ligand-thumb-empty">
                            <span className="muted small">No ligand</span>
                          </div>
                        )}
                      </td>
                      <td className="task-col-metric">
                        <span className={`task-metric-value metric-value-${plddtTone}`}>{formatMetric(metrics.plddt, 1)}</span>
                      </td>
                      <td className="task-col-metric">
                        <span className={`task-metric-value metric-value-${iptmTone}`}>{formatMetric(metrics.iptm, 3)}</span>
                      </td>
                      <td className="task-col-metric">
                        <span className={`task-metric-value metric-value-${paeTone}`}>{formatMetric(metrics.pae, 2)}</span>
                      </td>
                      <td className="project-col-time task-col-submitted">
                        <div className="task-submitted-cell">
                          <div className="task-submitted-main">
                            <span className={`task-state-chip ${stateTone}`}>{taskStateLabel(task.task_state)}</span>
                            <span className="task-submitted-time">{formatDateTime(submittedTs)}</span>
                          </div>
                          {showRunNote ? <div className={`task-run-note is-${stateTone}`}>{runNote}</div> : null}
                        </div>
                      </td>
                      <td className="task-col-backend">
                        <span className="badge task-backend-badge">{task.backend || '-'}</span>
                      </td>
                      <td className="task-col-seed">{task.seed ?? '-'}</td>
                      <td className="project-col-time">{formatDuration(task.duration_seconds)}</td>
                      <td className="project-col-actions">
                        <div className="row gap-6 project-action-row">
                          <button
                            className="btn btn-ghost btn-compact task-action-open"
                            onClick={() => void openTask(task)}
                            disabled={openingTaskId === task.id}
                            title={actionTitle}
                          >
                            {openingTaskId === task.id ? <LoaderCircle size={13} className="spin" /> : <ExternalLink size={13} />}
                            Current
                          </button>
                          <button
                            className="icon-btn danger"
                            onClick={() => void removeTask(task)}
                            disabled={deletingTaskId === task.id}
                            title="Delete task"
                          >
                            {deletingTaskId === task.id ? <LoaderCircle size={13} className="spin" /> : <Trash2 size={14} />}
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}

        {filteredRows.length > 0 && (
          <div className="project-pagination">
            <div className="project-pagination-info muted small">
              Page {currentPage} / {totalPages}
            </div>
            <div className="project-pagination-controls">
              <label className="project-page-size">
                <span className="muted small">Per page</span>
                <select value={String(pageSize)} onChange={(e) => setPageSize(Math.max(1, Number(e.target.value) || 12))}>
                  <option value="8">8</option>
                  <option value="12">12</option>
                  <option value="20">20</option>
                  <option value="50">50</option>
                </select>
              </label>
              <button className="btn btn-ghost btn-compact" disabled={currentPage <= 1} onClick={() => setPage(1)}>
                First
              </button>
              <button className="btn btn-ghost btn-compact" disabled={currentPage <= 1} onClick={() => setPage((p) => Math.max(1, p - 1))}>
                Prev
              </button>
              <button
                className="btn btn-ghost btn-compact"
                disabled={currentPage >= totalPages}
                onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
              >
                Next
              </button>
              <button className="btn btn-ghost btn-compact" disabled={currentPage >= totalPages} onClick={() => setPage(totalPages)}>
                Last
              </button>
              <label className="project-page-size">
                <span className="muted small">Go to</span>
                <input
                  type="number"
                  min={1}
                  max={totalPages}
                  value={String(currentPage)}
                  onChange={(e) => jumpToPage(e.target.value)}
                  aria-label="Go to tasks page"
                />
              </label>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
