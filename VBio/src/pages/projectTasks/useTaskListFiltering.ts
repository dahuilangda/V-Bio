import { useCallback, useEffect, useMemo, useState } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { ProjectTask } from '../../types/models';
import { loadRDKitModule } from '../../utils/rdkit';
import type {
  SeedFilterOption,
  SortDirection,
  SortKey,
  StructureSearchMode,
  SubmittedWithinDaysOption,
  TaskListRow,
  TaskWorkflowFilter,
} from './taskListTypes';
import {
  TASKS_PAGE_FILTERS_STORAGE_KEY,
  TASK_SORT_DIRECTIONS,
  TASK_SORT_KEYS,
  TASK_SUBMITTED_WINDOW_OPTIONS,
  TASK_SEED_FILTER_OPTIONS,
  TASK_STRUCTURE_SEARCH_MODES,
  TASK_PAGE_SIZE_OPTIONS,
  compareNullableNumber,
  defaultSortDirection,
  nextSortDirection,
  parseNumberOrNull,
  normalizePlddtThreshold,
  normalizeIptmThreshold,
  normalizeSmilesForSearch,
  hasSubstructureMatchPayload,
} from './taskDataUtils';
import { backendLabel } from './taskPresentation';

interface UseTaskListFilteringResult {
  sortKey: SortKey;
  sortDirection: SortDirection;
  taskSearch: string;
  stateFilter: 'all' | ProjectTask['task_state'];
  workflowFilter: TaskWorkflowFilter;
  backendFilter: 'all' | string;
  showAdvancedFilters: boolean;
  submittedWithinDays: SubmittedWithinDaysOption;
  seedFilter: SeedFilterOption;
  failureOnly: boolean;
  minPlddt: string;
  minIptm: string;
  maxPae: string;
  structureSearchMode: StructureSearchMode;
  structureSearchQuery: string;
  structureSearchMatches: Record<string, boolean>;
  structureSearchLoading: boolean;
  structureSearchError: string | null;
  pageSize: number;
  page: number;
  advancedFilterCount: number;
  filteredRows: TaskListRow[];
  pagedRows: TaskListRow[];
  totalPages: number;
  currentPage: number;
  setSortKey: Dispatch<SetStateAction<SortKey>>;
  setSortDirection: Dispatch<SetStateAction<SortDirection>>;
  setTaskSearch: Dispatch<SetStateAction<string>>;
  setStateFilter: Dispatch<SetStateAction<'all' | ProjectTask['task_state']>>;
  setWorkflowFilter: Dispatch<SetStateAction<TaskWorkflowFilter>>;
  setBackendFilter: Dispatch<SetStateAction<'all' | string>>;
  setShowAdvancedFilters: Dispatch<SetStateAction<boolean>>;
  setSubmittedWithinDays: Dispatch<SetStateAction<SubmittedWithinDaysOption>>;
  setSeedFilter: Dispatch<SetStateAction<SeedFilterOption>>;
  setFailureOnly: Dispatch<SetStateAction<boolean>>;
  setMinPlddt: Dispatch<SetStateAction<string>>;
  setMinIptm: Dispatch<SetStateAction<string>>;
  setMaxPae: Dispatch<SetStateAction<string>>;
  setStructureSearchMode: Dispatch<SetStateAction<StructureSearchMode>>;
  setStructureSearchQuery: Dispatch<SetStateAction<string>>;
  setPageSize: Dispatch<SetStateAction<number>>;
  setPage: Dispatch<SetStateAction<number>>;
  clearAdvancedFilters: () => void;
  handleSort: (key: SortKey) => void;
  sortMark: (key: SortKey) => string;
  jumpToPage: (rawValue: string) => void;
}

export function useTaskListFiltering(taskRows: TaskListRow[]): UseTaskListFilteringResult {
  const [sortKey, setSortKey] = useState<SortKey>('submitted');
  const [sortDirection, setSortDirection] = useState<SortDirection>('desc');
  const [taskSearch, setTaskSearch] = useState('');
  const [stateFilter, setStateFilter] = useState<'all' | ProjectTask['task_state']>('all');
  const [workflowFilter, setWorkflowFilter] = useState<TaskWorkflowFilter>('all');
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

  const advancedFilterCount = useMemo(() => {
    const leadOptOnlyFiltering = workflowFilter === 'lead_optimization';
    let count = 0;
    if (submittedWithinDays !== 'all') count += 1;
    if (seedFilter !== 'all') count += 1;
    if (failureOnly) count += 1;
    if (!leadOptOnlyFiltering && minPlddt.trim()) count += 1;
    if (!leadOptOnlyFiltering && minIptm.trim()) count += 1;
    if (!leadOptOnlyFiltering && maxPae.trim()) count += 1;
    if (structureSearchQuery.trim()) count += 1;
    return count;
  }, [workflowFilter, submittedWithinDays, seedFilter, failureOnly, minPlddt, minIptm, maxPae, structureSearchQuery]);

  const clearAdvancedFilters = useCallback(() => {
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
  }, []);

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
      if (
        typeof saved.workflowFilter === 'string' &&
        ['all', 'prediction', 'affinity', 'lead_optimization'].includes(saved.workflowFilter)
      ) {
        setWorkflowFilter(saved.workflowFilter as TaskWorkflowFilter);
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
      workflowFilter,
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
    workflowFilter,
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
    const leadOptOnlyFiltering = workflowFilter === 'lead_optimization';
    const structureSearchActive = Boolean(structureSearchQuery.trim());
    const applyStructureFilter = structureSearchActive && !structureSearchLoading && !structureSearchError;
    const submittedWindowMs = submittedWithinDays === 'all' ? null : Number(submittedWithinDays) * 24 * 60 * 60 * 1000;
    const submittedCutoff = submittedWindowMs === null ? null : Date.now() - submittedWindowMs;
    const minPlddtThreshold = leadOptOnlyFiltering ? null : normalizePlddtThreshold(parseNumberOrNull(minPlddt));
    const minIptmThreshold = leadOptOnlyFiltering ? null : normalizeIptmThreshold(parseNumberOrNull(minIptm));
    const maxPaeThreshold = leadOptOnlyFiltering ? null : parseNumberOrNull(maxPae);

    const filtered = taskRows.filter((row) => {
      const { task, metrics } = row;
      if (stateFilter !== 'all' && task.task_state !== stateFilter) return false;
      if (workflowFilter !== 'all' && row.workflowKey !== workflowFilter) return false;
      if (!leadOptOnlyFiltering && backendFilter !== 'all' && row.backendValue !== backendFilter) return false;
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
        task.name,
        task.summary,
        task.task_state,
        row.backendValue,
        row.workflowLabel,
        backendLabel(row.backendValue),
        task.status_text,
        task.error_text,
        task.structure_name,
        task.seed ?? '',
        metrics.plddt ?? '',
        metrics.iptm ?? '',
        metrics.pae ?? '',
        row.leadOptMmpSummary,
        row.leadOptMmpStage,
        row.leadOptSelectedFragmentIds.join(' '),
        row.leadOptSelectedFragmentQuery
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
    workflowFilter,
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
    workflowFilter,
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

  const handleSort = useCallback((key: SortKey) => {
    if (sortKey === key) {
      setSortDirection((prev) => nextSortDirection(prev));
      return;
    }
    setSortKey(key);
    setSortDirection(defaultSortDirection(key));
  }, [sortKey]);

  const sortMark = useCallback(
    (key: SortKey) => {
      if (sortKey !== key) return '↕';
      return sortDirection === 'asc' ? '↑' : '↓';
    },
    [sortDirection, sortKey]
  );

  return {
    sortKey,
    sortDirection,
    taskSearch,
    stateFilter,
    workflowFilter,
    backendFilter,
    showAdvancedFilters,
    submittedWithinDays,
    seedFilter,
    failureOnly,
    minPlddt,
    minIptm,
    maxPae,
    structureSearchMode,
    structureSearchQuery,
    structureSearchMatches,
    structureSearchLoading,
    structureSearchError,
    pageSize,
    page,
    advancedFilterCount,
    filteredRows,
    pagedRows,
    totalPages,
    currentPage,
    setSortKey,
    setSortDirection,
    setTaskSearch,
    setStateFilter,
    setWorkflowFilter,
    setBackendFilter,
    setShowAdvancedFilters,
    setSubmittedWithinDays,
    setSeedFilter,
    setFailureOnly,
    setMinPlddt,
    setMinIptm,
    setMaxPae,
    setStructureSearchMode,
    setStructureSearchQuery,
    setPageSize,
    setPage,
    clearAdvancedFilters,
    handleSort,
    sortMark,
    jumpToPage,
  };
}
