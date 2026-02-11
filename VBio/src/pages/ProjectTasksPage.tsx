import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  Activity,
  ArrowLeft,
  Clock3,
  ExternalLink,
  Filter,
  LoaderCircle,
  RefreshCcw,
  Search,
  SlidersHorizontal,
  Trash2
} from 'lucide-react';
import { downloadResultBlob, getTaskStatus, parseResultBundle } from '../api/backendApi';
import { deleteProjectTask, getProjectById, listProjectTasks, updateProject, updateProjectTask } from '../api/supabaseLite';
import { JSMEEditor } from '../components/project/JSMEEditor';
import { Ligand2DPreview } from '../components/project/Ligand2DPreview';
import { useAuth } from '../hooks/useAuth';
import type { InputComponent, Project, ProjectTask } from '../types/models';
import { assignChainIdsForComponents } from '../utils/chainAssignments';
import { formatDateTime, formatDuration } from '../utils/date';
import { normalizeComponentSequence } from '../utils/projectInputs';
import { loadRDKitModule } from '../utils/rdkit';

function normalizeTaskComponents(components: InputComponent[]): InputComponent[] {
  return components.map((item) => ({
    ...item,
    sequence: normalizeComponentSequence(item.type, item.sequence)
  }));
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

interface TaskSelectionContext extends TaskMetricContext {
  ligandSmiles: string;
  ligandIsSmiles: boolean;
  ligandComponentCount: number;
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
}

function toneForPlddt(value: number | null): MetricTone {
  if (value === null) return 'neutral';
  if (value >= 90) return 'excellent';
  if (value >= 70) return 'good';
  if (value >= 50) return 'medium';
  return 'low';
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

function resolveTaskSelectionContext(task: ProjectTask): TaskSelectionContext {
  const activeComponents = readTaskComponents(task).filter((item) => Boolean(item.sequence.trim()));
  const ligandComponentCount = activeComponents.filter((item) => item.type === 'ligand').length;
  if (activeComponents.length === 0) {
    const ligand = readTaskPrimaryLigand(task, [], null);
    return {
      chainIds: [],
      targetChainId: null,
      ligandChainId: null,
      ligandSmiles: ligand.smiles,
      ligandIsSmiles: ligand.isSmiles,
      ligandComponentCount
    };
  }

  const chainAssignments = assignChainIdsForComponents(activeComponents);
  const chainIds = chainAssignments.flat();
  const validChainIds = new Set(chainIds);
  const taskProperties = task.properties && typeof task.properties === 'object' ? task.properties : null;
  const rawTarget = taskProperties && typeof taskProperties.target === 'string' ? taskProperties.target.trim() : '';
  const rawLigand = taskProperties && typeof taskProperties.ligand === 'string' ? taskProperties.ligand.trim() : '';
  const fallbackTargetChainId = chainAssignments[0]?.[0] || null;
  const fallbackLigandChainId = chainAssignments[chainAssignments.length - 1]?.[0] || null;
  const targetChainId = rawTarget && validChainIds.has(rawTarget) ? rawTarget : fallbackTargetChainId;
  const ligandChainId = rawLigand && validChainIds.has(rawLigand) ? rawLigand : fallbackLigandChainId;

  const chainToComponent = new Map<string, InputComponent>();
  chainAssignments.forEach((chainGroup, index) => {
    chainGroup.forEach((chainId) => {
      chainToComponent.set(chainId, activeComponents[index]);
    });
  });
  const selectedLigandComponent = ligandChainId ? chainToComponent.get(ligandChainId) || null : null;
  const ligand = readTaskPrimaryLigand(task, activeComponents, selectedLigandComponent?.id || null);

  return {
    chainIds,
    targetChainId,
    ligandChainId,
    ligandSmiles: ligand.smiles,
    ligandIsSmiles: ligand.isSmiles,
    ligandComponentCount
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
  if (Array.isArray(value)) {
    return value.filter((item): item is number => typeof item === 'number' && Number.isFinite(item));
  }
  if (typeof value === 'string') {
    try {
      const parsed = JSON.parse(value) as unknown;
      if (Array.isArray(parsed)) {
        return parsed.filter((item): item is number => typeof item === 'number' && Number.isFinite(item));
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
  return normalized.slice(0, 320);
}

function mean(values: number[] | null): number | null {
  if (!values || values.length === 0) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

function readTaskLigandAtomPlddts(task: ProjectTask): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
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

function hasTaskLigandAtomPlddts(task: ProjectTask): boolean {
  return Boolean(readTaskLigandAtomPlddts(task)?.length);
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

interface LoadTaskDataOptions {
  silent?: boolean;
  showRefreshing?: boolean;
  preferBackendStatus?: boolean;
}

export function ProjectTasksPage() {
  const { projectId = '' } = useParams();
  const navigate = useNavigate();
  const { session } = useAuth();
  const [project, setProject] = useState<Project | null>(null);
  const [tasks, setTasks] = useState<ProjectTask[]>([]);
  const [loading, setLoading] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
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
  const loadInFlightRef = useRef(false);
  const resultHydrationInFlightRef = useRef<Set<string>>(new Set());
  const resultHydrationDoneRef = useRef<Set<string>>(new Set());
  const resultHydrationAttemptsRef = useRef<Map<string, number>>(new Map());

  const syncRuntimeTasks = useCallback(async (projectRow: Project, taskRows: ProjectTask[]) => {
    const runtimeRows = taskRows.filter((row) => Boolean(row.task_id) && (row.task_state === 'QUEUED' || row.task_state === 'RUNNING'));
    if (runtimeRows.length === 0) {
      return {
        project: projectRow,
        taskRows: sortProjectTasks(taskRows)
      };
    }

    const checks = await Promise.allSettled(runtimeRows.map((row) => getTaskStatus(row.task_id)));
    let nextProject = projectRow;
    let nextTaskRows = [...taskRows];

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
        const patchedTask = await updateProjectTask(runtimeTask.id, taskPatch).catch(() => ({
          ...runtimeTask,
          ...taskPatch
        }));
        nextTaskRows = nextTaskRows.map((row) => (row.id === runtimeTask.id ? patchedTask : row));
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
          const patchedProject = await updateProject(nextProject.id, projectPatch).catch(() => ({
            ...nextProject,
            ...projectPatch
          }));
          nextProject = patchedProject;
        }
      }
    }

    return {
      project: nextProject,
      taskRows: sortProjectTasks(nextTaskRows)
    };
  }, []);

  const hydrateTaskMetricsFromResults = useCallback(async (projectRow: Project, taskRows: ProjectTask[]) => {
    const candidates = taskRows
      .filter((row) => {
        const taskId = String(row.task_id || '').trim();
        if (!taskId || row.task_state !== 'SUCCESS') return false;
        const selection = resolveTaskSelectionContext(row);
        const needsSummaryHydration = !hasTaskSummaryMetrics(row);
        const needsLigandAtomHydration =
          selection.ligandComponentCount === 1 &&
          Boolean(selection.ligandSmiles && selection.ligandIsSmiles && !hasTaskLigandAtomPlddts(row));
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
        taskRows
      };
    }

    let nextProject = projectRow;
    let nextTaskRows = [...taskRows];

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
        const patchedTask = await updateProjectTask(task.id, taskPatch).catch(() => ({
          ...task,
          ...taskPatch
        }));

        nextTaskRows = nextTaskRows.map((row) => (row.id === task.id ? patchedTask : row));

        if (nextProject.task_id === taskId) {
          const projectPatch: Partial<Project> = {
            confidence: taskPatch.confidence || {},
            affinity: taskPatch.affinity || {},
            structure_name: taskPatch.structure_name || ''
          };
          const patchedProject = await updateProject(nextProject.id, projectPatch).catch(() => ({
            ...nextProject,
            ...projectPatch
          }));
          nextProject = patchedProject;
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
      taskRows: sortProjectTasks(nextTaskRows)
    };
  }, []);

  const loadData = useCallback(
    async (options?: LoadTaskDataOptions) => {
      if (loadInFlightRef.current) return;
      loadInFlightRef.current = true;
      const silent = Boolean(options?.silent);
      const showRefreshing = silent && options?.showRefreshing !== false;
      if (showRefreshing) {
        setRefreshing(true);
      } else if (!silent) {
        setLoading(true);
      }
      if (!silent) {
        setError(null);
      }
      try {
        const [projectRow, taskRows] = await Promise.all([getProjectById(projectId), listProjectTasks(projectId)]);
        if (!projectRow || projectRow.deleted_at) {
          throw new Error('Project not found or already deleted.');
        }
        if (session && projectRow.user_id !== session.userId) {
          throw new Error('You do not have permission to access this project.');
        }

        const synced =
          options?.preferBackendStatus === false
            ? { project: projectRow, taskRows: sortProjectTasks(taskRows) }
            : await syncRuntimeTasks(projectRow, taskRows);
        const hydrated =
          options?.preferBackendStatus === false
            ? synced
            : await hydrateTaskMetricsFromResults(synced.project, synced.taskRows);

        setProject(hydrated.project);
        setTasks(hydrated.taskRows);
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
      void loadData({ silent: true, showRefreshing: false });
    };
    const onVisible = () => {
      if (document.visibilityState === 'visible') {
        void loadData({ silent: true, showRefreshing: false });
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
    () => tasks.some((row) => Boolean(row.task_id) && (row.task_state === 'QUEUED' || row.task_state === 'RUNNING')),
    [tasks]
  );

  useEffect(() => {
    if (!hasActiveRuntime) return;
    const timer = window.setInterval(() => {
      void loadData({ silent: true, showRefreshing: false });
    }, 4000);
    return () => window.clearInterval(timer);
  }, [hasActiveRuntime, loadData]);

  const taskCountText = useMemo(() => `${tasks.length} tasks`, [tasks.length]);

  const taskRows = useMemo<TaskListRow[]>(() => {
    return tasks.map((task) => {
      const submittedTs = new Date(task.submitted_at || task.created_at).getTime();
      const durationValue = typeof task.duration_seconds === 'number' && Number.isFinite(task.duration_seconds) ? task.duration_seconds : null;
      const selection = resolveTaskSelectionContext(task);
      const ligandAtomPlddtsRaw = readTaskLigandAtomPlddts(task);
      const ligandAtomPlddts = selection.ligandComponentCount === 1 ? ligandAtomPlddtsRaw : null;
      const metrics = readTaskConfidenceMetrics(task, selection);
      const ligandMeanPlddt = mean(ligandAtomPlddts);
      const plddt = metrics.plddt !== null ? metrics.plddt : ligandMeanPlddt;
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
        ligandAtomPlddts
      };
    });
  }, [tasks]);
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

  const openTask = async (task: ProjectTask) => {
    if (!project || !task.task_id) return;
    setOpeningTaskId(task.id);
    setError(null);
    try {
      await updateProject(project.id, {
        task_id: task.task_id,
        task_state: task.task_state,
        status_text: task.status_text || '',
        error_text: task.error_text || '',
        submitted_at: task.submitted_at,
        completed_at: task.completed_at,
        duration_seconds: task.duration_seconds,
        confidence: task.confidence || {},
        affinity: task.affinity || {},
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
    if (!window.confirm(`Delete task "${task.task_id || task.id}" from this project?`)) return;
    setDeletingTaskId(task.id);
    setError(null);
    try {
      await deleteProjectTask(task.id);
      setTasks((prev) => prev.filter((row) => row.id !== task.id));
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
          </p>
        </div>
        <div className="row gap-8 page-header-actions">
          <Link className="btn btn-ghost btn-compact" to={`/projects/${project.id}`}>
            <ArrowLeft size={14} />
            Back to Project
          </Link>
          <button className="btn btn-ghost btn-compact btn-square" onClick={() => void loadData({ silent: true })} disabled={refreshing}>
            <RefreshCcw size={15} className={refreshing ? 'spin' : undefined} />
          </button>
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
                    <span className="project-th">Ligand 2D</span>
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
                              width={160}
                              height={102}
                              atomConfidences={row.ligandAtomPlddts}
                            />
                          </div>
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
                            disabled={!task.task_id || openingTaskId === task.id}
                            title="Open this task result"
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
            </div>
          </div>
        )}
      </section>
    </div>
  );
}
