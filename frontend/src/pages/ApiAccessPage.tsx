import {

  createApiToken as createApiTokenServer,
  deleteApiToken as deleteApiTokenServer,
  listApiTokens as listApiTokensServer,
  toApiToken,
  updateApiToken as updateApiTokenServer
} from '../api/authServerApi';
import {
  FormEvent,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import {
  BarChart3,
  ChevronLeft,
  Download,
  Info,
  KeyRound,
  ShieldCheck
} from 'lucide-react';
import { InfoTip } from '../components/common/InfoTip';
import { useModalDialog } from '../components/ui/useModalDialog';
import { useLocation, useNavigate, useParams } from 'react-router-dom';
import type {
  AffinityScoringMode,
  ApiToken,
  ApiTokenUsage,
  ApiTokenUsageDaily,
  InputComponent,
  PredictionConstraint,
  PredictionProperties,
  Project,
  ProteinModification,
  ProteinModificationInputMethod,
} from '../types/models';
import { useAuth } from '../hooks/useAuth';
import {
  getProjectTaskById,
  listApiTokenUsagePage,
  listApiTokenUsageDailyByTokenIds,
  listApiTokenUsageDaily,
  listProjects,
} from '../api/supabaseLite';
import { ENV } from '../utils/env';
import { componentTypeLabel, normalizeInputComponents } from '../utils/projectInputs';
import { buildVirtualScreeningYaml, VIRTUAL_SCREENING_EXAMPLE } from '../utils/virtualScreening';
import { getWorkflowDefinition } from '../utils/workflows';
import { buildPredictionYamlFromComponents, collectCustomCcdMoleculesFromComponents } from '../utils/yaml';
import { assignChainIdsForComponents } from '../utils/chainAssignments';
import { loadRDKitModule } from '../utils/rdkit';
import { rdkitMolHasAminoAcidBackbone, looksLikeAminoAcidBackboneSmiles } from '../utils/inputValidation';
import { ApiYamlBuilderModal } from './ApiYamlBuilderModal';
import { ApiProjectTokenModal } from './ApiProjectTokenModal';
import { ApiTokenRegistryModal } from './ApiTokenRegistryModal';
import { ApiDocsPanel } from './ApiDocsPanel';
import { ApiCommandRightColumn } from './ApiCommandRightColumn';
import { LeadOptOptions } from './ApiLeadOptOptions';
import { AffinityConfigBlock } from './ApiAffinityConfigBlock';
import { UsagePanel } from './ApiUsagePanel';
import { ProjectStatsPanel } from './ApiProjectStatsPanel';
import { useCommandClipboard } from './useCommandClipboard';
import { Field } from '../components/common/Field';
import {
  ApiBuilderGridStyle,
  BUILDER_CUSTOM_RESIDUE_SCAFFOLD,
  BuilderWorkflowKey,
  CommandHistoryEntry,
  DAILY_USAGE_PAGE_SIZE,
  EMPTY_PREDICTION_PROPERTIES,
  EVENT_PAGE_SIZE,
  LEAD_OPT_API_ACCESS_ENABLED,
  PROJECT_STATS_PAGE_SIZE,
  PredictionBackend,
  AffinityBackend,
  ProjectStatsRow,
  ProjectStatsSort,
  ProjectStatsWorkflowFilter,
  TOKEN_PAGE_SIZE,
  UsageSummary,
  UsageWindow,
  YamlProteinTemplateConfig,
  buildBuilderCustomCcd,
  builderPositionForTerminal,
  builderResidueAt,
  builderTerminalForPosition,
  computeUsageSummaryFromDaily,
  createBuilderModification,
  createYamlBuilderComponent,
  escapeForDoubleQuotedShell,
  extractFileNameFromPath,
  inferTemplateFormat,
  isAffinityUploadComponent,
  isSamePredictionProperties,
  normalizeAffinityBackend,
  normalizeAffinityBuilderMode,
  normalizeBaseUrl,
  normalizeBuilderCcd,
  normalizeChainId,
  normalizePredictionBackend,
  normalizePredictionChainValue,
  normalizeProjectStatsSort,
  normalizeProjectStatsWorkflowFilter,
  normalizeUsageWindow,
  readCommandHistoryFromStorage,
  shortUuidLike,
  usageSince
} from './apiAccessHelpers';

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
  const [error, setError] = useState<string | null>(null);
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
  const [builderVirtualScreeningProtein, setBuilderVirtualScreeningProtein] = useState('');
  const [builderVirtualScreeningInput, setBuilderVirtualScreeningInput] = useState(VIRTUAL_SCREENING_EXAMPLE);
  const [builderVsLibraryPath, setBuilderVsLibraryPath] = useState('');
  const [builderYamlComponents, setBuilderYamlComponents] = useState<InputComponent[]>([
    createYamlBuilderComponent('protein'),
    createYamlBuilderComponent('ligand')
  ]);
  const [builderYamlTemplates, setBuilderYamlTemplates] = useState<Record<string, YamlProteinTemplateConfig>>({});
  const [builderYamlCollapsed, setBuilderYamlCollapsed] = useState<Record<string, boolean>>({});
  const [builderYamlConstraintsOpen, setBuilderYamlConstraintsOpen] = useState(false);
  const [builderYamlConstraints, setBuilderYamlConstraints] = useState<PredictionConstraint[]>([]);
  const [builderYamlProperties, setBuilderYamlProperties] = useState<PredictionProperties>({ ...EMPTY_PREDICTION_PROPERTIES });
  const [builderCustomResidueValidity, setBuilderCustomResidueValidity] = useState<Record<string, boolean>>({});
  const [builderTargetPath, setBuilderTargetPath] = useState('./protein.pdb');
  const [builderLigandPath, setBuilderLigandPath] = useState('./ligand.sdf');
  const [builderResultPath, setBuilderResultPath] = useState('./result.zip');
  const [builderPredictionBackend, setBuilderPredictionBackend] = useState<PredictionBackend>('boltz');
  const [builderPredictionLowVram, setBuilderPredictionLowVram] = useState(false);
  const [builderAffinityMode, setBuilderAffinityMode] = useState<AffinityScoringMode>('dock');
  const [builderAffinitySeed, setBuilderAffinitySeed] = useState<number | null>(null);
  const [builderAffinityConfidenceOnly, setBuilderAffinityConfidenceOnly] = useState(true);
  const [builderAffinityTargetChain, setBuilderAffinityTargetChain] = useState('A');
  const [builderAffinityLigandChain, setBuilderAffinityLigandChain] = useState('L');
  const [builderAffinityLigandSmiles, setBuilderAffinityLigandSmiles] = useState('');
  const [builderDockCenterX, setBuilderDockCenterX] = useState('');
  const [builderDockCenterY, setBuilderDockCenterY] = useState('');
  const [builderDockCenterZ, setBuilderDockCenterZ] = useState('');
  const [builderDockSizeX, setBuilderDockSizeX] = useState('');
  const [builderDockSizeY, setBuilderDockSizeY] = useState('');
  const [builderDockSizeZ, setBuilderDockSizeZ] = useState('');
  const [builderPocketMethod, setBuilderPocketMethod] = useState<'center' | 'ligand' | 'residues'>('center');
  const [builderPocketLigandPath, setBuilderPocketLigandPath] = useState('./reference_ligand.sdf');
  const [builderPocketResidues, setBuilderPocketResidues] = useState('');
  const [builderLeadOptTargetConfigPath, setBuilderLeadOptTargetConfigPath] = useState('./target.yaml');
  const [builderLeadOptInputCompound, setBuilderLeadOptInputCompound] = useState('');
  const [builderLeadOptTargetChain, setBuilderLeadOptTargetChain] = useState('A');
  const [builderLeadOptLigandChain, setBuilderLeadOptLigandChain] = useState('L');
  const [builderLeadOptObjectiveProfile, setBuilderLeadOptObjectiveProfile] = useState('balanced');
  const [builderLeadOptEnableAffinity, setBuilderLeadOptEnableAffinity] = useState(false);
  const [builderTaskOperation, setBuilderTaskOperation] = useState<'cancel' | 'delete'>('cancel');
  const [builderAffinityBackend, setBuilderAffinityBackend] = useState<AffinityBackend>('boltz');
  const [builderLeftWidth, setBuilderLeftWidth] = useState(38);
  const [isBuilderResizing, setIsBuilderResizing] = useState(false);
  const [yamlBuilderLeftWidth, setYamlBuilderLeftWidth] = useState(68);
  const [isYamlBuilderResizing, setIsYamlBuilderResizing] = useState(false);
  const [projectTokenPanelProjectId, setProjectTokenPanelProjectId] = useState<string | null>(null);
  const commandPanelRef = useRef<HTMLElement | null>(null);
  const builderGridRef = useRef<HTMLDivElement | null>(null);
  const builderResizeRef = useRef<{ startX: number; startWidthPercent: number } | null>(null);
  const yamlBuilderGridRef = useRef<HTMLDivElement | null>(null);
  const yamlBuilderResizeRef = useRef<{ startX: number; startWidthPercent: number } | null>(null);
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
          listApiTokensServer(),
          listProjects({ userId: session.userId })
        ]);
        if (cancelled) return;

        setTokens(tokenRows.map(toApiToken));
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
      // Server-side mint (F2): the plaintext is generated on the server, returned ONCE for
      // this display, and only the sha256 hash is stored — the browser never writes
      // api_tokens nor sees other users' rows.
      const minted = await createApiTokenServer({
        name: label,
        project_id: selectedProjectId,
        allow_submit: allowSubmit,
        allow_delete: allowDelete,
        allow_cancel: allowCancel
      });
      const plain = minted.token_plain;
      // The list rows never carry the plaintext (hash-only storage); attaching it to the
      // in-memory row keeps it visible in the Builder's "Token Plaintext" field for this
      // session — a reload clears it back to the shown-once model.
      const saved: ApiToken = { ...toApiToken(minted.token), token_plain: plain };

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
      setSuccess('API token created. Copy it now; it will not be shown again.');
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to create API token.';
      setError(message);
    } finally {
      setTokenCreating(false);
    }
  };

  const revokeToken = async (tokenId: string) => {
    setTokenRevokingId(tokenId);
    setError(null);
    setSuccess(null);
    try {
      const updated = toApiToken(await updateApiTokenServer(tokenId, { revoked_at: new Date().toISOString() }));
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
      await deleteApiTokenServer(tokenId);
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
  const isPredictionWorkflow = selectedWorkflow.key === 'prediction' || selectedWorkflow.key === 'peptide_design';
  const isVirtualScreeningWorkflow = selectedWorkflow.key === 'virtual_screening';
  const isLeadOptimizationWorkflow = selectedWorkflow.key === 'lead_optimization';
  const isSupportedSubmitWorkflow = isPredictionWorkflow || isVirtualScreeningWorkflow || isAffinityWorkflow;
  const selectedPredictionBackend = normalizePredictionBackend(builderPredictionBackend);
  const effectivePredictionBackend: PredictionBackend = isVirtualScreeningWorkflow
    ? 'nesso'
    : selectedPredictionBackend === 'nesso'
      ? 'boltz'
      : selectedPredictionBackend;
  const isNessoPredictionBackend = isVirtualScreeningWorkflow;
  const effectiveAffinityBackend: AffinityBackend = normalizeAffinityBackend(builderAffinityBackend);
  const builderWorkflowKey: BuilderWorkflowKey = isAffinityWorkflow
    ? 'affinity'
    : isVirtualScreeningWorkflow
      ? 'virtual_screening'
      : 'prediction';
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
      const workflowKey: ProjectStatsWorkflowFilter = workflow.key === 'affinity'
        ? 'affinity'
        : workflow.key === 'virtual_screening'
          ? 'virtual_screening'
          : 'prediction';
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
    // A task-scoped builder (task_row_id in the URL) is prefilled from the task snapshot;
    // the reset must not wipe that prefill when the async workflow flag flips after it lands.
    if (isProjectScoped && scopedTaskRowId) return;
    // Reset on PROJECT IDENTITY only: depending on selectedProject?.use_msa made this effect
    // re-fire when the projects list resolved AFTER a task prefill, wiping the prefill back
    // to defaults (mode/seed/chains/SMILES).
    setBuilderAffinityMode('dock');
    setBuilderAffinitySeed(null);
    setBuilderAffinityConfidenceOnly(true);
    setBuilderAffinityTargetChain('A');
    setBuilderAffinityLigandChain('L');
    setBuilderAffinityLigandSmiles('');
    setBuilderDockCenterX(''); setBuilderDockCenterY(''); setBuilderDockCenterZ('');
    setBuilderDockSizeX(''); setBuilderDockSizeY(''); setBuilderDockSizeZ('');
    setBuilderPocketMethod('center');
    setBuilderPocketLigandPath('./reference_ligand.sdf');
    setBuilderPocketResidues('');
  }, [isAffinityWorkflow, selectedTokenProjectId, isProjectScoped, scopedTaskRowId]);

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
    if (!isVirtualScreeningWorkflow) return;
    const protein = String(selectedProject?.protein_sequence || '').replace(/\s+/g, '').toUpperCase();
    if (protein) setBuilderVirtualScreeningProtein(protein);
  }, [isVirtualScreeningWorkflow, selectedProject?.protein_sequence, selectedTokenProjectId]);

  useEffect(() => {
    if (!LEAD_OPT_API_ACCESS_ENABLED || !isLeadOptimizationWorkflow) return;
    const ligand = String(selectedProject?.ligand_smiles || '').trim();
    if (ligand) {
      setBuilderLeadOptInputCompound((prev) => prev.trim() ? prev : ligand);
    }
  }, [isLeadOptimizationWorkflow, selectedTokenProjectId, selectedProject?.ligand_smiles]);

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
        setBuilderAffinitySeed(
          typeof task.seed === 'number' && Number.isFinite(task.seed) ? Math.max(0, Math.floor(task.seed)) : null
        );

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
        const taskPropertiesRaw =
          task.properties && typeof task.properties === 'object'
            ? (task.properties as unknown as Record<string, unknown>)
            : null;
        if (taskPropertiesRaw) {
          const taskInputOptions =
            taskPropertiesRaw.__vbio_input_options_v1 && typeof taskPropertiesRaw.__vbio_input_options_v1 === 'object'
              ? (taskPropertiesRaw.__vbio_input_options_v1 as Record<string, unknown>)
              : {};
          if (isVirtualScreeningWorkflow) {
            const screeningInput = String(
              taskInputOptions.virtualScreeningInput ?? taskInputOptions.virtual_screening_input ?? ''
            );
            if (screeningInput.trim()) {
              setBuilderVirtualScreeningInput(screeningInput);
              // The snapshot carries an inline library; a library path left over from a
              // different task would silently drop the prefilled compounds (file mode
              // disables the textarea) and submit the wrong file channel.
              setBuilderVsLibraryPath('');
            }
            const target = taskComponents.find((component) => component.type === 'protein');
            if (target?.sequence) setBuilderVirtualScreeningProtein(target.sequence.replace(/\s+/g, '').toUpperCase());
          }
          setBuilderAffinityMode(
            normalizeAffinityBuilderMode(taskInputOptions.affinityMode ?? taskPropertiesRaw.affinityMode ?? taskPropertiesRaw.mode)
          );
          setBuilderYamlProperties({
            ...EMPTY_PREDICTION_PROPERTIES,
            ...(taskPropertiesRaw as unknown as PredictionProperties)
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
    // isAffinityWorkflow re-fires when the async projects list resolves; without it the
    // affinity reset effect wipes a task prefill that landed before the workflow was known.
  }, [isProjectScoped, isVirtualScreeningWorkflow, isAffinityWorkflow, scopedProjectId, scopedTaskRowId]);

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
  const normalizedYamlBuilderComponents = useMemo(
    () => normalizeInputComponents(builderYamlComponents),
    [builderYamlComponents]
  );
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
  const yamlAssignments = useMemo(
    () => assignChainIdsForComponents(normalizedYamlBuilderComponents),
    [normalizedYamlBuilderComponents]
  );
  const predictionChainOptions = useMemo(
    () =>
      normalizedYamlBuilderComponents.flatMap((component, index) => {
        const chainIds = yamlAssignments[index] || [];
        const isSmallMolecule = component.type === 'ligand' && component.inputMethod !== 'ccd';
        return chainIds.map((chainId) => ({
          chainId,
          componentId: component.id,
          type: component.type,
          isSmallMolecule,
          label: `${chainId} · ${componentTypeLabel(component.type)} ${index + 1}`
        }));
      }),
    [normalizedYamlBuilderComponents, yamlAssignments]
  );
  const predictionTargetChainOptions = useMemo(() => {
    const preferred = predictionChainOptions.filter((item) => item.type !== 'ligand');
    return preferred.length > 0 ? preferred : predictionChainOptions;
  }, [predictionChainOptions]);
  const predictionPairTargetChain = String(builderYamlProperties.target || '').trim();
  const predictionLigandChainOptions = useMemo(() => {
    if (!predictionPairTargetChain) return predictionChainOptions;
    return predictionChainOptions.filter((item) => item.chainId !== predictionPairTargetChain);
  }, [predictionChainOptions, predictionPairTargetChain]);
  const predictionPairLigandChain = String(builderYamlProperties.ligand || builderYamlProperties.binder || '').trim();
  const predictionSelectedLigandOption = useMemo(
    () => predictionChainOptions.find((item) => item.chainId === predictionPairLigandChain) || null,
    [predictionChainOptions, predictionPairLigandChain]
  );
  const predictionPairReady = Boolean(
    predictionPairTargetChain &&
      predictionPairLigandChain &&
      predictionPairTargetChain !== predictionPairLigandChain &&
      predictionTargetChainOptions.some((item) => item.chainId === predictionPairTargetChain) &&
      predictionLigandChainOptions.some((item) => item.chainId === predictionPairLigandChain)
  );
  const predictionAffinityAvailable = Boolean(predictionPairReady && predictionSelectedLigandOption?.isSmallMolecule);
  const predictionAffinityEnabled = Boolean(builderYamlProperties.affinity);
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
  const setPredictionAffinityEnabled = useCallback(
    (enabled: boolean) => {
      setBuilderYamlProperties((prev) => {
        const base = { ...EMPTY_PREDICTION_PROPERTIES, ...prev };
        const currentTarget = normalizePredictionChainValue(base.target);
        const currentLigand = normalizePredictionChainValue(base.ligand || base.binder);
        const target =
          currentTarget && predictionTargetChainOptions.some((item) => item.chainId === currentTarget)
            ? currentTarget
            : (predictionTargetChainOptions[0]?.chainId || null);
        const ligandOptions = target
          ? predictionChainOptions.filter((item) => item.chainId !== target)
          : predictionChainOptions;
        const ligand =
          currentLigand && ligandOptions.some((item) => item.chainId === currentLigand)
            ? currentLigand
            : (ligandOptions[0]?.chainId || null);
        const selectedLigand = ligand ? predictionChainOptions.find((item) => item.chainId === ligand) || null : null;
        const allowAffinity = Boolean(enabled && target && ligand && target !== ligand && selectedLigand?.isSmallMolecule);
        return {
          ...base,
          affinity: allowAffinity,
          target,
          ligand,
          binder: ligand
        };
      });
    },
    [predictionTargetChainOptions, predictionChainOptions]
  );
  const setPredictionAffinityTargetChain = useCallback((chainId: string) => {
    setBuilderYamlProperties((prev) => ({
      ...EMPTY_PREDICTION_PROPERTIES,
      ...prev,
      target: chainId || null
    }));
  }, []);
  const setPredictionAffinityLigandChain = useCallback((chainId: string) => {
    setBuilderYamlProperties((prev) => ({
      ...EMPTY_PREDICTION_PROPERTIES,
      ...prev,
      ligand: chainId || null,
      binder: chainId || null
    }));
  }, []);
  useEffect(() => {
    if (!isPredictionWorkflow) return;
    setBuilderYamlProperties((prev) => {
      const base = { ...EMPTY_PREDICTION_PROPERTIES, ...prev };
      const currentTarget = normalizePredictionChainValue(base.target);
      const currentLigand = normalizePredictionChainValue(base.ligand || base.binder);
      const resolvedTarget =
        currentTarget && predictionTargetChainOptions.some((item) => item.chainId === currentTarget)
          ? currentTarget
          : (predictionTargetChainOptions[0]?.chainId || null);
      const resolvedLigandOptions = resolvedTarget
        ? predictionChainOptions.filter((item) => item.chainId !== resolvedTarget)
        : predictionChainOptions;
      const resolvedLigand =
        currentLigand && resolvedLigandOptions.some((item) => item.chainId === currentLigand)
          ? currentLigand
          : (resolvedLigandOptions[0]?.chainId || null);
      const selectedLigand = resolvedLigand
        ? predictionChainOptions.find((item) => item.chainId === resolvedLigand) || null
        : null;
      const allowAffinity = Boolean(
        resolvedTarget &&
          resolvedLigand &&
          resolvedTarget !== resolvedLigand &&
          selectedLigand?.isSmallMolecule
      );
      const normalized: PredictionProperties = {
        ...base,
        affinity: allowAffinity ? Boolean(base.affinity) : false,
        target: resolvedTarget,
        ligand: resolvedLigand,
        binder: resolvedLigand
      };
      return isSamePredictionProperties(base, normalized) ? prev : normalized;
    });
  }, [isPredictionWorkflow, predictionTargetChainOptions, predictionChainOptions]);
  // Library upload mode: when a compounds_file path is set, the library travels as its own
  // multipart part and the generated YAML carries only the target sequences — the two
  // channels are mutually exclusive by contract.
  const vsLibraryFileMode = isVirtualScreeningWorkflow && Boolean(builderVsLibraryPath.trim());
  useEffect(() => {
    if (!isVirtualScreeningWorkflow) return;
    setBuilderVsLibraryPath('');
  }, [isVirtualScreeningWorkflow, selectedTokenProjectId]);

  const yamlBuilderText = (() => {
    if (isVirtualScreeningWorkflow) {
      try {
        return buildVirtualScreeningYaml({
          proteinSequence: builderVirtualScreeningProtein,
          rawInput: vsLibraryFileMode ? '' : builderVirtualScreeningInput,
          batchName: selectedProject?.name || 'Virtual screening',
          libraryFromFile: vsLibraryFileMode
        }).yaml;
      } catch (error) {
        return `# ${error instanceof Error ? error.message : 'Virtual screening YAML generation failed.'}`;
      }
    }
    if (normalizedYamlBuilderComponents.length === 0) {
      return 'version: 1\nsequences: []';
    }
    try {
      return buildPredictionYamlFromComponents(normalizedYamlBuilderComponents, {
        constraints: isNessoPredictionBackend ? [] : builderYamlConstraints,
        properties: builderYamlProperties,
        templates: isNessoPredictionBackend ? [] : predictionTemplateEntries.map((entry) => ({
          fileName: entry.fileName,
          format: entry.format,
          templateChainId: entry.templateChainId,
          targetChainIds: entry.targetChainIds
        })),
        preserveLigandSmiles: true
      });
    } catch {
      return '# YAML generation failed. Please check component content.';
    }
  })();
  const predictionTemplateFlags = !isNessoPredictionBackend && predictionTemplateEnabled
    ? predictionTemplateEntries.map((entry) => ` \\\n  -F "template_files=@${entry.escapedPath}"`).join('')
    : '';
  const allCustomCcdMolecules = collectCustomCcdMoleculesFromComponents(normalizedYamlBuilderComponents, { includeLigandSmiles: false });
  const customCcdMolecules = isNessoPredictionBackend
    ? []
    : allCustomCcdMolecules.filter((item) => looksLikeAminoAcidBackboneSmiles(item.smiles));
  const customCcdFlags = customCcdMolecules.length > 0
    ? ` \\
  -F "custom_ccd_molecules=${escapeForDoubleQuotedShell(JSON.stringify(customCcdMolecules))}"`
    : '';
  const escapedTargetPath = escapeForDoubleQuotedShell(builderTargetPath.trim() || './protein.pdb');
  const escapedLigandPath = escapeForDoubleQuotedShell(builderLigandPath.trim() || './ligand.sdf');
  const escapedVsLibraryPath = escapeForDoubleQuotedShell(builderVsLibraryPath.trim());
  const vsCompoundsFileFlag = vsLibraryFileMode
    ? ` \\\n  -F "compounds_file=@${escapedVsLibraryPath}"`
    : '';
  const normalizedAffinityMode = normalizeAffinityBuilderMode(builderAffinityMode);
  const isDockBuilderMode = normalizedAffinityMode === 'dock';
  const normalizedAffinitySeed =
    typeof builderAffinitySeed === 'number' && Number.isFinite(builderAffinitySeed)
      ? Math.max(0, Math.floor(builderAffinitySeed))
      : null;
  const affinityTargetChain = String(builderAffinityTargetChain || '').trim();
  const affinityLigandChain = String(builderAffinityLigandChain || '').trim();
  const affinityLigandSmiles = String(builderAffinityLigandSmiles || '').trim();
  // Affinity-mode semantics (dock pocket, SMILES-vs-file ligand) only apply inside the affinity
  // builder; the state default 'dock' must not leak hints or flags into other workflows' commands.
  const isDockAffinityMode = builderWorkflowKey === 'affinity' && isDockBuilderMode;
  const affinityCanEnableActivity =
    !builderAffinityConfidenceOnly &&
    Boolean(affinityTargetChain && affinityLigandChain && affinityLigandSmiles);
  const affinityActivityFlags = (effectiveAffinityBackend === 'boltz') && (affinityCanEnableActivity || (isDockAffinityMode && affinityLigandSmiles))
    ? (() => {
        // Dock mode always scores the docked pose (the workspace submits enable_affinity with
        // target chain A / ligand chain L); Confidence Only applies to the score-mode family.
        const targetChain = affinityTargetChain || 'A';
        const ligandChain = affinityLigandChain || 'L';
        const affinitySmilesMap = escapeForDoubleQuotedShell(
          JSON.stringify({ [ligandChain]: affinityLigandSmiles })
        );
        return ` \\\n  -F "enable_affinity=true" \\
  -F "target_chain=${escapeForDoubleQuotedShell(targetChain)}" \\
  -F "ligand_chain=${escapeForDoubleQuotedShell(ligandChain)}" \\
  -F "ligand_smiles_map=${affinitySmilesMap}"`;
      })()
    : '';
  const affinitySeedFlag = normalizedAffinitySeed !== null ? ` \\\n  -F "seed=${normalizedAffinitySeed}"` : '';
  const affinityLigandInputFlag = isDockAffinityMode
    ? ` \\\n  -F "ligand_smiles=${escapeForDoubleQuotedShell(affinityLigandSmiles || '<LIGAND_SMILES>')}"`
    : ` \\\n  -F "ligand_file=@${escapedLigandPath}"`;
  const dockCenterComplete = Boolean(builderDockCenterX && builderDockCenterY && builderDockCenterZ);
  const dockPocketNumbers = {
    x: Number(builderDockCenterX), y: Number(builderDockCenterY), z: Number(builderDockCenterZ),
    sx: builderDockSizeX ? Number(builderDockSizeX) : 22,
    sy: builderDockSizeY ? Number(builderDockSizeY) : 22,
    sz: builderDockSizeZ ? Number(builderDockSizeZ) : 22,
  };
  // The backend accepts exactly one pocket method: explicit center axes, a
  // pocket_residues list, or a pocket_ligand reference compound whose pose
  // defines the pocket (server-side auto detection). Size axes are optional
  // extras for every method (omitted -> server default radius).
  const pocketResiduesTrimmed = builderPocketResidues.trim();
  const pocketResiduesValid = /^[A-Za-z]+:\d+(\s*,\s*[A-Za-z]+:\d+)*$/.test(pocketResiduesTrimmed);
  const pocketLigandPath = builderPocketLigandPath.trim() || './reference_ligand.sdf';
  const dockPocketSizeFlags = ` \\\n  -F "size_x=${dockPocketNumbers.sx}" \\\n  -F "size_y=${dockPocketNumbers.sy}" \\\n  -F "size_z=${dockPocketNumbers.sz}"`;
  const dockPocketMethodFlags = (() => {
    if (builderPocketMethod === 'ligand') {
      return ` \\\n  -F "pocket_ligand=@${escapeForDoubleQuotedShell(pocketLigandPath)}"`;
    }
    if (builderPocketMethod === 'residues') {
      return ` \\\n  -F "pocket_residues=${escapeForDoubleQuotedShell(
        pocketResiduesTrimmed && pocketResiduesValid ? pocketResiduesTrimmed.replace(/\s+/g, '') : '<CHAIN:RESNUM,...>'
      )}"`;
    }
    return ` \\\n  -F "center_x=${dockCenterComplete ? dockPocketNumbers.x : '<POCKET_X>'}" \\\n  -F "center_y=${dockCenterComplete ? dockPocketNumbers.y : '<POCKET_Y>'}" \\\n  -F "center_z=${dockCenterComplete ? dockPocketNumbers.z : '<POCKET_Z>'}"`;
  })();
  const affinityDockPocketFlags = isDockAffinityMode
    ? ` \\\n  -F "ligand_filename=ligand_from_smiles.sdf"${dockPocketMethodFlags}${dockPocketSizeFlags}`
    : '';
  const dockPocketMethodHint = !isDockAffinityMode
    ? ''
    : builderPocketMethod === 'ligand'
      ? '\n# Pocket is auto-detected server-side from the reference ligand file (pocket_ligand) — an SDF/MOL/PDB of a known binder placed in the binding site.'
      : builderPocketMethod === 'residues'
        ? !pocketResiduesTrimmed
          ? '\n# Fill pocket_residues with a comma-separated CHAIN:RESNUM list (e.g. A:100,A:101), or edit the command before running.'
          : pocketResiduesValid
            ? '\n# Pocket is defined by residue numbers (pocket_residues, CHAIN:RESNUM list).'
            : '\n# pocket_residues must be a comma-separated CHAIN:RESNUM list, e.g. A:100,A:101.'
        : dockCenterComplete
          ? ''
          : '\n# Dock mode submits the ligand as SMILES and needs a pocket box — fill the Pocket center fields, switch to the reference-ligand or residues method, or edit the command before running.';
  const affinityModeHint = !isAffinityWorkflow
    ? ''
    : isDockAffinityMode
      ? dockPocketMethodHint
      : !builderAffinityConfidenceOnly && !affinityCanEnableActivity
        ? '\n# Affinity activity scoring (enable_affinity) requires target chain, ligand chain, and ligand SMILES.'
        : '';
  const predictionPairHint =
    isPredictionWorkflow && !predictionPairReady
      ? '\n# Prediction chain pairing must be encoded inside yaml_file properties.'
      : '';
  const predictionAffinityHint =
    isPredictionWorkflow && predictionAffinityEnabled
      ? (predictionAffinityAvailable
        ? '\n# Prediction affinity settings are taken from yaml_file properties.'
        : '\n# Prediction affinity in yaml_file requires the selected ligand chain to be a small molecule.')
      : '';
  const leadOptInputCompound = String(builderLeadOptInputCompound || '').trim();
  const leadOptHint =
    LEAD_OPT_API_ACCESS_ENABLED && isLeadOptimizationWorkflow && !leadOptInputCompound
      ? '\n# Lead Optimization requires input_compound (SMILES).'
      : '';
  const customResidueHint =
    isPredictionWorkflow && !isNessoPredictionBackend && allCustomCcdMolecules.length > customCcdMolecules.length
      ? '\n# Some custom residue SMILES were omitted because no amino-acid backbone was detected.'
      : '';
  const predictionDeviceFlags =
    effectivePredictionBackend !== 'alphafold3' && !isNessoPredictionBackend && isPredictionWorkflow && builderPredictionLowVram
      ? ` \\\n  -F "low_vram=true"`
      : '';
  const commandEnv = `export VBIO_API_BASE="${managementApiBaseUrl}"\nexport VBIO_API_TOKEN="${curlToken}"\nexport VBIO_PROJECT_ID="${selectedTokenProjectId}"`;
  const submitTaskIdCapture = `echo "$RESPONSE"
TASK_ID=$(printf '%s' "$RESPONSE" | tr -d '\\n\\r' | sed -n 's/.*"task_id"[[:space:]]*:[[:space:]]*"\\([^"]*\\)".*/\\1/p')
if [ -z "$TASK_ID" ] || [ "$TASK_ID" = "null" ]; then
  echo "Warning: failed to parse task_id from submit response. Continue without TASK_ID." >&2
else
  echo "TASK_ID=$TASK_ID"
fi
`;
  const commandSubmitPrediction = `RESPONSE=$(curl -X POST "${managementApiBaseUrl}/predict" \\
  -H "X-API-Token: ${curlToken}" \\
  -F "project_id=${selectedTokenProjectId}"${submitTaskMetaFlags} \\
  -F "yaml_file=@${escapedYamlPath}"${vsCompoundsFileFlag} \\
  -F "backend=${effectivePredictionBackend}" \\
  -F "workflow=${isVirtualScreeningWorkflow ? 'virtual_screening' : selectedWorkflow.key === 'peptide_design' ? 'peptide_design' : 'prediction'}" \
  -F "priority=high"${predictionTemplateFlags}${customCcdFlags}${predictionDeviceFlags})
${submitTaskIdCapture}`;
  const commandSubmitAffinity = `RESPONSE=$(curl -X POST "${managementApiBaseUrl}/api/boltz2score" \\
  -H "X-API-Token: ${curlToken}" \\
  -F "project_id=${selectedTokenProjectId}"${submitTaskMetaFlags} \\
  -F "protein_file=@${escapedTargetPath}"${affinityLigandInputFlag}${affinityDockPocketFlags} \\
  -F "backend=${effectiveAffinityBackend}" \\
  -F "mode=${normalizedAffinityMode}" \\
  -F "compute_ipsae=true" \\
  -F "use_msa_server=true" \\
  -F "priority=high"${affinitySeedFlag}${affinityActivityFlags})
${submitTaskIdCapture}`;
  const commandSubmit = !isSupportedSubmitWorkflow
    ? `# Workflow "${selectedWorkflow.title}" is not supported in Command Builder.\n# Use project workflows: Prediction, Virtual Screening, or Affinity.`
    : (builderWorkflowKey === 'affinity'
      ? commandSubmitAffinity
      : commandSubmitPrediction);
  const commandSubmitWithHints = `${commandSubmit}${affinityModeHint}${predictionPairHint}${predictionAffinityHint}${leadOptHint}${customResidueHint}`;
  const submitBackendLabel = builderWorkflowKey === 'affinity' ? effectiveAffinityBackend : effectivePredictionBackend;
  const {
    copiedActionId,
    commandHistory,
    copyText,
    setCommandHistory,
  } = useCommandClipboard({
    onError: setError,
    onSuccess: setSuccess,
    buildEntryContext: () => ({
      workflow: builderWorkflowKey,
      backend: submitBackendLabel,
      projectId: selectedTokenProjectId,
      projectName: selectedProject?.name || '',
      tokenId: selectedTokenId,
      tokenName: selectedToken?.name || '',
    }),
  });
  const statusEndpoint = `/status/${taskIdForCommand}`;
  const resultsEndpoint = `/results/${taskIdForCommand}`;
  const commandStatus = `curl -X GET "${managementApiBaseUrl}${statusEndpoint}?project_id=${selectedTokenProjectId}" \\
  -H "X-API-Token: ${curlToken}"`;
  const commandResults = `curl -X GET "${managementApiBaseUrl}${resultsEndpoint}?project_id=${selectedTokenProjectId}" \\
  -H "X-API-Token: ${curlToken}" \\
  -o "${escapedResultPath}"`;
  const screeningEndpoint = `/results/${taskIdForCommand}/screening`;
  const commandScreeningResults = isVirtualScreeningWorkflow
    ? `curl -X GET "${managementApiBaseUrl}${screeningEndpoint}?project_id=${selectedTokenProjectId}" \\
  -H "X-API-Token: ${curlToken}"`
    : '';
  const commandTaskAction = `curl -X DELETE "${managementApiBaseUrl}/tasks/${taskIdForCommand}?project_id=${selectedTokenProjectId}&operation_mode=${builderTaskOperation}" \\
  -H "X-API-Token: ${curlToken}"`;

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

  // WAI-ARIA dialog behaviour (Escape / focus management / containment) for the
  // three modal-mask dialogs this page hosts.
  const yamlBuilderDialogProps = useModalDialog(yamlBuilderOpen, () => setYamlBuilderOpen(false));
  const projectTokenDialogProps = useModalDialog(
    projectTokenPanelProjectId !== null,
    () => setProjectTokenPanelProjectId(null)
  );
  const tokenRegistryDialogProps = useModalDialog(registryOpen, closeTokenRegistry);

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

  const validateBuilderCustomSmiles = useCallback((modificationId: string, smiles: string) => {
    const text = String(smiles || '').trim();
    if (!text) {
      setBuilderCustomResidueValidity((prev) => ({ ...prev, [modificationId]: false }));
      return;
    }
    setBuilderCustomResidueValidity((prev) => ({ ...prev, [modificationId]: looksLikeAminoAcidBackboneSmiles(text) }));
    void loadRDKitModule()
      .then((rdkit) => {
        const valid = rdkitMolHasAminoAcidBackbone(rdkit, text, true);
        setBuilderCustomResidueValidity((prev) => ({ ...prev, [modificationId]: valid }));
      })
      .catch(() => {
        setBuilderCustomResidueValidity((prev) => ({ ...prev, [modificationId]: looksLikeAminoAcidBackboneSmiles(text) }));
      });
  }, []);

  const patchYamlBuilderModification = (componentId: string, modificationId: string, patch: Partial<ProteinModification>) => {
    updateYamlBuilderComponent(componentId, (component) => {
      if (component.type !== 'protein') return component;
      const modifications = (component.modifications || []).map((mod) => {
        if (mod.id !== modificationId) return mod;
        const next = { ...mod, ...patch };
        const terminal = builderTerminalForPosition(Number(next.position), component.sequence, next.terminal);
        const position = builderPositionForTerminal(terminal, Number(next.position), component.sequence);
        const smiles = String(next.smiles || '').trim();
        const inputMethod = (next.inputMethod === 'jsme' ? 'jsme' : 'ccd') as ProteinModificationInputMethod;
        return {
          ...next,
          terminal,
          position,
          inputMethod,
          baseResidue: String(next.baseResidue || builderResidueAt(component.sequence, position) || 'S').toUpperCase().slice(0, 1),
          ccd: inputMethod === 'jsme' ? buildBuilderCustomCcd(component.id, position, smiles) : normalizeBuilderCcd(String(next.ccd || '')),
          smiles: inputMethod === 'jsme' ? smiles || BUILDER_CUSTOM_RESIDUE_SCAFFOLD : undefined
        };
      });
      return { ...component, modifications };
    });
  };

  const addYamlBuilderModification = (componentId: string) => {
    updateYamlBuilderComponent(componentId, (component) => {
      if (component.type !== 'protein') return component;
      return { ...component, modifications: [...(component.modifications || []), createBuilderModification(component)] };
    });
  };

  const removeYamlBuilderModification = (componentId: string, modificationId: string) => {
    updateYamlBuilderComponent(componentId, (component) => {
      if (component.type !== 'protein') return component;
      return { ...component, modifications: (component.modifications || []).filter((mod) => mod.id !== modificationId) };
    });
    setBuilderCustomResidueValidity((prev) => {
      const next = { ...prev };
      delete next[modificationId];
      return next;
    });
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
      {error && (
        <div
          role="alert"
          style={{
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: 12,
            padding: '10px 14px',
            marginBottom: 12,
            background: '#fef2f2',
            border: '1px solid #fecaca',
            borderRadius: 6,
            color: '#b91c1c',
            fontSize: 13
          }}
        >
          <span>{error}</span>
          <button
            type="button"
            onClick={() => setError(null)}
            aria-label="Dismiss error"
            style={{ background: 'none', border: 'none', color: '#b91c1c', cursor: 'pointer', fontSize: 18, lineHeight: 1 }}
          >
            ×
          </button>
        </div>
      )}
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
      <ProjectStatsPanel
        filteredProjectStatsRows={filteredProjectStatsRows}
        projectStatsLoading={projectStatsLoading}
        projectStatsPage={projectStatsPage}
        projectStatsPageCount={projectStatsPageCount}
        projectStatsSearch={projectStatsSearch}
        projectStatsSort={projectStatsSort}
        projectStatsWorkflowFilter={projectStatsWorkflowFilter}
        usageWindow={usageWindow}
        selectedTokenProjectId={selectedTokenProjectId}
        pagedProjectStatsRows={pagedProjectStatsRows}
        normalizeWorkflowFilter={normalizeProjectStatsWorkflowFilter}
        normalizeSort={(v) => normalizeProjectStatsSort(v as string)}
        onSearchChange={setProjectStatsSearch}
        onWorkflowFilterChange={setProjectStatsWorkflowFilter}
        onSortChange={setProjectStatsSort}
        onPageChange={setProjectStatsPage}
        onWindowChange={setUsageWindow}
        onSelectProject={selectProjectContext}
        onJumpToBuilder={jumpToCommandBuilder}
        onJumpToBuilderForProject={jumpToCommandBuilderForProject}
        onOpenTokenPanel={openProjectTokenPanel}
        onOpenRegistry={openTokenRegistryForProject}
      />
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
            <Field label={<><KeyRound size={12} /> Token</>}>
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
            </Field>

                          <Field label="Token Plaintext">
              <input
              value={builderTokenPlainInput}
              onChange={(e) => setBuilderTokenPlainInput(e.target.value)}
              placeholder="vbio_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
              disabled={!selectedTokenId}
              />
              </Field>

            <div className="api-builder-project">
              <span className="muted small">Project (from token)</span>
              <strong>{selectedProject?.name || '-'}</strong>
              <code>{selectedTokenProjectId}</code>
            </div>

            {!isSupportedSubmitWorkflow && (
              <div className="api-builder-note muted small">
                Command Builder currently supports Prediction, Virtual Screening, and Affinity projects.
              </div>
            )}

            {(isPredictionWorkflow || isVirtualScreeningWorkflow || (LEAD_OPT_API_ACCESS_ENABLED && isLeadOptimizationWorkflow)) && (
              <Field label={isVirtualScreeningWorkflow ? 'Virtual Screening Backend' : isLeadOptimizationWorkflow ? 'Lead Opt Backend' : 'Prediction Backend'}>
                <select
                  value={effectivePredictionBackend}
                  onChange={(e) => setBuilderPredictionBackend(normalizePredictionBackend(e.target.value))}
                  disabled={isVirtualScreeningWorkflow}
                >
                  {isVirtualScreeningWorkflow ? (
                    <option value="nesso">nesso</option>
                  ) : (
                    <>
                      <option value="boltz">boltz</option>
                      <option value="alphafold3">alphafold3</option>
                      <option value="protenix">protenix</option>
                    </>
                  )}
                </select>
              </Field>
            )}

            {isPredictionWorkflow && effectivePredictionBackend !== 'alphafold3' && !isNessoPredictionBackend && (
              <label className="checkbox-inline">
                <input
                  type="checkbox"
                  checked={builderPredictionLowVram}
                  onChange={(e) => setBuilderPredictionLowVram(e.target.checked)}
                />
                <span>Low VRAM</span>
              </label>
            )}

            {LEAD_OPT_API_ACCESS_ENABLED && isLeadOptimizationWorkflow && (
              <>
                <LeadOptOptions
                  builderLeadOptTargetConfigPath={builderLeadOptTargetConfigPath}
                  builderLeadOptInputCompound={builderLeadOptInputCompound}
                  builderLeadOptTargetChain={builderLeadOptTargetChain}
                  builderLeadOptLigandChain={builderLeadOptLigandChain}
                  builderLeadOptObjectiveProfile={builderLeadOptObjectiveProfile}
                  builderLeadOptEnableAffinity={builderLeadOptEnableAffinity}
                  onTargetConfigPathChange={setBuilderLeadOptTargetConfigPath}
                  onInputCompoundChange={setBuilderLeadOptInputCompound}
                  onTargetChainChange={setBuilderLeadOptTargetChain}
                  onLigandChainChange={setBuilderLeadOptLigandChain}
                  onObjectiveProfileChange={setBuilderLeadOptObjectiveProfile}
                  onEnableAffinityChange={setBuilderLeadOptEnableAffinity}
                />
              </>
            )}

            {isAffinityWorkflow && (
                              <Field label="Affinity Backend">
                <select
                value={effectiveAffinityBackend}
                onChange={(e) => setBuilderAffinityBackend(normalizeAffinityBackend(e.target.value))}
                >
                <option value="boltz">boltz</option>
                <option value="protenix">protenix</option>
                </select>
                </Field>
            )}

                          <Field label="Task Name (optional)">
              <input value={builderTaskName} onChange={(e) => setBuilderTaskName(e.target.value)} placeholder="Only sent when filled" />
              </Field>

                          <Field label="Task Summary (optional)">
              <input value={builderTaskSummary} onChange={(e) => setBuilderTaskSummary(e.target.value)} placeholder="Only sent when filled" />
              </Field>

            {isPredictionWorkflow && (
              <>
                <Field
                  label="YAML file path"
                  hint="The YAML Builder keeps ligand SMILES as smiles; choose CCD input only for known CCD codes."
                  hintAlign="end"
                >
                  <input value={builderYamlPath} onChange={(e) => setBuilderYamlPath(e.target.value)} placeholder="./config.yaml" />
                </Field>
                <div className="api-yaml-builder-trigger">
                  <button className="btn btn-secondary" type="button" onClick={() => setYamlBuilderOpen(true)}>
                    Open YAML Builder
                  </button>
                  <span className="muted small">
                    {builderYamlComponents.length} components · {predictionTemplateEnabled ? 'template configured' : 'no template'}
                  </span>
                </div>
                <section className="api-prediction-affinity-panel">
                  <div className="api-prediction-affinity-head">
                    <span className="api-prediction-affinity-title">
                      <ShieldCheck size={14} />
                      Pair
                    </span>
                    <label className="checkbox-inline api-prediction-affinity-toggle">
                      <input
                        type="checkbox"
                        checked={predictionAffinityEnabled}
                        onChange={(e) => setPredictionAffinityEnabled(e.target.checked)}
                        disabled={!predictionAffinityAvailable}
                      />
                      <span>Affinity</span>
                    </label>
                  </div>
                  <div className="api-prediction-affinity-grid">
                                          <Field label="Target chain">
                      <select
                      value={predictionPairTargetChain}
                      onChange={(e) => setPredictionAffinityTargetChain(e.target.value)}
                      disabled={predictionTargetChainOptions.length === 0}
                      >
                      {predictionTargetChainOptions.length === 0 ? (
                        <option value="">None</option>
                      ) : (
                        predictionTargetChainOptions.map((item) => (
                          <option key={`prediction-target-${item.chainId}`} value={item.chainId}>
                            {item.label}
                          </option>
                        ))
                      )}
                      </select>
                      </Field>
                                          <Field label="Ligand chain">
                      <select
                      value={predictionPairLigandChain}
                      onChange={(e) => setPredictionAffinityLigandChain(e.target.value)}
                      disabled={predictionLigandChainOptions.length === 0}
                      >
                      {predictionLigandChainOptions.length === 0 ? (
                        <option value="">None</option>
                      ) : (
                        predictionLigandChainOptions.map((item) => (
                          <option key={`prediction-ligand-${item.chainId}`} value={item.chainId}>
                            {item.label}
                          </option>
                        ))
                      )}
                      </select>
                      </Field>
                  </div>
                  {!predictionPairReady && (
                    <span className="muted small">Target + ligand are required.</span>
                  )}
                  {predictionPairReady && !predictionAffinityAvailable && (
                    <span className="muted small">Not a small molecule — pair metrics still computed.</span>
                  )}
                </section>
              </>
            )}

            {isVirtualScreeningWorkflow && (
              <>
                                  <Field label="YAML file path">
                  <input value={builderYamlPath} onChange={(e) => setBuilderYamlPath(e.target.value)} placeholder="./config.yaml" />
                  </Field>
                                  <Field label="Target protein sequence">
                  <textarea
                  rows={7}
                  value={builderVirtualScreeningProtein}
                  onChange={(e) => setBuilderVirtualScreeningProtein(e.target.value.replace(/\s+/g, '').toUpperCase())}
                  placeholder="One-letter amino-acid sequence"
                  spellCheck={false}
                  />
                  </Field>
                <Field
                  label="Compound library"
                  hint="One SMILES per line; optional 'name SMILES' rows or FASTA-style >name headers."
                  hintAlign="end"
                >
                  <textarea
                    rows={10}
                    value={builderVirtualScreeningInput}
                    onChange={(e) => setBuilderVirtualScreeningInput(e.target.value)}
                    placeholder={VIRTUAL_SCREENING_EXAMPLE}
                    spellCheck={false}
                    disabled={vsLibraryFileMode}
                  />
                </Field>
                                  <Field label="Load library from file (.smi / .csv / .txt)">
                  <input
                  type="file"
                  accept=".smi,.smiles,.csv,.tsv,.txt"
                  disabled={vsLibraryFileMode}
                  onChange={(e) => {
                    const file = e.target.files?.[0];
                    e.currentTarget.value = '';
                    if (!file) return;
                    void file.text().then((text) => {
                      setBuilderVirtualScreeningInput(text.trim());
                    });
                  }}
                  />
                  </Field>
                <Field
                  label="Library file path"
                  hint="Uploaded as a separate compounds_file part, replacing the inline library. Provide the library inline or as a file — never both."
                  hintAlign="end"
                >
                  <input
                    value={builderVsLibraryPath}
                    onChange={(e) => setBuilderVsLibraryPath(e.target.value)}
                    placeholder="./library.smi"
                    spellCheck={false}
                  />
                </Field>
                <div className="api-builder-note muted small">
                  Ranking-only results (no structures) — read them from /results/&lt;TASK_ID&gt;/screening.
                  <InfoTip
                    text="screening.json carries rank, affinity_pred_value (log10 IC50 in µM — lower is stronger) and ic50_um per compound. Poll /status and /results like prediction tasks."
                    align="end"
                  />
                </div>
              </>
            )}

            <AffinityConfigBlock
              isAffinityWorkflow={isAffinityWorkflow}
              isDockBuilderMode={isDockBuilderMode}
              normalizeMode={normalizeAffinityBuilderMode}
              builderAffinityConfidenceOnly={builderAffinityConfidenceOnly}
              builderAffinityMode={builderAffinityMode}
              builderAffinitySeed={builderAffinitySeed}
              builderTargetPath={builderTargetPath}
              builderAffinityLigandSmiles={builderAffinityLigandSmiles}
              builderPocketMethod={builderPocketMethod}
              builderDockCenterX={builderDockCenterX}
              builderDockCenterY={builderDockCenterY}
              builderDockCenterZ={builderDockCenterZ}
              builderDockSizeX={builderDockSizeX}
              builderDockSizeY={builderDockSizeY}
              builderDockSizeZ={builderDockSizeZ}
              builderPocketLigandPath={builderPocketLigandPath}
              builderPocketResidues={builderPocketResidues}
              builderLigandPath={builderLigandPath}
              builderAffinityTargetChain={builderAffinityTargetChain}
              builderAffinityLigandChain={builderAffinityLigandChain}
              onAffinityConfidenceOnlyChange={setBuilderAffinityConfidenceOnly}
              onAffinityModeChange={(v) => setBuilderAffinityMode(v as typeof builderAffinityMode)}
              onAffinitySeedChange={setBuilderAffinitySeed}
              onTargetPathChange={setBuilderTargetPath}
              onAffinityLigandSmilesChange={setBuilderAffinityLigandSmiles}
              onPocketMethodChange={(v) => setBuilderPocketMethod(v as 'center' | 'ligand' | 'residues')}
              onDockCenterXChange={setBuilderDockCenterX}
              onDockCenterYChange={setBuilderDockCenterY}
              onDockCenterZChange={setBuilderDockCenterZ}
              onDockSizeXChange={setBuilderDockSizeX}
              onDockSizeYChange={setBuilderDockSizeY}
              onDockSizeZChange={setBuilderDockSizeZ}
              onPocketLigandPathChange={setBuilderPocketLigandPath}
              onPocketResiduesChange={setBuilderPocketResidues}
              onLigandPathChange={setBuilderLigandPath}
              onAffinityTargetChainChange={setBuilderAffinityTargetChain}
              onAffinityLigandChainChange={setBuilderAffinityLigandChain}
            />

                          <Field label="Result ZIP path">
              <input value={builderResultPath} onChange={(e) => setBuilderResultPath(e.target.value)} placeholder="./result.zip" />
              </Field>

                          <Field label="Task operation">
              <select value={builderTaskOperation} onChange={(e) => setBuilderTaskOperation((e.target.value === 'delete' ? 'delete' : 'cancel'))}>
              <option value="cancel">cancel</option>
              <option value="delete">delete</option>
              </select>
              </Field>
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

          <ApiCommandRightColumn
            clipboard={{
              copiedActionId,
              copyText,
              commandHistory,
              setCommandHistory,
              applyCommandHistory
            }}
            commands={{
              commandEnv,
              yamlBuilderText,
              downloadGeneratedYaml,
              commandSubmitWithHints,
              commandStatus,
              commandResults,
              commandScreeningResults,
              commandTaskAction
            }}
            workflow={{
              isPredictionWorkflow,
              isVirtualScreeningWorkflow,
              isSupportedSubmitWorkflow,
              selectedWorkflow,
              builderWorkflowKey,
              effectivePredictionBackend,
              effectiveAffinityBackend,
              builderTaskOperation
            }}
          />
        </div>
      </section>

      <UsagePanel
        tokens={tokens}
        selectedTokenId={selectedTokenId}
        selectedProjectTokens={selectedProjectTokens}
        tokenUsage={tokenUsage}
        tokenUsageDaily={tokenUsageDaily}
        tokenUsageTotal={tokenUsageTotal}
        selectedTokenUsageSummary={selectedTokenUsageSummary}
        eventPage={eventPage}
        eventPageCount={eventPageCount}
        usageBarsPage={usageBarsPage}
        usageBarsPageCount={usageBarsPageCount}
        pagedDailyUsage={pagedDailyUsage}
        maxDailyCount={maxDailyCount}
        onEventPageChange={setEventPage}
        onUsageBarsPageChange={setUsageBarsPage}
        onSelectedTokenIdChange={setSelectedTokenId}
        onOpenRegistry={openTokenRegistry}
      />

      {!isProjectScoped && (
        <ApiDocsPanel />
      )}

      {yamlBuilderOpen && (
        <ApiYamlBuilderModal
          chrome={{
            dialogProps: yamlBuilderDialogProps,
            setYamlBuilderOpen,
            gridRef: yamlBuilderGridRef,
            gridStyle: yamlBuilderGridStyle,
            isResizing: isYamlBuilderResizing,
            onResizePointerDown: handleYamlBuilderResizerPointerDown,
            onResizeKeyDown: handleYamlBuilderResizerKeyDown
          }}
          components={{
            builderYamlComponents,
            builderYamlCollapsed,
            builderCustomResidueValidity,
            addYamlBuilderComponent,
            updateYamlBuilderComponent,
            toggleYamlBuilderComponentCollapsed,
            removeYamlBuilderComponent,
            addYamlBuilderModification,
            patchYamlBuilderModification,
            removeYamlBuilderModification,
            validateBuilderCustomSmiles
          }}
          templates={{
            builderYamlTemplates,
            setBuilderYamlTemplates,
            updateYamlBuilderTemplate
          }}
          constraints={{
            builderYamlConstraints,
            setBuilderYamlConstraints,
            builderYamlProperties,
            setBuilderYamlProperties,
            builderYamlConstraintsOpen,
            setBuilderYamlConstraintsOpen,
            normalizedYamlBuilderComponents
          }}
          preview={{
            yamlComponentStats,
            yamlBuilderText,
            copiedActionId,
            copyText,
            downloadGeneratedYaml
          }}
        />
      )}

      {projectTokenPanelProjectId && (
        <ApiProjectTokenModal
          projectTokenPanelProjectId={projectTokenPanelProjectId}
          projectTokenPanelProject={projectTokenPanelProject}
          projectTokenPanelTokens={projectTokenPanelTokens}
          dialogProps={projectTokenDialogProps}
          setProjectTokenPanelProjectId={setProjectTokenPanelProjectId}
          setSelectedProjectId={setSelectedProjectId}
          setSelectedTokenId={setSelectedTokenId}
          tokenRevokingId={tokenRevokingId}
          tokenDeletingId={tokenDeletingId}
          revokeToken={revokeToken}
          removeToken={removeToken}
          openTokenRegistryForProject={openTokenRegistryForProject}
        />
      )}

      {registryOpen && (
        <ApiTokenRegistryModal
          registryScopeProject={registryScopeProject}
          tokenRegistryDialogProps={tokenRegistryDialogProps}
          closeTokenRegistry={closeTokenRegistry}
          createApiToken={createApiToken}
          tokenCreating={tokenCreating}
          newTokenName={newTokenName}
          setNewTokenName={setNewTokenName}
          newTokenExpiresDays={newTokenExpiresDays}
          setNewTokenExpiresDays={setNewTokenExpiresDays}
          newTokenPlainText={newTokenPlainText}
          setNewTokenPlainText={setNewTokenPlainText}
          allowSubmit={allowSubmit}
          setAllowSubmit={setAllowSubmit}
          allowDelete={allowDelete}
          setAllowDelete={setAllowDelete}
          allowCancel={allowCancel}
          setAllowCancel={setAllowCancel}
          projects={projects}
          projectLoading={projectLoading}
          selectedProjectId={selectedProjectId}
          setSelectedProjectId={setSelectedProjectId}
          showRegistryProjectColumn={showRegistryProjectColumn}
          tokenLoading={tokenLoading}
          tokenQuery={tokenQuery}
          setTokenQuery={setTokenQuery}
          tokenPage={tokenPage}
          setTokenPage={setTokenPage}
          tokenPageCount={tokenPageCount}
          pagedTokens={pagedTokens}
          selectedTokenId={selectedTokenId}
          setSelectedTokenId={setSelectedTokenId}
          tokenRevokingId={tokenRevokingId}
          tokenDeletingId={tokenDeletingId}
          revokeToken={revokeToken}
          removeToken={removeToken}
          copiedActionId={copiedActionId}
          copyText={copyText}
        />
      )}
    </div>
  );
}
