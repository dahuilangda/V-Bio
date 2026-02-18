import {
  CSSProperties,
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import {
  BarChart3,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Clock3,
  Copy,
  Download,
  Info,
  KeyRound,
  Plus,
  Search,
  ShieldCheck,
  ShieldOff,
  Trash2,
  X
} from 'lucide-react';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import type {
  ApiToken,
  ApiTokenUsage,
  ApiTokenUsageDaily,
  InputComponent,
  PredictionConstraint,
  PredictionProperties,
  Project
} from '../types/models';
import { useAuth } from '../hooks/useAuth';
import { ConstraintEditor } from '../components/project/ConstraintEditor';
import { JSMEEditor } from '../components/project/JSMEEditor';
import {
  deleteApiToken,
  getProjectTaskById,
  findApiTokenByHash,
  insertApiToken,
  listApiTokenUsagePage,
  listApiTokenUsageDailyByTokenIds,
  listApiTokenUsageDaily,
  listApiTokens,
  listProjects,
  revokeApiToken,
  updateApiToken
} from '../api/supabaseLite';
import { sha256Hex } from '../utils/crypto';
import { ENV } from '../utils/env';
import { componentTypeLabel, createInputComponent, normalizeInputComponents } from '../utils/projectInputs';
import { getWorkflowDefinition } from '../utils/workflows';
import { buildPredictionYamlFromComponents } from '../utils/yaml';
import { assignChainIdsForComponents } from '../utils/chainAssignments';

type UsageWindow = '7d' | '30d' | '90d' | 'all';
type ProjectStatsWorkflowFilter = 'all' | 'prediction' | 'affinity';
type ProjectStatsSort = 'calls_desc' | 'calls_asc' | 'success_desc' | 'success_asc' | 'last_desc' | 'last_asc';
type BuilderWorkflowKey = 'prediction' | 'affinity';
type PredictionBackend = 'boltz' | 'alphafold3' | 'protenix';
type AffinityBackend = 'boltz' | 'protenix';

interface ProjectStatsRow {
  project: Project;
  workflowKey: ProjectStatsWorkflowFilter;
  workflowLabel: string;
  tokenCount: number;
  activeTokenCount: number;
  totalCalls: number;
  successRate: number;
  lastEventAt: string | null;
  lastEventTs: number;
}

interface UsageSummary {
  total: number;
  success: number;
  errors: number;
  successRate: number;
  lastEventAt: string | null;
  lastEventTs: number;
}

interface CommandHistoryEntry {
  id: string;
  createdAt: string;
  label: string;
  command: string;
  workflow: BuilderWorkflowKey;
  backend: string;
  projectId: string;
  projectName: string;
  tokenId: string;
  tokenName: string;
}

interface YamlProteinTemplateConfig {
  path: string;
  format: 'auto' | 'pdb' | 'cif';
  templateChain: string;
  targetChains: string;
}

type ApiBuilderGridStyle = CSSProperties & {
  '--api-builder-left-width'?: string;
  '--api-yaml-left-width'?: string;
};

const TOKEN_PAGE_SIZE = 8;
const EVENT_PAGE_SIZE = 20;
const DAILY_USAGE_PAGE_SIZE = 30;
const PROJECT_STATS_PAGE_SIZE = 8;
const COMMAND_HISTORY_LIMIT = 12;
const COMMAND_HISTORY_STORAGE_KEY = 'vbio_api_command_history_v1';
const AFFINITY_TARGET_UPLOAD_COMPONENT_ID = '__affinity_target_upload__';
const AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = '__affinity_ligand_upload__';
const EMPTY_PREDICTION_PROPERTIES: PredictionProperties = {
  affinity: false,
  target: null,
  ligand: null,
  binder: null
};

function normalizeUsageWindow(value: string | null | undefined): UsageWindow {
  if (value === '7d' || value === '30d' || value === '90d' || value === 'all') return value;
  return '90d';
}

function normalizeProjectStatsWorkflowFilter(value: string | null | undefined): ProjectStatsWorkflowFilter {
  if (value === 'prediction' || value === 'affinity' || value === 'all') return value;
  return 'all';
}

function normalizeProjectStatsSort(value: string | null | undefined): ProjectStatsSort {
  if (
    value === 'calls_desc' ||
    value === 'calls_asc' ||
    value === 'success_desc' ||
    value === 'success_asc' ||
    value === 'last_desc' ||
    value === 'last_asc'
  ) {
    return value;
  }
  return 'last_desc';
}

function normalizePredictionBackend(value: string | null | undefined): PredictionBackend {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'alphafold3') return 'alphafold3';
  if (normalized === 'protenix') return 'protenix';
  return 'boltz';
}

function normalizeAffinityBackend(value: string | null | undefined): AffinityBackend {
  return String(value || '').trim().toLowerCase() === 'protenix' ? 'protenix' : 'boltz';
}

function randomAlphaNum(length: number): string {
  const alphabet = 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789';
  const bytes = new Uint8Array(length);
  if (globalThis.crypto?.getRandomValues) {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let i = 0; i < length; i += 1) {
      bytes[i] = Math.floor(Math.random() * 256);
    }
  }
  let out = '';
  for (let i = 0; i < length; i += 1) {
    out += alphabet[bytes[i] % alphabet.length];
  }
  return out;
}

function shortUuidLike(): string {
  const raw = typeof globalThis.crypto?.randomUUID === 'function'
    ? globalThis.crypto.randomUUID().replace(/-/g, '')
    : randomAlphaNum(16).toLowerCase();
  return `token-${raw.slice(0, 8)}`;
}

function formatIso(ts: string | null | undefined): string {
  if (!ts) return '-';
  const t = Date.parse(ts);
  if (!Number.isFinite(t)) return ts;
  return new Date(t).toLocaleString();
}

function computeUsageSummaryFromDaily(rows: ApiTokenUsageDaily[], lastEventAt?: string | null): UsageSummary {
  const total = rows.reduce((acc, row) => acc + Math.max(0, Number(row.total_count) || 0), 0);
  const success = rows.reduce((acc, row) => acc + Math.max(0, Number(row.success_count) || 0), 0);
  const errors = Math.max(0, total - success);
  const lastTsRaw = lastEventAt ? Date.parse(lastEventAt) : Number.NaN;
  const lastEventTs = Number.isFinite(lastTsRaw) ? lastTsRaw : 0;
  return {
    total,
    success,
    errors,
    successRate: total > 0 ? (success / total) * 100 : 0,
    lastEventAt: lastEventTs > 0 ? lastEventAt || null : null,
    lastEventTs
  };
}

function usageSince(window: UsageWindow): string | undefined {
  if (window === 'all') return undefined;
  const days = window === '7d' ? 7 : window === '30d' ? 30 : 90;
  return new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
}

function normalizeBaseUrl(value: string): string {
  const text = String(value || '').trim();
  if (!text) return '';
  return text.replace(/\/$/, '');
}

function escapeForDoubleQuotedShell(value: string): string {
  return String(value || '')
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"');
}

function extractFileNameFromPath(pathRaw: string): string {
  const normalized = String(pathRaw || '').trim().replace(/\\/g, '/');
  if (!normalized) return '';
  const segments = normalized.split('/').filter(Boolean);
  return segments.length > 0 ? segments[segments.length - 1] : '';
}

function inferTemplateFormat(pathRaw: string, selected: 'auto' | 'pdb' | 'cif'): 'pdb' | 'cif' {
  if (selected === 'pdb' || selected === 'cif') return selected;
  const lower = extractFileNameFromPath(pathRaw).toLowerCase();
  if (lower.endsWith('.pdb')) return 'pdb';
  return 'cif';
}

function normalizeChainId(value: string, fallback: string): string {
  const cleaned = String(value || '').trim();
  return cleaned || fallback;
}

function createYamlBuilderComponent(type: InputComponent['type'] = 'protein'): InputComponent {
  const component = createInputComponent(type);
  if (component.type === 'ligand') {
    component.inputMethod = 'smiles';
  }
  return component;
}

function isAffinityUploadComponent(value: unknown): boolean {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return false;
  const component = value as Record<string, unknown>;
  const componentId = String(component.id || '').trim();
  if (componentId === AFFINITY_TARGET_UPLOAD_COMPONENT_ID || componentId === AFFINITY_LIGAND_UPLOAD_COMPONENT_ID) {
    return true;
  }
  const uploadMeta =
    component.affinityUpload && typeof component.affinityUpload === 'object'
      ? (component.affinityUpload as Record<string, unknown>)
      : component.affinity_upload && typeof component.affinity_upload === 'object'
        ? (component.affinity_upload as Record<string, unknown>)
        : null;
  const role = String(uploadMeta?.role || '').trim().toLowerCase();
  return role === 'target' || role === 'ligand';
}

function readCommandHistoryFromStorage(): CommandHistoryEntry[] {
  if (typeof window === 'undefined') return [];
  try {
    const raw = window.localStorage.getItem(COMMAND_HISTORY_STORAGE_KEY);
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item) => item && typeof item === 'object')
      .map((item) => {
        const record = item as Partial<CommandHistoryEntry>;
        const legacyTemplate = (record as { template?: string }).template;
        const workflow: BuilderWorkflowKey = record.workflow === 'affinity' || legacyTemplate === 'affinity' ? 'affinity' : 'prediction';
        return {
          id: String(record.id || ''),
          createdAt: String(record.createdAt || ''),
          label: String(record.label || 'Command'),
          command: String(record.command || ''),
          workflow,
          backend: String(record.backend || ''),
          projectId: String(record.projectId || ''),
          projectName: String(record.projectName || ''),
          tokenId: String(record.tokenId || ''),
          tokenName: String(record.tokenName || '')
        } as CommandHistoryEntry;
      })
      .filter((item) => item.id && item.command);
  } catch {
    return [];
  }
}

function fallbackCopyText(text: string): boolean {
  if (typeof document === 'undefined') return false;
  const active = document.activeElement as HTMLElement | null;
  const selection = typeof window !== 'undefined' ? window.getSelection() : null;
  const ranges: Range[] = [];
  if (selection) {
    for (let i = 0; i < selection.rangeCount; i += 1) {
      const range = selection.getRangeAt(i);
      ranges.push(range.cloneRange());
    }
  }
  const textarea = document.createElement('textarea');
  textarea.value = text;
  textarea.setAttribute('readonly', 'true');
  textarea.style.position = 'fixed';
  textarea.style.opacity = '0';
  textarea.style.left = '-9999px';
  textarea.style.top = '0';
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  textarea.setSelectionRange(0, textarea.value.length);
  let ok = false;
  try {
    ok = document.execCommand('copy');
  } catch {
    ok = false;
  } finally {
    document.body.removeChild(textarea);
    if (selection) {
      selection.removeAllRanges();
      for (const range of ranges) {
        selection.addRange(range);
      }
    }
    if (active && typeof active.focus === 'function') {
      active.focus();
    }
  }
  return ok;
}

export function ApiAccessPage() {
  const { session } = useAuth();
  const { projectId: routeProjectId } = useParams<{ projectId?: string }>();
  const location = useLocation();
  const navigate = useNavigate();
  const scopedProjectId = String(routeProjectId || '').trim();
  const isProjectScoped = Boolean(scopedProjectId);
  const routeQuery = useMemo(() => new URLSearchParams(location.search), [location.search]);
  const scopedTaskRowId = String(routeQuery.get('task_row_id') || '').trim();
  const scopedTaskId = String(routeQuery.get('task_id') || '').trim();
  const scopedTaskName = String(routeQuery.get('task_name') || '').trim();
  const scopedTaskSummary = String(routeQuery.get('task_summary') || '').trim();
  const hasScopedTaskContext = Boolean(scopedTaskId || scopedTaskRowId || scopedTaskName || scopedTaskSummary);
  const scopedTaskContextTitle = scopedTaskName || scopedTaskSummary || scopedTaskId || scopedTaskRowId;
  const openBuilderFromQuery = routeQuery.get('open_builder') === '1';
  const projectBackPath = isProjectScoped ? `/projects/${scopedProjectId}/tasks` : '/projects';

  const [tokenCreating, setTokenCreating] = useState(false);
  const [tokenRevokingId, setTokenRevokingId] = useState<string | null>(null);
  const [tokenDeletingId, setTokenDeletingId] = useState<string | null>(null);
  const [tokenLoading, setTokenLoading] = useState(false);
  const [projectLoading, setProjectLoading] = useState(false);
  const [registryOpen, setRegistryOpen] = useState(false);
  const [registryScopeProjectId, setRegistryScopeProjectId] = useState<string | null>(null);
  const [, setError] = useState<string | null>(null);
  const [, setSuccess] = useState<string | null>(null);

  const [tokens, setTokens] = useState<ApiToken[]>([]);
  const [projects, setProjects] = useState<Project[]>([]);
  const [selectedTokenId, setSelectedTokenId] = useState('');
  const [tokenUsage, setTokenUsage] = useState<ApiTokenUsage[]>([]);
  const [tokenUsageDaily, setTokenUsageDaily] = useState<ApiTokenUsageDaily[]>([]);
  const [tokenUsageTotal, setTokenUsageTotal] = useState(0);
  const [usageByTokenId, setUsageByTokenId] = useState<Record<string, UsageSummary>>({});
  const [projectStatsLoading, setProjectStatsLoading] = useState(false);

  const [newTokenName, setNewTokenName] = useState(shortUuidLike);
  const [newTokenExpiresDays, setNewTokenExpiresDays] = useState('');
  const [newTokenPlainText, setNewTokenPlainText] = useState('');
  const [selectedProjectId, setSelectedProjectId] = useState('');
  const [allowSubmit, setAllowSubmit] = useState(true);
  const [allowDelete, setAllowDelete] = useState(true);
  const [allowCancel, setAllowCancel] = useState(true);

  const [usageWindow, setUsageWindow] = useState<UsageWindow>(() => {
    if (typeof window === 'undefined') return '90d';
    return normalizeUsageWindow(new URLSearchParams(window.location.search).get('ps_window'));
  });
  const [projectStatsSearch, setProjectStatsSearch] = useState(() => {
    if (typeof window === 'undefined') return '';
    return new URLSearchParams(window.location.search).get('ps_q') || '';
  });
  const [projectStatsWorkflowFilter, setProjectStatsWorkflowFilter] = useState<ProjectStatsWorkflowFilter>(() => {
    if (typeof window === 'undefined') return 'all';
    return normalizeProjectStatsWorkflowFilter(new URLSearchParams(window.location.search).get('ps_workflow'));
  });
  const [projectStatsSort, setProjectStatsSort] = useState<ProjectStatsSort>(() => {
    if (typeof window === 'undefined') return 'last_desc';
    return normalizeProjectStatsSort(new URLSearchParams(window.location.search).get('ps_sort'));
  });
  const [tokenQuery, setTokenQuery] = useState('');
  const [tokenPage, setTokenPage] = useState(1);
  const [eventPage, setEventPage] = useState(1);
  const [usageBarsPage, setUsageBarsPage] = useState(1);
  const [projectStatsPage, setProjectStatsPage] = useState(1);
  const [yamlBuilderOpen, setYamlBuilderOpen] = useState(false);
  const [builderTaskName, setBuilderTaskName] = useState('');
  const [builderTaskSummary, setBuilderTaskSummary] = useState('');
  const [builderTokenPlainInput, setBuilderTokenPlainInput] = useState('');
  const [builderYamlPath, setBuilderYamlPath] = useState('./config.yaml');
  const [builderYamlComponents, setBuilderYamlComponents] = useState<InputComponent[]>([
    createYamlBuilderComponent('protein'),
    createYamlBuilderComponent('ligand')
  ]);
  const [builderYamlTemplates, setBuilderYamlTemplates] = useState<Record<string, YamlProteinTemplateConfig>>({});
  const [builderYamlCollapsed, setBuilderYamlCollapsed] = useState<Record<string, boolean>>({});
  const [builderYamlConstraintsOpen, setBuilderYamlConstraintsOpen] = useState(false);
  const [builderYamlConstraints, setBuilderYamlConstraints] = useState<PredictionConstraint[]>([]);
  const [builderYamlProperties, setBuilderYamlProperties] = useState<PredictionProperties>({ ...EMPTY_PREDICTION_PROPERTIES });
  const [builderTargetPath, setBuilderTargetPath] = useState('./target.cif');
  const [builderLigandPath, setBuilderLigandPath] = useState('./ligand.sdf');
  const [builderComplexPath, setBuilderComplexPath] = useState('./complex.cif');
  const [builderResultPath, setBuilderResultPath] = useState('./result.zip');
  const [builderPredictionBackend, setBuilderPredictionBackend] = useState<PredictionBackend>('boltz');
  const [builderUseMsaProtenix, setBuilderUseMsaProtenix] = useState(true);
  const [builderUseMsaAffinity, setBuilderUseMsaAffinity] = useState(true);
  const [builderAffinityConfidenceOnly, setBuilderAffinityConfidenceOnly] = useState(true);
  const [builderAffinityTargetChain, setBuilderAffinityTargetChain] = useState('A');
  const [builderAffinityLigandChain, setBuilderAffinityLigandChain] = useState('L');
  const [builderAffinityLigandSmiles, setBuilderAffinityLigandSmiles] = useState('');
  const [builderTaskOperation, setBuilderTaskOperation] = useState<'cancel' | 'delete'>('cancel');
  const [builderAffinityBackend, setBuilderAffinityBackend] = useState<AffinityBackend>('boltz');
  const [builderLeftWidth, setBuilderLeftWidth] = useState(38);
  const [isBuilderResizing, setIsBuilderResizing] = useState(false);
  const [yamlBuilderLeftWidth, setYamlBuilderLeftWidth] = useState(68);
  const [isYamlBuilderResizing, setIsYamlBuilderResizing] = useState(false);
  const [projectTokenPanelProjectId, setProjectTokenPanelProjectId] = useState<string | null>(null);
  const [commandHistory, setCommandHistory] = useState<CommandHistoryEntry[]>([]);
  const [copiedActionId, setCopiedActionId] = useState('');
  const commandPanelRef = useRef<HTMLElement | null>(null);
  const builderGridRef = useRef<HTMLDivElement | null>(null);
  const builderResizeRef = useRef<{ startX: number; startWidthPercent: number } | null>(null);
  const yamlBuilderGridRef = useRef<HTMLDivElement | null>(null);
  const yamlBuilderResizeRef = useRef<{ startX: number; startWidthPercent: number } | null>(null);
  const copiedResetTimerRef = useRef<number | null>(null);
  const scopedTaskPrefillRef = useRef('');
  const openBuilderHandledRef = useRef('');

  const managementApiBaseUrl = normalizeBaseUrl(
    ENV.managementApiBaseUrl ||
      (typeof window !== 'undefined' ? `${window.location.origin}/vbio-api` : 'http://127.0.0.1:5055/vbio-api')
  );

  useEffect(() => {
    if (!session?.userId) return;
    let cancelled = false;

    const load = async () => {
      setTokenLoading(true);
      setProjectLoading(true);
      setError(null);
      try {
        const [tokenRows, projectRows] = await Promise.all([
          listApiTokens(session.userId),
          listProjects({ userId: session.userId })
        ]);
        if (cancelled) return;

        setTokens(tokenRows);
        setProjects(projectRows);
        const scopedProjectTokens = isProjectScoped
          ? tokenRows.filter((item) => String(item.project_id || '').trim() === scopedProjectId)
          : tokenRows;
        setSelectedTokenId((prev) => {
          if (isProjectScoped) {
            if (prev && scopedProjectTokens.some((item) => item.id === prev)) return prev;
            return scopedProjectTokens[0]?.id || '';
          }
          return prev && tokenRows.some((item) => item.id === prev) ? prev : tokenRows[0]?.id || '';
        });
        setSelectedProjectId((prev) => {
          if (isProjectScoped) return scopedProjectId;
          return prev || tokenRows[0]?.project_id || projectRows[0]?.id || '';
        });
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load API access data.');
      } finally {
        if (!cancelled) {
          setTokenLoading(false);
          setProjectLoading(false);
        }
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [session?.userId, isProjectScoped, scopedProjectId]);

  useEffect(() => {
    if (!selectedTokenId) {
      setTokenUsage([]);
      setTokenUsageDaily([]);
      setTokenUsageTotal(0);
      return;
    }
    let cancelled = false;

    const loadUsage = async () => {
      try {
        const since = usageSince(usageWindow);
        const offset = (eventPage - 1) * EVENT_PAGE_SIZE;
        const [events, daily] = await Promise.all([
          listApiTokenUsagePage(selectedTokenId, {
            sinceIso: since,
            limit: EVENT_PAGE_SIZE,
            offset
          }),
          listApiTokenUsageDaily(selectedTokenId, since)
        ]);
        if (cancelled) return;
        setTokenUsage(events.rows);
        const dailyTotal = daily.reduce((acc, row) => acc + Math.max(0, Number(row.total_count) || 0), 0);
        setTokenUsageTotal(events.total > 0 ? events.total : dailyTotal);
        setTokenUsageDaily(daily);
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load API usage.');
      }
    };

    void loadUsage();
    return () => {
      cancelled = true;
    };
  }, [selectedTokenId, usageWindow, eventPage]);

  useEffect(() => {
    if (!isProjectScoped) return;
    if (selectedProjectId === scopedProjectId) return;
    setSelectedProjectId(scopedProjectId);
  }, [isProjectScoped, scopedProjectId, selectedProjectId]);

  useEffect(() => {
    if (!isProjectScoped) return;
    setRegistryScopeProjectId(scopedProjectId);
  }, [isProjectScoped, scopedProjectId]);

  useEffect(() => {
    if (!selectedProjectId) return;
    if (isProjectScoped && selectedProjectId !== scopedProjectId) return;
    const tokenProjectId = tokens.find((item) => item.id === selectedTokenId)?.project_id || '';
    if (tokenProjectId && tokenProjectId !== selectedProjectId) return;
    const projectTokens = tokens.filter((item) => item.project_id === selectedProjectId);
    if (projectTokens.length === 0) return;
    if (!projectTokens.some((item) => item.id === selectedTokenId)) {
      const preferred = projectTokens.find((item) => item.is_active) || projectTokens[0];
      setSelectedTokenId(preferred.id);
    }
  }, [tokens, selectedTokenId, selectedProjectId, isProjectScoped, scopedProjectId]);

  useEffect(() => {
    const usageSourceTokens = isProjectScoped
      ? tokens.filter((item) => String(item.project_id || '').trim() === scopedProjectId)
      : tokens;
    if (usageSourceTokens.length === 0) {
      setUsageByTokenId({});
      return;
    }
    let cancelled = false;

    const loadProjectUsage = async () => {
      setProjectStatsLoading(true);
      try {
        const since = usageSince(usageWindow);
        const dailyRows = await listApiTokenUsageDailyByTokenIds(
          usageSourceTokens.map((item) => item.id),
          since
        );
        if (cancelled) return;
        const rowsByTokenId: Record<string, ApiTokenUsageDaily[]> = {};
        for (const row of dailyRows) {
          const tokenId = String(row.token_id || '').trim();
          if (!tokenId) continue;
          if (!rowsByTokenId[tokenId]) {
            rowsByTokenId[tokenId] = [];
          }
          rowsByTokenId[tokenId].push(row);
        }
        const next: Record<string, UsageSummary> = {};
        for (const token of usageSourceTokens) {
          const tokenRows = rowsByTokenId[token.id] || [];
          next[token.id] = computeUsageSummaryFromDaily(tokenRows, token.last_used_at || null);
        }
        setUsageByTokenId(next);
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : 'Failed to load project usage.');
        }
      } finally {
        if (!cancelled) {
          setProjectStatsLoading(false);
        }
      }
    };

    void loadProjectUsage();
    return () => {
      cancelled = true;
    };
  }, [tokens, usageWindow, isProjectScoped, scopedProjectId]);

  useEffect(() => {
    setCommandHistory(readCommandHistoryFromStorage());
  }, []);

  useEffect(() => {
    if (typeof window === 'undefined') return;
    try {
      window.localStorage.setItem(COMMAND_HISTORY_STORAGE_KEY, JSON.stringify(commandHistory));
    } catch {
      // ignore quota/storage errors
    }
  }, [commandHistory]);

  useEffect(() => {
    return () => {
      if (copiedResetTimerRef.current !== null) {
        window.clearTimeout(copiedResetTimerRef.current);
        copiedResetTimerRef.current = null;
      }
    };
  }, []);

  useEffect(() => {
    const query = new URLSearchParams(location.search);
    const nextSearch = query.get('ps_q') || '';
    const nextWorkflow = normalizeProjectStatsWorkflowFilter(query.get('ps_workflow'));
    const nextSort = normalizeProjectStatsSort(query.get('ps_sort'));
    const nextWindow = normalizeUsageWindow(query.get('ps_window'));

    setProjectStatsSearch((prev) => (prev === nextSearch ? prev : nextSearch));
    setProjectStatsWorkflowFilter((prev) => (prev === nextWorkflow ? prev : nextWorkflow));
    setProjectStatsSort((prev) => (prev === nextSort ? prev : nextSort));
    setUsageWindow((prev) => (prev === nextWindow ? prev : nextWindow));
  }, [location.search]);

  useEffect(() => {
    const query = new URLSearchParams(location.search);
    const next = new URLSearchParams(query);

    if (projectStatsSearch.trim()) {
      next.set('ps_q', projectStatsSearch.trim());
    } else {
      next.delete('ps_q');
    }

    if (projectStatsWorkflowFilter === 'all') {
      next.delete('ps_workflow');
    } else {
      next.set('ps_workflow', projectStatsWorkflowFilter);
    }

    if (projectStatsSort === 'last_desc') {
      next.delete('ps_sort');
    } else {
      next.set('ps_sort', projectStatsSort);
    }

    if (usageWindow === '90d') {
      next.delete('ps_window');
    } else {
      next.set('ps_window', usageWindow);
    }

    const currentSearch = query.toString();
    const nextSearch = next.toString();
    if (currentSearch === nextSearch) return;
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : ''
      },
      { replace: true }
    );
  }, [
    projectStatsSearch,
    projectStatsWorkflowFilter,
    projectStatsSort,
    usageWindow,
    location.pathname,
    location.search,
    navigate
  ]);

  const createApiToken = async (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (!session?.userId) return;

    setTokenCreating(true);
    setError(null);
    setSuccess(null);

    try {
      if (!selectedProjectId) {
        throw new Error('Please select a project.');
      }
      const label = newTokenName.trim() || shortUuidLike();
      let plain = `vbio_${randomAlphaNum(36)}`;
      let tokenHash = await sha256Hex(plain);

      const expiresDays = Number(newTokenExpiresDays);
      const expiresAt = Number.isFinite(expiresDays) && expiresDays > 0
        ? new Date(Date.now() + expiresDays * 24 * 60 * 60 * 1000).toISOString()
        : null;

      for (let attempt = 0; attempt < 5; attempt += 1) {
        const existing = await findApiTokenByHash(tokenHash);
        if (!existing) break;
        plain = `vbio_${randomAlphaNum(36)}`;
        tokenHash = await sha256Hex(plain);
      }

      const existing = await findApiTokenByHash(tokenHash);
      let saved: ApiToken;
      if (existing) {
        if (existing.user_id !== session.userId) {
          throw new Error('This token value is already registered by another account.');
        }
        saved = await updateApiToken(existing.id, {
          name: label,
          project_id: selectedProjectId,
          allow_submit: allowSubmit,
          allow_delete: allowDelete,
          allow_cancel: allowCancel,
          token_plain: plain,
          token_prefix: plain.slice(0, 12),
          token_last4: plain.slice(-4),
          scopes: ['runtime', 'project', 'task'],
          is_active: true,
          expires_at: expiresAt,
          revoked_at: null
        });
      } else {
        saved = await insertApiToken({
          user_id: session.userId,
          name: label,
          token_hash: tokenHash,
          token_plain: plain,
          project_id: selectedProjectId,
          allow_submit: allowSubmit,
          allow_delete: allowDelete,
          allow_cancel: allowCancel,
          token_prefix: plain.slice(0, 12),
          token_last4: plain.slice(-4),
          scopes: ['runtime', 'project', 'task'],
          is_active: true,
          expires_at: expiresAt,
          revoked_at: null
        });
      }

      setTokens((prev) => {
        const idx = prev.findIndex((item) => item.id === saved.id);
        if (idx >= 0) {
          const next = [...prev];
          next[idx] = saved;
          return next;
        }
        return [saved, ...prev];
      });
      setSelectedTokenId(saved.id);
      setNewTokenPlainText(plain);
      setNewTokenName(shortUuidLike());
      setNewTokenExpiresDays('');
      setAllowSubmit(true);
      setAllowDelete(true);
      setAllowCancel(true);
      setSuccess(existing ? 'Existing token updated.' : 'API token created. Copy it now; it will not be shown again.');
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to create API token.';
      if (message.includes('idx_api_tokens_token_hash') || message.includes('"code":"23505"')) {
        setError('Token hash already exists, please retry.');
      } else {
        setError(message);
      }
    } finally {
      setTokenCreating(false);
    }
  };

  const revokeToken = async (tokenId: string) => {
    setTokenRevokingId(tokenId);
    setError(null);
    setSuccess(null);
    try {
      const updated = await revokeApiToken(tokenId);
      setTokens((prev) => prev.map((item) => (item.id === updated.id ? updated : item)));
      setSuccess('API token revoked.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to revoke API token.');
    } finally {
      setTokenRevokingId(null);
    }
  };

  const removeToken = async (tokenId: string) => {
    const confirmed = window.confirm('Delete this token permanently? Usage records are kept but detached from the token.');
    if (!confirmed) return;

    setTokenDeletingId(tokenId);
    setError(null);
    setSuccess(null);
    try {
      await deleteApiToken(tokenId);
      setTokens((prev) => {
        const next = prev.filter((item) => item.id !== tokenId);
        setSelectedTokenId((current) => (current === tokenId ? next[0]?.id || '' : current));
        return next;
      });
      setSuccess('API token deleted.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete API token.');
    } finally {
      setTokenDeletingId(null);
    }
  };

  const selectedToken = useMemo(() => tokens.find((item) => item.id === selectedTokenId) || null, [tokens, selectedTokenId]);
  const selectedTokenUsageSummary = useMemo(() => {
    return computeUsageSummaryFromDaily(tokenUsageDaily, selectedToken?.last_used_at || null);
  }, [tokenUsageDaily, selectedToken?.last_used_at]);

  useEffect(() => {
    setBuilderTokenPlainInput(String(selectedToken?.token_plain || '').trim());
  }, [selectedTokenId, selectedToken?.token_plain]);

  useEffect(() => {
    if (isProjectScoped) {
      if (selectedProjectId !== scopedProjectId) {
        setSelectedProjectId(scopedProjectId);
      }
      return;
    }
    const tokenProjectId = selectedToken?.project_id || '';
    if (tokenProjectId && tokenProjectId !== selectedProjectId) {
      setSelectedProjectId(tokenProjectId);
    }
  }, [selectedToken?.project_id, selectedProjectId, isProjectScoped, scopedProjectId]);

  const selectedTokenProjectId = (isProjectScoped ? scopedProjectId : selectedProjectId) || selectedToken?.project_id || '<PROJECT_UUID>';
  const selectedProject = useMemo(
    () => projects.find((item) => item.id === selectedTokenProjectId) || null,
    [projects, selectedTokenProjectId]
  );
  const selectedWorkflow = useMemo(() => getWorkflowDefinition(selectedProject?.task_type), [selectedProject?.task_type]);
  const selectedBackend = String(selectedProject?.backend || 'boltz').trim().toLowerCase() || 'boltz';
  const isAffinityWorkflow = selectedWorkflow.key === 'affinity';
  const isPredictionWorkflow = selectedWorkflow.key === 'prediction';
  const isSupportedSubmitWorkflow = isPredictionWorkflow || isAffinityWorkflow;
  const effectivePredictionBackend: PredictionBackend = normalizePredictionBackend(builderPredictionBackend);
  const effectiveAffinityBackend: AffinityBackend = normalizeAffinityBackend(builderAffinityBackend);
  const builderWorkflowKey: BuilderWorkflowKey = isAffinityWorkflow ? 'affinity' : 'prediction';
  const selectedProjectTokens = useMemo(
    () => tokens.filter((item) => item.project_id === selectedTokenProjectId),
    [tokens, selectedTokenProjectId]
  );
  const projectStatsRows = useMemo<ProjectStatsRow[]>(() => {
    const visibleProjects = isProjectScoped
      ? projects.filter((project) => project.id === scopedProjectId)
      : projects;
    return visibleProjects.map((project) => {
      const projectTokens = tokens.filter((token) => token.project_id === project.id);
      const totalCalls = projectTokens.reduce((acc, token) => acc + (usageByTokenId[token.id]?.total || 0), 0);
      const successCalls = projectTokens.reduce((acc, token) => acc + (usageByTokenId[token.id]?.success || 0), 0);
      const successRate = totalCalls > 0 ? (successCalls / totalCalls) * 100 : 0;
      const workflow = getWorkflowDefinition(project.task_type);
      const workflowKey: ProjectStatsWorkflowFilter = workflow.key === 'affinity' ? 'affinity' : 'prediction';
      const lastEventAt = projectTokens.reduce<string | null>((latest, token) => {
        const current = usageByTokenId[token.id]?.lastEventAt || null;
        if (!current) return latest;
        if (!latest) return current;
        return Date.parse(current) > Date.parse(latest) ? current : latest;
      }, null);
      const lastEventTs = lastEventAt ? Date.parse(lastEventAt) : 0;
      return {
        project,
        workflowKey,
        workflowLabel: workflow.shortTitle,
        tokenCount: projectTokens.length,
        activeTokenCount: projectTokens.filter((token) => token.is_active).length,
        totalCalls,
        successRate,
        lastEventAt,
        lastEventTs: Number.isFinite(lastEventTs) ? lastEventTs : 0
      };
    });
  }, [projects, tokens, usageByTokenId, isProjectScoped, scopedProjectId]);
  const projectTokensByProjectId = useMemo(() => {
    const grouped: Record<string, ApiToken[]> = {};
    for (const token of tokens) {
      const projectId = String(token.project_id || '').trim();
      if (!projectId) continue;
      if (!grouped[projectId]) {
        grouped[projectId] = [];
      }
      grouped[projectId].push(token);
    }
    for (const projectId of Object.keys(grouped)) {
      grouped[projectId].sort((a, b) => {
        if (a.is_active !== b.is_active) return a.is_active ? -1 : 1;
        return a.name.localeCompare(b.name);
      });
    }
    return grouped;
  }, [tokens]);

  const filteredProjectStatsRows = useMemo(() => {
    const keyword = projectStatsSearch.trim().toLowerCase();
    let rows = projectStatsRows.filter((item) => {
      if (projectStatsWorkflowFilter !== 'all' && item.workflowKey !== projectStatsWorkflowFilter) return false;
      if (!keyword) return true;
      const hay = `${item.project.name} ${item.workflowLabel}`.toLowerCase();
      return hay.includes(keyword);
    });

    rows = [...rows].sort((a, b) => {
      switch (projectStatsSort) {
        case 'calls_asc':
          return a.totalCalls - b.totalCalls;
        case 'calls_desc':
          return b.totalCalls - a.totalCalls;
        case 'success_asc':
          return a.successRate - b.successRate;
        case 'success_desc':
          return b.successRate - a.successRate;
        case 'last_asc':
          return a.lastEventTs - b.lastEventTs;
        case 'last_desc':
        default:
          return b.lastEventTs - a.lastEventTs;
      }
    });
    return rows;
  }, [projectStatsRows, projectStatsSearch, projectStatsWorkflowFilter, projectStatsSort]);

  useEffect(() => {
    setProjectStatsPage(1);
  }, [projectStatsSearch, projectStatsWorkflowFilter, projectStatsSort]);

  const projectStatsPageCount = Math.max(1, Math.ceil(filteredProjectStatsRows.length / PROJECT_STATS_PAGE_SIZE));
  useEffect(() => {
    if (projectStatsPage > projectStatsPageCount) {
      setProjectStatsPage(projectStatsPageCount);
    }
  }, [projectStatsPage, projectStatsPageCount]);

  const pagedProjectStatsRows = useMemo(() => {
    const start = (projectStatsPage - 1) * PROJECT_STATS_PAGE_SIZE;
    return filteredProjectStatsRows.slice(start, start + PROJECT_STATS_PAGE_SIZE);
  }, [filteredProjectStatsRows, projectStatsPage]);
  const registryScopeProject = useMemo(
    () => projects.find((project) => project.id === registryScopeProjectId) || null,
    [projects, registryScopeProjectId]
  );
  const showRegistryProjectColumn = !registryScopeProjectId && !isProjectScoped;
  const registryTokensSource = useMemo(() => {
    if (isProjectScoped) {
      return tokens.filter((token) => String(token.project_id || '').trim() === scopedProjectId);
    }
    if (!registryScopeProjectId) return tokens;
    return tokens.filter((token) => token.project_id === registryScopeProjectId);
  }, [tokens, registryScopeProjectId, isProjectScoped, scopedProjectId]);
  const projectTokenPanelProject = useMemo(
    () => projects.find((project) => project.id === projectTokenPanelProjectId) || null,
    [projects, projectTokenPanelProjectId]
  );
  const projectTokenPanelTokens = useMemo(() => {
    if (!projectTokenPanelProjectId) return [];
    return projectTokensByProjectId[projectTokenPanelProjectId] || [];
  }, [projectTokenPanelProjectId, projectTokensByProjectId]);

  useEffect(() => {
    setBuilderPredictionBackend(normalizePredictionBackend(selectedBackend));
    setBuilderAffinityBackend(normalizeAffinityBackend(selectedBackend));
  }, [selectedBackend, selectedTokenProjectId]);

  useEffect(() => {
    if (!isAffinityWorkflow) return;
    const useMsa = Boolean(selectedProject?.use_msa);
    setBuilderUseMsaAffinity(useMsa);
    setBuilderUseMsaProtenix(useMsa);
    setBuilderAffinityConfidenceOnly(true);
    setBuilderAffinityTargetChain('A');
    setBuilderAffinityLigandChain('L');
    setBuilderAffinityLigandSmiles('');
  }, [isAffinityWorkflow, selectedTokenProjectId, selectedProject?.use_msa]);

  useEffect(() => {
    if (!isPredictionWorkflow) return;
    const protein = String(selectedProject?.protein_sequence || '').trim();
    const ligand = String(selectedProject?.ligand_smiles || '').trim();
    if (!protein && !ligand) return;
    setBuilderYamlComponents((prev) => {
      const hasUserContent = prev.some((component) => String(component.sequence || '').trim().length > 0);
      if (hasUserContent) return prev;
      const next: InputComponent[] = [];
      if (protein) {
        const proteinComponent = createYamlBuilderComponent('protein');
        proteinComponent.sequence = protein;
        next.push(proteinComponent);
      }
      if (ligand) {
        const ligandComponent = createYamlBuilderComponent('ligand');
        ligandComponent.sequence = ligand;
        next.push(ligandComponent);
      }
      return next.length > 0 ? next : prev;
    });
  }, [isPredictionWorkflow, selectedTokenProjectId, selectedProject?.protein_sequence, selectedProject?.ligand_smiles]);

  useEffect(() => {
    if (!isProjectScoped) return;
    const scopedPrefillKey = `${scopedProjectId}|${scopedTaskRowId}|${scopedTaskName}|${scopedTaskSummary}`;
    if (scopedTaskPrefillRef.current === scopedPrefillKey) return;
    scopedTaskPrefillRef.current = scopedPrefillKey;
    if (scopedTaskName) {
      setBuilderTaskName(scopedTaskName);
    }
    if (scopedTaskSummary) {
      setBuilderTaskSummary(scopedTaskSummary);
    }
  }, [isProjectScoped, scopedProjectId, scopedTaskRowId, scopedTaskName, scopedTaskSummary]);

  useEffect(() => {
    if (!isProjectScoped || !scopedTaskRowId) return;
    let cancelled = false;
    const loadTaskSnapshot = async () => {
      try {
        const task = await getProjectTaskById(scopedTaskRowId);
        if (cancelled || !task) return;
        if (String(task.project_id || '').trim() !== scopedProjectId) return;

        const taskName = String(task.name || '').trim();
        const taskSummary = String(task.summary || '').trim();
        if (taskName) setBuilderTaskName(taskName);
        if (taskSummary) setBuilderTaskSummary(taskSummary);

        const taskBackend = String(task.backend || '').trim();
        if (taskBackend) {
          setBuilderPredictionBackend(normalizePredictionBackend(taskBackend));
          setBuilderAffinityBackend(normalizeAffinityBackend(taskBackend));
        }

        const taskComponents = Array.isArray(task.components)
          ? normalizeInputComponents(task.components.filter((component) => !isAffinityUploadComponent(component)) as InputComponent[])
          : [];
        if (taskComponents.length > 0) {
          setBuilderYamlComponents(taskComponents);
        }
        if (Array.isArray(task.constraints)) {
          setBuilderYamlConstraints(task.constraints);
          setBuilderYamlConstraintsOpen(task.constraints.length > 0);
        }
        const taskProperties =
          task.properties && typeof task.properties === 'object'
            ? (task.properties as PredictionProperties)
            : null;
        if (taskProperties) {
          setBuilderYamlProperties({
            ...EMPTY_PREDICTION_PROPERTIES,
            ...taskProperties
          });
        }
      } catch {
        // ignore task prefill failures; manual builder setup is still available
      }
    };
    void loadTaskSnapshot();
    return () => {
      cancelled = true;
    };
  }, [isProjectScoped, scopedProjectId, scopedTaskRowId]);

  const filteredTokens = useMemo(() => {
    const keyword = tokenQuery.trim().toLowerCase();
    if (!keyword) return registryTokensSource;
    return registryTokensSource.filter((item) => {
      const hay = `${item.name} ${item.token_prefix} ${item.token_last4}`.toLowerCase();
      return hay.includes(keyword);
    });
  }, [registryTokensSource, tokenQuery]);

  const tokenPageCount = Math.max(1, Math.ceil(filteredTokens.length / TOKEN_PAGE_SIZE));
  useEffect(() => {
    setTokenPage(1);
  }, [tokenQuery]);
  useEffect(() => {
    if (tokenPage > tokenPageCount) {
      setTokenPage(tokenPageCount);
    }
  }, [tokenPage, tokenPageCount]);

  const pagedTokens = useMemo(() => {
    const start = (tokenPage - 1) * TOKEN_PAGE_SIZE;
    return filteredTokens.slice(start, start + TOKEN_PAGE_SIZE);
  }, [filteredTokens, tokenPage]);

  const eventPageCount = Math.max(1, Math.ceil(tokenUsageTotal / EVENT_PAGE_SIZE));
  useEffect(() => {
    setEventPage(1);
  }, [selectedTokenId, usageWindow]);
  useEffect(() => {
    if (eventPage > eventPageCount) {
      setEventPage(eventPageCount);
    }
  }, [eventPage, eventPageCount]);
  const usageBarsPageCount = Math.max(1, Math.ceil(tokenUsageDaily.length / DAILY_USAGE_PAGE_SIZE));
  useEffect(() => {
    setUsageBarsPage(usageBarsPageCount);
  }, [selectedTokenId, usageWindow, usageBarsPageCount]);
  useEffect(() => {
    if (usageBarsPage > usageBarsPageCount) {
      setUsageBarsPage(usageBarsPageCount);
    }
  }, [usageBarsPage, usageBarsPageCount]);
  const pagedDailyUsage = useMemo(() => {
    const start = (usageBarsPage - 1) * DAILY_USAGE_PAGE_SIZE;
    return tokenUsageDaily.slice(start, start + DAILY_USAGE_PAGE_SIZE);
  }, [tokenUsageDaily, usageBarsPage]);
  const maxDailyCount = useMemo(
    () => Math.max(1, ...pagedDailyUsage.map((item) => item.total_count)),
    [pagedDailyUsage]
  );

  const clampBuilderLeftWidth = (value: number): number => Math.min(60, Math.max(28, value));
  const clampYamlBuilderLeftWidth = (value: number): number => Math.min(82, Math.max(52, value));

  const handleBuilderResizerPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    if (window.matchMedia('(max-width: 1100px)').matches) return;
    const grid = builderGridRef.current;
    if (!grid) return;
    builderResizeRef.current = {
      startX: event.clientX,
      startWidthPercent: builderLeftWidth
    };
    setIsBuilderResizing(true);
    event.preventDefault();
  };

  const handleBuilderResizerKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setBuilderLeftWidth((current) => clampBuilderLeftWidth(current - 1.5));
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setBuilderLeftWidth((current) => clampBuilderLeftWidth(current + 1.5));
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      setBuilderLeftWidth(38);
    }
  };

  const handleYamlBuilderResizerPointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (event.button !== 0) return;
    if (window.matchMedia('(max-width: 1100px)').matches) return;
    const grid = yamlBuilderGridRef.current;
    if (!grid) return;
    yamlBuilderResizeRef.current = {
      startX: event.clientX,
      startWidthPercent: yamlBuilderLeftWidth
    };
    setIsYamlBuilderResizing(true);
    event.preventDefault();
  };

  const handleYamlBuilderResizerKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      setYamlBuilderLeftWidth((current) => clampYamlBuilderLeftWidth(current - 1.5));
      return;
    }
    if (event.key === 'ArrowRight') {
      event.preventDefault();
      setYamlBuilderLeftWidth((current) => clampYamlBuilderLeftWidth(current + 1.5));
      return;
    }
    if (event.key === 'Home') {
      event.preventDefault();
      setYamlBuilderLeftWidth(68);
    }
  };

  useEffect(() => {
    if (!isBuilderResizing) return;
    const onPointerMove = (event: PointerEvent) => {
      const context = builderResizeRef.current;
      const grid = builderGridRef.current;
      if (!context || !grid) return;
      const rect = grid.getBoundingClientRect();
      if (rect.width <= 0) return;
      const deltaPercent = ((event.clientX - context.startX) / rect.width) * 100;
      const next = clampBuilderLeftWidth(context.startWidthPercent + deltaPercent);
      setBuilderLeftWidth(next);
    };
    const onPointerUp = () => {
      builderResizeRef.current = null;
      setIsBuilderResizing(false);
    };
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
    };
  }, [isBuilderResizing]);

  useEffect(() => {
    if (!isYamlBuilderResizing) return;
    const onPointerMove = (event: PointerEvent) => {
      const context = yamlBuilderResizeRef.current;
      const grid = yamlBuilderGridRef.current;
      if (!context || !grid) return;
      const rect = grid.getBoundingClientRect();
      if (rect.width <= 0) return;
      const deltaPercent = ((event.clientX - context.startX) / rect.width) * 100;
      const next = clampYamlBuilderLeftWidth(context.startWidthPercent + deltaPercent);
      setYamlBuilderLeftWidth(next);
    };
    const onPointerUp = () => {
      yamlBuilderResizeRef.current = null;
      setIsYamlBuilderResizing(false);
    };
    window.addEventListener('pointermove', onPointerMove);
    window.addEventListener('pointerup', onPointerUp);
    return () => {
      window.removeEventListener('pointermove', onPointerMove);
      window.removeEventListener('pointerup', onPointerUp);
    };
  }, [isYamlBuilderResizing]);

  const builderGridStyle = useMemo<ApiBuilderGridStyle>(
    () => ({
      '--api-builder-left-width': `${builderLeftWidth.toFixed(2)}%`
    }),
    [builderLeftWidth]
  );
  const yamlBuilderGridStyle = useMemo<ApiBuilderGridStyle>(
    () => ({
      '--api-yaml-left-width': `${yamlBuilderLeftWidth.toFixed(2)}%`
    }),
    [yamlBuilderLeftWidth]
  );

  const curlToken = String(builderTokenPlainInput || '').trim() || String(selectedToken?.token_plain || '').trim() || String(newTokenPlainText || '').trim() || '<YOUR_API_TOKEN>';
  const taskIdForCommand = '${TASK_ID}';
  const taskNameForCommand = builderTaskName.trim();
  const taskSummaryForCommand = builderTaskSummary.trim();
  const escapedTaskName = escapeForDoubleQuotedShell(taskNameForCommand);
  const escapedTaskSummary = escapeForDoubleQuotedShell(taskSummaryForCommand);
  const submitTaskMetaFlags = `${taskNameForCommand ? ` \\\n  -F "task_name=${escapedTaskName}"` : ''}${taskSummaryForCommand ? ` \\\n  -F "task_summary=${escapedTaskSummary}"` : ''}`;
  const escapedResultPath = escapeForDoubleQuotedShell(builderResultPath.trim() || './result.zip');
  const escapedYamlPath = escapeForDoubleQuotedShell(builderYamlPath.trim() || './config.yaml');
  const normalizedYamlBuilderComponents = normalizeInputComponents(builderYamlComponents);
  const yamlComponentStats = useMemo(() => {
    const stats: Record<InputComponent['type'], number> = {
      protein: 0,
      ligand: 0,
      dna: 0,
      rna: 0
    };
    for (const component of normalizedYamlBuilderComponents) {
      stats[component.type] += 1;
    }
    return stats;
  }, [normalizedYamlBuilderComponents]);
  const yamlAssignments = assignChainIdsForComponents(normalizedYamlBuilderComponents);
  const predictionTemplateEntries = normalizedYamlBuilderComponents.flatMap((component, index) => {
    if (component.type !== 'protein') return [];
    const config = builderYamlTemplates[component.id];
    const path = String(config?.path || '').trim();
    if (!path) return [];
    const format = inferTemplateFormat(path, config?.format || 'auto');
    const templateChain = normalizeChainId(config?.templateChain || '', 'A');
    const targetChains = String(config?.targetChains || '')
      .split(',')
      .map((item) => normalizeChainId(item, ''))
      .filter(Boolean);
    const assignmentChains = yamlAssignments[index] || [];
    const fileName = extractFileNameFromPath(path) || `template_${component.id}.${format === 'pdb' ? 'pdb' : 'cif'}`;
    const targetChainIds = targetChains.length > 0 ? targetChains : (assignmentChains.length > 0 ? assignmentChains : ['A']);
    return [{
      componentId: component.id,
      path,
      escapedPath: escapeForDoubleQuotedShell(path),
      fileName,
      format,
      templateChainId: templateChain,
      targetChainIds
    }];
  });
  const predictionTemplateEnabled = predictionTemplateEntries.length > 0;
  const yamlBuilderText = (() => {
    if (normalizedYamlBuilderComponents.length === 0) {
      return 'version: 1\nsequences: []';
    }
    try {
      return buildPredictionYamlFromComponents(normalizedYamlBuilderComponents, {
        constraints: builderYamlConstraints,
        properties: builderYamlProperties,
        templates: predictionTemplateEntries.map((entry) => ({
          fileName: entry.fileName,
          format: entry.format,
          templateChainId: entry.templateChainId,
          targetChainIds: entry.targetChainIds
        }))
      });
    } catch {
      return '# YAML generation failed. Please check component content.';
    }
  })();
  const predictionTemplateFlags = predictionTemplateEnabled
    ? predictionTemplateEntries.map((entry) => ` \\\n  -F "template_files=@${entry.escapedPath}"`).join('')
    : '';
  const escapedTargetPath = escapeForDoubleQuotedShell(builderTargetPath.trim() || './target.cif');
  const escapedLigandPath = escapeForDoubleQuotedShell(builderLigandPath.trim() || './ligand.sdf');
  const escapedComplexPath = escapeForDoubleQuotedShell(builderComplexPath.trim() || './complex.cif');
  const affinityTargetChain = String(builderAffinityTargetChain || '').trim();
  const affinityLigandChain = String(builderAffinityLigandChain || '').trim();
  const affinityLigandSmiles = String(builderAffinityLigandSmiles || '').trim();
  const affinityCanEnableActivity =
    !builderAffinityConfidenceOnly &&
    Boolean(affinityTargetChain && affinityLigandChain && affinityLigandSmiles);
  const affinityActivityFlags = affinityCanEnableActivity
    ? (() => {
      const affinitySmilesMap = escapeForDoubleQuotedShell(
        JSON.stringify({ [affinityLigandChain]: affinityLigandSmiles })
      );
      return ` \\\n  -F "enable_affinity=true" \\
  -F "auto_enable_affinity=true" \\
  -F "target_chain=${escapeForDoubleQuotedShell(affinityTargetChain)}" \\
  -F "ligand_chain=${escapeForDoubleQuotedShell(affinityLigandChain)}" \\
  -F "ligand_smiles_map=${affinitySmilesMap}"`;
    })()
    : '';
  const affinityModeHint =
    !builderAffinityConfidenceOnly && !affinityCanEnableActivity
      ? '\n# Affinity mode requires target chain, ligand chain, and ligand SMILES.'
      : '';
  const commandEnv = `export VBIO_API_BASE="${managementApiBaseUrl}"\nexport VBIO_API_TOKEN="${curlToken}"\nexport VBIO_PROJECT_ID="${selectedTokenProjectId}"`;
  const submitTaskIdCapture = `echo "$RESPONSE"
TASK_ID=$(printf '%s' "$RESPONSE" | tr -d '\\n\\r' | sed -n 's/.*"task_id"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')
if [ -z "$TASK_ID" ] || [ "$TASK_ID" = "null" ]; then
  echo "Failed to parse task_id from submit response." >&2
  exit 1
fi
echo "TASK_ID=$TASK_ID"`;
  const commandSubmitPrediction = `RESPONSE=$(curl -X POST "${managementApiBaseUrl}/predict" \\
  -H "X-API-Token: ${curlToken}" \\
  -F "project_id=${selectedTokenProjectId}"${submitTaskMetaFlags} \\
  -F "yaml_file=@${escapedYamlPath}" \\
  -F "backend=${effectivePredictionBackend}"${predictionTemplateFlags})
${submitTaskIdCapture}`;
  const commandSubmitAffinityBoltz = `RESPONSE=$(curl -X POST "${managementApiBaseUrl}/api/boltz2score" \\
  -H "X-API-Token: ${curlToken}" \\
  -F "project_id=${selectedTokenProjectId}"${submitTaskMetaFlags} \\
  -F "protein_file=@${escapedTargetPath}" \\
  -F "ligand_file=@${escapedLigandPath}" \\
  -F "backend=boltz" \\
  -F "use_msa_server=${builderUseMsaAffinity ? 'true' : 'false'}" \\
  -F "structure_refine=false" \\
  -F "recycling_steps=20" \\
  -F "sampling_steps=1" \\
  -F "diffusion_samples=1" \\
  -F "max_parallel_samples=1" \\
  -F "priority=high"${affinityActivityFlags})
${submitTaskIdCapture}`;
  const commandSubmitAffinityProtenix = `RESPONSE=$(curl -X POST "${managementApiBaseUrl}/api/protenix2score" \\
  -H "X-API-Token: ${curlToken}" \\
  -F "project_id=${selectedTokenProjectId}"${submitTaskMetaFlags} \\
  -F "input_file=@${escapedComplexPath}" \\
  -F "use_msa=${builderUseMsaProtenix ? 'true' : 'false'}" \\
  -F "use_template=false" \\
  -F "priority=high"${affinityActivityFlags})
${submitTaskIdCapture}`;
  const commandSubmit = !isSupportedSubmitWorkflow
    ? `# Workflow "${selectedWorkflow.title}" is not supported in Command Builder.\n# Use project workflows: Prediction or Affinity.`
    : (builderWorkflowKey === 'affinity'
      ? (effectiveAffinityBackend === 'protenix' ? commandSubmitAffinityProtenix : commandSubmitAffinityBoltz)
      : commandSubmitPrediction);
  const commandSubmitWithHints = `${commandSubmit}${affinityModeHint}`;
  const submitBackendLabel = builderWorkflowKey === 'affinity' ? effectiveAffinityBackend : effectivePredictionBackend;
  const commandStatus = `curl -X GET "${managementApiBaseUrl}/status/${taskIdForCommand}?project_id=${selectedTokenProjectId}" \\
  -H "X-API-Token: ${curlToken}"`;
  const commandResults = `curl -X GET "${managementApiBaseUrl}/results/${taskIdForCommand}?project_id=${selectedTokenProjectId}" \\
  -H "X-API-Token: ${curlToken}" \\
  -o "${escapedResultPath}"`;
  const commandTaskAction = `curl -X DELETE "${managementApiBaseUrl}/tasks/${taskIdForCommand}?project_id=${selectedTokenProjectId}&operation_mode=${builderTaskOperation}" \\
  -H "X-API-Token: ${curlToken}"`;

  const rememberCommandHistory = (label: string, command: string) => {
    const entry: CommandHistoryEntry = {
      id: typeof globalThis.crypto?.randomUUID === 'function' ? globalThis.crypto.randomUUID() : `hist_${Date.now()}`,
      createdAt: new Date().toISOString(),
      label,
      command,
      workflow: builderWorkflowKey,
      backend: submitBackendLabel,
      projectId: selectedTokenProjectId,
      projectName: selectedProject?.name || '',
      tokenId: selectedTokenId,
      tokenName: selectedToken?.name || ''
    };
    setCommandHistory((prev) => [entry, ...prev.filter((item) => item.command !== command)].slice(0, COMMAND_HISTORY_LIMIT));
  };

  const copyText = async (text: string, okMessage: string, historyLabel?: string, copyId?: string) => {
    let copied = false;
    try {
      if (navigator?.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        copied = true;
      }
    } catch {
      copied = false;
    }
    if (!copied) {
      copied = fallbackCopyText(text);
    }
    if (!copied) {
      setError('Copy failed. Clipboard permission may be blocked in this context.');
      return;
    }
    if (historyLabel) {
      rememberCommandHistory(historyLabel, text);
    }
    if (copyId) {
      setCopiedActionId(copyId);
      if (copiedResetTimerRef.current !== null) {
        window.clearTimeout(copiedResetTimerRef.current);
      }
      copiedResetTimerRef.current = window.setTimeout(() => {
        setCopiedActionId((prev) => (prev === copyId ? '' : prev));
      }, 1200);
    }
    setSuccess(okMessage);
  };

  const downloadGeneratedYaml = () => {
    if (typeof document === 'undefined' || typeof window === 'undefined') return;
    try {
      const requestedName = extractFileNameFromPath(builderYamlPath.trim());
      const fileName = requestedName
        ? (/\.(ya?ml)$/i.test(requestedName) ? requestedName : `${requestedName}.yaml`)
        : 'config.yaml';
      const blob = new Blob([yamlBuilderText], { type: 'text/yaml;charset=utf-8' });
      const url = window.URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = fileName;
      document.body.appendChild(anchor);
      anchor.click();
      document.body.removeChild(anchor);
      window.URL.revokeObjectURL(url);
      setSuccess(`Generated YAML downloaded: ${fileName}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to download generated YAML.');
    }
  };

  const selectProjectContext = (projectId: string) => {
    if (isProjectScoped && projectId !== scopedProjectId) return;
    setSelectedProjectId(projectId);
    const projectTokens = tokens.filter((item) => item.project_id === projectId);
    if (projectTokens.length === 0) {
      setSelectedTokenId('');
      return;
    }
    const preferred = projectTokens.find((item) => item.is_active) || projectTokens[0];
    setSelectedTokenId(preferred.id);
  };

  const openTokenRegistry = (projectId: string | null = null) => {
    const targetProjectId = projectId || (isProjectScoped ? scopedProjectId : null);
    setRegistryScopeProjectId(targetProjectId);
    if (targetProjectId) {
      selectProjectContext(targetProjectId);
    }
    setNewTokenExpiresDays('');
    setAllowSubmit(true);
    setAllowDelete(true);
    setAllowCancel(true);
    setTokenQuery('');
    setTokenPage(1);
    setRegistryOpen(true);
  };

  const closeTokenRegistry = () => {
    setRegistryOpen(false);
    setRegistryScopeProjectId(isProjectScoped ? scopedProjectId : null);
  };

  const openTokenRegistryForProject = (projectId: string) => {
    openTokenRegistry(projectId);
  };

  const openProjectTokenPanel = (projectId: string) => {
    if (isProjectScoped && projectId !== scopedProjectId) return;
    selectProjectContext(projectId);
    setProjectTokenPanelProjectId(projectId);
  };

  const applyCommandHistory = (entry: CommandHistoryEntry) => {
    if (isProjectScoped && entry.projectId && entry.projectId !== scopedProjectId) {
      setError('This command history entry belongs to another project.');
      return;
    }
    if (entry.projectId) {
      selectProjectContext(entry.projectId);
    }
    if (entry.tokenId && tokens.some((item) => item.id === entry.tokenId)) {
      setSelectedTokenId(entry.tokenId);
    }
    if (entry.workflow === 'affinity') {
      setBuilderAffinityBackend(normalizeAffinityBackend(entry.backend));
    } else {
      setBuilderPredictionBackend(normalizePredictionBackend(entry.backend));
    }
    setSuccess(`Loaded command context from history: ${entry.label}`);
  };

  const addYamlBuilderComponent = (type: InputComponent['type']) => {
    setBuilderYamlComponents((prev) => [...prev, createYamlBuilderComponent(type)]);
  };

  const updateYamlBuilderComponent = (componentId: string, updater: (component: InputComponent) => InputComponent) => {
    setBuilderYamlComponents((prev) => prev.map((component) => (component.id === componentId ? updater(component) : component)));
  };

  const removeYamlBuilderComponent = (componentId: string) => {
    setBuilderYamlComponents((prev) => {
      const next = prev.filter((component) => component.id !== componentId);
      return next.length > 0 ? next : [createYamlBuilderComponent('protein')];
    });
    setBuilderYamlTemplates((prev) => {
      const next = { ...prev };
      delete next[componentId];
      return next;
    });
    setBuilderYamlCollapsed((prev) => {
      const next = { ...prev };
      delete next[componentId];
      return next;
    });
  };

  const updateYamlBuilderTemplate = (
    componentId: string,
    updater: (config: YamlProteinTemplateConfig) => YamlProteinTemplateConfig
  ) => {
    setBuilderYamlTemplates((prev) => {
      const current = prev[componentId] || {
        path: '',
        format: 'auto',
        templateChain: 'A',
        targetChains: ''
      };
      return {
        ...prev,
        [componentId]: updater(current)
      };
    });
  };

  const toggleYamlBuilderComponentCollapsed = (componentId: string) => {
    setBuilderYamlCollapsed((prev) => ({
      ...prev,
      [componentId]: !prev[componentId]
    }));
  };

  const jumpToCommandBuilder = () => {
    if (!commandPanelRef.current) return;
    commandPanelRef.current.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };

  const jumpToCommandBuilderForProject = (projectId: string) => {
    selectProjectContext(projectId);
    jumpToCommandBuilder();
  };

  const goBackToTaskList = () => {
    navigate(projectBackPath);
  };

  useEffect(() => {
    if (!openBuilderFromQuery) return;
    const scopedKey = `${scopedProjectId}|${scopedTaskRowId}|${scopedTaskId}`;
    if (openBuilderHandledRef.current === scopedKey) return;
    if (!commandPanelRef.current) return;
    openBuilderHandledRef.current = scopedKey;
    window.requestAnimationFrame(() => {
      commandPanelRef.current?.scrollIntoView({ behavior: 'smooth', block: 'start' });
    });
  }, [openBuilderFromQuery, scopedProjectId, scopedTaskRowId, scopedTaskId, selectedProjectId, selectedTokenId]);

  return (
    <div className={`page-grid api-access-page ${isProjectScoped ? 'is-project-scope' : ''}`}>
      <section className="page-header api-access-header">
        <div className="api-access-header-main">
          <div className="api-access-title-row">
            <h1>API Access</h1>
            {isProjectScoped && (
              <span className="api-access-project-chip">
                <ShieldCheck size={12} />
                {selectedProject?.name || 'Project'}
              </span>
            )}
          </div>
          <div className="api-access-context" aria-label="API access context">
            <span className="api-context-pill">
              <KeyRound size={12} />
              Tokens
            </span>
            <span className="api-context-pill">
              <BarChart3 size={12} />
              Usage
            </span>
            <span className="api-context-pill">
              <Download size={12} />
              Builder
            </span>
            {isProjectScoped && hasScopedTaskContext && (
              <span className="api-context-pill" title={scopedTaskContextTitle || undefined}>
                <Info size={12} />
                Task Prefill
              </span>
            )}
          </div>
        </div>
        {isProjectScoped && (
          <div className="api-access-actions" role="group" aria-label="API access quick actions">
            <button
              type="button"
              className="api-access-action-btn"
              onClick={goBackToTaskList}
              aria-label="Back to tasks"
              title="Back to tasks"
            >
              <ChevronLeft size={14} />
            </button>
            <button
              className={`api-access-action-btn ${registryOpen ? 'active' : ''}`}
              type="button"
              onClick={() => openTokenRegistry(scopedProjectId)}
              aria-label="Manage tokens"
              title="Manage tokens"
            >
              <ShieldCheck size={14} />
            </button>
          </div>
        )}
      </section>

      {!isProjectScoped && (
      <section className="panel api-project-stats-panel">
        <div className="api-section-head">
          <h2><BarChart3 size={16} /> Project Stats</h2>
        </div>
        <div className="api-project-stats-controls">
          <div className="api-project-stats-controls-left">
            <label className="field api-project-search-field">
              <span><Search size={12} /> Find</span>
              <input
                value={projectStatsSearch}
                onChange={(e) => setProjectStatsSearch(e.target.value)}
                placeholder="project / workflow"
              />
            </label>
            <label className="field api-project-filter-field">
              <span>Workflow</span>
              <select
                value={projectStatsWorkflowFilter}
                onChange={(e) => setProjectStatsWorkflowFilter(normalizeProjectStatsWorkflowFilter(e.target.value))}
              >
                <option value="all">All</option>
                <option value="prediction">Prediction</option>
                <option value="affinity">Affinity</option>
              </select>
            </label>
            <label className="field api-project-sort-field">
              <span>Sort</span>
              <select
                value={projectStatsSort}
                onChange={(e) => setProjectStatsSort(normalizeProjectStatsSort(e.target.value))}
              >
                <option value="last_desc">Last call (newest)</option>
                <option value="last_asc">Last call (oldest)</option>
                <option value="calls_desc">Calls (high to low)</option>
                <option value="calls_asc">Calls (low to high)</option>
                <option value="success_desc">Success (high to low)</option>
                <option value="success_asc">Success (low to high)</option>
              </select>
            </label>
          </div>
          <div className="api-project-stats-controls-right">
            <div className="api-range-switch" role="radiogroup" aria-label="Project stats window">
              <span className="api-range-icon" aria-hidden="true"><Clock3 size={13} /></span>
              {(['7d', '30d', '90d', 'all'] as UsageWindow[]).map((item) => (
                <button
                  key={item}
                  type="button"
                  className={`api-range-item ${usageWindow === item ? 'active' : ''}`}
                  onClick={() => setUsageWindow(item)}
                  aria-pressed={usageWindow === item}
                >
                  {item.toUpperCase()}
                </button>
              ))}
            </div>
            <button className="btn btn-secondary api-builder-jump-btn" type="button" onClick={jumpToCommandBuilder}>
              <KeyRound size={13} /> Open Builder
            </button>
          </div>
        </div>
        <div className="table-wrap api-project-table-wrap">
          <table className="table api-project-table">
            <thead>
              <tr>
                <th>Project</th>
                <th>Workflow</th>
                <th>Tokens</th>
                <th>Calls</th>
                <th>Success</th>
                <th>Last Call</th>
                <th>Actions</th>
              </tr>
            </thead>
            <tbody>
              {projectStatsLoading ? (
                <tr>
                  <td colSpan={7} className="muted">Loading project stats...</td>
                </tr>
              ) : pagedProjectStatsRows.length === 0 ? (
                <tr>
                  <td colSpan={7} className="muted">No projects.</td>
                </tr>
              ) : (
                pagedProjectStatsRows.map((item) => {
                  const isSelected = item.project.id === selectedTokenProjectId;
                  return (
                    <tr
                      key={item.project.id}
                      className={isSelected ? 'row-selected' : ''}
                      onClick={() => selectProjectContext(item.project.id)}
                    >
                      <td>{item.project.name}</td>
                      <td>
                        <span className={`api-workflow-pill workflow-${item.workflowKey}`}>
                          {item.workflowLabel}
                        </span>
                      </td>
                      <td>
                        <div className="api-project-token-stat">{item.activeTokenCount}/{item.tokenCount}</div>
                      </td>
                      <td>
                        <div className="api-project-calls-cell">
                          <div className="api-project-calls-head">
                            <BarChart3 size={12} />
                            <strong>{item.totalCalls}</strong>
                          </div>
                        </div>
                      </td>
                      <td>
                        <span className={`api-project-success-chip ${item.successRate >= 80 ? 'high' : item.successRate >= 50 ? 'mid' : 'low'}`}>
                          {item.successRate.toFixed(1)}%
                        </span>
                      </td>
                      <td>{item.lastEventAt ? formatIso(item.lastEventAt) : '-'}</td>
                      <td>
                        <div className="api-project-manage-actions">
                          <button
                            type="button"
                            className="api-project-builder-btn"
                            title="Open Builder"
                            aria-label="Open Builder"
                            onClick={(e) => {
                              e.stopPropagation();
                              jumpToCommandBuilderForProject(item.project.id);
                            }}
                          >
                            <ChevronRight size={12} />
                          </button>
                          <button
                            type="button"
                            className="api-project-token-view-btn"
                            title="View project tokens"
                            aria-label="View project tokens"
                            onClick={(e) => {
                              e.stopPropagation();
                              openProjectTokenPanel(item.project.id);
                            }}
                          >
                            <KeyRound size={12} />
                          </button>
                          <button
                            type="button"
                            className="api-project-manage-btn"
                            title="Open token registry"
                            aria-label="Open token registry"
                            onClick={(e) => {
                              e.stopPropagation();
                              openTokenRegistryForProject(item.project.id);
                            }}
                          >
                            <ShieldCheck size={12} />
                          </button>
                        </div>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        {filteredProjectStatsRows.length > PROJECT_STATS_PAGE_SIZE && (
          <div className="api-pager">
            <button
              type="button"
              className="icon-btn"
              onClick={() => setProjectStatsPage((prev) => Math.max(1, prev - 1))}
              disabled={projectStatsPage <= 1}
              title="Previous page"
              aria-label="Previous page"
            >
              <ChevronLeft size={14} />
            </button>
            <span className="muted small">{projectStatsPage} / {projectStatsPageCount}</span>
            <button
              type="button"
              className="icon-btn"
              onClick={() => setProjectStatsPage((prev) => Math.min(projectStatsPageCount, prev + 1))}
              disabled={projectStatsPage >= projectStatsPageCount}
              title="Next page"
              aria-label="Next page"
            >
              <ChevronRight size={14} />
            </button>
          </div>
        )}
      </section>
      )}
      <section className="panel api-command-panel" ref={commandPanelRef}>
        <div className="api-section-head">
          <h2><KeyRound size={16} /> Command Builder</h2>
          <p className="muted small">Select token, then copy the generated command set.</p>
        </div>
        <div
          ref={builderGridRef}
          className={`api-builder-grid api-builder-resizable ${isBuilderResizing ? 'is-resizing' : ''}`}
          style={builderGridStyle}
        >
          <aside className="api-builder-controls">
            <label className="field">
              <span><KeyRound size={12} /> Token</span>
              <select value={selectedTokenId} onChange={(e) => setSelectedTokenId(e.target.value)} disabled={tokens.length === 0}>
                {selectedProjectTokens.length === 0 ? (
                  <option value="">No tokens</option>
                ) : (
                  selectedProjectTokens.map((token) => (
                    <option key={token.id} value={token.id}>
                      {token.name} ({token.token_prefix}...{token.token_last4})
                    </option>
                  ))
                )}
              </select>
            </label>

            <label className="field">
              <span>Token Plaintext</span>
              <input
                value={builderTokenPlainInput}
                onChange={(e) => setBuilderTokenPlainInput(e.target.value)}
                placeholder="vbio_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
                disabled={!selectedTokenId}
              />
            </label>

            <div className="api-builder-project">
              <span className="muted small">Project (from token)</span>
              <strong>{selectedProject?.name || '-'}</strong>
              <code>{selectedTokenProjectId}</code>
            </div>

            {!isSupportedSubmitWorkflow && (
              <div className="api-builder-note muted small">
                Command Builder currently supports Prediction and Affinity projects only.
              </div>
            )}

            {isPredictionWorkflow && (
              <label className="field">
                <span>Prediction Backend</span>
                <select
                  value={effectivePredictionBackend}
                  onChange={(e) => setBuilderPredictionBackend(normalizePredictionBackend(e.target.value))}
                >
                  <option value="boltz">boltz</option>
                  <option value="alphafold3">alphafold3</option>
                  <option value="protenix">protenix</option>
                </select>
              </label>
            )}

            {isAffinityWorkflow && (
              <label className="field">
                <span>Affinity Backend</span>
                <select
                  value={effectiveAffinityBackend}
                  onChange={(e) => setBuilderAffinityBackend(normalizeAffinityBackend(e.target.value))}
                >
                  <option value="boltz">boltz</option>
                  <option value="protenix">protenix</option>
                </select>
              </label>
            )}

            <label className="field">
              <span>Task Name (optional)</span>
              <input value={builderTaskName} onChange={(e) => setBuilderTaskName(e.target.value)} placeholder="Only sent when filled" />
            </label>

            <label className="field">
              <span>Task Summary (optional)</span>
              <input value={builderTaskSummary} onChange={(e) => setBuilderTaskSummary(e.target.value)} placeholder="Only sent when filled" />
            </label>

            {isPredictionWorkflow && (
              <>
                <label className="field">
                  <span>YAML file path</span>
                  <input value={builderYamlPath} onChange={(e) => setBuilderYamlPath(e.target.value)} placeholder="./config.yaml" />
                </label>
                <div className="api-yaml-builder-trigger">
                  <button className="btn btn-secondary" type="button" onClick={() => setYamlBuilderOpen(true)}>
                    Open YAML Builder
                  </button>
                  <span className="muted small">
                    {builderYamlComponents.length} components · {predictionTemplateEnabled ? 'template configured' : 'no template'}
                  </span>
                </div>
                <div className="api-builder-note muted small">
                  YAML Builder uses component-based input (type / copies / sequence) and writes templates into generated YAML.
                </div>
              </>
            )}

            {isAffinityWorkflow && effectiveAffinityBackend === 'boltz' && (
              <>
                <div className="api-yaml-component-flags api-affinity-options">
                  <label className="checkbox-inline">
                    <input
                      type="checkbox"
                      checked={builderUseMsaAffinity}
                      onChange={(e) => setBuilderUseMsaAffinity(e.target.checked)}
                    />
                    <span>MSA</span>
                  </label>
                  <label className="checkbox-inline">
                    <input
                      type="checkbox"
                      checked={builderAffinityConfidenceOnly}
                      onChange={(e) => setBuilderAffinityConfidenceOnly(e.target.checked)}
                    />
                    <span>Confidence Only</span>
                  </label>
                </div>
                <label className="field">
                  <span>Target file path</span>
                  <input value={builderTargetPath} onChange={(e) => setBuilderTargetPath(e.target.value)} placeholder="./target.cif" />
                </label>
                <label className="field">
                  <span>Ligand file path</span>
                  <input value={builderLigandPath} onChange={(e) => setBuilderLigandPath(e.target.value)} placeholder="./ligand.sdf" />
                </label>
                {!builderAffinityConfidenceOnly && (
                  <>
                    <label className="field">
                      <span>Target chain</span>
                      <input
                        value={builderAffinityTargetChain}
                        onChange={(e) => setBuilderAffinityTargetChain(e.target.value)}
                        placeholder="A"
                      />
                    </label>
                    <label className="field">
                      <span>Ligand chain</span>
                      <input
                        value={builderAffinityLigandChain}
                        onChange={(e) => setBuilderAffinityLigandChain(e.target.value)}
                        placeholder="L"
                      />
                    </label>
                    <label className="field">
                      <span>Ligand SMILES</span>
                      <input
                        value={builderAffinityLigandSmiles}
                        onChange={(e) => setBuilderAffinityLigandSmiles(e.target.value)}
                        placeholder="Required for affinity mode"
                      />
                    </label>
                  </>
                )}
              </>
            )}

            {isAffinityWorkflow && effectiveAffinityBackend === 'protenix' && (
              <>
                <div className="api-yaml-component-flags api-affinity-options">
                  <label className="checkbox-inline">
                    <input
                      type="checkbox"
                      checked={builderUseMsaProtenix}
                      onChange={(e) => setBuilderUseMsaProtenix(e.target.checked)}
                    />
                    <span>MSA</span>
                  </label>
                  <label className="checkbox-inline">
                    <input
                      type="checkbox"
                      checked={builderAffinityConfidenceOnly}
                      onChange={(e) => setBuilderAffinityConfidenceOnly(e.target.checked)}
                    />
                    <span>Confidence Only</span>
                  </label>
                </div>
                <label className="field">
                  <span>Complex file path</span>
                  <input value={builderComplexPath} onChange={(e) => setBuilderComplexPath(e.target.value)} placeholder="./complex.cif" />
                </label>
                {!builderAffinityConfidenceOnly && (
                  <>
                    <label className="field">
                      <span>Target chain</span>
                      <input
                        value={builderAffinityTargetChain}
                        onChange={(e) => setBuilderAffinityTargetChain(e.target.value)}
                        placeholder="A"
                      />
                    </label>
                    <label className="field">
                      <span>Ligand chain</span>
                      <input
                        value={builderAffinityLigandChain}
                        onChange={(e) => setBuilderAffinityLigandChain(e.target.value)}
                        placeholder="L"
                      />
                    </label>
                    <label className="field">
                      <span>Ligand SMILES</span>
                      <input
                        value={builderAffinityLigandSmiles}
                        onChange={(e) => setBuilderAffinityLigandSmiles(e.target.value)}
                        placeholder="Required for affinity mode"
                      />
                    </label>
                  </>
                )}
              </>
            )}

            <label className="field">
              <span>Result ZIP path</span>
              <input value={builderResultPath} onChange={(e) => setBuilderResultPath(e.target.value)} placeholder="./result.zip" />
            </label>

            <label className="field">
              <span>Task operation</span>
              <select value={builderTaskOperation} onChange={(e) => setBuilderTaskOperation((e.target.value === 'delete' ? 'delete' : 'cancel'))}>
                <option value="cancel">cancel</option>
                <option value="delete">delete</option>
              </select>
            </label>
          </aside>

          <div
            className={`panel-resizer ${isBuilderResizing ? 'dragging' : ''}`}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize command builder panels"
            tabIndex={0}
            onPointerDown={handleBuilderResizerPointerDown}
            onKeyDown={handleBuilderResizerKeyDown}
          />

          <div className="api-command-right">
            <div className="api-command-list">
              <article className="api-command-item">
                <header>
                  <span>1. Environment</span>
                  <button className={`icon-btn ${copiedActionId === 'copy-env' ? 'is-copied' : ''}`} type="button" aria-label="Copy env command" onClick={() => { void copyText(commandEnv, 'Environment command copied.', 'Environment', 'copy-env'); }}>
                    <Copy size={14} />
                  </button>
                </header>
                <p className="muted small">Set runtime variables once in your local terminal session.</p>
                <pre><code>{commandEnv}</code></pre>
              </article>

              {isPredictionWorkflow && (
                <article className="api-command-item">
                  <header>
                    <span>YAML Preview</span>
                    <div className="api-yaml-preview-actions">
                      <button className="icon-btn" type="button" aria-label="Download generated YAML" onClick={downloadGeneratedYaml}>
                        <Download size={14} />
                      </button>
                      <button className={`icon-btn ${copiedActionId === 'copy-yaml-preview' ? 'is-copied' : ''}`} type="button" aria-label="Copy generated YAML" onClick={() => { void copyText(yamlBuilderText, 'Generated YAML copied.', 'YAML Preview', 'copy-yaml-preview'); }}>
                        <Copy size={14} />
                      </button>
                    </div>
                  </header>
                  <p className="muted small">Generated from YAML Builder inputs.</p>
                  <pre><code>{yamlBuilderText}</code></pre>
                </article>
              )}

              <article className="api-command-item">
                <header>
                  <span>
                    2. Submit ({!isSupportedSubmitWorkflow
                      ? selectedWorkflow.shortTitle
                      : builderWorkflowKey === 'prediction'
                        ? `Prediction/${effectivePredictionBackend}`
                        : `Affinity/${effectiveAffinityBackend}`})
                  </span>
                  <button
                    className={`icon-btn ${copiedActionId === 'copy-submit' ? 'is-copied' : ''}`}
                    type="button"
                    aria-label="Copy submit command"
                    disabled={!isSupportedSubmitWorkflow}
                    onClick={() => { void copyText(commandSubmitWithHints, 'Submit command copied.', 'Submit', 'copy-submit'); }}
                  >
                    <Copy size={14} />
                  </button>
                </header>
                <p className="muted small">
                  {!isSupportedSubmitWorkflow
                    ? 'Select a Prediction/Affinity project to generate submit command.'
                    : 'Generated from project workflow and selected backend. task_name/task_summary are omitted unless you fill them.'}
                </p>
                <pre><code>{commandSubmitWithHints}</code></pre>
              </article>

              <article className="api-command-item">
                <header>
                  <span>3. Check Status</span>
                  <button className={`icon-btn ${copiedActionId === 'copy-status' ? 'is-copied' : ''}`} type="button" aria-label="Copy status command" onClick={() => { void copyText(commandStatus, 'Status command copied.', 'Status', 'copy-status'); }}>
                    <Copy size={14} />
                  </button>
                </header>
                <p className="muted small">Uses <code>$TASK_ID</code> captured from submit response.</p>
                <pre><code>{commandStatus}</code></pre>
              </article>

              <article className="api-command-item">
                <header>
                  <span>4. Download Result</span>
                  <button className={`icon-btn ${copiedActionId === 'copy-result' ? 'is-copied' : ''}`} type="button" aria-label="Copy result command" onClick={() => { void copyText(commandResults, 'Result command copied.', 'Result', 'copy-result'); }}>
                    <Copy size={14} />
                  </button>
                </header>
                <p className="muted small">Download result archive to the chosen local path.</p>
                <pre><code>{commandResults}</code></pre>
              </article>

              <article className="api-command-item">
                <header>
                  <span>5. {builderTaskOperation === 'delete' ? 'Delete Task' : 'Cancel Task'}</span>
                  <button className={`icon-btn ${copiedActionId === 'copy-task-action' ? 'is-copied' : ''}`} type="button" aria-label="Copy task action command" onClick={() => { void copyText(commandTaskAction, 'Task action command copied.', builderTaskOperation === 'delete' ? 'Delete Task' : 'Cancel Task', 'copy-task-action'); }}>
                    <Copy size={14} />
                  </button>
                </header>
                <p className="muted small">Use operation mode `{builderTaskOperation}` for task control.</p>
                <pre><code>{commandTaskAction}</code></pre>
              </article>
            </div>

            <section className="api-command-history">
              <div className="api-command-history-head">
                <h3>Recent Command History</h3>
                <button className="btn btn-ghost" type="button" onClick={() => setCommandHistory([])} disabled={commandHistory.length === 0}>
                  Clear
                </button>
              </div>
              {commandHistory.length === 0 ? (
                <div className="muted small">No history yet. Copy any command to add it here.</div>
              ) : (
                <div className="api-history-list">
                  {commandHistory.map((entry) => (
                    <div key={entry.id} className="api-history-item">
                      <div className="api-history-item-main">
                        <strong>{entry.label}</strong>
                        <span className="muted small">
                          {entry.projectName || '-'} · {entry.workflow}/{entry.backend || '-'} · {entry.tokenName || '-'} · {formatIso(entry.createdAt)}
                        </span>
                      </div>
                      <div className="api-history-item-actions">
                        <button className="icon-btn" type="button" aria-label="Use command context" onClick={() => applyCommandHistory(entry)}>
                          <Check size={14} />
                        </button>
                        <button className={`icon-btn ${copiedActionId === `copy-history-${entry.id}` ? 'is-copied' : ''}`} type="button" aria-label="Copy command from history" onClick={() => { void copyText(entry.command, 'History command copied.', undefined, `copy-history-${entry.id}`); }}>
                          <Copy size={14} />
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </section>
          </div>
        </div>
      </section>

      <section className="panel api-usage-panel">
        <div className="api-section-head">
          <h2><BarChart3 size={16} /> Usage</h2>
          <div className="api-usage-controls">
            <label className="api-token-inline" aria-label="Usage token">
              <span className="api-token-inline-label"><KeyRound size={12} /> Token</span>
              <select
                value={selectedTokenId}
                onChange={(e) => setSelectedTokenId(e.target.value)}
                disabled={tokens.length === 0}
              >
                {selectedProjectTokens.length === 0 ? (
                  <option value="">No tokens</option>
                ) : (
                  selectedProjectTokens.map((token) => (
                    <option key={token.id} value={token.id}>
                      {token.name} ({token.token_prefix}...{token.token_last4})
                    </option>
                  ))
                )}
              </select>
            </label>
            <div className="api-builder-meta api-usage-meta">
              <span className="badge">Calls {selectedTokenUsageSummary.total}</span>
              <span className="badge">Success {selectedTokenUsageSummary.successRate.toFixed(1)}%</span>
            </div>
          </div>
        </div>

        {!selectedTokenId ? (
          <div className="api-empty-state">
            <p className="muted">No token selected.</p>
            <button className="btn btn-primary" type="button" onClick={() => openTokenRegistry()}>
              <Plus size={14} /> New Token
            </button>
          </div>
        ) : (
          <>
            <div className="api-usage-bars">
              <div className="api-usage-bars-head">
                <span className="muted small">Daily traffic</span>
                {usageBarsPageCount > 1 && (
                  <div className="api-pager">
                    <button
                      type="button"
                      className="icon-btn"
                      onClick={() => setUsageBarsPage((prev) => Math.max(1, prev - 1))}
                      disabled={usageBarsPage <= 1}
                      title="Previous daily page"
                      aria-label="Previous daily page"
                    >
                      <ChevronLeft size={14} />
                    </button>
                    <span className="muted small">{usageBarsPage} / {usageBarsPageCount}</span>
                    <button
                      type="button"
                      className="icon-btn"
                      onClick={() => setUsageBarsPage((prev) => Math.min(usageBarsPageCount, prev + 1))}
                      disabled={usageBarsPage >= usageBarsPageCount}
                      title="Next daily page"
                      aria-label="Next daily page"
                    >
                      <ChevronRight size={14} />
                    </button>
                  </div>
                )}
              </div>
              {tokenUsageDaily.length === 0 ? (
                <div className="muted">No usage data.</div>
              ) : (
                pagedDailyUsage.map((item) => {
                  const width = Math.max(4, (item.total_count / maxDailyCount) * 100);
                  return (
                    <div className="api-usage-bar-row" key={`${item.token_id}-${item.usage_day}`}>
                      <span className="api-usage-day">{item.usage_day}</span>
                      <div className="api-usage-bar-track">
                        <span className="api-usage-bar-fill" style={{ width: `${width}%` }} />
                      </div>
                      <span className="api-usage-count">{item.total_count}</span>
                    </div>
                  );
                })
              )}
            </div>

            <div className="table-wrap api-usage-table-wrap">
              <table className="table api-usage-table">
                <thead>
                  <tr>
                    <th>Time</th>
                    <th>Action</th>
                    <th>Status</th>
                    <th>Path</th>
                  </tr>
                </thead>
                <tbody>
                  {tokenUsage.map((item) => (
                    <tr key={item.id}>
                      <td>{formatIso(item.created_at)}</td>
                      <td>{item.action || `${item.method} ${item.path}`}</td>
                      <td>{item.succeeded ? 'OK' : `Error (${item.status_code})`}</td>
                      <td><code>{item.path}</code></td>
                    </tr>
                  ))}
                  {tokenUsageTotal === 0 && (
                    <tr>
                      <td colSpan={4} className="muted">No events.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>

            {eventPageCount > 1 && (
              <div className="api-pager">
                <button
                  type="button"
                  className="icon-btn"
                  onClick={() => setEventPage((prev) => Math.max(1, prev - 1))}
                  disabled={eventPage <= 1}
                  title="Previous page"
                  aria-label="Previous page"
                >
                  <ChevronLeft size={14} />
                </button>
                <span className="muted small">{eventPage} / {eventPageCount}</span>
                <button
                  type="button"
                  className="icon-btn"
                  onClick={() => setEventPage((prev) => Math.min(eventPageCount, prev + 1))}
                  disabled={eventPage >= eventPageCount}
                  title="Next page"
                  aria-label="Next page"
                >
                  <ChevronRight size={14} />
                </button>
              </div>
            )}
          </>
        )}
      </section>

      {!isProjectScoped && (
      <section className="panel api-docs-panel">
        <div className="api-section-head">
          <h2><Info size={16} /> API Docs</h2>
        </div>
        <ol className="api-doc-steps">
          <li>
            <strong>Create project in VBio web</strong>
            <span className="muted small">API does not create project. Pick existing project in the builder.</span>
          </li>
          <li>
            <strong>Create token and bind project</strong>
            <span className="muted small">Token registry controls submit/cancel/delete permissions per project.</span>
          </li>
          <li>
            <strong>Use generated submit command</strong>
            <span className="muted small">Workflow is fixed by project type; select backend where applicable before copying.</span>
          </li>
          <li>
            <strong>YAML format (prediction)</strong>
            <span className="muted small">Use `version + sequences`; add `constraints/properties/templates` only when needed.</span>
          </li>
          <li>
            <strong>Track and download</strong>
            <span className="muted small">Use status and result commands with the same `project_id` and token.</span>
          </li>
          <li>
            <strong>Cancel or delete safely</strong>
            <span className="muted small">`operation_mode=cancel|delete`, permission checked by gateway.</span>
          </li>
          <li>
            <strong>Reuse from history</strong>
            <span className="muted small">Recent copied commands are saved below builder for one-click reuse.</span>
          </li>
        </ol>
      </section>
      )}

      {yamlBuilderOpen && (
        <div className="modal-mask" onClick={() => setYamlBuilderOpen(false)}>
          <div className="modal modal-wide api-yaml-modal" onClick={(e) => e.stopPropagation()}>
            <div className="api-token-modal-head">
              <h2><Info size={17} /> YAML Builder</h2>
              <button
                className="icon-btn"
                type="button"
                aria-label="Close yaml builder"
                onClick={() => setYamlBuilderOpen(false)}
              >
                <X size={16} />
              </button>
            </div>

            <div
              ref={yamlBuilderGridRef}
              className={`api-yaml-modal-body api-yaml-modal-body-resizable ${isYamlBuilderResizing ? 'is-resizing' : ''}`}
              style={yamlBuilderGridStyle}
            >
              <section className="api-yaml-modal-editor">
                <div className="api-builder-meta">
                  <span className="badge">Protein {yamlComponentStats.protein}</span>
                  <span className="badge">DNA {yamlComponentStats.dna}</span>
                  <span className="badge">RNA {yamlComponentStats.rna}</span>
                  <span className="badge">Ligand {yamlComponentStats.ligand}</span>
                  <span className="badge">Constraints {builderYamlConstraints.length}</span>
                  <span className="badge">Affinity {builderYamlProperties.affinity ? 'on' : 'off'}</span>
                </div>
                <div className="component-sidebar-list api-yaml-components api-yaml-components-flat">
                  {builderYamlComponents.map((component, index) => (
                    <article
                      key={component.id}
                      className={`api-yaml-component-card ${index % 2 === 0 ? 'api-yaml-component-card-odd' : 'api-yaml-component-card-even'}`}
                    >
                      <header>
                        <button
                          className="btn btn-ghost api-yaml-collapse-btn"
                          type="button"
                          onClick={() => toggleYamlBuilderComponentCollapsed(component.id)}
                          aria-label="Toggle component details"
                        >
                          <ChevronRight size={13} className={builderYamlCollapsed[component.id] ? '' : 'api-icon-rotated'} />
                          <strong>Component {index + 1}</strong>
                          <span className="muted small">({componentTypeLabel(component.type)}, x{component.numCopies})</span>
                        </button>
                        <button className="icon-btn danger" type="button" aria-label="Remove component" onClick={() => removeYamlBuilderComponent(component.id)}>
                          <Trash2 size={14} />
                        </button>
                      </header>

                      {!builderYamlCollapsed[component.id] && (
                        <>
                          <div className="api-yaml-component-grid">
                            <label className="field">
                              <span>Type</span>
                              <select
                                value={component.type}
                                onChange={(e) => {
                                  const nextType = e.target.value === 'dna' || e.target.value === 'rna' || e.target.value === 'ligand' ? e.target.value : 'protein';
                                  updateYamlBuilderComponent(component.id, (current) => {
                                    const next: InputComponent = { ...current, type: nextType };
                                    if (nextType === 'ligand') {
                                      next.inputMethod = current.inputMethod === 'ccd' ? 'ccd' : current.inputMethod === 'jsme' ? 'jsme' : 'smiles';
                                      delete next.useMsa;
                                      delete next.cyclic;
                                    } else {
                                      next.useMsa = current.useMsa !== false;
                                      next.cyclic = Boolean(current.cyclic);
                                      delete next.inputMethod;
                                    }
                                    return next;
                                  });
                                  if (nextType !== 'protein') {
                                    setBuilderYamlTemplates((prev) => {
                                      const next = { ...prev };
                                      delete next[component.id];
                                      return next;
                                    });
                                  }
                                }}
                              >
                                <option value="protein">protein</option>
                                <option value="dna">dna</option>
                                <option value="rna">rna</option>
                                <option value="ligand">ligand</option>
                              </select>
                            </label>

                            <label className="field">
                              <span>Copies</span>
                              <input
                                type="number"
                                min={1}
                                value={component.numCopies}
                                onChange={(e) => {
                                  const copies = Math.max(1, Math.floor(Number(e.target.value) || 1));
                                  updateYamlBuilderComponent(component.id, (current) => ({ ...current, numCopies: copies }));
                                }}
                              />
                            </label>
                          </div>

                          {component.type === 'ligand' && (
                            <>
                              <label className="field">
                                <span>Ligand input</span>
                                <select
                                  value={component.inputMethod === 'ccd' ? 'ccd' : component.inputMethod === 'jsme' ? 'jsme' : 'smiles'}
                                  onChange={(e) =>
                                    updateYamlBuilderComponent(component.id, (current) => ({
                                      ...current,
                                      inputMethod: e.target.value === 'ccd' ? 'ccd' : e.target.value === 'jsme' ? 'jsme' : 'smiles'
                                    }))
                                  }
                                >
                                  <option value="smiles">smiles</option>
                                  <option value="jsme">jsme</option>
                                  <option value="ccd">ccd</option>
                                </select>
                              </label>
                              <label className="field">
                                <span>{component.inputMethod === 'ccd' ? 'CCD Code' : 'SMILES'}</span>
                                <input
                                  value={component.sequence}
                                  onChange={(e) => updateYamlBuilderComponent(component.id, (current) => ({ ...current, sequence: e.target.value }))}
                                  placeholder={component.inputMethod === 'ccd' ? 'Example: ATP' : 'Example: CC(=O)NC1=CC=C(C=C1)O'}
                                />
                              </label>
                              {component.inputMethod === 'jsme' && (
                                <div className="field">
                                  <span>JSME Molecule Editor</span>
                                  <div className="jsme-editor-container component-jsme-shell api-yaml-jsme-shell">
                                    <JSMEEditor
                                      smiles={component.sequence}
                                      height={320}
                                      onSmilesChange={(value) =>
                                        updateYamlBuilderComponent(component.id, (current) => ({ ...current, sequence: value }))
                                      }
                                    />
                                  </div>
                                </div>
                              )}
                            </>
                          )}

                          {component.type !== 'ligand' && (
                            <label className="field">
                              <span>Sequence</span>
                              <textarea
                                rows={3}
                                value={component.sequence}
                                onChange={(e) => updateYamlBuilderComponent(component.id, (current) => ({ ...current, sequence: e.target.value }))}
                                placeholder="Component sequence"
                              />
                            </label>
                          )}

                          {component.type === 'protein' && (
                            <>
                              <div className="api-yaml-component-flags">
                                <label className="checkbox-inline">
                                  <input
                                    type="checkbox"
                                    checked={component.useMsa !== false}
                                    onChange={(e) => updateYamlBuilderComponent(component.id, (current) => ({ ...current, useMsa: e.target.checked }))}
                                  />
                                  <span>MSA</span>
                                </label>
                                <label className="checkbox-inline">
                                  <input
                                    type="checkbox"
                                    checked={Boolean(component.cyclic)}
                                    onChange={(e) => updateYamlBuilderComponent(component.id, (current) => ({ ...current, cyclic: e.target.checked }))}
                                  />
                                  <span>Cyclic</span>
                                </label>
                              </div>

                              <div className="api-yaml-builder">
                                <label className="field">
                                  <span>Template absolute path (optional)</span>
                                  <input
                                    value={builderYamlTemplates[component.id]?.path || ''}
                                    onChange={(e) => updateYamlBuilderTemplate(component.id, (current) => ({ ...current, path: e.target.value }))}
                                    placeholder="/abs/path/template.cif"
                                  />
                                </label>
                                <div className="api-yaml-builder-grid api-yaml-template-grid">
                                  <label className="field">
                                    <span>Template format</span>
                                    <select
                                      value={builderYamlTemplates[component.id]?.format || 'auto'}
                                      onChange={(e) =>
                                        updateYamlBuilderTemplate(component.id, (current) => ({
                                          ...current,
                                          format: e.target.value === 'pdb' ? 'pdb' : e.target.value === 'cif' ? 'cif' : 'auto'
                                        }))
                                      }
                                    >
                                      <option value="auto">auto</option>
                                      <option value="pdb">pdb</option>
                                      <option value="cif">cif</option>
                                    </select>
                                  </label>
                                  <label className="field">
                                    <span>Template chain</span>
                                    <input
                                      value={builderYamlTemplates[component.id]?.templateChain || ''}
                                      onChange={(e) => updateYamlBuilderTemplate(component.id, (current) => ({ ...current, templateChain: e.target.value }))}
                                      placeholder="A"
                                    />
                                  </label>
                                  <label className="field">
                                    <span>Target chains</span>
                                    <input
                                      value={builderYamlTemplates[component.id]?.targetChains || ''}
                                      onChange={(e) => updateYamlBuilderTemplate(component.id, (current) => ({ ...current, targetChains: e.target.value }))}
                                      placeholder="A,B"
                                    />
                                  </label>
                                </div>
                              </div>
                            </>
                          )}
                        </>
                      )}
                    </article>
                  ))}
                </div>
                <div className="api-yaml-component-toolbar api-yaml-component-toolbar-bottom">
                  <span className="muted small">Add component below</span>
                  <div className="api-yaml-component-toolbar-actions">
                    <button type="button" className="btn btn-secondary btn-compact" onClick={() => addYamlBuilderComponent('protein')}>
                      <Plus size={13} /> Protein
                    </button>
                    <button type="button" className="btn btn-secondary btn-compact" onClick={() => addYamlBuilderComponent('ligand')}>
                      <Plus size={13} /> Ligand
                    </button>
                    <button type="button" className="btn btn-secondary btn-compact" onClick={() => addYamlBuilderComponent('dna')}>
                      <Plus size={13} /> DNA
                    </button>
                    <button type="button" className="btn btn-secondary btn-compact" onClick={() => addYamlBuilderComponent('rna')}>
                      <Plus size={13} /> RNA
                    </button>
                  </div>
                </div>
                <section className="api-yaml-constraints">
                  <button
                    className="btn btn-ghost api-yaml-collapse-btn api-yaml-constraints-toggle"
                    type="button"
                    onClick={() => setBuilderYamlConstraintsOpen((prev) => !prev)}
                    aria-expanded={builderYamlConstraintsOpen}
                    aria-label="Toggle constraints and properties editor"
                  >
                    {builderYamlConstraintsOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                    <strong>Constraints &amp; Properties</strong>
                    <span className="muted small">
                      {builderYamlConstraints.length} constraint{builderYamlConstraints.length === 1 ? '' : 's'}
                    </span>
                  </button>
                  {builderYamlConstraintsOpen && (
                    <div className="api-yaml-constraints-body">
                      <ConstraintEditor
                        components={normalizedYamlBuilderComponents}
                        constraints={builderYamlConstraints}
                        properties={builderYamlProperties}
                        onConstraintsChange={setBuilderYamlConstraints}
                        onPropertiesChange={setBuilderYamlProperties}
                        showAffinitySection
                      />
                    </div>
                  )}
                </section>
              </section>

              <div
                className={`panel-resizer ${isYamlBuilderResizing ? 'dragging' : ''}`}
                role="separator"
                aria-orientation="vertical"
                aria-label="Resize YAML Builder panels"
                tabIndex={0}
                onPointerDown={handleYamlBuilderResizerPointerDown}
                onKeyDown={handleYamlBuilderResizerKeyDown}
              />

              <section className="api-yaml-modal-preview">
                <div className="api-command-item">
                  <header>
                    <span>Generated YAML</span>
                    <div className="api-yaml-preview-actions">
                      <button className="icon-btn" type="button" aria-label="Download generated YAML" onClick={downloadGeneratedYaml}>
                        <Download size={14} />
                      </button>
                      <button className={`icon-btn ${copiedActionId === 'copy-yaml-modal' ? 'is-copied' : ''}`} type="button" aria-label="Copy generated YAML" onClick={() => { void copyText(yamlBuilderText, 'Generated YAML copied.', 'YAML Builder', 'copy-yaml-modal'); }}>
                        <Copy size={14} />
                      </button>
                    </div>
                  </header>
                  <pre><code>{yamlBuilderText}</code></pre>
                </div>
              </section>
            </div>
          </div>
        </div>
      )}

      {projectTokenPanelProjectId && (
        <div className="modal-mask" onClick={() => setProjectTokenPanelProjectId(null)}>
          <div className="modal api-project-token-modal" onClick={(e) => e.stopPropagation()}>
            <div className="api-token-modal-head">
              <h2><KeyRound size={17} /> {projectTokenPanelProject?.name || 'Project'} Tokens</h2>
              <button
                className="icon-btn"
                type="button"
                aria-label="Close project token panel"
                onClick={() => setProjectTokenPanelProjectId(null)}
              >
                <X size={16} />
              </button>
            </div>
            <div className="api-project-token-modal-body">
              {projectTokenPanelTokens.length === 0 ? (
                <div className="api-project-token-modal-empty muted">
                  <ShieldOff size={16} />
                  <span>No tokens in this project yet.</span>
                </div>
              ) : (
                <div className="api-project-token-modal-list">
                  {projectTokenPanelTokens.map((token) => (
                    <article key={token.id} className="api-project-token-modal-item">
                      <div className="api-project-token-modal-main">
                        <strong>{token.name}</strong>
                        <code>{token.token_prefix}...{token.token_last4}</code>
                      </div>
                      <div className="api-project-token-modal-meta">
                        <span className={`badge ${token.is_active ? '' : 'badge-muted'}`}>
                          {token.is_active ? 'active' : 'revoked'}
                        </span>
                        <button
                          type="button"
                          className="btn btn-ghost btn-compact"
                          onClick={() => {
                            setSelectedProjectId(String(token.project_id || ''));
                            setSelectedTokenId(token.id);
                            setProjectTokenPanelProjectId(null);
                          }}
                        >
                          Use
                        </button>
                        {token.is_active && (
                          <button
                            type="button"
                            className="icon-btn"
                            title="Revoke token"
                            aria-label="Revoke token"
                            disabled={tokenRevokingId === token.id}
                            onClick={() => { void revokeToken(token.id); }}
                          >
                            <ShieldOff size={13} />
                          </button>
                        )}
                        <button
                          type="button"
                          className="icon-btn danger"
                          title="Delete token"
                          aria-label="Delete token"
                          disabled={tokenDeletingId === token.id}
                          onClick={() => { void removeToken(token.id); }}
                        >
                          <Trash2 size={13} />
                        </button>
                      </div>
                    </article>
                  ))}
                </div>
              )}
              <div className="api-project-token-modal-actions">
                <button
                  type="button"
                  className="btn btn-secondary"
                  onClick={() => {
                    if (!projectTokenPanelProjectId) return;
                    setProjectTokenPanelProjectId(null);
                    openTokenRegistryForProject(projectTokenPanelProjectId);
                  }}
                >
                  <KeyRound size={13} />
                  Open Token Registry
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {registryOpen && (
        <div className="modal-mask" onClick={closeTokenRegistry}>
          <div className="modal modal-wide api-token-modal" onClick={(e) => e.stopPropagation()}>
            <div className="api-token-modal-head">
              <h2><ShieldCheck size={17} /> Token Registry{registryScopeProject ? ` · ${registryScopeProject.name}` : ''}</h2>
              <button
                className="icon-btn"
                type="button"
                aria-label="Close token registry"
                onClick={closeTokenRegistry}
              >
                <X size={16} />
              </button>
            </div>

            <div className="api-token-modal-body">
              <section className="api-token-modal-create">
                <form className="api-token-create" onSubmit={createApiToken}>
                  <label className="field api-token-name-field">
                    <span>Name</span>
                    <input value={newTokenName} onChange={(e) => setNewTokenName(e.target.value)} placeholder="token-xxxxxxxx" required />
                  </label>

                  {!registryScopeProject && (
                    <label className="field api-token-project-field">
                      <span>Project</span>
                      <select
                        value={selectedProjectId}
                        onChange={(e) => setSelectedProjectId(e.target.value)}
                        disabled={projectLoading || projects.length === 0}
                      >
                        {projects.length === 0 ? (
                          <option value="">No project</option>
                        ) : (
                          projects.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)
                        )}
                      </select>
                    </label>
                  )}

                  <label className="field api-token-expiry-field">
                    <span>Expire (d)</span>
                    <input
                      type="number"
                      min={1}
                      value={newTokenExpiresDays}
                      onChange={(e) => setNewTokenExpiresDays(e.target.value)}
                      placeholder="Never"
                    />
                  </label>

                  <div className="field api-token-source-wrap api-token-permissions-field">
                    <span>Permissions</span>
                    <div className="api-permission-grid">
                      <button
                        type="button"
                        className={`api-permission-chip ${allowSubmit ? 'active' : ''}`}
                        onClick={() => setAllowSubmit((prev) => !prev)}
                        aria-pressed={allowSubmit}
                      >
                        Submit
                      </button>
                      <button
                        type="button"
                        className={`api-permission-chip ${allowDelete ? 'active' : ''}`}
                        onClick={() => setAllowDelete((prev) => !prev)}
                        aria-pressed={allowDelete}
                      >
                        Delete
                      </button>
                      <button
                        type="button"
                        className={`api-permission-chip ${allowCancel ? 'active' : ''}`}
                        onClick={() => setAllowCancel((prev) => !prev)}
                        aria-pressed={allowCancel}
                      >
                        Cancel
                      </button>
                    </div>
                  </div>

                  <div className="row end api-token-create-action">
                    <button className="btn btn-primary" type="submit" disabled={tokenCreating || !selectedProjectId}>
                      <Plus size={14} /> {tokenCreating ? 'Creating...' : 'Create Token'}
                    </button>
                  </div>
                </form>

                {newTokenPlainText && (
                  <div className="token-plain-block">
                    <label className="field">
                      <span>New Token</span>
                      <textarea rows={2} readOnly value={newTokenPlainText} />
                    </label>
                    <div className="row">
                      <button
                        className={`btn btn-secondary ${copiedActionId === 'copy-new-token' ? 'is-copied' : ''}`}
                        type="button"
                        onClick={() => { void copyText(newTokenPlainText, 'Token copied.', undefined, 'copy-new-token'); }}
                      >
                        <Copy size={14} /> Copy
                      </button>
                      <button className="btn btn-ghost" type="button" onClick={() => setNewTokenPlainText('')}>
                        Hide
                      </button>
                    </div>
                  </div>
                )}
              </section>

              <section className="api-token-modal-list">
                <div className="api-token-list-toolbar">
                  {registryScopeProject && (
                    <div className="api-token-scope-indicator">
                      <span className="badge">Project scope</span>
                      <strong>{registryScopeProject.name}</strong>
                    </div>
                  )}
                  <label className="field api-token-search-field">
                    <span><Search size={12} /> Find</span>
                    <input
                      value={tokenQuery}
                      onChange={(e) => setTokenQuery(e.target.value)}
                      placeholder="name / prefix"
                    />
                  </label>
                </div>

                <div className="table-wrap api-token-table-wrap api-token-table-scroll">
                  <table className="table api-token-table">
                    <thead>
                      <tr>
                        <th>Name</th>
                        {showRegistryProjectColumn && <th>Project</th>}
                        <th>Permissions</th>
                        <th>Status</th>
                        <th>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {tokenLoading ? (
                        <tr>
                          <td colSpan={showRegistryProjectColumn ? 5 : 4} className="muted">Loading...</td>
                        </tr>
                      ) : pagedTokens.length === 0 ? (
                        <tr>
                          <td colSpan={showRegistryProjectColumn ? 5 : 4} className="muted">No tokens.</td>
                        </tr>
                      ) : (
                        pagedTokens.map((token) => {
                          const projectName = projects.find((item) => item.id === token.project_id)?.name || '-';
                          return (
                            <tr key={token.id} className={selectedTokenId === token.id ? 'row-selected' : ''}>
                              <td>{token.name}<br /><code>{token.token_prefix}...{token.token_last4}</code></td>
                              {showRegistryProjectColumn && <td>{projectName}</td>}
                              <td>
                                <div className="api-token-perm-badges">
                                  <span className={`api-token-perm-badge ${token.allow_submit ? 'on' : 'off'}`}>S</span>
                                  <span className={`api-token-perm-badge ${token.allow_delete ? 'on' : 'off'}`}>D</span>
                                  <span className={`api-token-perm-badge ${token.allow_cancel ? 'on' : 'off'}`}>C</span>
                                </div>
                              </td>
                              <td>
                                <span className={`api-token-status-chip ${token.is_active ? 'active' : 'revoked'}`}>
                                  {token.is_active ? 'Active' : 'Revoked'}
                                </span>
                              </td>
                              <td>
                                <div className="api-token-actions">
                                  <button
                                    className="icon-btn"
                                    type="button"
                                    title="Select"
                                    aria-label="Select token"
                                    onClick={() => setSelectedTokenId(token.id)}
                                  >
                                    <Check size={14} />
                                  </button>
                                  <button
                                    className="icon-btn"
                                    type="button"
                                    title="Revoke"
                                    aria-label="Revoke token"
                                    disabled={!token.is_active || tokenRevokingId === token.id}
                                    onClick={() => {
                                      void revokeToken(token.id);
                                    }}
                                  >
                                    <ShieldOff size={14} />
                                  </button>
                                  <button
                                    className="icon-btn danger"
                                    type="button"
                                    title="Delete"
                                    aria-label="Delete token"
                                    disabled={tokenDeletingId === token.id}
                                    onClick={() => {
                                      void removeToken(token.id);
                                    }}
                                  >
                                    <Trash2 size={14} />
                                  </button>
                                </div>
                              </td>
                            </tr>
                          );
                        })
                      )}
                    </tbody>
                  </table>
                </div>

                <div className="api-pager">
                  <button
                    type="button"
                    className="icon-btn"
                    onClick={() => setTokenPage((prev) => Math.max(1, prev - 1))}
                    disabled={tokenPage <= 1}
                    title="Previous page"
                    aria-label="Previous page"
                  >
                    <ChevronLeft size={14} />
                  </button>
                  <span className="muted small">{tokenPage} / {tokenPageCount}</span>
                  <button
                    type="button"
                    className="icon-btn"
                    onClick={() => setTokenPage((prev) => Math.min(tokenPageCount, prev + 1))}
                    disabled={tokenPage >= tokenPageCount}
                    title="Next page"
                    aria-label="Next page"
                  >
                    <ChevronRight size={14} />
                  </button>
                </div>
              </section>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
