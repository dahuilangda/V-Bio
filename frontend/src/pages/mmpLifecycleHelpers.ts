import type {
  MmpLifecycleBatch,
  MmpLifecycleDatabaseItem,
  MmpLifecycleMethod,
} from '../api/backendApi';

export function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

export function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

export function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value).trim();
}

export function isMissingCellToken(value: unknown): boolean {
  const token = readText(value);
  if (!token) return true;
  const upper = token.toUpperCase();
  return upper === '*' || upper === 'NA' || upper === 'N/A' || upper === 'NAN' || upper === 'NULL' || upper === 'NONE' || upper === '-';
}

export function readNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const num = Number(value);
    if (Number.isFinite(num)) return num;
  }
  return null;
}

export function normalizeBatchStatusToken(value: unknown): string {
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

export type ActivityTransform =
  | 'none'
  | 'to_pic50_from_nm'
  | 'to_pic50_from_um'
  | 'to_ic50_nm_from_pic50'
  | 'to_ic50_um_from_pic50'
  | 'log10'
  | 'neg_log10'
  | 'from_log10'
  | 'from_neg_log10';

export interface MappingDraft {
  id: string;
  source_property: string;
  mmp_property: string;
  method_id: string;
  value_transform: ActivityTransform;
  notes: string;
}

export interface MethodFormErrors {
  key: boolean;
  name: boolean;
  output_property: boolean;
}

export interface CheckOverview {
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

export type BatchSortKey = 'updated_at' | 'name' | 'status' | 'selected_database_id';
export type SortDirection = 'asc' | 'desc';
export type BatchRunScope = 'auto' | 'compounds' | 'properties' | 'both';

export type FlowStep = 'batch' | 'upload' | 'mapping' | 'qa';

export const FLOW_STEPS: Array<{ key: FlowStep; label: string }> = [
  { key: 'batch', label: 'Batch' },
  { key: 'upload', label: 'Upload' },
  { key: 'mapping', label: 'Mapping' },
  { key: 'qa', label: 'Check' },
];

export const BATCH_RUN_SCOPE_OPTIONS: Array<{ value: BatchRunScope; label: string }> = [
  { value: 'auto', label: 'Auto' },
  { value: 'compounds', label: 'Compounds' },
  { value: 'properties', label: 'Props' },
  { value: 'both', label: 'Both' },
];

export const BULK_APPLY_RELAXED_POLICY: Record<string, unknown> = {
  max_compound_invalid_smiles_rows: 1_000_000_000,
  max_experiment_invalid_rows: 1_000_000_000,
  max_unmapped_property_rows: 1_000_000_000,
  max_unmatched_compound_rows: 1_000_000_000,
  require_check_for_selected_database: false,
  require_approved_status: false,
  require_importable_experiment_rows: false,
  require_importable_compound_rows: false,
};

export const UPLOAD_PREVIEW_ROW_CAP = 500;
export const UPLOAD_PREVIEW_PAGE_SIZE = 8;
export const ASSAY_METHODS_PAGE_SIZE = 10;

export const ASSAY_CATEGORY_OPTIONS = [
  'Binding',
  'Functional',
  'ADME',
  'PK/PD',
  'Safety',
  'Cell-based',
  'In vivo',
  'Other',
];

export const ACTIVITY_TRANSFORM_OPTIONS: Array<{ value: ActivityTransform; label: string }> = [
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

export function summarizeCount(value: unknown): string {
  const num = readNumber(value);
  if (num === null) return '-';
  return Math.trunc(num).toLocaleString();
}

export function asNonNegativeInt(value: unknown): number {
  const num = readNumber(value);
  if (num === null) return 0;
  return Math.max(0, Math.trunc(num));
}

export function readActionCount(summary: Record<string, unknown>, action: string): number {
  return asNonNegativeInt(asRecord(summary.action_counts)[action]);
}

export function buildCheckOverview(compoundSummary: Record<string, unknown>, experimentSummary: Record<string, unknown>): CheckOverview {
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

export type DatabaseBuildState = 'ready' | 'building' | 'failed';

export function getDatabaseBuildState(item: MmpLifecycleDatabaseItem): DatabaseBuildState {
  const status = readText(item.status).toLowerCase();
  if (status === 'ready') return 'ready';
  if (status === 'failed') return 'failed';
  if (status === 'building') return 'building';
  const stats = asRecord(item.stats);
  const compounds = readNumber(stats.compounds);
  const rules = readNumber(stats.rules);
  const pairs = readNumber(stats.pairs);
  return compounds !== null && rules !== null && pairs !== null ? 'ready' : 'building';
}

export function getDatabaseBuildStateLabel(item: MmpLifecycleDatabaseItem): string {
  const state = getDatabaseBuildState(item);
  if (state === 'ready') return 'Ready';
  if (state === 'failed') return 'Failed';
  return 'Building';
}

export function getDatabaseBuildProgressLabel(item: MmpLifecycleDatabaseItem): string {
  const progress = asRecord(item.build_progress);
  const shardCount = readNumber(progress.shard_count);
  const mergedShardCount = readNumber(progress.merged_shard_count);
  if (shardCount !== null && shardCount > 0 && mergedShardCount !== null) {
    return `Merge ${Math.min(shardCount, Math.max(0, Math.trunc(mergedShardCount)))}/${Math.trunc(shardCount)} shards`;
  }
  const state = getDatabaseBuildState(item);
  if (state === 'failed') {
    return readText(item.status_message) || 'Build failed';
  }
  return '';
}

export function hasBatchFileMeta(fileMeta: unknown): boolean {
  const meta = asRecord(fileMeta);
  return Boolean(
    readText(meta.path)
    || readText(meta.path_rel)
    || readText(meta.stored_name)
    || readText(meta.original_name)
    || readText(meta.uploaded_at)
  );
}

export function summarizeBatchImportMode(importCompounds: boolean, importExperiments: boolean): string {
  if (importCompounds && importExperiments) return 'Compounds + Props';
  if (importCompounds) return 'Compounds';
  if (importExperiments) return 'Props';
  return 'Nothing';
}

export function isBatchRuntimeActiveToken(value: unknown): boolean {
  const token = readText(value).toLowerCase();
  return token === 'queued' || token === 'queue' || token === 'running' || token === 'deleting';
}

export function resolveBatchRunPlan(
  batch: MmpLifecycleBatch | null,
  scope: BatchRunScope,
): {
  importCompounds: boolean;
  importExperiments: boolean;
  availableCompounds: boolean;
  availableExperiments: boolean;
  label: string;
  issue: string;
} {
  const files = asRecord(batch?.files);
  const availableCompounds = hasBatchFileMeta(files.compounds);
  const availableExperiments = hasBatchFileMeta(files.experiments);
  let importCompounds = false;
  let importExperiments = false;
  let issue = '';

  if (scope === 'auto') {
    importCompounds = availableCompounds;
    importExperiments = availableExperiments;
  } else if (scope === 'compounds') {
    importCompounds = availableCompounds;
    if (!availableCompounds) issue = 'Compounds-only run requires an uploaded compounds file.';
  } else if (scope === 'properties') {
    importExperiments = availableExperiments;
    if (!availableExperiments) issue = 'Props-only run requires an uploaded experiments/property file.';
  } else {
    importCompounds = availableCompounds;
    importExperiments = availableExperiments;
    if (!availableCompounds || !availableExperiments) {
      issue = 'Both run requires both compounds and props files.';
    }
  }

  if (!issue && !importCompounds && !importExperiments) {
    issue = 'Nothing to run. Upload compounds and/or props first.';
  }

  return {
    importCompounds,
    importExperiments,
    availableCompounds,
    availableExperiments,
    label: summarizeBatchImportMode(importCompounds, importExperiments),
    issue,
  };
}

export function splitDelimitedLine(line: string, delimiter: string): string[] {
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

export function dedupeColumns(columns: string[]): string[] {
  const unique = new Set<string>();
  for (const item of columns) {
    const token = item.trim();
    if (!token) continue;
    unique.add(token);
  }
  return Array.from(unique);
}

export function normalizeMappingRowsWithMethods(rows: MappingDraft[], methods: MmpLifecycleMethod[]): MappingDraft[] {
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

export function dedupeMappingRows(rows: MappingDraft[]): MappingDraft[] {
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

export function detectDelimiter(headerLine: string, fileName: string): string {
  const lower = fileName.toLowerCase();
  if (lower.endsWith('.tsv')) return '\t';
  const tabCount = (headerLine.match(/\t/g) || []).length;
  const commaCount = (headerLine.match(/,/g) || []).length;
  if (tabCount > commaCount) return '\t';
  return ',';
}

export function pickColumnCandidate(headers: string[], current: string, hints: string[], fallback = ''): string {
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

export function toTsvCell(value: string): string {
  if (/["\t\r\n]/.test(value)) {
    return `"${value.replace(/"/g, '""')}"`;
  }
  return value;
}

export function parseConfiguredColumns(value: unknown): string[] {
  if (Array.isArray(value)) return value.map((item) => readText(item)).filter(Boolean);
  return readText(value)
    .split(/[,\t|]/g)
    .map((item) => item.trim())
    .filter(Boolean);
}

export interface ParsedUploadTable {
  headers: string[];
  rows: Array<Record<string, string>>;
  totalRows: number;
  previewTruncated: boolean;
  columnNonEmptyCounts: Record<string, number>;
  columnNumericCounts: Record<string, number>;
  columnPositiveNumericCounts: Record<string, number>;
}

export function normalizeActivityTransform(value: string): ActivityTransform {
  const token = readText(value) as ActivityTransform;
  if (ACTIVITY_TRANSFORM_OPTIONS.some((item) => item.value === token)) return token;
  return 'none';
}

export function readBatchActivityConfig(batch: MmpLifecycleBatch | null): {
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

export function buildUploadExecutionSignature(
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

export function transformActivityValue(raw: string, transform: ActivityTransform): number {
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

export function inferDisplayUnit(
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

export function methodCompletenessScore(method: MmpLifecycleMethod): number {
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

export function dedupeLifecycleMethods(rows: MmpLifecycleMethod[]): MmpLifecycleMethod[] {
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
