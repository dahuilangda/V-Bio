import { ChangeEvent, FormEvent, useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from 'react';
import { Activity, ArrowLeft, CheckCircle2, Database, Filter, Link2, ListChecks, Loader2, Pencil, Plus, RotateCcw, Save, Search, Settings2, ShieldCheck, Sparkles, Trash2, Upload, X } from 'lucide-react';
import {
  applyMmpLifecycleBatch,
  clearMmpLifecycleExperiments,
  checkMmpLifecycleBatch,
  createMmpLifecycleBatch,
  createMmpLifecycleMethod,
  deleteMmpLifecycleBatch,
  deleteMmpLifecycleMethod,
  fetchMmpLifecycleCompoundsPreview,
  fetchMmpLifecycleDatabaseSyncQueue,
  fetchMmpLifecycleMethodUsage,
  fetchMmpLifecycleOverview,
  fetchMmpLifecycleDatabaseProperties,
  fetchMmpLifecyclePropertyMappings,
  materializeMmpLifecycleExperimentsFromCompounds,
  patchMmpLifecycleBatch,
  patchMmpLifecycleMethod,
  saveMmpLifecyclePropertyMappings,
  uploadMmpLifecycleCompounds,
  uploadMmpLifecycleExperiments,
  type MmpLifecycleBatch,
  type MmpLifecycleDatabaseOperationLock,
  type MmpLifecycleDatabaseItem,
  type MmpLifecycleMethod,
  type MmpLifecycleMethodUsage,
  type MmpLifecyclePendingDatabaseSync,
} from '../api/backendApi';
import { formatDateTime } from '../utils/date';
import { useAuth } from '../hooks/useAuth';
import { MmpDatabaseAdminPanel } from '../components/admin/MmpDatabaseAdminPanel';
import { MemoLigand2DPreview } from '../components/project/Ligand2DPreview';
import { useResizablePane } from './projectDetail/useResizablePane';
import { RunPlayIcon } from './projectDetail/RunPlayIcon';
import '../styles/project-tasks.css';

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

function isMissingCellToken(value: unknown): boolean {
  const token = readText(value);
  if (!token) return true;
  const upper = token.toUpperCase();
  return upper === '*' || upper === 'NA' || upper === 'N/A' || upper === 'NAN' || upper === 'NULL' || upper === 'NONE' || upper === '-';
}

function readNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const num = Number(value);
    if (Number.isFinite(num)) return num;
  }
  return null;
}

function normalizeBatchStatusToken(value: unknown): string {
  const text = readText(value).toLowerCase();
  if (!text) return 'draft';
  if (text.includes('deleting')) return 'deleting';
  if (text.includes('queued') || text.includes('queue')) return 'queued';
  if (text.includes('running')) return 'running';
  if (text.includes('applied')) return 'applied';
  if (text.includes('failed')) return 'failed';
  if (text.includes('checked')) return 'checked';
  if (text.includes('review')) return 'reviewed';
  if (text.includes('approved')) return 'approved';
  if (text.includes('rejected')) return 'rejected';
  if (text.includes('rolled')) return 'rolled-back';
  return 'draft';
}

interface MappingDraft {
  id: string;
  source_property: string;
  mmp_property: string;
  method_id: string;
  value_transform: ActivityTransform;
  notes: string;
}

interface MethodFormErrors {
  key: boolean;
  name: boolean;
  output_property: boolean;
}

interface CheckOverview {
  compoundTotalRows: number;
  compoundAnnotatedRows: number;
  compoundReindexRows: number;
  experimentTotalRows: number;
  experimentImportableRows: number;
  experimentUpdateRows: number;
  experimentInsertRows: number;
  experimentNoopRows: number;
  experimentUnmappedRows: number;
  experimentUnmatchedRows: number;
  experimentInvalidRows: number;
}

type BatchSortKey = 'updated_at' | 'name' | 'status' | 'selected_database_id';
type SortDirection = 'asc' | 'desc';

type FlowStep = 'batch' | 'upload' | 'mapping' | 'qa';

const FLOW_STEPS: Array<{ key: FlowStep; label: string }> = [
  { key: 'batch', label: 'Batch' },
  { key: 'upload', label: 'Upload' },
  { key: 'mapping', label: 'Mapping' },
  { key: 'qa', label: 'Check' },
];

const BULK_APPLY_RELAXED_POLICY: Record<string, unknown> = {
  max_compound_invalid_smiles_rows: 1_000_000_000,
  max_experiment_invalid_rows: 1_000_000_000,
  max_unmapped_property_rows: 1_000_000_000,
  max_unmatched_compound_rows: 1_000_000_000,
  require_check_for_selected_database: false,
  require_approved_status: false,
  require_importable_experiment_rows: false,
  require_importable_compound_rows: false,
};

const UPLOAD_PREVIEW_ROW_CAP = 500;
const UPLOAD_PREVIEW_PAGE_SIZE = 8;

type ActivityTransform =
  | 'none'
  | 'to_pic50_from_nm'
  | 'to_pic50_from_um'
  | 'to_ic50_nm_from_pic50'
  | 'to_ic50_um_from_pic50'
  | 'log10'
  | 'neg_log10'
  | 'from_log10'
  | 'from_neg_log10';

const ASSAY_CATEGORY_OPTIONS = [
  'Binding',
  'Functional',
  'ADME',
  'PK/PD',
  'Safety',
  'Cell-based',
  'In vivo',
  'Other',
];

const ACTIVITY_TRANSFORM_OPTIONS: Array<{ value: ActivityTransform; label: string }> = [
  { value: 'none', label: 'Raw' },
  { value: 'to_pic50_from_nm', label: 'nM -> pIC50' },
  { value: 'to_pic50_from_um', label: 'uM -> pIC50' },
  { value: 'to_ic50_nm_from_pic50', label: 'pIC50 -> nM' },
  { value: 'to_ic50_um_from_pic50', label: 'pIC50 -> uM' },
  { value: 'log10', label: 'log10(x)' },
  { value: 'neg_log10', label: '-log10(x)' },
  { value: 'from_log10', label: '10^x (from log10)' },
  { value: 'from_neg_log10', label: '10^-x (from -log10)' },
];

function summarizeCount(value: unknown): string {
  const num = readNumber(value);
  if (num === null) return '-';
  return Math.trunc(num).toLocaleString();
}

function asNonNegativeInt(value: unknown): number {
  const num = readNumber(value);
  if (num === null) return 0;
  return Math.max(0, Math.trunc(num));
}

function readActionCount(summary: Record<string, unknown>, action: string): number {
  return asNonNegativeInt(asRecord(summary.action_counts)[action]);
}

function buildCheckOverview(compoundSummary: Record<string, unknown>, experimentSummary: Record<string, unknown>): CheckOverview {
  return {
    compoundTotalRows: asNonNegativeInt(compoundSummary.total_rows),
    compoundAnnotatedRows: asNonNegativeInt(compoundSummary.annotated_rows),
    compoundReindexRows: asNonNegativeInt(compoundSummary.reindex_rows),
    experimentTotalRows: asNonNegativeInt(experimentSummary.rows_total),
    experimentImportableRows: asNonNegativeInt(experimentSummary.rows_will_import),
    experimentUpdateRows: readActionCount(experimentSummary, 'UPDATE_COMPOUND_PROPERTY'),
    experimentInsertRows:
      readActionCount(experimentSummary, 'INSERT_PROPERTY_NAME_AND_COMPOUND_PROPERTY') +
      readActionCount(experimentSummary, 'INSERT_COMPOUND_PROPERTY'),
    experimentNoopRows: readActionCount(experimentSummary, 'NOOP_VALUE_UNCHANGED'),
    experimentUnmappedRows: asNonNegativeInt(experimentSummary.rows_unmapped),
    experimentUnmatchedRows: asNonNegativeInt(experimentSummary.rows_unmatched_compound),
    experimentInvalidRows: asNonNegativeInt(experimentSummary.rows_invalid),
  };
}

function getDatabaseBuildState(item: MmpLifecycleDatabaseItem): 'ready' | 'building' {
  const stats = asRecord(item.stats);
  const compounds = readNumber(stats.compounds);
  const rules = readNumber(stats.rules);
  const pairs = readNumber(stats.pairs);
  return compounds !== null && rules !== null && pairs !== null ? 'ready' : 'building';
}

function splitDelimitedLine(line: string, delimiter: string): string[] {
  const cells: string[] = [];
  let current = '';
  let quoted = false;
  for (let i = 0; i < line.length; i += 1) {
    const ch = line[i];
    if (ch === '"') {
      if (quoted && line[i + 1] === '"') {
        current += '"';
        i += 1;
      } else {
        quoted = !quoted;
      }
      continue;
    }
    if (!quoted && ch === delimiter) {
      cells.push(current.trim());
      current = '';
      continue;
    }
    current += ch;
  }
  cells.push(current.trim());
  return cells;
}

function dedupeColumns(columns: string[]): string[] {
  const unique = new Set<string>();
  for (const item of columns) {
    const token = item.trim();
    if (!token) continue;
    unique.add(token);
  }
  return Array.from(unique);
}

function normalizeMappingRowsWithMethods(rows: MappingDraft[], methods: MmpLifecycleMethod[]): MappingDraft[] {
  const methodById = new Map<string, MmpLifecycleMethod>();
  const methodIdByOutputLower = new Map<string, string>();
  for (const method of methods) {
    const id = readText(method.id);
    if (!id) continue;
    methodById.set(id, method);
    const output = readText(method.output_property).toLowerCase();
    if (output && !methodIdByOutputLower.has(output)) {
      methodIdByOutputLower.set(output, id);
    }
  }

  return rows.map((row) => {
    const sourceProperty = readText(row.source_property);
    const notes = readText(row.notes);
    let methodId = readText(row.method_id);
    let mmpProperty = readText(row.mmp_property);
    const valueTransform: ActivityTransform = 'none';

    if (methodId) {
      const method = methodById.get(methodId);
      const output = readText(method?.output_property);
      if (output) mmpProperty = output;
    } else if (mmpProperty) {
      const matchedMethodId = methodIdByOutputLower.get(mmpProperty.toLowerCase());
      if (matchedMethodId) {
        methodId = matchedMethodId;
        const method = methodById.get(matchedMethodId);
        const output = readText(method?.output_property);
        if (output) mmpProperty = output;
      }
    }

    return {
      ...row,
      source_property: sourceProperty,
      method_id: methodId,
      mmp_property: mmpProperty,
      value_transform: valueTransform,
      notes,
    };
  });
}

function dedupeMappingRows(rows: MappingDraft[]): MappingDraft[] {
  const bySource = new Map<string, MappingDraft>();
  const score = (row: MappingDraft): number => {
    const notes = readText(row.notes).toLowerCase();
    let value = 0;
    if (readText(row.method_id)) value += 3;
    if (readText(row.mmp_property)) value += 2;
    if (notes.includes('method-bound')) value += 4;
    if (notes.includes('activity pair')) value += 3;
    if (readText(row.id).startsWith('map_')) value += 1;
    return value;
  };

  for (const row of rows) {
    const sourceProperty = readText(row.source_property);
    const mmpProperty = readText(row.mmp_property);
    if (!sourceProperty || !mmpProperty) continue;
    const normalized: MappingDraft = {
      ...row,
      source_property: sourceProperty,
      mmp_property: mmpProperty,
      method_id: readText(row.method_id),
      value_transform: 'none',
      notes: readText(row.notes),
    };
    const key = sourceProperty.toLowerCase();
    const existing = bySource.get(key);
    if (!existing) {
      bySource.set(key, normalized);
      continue;
    }
    bySource.set(key, score(normalized) >= score(existing) ? normalized : existing);
  }
  return Array.from(bySource.values()).sort((lhs, rhs) => lhs.source_property.localeCompare(rhs.source_property));
}

function detectDelimiter(headerLine: string, fileName: string): string {
  const lower = fileName.toLowerCase();
  if (lower.endsWith('.tsv')) return '\t';
  const tabCount = (headerLine.match(/\t/g) || []).length;
  const commaCount = (headerLine.match(/,/g) || []).length;
  if (tabCount > commaCount) return '\t';
  return ',';
}

function pickColumnCandidate(headers: string[], current: string, hints: string[], fallback = ''): string {
  const normalizedCurrent = current.trim();
  if (normalizedCurrent && headers.includes(normalizedCurrent)) return normalizedCurrent;
  const lookup = headers.map((item) => ({ raw: item, lower: item.toLowerCase() }));
  for (const hint of hints) {
    const exact = lookup.find((item) => item.lower === hint);
    if (exact) return exact.raw;
  }
  for (const hint of hints) {
    const contains = lookup.find((item) => item.lower.includes(hint));
    if (contains) return contains.raw;
  }
  return fallback;
}

function toTsvCell(value: string): string {
  if (/["\t\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

function parseConfiguredColumns(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => readText(item)).filter(Boolean);
  return readText(value)
    .split(/[,\t|]/g)
    .map((item) => item.trim())
    .filter(Boolean);
}

interface ParsedUploadTable {
  headers: string[];
  rows: Array<Record<string, string>>;
  totalRows: number;
  previewTruncated: boolean;
  columnNonEmptyCounts: Record<string, number>;
  columnNumericCounts: Record<string, number>;
  columnPositiveNumericCounts: Record<string, number>;
}

function normalizeActivityTransform(value: string): ActivityTransform {
  const token = readText(value) as ActivityTransform;
  if (ACTIVITY_TRANSFORM_OPTIONS.some((item) => item.value === token)) return token;
  return 'none';
}

function readBatchActivityConfig(batch: MmpLifecycleBatch | null): {
  structureColumn: string;
  activityColumns: string[];
  activityMethodMap: Record<string, string>;
  activityTransformMap: Record<string, ActivityTransform>;
} {
  if (!batch) {
    return {
      structureColumn: '',
      activityColumns: [],
      activityMethodMap: {},
      activityTransformMap: {},
    };
  }
  const files = asRecord(batch.files);
  const compoundsCfg = asRecord(asRecord(files.compounds).column_config);
  const experimentsCfg = asRecord(asRecord(files.experiments).column_config);

  const structureColumn = readText(compoundsCfg.smiles_column);
  const activityColumns = parseConfiguredColumns(experimentsCfg.activity_columns);

  const rawMethodMap = asRecord(experimentsCfg.activity_method_map);
  const activityMethodMap: Record<string, string> = {};
  for (const [activityCol, methodId] of Object.entries(rawMethodMap)) {
    const col = readText(activityCol);
    const mid = readText(methodId);
    if (!col || !mid) continue;
    activityMethodMap[col] = mid;
  }
  const legacyMethodId = readText(experimentsCfg.assay_method_id);
  if (legacyMethodId) {
    for (const col of activityColumns) {
      if (!activityMethodMap[col]) activityMethodMap[col] = legacyMethodId;
    }
  }

  const rawTransformMap = asRecord(experimentsCfg.activity_transform_map);
  const activityTransformMap: Record<string, ActivityTransform> = {};
  for (const col of activityColumns) {
    activityTransformMap[col] = normalizeActivityTransform(rawTransformMap[col] as string);
  }
  return {
    structureColumn,
    activityColumns,
    activityMethodMap,
    activityTransformMap,
  };
}

function buildUploadExecutionSignature(
  batchId: string,
  file: File,
  structureCol: string,
  activityColumns: string[],
  activityTransformMap: Record<string, ActivityTransform>,
  activityMethodMap: Record<string, string>,
): string {
  const normalizedBatchId = readText(batchId);
  const normalizedStructure = readText(structureCol);
  const normalizedColumns = Array.from(new Set(activityColumns.map((item) => readText(item)).filter(Boolean))).sort();
  const transformToken = normalizedColumns
    .map((col) => `${col}:${normalizeActivityTransform(activityTransformMap[col] || 'none')}`)
    .join('|');
  const methodToken = normalizedColumns
    .map((col) => `${col}:${readText(activityMethodMap[col])}`)
    .join('|');
  return [
    normalizedBatchId,
    file.name,
    String(file.size),
    String(file.lastModified),
    normalizedStructure,
    transformToken,
    methodToken,
  ].join('::');
}

function transformActivityValue(raw: string, transform: ActivityTransform): number {
  const parsed = Number(raw);
  if (!Number.isFinite(parsed)) {
    throw new Error(`Value "${raw}" is not numeric.`);
  }
  if (transform === 'none') return parsed;
  if (
    (transform === 'to_pic50_from_nm' || transform === 'to_pic50_from_um' || transform === 'log10' || transform === 'neg_log10')
    && parsed <= 0
  ) {
    throw new Error(`Value "${raw}" must be > 0 for transform "${transform}".`);
  }
  if (transform === 'to_pic50_from_nm') return 9 - Math.log10(parsed);
  if (transform === 'to_pic50_from_um') return 6 - Math.log10(parsed);
  if (transform === 'to_ic50_nm_from_pic50') return 10 ** (9 - parsed);
  if (transform === 'to_ic50_um_from_pic50') return 10 ** (6 - parsed);
  if (transform === 'log10') return Math.log10(parsed);
  if (transform === 'neg_log10') return -Math.log10(parsed);
  if (transform === 'from_log10') return 10 ** parsed;
  return 10 ** (-parsed);
}

function inferDisplayUnit(
  displayTransform: ActivityTransform,
  storedUnit: string,
  inputUnit: string,
): string {
  const stored = readText(storedUnit);
  const input = readText(inputUnit);
  if (displayTransform === 'to_ic50_nm_from_pic50') return 'nM';
  if (displayTransform === 'to_ic50_um_from_pic50') return 'uM';
  if (displayTransform === 'to_pic50_from_nm' || displayTransform === 'to_pic50_from_um') return 'pIC50';
  if (displayTransform === 'none') return stored || input;
  return stored || input;
}

function methodCompletenessScore(method: MmpLifecycleMethod): number {
  const keys: Array<keyof MmpLifecycleMethod> = [
    'key',
    'name',
    'output_property',
    'input_unit',
    'output_unit',
    'display_unit',
    'import_transform',
    'display_transform',
    'category',
    'description',
    'reference',
  ];
  return keys.reduce((acc, key) => (readText(method[key]) ? acc + 1 : acc), 0);
}

function dedupeLifecycleMethods(rows: MmpLifecycleMethod[]): MmpLifecycleMethod[] {
  const byId = new Map<string, MmpLifecycleMethod>();
  for (const item of rows) {
    const id = readText(item.id);
    if (!id) continue;
    const current = byId.get(id);
    if (!current) {
      byId.set(id, { ...item, id });
      continue;
    }
    const currentUpdated = readText(current.updated_at);
    const nextUpdated = readText(item.updated_at);
    let preferred = current;
    let fallback = item;
    if (nextUpdated > currentUpdated) {
      preferred = item;
      fallback = current;
    } else if (nextUpdated === currentUpdated && methodCompletenessScore(item) >= methodCompletenessScore(current)) {
      preferred = item;
      fallback = current;
    }
    byId.set(id, {
      ...fallback,
      ...preferred,
      id,
      key: readText(preferred.key || fallback.key),
      name: readText(preferred.name || fallback.name),
      output_property: readText(preferred.output_property || fallback.output_property),
      input_unit: readText(preferred.input_unit || fallback.input_unit),
      output_unit: readText(preferred.output_unit || fallback.output_unit),
      display_unit: readText(preferred.display_unit || fallback.display_unit),
      import_transform: readText(preferred.import_transform || fallback.import_transform || 'none'),
      display_transform: readText(preferred.display_transform || fallback.display_transform || 'none'),
      category: readText(preferred.category || fallback.category),
      description: readText(preferred.description || fallback.description),
      reference: readText(preferred.reference || fallback.reference),
      updated_at: readText(preferred.updated_at || fallback.updated_at),
      created_at: readText(preferred.created_at || fallback.created_at),
    });
  }
  return Array.from(byId.values());
}

export function MmpLifecycleAdminPage() {
  const { session } = useAuth();
  const isAdmin = Boolean(session?.isAdmin);
  const [, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const [databases, setDatabases] = useState<MmpLifecycleDatabaseItem[]>([]);
  const [batches, setBatches] = useState<MmpLifecycleBatch[]>([]);
  const [methods, setMethods] = useState<MmpLifecycleMethod[]>([]);

  const [selectedBatchId, setSelectedBatchId] = useState('');

  const [batchName, setBatchName] = useState('');
  const [batchDescription, setBatchDescription] = useState('');
  const [batchNotes, setBatchNotes] = useState('');
  const [batchDatabaseId, setBatchDatabaseId] = useState('');

  const [uploadFile, setUploadFile] = useState<File | null>(null);
  const [uploadHeaderOptions, setUploadHeaderOptions] = useState<string[]>([]);
  const [uploadParsing, setUploadParsing] = useState(false);
  const [uploadPreviewRows, setUploadPreviewRows] = useState<Array<Record<string, string>>>([]);
  const [uploadPreviewTotalRows, setUploadPreviewTotalRows] = useState(0);
  const [uploadPreviewTruncated, setUploadPreviewTruncated] = useState(false);
  const [uploadPreviewPage, setUploadPreviewPage] = useState(1);
  const [uploadPreviewColumnNonEmptyCounts, setUploadPreviewColumnNonEmptyCounts] = useState<Record<string, number>>({});
  const [uploadPreviewColumnNumericCounts, setUploadPreviewColumnNumericCounts] = useState<Record<string, number>>({});
  const [uploadPreviewColumnPositiveCounts, setUploadPreviewColumnPositiveCounts] = useState<Record<string, number>>({});
  const [structureColumn, setStructureColumn] = useState('');
  const [activityColumns, setActivityColumns] = useState<string[]>([]);
  const [activityMethodMap, setActivityMethodMap] = useState<Record<string, string>>({});
  const [activityTransformMap, setActivityTransformMap] = useState<Record<string, ActivityTransform>>({});
  const [autoUploadTrigger, setAutoUploadTrigger] = useState(0);
  const [autoUploadStatus, setAutoUploadStatus] = useState<'idle' | 'pending' | 'uploading' | 'success' | 'error'>('idle');
  const [autoUploadStatusText, setAutoUploadStatusText] = useState('');
  const autoUploadLastSignatureRef = useRef('');
  const savedCompoundsPreviewSignatureRef = useRef('');

  const [editingMethodId, setEditingMethodId] = useState('');
  const [methodKey, setMethodKey] = useState('');
  const [methodName, setMethodName] = useState('');
  const [methodOutputProperty, setMethodOutputProperty] = useState('');
  const [methodInputUnit, setMethodInputUnit] = useState('');
  const [methodDisplayTransform, setMethodDisplayTransform] = useState<ActivityTransform>('none');
  const [methodCategory, setMethodCategory] = useState(ASSAY_CATEGORY_OPTIONS[0]);
  const [methodDescription, setMethodDescription] = useState('');
  const [methodFormErrors, setMethodFormErrors] = useState<MethodFormErrors>({
    key: false,
    name: false,
    output_property: false,
  });
  const [methodFormMessage, setMethodFormMessage] = useState<string | null>(null);

  const [mappingRows, setMappingRows] = useState<MappingDraft[]>([]);

  const [checkResult, setCheckResult] = useState<Record<string, unknown> | null>(null);
  const [qaAction, setQaAction] = useState<'none' | 'check' | 'apply'>('none');
  const [selectedBatchIds, setSelectedBatchIds] = useState<string[]>([]);
  const [bulkApplyRunning, setBulkApplyRunning] = useState(false);
  const [flowStep, setFlowStep] = useState<FlowStep>('batch');
  const [viewMode, setViewMode] = useState<'list' | 'detail'>('list');
  const [databaseAdminModalOpen, setDatabaseAdminModalOpen] = useState(false);
  const [assayMethodModalOpen, setAssayMethodModalOpen] = useState(false);
  const [batchQuery, setBatchQuery] = useState('');
  const [batchStatusFilter, setBatchStatusFilter] = useState('all');
  const [batchDatabaseFilter, setBatchDatabaseFilter] = useState('all');
  const [batchSortKey, setBatchSortKey] = useState<BatchSortKey>('updated_at');
  const [batchSortDirection, setBatchSortDirection] = useState<SortDirection>('desc');

  const [methodEditorOpen, setMethodEditorOpen] = useState(false);
  const [assayMethodDatabaseId, setAssayMethodDatabaseId] = useState('');
  const [assayDatabasePropertyNames, setAssayDatabasePropertyNames] = useState<string[]>([]);
  const [assayBoundMethodIds, setAssayBoundMethodIds] = useState<string[]>([]);
  const [assayMethodUsageRows, setAssayMethodUsageRows] = useState<MmpLifecycleMethodUsage[]>([]);
  const [assayMethodDisplayTransformById, setAssayMethodDisplayTransformById] = useState<Record<string, ActivityTransform>>({});
  const [pendingSyncByDatabase, setPendingSyncByDatabase] = useState<Record<string, number>>({});
  const [pendingDatabaseSyncRows, setPendingDatabaseSyncRows] = useState<MmpLifecyclePendingDatabaseSync[]>([]);
  const [databaseOperationLocks, setDatabaseOperationLocks] = useState<MmpLifecycleDatabaseOperationLock[]>([]);
  const [busyOperationsByDatabase, setBusyOperationsByDatabase] = useState<Record<string, number>>({});
  const keepQaStepOnBatchSwitchRef = useRef(false);
  const {
    mainWidth: uploadConfigPanelWidth,
    isResizing: isUploadWorkspaceResizing,
    containerRef: uploadWorkspaceRef,
    onResizerPointerDown: onUploadWorkspaceResizerPointerDown,
    onResizerKeyDown: onUploadWorkspaceResizerKeyDown,
  } = useResizablePane({
    storageKey: 'mmp_lifecycle_upload_left_width',
    defaultWidth: 44,
    minWidth: 33,
    maxWidth: 56,
    minAsideWidth: 420,
    mediaQuery: '(max-width: 1080px)',
    keyStep: 1.5,
    handleWidth: 10,
  });
  const uploadWorkspaceStyle = useMemo<CSSProperties>(
    () => ({ '--mmp-life-upload-left-width': `${uploadConfigPanelWidth.toFixed(2)}%` } as CSSProperties),
    [uploadConfigPanelWidth]
  );

  const loadOverview = useCallback(async (options?: { silent?: boolean }) => {
    const silent = options?.silent === true;
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      const overview = await fetchMmpLifecycleOverview();
      setDatabases(Array.isArray(overview.databases) ? overview.databases : []);
      const nextBatches = Array.isArray(overview.batches) ? overview.batches : [];
      setBatches(nextBatches);
      setMethods(dedupeLifecycleMethods(Array.isArray(overview.methods) ? (overview.methods as MmpLifecycleMethod[]) : []));
      setPendingDatabaseSyncRows(Array.isArray(overview.pending_database_sync) ? overview.pending_database_sync : []);
      setPendingSyncByDatabase(
        overview.pending_sync_by_database && typeof overview.pending_sync_by_database === 'object'
          ? (overview.pending_sync_by_database as Record<string, number>)
          : {}
      );
      setDatabaseOperationLocks(Array.isArray(overview.database_operation_locks) ? overview.database_operation_locks : []);
      setBusyOperationsByDatabase(
        overview.busy_operations_by_database && typeof overview.busy_operations_by_database === 'object'
          ? (overview.busy_operations_by_database as Record<string, number>)
          : {}
      );
      setSelectedBatchId((prev) => {
        if (nextBatches.length === 0) return '';
        if (prev && nextBatches.some((item) => readText(item.id) === prev)) return prev;
        return readText(nextBatches[0].id);
      });
    } catch (err) {
      if (!silent) {
        setError(err instanceof Error ? err.message : 'Failed to load lifecycle overview.');
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, []);

  const loadAssayDatabaseMethodBindings = useCallback(async (databaseIdRaw: string) => {
    const databaseId = readText(databaseIdRaw);
    if (!databaseId) {
      setAssayBoundMethodIds([]);
      setAssayMethodDisplayTransformById({});
      return;
    }
    try {
      const mappings = await fetchMmpLifecyclePropertyMappings(databaseId);
      const seen = new Set<string>();
      const methodIds: string[] = [];
      const nextDisplayByMethodId: Record<string, ActivityTransform> = {};
      for (const row of mappings) {
        const methodId = readText(row.method_id);
        if (!methodId || seen.has(methodId)) continue;
        const transform = normalizeActivityTransform(readText(row.value_transform || 'none'));
        seen.add(methodId);
        methodIds.push(methodId);
        nextDisplayByMethodId[methodId] = transform;
      }
      for (const row of mappings) {
        const methodId = readText(row.method_id);
        if (!methodId) continue;
        const transform = normalizeActivityTransform(readText(row.value_transform || 'none'));
        const current = nextDisplayByMethodId[methodId];
        if (!current || current === 'none') {
          nextDisplayByMethodId[methodId] = transform;
        }
      }
      setAssayBoundMethodIds(methodIds);
      setAssayMethodDisplayTransformById(nextDisplayByMethodId);
    } catch {
      setAssayBoundMethodIds([]);
      setAssayMethodDisplayTransformById({});
    }
  }, []);

  const loadPendingSyncSummary = useCallback(async (databaseId?: string) => {
    try {
      const data = await fetchMmpLifecycleDatabaseSyncQueue({ database_id: readText(databaseId) || undefined });
      setPendingSyncByDatabase(data.pending_by_database || {});
      setPendingDatabaseSyncRows(Array.isArray(data.rows) ? data.rows : []);
    } catch {
      setPendingSyncByDatabase({});
      setPendingDatabaseSyncRows([]);
    }
  }, []);

  const loadAssayDatabaseProperties = useCallback(async (databaseIdRaw: string) => {
    const databaseId = readText(databaseIdRaw);
    if (!databaseId) {
      setAssayDatabasePropertyNames([]);
      return;
    }
    try {
      const rows = await fetchMmpLifecycleDatabaseProperties(databaseId);
      const names: string[] = [];
      const seen = new Set<string>();
      for (const item of rows) {
        const row = asRecord(item);
        const name = readText(row.name || row.label);
        if (!name) continue;
        const key = name.toLowerCase();
        if (seen.has(key)) continue;
        seen.add(key);
        names.push(name);
      }
      setAssayDatabasePropertyNames(names);
    } catch {
      setAssayDatabasePropertyNames([]);
    }
  }, []);

  const loadAssayMethodUsage = useCallback(async (databaseIdRaw: string) => {
    const databaseId = readText(databaseIdRaw);
    if (!databaseId) {
      setAssayMethodUsageRows([]);
      return;
    }
    try {
      const rows = await fetchMmpLifecycleMethodUsage({ database_id: databaseId });
      setAssayMethodUsageRows(Array.isArray(rows) ? rows : []);
    } catch {
      setAssayMethodUsageRows([]);
    }
  }, []);

  useEffect(() => {
    void loadOverview();
  }, [loadOverview]);

  const hasApplyingBatch = useMemo(
    () =>
      batches.some((item) => {
        const runtime = asRecord(item.apply_runtime);
        const phase = readText(runtime.phase).toLowerCase();
        const status = readText(item.status).toLowerCase();
        return (
          phase === 'queued'
          || phase === 'queue'
          || phase === 'running'
          || phase === 'deleting'
          || status === 'queued'
          || status === 'running'
          || status === 'deleting'
        );
      }),
    [batches]
  );

  const applyingByDatabase = useMemo(() => {
    const output: Record<string, number> = {};
    for (const item of batches) {
      const runtime = asRecord(item.apply_runtime);
      const phase = readText(runtime.phase).toLowerCase();
      const status = readText(item.status).toLowerCase();
      const active =
        phase === 'queued'
        || phase === 'queue'
        || phase === 'running'
        || phase === 'deleting'
        || status === 'queued'
        || status === 'running'
        || status === 'deleting';
      if (!active) continue;
      const dbId = readText(runtime.database_id || item.selected_database_id);
      if (!dbId) continue;
      output[dbId] = Number(output[dbId] || 0) + 1;
    }
    return output;
  }, [batches]);

  const lockedByDatabase = useMemo(() => {
    const output: Record<string, number> = {};
    for (const row of databaseOperationLocks) {
      const status = readText(row.status).toLowerCase();
      if (status && status !== 'active') continue;
      const dbId = readText(row.database_id);
      if (!dbId) continue;
      output[dbId] = Number(output[dbId] || 0) + 1;
    }
    for (const [dbId, countRaw] of Object.entries(busyOperationsByDatabase)) {
      const count = Number(countRaw || 0);
      if (!dbId || count <= 0) continue;
      output[dbId] = Math.max(Number(output[dbId] || 0), count);
    }
    return output;
  }, [databaseOperationLocks, busyOperationsByDatabase]);

  const isDatabaseBusy = useCallback(
    (databaseIdRaw: string): boolean => {
      const dbId = readText(databaseIdRaw);
      if (!dbId) return false;
      return Number(applyingByDatabase[dbId] || 0) > 0 || Number(lockedByDatabase[dbId] || 0) > 0;
    },
    [applyingByDatabase, lockedByDatabase]
  );

  useEffect(() => {
    if (!hasApplyingBatch) return;
    const timer = window.setInterval(() => {
      void loadOverview({ silent: true });
    }, 3000);
    return () => window.clearInterval(timer);
  }, [hasApplyingBatch, loadOverview]);

  const selectedBatch = useMemo(
    () => batches.find((item) => readText(item.id) === selectedBatchId) || null,
    [batches, selectedBatchId]
  );

  const compoundFileMeta = asRecord(asRecord(selectedBatch?.files).compounds);

  useEffect(() => {
    if (!selectedBatch) {
      setBatchName('');
      setBatchDescription('');
      setBatchNotes('');
      setBatchDatabaseId('');
      setFlowStep('batch');
      const emptyConfig = readBatchActivityConfig(null);
      setStructureColumn(emptyConfig.structureColumn);
      setActivityColumns(emptyConfig.activityColumns);
      setActivityMethodMap(emptyConfig.activityMethodMap);
      setActivityTransformMap(emptyConfig.activityTransformMap);
      return;
    }
    setBatchName(readText(selectedBatch.name));
    setBatchDescription(readText(selectedBatch.description));
    setBatchNotes(readText(selectedBatch.notes));
    setBatchDatabaseId(readText(selectedBatch.selected_database_id));
    const batchConfig = readBatchActivityConfig(selectedBatch);
    setStructureColumn(batchConfig.structureColumn);
    setActivityColumns(batchConfig.activityColumns);
    setActivityMethodMap(batchConfig.activityMethodMap);
    setActivityTransformMap(batchConfig.activityTransformMap);
  }, [selectedBatch]);

  useEffect(() => {
    setCheckResult(null);
    setUploadFile(null);
    setUploadHeaderOptions([]);
    setUploadPreviewRows([]);
    setUploadPreviewTotalRows(0);
    setUploadPreviewTruncated(false);
    setUploadPreviewPage(1);
    setUploadPreviewColumnNonEmptyCounts({});
    setUploadPreviewColumnNumericCounts({});
    setUploadPreviewColumnPositiveCounts({});
    setAutoUploadTrigger(0);
    setAutoUploadStatus('idle');
    setAutoUploadStatusText('');
    autoUploadLastSignatureRef.current = '';
    savedCompoundsPreviewSignatureRef.current = '';
    if (keepQaStepOnBatchSwitchRef.current) {
      keepQaStepOnBatchSwitchRef.current = false;
      setFlowStep('qa');
    } else {
      setFlowStep('batch');
    }
  }, [selectedBatchId]);

  useEffect(() => {
    const validMethodIds = new Set(methods.map((item) => readText(item.id)).filter(Boolean));
    setActivityMethodMap((prev) => {
      const next: Record<string, string> = {};
      for (const col of activityColumns) {
        const current = readText(prev[col]);
        if (current && validMethodIds.has(current)) {
          next[col] = current;
        }
      }
      const prevKeys = Object.keys(prev);
      const nextKeys = Object.keys(next);
      const unchanged =
        prevKeys.length === nextKeys.length &&
        nextKeys.every((key) => readText(prev[key]) === readText(next[key]));
      return unchanged ? prev : next;
    });
  }, [activityColumns, methods]);

  useEffect(() => {
    setActivityTransformMap((prev) => {
      const next: Record<string, ActivityTransform> = {};
      for (const col of activityColumns) {
        next[col] = normalizeActivityTransform(prev[col] || 'none');
      }
      const prevKeys = Object.keys(prev);
      const nextKeys = Object.keys(next);
      const unchanged =
        prevKeys.length === nextKeys.length &&
        nextKeys.every((key) => normalizeActivityTransform(prev[key]) === normalizeActivityTransform(next[key]));
      return unchanged ? prev : next;
    });
  }, [activityColumns]);

  useEffect(() => {
    if (viewMode === 'detail' && !selectedBatch) {
      setViewMode('list');
    }
  }, [viewMode, selectedBatch]);

  useEffect(() => {
    const totalPages = Math.max(1, Math.ceil(uploadPreviewRows.length / UPLOAD_PREVIEW_PAGE_SIZE));
    if (uploadPreviewPage > totalPages) {
      setUploadPreviewPage(totalPages);
    }
  }, [uploadPreviewPage, uploadPreviewRows.length]);

  const mergeBatch = useCallback((nextBatch: MmpLifecycleBatch) => {
    const nextId = readText(nextBatch.id);
    setBatches((prev) => {
      const index = prev.findIndex((item) => readText(item.id) === nextId);
      if (index < 0) {
        return [nextBatch, ...prev];
      }
      const rows = [...prev];
      rows[index] = nextBatch;
      return rows;
    });
    if (nextId) setSelectedBatchId(nextId);
  }, []);

  const hasBatchBeenApplied = useCallback((batch: MmpLifecycleBatch | null): boolean => {
    if (!batch) return false;
    const status = readText(batch.status).toLowerCase();
    const inlineCount = Array.isArray(batch.apply_history) ? batch.apply_history.length : 0;
    const summaryCount = asNonNegativeInt((batch as unknown as Record<string, unknown>).apply_history_count);
    return inlineCount > 0 || summaryCount > 0 || status === 'applied' || status === 'rolled_back';
  }, []);

  const ensureEditableBatchForMutation = useCallback(async (): Promise<MmpLifecycleBatch | null> => {
    if (!selectedBatch) return null;
    const runtime = asRecord(selectedBatch.apply_runtime);
    const phase = readText(runtime.phase).toLowerCase();
    if (phase === 'queued' || phase === 'queue' || phase === 'running' || phase === 'deleting') {
      throw new Error('Current batch is running. Wait for completion before editing.');
    }
    if (!hasBatchBeenApplied(selectedBatch)) {
      return selectedBatch;
    }

    const sourceId = readText(selectedBatch.id);
    const sourceName = readText(selectedBatch.name) || sourceId || 'batch';
    const forkName = `${sourceName} (edit ${new Date().toISOString().slice(0, 16).replace('T', ' ')})`;
    const sourceNotes = readText(selectedBatch.notes);
    const lineage = `Forked from ${sourceId} after apply.`;
    const forked = await createMmpLifecycleBatch({
      name: forkName,
      description: readText(selectedBatch.description),
      notes: sourceNotes ? `${sourceNotes}\n${lineage}` : lineage,
      selected_database_id: readText(selectedBatch.selected_database_id || batchDatabaseId),
      clone_from_batch_id: sourceId,
      clone_uploaded_files: true,
    });
    mergeBatch(forked);
    setCheckResult(null);
    return forked;
  }, [selectedBatch, hasBatchBeenApplied, batchDatabaseId, mergeBatch]);

  const saveBatchMeta = async () => {
    if (!selectedBatch) return;
    setSaving(true);
    setError(null);
    try {
      const next = await patchMmpLifecycleBatch(readText(selectedBatch.id), {
        name: batchName.trim(),
        description: batchDescription.trim(),
        notes: batchNotes.trim(),
        selected_database_id: batchDatabaseId.trim(),
      });
      mergeBatch(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save batch metadata.');
    } finally {
      setSaving(false);
    }
  };

  const resetBatchMetaDraft = useCallback(() => {
    if (!selectedBatch) return;
    setError(null);
    setBatchName(readText(selectedBatch.name));
    setBatchDescription(readText(selectedBatch.description));
    setBatchNotes(readText(selectedBatch.notes));
    setBatchDatabaseId(readText(selectedBatch.selected_database_id));
  }, [selectedBatch]);

  const removeBatch = async (batchId?: string) => {
    const id = readText(batchId || selectedBatch?.id);
    if (!id) return;
    if (!window.confirm(`Delete lifecycle batch "${id}"? This will queue database cleanup first, and the row will disappear only after cleanup succeeds.`)) return;
    setSaving(true);
    setError(null);
    try {
      const result = asRecord(await deleteMmpLifecycleBatch(id));
      const queuedBatch = asRecord(result.batch);
      if (Object.keys(queuedBatch).length > 0) {
        setBatches((prev) => {
          const index = prev.findIndex((item) => readText(item.id) === id);
          if (index < 0) return prev;
          const next = [...prev];
          next[index] = queuedBatch as unknown as MmpLifecycleBatch;
          return next;
        });
        await loadOverview({ silent: true });
      } else {
        setBatches((prev) => prev.filter((item) => readText(item.id) !== id));
        if (selectedBatchId === id) {
          setSelectedBatchId('');
          setViewMode('list');
        }
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete batch.');
    } finally {
      setSaving(false);
    }
  };

  const parseUploadTable = useCallback(
    async (file: File, options?: { maxRows?: number }): Promise<ParsedUploadTable> => {
      const maxRows = Math.max(0, Math.trunc(Number(options?.maxRows) || 0));
      const rowCap = maxRows > 0 ? maxRows : Number.MAX_SAFE_INTEGER;
      const lower = file.name.toLowerCase();
      const columnNonEmptyCounts: Record<string, number> = {};
      const columnNumericCounts: Record<string, number> = {};
      const columnPositiveNumericCounts: Record<string, number> = {};
      const accumulateColumnStats = (header: string, rawValue: string) => {
        const key = readText(header);
        if (!key) return;
        const token = readText(rawValue);
        if (isMissingCellToken(token)) return;
        columnNonEmptyCounts[key] = (columnNonEmptyCounts[key] || 0) + 1;
        const numeric = Number(token);
        if (Number.isFinite(numeric)) {
          columnNumericCounts[key] = (columnNumericCounts[key] || 0) + 1;
          if (numeric > 0) {
            columnPositiveNumericCounts[key] = (columnPositiveNumericCounts[key] || 0) + 1;
          }
        }
      };
      if (lower.endsWith('.xlsx')) {
        const [{ default: ExcelJS }] = await Promise.all([import('exceljs')]);
        const workbook = new ExcelJS.Workbook();
        await workbook.xlsx.load(await file.arrayBuffer());
        const sheet = workbook.worksheets[0];
        if (!sheet) {
          throw new Error('Excel file has no worksheet.');
        }
        const headerRow = sheet.getRow(1);
        const maxCol = Math.max(1, headerRow.cellCount, sheet.columnCount);
        const rawHeaders: string[] = [];
        for (let colNo = 1; colNo <= maxCol; colNo += 1) {
          const text = (headerRow.getCell(colNo).text || readText(headerRow.getCell(colNo).value)).trim();
          rawHeaders.push(text);
        }
        const effectiveHeaders = dedupeColumns(rawHeaders);
        const rows: Array<Record<string, string>> = [];
        let totalRows = 0;
        for (let rowNo = 2; rowNo <= sheet.rowCount; rowNo += 1) {
          const row = sheet.getRow(rowNo);
          const bucket: Record<string, string> = {};
          let hasAnyValue = false;
          for (let colNo = 1; colNo <= rawHeaders.length; colNo += 1) {
            const header = rawHeaders[colNo - 1];
            if (!header) continue;
            const value = (row.getCell(colNo).text || readText(row.getCell(colNo).value)).replace(/\r\n/g, '\n').replace(/\r/g, '\n');
            bucket[header] = value;
            accumulateColumnStats(header, value);
            if (!isMissingCellToken(value)) hasAnyValue = true;
          }
          if (hasAnyValue) {
            totalRows += 1;
            if (rows.length < rowCap) rows.push(bucket);
          }
        }
        return {
          headers: effectiveHeaders,
          rows,
          totalRows,
          previewTruncated: totalRows > rows.length,
          columnNonEmptyCounts,
          columnNumericCounts,
          columnPositiveNumericCounts,
        };
      }
      const text = await file.text();
      const normalized = text.replace(/^\uFEFF/, '').replace(/\r\n/g, '\n').replace(/\r/g, '\n');
      const lines = normalized.split('\n');
      const headerIndex = lines.findIndex((line) => line.trim().length > 0);
      if (headerIndex < 0) {
        throw new Error('Uploaded file has no header row.');
      }
      const delimiter = detectDelimiter(lines[headerIndex], file.name);
      const rawHeaders = splitDelimitedLine(lines[headerIndex], delimiter).map((item) => item.trim());
      const effectiveHeaders = dedupeColumns(rawHeaders);
      const rows: Array<Record<string, string>> = [];
      let totalRows = 0;
      for (let idx = headerIndex + 1; idx < lines.length; idx += 1) {
        const line = lines[idx];
        if (!line.trim()) continue;
        const cells = splitDelimitedLine(line, delimiter);
        const bucket: Record<string, string> = {};
        let hasAnyValue = false;
        for (let colNo = 0; colNo < rawHeaders.length; colNo += 1) {
          const header = rawHeaders[colNo];
          if (!header) continue;
          const value = readText(cells[colNo] || '');
          bucket[header] = value;
          accumulateColumnStats(header, value);
          if (!isMissingCellToken(value)) hasAnyValue = true;
        }
        if (hasAnyValue) {
          totalRows += 1;
          if (rows.length < rowCap) rows.push(bucket);
        }
      }
      return {
        headers: effectiveHeaders,
        rows,
        totalRows,
        previewTruncated: totalRows > rows.length,
        columnNonEmptyCounts,
        columnNumericCounts,
        columnPositiveNumericCounts,
      };
    },
    []
  );

  const buildTsvFile = useCallback((sourceName: string, headers: string[], rows: Array<Record<string, string>>, suffix = ''): File => {
    const safeHeaders = headers.map((item) => item.trim()).filter(Boolean);
    const lineRows = [safeHeaders.map((item) => toTsvCell(item)).join('\t')];
    for (const row of rows) {
      const cells = safeHeaders.map((header) => toTsvCell(readText(row[header])));
      lineRows.push(cells.join('\t'));
    }
    const base = sourceName.replace(/\.[^.]+$/, '') || 'upload';
    const normalizedName = `${base}${suffix}.tsv`;
    return new File([lineRows.join('\n')], normalizedName, { type: 'text/tab-separated-values' });
  }, []);

  const applyUploadPreviewTable = useCallback((table: ParsedUploadTable) => {
    setUploadHeaderOptions(Array.isArray(table.headers) ? table.headers : []);
    setUploadPreviewRows(Array.isArray(table.rows) ? table.rows : []);
    setUploadPreviewTotalRows(Math.max(0, Number(table.totalRows || 0)));
    setUploadPreviewTruncated(Boolean(table.previewTruncated));
    setUploadPreviewColumnNonEmptyCounts(
      table.columnNonEmptyCounts && typeof table.columnNonEmptyCounts === 'object' ? table.columnNonEmptyCounts : {}
    );
    setUploadPreviewColumnNumericCounts(
      table.columnNumericCounts && typeof table.columnNumericCounts === 'object' ? table.columnNumericCounts : {}
    );
    setUploadPreviewColumnPositiveCounts(
      table.columnPositiveNumericCounts && typeof table.columnPositiveNumericCounts === 'object'
        ? table.columnPositiveNumericCounts
        : {}
    );
    setUploadPreviewPage(1);
  }, []);

  useEffect(() => {
    const batchId = readText(selectedBatch?.id);
    const compoundsPath = readText(compoundFileMeta.path);
    const compoundsPathRel = readText(compoundFileMeta.path_rel);
    const compoundsStoredName = readText(compoundFileMeta.stored_name);
    const compoundsOriginalName = readText(compoundFileMeta.original_name);
    const compoundsUploadedAt = readText(compoundFileMeta.uploaded_at);
    const hasSavedMeta = Boolean(compoundsPath || compoundsPathRel || compoundsStoredName || compoundsOriginalName || compoundsUploadedAt);
    if (!batchId || !hasSavedMeta || uploadFile) return;
    const signature = `${batchId}:${compoundsPath}:${compoundsPathRel}:${compoundsStoredName}:${compoundsOriginalName}:${compoundsUploadedAt}`;
    if (savedCompoundsPreviewSignatureRef.current === signature) return;
    savedCompoundsPreviewSignatureRef.current = signature;
    let cancelled = false;
    setUploadParsing(true);
    void (async () => {
      try {
        const preview = await fetchMmpLifecycleCompoundsPreview(batchId, { max_rows: UPLOAD_PREVIEW_ROW_CAP });
        if (cancelled) return;
        applyUploadPreviewTable({
          headers: Array.isArray(preview.headers) ? preview.headers : [],
          rows: Array.isArray(preview.rows) ? preview.rows : [],
          totalRows: Math.max(0, Number(preview.total_rows || 0)),
          previewTruncated: Boolean(preview.preview_truncated),
          columnNonEmptyCounts: preview.column_non_empty_counts || {},
          columnNumericCounts: preview.column_numeric_counts || {},
          columnPositiveNumericCounts: preview.column_positive_numeric_counts || {},
        });
      } catch (err) {
        if (cancelled) return;
        savedCompoundsPreviewSignatureRef.current = '';
        setError(err instanceof Error ? err.message : 'Failed to load saved file preview.');
      } finally {
        setUploadParsing(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    selectedBatchId,
    compoundFileMeta.path,
    compoundFileMeta.path_rel,
    compoundFileMeta.stored_name,
    compoundFileMeta.original_name,
    compoundFileMeta.uploaded_at,
    uploadFile,
    applyUploadPreviewTable,
  ]);

  const addActivityPair = () => {
    const selected = new Set(activityColumns.map((item) => readText(item)));
    const structureToken = readText(structureColumn);
    const candidate = uploadColumnOptions.find((item) => {
      const token = readText(item);
      if (!token) return false;
      if (selected.has(token)) return false;
      if (token === structureToken) return false;
      return true;
    });
    if (!candidate) return;
    setActivityColumns((prev) => dedupeColumns([...prev, candidate]));
    setActivityMethodMap((prev) => ({ ...prev, [candidate]: readText(prev[candidate]) }));
    setActivityTransformMap((prev) => ({
      ...prev,
      [candidate]: normalizeActivityTransform(prev[candidate] || 'none'),
    }));
  };

  const removeActivityPair = (targetColumn: string) => {
    const target = readText(targetColumn);
    if (!target) return;
    setActivityColumns((prev) => prev.filter((item) => readText(item) !== target));
    setActivityMethodMap((prev) => {
      const next = { ...prev };
      delete next[target];
      return next;
    });
    setActivityTransformMap((prev) => {
      const next = { ...prev };
      delete next[target];
      return next;
    });
  };

  const updateActivityPairColumn = (oldColumn: string, nextColumnRaw: string) => {
    const oldCol = readText(oldColumn);
    const nextCol = readText(nextColumnRaw);
    if (!oldCol || !nextCol || oldCol === nextCol) return;
    setActivityColumns((prev) => {
      const next = prev.map((item) => (readText(item) === oldCol ? nextCol : item));
      return dedupeColumns(next);
    });
    setActivityMethodMap((prev) => {
      const next: Record<string, string> = { ...prev };
      const oldMethod = readText(next[oldCol]);
      const existingMethod = readText(next[nextCol]);
      delete next[oldCol];
      if (existingMethod) next[nextCol] = existingMethod;
      else if (oldMethod) next[nextCol] = oldMethod;
      return next;
    });
    setActivityTransformMap((prev) => {
      const next: Record<string, ActivityTransform> = { ...prev };
      const oldTransform = normalizeActivityTransform(next[oldCol] || 'none');
      const existingTransform = next[nextCol] ? normalizeActivityTransform(next[nextCol]) : null;
      delete next[oldCol];
      next[nextCol] = existingTransform || oldTransform;
      return next;
    });
  };

  const executeUnifiedUpload = useCallback(
    async (
      file: File,
      structureColRaw: string,
      activityColumnsRaw: string[],
      transformMapRaw: Record<string, ActivityTransform>,
    ) => {
      if (!selectedBatch) {
        throw new Error('Select one lifecycle batch first.');
      }
      const editableBatch = await ensureEditableBatchForMutation();
      if (!editableBatch) {
        throw new Error('No editable batch available.');
      }
      const structureCol = readText(structureColRaw);
      if (!structureCol) {
        throw new Error('Select structure column first.');
      }
      const selectedActivityColumns = Array.from(new Set(activityColumnsRaw.map((item) => readText(item)).filter(Boolean)));
      const hadExistingExperiments = Boolean(asRecord(asRecord(editableBatch.files).experiments).path);
      setSaving(true);
      setError(null);
      try {
        const parsed = await parseUploadTable(file);
        if (!parsed.headers.includes(structureCol)) {
          throw new Error(`Selected structure column "${structureCol}" is not in uploaded file.`);
        }
        for (const activityColumn of selectedActivityColumns) {
          if (!parsed.headers.includes(activityColumn)) {
            throw new Error(`Selected activity column "${activityColumn}" is not in uploaded file.`);
          }
        }

        const compoundsFile = buildTsvFile(file.name, parsed.headers, parsed.rows, '_compounds');
        const batchId = readText(editableBatch.id);
        if (!batchId) {
          throw new Error('Selected lifecycle batch is missing id.');
        }
        const uploadedCompounds = await uploadMmpLifecycleCompounds(batchId, {
          file: compoundsFile,
          smiles_column: structureCol,
        });
        mergeBatch(uploadedCompounds);

        if (selectedActivityColumns.length > 0) {
          const transformByActivity: Record<string, ActivityTransform> = {};
          const methodByActivity: Record<string, string> = {};
          const outputPropertyByActivity: Record<string, string> = {};
          const methodOutputById = new Map<string, string>();
          for (const item of methods) {
            const methodId = readText(item.id);
            if (!methodId) continue;
            methodOutputById.set(methodId, readText(item.output_property));
          }
          for (const activityColumn of selectedActivityColumns) {
            transformByActivity[activityColumn] = normalizeActivityTransform(transformMapRaw[activityColumn] || 'none');
            const methodId = readText(activityMethodMap[activityColumn]);
            if (methodId) {
              methodByActivity[activityColumn] = methodId;
            }
            outputPropertyByActivity[activityColumn] = readText(methodOutputById.get(methodId) || activityColumn);
          }
          const effectiveActivityColumns = selectedActivityColumns.slice();
          const experimentRows: Array<Record<string, string>> = [];
          for (const sourceRow of parsed.rows) {
            const smiles = readText(sourceRow[structureCol]);
            for (const activityColumn of effectiveActivityColumns) {
              const value = readText(sourceRow[activityColumn]);
              if (isMissingCellToken(value)) continue;
              const transform = transformByActivity[activityColumn];
              let transformedValue = value;
              if (transform !== 'none') {
                try {
                  const output = transformActivityValue(value, transform);
                  transformedValue = Number.isFinite(output) ? String(output) : '';
                } catch {
                  continue;
                }
              }
              experimentRows.push({
                smiles,
                source_property: activityColumn,
                value: transformedValue,
              });
            }
          }
          if (experimentRows.length > 0) {
            const experimentsFile = buildTsvFile(
              file.name,
              ['smiles', 'source_property', 'value'],
              experimentRows,
              '_experiments'
            );
            const uploadedExperiments = await uploadMmpLifecycleExperiments(batchId, {
              file: experimentsFile,
              smiles_column: 'smiles',
              property_column: 'source_property',
              value_column: 'value',
              activity_columns: effectiveActivityColumns,
              activity_method_map: effectiveActivityColumns.reduce<Record<string, string>>((acc, col) => {
                const methodId = readText(methodByActivity[col]);
                if (methodId) acc[col] = methodId;
                return acc;
              }, {}),
              activity_transform_map: effectiveActivityColumns.reduce<Record<string, string>>((acc, col) => {
                acc[col] = transformByActivity[col];
                return acc;
              }, {}),
              activity_output_property_map: effectiveActivityColumns.reduce<Record<string, string>>((acc, col) => {
                acc[col] = readText(outputPropertyByActivity[col] || col);
                return acc;
              }, {}),
            });
            mergeBatch(uploadedExperiments);
          } else if (hadExistingExperiments) {
            const cleared = await clearMmpLifecycleExperiments(batchId);
            mergeBatch(cleared);
          }
        } else if (hadExistingExperiments) {
          const cleared = await clearMmpLifecycleExperiments(batchId);
          mergeBatch(cleared);
        }
      } finally {
        setSaving(false);
      }
    },
    [selectedBatch, ensureEditableBatchForMutation, parseUploadTable, buildTsvFile, mergeBatch, activityMethodMap, methods]
  );

  const onUploadFileChange = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0] || null;
    setUploadFile(file);
    setUploadHeaderOptions([]);
    setUploadPreviewRows([]);
    setUploadPreviewTotalRows(0);
    setUploadPreviewTruncated(false);
    setUploadPreviewPage(1);
    setUploadPreviewColumnNonEmptyCounts({});
    setUploadPreviewColumnNumericCounts({});
    setUploadPreviewColumnPositiveCounts({});
    setError(null);
    autoUploadLastSignatureRef.current = '';
    savedCompoundsPreviewSignatureRef.current = '';
    if (!file) {
      setAutoUploadStatus('idle');
      setAutoUploadStatusText('');
      setAutoUploadTrigger(0);
      return;
    }
    setAutoUploadStatus('pending');
    setAutoUploadStatusText('Analyzing file...');
    setUploadParsing(true);
    try {
      const table = await parseUploadTable(file, { maxRows: UPLOAD_PREVIEW_ROW_CAP });
      const headers = table.headers;
      applyUploadPreviewTable(table);

      const nextStructure = pickColumnCandidate(headers, structureColumn, ['smiles', 'canonical_smiles', 'mol_smiles']);

      const preservedActivityColumns = activityColumns.filter((item) => headers.includes(item));
      const reserved = new Set([nextStructure].map((item) => item.toLowerCase()));
      const guessHints = ['ic50', 'ki', 'kd', 'ec50', 'ac50', 'pic50', 'activity', 'potency', 'inhibition', 'affinity'];
      const guessedActivities = headers.filter((header) => {
        const lower = header.toLowerCase();
        if (!lower || reserved.has(lower)) return false;
        return guessHints.some((token) => lower.includes(token));
      });

      setStructureColumn(nextStructure);
      const nextActivities = preservedActivityColumns.length > 0
        ? preservedActivityColumns
        : guessedActivities.slice(0, 8);
      setActivityColumns(nextActivities);
      setActivityMethodMap((prev) => {
        const next: Record<string, string> = {};
        for (const col of nextActivities) {
          const current = readText(prev[col]);
          if (current) next[col] = current;
        }
        return next;
      });
      setActivityTransformMap((prev) => {
        const next: Record<string, ActivityTransform> = {};
        for (const col of nextActivities) {
          next[col] = normalizeActivityTransform(prev[col] || 'none');
        }
        return next;
      });
      setAutoUploadStatus('pending');
      setAutoUploadStatusText('File parsed. Uploading automatically...');
      setAutoUploadTrigger((prev) => prev + 1);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to read file headers.';
      setError(message);
      setAutoUploadStatus('error');
      setAutoUploadStatusText(message);
    } finally {
      setUploadParsing(false);
    }
  };

  const resetUploadDraft = useCallback(() => {
    if (!selectedBatch) return;
    const batchConfig = readBatchActivityConfig(selectedBatch);
    setError(null);
    setUploadFile(null);
    setUploadHeaderOptions([]);
    setUploadPreviewRows([]);
    setUploadPreviewTotalRows(0);
    setUploadPreviewTruncated(false);
    setUploadPreviewPage(1);
    setUploadPreviewColumnNonEmptyCounts({});
    setUploadPreviewColumnNumericCounts({});
    setUploadPreviewColumnPositiveCounts({});
    setStructureColumn(batchConfig.structureColumn);
    setActivityColumns(batchConfig.activityColumns);
    setActivityMethodMap(batchConfig.activityMethodMap);
    setActivityTransformMap(batchConfig.activityTransformMap);
    setAutoUploadTrigger(0);
    setAutoUploadStatus('idle');
    setAutoUploadStatusText('');
    autoUploadLastSignatureRef.current = '';
    savedCompoundsPreviewSignatureRef.current = '';
  }, [selectedBatch]);

  const resetCheckDraft = useCallback(() => {
    setError(null);
    setCheckResult(null);
  }, []);

  const resetMethodEditorDraft = useCallback(() => {
    setEditingMethodId('');
    setMethodKey('');
    setMethodName('');
    setMethodOutputProperty('');
    setMethodInputUnit('');
    setMethodDisplayTransform('none');
    setMethodCategory(ASSAY_CATEGORY_OPTIONS[0]);
    setMethodDescription('');
    setMethodFormErrors({ key: false, name: false, output_property: false });
    setMethodFormMessage(null);
  }, []);

  const createMethod = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextErrors: MethodFormErrors = {
      key: !methodKey.trim(),
      name: !methodName.trim(),
      output_property: !methodOutputProperty.trim(),
    };
    if (nextErrors.key || nextErrors.name || nextErrors.output_property) {
      setMethodFormErrors(nextErrors);
      setMethodFormMessage(null);
      return;
    }
    setMethodFormErrors({ key: false, name: false, output_property: false });
    setMethodFormMessage(null);
    const selectedDbId = readText(assayMethodDatabaseId);
    if (selectedDbId) {
      const dbMeta = databaseById.get(selectedDbId);
      if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
        setMethodFormMessage('Target database is still building. Please wait before updating methods.');
        return;
      }
      if (isDatabaseBusy(selectedDbId)) {
        setMethodFormMessage('Target database is busy with another update. Try again after current task finishes.');
        return;
      }
    }
    setSaving(true);
    try {
      const normalizedDisplayTransform = normalizeActivityTransform(methodDisplayTransform || 'none');
      const resolvedInputUnit = readText(methodInputUnit);
      const resolvedOutputUnit = resolvedInputUnit;
      const payload = {
        key: methodKey.trim(),
        name: methodName.trim(),
        output_property: methodOutputProperty.trim(),
        database_id: readText(assayMethodDatabaseId),
        input_unit: resolvedInputUnit,
        output_unit: resolvedOutputUnit,
        display_unit: inferDisplayUnit(normalizedDisplayTransform, resolvedOutputUnit, resolvedInputUnit),
        import_transform: 'none',
        display_transform: normalizedDisplayTransform,
        category: methodCategory.trim(),
        description: methodDescription.trim(),
      };
      await (editingMethodId
        ? patchMmpLifecycleMethod(editingMethodId, payload)
        : createMmpLifecycleMethod(payload));
      await loadOverview();
      await loadAssayDatabaseMethodBindings(assayMethodDatabaseId);
      await loadAssayDatabaseProperties(assayMethodDatabaseId);
      await loadAssayMethodUsage(assayMethodDatabaseId);
      await loadPendingSyncSummary(assayMethodDatabaseId);
      resetMethodEditorDraft();
      setMethodEditorOpen(false);
    } catch (err) {
      setMethodFormMessage(err instanceof Error ? err.message : 'Failed to save experiment method.');
    } finally {
      setSaving(false);
    }
  };

  const removeMethod = async (methodId: string) => {
    if (!methodId) return;
    const selectedDbId = readText(assayMethodDatabaseId);
    if (!selectedDbId) {
      setError('Select a target database in Assay Methods before deleting.');
      return;
    }
    const selectedDbMeta = databaseById.get(selectedDbId);
    if (!selectedDbMeta || getDatabaseBuildState(selectedDbMeta) !== 'ready') {
      setError('Target database is still building. Please wait before deleting methods.');
      return;
    }
    if (isDatabaseBusy(selectedDbId)) {
      setError('Target database is busy with another update. Try again after current task finishes.');
      return;
    }
    const method = methods.find((item) => readText(item.id) === methodId);
    const outputProperty = readText(method?.output_property);
    const displayName = readText(method?.key || method?.name || methodId);
    if (
      !window.confirm(
        `Delete method "${displayName}" from selected database and mark "${outputProperty || 'property'}" for purge on next database apply?`
      )
    ) {
      return;
    }
    const confirmToken = window.prompt(`Type "${outputProperty}" to confirm destructive delete:`) || '';
    if (outputProperty && confirmToken !== outputProperty) {
      setError('Delete canceled: confirmation text does not match output property.');
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const result = await deleteMmpLifecycleMethod(methodId, {
        database_id: selectedDbId,
        purge_database_data: true,
        confirm_output_property: confirmToken,
      });
      const deletedMethod = Boolean((result as Record<string, unknown>).deleted_method);
      if (deletedMethod) {
        setMethods((prev) => prev.filter((item) => readText(item.id) !== methodId));
      }
      void loadAssayDatabaseMethodBindings(selectedDbId);
      void loadAssayDatabaseProperties(selectedDbId);
      void loadAssayMethodUsage(selectedDbId);
      void loadPendingSyncSummary(selectedDbId);
      setMappingRows((prev) =>
        prev.map((row) => (row.method_id === methodId ? { ...row, method_id: '', value_transform: 'none' } : row))
      );
      if (editingMethodId === methodId) {
        resetMethodEditorDraft();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete experiment method.');
    } finally {
      setSaving(false);
    }
  };

  const startEditMethod = (method: MmpLifecycleMethod) => {
    const methodId = readText(method.id);
    const dbDisplayTransform = normalizeActivityTransform(
      readText(method.display_transform || assayMethodDisplayTransformById[methodId] || 'none')
    );
    setEditingMethodId(methodId);
    setMethodKey(readText(method.key));
    setMethodName(readText(method.name));
    setMethodOutputProperty(readText(method.output_property));
    setMethodInputUnit(readText(method.input_unit) || readText(method.output_unit));
    setMethodDisplayTransform(dbDisplayTransform);
    setMethodCategory(readText(method.category) || ASSAY_CATEGORY_OPTIONS[0]);
    setMethodDescription(readText(method.description));
    setMethodFormErrors({ key: false, name: false, output_property: false });
    setMethodFormMessage(null);
    setMethodEditorOpen(true);
  };

  const openCreateMethodEditor = () => {
    resetMethodEditorDraft();
    setMethodEditorOpen(true);
  };

  useEffect(() => {
    const dbId = batchDatabaseId.trim();
    if (!dbId) {
      setMappingRows([]);
      return;
    }
    const normalizedMethods = dedupeLifecycleMethods(methods);
    let cancelled = false;

    const loadMappingContext = async () => {
      try {
        const mappings = await fetchMmpLifecyclePropertyMappings(dbId);
        if (cancelled) return;
        const rows: MappingDraft[] = mappings.map((item) => ({
          id: readText(item.id),
          source_property: readText(item.source_property),
          mmp_property: readText(item.mmp_property),
          method_id: readText(item.method_id),
          value_transform: normalizeActivityTransform(readText(item.value_transform || 'none')),
          notes: readText(item.notes),
        }));
        const normalized = normalizeMappingRowsWithMethods(rows, normalizedMethods);
        setMappingRows(dedupeMappingRows(normalized));
        const manualPairSources = dedupeColumns(
          normalized
            .filter((row) => readText(row.notes).toLowerCase() === 'activity pair mapping.')
            .map((row) => readText(row.source_property))
            .filter(Boolean)
        );
        if (manualPairSources.length > 0) {
          setActivityColumns((prev) => {
            const next = dedupeColumns([...prev, ...manualPairSources]);
            const unchanged = prev.length === next.length && prev.every((item, index) => item === next[index]);
            return unchanged ? prev : next;
          });
        }
        const methodBySource = new Map<string, string>();
        for (const row of normalized) {
          const source = readText(row.source_property).toLowerCase();
          const methodId = readText(row.method_id);
          if (!source || !methodId || methodBySource.has(source)) continue;
          methodBySource.set(source, methodId);
        }
        if (methodBySource.size > 0) {
          setActivityMethodMap((prev) => {
            const next: Record<string, string> = { ...prev };
            let changed = false;
            const targetColumns = dedupeColumns([...activityColumns, ...manualPairSources]);
            for (const col of targetColumns) {
              const token = readText(col);
              if (!token) continue;
              if (readText(next[token])) continue;
              const mapped = readText(methodBySource.get(token.toLowerCase()) || '');
              if (!mapped) continue;
              next[token] = mapped;
              changed = true;
            }
            return changed ? next : prev;
          });
        }
      } catch (err) {
        if (cancelled) return;
        setError(err instanceof Error ? err.message : 'Failed to load property mapping context.');
      }
    };

    void loadMappingContext();
    return () => {
      cancelled = true;
    };
  }, [batchDatabaseId, methods]);

  useEffect(() => {
    const normalizedMethods = dedupeLifecycleMethods(methods);
    if (normalizedMethods.length === 0) return;
    setMappingRows((prev) => {
      const normalized = normalizeMappingRowsWithMethods(prev, normalizedMethods);
      const next = dedupeMappingRows(normalized);
      const unchanged =
        prev.length === next.length &&
        prev.every((item, idx) => {
          const rhs = next[idx];
          return (
            item.id === rhs.id &&
            item.source_property === rhs.source_property &&
            item.mmp_property === rhs.mmp_property &&
            item.method_id === rhs.method_id &&
            item.value_transform === rhs.value_transform &&
            item.notes === rhs.notes
          );
        });
      return unchanged ? prev : next;
    });
  }, [methods]);

  const saveMappings = async (options?: { silent?: boolean }): Promise<boolean> => {
    const silent = options?.silent === true;
    const editableBatch = await ensureEditableBatchForMutation();
    if (!editableBatch) return false;
    const dbId = batchDatabaseId.trim();
    if (!dbId) {
      if (!silent) setError('Choose target MMP database before saving mappings.');
      return false;
    }
    const dbMeta = databaseById.get(dbId);
    if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
      if (!silent) setError('Target database is still building. Please wait before saving mappings.');
      return false;
    }
    if (isDatabaseBusy(dbId)) {
      if (!silent) setError('Target database is busy with another update. Try again after current task finishes.');
      return false;
    }
    setSaving(true);
    if (!silent) setError(null);
    try {
      const preservedRows = dedupeMappingRows(normalizeMappingRowsWithMethods(mappingRows, methods));
      const selectedActivityColumns = dedupeColumns(activityColumns.map((item) => readText(item)).filter(Boolean));
      const mappedActivityColumns: string[] = [];
      const pairRows: MappingDraft[] = [];
      for (const sourceProperty of selectedActivityColumns) {
        const methodId = readText(activityMethodMap[sourceProperty]);
        const mmpProperty = readText(methodOutputPropertyById.get(methodId) || '');
        if (!methodId || !mmpProperty) {
          continue;
        }
        mappedActivityColumns.push(sourceProperty);
        const existing = preservedRows.find((row) => readText(row.source_property).toLowerCase() === sourceProperty.toLowerCase());
        pairRows.push({
          id: readText(existing?.id),
          source_property: sourceProperty,
          mmp_property: mmpProperty,
          method_id: methodId,
          value_transform: 'none',
          notes: 'Activity pair mapping.',
        });
      }
      const selectedSourceSet = new Set(mappedActivityColumns.map((item) => item.toLowerCase()));
      const preservedRowsAfterDelete = preservedRows.filter((row) => {
        const note = readText(row.notes).toLowerCase();
        if (note === 'activity pair mapping.') {
          return selectedSourceSet.has(readText(row.source_property).toLowerCase());
        }
        return true;
      });
      const pairSourceSet = new Set(pairRows.map((row) => readText(row.source_property).toLowerCase()));
      const mergedRows = dedupeMappingRows([
        ...preservedRowsAfterDelete.filter((row) => !pairSourceSet.has(readText(row.source_property).toLowerCase())),
        ...pairRows,
      ]);

      const payload = mergedRows
        .map((item) => ({
          id: item.id,
          source_property: item.source_property.trim(),
          mmp_property: item.mmp_property.trim(),
          method_id: item.method_id.trim(),
          value_transform: 'none' as ActivityTransform,
          notes: item.notes.trim(),
        }))
        .filter((item) => item.source_property && item.method_id && item.mmp_property);
      const saved = await saveMmpLifecyclePropertyMappings(dbId, payload);
      setMappingRows(
        dedupeMappingRows(
          normalizeMappingRowsWithMethods(
          saved.map((item) => ({
            id: readText(item.id),
            source_property: readText(item.source_property),
            mmp_property: readText(item.mmp_property),
            method_id: readText(item.method_id),
            value_transform: normalizeActivityTransform(readText(item.value_transform || 'none')),
            notes: readText(item.notes),
          })),
          methods
          )
        )
      );
      if (mappedActivityColumns.length > 0) {
        const batchId = readText(editableBatch.id);
        if (batchId) {
          const materialized = await materializeMmpLifecycleExperimentsFromCompounds(batchId, {
            smiles_column: readText(structureColumn),
            activity_columns: mappedActivityColumns,
            activity_method_map: mappedActivityColumns.reduce<Record<string, string>>((acc, col) => {
              const methodId = readText(activityMethodMap[col]);
              if (methodId) acc[col] = methodId;
              return acc;
            }, {}),
            activity_transform_map: mappedActivityColumns.reduce<Record<string, string>>((acc, col) => {
              const transform = normalizeActivityTransform(activityTransformMap[col] || 'none');
              if (transform !== 'none') acc[col] = transform;
              return acc;
            }, {}),
            activity_output_property_map: mappedActivityColumns.reduce<Record<string, string>>((acc, col) => {
              acc[col] = readText(methodOutputPropertyById.get(readText(activityMethodMap[col])) || col);
              return acc;
            }, {}),
          });
          mergeBatch(materialized);
        }
      }
      return true;
    } catch (err) {
      if (!silent) {
        setError(err instanceof Error ? err.message : 'Failed to save property mappings.');
      }
      return false;
    } finally {
      setSaving(false);
    }
  };

  const resetMappingDraft = async () => {
    if (!selectedBatch) return;
    const dbId = readText(batchDatabaseId || selectedBatch.selected_database_id);
    const batchConfig = readBatchActivityConfig(selectedBatch);
    setError(null);
    setSaving(true);
    try {
      setStructureColumn(batchConfig.structureColumn);
      if (!dbId) {
        setActivityColumns(batchConfig.activityColumns);
        setActivityMethodMap(batchConfig.activityMethodMap);
        setActivityTransformMap(batchConfig.activityTransformMap);
        setMappingRows([]);
        return;
      }
      const mappings = await fetchMmpLifecyclePropertyMappings(dbId);
      const normalizedMethods = dedupeLifecycleMethods(methods);
      const rows: MappingDraft[] = mappings.map((item) => ({
        id: readText(item.id),
        source_property: readText(item.source_property),
        mmp_property: readText(item.mmp_property),
        method_id: readText(item.method_id),
        value_transform: normalizeActivityTransform(readText(item.value_transform || 'none')),
        notes: readText(item.notes),
      }));
      const normalizedRows = normalizeMappingRowsWithMethods(rows, normalizedMethods);
      const dedupedRows = dedupeMappingRows(normalizedRows);
      const manualPairSources = dedupeColumns(
        normalizedRows
          .filter((row) => readText(row.notes).toLowerCase() === 'activity pair mapping.')
          .map((row) => readText(row.source_property))
          .filter(Boolean)
      );
      const nextActivityColumns = dedupeColumns([...batchConfig.activityColumns, ...manualPairSources]);
      const nextMethodMap: Record<string, string> = { ...batchConfig.activityMethodMap };
      const methodBySource = new Map<string, string>();
      for (const row of normalizedRows) {
        const source = readText(row.source_property).toLowerCase();
        const methodId = readText(row.method_id);
        if (!source || !methodId || methodBySource.has(source)) continue;
        methodBySource.set(source, methodId);
      }
      for (const col of nextActivityColumns) {
        const token = readText(col);
        if (!token || readText(nextMethodMap[token])) continue;
        const mapped = readText(methodBySource.get(token.toLowerCase()) || '');
        if (!mapped) continue;
        nextMethodMap[token] = mapped;
      }

      setActivityColumns(nextActivityColumns);
      setActivityMethodMap(nextMethodMap);
      setActivityTransformMap(batchConfig.activityTransformMap);
      setMappingRows(dedupedRows);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to reset property mappings.');
    } finally {
      setSaving(false);
    }
  };

  const prepareBatchForCheckApply = useCallback(async (dbId: string): Promise<MmpLifecycleBatch | null> => {
    if (!selectedBatch) return null;
    let workingBatch = selectedBatch;
    const methodOutputById = new Map<string, string>();
    for (const method of methods) {
      const methodId = readText(method.id);
      if (!methodId) continue;
      methodOutputById.set(methodId, readText(method.output_property));
    }
    const desiredSmilesColumn = readText(structureColumn);
    const runMappedActivityColumns = dedupeColumns(activityColumns.map((item) => readText(item)).filter(Boolean))
      .filter((sourceProperty) => {
        const methodId = readText(activityMethodMap[sourceProperty]);
        const mmpProperty = readText(methodOutputById.get(methodId) || '');
        return Boolean(methodId && mmpProperty);
      });
    const desiredMethodMap = runMappedActivityColumns.reduce<Record<string, string>>((acc, col) => {
      const methodId = readText(activityMethodMap[col]);
      if (methodId) acc[col] = methodId;
      return acc;
    }, {});
    const desiredTransformMap = runMappedActivityColumns.reduce<Record<string, string>>((acc, col) => {
      const transform = normalizeActivityTransform(activityTransformMap[col] || 'none');
      if (transform !== 'none') acc[col] = transform;
      return acc;
    }, {});
    const desiredOutputMap = runMappedActivityColumns.reduce<Record<string, string>>((acc, col) => {
      acc[col] = readText(methodOutputById.get(readText(activityMethodMap[col])) || col);
      return acc;
    }, {});
    const normalizeCols = (cols: string[]): string[] => {
      return dedupeColumns(cols.map((item) => readText(item)).filter(Boolean)).sort((lhs, rhs) => lhs.localeCompare(rhs));
    };
    const normalizeConfigMap = (obj: Record<string, string>, keys: string[]): Record<string, string> => {
      const out: Record<string, string> = {};
      for (const key of normalizeCols(keys)) {
        const value = readText(obj[key]);
        if (value) out[key] = value;
      }
      return out;
    };
    const experimentsCfg = asRecord(asRecord(asRecord(workingBatch.files).experiments).column_config);
    const existingCols = normalizeCols(parseConfiguredColumns(experimentsCfg.activity_columns));
    const existingMethodRaw = asRecord(experimentsCfg.activity_method_map);
    const existingTransformRaw = asRecord(experimentsCfg.activity_transform_map);
    const existingOutputRaw = asRecord(experimentsCfg.activity_output_property_map);
    const existingMethodMap = normalizeConfigMap(
      Object.fromEntries(existingCols.map((col) => [col, readText(existingMethodRaw[col])])),
      existingCols
    );
    const existingTransformMap = normalizeConfigMap(
      Object.fromEntries(
        existingCols
          .map((col) => [col, normalizeActivityTransform(readText(existingTransformRaw[col] || 'none'))])
          .filter(([, value]) => value !== 'none')
      ) as Record<string, string>,
      existingCols
    );
    const existingOutputMap = normalizeConfigMap(
      Object.fromEntries(existingCols.map((col) => [col, readText(existingOutputRaw[col] || col)])),
      existingCols
    );
    const desiredCols = normalizeCols(runMappedActivityColumns);
    const desiredMethodMapNormalized = normalizeConfigMap(desiredMethodMap, desiredCols);
    const desiredTransformMapNormalized = normalizeConfigMap(desiredTransformMap, desiredCols);
    const desiredOutputMapNormalized = normalizeConfigMap(desiredOutputMap, desiredCols);
    const needsMaterialize = (
      desiredCols.length > 0
      && (
        readText(experimentsCfg.smiles_column) !== desiredSmilesColumn
        || JSON.stringify(existingCols) !== JSON.stringify(desiredCols)
        || JSON.stringify(existingMethodMap) !== JSON.stringify(desiredMethodMapNormalized)
        || JSON.stringify(existingTransformMap) !== JSON.stringify(desiredTransformMapNormalized)
        || JSON.stringify(existingOutputMap) !== JSON.stringify(desiredOutputMapNormalized)
      )
    );
    const pendingMetaChanges =
      readText(workingBatch.name) !== batchName.trim()
      || readText(workingBatch.description) !== batchDescription.trim()
      || readText(workingBatch.notes) !== batchNotes.trim()
      || readText(workingBatch.selected_database_id) !== dbId;
    if (pendingMetaChanges || needsMaterialize) {
      const editableBatch = await ensureEditableBatchForMutation();
      if (!editableBatch) return null;
      workingBatch = editableBatch;
    }
    const patchPayload: Record<string, unknown> = {};
    if (readText(workingBatch.name) !== batchName.trim()) patchPayload.name = batchName.trim();
    if (readText(workingBatch.description) !== batchDescription.trim()) patchPayload.description = batchDescription.trim();
    if (readText(workingBatch.notes) !== batchNotes.trim()) patchPayload.notes = batchNotes.trim();
    if (readText(workingBatch.selected_database_id) !== dbId) patchPayload.selected_database_id = dbId;
    if (Object.keys(patchPayload).length > 0) {
      const patched = await patchMmpLifecycleBatch(readText(workingBatch.id), patchPayload);
      mergeBatch(patched);
      workingBatch = patched;
    }
    if (needsMaterialize && runMappedActivityColumns.length > 0) {
      const materialized = await materializeMmpLifecycleExperimentsFromCompounds(readText(workingBatch.id), {
        smiles_column: desiredSmilesColumn,
        activity_columns: runMappedActivityColumns,
        activity_method_map: desiredMethodMap,
        activity_transform_map: desiredTransformMap,
        activity_output_property_map: desiredOutputMap,
      });
      mergeBatch(materialized);
      workingBatch = materialized;
    }
    return workingBatch;
  }, [
    selectedBatch,
    ensureEditableBatchForMutation,
    batchName,
    batchDescription,
    batchNotes,
    activityColumns,
    activityMethodMap,
    activityTransformMap,
    methods,
    structureColumn,
    mergeBatch,
  ]);

  const runCheckOnly = async () => {
    if (!selectedBatch) return;
    const dbId = batchDatabaseId.trim();
    if (!dbId) {
      setError('Choose target MMP database before check.');
      return;
    }
    const dbMeta = databaseById.get(dbId);
    if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
      setError('Target database is still building. Please wait before check.');
      return;
    }
    const keepQaStep = flowStep === 'qa';
    if (keepQaStep) {
      keepQaStepOnBatchSwitchRef.current = true;
    }
    setSaving(true);
    setQaAction('check');
    setError(null);
    try {
      const workingBatch = await prepareBatchForCheckApply(dbId);
      if (!workingBatch) return;
      const checkPayload = await checkMmpLifecycleBatch(readText(workingBatch.id), {
        database_id: dbId,
        row_limit: 400,
        check_policy: { ...BULK_APPLY_RELAXED_POLICY } as Record<string, unknown>,
      });
      setCheckResult(checkPayload);
      const checkedBatch = asRecord(checkPayload.batch);
      if (Object.keys(checkedBatch).length > 0) {
        mergeBatch(checkedBatch as unknown as MmpLifecycleBatch);
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to run check.');
    } finally {
      if (keepQaStep) {
        setFlowStep('qa');
      }
      keepQaStepOnBatchSwitchRef.current = false;
      setQaAction('none');
      setSaving(false);
    }
  };

  const runCheckAndApply = async () => {
    if (!selectedBatch) return;
    const dbId = batchDatabaseId.trim();
    if (!dbId) {
      setError('Choose target MMP database before run.');
      return;
    }
    const dbMeta = databaseById.get(dbId);
    if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
      setError('Target database is still building. Please wait before run.');
      return;
    }
    if (isDatabaseBusy(dbId)) {
      setError('Target database is busy with another update. Try again after current task finishes.');
      return;
    }
    const keepQaStep = flowStep === 'qa';
    if (keepQaStep) {
      keepQaStepOnBatchSwitchRef.current = true;
    }
    setSaving(true);
    setQaAction('apply');
    setError(null);
    try {
      const workingBatch = await prepareBatchForCheckApply(dbId);
      if (!workingBatch) return;
      const checkPayload = await checkMmpLifecycleBatch(readText(workingBatch.id), {
        database_id: dbId,
        row_limit: 400,
        check_policy: { ...BULK_APPLY_RELAXED_POLICY } as Record<string, unknown>,
      });
      setCheckResult(checkPayload);
      const checkedBatch = asRecord(checkPayload.batch);
      if (Object.keys(checkedBatch).length > 0) {
        mergeBatch(checkedBatch as unknown as MmpLifecycleBatch);
      }
      const gate = asRecord(checkPayload.check_gate);
      const passed = gate.passed === true;
      if (!passed) {
        const reasons = asArray(gate.reasons).map((item) => readText(item)).filter(Boolean);
        setError(reasons.length > 0 ? reasons.join(' ') : 'Check gate blocked. Fix issues then run again.');
        return;
      }
      const applyPayload = await applyMmpLifecycleBatch(readText(workingBatch.id), {
        async: true,
        database_id: dbId,
        import_compounds: true,
        import_experiments: true,
        check_policy: { ...BULK_APPLY_RELAXED_POLICY } as Record<string, unknown>,
      });
      const appliedBatch = asRecord(applyPayload.batch);
      if (Object.keys(appliedBatch).length > 0) {
        mergeBatch(appliedBatch as unknown as MmpLifecycleBatch);
      }
      await loadOverview({ silent: true });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to run check + apply.');
    } finally {
      if (keepQaStep) {
        setFlowStep('qa');
      }
      keepQaStepOnBatchSwitchRef.current = false;
      setQaAction('none');
      setSaving(false);
    }
  };

  const runBulkApplyFromList = async () => {
    const targetIds = selectedBatchIds.filter(Boolean);
    if (targetIds.length === 0) {
      setError('Select at least one batch first.');
      return;
    }

    const batchById = new Map<string, MmpLifecycleBatch>();
    for (const batch of batches) {
      const id = readText(batch.id);
      if (!id) continue;
      batchById.set(id, batch);
    }
    const runnableIds = targetIds.filter((id) => batchById.has(id));
    if (runnableIds.length === 0) {
      setError('Selected batches are no longer available.');
      return;
    }

    setSaving(true);
    setBulkApplyRunning(true);
    setError(null);
    let failedCount = 0;
    let firstFailureMessage = '';
    const reservedDatabaseIds = new Set<string>();
    try {
      const pushFailure = (batch: MmpLifecycleBatch | undefined, batchId: string, message: string) => {
        failedCount += 1;
        if (!firstFailureMessage) firstFailureMessage = `${readText(batch?.name) || batchId}: ${message}`;
      };

      const submitOne = async (batchId: string) => {
        const batch = batchById.get(batchId);
        if (!batch) return;
        const databaseId = readText(batch.selected_database_id);
        if (!databaseId) {
          pushFailure(batch, batchId, 'Missing target database.');
          return;
        }
        if (reservedDatabaseIds.has(databaseId)) {
          pushFailure(batch, batchId, 'Target database is already queued in this apply run.');
          return;
        }
        const dbMeta = databaseById.get(databaseId);
        if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
          pushFailure(batch, batchId, 'Target database is still building.');
          return;
        }
        if (isDatabaseBusy(databaseId)) {
          pushFailure(batch, batchId, 'Target database is busy with another update.');
          return;
        }
        reservedDatabaseIds.add(databaseId);

        try {
          const applyPayload = await applyMmpLifecycleBatch(batchId, {
            async: true,
            database_id: databaseId,
            import_compounds: true,
            import_experiments: true,
            check_policy: { ...BULK_APPLY_RELAXED_POLICY } as Record<string, unknown>,
          });
          const appliedBatch = asRecord(applyPayload.batch);
          if (Object.keys(appliedBatch).length > 0) {
            mergeBatch(appliedBatch as unknown as MmpLifecycleBatch);
          }
        } catch (err) {
          const message = err instanceof Error ? err.message : 'Failed to apply batch.';
          pushFailure(batch, batchId, message);
        }
      };

      const queue = [...runnableIds];
      const maxConcurrency = 1;
      await Promise.all(
        Array.from({ length: maxConcurrency }, async () => {
          while (queue.length > 0) {
            const nextBatchId = queue.shift();
            if (!nextBatchId) break;
            await submitOne(nextBatchId);
          }
        })
      );

      if (failedCount > 0) {
        setError(`${failedCount}/${runnableIds.length} batch apply failed. ${firstFailureMessage}`);
      }
      await loadOverview();
      setSelectedBatchIds([]);
    } finally {
      setBulkApplyRunning(false);
      setSaving(false);
    }
  };

  const resetBatchListView = useCallback(() => {
    setError(null);
    setBatchQuery('');
    setBatchStatusFilter('all');
    setBatchDatabaseFilter('all');
    setBatchSortKey('updated_at');
    setBatchSortDirection('desc');
    setSelectedBatchIds([]);
  }, []);

  const createBatchFromHeader = async () => {
    const seed = `Batch ${new Date().toISOString().slice(0, 19).replace('T', ' ')}`;
    const typed = window.prompt('Batch name', seed);
    if (typed === null) return;
    const name = typed.trim() || seed;
    setSaving(true);
    setError(null);
    try {
      const batch = await createMmpLifecycleBatch({
        name,
        description: '',
        notes: '',
      });
      mergeBatch(batch);
      setViewMode('detail');
      setFlowStep('batch');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to create batch.');
    } finally {
      setSaving(false);
    }
  };

  const selectedBatchStatusToken = selectedBatch ? normalizeBatchStatusToken(selectedBatch.status) : 'draft';
  const selectedBatchStatusLabel = selectedBatchStatusToken;
  const selectedBatchApplyHistoryCount = useMemo(() => {
    if (!selectedBatch) return 0;
    const inlineCount = Array.isArray(selectedBatch.apply_history) ? selectedBatch.apply_history.length : 0;
    const summaryCount = asNonNegativeInt((selectedBatch as unknown as Record<string, unknown>).apply_history_count);
    return Math.max(inlineCount, summaryCount);
  }, [selectedBatch]);
  const batchLastCheck = asRecord(selectedBatch?.last_check);
  const runtimeCheckGate = (() => {
    const fromCheck = asRecord(asRecord(checkResult).check_gate);
    if (Object.keys(fromCheck).length > 0) return fromCheck;
    return asRecord(batchLastCheck.check_gate);
  })();
  const gatePass = runtimeCheckGate.passed === true;
  const gateReasons = asArray(runtimeCheckGate.reasons).map((item) => readText(item)).filter(Boolean);
  const runtimeCompoundSummary = (() => {
    const fromCheck = asRecord(asRecord(asRecord(checkResult).compound_check).summary);
    if (Object.keys(fromCheck).length > 0) return fromCheck;
    return asRecord(batchLastCheck.compound_summary);
  })();
  const runtimeExperimentSummary = (() => {
    const fromCheck = asRecord(asRecord(asRecord(checkResult).experiment_check).summary);
    if (Object.keys(fromCheck).length > 0) return fromCheck;
    return asRecord(batchLastCheck.experiment_summary);
  })();
  const runtimeCheckOverview = buildCheckOverview(runtimeCompoundSummary, runtimeExperimentSummary);
  const flowStepIndex = Math.max(0, FLOW_STEPS.findIndex((item) => item.key === flowStep));
  const uploadColumnOptions = useMemo(() => {
    const collected = new Set<string>(uploadHeaderOptions);
    [structureColumn, ...activityColumns]
      .map((item) => item.trim())
      .filter(Boolean)
      .forEach((item) => collected.add(item));
    return Array.from(collected);
  }, [uploadHeaderOptions, structureColumn, activityColumns]);
  const uploadPreviewColumns = useMemo(
    () => dedupeColumns(uploadHeaderOptions),
    [uploadHeaderOptions]
  );
  const uploadPreviewSmilesColumn = useMemo(
    () => pickColumnCandidate(uploadPreviewColumns, '', ['smiles', 'canonical_smiles', 'mol_smiles']),
    [uploadPreviewColumns]
  );
  const uploadPreviewTotalPages = useMemo(() => {
    if (uploadPreviewRows.length === 0) return 1;
    return Math.max(1, Math.ceil(uploadPreviewRows.length / UPLOAD_PREVIEW_PAGE_SIZE));
  }, [uploadPreviewRows.length]);
  const uploadPreviewCurrentPage = Math.min(uploadPreviewPage, uploadPreviewTotalPages);
  const uploadPreviewPageRows = useMemo(() => {
    const start = (uploadPreviewCurrentPage - 1) * UPLOAD_PREVIEW_PAGE_SIZE;
    return uploadPreviewRows.slice(start, start + UPLOAD_PREVIEW_PAGE_SIZE);
  }, [uploadPreviewRows, uploadPreviewCurrentPage]);
  const savedCompoundsFilePath = readText(compoundFileMeta.path);
  const savedCompoundsFilePathRel = readText(compoundFileMeta.path_rel);
  const savedCompoundsFileName = readText(compoundFileMeta.original_name || compoundFileMeta.stored_name);
  const activeCompoundsFileName = uploadFile ? uploadFile.name : (savedCompoundsFileName || 'No file');
  const hasSavedCompoundsFile = Boolean(savedCompoundsFilePath || savedCompoundsFilePathRel || savedCompoundsFileName);
  const hasRegisteredAssayMethod = methods.length > 0;
  const methodNameById = useMemo(() => {
    const map = new Map<string, string>();
    for (const method of methods) {
      const id = readText(method.id);
      if (!id) continue;
      map.set(id, readText(method.key || method.name || method.id) || id);
    }
    return map;
  }, [methods]);
  const methodOutputPropertyById = useMemo(() => {
    const map = new Map<string, string>();
    for (const method of methods) {
      const id = readText(method.id);
      if (!id) continue;
      map.set(id, readText(method.output_property));
    }
    return map;
  }, [methods]);
  const uploadPreviewStats = useMemo(() => {
    const totalRows = Math.max(0, uploadPreviewTotalRows);
    const structureCoverage = uploadPreviewSmilesColumn
      ? Number(uploadPreviewColumnNonEmptyCounts[uploadPreviewSmilesColumn] || 0)
      : 0;
    const perColumn = uploadPreviewColumns.map((col) => {
      const nonEmpty = Number(uploadPreviewColumnNonEmptyCounts[col] || 0);
      const numeric = Number(uploadPreviewColumnNumericCounts[col] || 0);
      const positive = Number(uploadPreviewColumnPositiveCounts[col] || 0);
      const coverage = totalRows > 0 ? nonEmpty / totalRows : 0;
      return {
        column: col,
        nonEmpty,
        numeric,
        positive,
        coverage,
      };
    });
    return {
      totalRows,
      structureCoverage,
      columnCount: uploadPreviewColumns.length,
      perColumn,
    };
  }, [
    uploadPreviewTotalRows,
    uploadPreviewSmilesColumn,
    uploadPreviewColumns,
    uploadPreviewColumnNonEmptyCounts,
    uploadPreviewColumnNumericCounts,
    uploadPreviewColumnPositiveCounts,
  ]);
  const autoPickStructureColumn = useCallback(() => {
    const next = pickColumnCandidate(
      uploadColumnOptions,
      readText(structureColumn),
      ['smiles', 'canonical_smiles', 'mol_smiles', 'structure', 'smi'],
      readText(uploadColumnOptions[0] || '')
    );
    if (next) setStructureColumn(next);
  }, [uploadColumnOptions, structureColumn]);
  const uploadConfigIssues = useMemo(() => {
    const issues: string[] = [];
    const structureToken = readText(structureColumn);
    if (!structureToken) {
      issues.push('Select structure column.');
      return issues;
    }
    if (uploadColumnOptions.length > 0 && !uploadColumnOptions.includes(structureToken)) {
      issues.push('Structure column is not in current file.');
      return issues;
    }
    for (const activityCol of activityColumns) {
      const token = readText(activityCol);
      if (!token) {
        issues.push('Activity pair has empty column.');
        continue;
      }
      if (!uploadColumnOptions.includes(token)) {
        issues.push(`Activity column "${token}" is not in current file.`);
        continue;
      }
      const transform = normalizeActivityTransform(activityTransformMap[token] || 'none');
      if (transform !== 'none') {
        const positive = Number(uploadPreviewColumnPositiveCounts[token] || 0);
        const numeric = Number(uploadPreviewColumnNumericCounts[token] || 0);
        if (numeric <= 0) {
          issues.push(`Activity column "${token}" has no numeric values for conversion.`);
          continue;
        }
        if (positive <= 0) {
          issues.push(`Activity column "${token}" has no positive values for conversion.`);
        }
      }
    }
    return issues;
  }, [structureColumn, uploadColumnOptions, activityColumns, activityTransformMap, uploadPreviewColumnNumericCounts, uploadPreviewColumnPositiveCounts]);
  const isUploadConfigComplete = uploadConfigIssues.length === 0;
  useEffect(() => {
    if (autoUploadTrigger <= 0 || !uploadFile || !selectedBatch) return;
    if (uploadParsing || saving) return;
    if (!isUploadConfigComplete) {
      setAutoUploadStatus('pending');
      setAutoUploadStatusText(uploadConfigIssues[0] || 'Adjust conversion settings to continue.');
      return;
    }

    const batchId = readText(selectedBatch.id);
    const structureToken = readText(structureColumn);
    const selectedActivityColumns = Array.from(new Set(activityColumns.map((item) => readText(item)).filter(Boolean)));
    const signature = buildUploadExecutionSignature(
      batchId,
      uploadFile,
      structureToken,
      selectedActivityColumns,
      activityTransformMap,
      activityMethodMap
    );
    if (autoUploadLastSignatureRef.current === signature) return;
    autoUploadLastSignatureRef.current = signature;

    setAutoUploadStatus('uploading');
    setAutoUploadStatusText('Uploading data...');
    void (async () => {
      try {
        await executeUnifiedUpload(uploadFile, structureToken, selectedActivityColumns, activityTransformMap);
        setAutoUploadStatus('success');
        setAutoUploadStatusText(selectedActivityColumns.length > 0 ? 'Compounds + experiments uploaded.' : 'Compounds uploaded.');
        setAutoUploadTrigger(0);
      } catch (err) {
        const message = err instanceof Error ? err.message : 'Failed to upload data file.';
        autoUploadLastSignatureRef.current = '';
        setError(message);
        setAutoUploadStatus('error');
        setAutoUploadStatusText(message);
      }
    })();
  }, [
    autoUploadTrigger,
    uploadFile,
    selectedBatch,
    uploadParsing,
    saving,
    isUploadConfigComplete,
    uploadConfigIssues,
    structureColumn,
    activityColumns,
    activityTransformMap,
    activityMethodMap,
    executeUnifiedUpload,
  ]);
  const activityTransformByColumn = useMemo(() => {
    const byColumn = new Map<string, ActivityTransform>();
    for (const col of activityColumns) {
      const token = readText(col);
      if (!token) continue;
      byColumn.set(token, normalizeActivityTransform(activityTransformMap[token] || 'none'));
    }
    return byColumn;
  }, [activityColumns, activityTransformMap]);
  const activityPairChips = useMemo(() => {
    return activityColumns.map((activityCol) => {
      const methodId = readText(activityMethodMap[activityCol]);
      const outputBase = methodOutputPropertyById.get(methodId) || '';
      const outputProperty = outputBase || '';
      return {
        column: activityCol,
        outputProperty,
      };
    });
  }, [activityColumns, activityMethodMap, methodOutputPropertyById]);
  const databaseById = useMemo(() => {
    const map = new Map<string, MmpLifecycleDatabaseItem>();
    for (const item of databases) {
      const id = readText(item.id);
      if (!id) continue;
      map.set(id, item);
    }
    return map;
  }, [databases]);
  const mappingSaveBlockReason = useMemo(() => {
    const dbId = readText(batchDatabaseId);
    if (!dbId) return 'Choose target MMP database before saving mappings';
    const dbMeta = databaseById.get(dbId);
    if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
      return 'Target database is still building';
    }
    if (isDatabaseBusy(dbId)) {
      return 'Target database is busy with another update';
    }
    return '';
  }, [batchDatabaseId, databaseById, isDatabaseBusy]);
  const mappingSaveBlocked = Boolean(mappingSaveBlockReason);
  const activityColumnsMissingMethod = useMemo(() => {
    const missing = new Set<string>();
    for (const item of activityColumns) {
      const sourceProperty = readText(item);
      if (!sourceProperty) continue;
      const methodId = readText(activityMethodMap[sourceProperty]);
      const outputProperty = readText(methodOutputPropertyById.get(methodId) || '');
      if (!methodId || !outputProperty) {
        missing.add(sourceProperty.toLowerCase());
      }
    }
    return missing;
  }, [activityColumns, activityMethodMap, methodOutputPropertyById]);
  const getBatchAssayMetadata = useCallback((batch: MmpLifecycleBatch) => {
    const files = asRecord(batch.files);
    const experimentCfg = asRecord(asRecord(files.experiments).column_config);
    const batchActivityColumns = parseConfiguredColumns(experimentCfg.activity_columns);
    const batchOutputPropertyMap = asRecord(experimentCfg.activity_output_property_map);
    const batchMethodMap = asRecord(experimentCfg.activity_method_map);
    const assayTokens = new Set<string>();
    const methodTokens = new Set<string>();
    for (const activityCol of batchActivityColumns) {
      const outputName = readText(batchOutputPropertyMap[activityCol]) || activityCol;
      if (outputName) assayTokens.add(outputName);
      const methodId = readText(batchMethodMap[activityCol]);
      if (methodId) methodTokens.add(methodNameById.get(methodId) || methodId);
    }
    return {
      assays: Array.from(assayTokens),
      methods: Array.from(methodTokens),
    };
  }, [methodNameById]);
  const batchDatabaseFilterOptions = useMemo(() => {
    const uniq = new Set<string>();
    for (const item of batches) {
      const dbId = readText(item.selected_database_id);
      if (!dbId) continue;
      uniq.add(dbId);
    }
    return Array.from(uniq).map((dbId) => {
      const dbMeta = databaseById.get(dbId);
      return {
        value: dbId,
        label: readText(dbMeta?.label || dbMeta?.schema || dbMeta?.id || dbId) || dbId,
      };
    }).sort((a, b) => a.label.localeCompare(b.label));
  }, [batches, databaseById]);
  const batchStatusOptions = useMemo(() => {
    const uniq = new Set<string>();
    for (const item of batches) {
      const token = readText(item.status).toLowerCase();
      if (!token) continue;
      uniq.add(token);
    }
    return Array.from(uniq).sort((a, b) => a.localeCompare(b));
  }, [batches]);
  const filteredBatches = useMemo(() => {
    const query = batchQuery.trim().toLowerCase();
    const statusFilter = batchStatusFilter.trim().toLowerCase();
    const databaseFilter = batchDatabaseFilter.trim();
    const rows = batches.filter((item) => {
      if (statusFilter && statusFilter !== 'all') {
        const status = readText(item.status).toLowerCase();
        if (status !== statusFilter) return false;
      }
      if (databaseFilter && databaseFilter !== 'all') {
        const dbId = readText(item.selected_database_id);
        if (dbId !== databaseFilter) return false;
      }
      if (!query) return true;
      const name = readText(item.name).toLowerCase();
      const id = readText(item.id).toLowerCase();
      const status = readText(item.status).toLowerCase();
      const dbId = readText(item.selected_database_id);
      const dbMeta = databaseById.get(dbId);
      const db = readText(dbMeta?.label || dbMeta?.schema || dbMeta?.id || dbId).toLowerCase();
      const assayMeta = getBatchAssayMetadata(item);
      const assayBlob = [...assayMeta.assays, ...assayMeta.methods].join(' ').toLowerCase();
      return name.includes(query) || id.includes(query) || status.includes(query) || db.includes(query) || assayBlob.includes(query);
    });
    rows.sort((a, b) => {
      const key = batchSortKey;
      const direction = batchSortDirection === 'asc' ? 1 : -1;
      if (key === 'updated_at') {
        const av = Date.parse(readText(a.updated_at) || '') || 0;
        const bv = Date.parse(readText(b.updated_at) || '') || 0;
        return (av - bv) * direction;
      }
      const av = readText((a as unknown as Record<string, unknown>)[key]).toLowerCase();
      const bv = readText((b as unknown as Record<string, unknown>)[key]).toLowerCase();
      return av.localeCompare(bv) * direction;
    });
    return rows;
  }, [batches, batchQuery, batchStatusFilter, batchDatabaseFilter, batchSortKey, batchSortDirection, getBatchAssayMetadata, databaseById]);
  const filteredBatchIds = useMemo(
    () => filteredBatches.map((item) => readText(item.id)).filter(Boolean),
    [filteredBatches]
  );
  const allFilteredSelected =
    filteredBatchIds.length > 0 && filteredBatchIds.every((id) => selectedBatchIds.includes(id));
  const selectedCount = selectedBatchIds.length;
  const bulkApplyBlockReason = useMemo(() => {
    if (selectedCount <= 0) return 'Select batches first';
    const batchById = new Map<string, MmpLifecycleBatch>();
    for (const item of batches) {
      const id = readText(item.id);
      if (!id) continue;
      batchById.set(id, item);
    }
    for (const batchId of selectedBatchIds) {
      const batch = batchById.get(batchId);
      if (!batch) continue;
      const databaseId = readText(batch.selected_database_id);
      if (!databaseId) return `Batch "${readText(batch.name) || batchId}" is missing target database`;
      const dbMeta = databaseById.get(databaseId);
      if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
        return `Database for "${readText(batch.name) || batchId}" is still building`;
      }
      if (isDatabaseBusy(databaseId)) {
        return `Database for "${readText(batch.name) || batchId}" is busy with another update`;
      }
    }
    return '';
  }, [selectedCount, batches, selectedBatchIds, databaseById, isDatabaseBusy]);
  const bulkApplyBlocked = Boolean(bulkApplyBlockReason);
  useEffect(() => {
    const visible = new Set(filteredBatchIds);
    setSelectedBatchIds((prev) => {
      const next = prev.filter((id) => visible.has(id));
      if (next.length === prev.length && next.every((id, index) => id === prev[index])) {
        return prev;
      }
      return next;
    });
  }, [filteredBatchIds]);
  const toggleBatchSelection = useCallback((batchIdRaw: string, checked: boolean) => {
    const batchId = readText(batchIdRaw);
    if (!batchId) return;
    setSelectedBatchIds((prev) => {
      if (checked) {
        if (prev.includes(batchId)) return prev;
        return [...prev, batchId];
      }
      return prev.filter((id) => id !== batchId);
    });
  }, []);
  const toggleSelectAllFiltered = useCallback((checked: boolean) => {
    if (!checked) {
      setSelectedBatchIds([]);
      return;
    }
    setSelectedBatchIds(filteredBatchIds);
  }, [filteredBatchIds]);
  const openBatchDetail = (batchId: string) => {
    const id = readText(batchId);
    if (!id) return;
    setSelectedBatchId(id);
    setFlowStep('batch');
    setViewMode('detail');
  };

  const openAssayMethodsModal = () => {
    resetMethodEditorDraft();
    setMethodEditorOpen(false);
    setError(null);
    const candidateDbId = readText(batchDatabaseId) || readText(databases[0]?.id);
    setAssayMethodDatabaseId(candidateDbId);
    void loadAssayDatabaseProperties(candidateDbId);
    void loadAssayDatabaseMethodBindings(candidateDbId);
    void loadAssayMethodUsage(candidateDbId);
    void loadPendingSyncSummary(candidateDbId);
    setAssayMethodModalOpen(true);
  };

  const closeAssayMethodsModal = () => {
    setMethodEditorOpen(false);
    setAssayMethodDatabaseId('');
    setAssayDatabasePropertyNames([]);
    setAssayBoundMethodIds([]);
    setAssayMethodUsageRows([]);
    setAssayMethodDisplayTransformById({});
    setAssayMethodModalOpen(false);
  };

  const selectedAssayDatabasePendingSyncCount = useMemo(() => {
    const dbId = readText(assayMethodDatabaseId);
    if (!dbId) return 0;
    return Number(pendingSyncByDatabase[dbId] || 0);
  }, [assayMethodDatabaseId, pendingSyncByDatabase]);

  const selectedAssayDatabaseStateLabel = useMemo(() => {
    const dbId = readText(assayMethodDatabaseId);
    if (!dbId) return '-';
    const db = databaseById.get(dbId);
    if (!db) return '-';
    return getDatabaseBuildState(db) === 'ready' ? 'Ready' : 'Building';
  }, [assayMethodDatabaseId, databaseById]);

  const selectedAssayDatabasePendingProperties = useMemo(() => {
    const dbId = readText(assayMethodDatabaseId);
    if (!dbId) return [] as string[];
    const tokens = new Set<string>();
    for (const item of pendingDatabaseSyncRows) {
      if (readText(item.database_id) !== dbId) continue;
      const op = readText(item.operation);
      const payload = asRecord(item.payload);
      const prop = readText(payload.property_name || payload.new_name);
      if (op === 'ensure_property' || op === 'rename_property') {
        if (prop) tokens.add(prop.toLowerCase());
      }
    }
    return Array.from(tokens);
  }, [assayMethodDatabaseId, pendingDatabaseSyncRows]);

  const assayMethodsForSelectedDatabase = useMemo(() => {
    const dbId = readText(assayMethodDatabaseId);
    if (!dbId) return [];
    const dedupedMethods = dedupeLifecycleMethods(methods);
    const mappedMethodIds = new Set(assayBoundMethodIds.map((id) => readText(id)).filter(Boolean));
    const propertySet = new Set(assayDatabasePropertyNames.map((name) => readText(name).toLowerCase()).filter(Boolean));
    const pendingPropertySet = new Set(selectedAssayDatabasePendingProperties.map((name) => readText(name).toLowerCase()).filter(Boolean));
    return dedupedMethods
      .filter((method) => {
        const methodId = readText(method.id);
        if (!methodId) return false;
        if (!mappedMethodIds.has(methodId)) return false;
        const outputProperty = readText(method.output_property).toLowerCase();
        if (!outputProperty) return false;
        if (propertySet.has(outputProperty)) return true;
        if (pendingPropertySet.has(outputProperty)) return true;
        return propertySet.size === 0;
      })
      .sort((a, b) => {
        const left = readText(a.output_property || a.key || a.name);
        const right = readText(b.output_property || b.key || b.name);
        return left.localeCompare(right);
      });
  }, [assayMethodDatabaseId, assayBoundMethodIds, assayDatabasePropertyNames, selectedAssayDatabasePendingProperties, methods]);

  const assayMethodDataCountById = useMemo(() => {
    const dbId = readText(assayMethodDatabaseId);
    const output = new Map<string, number>();
    if (!dbId) return output;
    for (const item of assayMethodUsageRows) {
      const rowDbId = readText(item.database_id);
      if (rowDbId !== dbId) continue;
      const methodId = readText(item.method_id);
      const count = readNumber(item.data_count);
      if (!methodId || count === null) continue;
      output.set(methodId, Math.max(0, Math.trunc(count)));
    }
    return output;
  }, [assayMethodDatabaseId, assayMethodUsageRows]);

  const assayPropertySummaryText = useMemo(() => {
    if (assayDatabasePropertyNames.length === 0) return '-';
    const head = assayDatabasePropertyNames.slice(0, 3);
    const extra = assayDatabasePropertyNames.length - head.length;
    return extra > 0 ? `${head.join(' · ')} +${extra}` : head.join(' · ');
  }, [assayDatabasePropertyNames]);
  const runActionBlockReason = useMemo(() => {
    const dbId = readText(batchDatabaseId);
    if (!viewMode || viewMode !== 'detail') return 'Open one batch first';
    if (!selectedBatch) return 'Open one batch first';
    if (!dbId) return 'Choose target MMP database first';
    const dbMeta = databaseById.get(dbId);
    if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
      return 'Target database is still building';
    }
    if (isDatabaseBusy(dbId)) {
      return 'Target database is busy with another update';
    }
    return '';
  }, [viewMode, selectedBatch, batchDatabaseId, databaseById, isDatabaseBusy]);
  const runActionBlocked = Boolean(runActionBlockReason);
  const checkActionBlockReason = useMemo(() => {
    const dbId = readText(batchDatabaseId);
    if (!viewMode || viewMode !== 'detail') return 'Open one batch first';
    if (!selectedBatch) return 'Open one batch first';
    if (!dbId) return 'Choose target MMP database first';
    const dbMeta = databaseById.get(dbId);
    if (!dbMeta || getDatabaseBuildState(dbMeta) !== 'ready') {
      return 'Target database is still building';
    }
    return '';
  }, [viewMode, selectedBatch, batchDatabaseId, databaseById]);
  const checkActionBlocked = Boolean(checkActionBlockReason);
  const uploadSaveBlockReason = useMemo(() => {
    if (!selectedBatch) return 'Select one lifecycle batch first';
    if (!uploadFile) return 'Select file in left upload panel';
    if (uploadParsing) return 'Parsing upload file';
    if (saving || autoUploadStatus === 'uploading') return 'Upload is running';
    if (!isUploadConfigComplete) return uploadConfigIssues[0] || 'Adjust upload configuration first';
    return '';
  }, [selectedBatch, uploadFile, uploadParsing, saving, autoUploadStatus, isUploadConfigComplete, uploadConfigIssues]);
  const uploadSaveBlocked = Boolean(uploadSaveBlockReason);
  const saveUploadDraft = useCallback(() => {
    if (uploadSaveBlocked) {
      if (uploadSaveBlockReason) setError(uploadSaveBlockReason);
      return;
    }
    setError(null);
    setAutoUploadStatus('pending');
    setAutoUploadStatusText('Manual save queued...');
    setAutoUploadTrigger((prev) => prev + 1);
  }, [uploadSaveBlocked, uploadSaveBlockReason]);
  const showHeaderStepActions = viewMode === 'detail' && Boolean(selectedBatch);
  const showListHeaderActions = viewMode === 'list';

  return (
    <div className="page-grid mmp-life-page">
      <section className="page-header">
        <div className="page-header-left">
          <h1>MMP Lifecycle Admin</h1>
          <p className="muted">Batch-driven compound and experiment lifecycle management for incremental MMP updates.</p>
          {viewMode === 'detail' && selectedBatch ? (
            <div className="project-compact-meta mmp-life-header-status">
              <span className={`task-state-chip mmp-life-head-pill ${selectedBatchStatusToken || 'draft'}`}>{selectedBatchStatusLabel || '-'}</span>
              <span className={`badge mmp-life-head-pill ${gatePass ? 'is-good' : 'is-bad'}`}>Gate: {gatePass ? 'PASS' : 'BLOCKED'}</span>
              <span className="meta-chip mmp-life-head-pill">Checked: {formatDateTime(readText(batchLastCheck.checked_at))}</span>
              <span className="meta-chip mmp-life-head-pill">Applied: {summarizeCount(selectedBatchApplyHistoryCount)}</span>
            </div>
          ) : null}
        </div>
        <div className="page-header-actions mmp-life-header-actions">
          {viewMode === 'detail' ? (
            <button
              className="mmp-life-header-circle-btn"
              type="button"
              title="Back to Batch Table"
              aria-label="Back to Batch Table"
              onClick={() => setViewMode('list')}
            >
              <ArrowLeft size={14} />
            </button>
          ) : null}
          {showHeaderStepActions && flowStep === 'batch' ? (
            <div className="mmp-life-header-inline-actions">
              <button
                className="mmp-life-header-circle-btn mmp-life-header-circle-btn-primary"
                type="button"
                onClick={saveBatchMeta}
                disabled={saving}
                title="Save batch metadata"
                aria-label="Save batch metadata"
              >
                <Save size={14} />
              </button>
              <button
                className="mmp-life-header-circle-btn"
                type="button"
                onClick={resetBatchMetaDraft}
                disabled={saving}
                title="Reset batch metadata draft"
                aria-label="Reset batch metadata draft"
              >
                <RotateCcw size={14} />
              </button>
            </div>
          ) : null}
          {showHeaderStepActions && flowStep === 'upload' ? (
            <div className="mmp-life-header-inline-actions">
              <button
                className="mmp-life-header-circle-btn mmp-life-header-circle-btn-primary"
                type="button"
                onClick={saveUploadDraft}
                disabled={uploadSaveBlocked}
                title={uploadSaveBlockReason || 'Save upload draft'}
                aria-label={uploadSaveBlockReason || 'Save upload draft'}
              >
                <Save size={14} />
              </button>
              <button
                className="mmp-life-header-circle-btn"
                type="button"
                onClick={resetUploadDraft}
                disabled={saving || uploadParsing}
                title="Reset upload draft to saved batch state"
                aria-label="Reset upload draft to saved batch state"
              >
                <RotateCcw size={14} />
              </button>
            </div>
          ) : null}
          {showHeaderStepActions && flowStep === 'mapping' ? (
            <div className="mmp-life-header-inline-actions">
              <button
                className="mmp-life-header-circle-btn mmp-life-header-circle-btn-primary"
                type="button"
                onClick={() => void saveMappings()}
                disabled={saving || mappingSaveBlocked}
                title={mappingSaveBlocked ? mappingSaveBlockReason : 'Save mapping rules'}
                aria-label="Save mapping rules"
              >
                <Save size={14} />
              </button>
              <button
                className="mmp-life-header-circle-btn"
                type="button"
                onClick={() => void resetMappingDraft()}
                disabled={saving}
                title="Reset to saved batch mapping"
                aria-label="Reset to saved batch mapping"
              >
                <RotateCcw size={14} />
              </button>
            </div>
          ) : null}
          {showHeaderStepActions && flowStep === 'qa' ? (
            <div className="mmp-life-header-inline-actions">
              <button
                className="mmp-life-header-circle-btn"
                type="button"
                onClick={() => void runCheckOnly()}
                disabled={saving || checkActionBlocked}
                title={checkActionBlockReason || 'Run check only (no database write)'}
                aria-label={checkActionBlockReason || 'Run check only (no database write)'}
              >
                {qaAction === 'check' ? <Loader2 size={14} className="mmp-life-icon-spin" /> : <ShieldCheck size={14} />}
              </button>
              <button
                className="mmp-life-header-circle-btn mmp-life-header-circle-btn-primary"
                type="button"
                onClick={() => void runCheckAndApply()}
                disabled={saving || runActionBlocked}
                title={runActionBlockReason || 'Apply (auto check + apply)'}
                aria-label={runActionBlockReason || 'Apply (auto check + apply)'}
              >
                {qaAction === 'apply' ? <Loader2 size={14} className="mmp-life-icon-spin" /> : <RunPlayIcon size={14} />}
              </button>
              <button
                className="mmp-life-header-circle-btn"
                type="button"
                onClick={resetCheckDraft}
                disabled={saving}
                title="Reset check panel to saved batch summary"
                aria-label="Reset check panel to saved batch summary"
              >
                <RotateCcw size={14} />
              </button>
            </div>
          ) : null}
          {showHeaderStepActions && flowStep !== 'qa' ? (
            <button
              className="mmp-life-header-circle-btn mmp-life-header-circle-btn-primary"
              type="button"
              onClick={() => void runCheckAndApply()}
              disabled={saving || runActionBlocked}
              title={runActionBlockReason || 'Run (check + apply)'}
              aria-label={runActionBlockReason || 'Run (check + apply)'}
            >
              {qaAction === 'apply' ? <Loader2 size={14} className="mmp-life-icon-spin" /> : <RunPlayIcon size={14} />}
            </button>
          ) : null}
          {showListHeaderActions ? (
            <div className="mmp-life-header-inline-actions">
              <button
                className="mmp-life-header-circle-btn mmp-life-header-circle-btn-primary"
                type="button"
                onClick={() => void runBulkApplyFromList()}
                disabled={saving || bulkApplyRunning || selectedCount <= 0 || bulkApplyBlocked}
                aria-label="Apply Selected"
              >
                {bulkApplyRunning ? <Loader2 size={14} className="mmp-life-icon-spin" /> : <RunPlayIcon size={14} />}
              </button>
              <button
                className="mmp-life-header-circle-btn"
                type="button"
                onClick={resetBatchListView}
                disabled={saving || bulkApplyRunning}
                title="Reset list filters and selections"
                aria-label="Reset list filters and selections"
              >
                <RotateCcw size={14} />
              </button>
              <button
                className="mmp-life-header-circle-btn"
                type="button"
                onClick={() => void createBatchFromHeader()}
                disabled={saving || bulkApplyRunning}
                title="Create batch"
                aria-label="Create batch"
              >
                <Plus size={14} />
              </button>
            </div>
          ) : null}
          {(showHeaderStepActions || showListHeaderActions) ? <span className="mmp-life-header-divider" aria-hidden="true" /> : null}
          <button
            className="mmp-life-header-circle-btn"
            type="button"
            title="Assay Methods"
            aria-label="Assay Methods"
            onClick={openAssayMethodsModal}
            disabled={saving}
          >
            <ListChecks size={14} />
          </button>
          {isAdmin ? (
            <button
              className="mmp-life-header-circle-btn"
              type="button"
              title="Lead Opt Databases"
              aria-label="Lead Opt Databases"
              onClick={() => setDatabaseAdminModalOpen(true)}
              disabled={saving}
            >
              <Settings2 size={14} />
            </button>
          ) : null}
        </div>
      </section>

      {error ? <div className="alert error">{error}</div> : null}

      {viewMode === 'list' ? (
        <>
          <div className="toolbar project-toolbar mmp-life-list-toolbar">
            <div className="project-toolbar-filters">
              <div className="project-filter-field project-filter-field-search">
                <div className="input-wrap search-input">
                  <Search size={16} />
                  <input
                    value={batchQuery}
                    onChange={(event) => setBatchQuery(event.target.value)}
                    placeholder="Search batch / id / status / database / assay"
                    aria-label="Search batch list"
                  />
                </div>
              </div>
              <label className="project-filter-field">
                <Activity size={14} />
                <select
                  className="project-filter-select"
                  value={batchStatusFilter}
                  onChange={(event) => setBatchStatusFilter(event.target.value)}
                  aria-label="Filter batches by status"
                >
                  <option value="all">All Status</option>
                  {batchStatusOptions.map((status) => (
                    <option key={status} value={status}>
                      {status}
                    </option>
                  ))}
                </select>
              </label>
              <label className="project-filter-field">
                <Database size={14} />
                <select
                  className="project-filter-select"
                  value={batchDatabaseFilter}
                  onChange={(event) => setBatchDatabaseFilter(event.target.value)}
                  aria-label="Filter batches by database"
                >
                  <option value="all">All Database</option>
                  {batchDatabaseFilterOptions.map((item) => (
                    <option key={item.value} value={item.value}>
                      {item.label}
                    </option>
                  ))}
                </select>
              </label>
              <label className="project-filter-field">
                <Filter size={14} />
                <select
                  className="project-filter-select"
                  value={batchSortKey}
                  onChange={(event) => setBatchSortKey(event.target.value as BatchSortKey)}
                  aria-label="Sort batches by field"
                >
                  <option value="updated_at">Sort: Updated</option>
                  <option value="name">Sort: Name</option>
                  <option value="status">Sort: Status</option>
                  <option value="selected_database_id">Sort: Database</option>
                </select>
              </label>
              <label className="project-filter-field">
                <Filter size={14} />
                <select
                  className="project-filter-select"
                  value={batchSortDirection}
                  onChange={(event) => setBatchSortDirection(event.target.value as SortDirection)}
                  aria-label="Sort direction"
                >
                  <option value="desc">Order: Desc</option>
                  <option value="asc">Order: Asc</option>
                </select>
              </label>
            </div>
            <div className="project-toolbar-meta project-toolbar-meta-rich">
              <span className="meta-chip">Selected {selectedCount}</span>
              <span className="meta-chip">Visible {filteredBatches.length}</span>
            </div>
          </div>
          <div className="table-wrap project-table-wrap task-table-wrap">
            <table className="table project-table task-table task-table--leadopt mmp-life-batch-table">
              <colgroup>
                <col className="mmp-life-col-select" />
                <col className="mmp-life-col-batch" />
                <col className="mmp-life-col-status" />
                <col className="mmp-life-col-database" />
                <col className="mmp-life-col-compounds" />
                <col className="mmp-life-col-assays" />
                <col className="mmp-life-col-files" />
                <col className="mmp-life-col-updated" />
                <col className="mmp-life-col-actions" />
              </colgroup>
              <thead>
                <tr>
                  <th className="mmp-life-select-head">
                    <input
                      type="checkbox"
                      checked={allFilteredSelected}
                      onChange={(event) => toggleSelectAllFiltered(event.target.checked)}
                      aria-label="Select all visible batches"
                    />
                  </th>
                  <th><span className="project-th">Batch</span></th>
                  <th><span className="project-th">Status</span></th>
                  <th><span className="project-th">Database</span></th>
                  <th><span className="project-th">Compounds</span></th>
                  <th><span className="project-th">Assays</span></th>
                  <th><span className="project-th">Files</span></th>
                  <th><span className="project-th">Updated</span></th>
                  <th className="mmp-life-actions-head"><span className="project-th">Actions</span></th>
                </tr>
              </thead>
              <tbody>
                {batches.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="muted">No batch yet.</td>
                  </tr>
                ) : filteredBatches.length === 0 ? (
                  <tr>
                    <td colSpan={9} className="muted">No batch matches current filters.</td>
                  </tr>
                ) : (
                  filteredBatches.map((item) => {
                    const id = readText(item.id);
                    const files = asRecord(item.files);
                    const runtime = asRecord(item.apply_runtime);
                    const hasRuntimePhase = Boolean(readText(runtime.phase));
                    const statusToken = hasRuntimePhase
                      ? normalizeBatchStatusToken(runtime.phase)
                      : normalizeBatchStatusToken(item.status);
                    const statusLabel = statusToken;
                    const fileSummary = `${asRecord(files.compounds).path ? 'compounds ' : ''}${asRecord(files.experiments).path ? 'experiments' : ''}`.trim() || '-';
                    const dbId = readText(item.selected_database_id);
                    const dbMeta = databaseById.get(dbId);
                    const dbLabel = readText(dbMeta?.label || dbMeta?.schema || dbMeta?.id || dbId) || '-';
                    const dbState = dbMeta ? getDatabaseBuildState(dbMeta) : 'building';
                    const dbPendingSync = Number(pendingSyncByDatabase[dbId] || 0);
                    const lastCheck = asRecord(item.last_check);
                    const compoundSummary = asRecord(lastCheck.compound_summary);
                    const compoundCount = readNumber(compoundSummary.annotated_rows) ?? readNumber(compoundSummary.total_rows);
                    const assayMeta = getBatchAssayMetadata(item);
                    const assayList = assayMeta.assays;
                    const methodList = assayMeta.methods;
                    const assayPrimary = assayList.length > 0
                      ? `${assayList[0]}${assayList.length > 1 ? ` +${assayList.length - 1}` : ''}`
                      : '-';
                    const methodPrimary = methodList.length > 0
                      ? `${methodList[0]}${methodList.length > 1 ? ` +${methodList.length - 1}` : ''}`
                      : '-';
                    const checked = selectedBatchIds.includes(id);
                    return (
                      <tr key={id}>
                        <td className="mmp-life-select-cell">
                          <input
                            type="checkbox"
                            checked={checked}
                            onChange={(event) => toggleBatchSelection(id, event.target.checked)}
                            aria-label={`Select batch ${readText(item.name) || id || '-'}`}
                          />
                        </td>
                        <td className="task-col-submitted">
                          <div className="task-submitted-cell">
                            <div className="task-submitted-title">{readText(item.name) || '-'}</div>
                            <div className="task-submitted-summary">{id || '-'}</div>
                          </div>
                        </td>
                        <td className="mmp-life-status-cell">
                          <div className="mmp-life-status-wrap">
                            <span className={`task-state-chip ${statusToken || 'draft'}`}>
                              {statusLabel}
                            </span>
                          </div>
                        </td>
                        <td className="mmp-life-db-cell">
                          <div className="mmp-life-db-primary">{dbLabel}</div>
                          <div className="mmp-life-db-meta">
                            <span>{readText(dbMeta?.schema) || dbId || '-'}</span>
                            <span>{dbState === 'ready' ? 'Ready' : 'Building'}</span>
                            {dbPendingSync > 0 ? <span>{`Pending Sync ${summarizeCount(dbPendingSync)}`}</span> : null}
                          </div>
                        </td>
                        <td className="mmp-life-num-cell">{compoundCount === null ? '-' : summarizeCount(compoundCount)}</td>
                        <td className="mmp-life-assay-cell">
                          <div className="mmp-life-assay-primary" title={assayList.join(', ') || '-'}>
                            {assayPrimary}
                          </div>
                          <div className="mmp-life-assay-meta" title={methodList.join(', ') || '-'}>
                            {assayList.length} assay{assayList.length === 1 ? '' : 's'} · {methodPrimary}
                          </div>
                        </td>
                        <td className="mmp-life-files-cell">{fileSummary}</td>
                        <td className="mmp-life-updated-cell">{formatDateTime(readText(item.updated_at))}</td>
                        <td className="mmp-life-actions-cell">
                          <div className="mmp-life-run-row">
                            <button
                              className="icon-btn"
                              type="button"
                              title="Manage batch"
                              aria-label="Manage batch"
                              onClick={() => openBatchDetail(id)}
                              disabled={!id}
                            >
                              <ListChecks size={14} />
                            </button>
                            <button
                              className="icon-btn danger"
                              type="button"
                              title="Delete batch"
                              aria-label="Delete batch"
                              onClick={() => void removeBatch(id)}
                              disabled={saving || !id}
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
        </>
      ) : (
      <div className="workspace-shell mmp-life-main">
          <aside className="workspace-stepper mmp-life-workspace-stepper" aria-label="Lifecycle sections">
            <div className="workspace-stepper-track" aria-hidden="true" />
            {FLOW_STEPS.map((item, index) => (
              <button
                key={item.key}
                type="button"
                className={`workspace-step ${flowStep === item.key ? 'active' : ''}${index < flowStepIndex ? ' done' : ''}`}
                onClick={() => setFlowStep(item.key)}
                aria-label={item.label}
                data-label={item.label}
                title={item.label}
              >
                <span className="workspace-step-dot">
                  {item.key === 'batch' ? <Database size={13} /> : null}
                  {item.key === 'upload' ? <Upload size={13} /> : null}
                  {item.key === 'mapping' ? <ListChecks size={13} /> : null}
                  {item.key === 'qa' ? <ShieldCheck size={13} /> : null}
                </span>
              </button>
            ))}
          </aside>

          <div className="workspace-content">
            {!selectedBatch ? (
              <section className="panel">
                <div className="alert warning">Batch not selected. Go back to the table and choose one batch.</div>
              </section>
            ) : null}

            {flowStep === 'batch' && selectedBatch ? (
              <section className="panel">
                <div className="toolbar">
                  <span />
                  <span />
                </div>
                <section className="panel subtle">
                  <h3>Batch Metadata</h3>
                  <div className="mmp-life-meta-grid">
                    <label className="field">
                      <span>Batch name</span>
                      <input value={batchName} onChange={(event) => setBatchName(event.target.value)} />
                    </label>
                    <label className="field">
                      <span>Target MMP database</span>
                      <select value={batchDatabaseId} onChange={(event) => setBatchDatabaseId(event.target.value)}>
                        <option value="">Select database</option>
                        {databases.map((item) => {
                          const id = readText(item.id);
                          const label = readText(item.label || item.schema || item.id);
                          const state = getDatabaseBuildState(item);
                          const stateLabel = state === 'ready' ? 'Ready' : 'Building';
                          return (
                            <option key={id} value={id}>{label} · {stateLabel}</option>
                          );
                        })}
                      </select>
                    </label>
                    <label className="field">
                      <span>Description</span>
                      <input value={batchDescription} onChange={(event) => setBatchDescription(event.target.value)} />
                    </label>
                    <label className="field">
                      <span>Notes</span>
                      <input value={batchNotes} onChange={(event) => setBatchNotes(event.target.value)} />
                    </label>
                  </div>
                </section>
              </section>
            ) : null}

            {flowStep === 'upload' && selectedBatch ? (
              <section className="panel">
                <div className="mmp-life-upload-form">
                  <div
                    ref={uploadWorkspaceRef}
                    className={`mmp-life-upload-resizable ${isUploadWorkspaceResizing ? 'is-resizing' : ''}`}
                    style={uploadWorkspaceStyle}
                  >
                    <section className="panel subtle mmp-life-upload-file-card mmp-life-upload-pane">
                      <div className="mmp-life-upload-file-head">
                        <div className="mmp-life-upload-file-field">
                          <input
                            className="file-input-unified"
                            type="file"
                            accept=".xlsx,.csv,.tsv,.txt"
                            onChange={(event) => {
                              void onUploadFileChange(event);
                            }}
                          />
                        </div>
                        <div className="project-compact-meta mmp-life-upload-meta">
                          <span className="meta-chip">Cols {uploadHeaderOptions.length}</span>
                          <span className="meta-chip">{activeCompoundsFileName}</span>
                          <span className="meta-chip">Act {activityColumns.length}</span>
                        </div>
                      </div>
                      <section className="mmp-life-upload-left-stack">
                        <section className="mmp-life-map-section mmp-life-structure-card">
                          <div className="mmp-life-structure-head">
                            <span className="mmp-life-conversion-title">
                              <Link2 size={13} />
                              <span>Structure</span>
                            </span>
                          </div>
                          <div className="mmp-life-structure-row">
                            <label className="mmp-life-conversion-select-wrap mmp-life-structure-select-wrap" aria-label="Structure column">
                              <select value={structureColumn} onChange={(event) => setStructureColumn(event.target.value)}>
                                <option value="">Choose structure column</option>
                                {uploadColumnOptions.map((name) => (
                                  <option key={`upload-structure-${name}`} value={name}>{name}</option>
                                ))}
                              </select>
                            </label>
                            <button
                              className="icon-btn"
                              type="button"
                              title="Auto-detect structure column"
                              aria-label="Auto-detect structure column"
                              onClick={autoPickStructureColumn}
                              disabled={uploadColumnOptions.length === 0}
                            >
                              <Sparkles size={14} />
                            </button>
                          </div>
                        </section>
                        <section className="mmp-life-activity-method-map" aria-label="Activity mapping">
                          <div className="toolbar">
                            <h4>
                              <Activity size={13} />
                              <span>Activity</span>
                            </h4>
                            <button className="btn btn-ghost btn-compact" type="button" onClick={addActivityPair} disabled={uploadColumnOptions.length === 0}>
                              <Plus size={14} />
                              Add
                            </button>
                          </div>
                          {activityColumns.length === 0 ? (
                            <div className="muted small">No activity column.</div>
                          ) : (
                            <div className="table-wrap">
                              <table className="table mmp-life-pairs-table">
                                <thead>
                                  <tr>
                                    <th>Column</th>
                                    <th>Transform</th>
                                    <th />
                                  </tr>
                                </thead>
                                <tbody>
                                  {activityColumns.map((activityCol, idx) => {
                                    const transform = normalizeActivityTransform(activityTransformMap[activityCol] || 'none');
                                    const occupied = new Set(activityColumns.filter((_, rowIdx) => rowIdx !== idx));
                                    const structureToken = readText(structureColumn);
                                    return (
                                      <tr key={`upload-activity-${activityCol}-${idx}`}>
                                        <td>
                                          <select
                                            className="mmp-life-activity-col-select"
                                            value={activityCol}
                                            onChange={(event) => updateActivityPairColumn(activityCol, event.target.value)}
                                          >
                                            <option value="">Select column</option>
                                            {uploadColumnOptions
                                              .filter((name) => {
                                                if (name === activityCol) return true;
                                                if (occupied.has(name)) return false;
                                                if (name === structureToken) return false;
                                                return true;
                                              })
                                              .map((name) => (
                                                <option key={`upload-pair-col-${idx}-${name}`} value={name}>{name}</option>
                                              ))}
                                          </select>
                                        </td>
                                        <td>
                                          <select
                                            className="mmp-life-activity-transform-select"
                                            value={transform}
                                            onChange={(event) =>
                                              setActivityTransformMap((prev) => ({
                                                ...prev,
                                                [activityCol]: normalizeActivityTransform(event.target.value),
                                              }))
                                            }
                                          >
                                            {ACTIVITY_TRANSFORM_OPTIONS.map((option) => (
                                              <option key={`upload-${activityCol}-${option.value}`} value={option.value}>
                                                {option.label}
                                              </option>
                                            ))}
                                          </select>
                                        </td>
                                        <td>
                                          <button
                                            className="icon-btn danger"
                                            type="button"
                                            title="Remove activity column"
                                            aria-label="Remove activity column"
                                            onClick={() => removeActivityPair(activityCol)}
                                          >
                                            <Trash2 size={14} />
                                          </button>
                                        </td>
                                      </tr>
                                    );
                                  })}
                                </tbody>
                              </table>
                            </div>
                          )}
                        </section>
                        {uploadPreviewStats.perColumn.length > 0 ? (
                          <section className="mmp-life-conversion-stats">
                            <div className="mmp-life-conversion-stats-head">
                              <Database size={12} />
                              <span>Column Quality</span>
                            </div>
                            <div className="table-wrap">
                              <table className="table mmp-life-upload-stats-table">
                                <thead>
                                  <tr>
                                    <th>Column</th>
                                    <th>Fill</th>
                                    <th>Num</th>
                                    <th>Pos</th>
                                    <th>%</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {uploadPreviewStats.perColumn.map((stat) => (
                                    <tr key={`upload-stats-left-${stat.column}`}>
                                      <td className="mmp-life-mono">{stat.column}</td>
                                      <td>{summarizeCount(stat.nonEmpty)}</td>
                                      <td>{summarizeCount(stat.numeric)}</td>
                                      <td>{summarizeCount(stat.positive)}</td>
                                      <td>{(stat.coverage * 100).toFixed(1)}</td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </section>
                        ) : null}
                        <div className={`mmp-life-upload-auto-state is-${autoUploadStatus}`}>
                          <span className="mmp-life-upload-auto-icon" aria-hidden>
                            {autoUploadStatus === 'success' ? <CheckCircle2 size={14} /> : null}
                            {autoUploadStatus === 'error' ? <X size={14} /> : null}
                            {autoUploadStatus === 'idle' || autoUploadStatus === 'pending' || autoUploadStatus === 'uploading' ? <Upload size={14} /> : null}
                          </span>
                          <span className="mmp-life-upload-auto-text">
                            {autoUploadStatusText || (
                              uploadFile
                                ? 'Waiting for auto upload.'
                                : (hasSavedCompoundsFile ? `Using saved file: ${activeCompoundsFileName}` : 'Ready for file upload.')
                            )}
                          </span>
                        </div>
                      </section>
                    </section>

                    <div
                      className={`panel-resizer ${isUploadWorkspaceResizing ? 'dragging' : ''}`}
                      role="separator"
                      aria-orientation="vertical"
                      aria-label="Resize upload and preview panels"
                      tabIndex={0}
                      onPointerDown={onUploadWorkspaceResizerPointerDown}
                      onKeyDown={onUploadWorkspaceResizerKeyDown}
                    />

                    <section className="panel subtle mmp-life-upload-preview-card mmp-life-upload-pane">
                      <div className="toolbar">
                        <div className="mmp-life-run-row mmp-life-preview-head-row">
                          <div className="mmp-life-preview-head-stats">
                            <span className="meta-chip">SMILES {summarizeCount(uploadPreviewStats.structureCoverage)} / {summarizeCount(uploadPreviewStats.totalRows)}</span>
                            <span className="meta-chip">Rows {summarizeCount(uploadPreviewTotalRows)}</span>
                            <span className="meta-chip">Cols {summarizeCount(uploadPreviewStats.columnCount)}</span>
                            {uploadPreviewTruncated ? (
                              <span className="meta-chip">Cap {summarizeCount(uploadPreviewRows.length)}</span>
                            ) : null}
                          </div>
                          <div className="mmp-life-preview-head-page">
                            <button
                              className="icon-btn mmp-life-preview-page-btn"
                              type="button"
                              disabled={uploadPreviewCurrentPage <= 1}
                              onClick={() => setUploadPreviewPage((prev) => Math.max(1, prev - 1))}
                              title="Previous page"
                            >
                              {'<'}
                            </button>
                            <span className="meta-chip">Page {uploadPreviewCurrentPage} / {uploadPreviewTotalPages}</span>
                            <button
                              className="icon-btn mmp-life-preview-page-btn"
                              type="button"
                              disabled={uploadPreviewCurrentPage >= uploadPreviewTotalPages}
                              onClick={() => setUploadPreviewPage((prev) => Math.min(uploadPreviewTotalPages, prev + 1))}
                              title="Next page"
                            >
                              {'>'}
                            </button>
                          </div>
                        </div>
                      </div>
                      {uploadPreviewRows.length === 0 ? (
                        <div className="mmp-life-upload-preview-empty muted small">
                          {uploadParsing
                            ? 'Loading saved preview...'
                            : (hasSavedCompoundsFile ? 'Saved file exists but preview is unavailable. Re-upload or retry.' : 'No saved preview. Select a file or upload once.')}
                        </div>
                      ) : (
                        <div className="table-wrap">
                          <table className="table mmp-life-upload-preview-table">
                            <thead>
                              <tr>
                                <th>2D</th>
                                {uploadPreviewColumns.map((col) => (
                                  <th
                                    key={`preview-col-${col}`}
                                    className={`mmp-life-mono${col === uploadPreviewSmilesColumn ? ' mmp-life-upload-col-smiles' : ''}`}
                                  >
                                    {col}
                                  </th>
                                ))}
                              </tr>
                            </thead>
                            <tbody>
                              {uploadPreviewPageRows.map((row, idx) => {
                                const smiles = uploadPreviewSmilesColumn ? readText(row[uploadPreviewSmilesColumn]) : '';
                                return (
                                  <tr key={`upload-preview-${uploadPreviewCurrentPage}-${idx}`}>
                                    <td className="mmp-life-upload-preview-2d">
                                      {smiles ? (
                                        <MemoLigand2DPreview smiles={smiles} width={152} height={96} />
                                      ) : (
                                        <span className="muted small">-</span>
                                      )}
                                    </td>
                                    {uploadPreviewColumns.map((col) => {
                                      const rawValue = readText(row[col]);
                                      let value = isMissingCellToken(rawValue) ? '-' : rawValue;
                                      const transform = activityTransformByColumn.get(col) || 'none';
                                      let transformed = false;
                                      if (value !== '-' && transform !== 'none') {
                                        try {
                                          const output = transformActivityValue(rawValue, transform);
                                          if (Number.isFinite(output)) {
                                            value = String(output);
                                            transformed = true;
                                          }
                                        } catch {
                                          // Keep raw preview if conversion fails for this cell.
                                        }
                                      }
                                      const classNameParts = ['mmp-life-mono'];
                                      if (col === uploadPreviewSmilesColumn) classNameParts.push('mmp-life-upload-cell-smiles');
                                      if (transformed) classNameParts.push('mmp-life-upload-cell-transformed');
                                      return (
                                        <td key={`preview-val-${uploadPreviewCurrentPage}-${idx}-${col}`} className={classNameParts.join(' ')}>
                                          {value}
                                        </td>
                                      );
                                    })}
                                  </tr>
                                );
                              })}
                            </tbody>
                          </table>
                        </div>
                      )}
                    </section>
                  </div>
                </div>
              </section>
            ) : null}

            {flowStep === 'mapping' && selectedBatch ? (
              <section className="panel">
                <div className="toolbar">
                  <span />
                  <span />
                </div>
                <section className="panel subtle mmp-life-activity-method-map">
                  <div className="toolbar">
                    <h4>
                      <Activity size={13} />
                      <span>Activity-Method Pairs</span>
                    </h4>
                    <button className="btn btn-ghost btn-compact" type="button" onClick={addActivityPair}>
                      <Plus size={14} />
                      Add Pair
                    </button>
                  </div>
                  <div className="project-compact-meta mmp-life-upload-meta">
                    <span className="meta-chip">
                      <Link2 size={12} />
                      {' '}
                      {structureColumn || 'No structure'}
                    </span>
                    <span className="meta-chip">Pairs {activityColumns.length}</span>
                    <span className="meta-chip">Methods {methods.length}</span>
                  </div>
                  {activityColumns.length === 0 ? (
                    <div className="muted small">No activity pair configured.</div>
                  ) : (
                    <div className="table-wrap">
                      <table className="table mmp-life-pairs-table mmp-life-pairs-table--mapping">
                        <colgroup>
                          <col className="mmp-life-col-activity" />
                          <col className="mmp-life-col-method" />
                          <col className="mmp-life-col-output" />
                          <col className="mmp-life-col-actions" />
                        </colgroup>
                        <thead>
                          <tr>
                            <th>Activity Column</th>
                            <th className="mmp-life-col-assay-method">
                              <div className="mmp-life-assay-method-head">
                                <span>Method</span>
                                {!hasRegisteredAssayMethod ? (
                                  <button
                                    className="btn btn-ghost btn-compact"
                                    type="button"
                                    onClick={openAssayMethodsModal}
                                  >
                                    <ListChecks size={14} />
                                    Register
                                  </button>
                                ) : null}
                              </div>
                            </th>
                            <th>Output Property</th>
                            <th>Actions</th>
                          </tr>
                        </thead>
                        <tbody>
                          {activityColumns.map((activityCol, idx) => {
                            const occupied = new Set(activityColumns.filter((_, rowIdx) => rowIdx !== idx));
                            const structureToken = readText(structureColumn);
                            const isMethodMissing = activityColumnsMissingMethod.has(readText(activityCol).toLowerCase());
                            return (
                              <tr key={`activity-method-${activityCol}-${idx}`}>
                                <td>
                                  <select
                                    className="mmp-life-activity-col-select"
                                    value={activityCol}
                                    onChange={(event) => updateActivityPairColumn(activityCol, event.target.value)}
                                  >
                                    <option value="">Select column</option>
                                    {uploadColumnOptions
                                      .filter((name) => {
                                        if (name === activityCol) return true;
                                        if (occupied.has(name)) return false;
                                        if (name === structureToken) return false;
                                        return true;
                                      })
                                      .map((name) => (
                                        <option key={`pair-col-${idx}-${name}`} value={name}>{name}</option>
                                      ))}
                                  </select>
                                </td>
                                <td className="mmp-life-cell-assay-method">
                                  <select
                                    className={`mmp-life-assay-method-select${isMethodMissing ? ' is-missing' : ''}`}
                                    value={readText(activityMethodMap[activityCol])}
                                    onChange={(event) => {
                                      const nextMethodId = readText(event.target.value);
                                      setActivityMethodMap((prev) => ({
                                        ...prev,
                                        [activityCol]: nextMethodId,
                                      }));
                                    }}
                                    disabled={!hasRegisteredAssayMethod}
                                    title={isMethodMissing ? 'Assign assay method for this activity column' : ''}
                                  >
                                    <option value="">{hasRegisteredAssayMethod ? 'None' : 'Register methods first'}</option>
                                    {methods.map((method) => {
                                      const methodId = readText(method.id);
                                      const labelToken = readText(method.key || method.name || method.id);
                                      const shortLabel = labelToken.length > 22 ? `${labelToken.slice(0, 22)}...` : labelToken;
                                      return (
                                        <option key={methodId} value={methodId}>
                                          {shortLabel}
                                        </option>
                                      );
                                    })}
                                  </select>
                                </td>
                                <td>{activityPairChips[idx]?.outputProperty || '-'}</td>
                                <td>
                                  <button
                                    className="icon-btn danger"
                                    type="button"
                                    title="Remove pair"
                                    aria-label="Remove pair"
                                    onClick={() => removeActivityPair(activityCol)}
                                  >
                                    <Trash2 size={14} />
                                  </button>
                                </td>
                              </tr>
                            );
                          })}
                        </tbody>
                      </table>
                    </div>
                  )}
                </section>
                <div className="mmp-life-upload-actions">
                  <div className="mmp-life-upload-actions-note">
                    <span className="muted small">
                      {activityColumnsMissingMethod.size > 0
                        ? 'Assign methods in highlighted rows before saving.'
                        : 'Click Save to persist mapping rules.'}
                    </span>
                  </div>
                </div>
              </section>
            ) : null}

            {flowStep === 'qa' && selectedBatch ? (
              <>
                <section className="panel">
                <div className="toolbar">
                  <span />
                  <span />
                </div>
                {selectedBatchApplyHistoryCount <= 0 ? (
                  <div className="muted small">
                    Check is dry-run only. Use Apply to persist assays into database.
                  </div>
                ) : null}
                {gateReasons.length > 0 ? (
                  <div className="alert warning">{gateReasons.join(' ')}</div>
                ) : null}
                <div className="mmp-life-check-layout">
                    <section className="panel subtle mmp-life-check-card">
                      <div className="mmp-life-check-card-head">
                        <h3>Compounds</h3>
                        <span className="badge">Upsert {summarizeCount(runtimeCheckOverview.compoundAnnotatedRows)}</span>
                      </div>
                      <div className="mmp-life-check-main-stat">
                        <strong>{summarizeCount(runtimeCheckOverview.compoundTotalRows)}</strong>
                        <span>Total Rows</span>
                      </div>
                      <div className="mmp-life-check-sub-stats">
                        <div className="mmp-life-check-sub-stat">
                          <span>Need Reindex</span>
                          <strong>{summarizeCount(runtimeCheckOverview.compoundReindexRows)}</strong>
                        </div>
                        <div className="mmp-life-check-sub-stat">
                          <span>No Reindex</span>
                          <strong>{summarizeCount(Math.max(0, runtimeCheckOverview.compoundAnnotatedRows - runtimeCheckOverview.compoundReindexRows))}</strong>
                        </div>
                      </div>
                    </section>
                    <section className="panel subtle mmp-life-check-card">
                      <div className="mmp-life-check-card-head">
                        <h3>Experiments</h3>
                        <span className="badge">Will Import {summarizeCount(runtimeCheckOverview.experimentImportableRows)}</span>
                      </div>
                      <div className="mmp-life-check-main-stat">
                        <strong>{summarizeCount(runtimeCheckOverview.experimentTotalRows)}</strong>
                        <span>Total Rows</span>
                      </div>
                      <div className="mmp-life-check-sub-stats mmp-life-check-sub-stats-4">
                        <div className="mmp-life-check-sub-stat">
                          <span>Update</span>
                          <strong>{summarizeCount(runtimeCheckOverview.experimentUpdateRows)}</strong>
                        </div>
                        <div className="mmp-life-check-sub-stat">
                          <span>Insert</span>
                          <strong>{summarizeCount(runtimeCheckOverview.experimentInsertRows)}</strong>
                        </div>
                        <div className="mmp-life-check-sub-stat">
                          <span>Unchanged</span>
                          <strong>{summarizeCount(runtimeCheckOverview.experimentNoopRows)}</strong>
                        </div>
                        <div className="mmp-life-check-sub-stat">
                          <span>Invalid</span>
                          <strong>{summarizeCount(runtimeCheckOverview.experimentInvalidRows)}</strong>
                        </div>
                      </div>
                      <div className="mmp-life-check-footnote">
                        Unmapped {summarizeCount(runtimeCheckOverview.experimentUnmappedRows)} · Unmatched {summarizeCount(runtimeCheckOverview.experimentUnmatchedRows)}
                      </div>
                    </section>
                  </div>
                </section>
              </>
            ) : null}
        </div>
      </div>
      )}

      {databaseAdminModalOpen ? (
        <div className="modal-mask" onClick={() => setDatabaseAdminModalOpen(false)}>
          <div className="modal modal-wide mmp-life-db-modal" onClick={(event) => event.stopPropagation()}>
            <div className="mmp-life-modal-head">
              <h2>Lead Opt Databases</h2>
              <button className="icon-btn" type="button" onClick={() => setDatabaseAdminModalOpen(false)} aria-label="Close database admin dialog">
                <X size={14} />
              </button>
            </div>
            <MmpDatabaseAdminPanel compact />
          </div>
        </div>
      ) : null}

      {assayMethodModalOpen ? (
        <div className="modal-mask" onClick={closeAssayMethodsModal}>
          <div className="modal modal-wide mmp-life-assay-modal" onClick={(event) => event.stopPropagation()}>
            <div className="mmp-life-modal-head">
              <h2>Assay Methods</h2>
              <div className="mmp-life-modal-actions">
                <button
                  className="btn btn-ghost btn-compact"
                  type="button"
                  onClick={() => {
                    if (methodEditorOpen) {
                      resetMethodEditorDraft();
                      setMethodEditorOpen(false);
                      return;
                    }
                    openCreateMethodEditor();
                  }}
                  disabled={saving || (!methodEditorOpen && !readText(assayMethodDatabaseId))}
                >
                  {!methodEditorOpen ? <Plus size={14} /> : null}
                  {methodEditorOpen ? 'Close Editor' : 'Add Method'}
                </button>
                <button className="icon-btn" type="button" onClick={closeAssayMethodsModal} aria-label="Close assay methods dialog">
                  <X size={14} />
                </button>
              </div>
            </div>
            <div className="mmp-life-method-scope-inline">
              <label className="mmp-life-method-scope-db">
                <span>Database</span>
                <select
                  className="mmp-life-method-scope-select"
                  value={assayMethodDatabaseId}
                  onChange={(event) => {
                    const nextDbId = readText(event.target.value);
                    setAssayMethodDatabaseId(nextDbId);
                    void loadAssayDatabaseProperties(nextDbId);
                    void loadAssayDatabaseMethodBindings(nextDbId);
                    void loadAssayMethodUsage(nextDbId);
                    void loadPendingSyncSummary(nextDbId);
                  }}
                >
                  <option value="">Select database</option>
                  {databases.map((item) => {
                    const id = readText(item.id);
                    const label = readText(item.label || item.schema || item.id) || id;
                    const state = getDatabaseBuildState(item);
                    return (
                      <option key={`assay-db-${id}`} value={id}>
                        {`${label} · ${state === 'ready' ? 'Ready' : 'Building'}`}
                      </option>
                    );
                  })}
                </select>
              </label>
              <div className="mmp-life-method-scope-meta muted small">
                {`State ${selectedAssayDatabaseStateLabel} · Props ${summarizeCount(assayDatabasePropertyNames.length)} · Pending ${summarizeCount(selectedAssayDatabasePendingSyncCount)} · ${assayPropertySummaryText}`}
              </div>
            </div>
            {methodEditorOpen ? (
              <section className="panel subtle mmp-life-method-editor-card">
                <form className="form-grid" onSubmit={createMethod}>
                  <div className="mmp-life-mini-grid">
                    <label className={`field ${methodFormErrors.key ? 'mmp-life-field-invalid' : ''}`}>
                      <span>Method key</span>
                      <input
                        value={methodKey}
                        onChange={(event) => {
                          setMethodKey(event.target.value);
                          if (methodFormErrors.key) {
                            setMethodFormErrors((prev) => ({ ...prev, key: false }));
                          }
                        }}
                        placeholder="ic50"
                      />
                      {methodFormErrors.key ? <span className="mmp-life-field-error">Method key is required.</span> : null}
                    </label>
                    <label className={`field ${methodFormErrors.name ? 'mmp-life-field-invalid' : ''}`}>
                      <span>Method name</span>
                      <input
                        value={methodName}
                        onChange={(event) => {
                          setMethodName(event.target.value);
                          if (methodFormErrors.name) {
                            setMethodFormErrors((prev) => ({ ...prev, name: false }));
                          }
                        }}
                        placeholder="IC50 inhibition assay"
                      />
                      {methodFormErrors.name ? <span className="mmp-life-field-error">Method name is required.</span> : null}
                    </label>
                    <label className={`field ${methodFormErrors.output_property ? 'mmp-life-field-invalid' : ''}`}>
                      <span>Output property</span>
                      <input
                        value={methodOutputProperty}
                        onChange={(event) => {
                          setMethodOutputProperty(event.target.value);
                          if (methodFormErrors.output_property) {
                            setMethodFormErrors((prev) => ({ ...prev, output_property: false }));
                          }
                        }}
                        placeholder="CYP3A4"
                      />
                      {methodFormErrors.output_property ? <span className="mmp-life-field-error">Output property is required.</span> : null}
                    </label>
                    <label className="field">
                      <span>Category</span>
                      <select value={methodCategory} onChange={(event) => setMethodCategory(event.target.value)}>
                        {ASSAY_CATEGORY_OPTIONS.map((option) => (
                          <option key={option} value={option}>{option}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <div className="mmp-life-mini-grid">
                    <label className="field">
                      <span>Stored unit</span>
                      <input value={methodInputUnit} onChange={(event) => setMethodInputUnit(event.target.value)} placeholder="uM / nM / pIC50" />
                    </label>
                    <label className="field">
                      <span>Display transform</span>
                      <select
                        value={methodDisplayTransform}
                        onChange={(event) => setMethodDisplayTransform(normalizeActivityTransform(event.target.value))}
                      >
                        {ACTIVITY_TRANSFORM_OPTIONS.map((option) => (
                          <option key={`method-display-${option.value}`} value={option.value}>{option.label}</option>
                        ))}
                      </select>
                    </label>
                  </div>
                  <label className="field">
                    <span>Description</span>
                    <input value={methodDescription} onChange={(event) => setMethodDescription(event.target.value)} placeholder="Optional" />
                  </label>
                  {methodFormMessage ? <div className="mmp-life-field-error">{methodFormMessage}</div> : null}
                  <div className="mmp-life-method-form-actions">
                    <button className="btn btn-ghost" type="submit" disabled={saving}>
                      {editingMethodId ? 'Update Method' : 'Save Method'}
                    </button>
                    <button className="btn btn-ghost" type="button" onClick={resetMethodEditorDraft}>
                      Reset
                    </button>
                  </div>
                </form>
              </section>
            ) : null}
            <div className="table-wrap">
              <table className="table">
                <thead>
                  <tr>
                    <th>Key</th>
                    <th>Name</th>
                    <th>Output Property</th>
                    <th>Data Rows</th>
                    <th>Stored Unit</th>
                    <th>Display Transform</th>
                    <th>Category</th>
                    <th>Actions</th>
                  </tr>
                </thead>
                <tbody>
                  {assayMethodsForSelectedDatabase.length === 0 ? (
                    <tr>
                      <td colSpan={8} className="muted">No assay method for selected database.</td>
                    </tr>
                  ) : (
                    assayMethodsForSelectedDatabase.map((item) => {
                      const id = readText(item.id);
                      const dataCount = assayMethodDataCountById.get(id);
                      const effectiveDisplayTransform = normalizeActivityTransform(
                        readText(item.display_transform || assayMethodDisplayTransformById[id] || 'none')
                      );
                      const displayLabel = ACTIVITY_TRANSFORM_OPTIONS.find(
                        (option) => option.value === effectiveDisplayTransform
                      )?.label || 'Raw';
                      const inputUnit = readText(item.input_unit);
                      const outputUnit = readText(item.output_unit);
                      const storedUnitText =
                        inputUnit && outputUnit && inputUnit !== outputUnit
                          ? `${inputUnit} -> ${outputUnit}`
                          : outputUnit || inputUnit || '-';
                      return (
                        <tr key={id}>
                          <td>{readText(item.key) || '-'}</td>
                          <td>{readText(item.name) || '-'}</td>
                          <td>{readText(item.output_property) || '-'}</td>
                          <td>{dataCount === undefined ? '-' : summarizeCount(dataCount)}</td>
                          <td>{storedUnitText}</td>
                          <td>{displayLabel}</td>
                          <td>{readText(item.category) || '-'}</td>
                          <td className="mmp-life-method-actions-cell">
                            <div className="mmp-life-method-actions-row">
                              <button className="icon-btn" type="button" onClick={() => startEditMethod(item)} title="Edit method">
                                <Pencil size={14} />
                              </button>
                              <button className="icon-btn danger" type="button" onClick={() => void removeMethod(id)} title="Delete method">
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
          </div>
        </div>
      ) : null}
    </div>
  );
}
