import type { CSSProperties } from 'react';
import type {
  AffinityScoringMode,
  ApiTokenUsageDaily,
  InputComponent,
  PredictionProperties,
  Project,
  ProteinModification,
  ProteinModificationTerminal
} from '../types/models';
import { createInputComponent, randomId } from '../utils/projectInputs';

export type UsageWindow = '7d' | '30d' | '90d' | 'all';
export type ProjectStatsWorkflowFilter = 'all' | 'prediction' | 'virtual_screening' | 'affinity' | 'lead_optimization';
export type ProjectStatsSort = 'calls_desc' | 'calls_asc' | 'success_desc' | 'success_asc' | 'last_desc' | 'last_asc';
export type BuilderWorkflowKey = 'prediction' | 'virtual_screening' | 'affinity' | 'lead_optimization';
export type PredictionBackend = 'boltz' | 'alphafold3' | 'protenix' | 'nesso';
export type AffinityBackend = 'boltz';

export interface ProjectStatsRow {
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

export interface UsageSummary {
  total: number;
  success: number;
  errors: number;
  successRate: number;
  lastEventAt: string | null;
  lastEventTs: number;
}

export interface CommandHistoryEntry {
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

export interface YamlProteinTemplateConfig {
  path: string;
  format: 'auto' | 'pdb' | 'cif';
  templateChain: string;
  targetChains: string;
}

export type ApiBuilderGridStyle = CSSProperties & {
  '--api-builder-left-width'?: string;
  '--api-yaml-left-width'?: string;
};

export const TOKEN_PAGE_SIZE = 8;
export const EVENT_PAGE_SIZE = 20;
export const DAILY_USAGE_PAGE_SIZE = 30;
export const PROJECT_STATS_PAGE_SIZE = 8;
export const COMMAND_HISTORY_LIMIT = 12;
export const COMMAND_HISTORY_STORAGE_KEY = 'vbio_api_command_history_v1';
export const LEAD_OPT_API_ACCESS_ENABLED: boolean = false;
export const AFFINITY_TARGET_UPLOAD_COMPONENT_ID = '__affinity_target_upload__';
export const AFFINITY_LIGAND_UPLOAD_COMPONENT_ID = '__affinity_ligand_upload__';
export const EMPTY_PREDICTION_PROPERTIES: PredictionProperties = {
  affinity: false,
  target: null,
  ligand: null,
  binder: null
};

export function normalizePredictionChainValue(value: unknown): string | null {
  const chainId = String(value || '').trim();
  return chainId || null;
}

export function isSamePredictionProperties(
  a: PredictionProperties | null | undefined,
  b: PredictionProperties | null | undefined
): boolean {
  return (
    Boolean(a?.affinity) === Boolean(b?.affinity) &&
    normalizePredictionChainValue(a?.target) === normalizePredictionChainValue(b?.target) &&
    normalizePredictionChainValue(a?.ligand) === normalizePredictionChainValue(b?.ligand) &&
    normalizePredictionChainValue(a?.binder) === normalizePredictionChainValue(b?.binder)
  );
}

export function normalizeUsageWindow(value: string | null | undefined): UsageWindow {
  if (value === '7d' || value === '30d' || value === '90d' || value === 'all') return value;
  return '90d';
}

export function normalizeProjectStatsWorkflowFilter(value: string | null | undefined): ProjectStatsWorkflowFilter {
  if (value === 'prediction' || value === 'virtual_screening' || value === 'affinity' || value === 'lead_optimization' || value === 'all') return value;
  return 'all';
}

export function normalizeProjectStatsSort(value: string | null | undefined): ProjectStatsSort {
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

export function normalizePredictionBackend(value: string | null | undefined): PredictionBackend {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'alphafold3') return 'alphafold3';
  if (normalized === 'protenix') return 'protenix';
  if (normalized === 'nesso' || normalized === 'nesso1' || normalized === 'nesso-1') return 'nesso';
  return 'boltz';
}

export function normalizeAffinityBackend(_value: string | null | undefined): AffinityBackend {
  return 'boltz';
}

export function normalizeAffinityBuilderMode(value: unknown): AffinityScoringMode {
  const normalized = String(value || '').trim().toLowerCase();
  if (normalized === 'pose' || normalized === 'refine' || normalized === 'interface') {
    return normalized;
  }
  return 'score';
}

export function randomAlphaNum(length: number): string {
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

export function shortUuidLike(): string {
  const raw = typeof globalThis.crypto?.randomUUID === 'function'
    ? globalThis.crypto.randomUUID().replace(/-/g, '')
    : randomAlphaNum(16).toLowerCase();
  return `token-${raw.slice(0, 8)}`;
}

export function formatIso(ts: string | null | undefined): string {
  if (!ts) return '-';
  const t = Date.parse(ts);
  if (!Number.isFinite(t)) return ts;
  return new Date(t).toLocaleString();
}

export function computeUsageSummaryFromDaily(rows: ApiTokenUsageDaily[], lastEventAt?: string | null): UsageSummary {
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

export function usageSince(window: UsageWindow): string | undefined {
  if (window === 'all') return undefined;
  const days = window === '7d' ? 7 : window === '30d' ? 30 : 90;
  return new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
}

export function normalizeBaseUrl(value: string): string {
  const text = String(value || '').trim();
  if (!text) return '';
  return text.replace(/\/$/, '');
}

export function escapeForDoubleQuotedShell(value: string): string {
  return String(value || '')
    .replace(/\\/g, '\\\\')
    .replace(/"/g, '\\"');
}

export function extractFileNameFromPath(pathRaw: string): string {
  const normalized = String(pathRaw || '').trim().replace(/\\/g, '/');
  if (!normalized) return '';
  const segments = normalized.split('/').filter(Boolean);
  return segments.length > 0 ? segments[segments.length - 1] : '';
}

export function inferTemplateFormat(pathRaw: string, selected: 'auto' | 'pdb' | 'cif'): 'pdb' | 'cif' {
  if (selected === 'pdb' || selected === 'cif') return selected;
  const lower = extractFileNameFromPath(pathRaw).toLowerCase();
  if (lower.endsWith('.pdb')) return 'pdb';
  return 'cif';
}

export function normalizeChainId(value: string, fallback: string): string {
  const cleaned = String(value || '').trim();
  return cleaned || fallback;
}

export function createYamlBuilderComponent(type: InputComponent['type'] = 'protein'): InputComponent {
  const component = createInputComponent(type);
  if (component.type === 'ligand') {
    component.inputMethod = 'smiles';
  }
  return component;
}

export const BUILDER_CUSTOM_RESIDUE_SCAFFOLD = 'N[C@@H](C)C(=O)O';
export const BUILDER_BUILT_IN_MODIFICATIONS = [
  { ccd: 'AIB', label: 'AIB', baseResidue: 'A' },
  { ccd: 'NLE', label: 'NLE', baseResidue: 'L' },
  { ccd: 'NVA', label: 'NVA', baseResidue: 'V' },
  { ccd: 'ORN', label: 'ORN', baseResidue: 'K' },
  { ccd: 'CIT', label: 'CIT', baseResidue: 'R' },
  { ccd: 'MSE', label: 'MSE', baseResidue: 'M' },
  { ccd: 'SEC', label: 'SEC', baseResidue: 'C' },
  { ccd: 'SEP', label: 'SEP', baseResidue: 'S' },
  { ccd: 'TPO', label: 'TPO', baseResidue: 'T' },
  { ccd: 'PTR', label: 'PTR', baseResidue: 'Y' },
  { ccd: 'MLY', label: 'MLY', baseResidue: 'K' },
  { ccd: 'DAL', label: 'DAL', baseResidue: 'A' }
];

export function normalizeBuilderCcd(value: string): string {
  return String(value || '').replace(/[^A-Za-z0-9_-]/g, '').toUpperCase().slice(0, 12);
}

export function hashBuilderText(value: string): string {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
  }
  return (hash >>> 0).toString(36).toUpperCase().slice(0, 5);
}

export function buildBuilderCustomCcd(componentId: string, position: number, smiles = ''): string {
  return `U${Math.max(1, Math.floor(position)).toString(36).toUpperCase()}${hashBuilderText(`${componentId}:${position}:${smiles}`)}`.slice(0, 5);
}

export function cleanProteinSequence(sequence: string): string {
  return String(sequence || '').replace(/\s+/g, '').toUpperCase();
}

export function builderSequenceLength(sequence: string): number {
  return cleanProteinSequence(sequence).length;
}

export function clampBuilderModPosition(value: number, sequence: string): number {
  const max = Math.max(1, builderSequenceLength(sequence) || 1);
  if (!Number.isFinite(value) || value < 1) return 1;
  return Math.min(max, Math.floor(value));
}

export function builderResidueAt(sequence: string, position: number): string {
  return cleanProteinSequence(sequence)[Math.max(0, position - 1)] || '';
}

export function builderPositionForTerminal(terminal: ProteinModificationTerminal, position: number, sequence: string): number {
  if (terminal === 'n_term') return 1;
  if (terminal === 'c_term') return Math.max(1, builderSequenceLength(sequence) || 1);
  return clampBuilderModPosition(position, sequence);
}

export function builderTerminalForPosition(position: number, sequence: string, terminal?: ProteinModificationTerminal): ProteinModificationTerminal {
  if (terminal === 'n_term' || terminal === 'c_term') return terminal;
  if (Math.floor(Number(position)) === 1) return 'n_term';
  if (builderSequenceLength(sequence) > 0 && Math.floor(Number(position)) === builderSequenceLength(sequence)) return 'c_term';
  return 'internal';
}

export function createBuilderModification(component: InputComponent): ProteinModification {
  const position = clampBuilderModPosition(1, component.sequence);
  const residue = builderResidueAt(component.sequence, position);
  const builtin = BUILDER_BUILT_IN_MODIFICATIONS.find((item) => item.baseResidue === residue) || BUILDER_BUILT_IN_MODIFICATIONS[0];
  return {
    id: randomId(),
    position,
    terminal: builderTerminalForPosition(position, component.sequence),
    baseResidue: residue || builtin.baseResidue,
    ccd: builtin.ccd,
    inputMethod: 'ccd',
    label: builtin.label,
    customEditorCollapsed: true
  };
}

export function isAffinityUploadComponent(value: unknown): boolean {
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

export function readCommandHistoryFromStorage(): CommandHistoryEntry[] {
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
        const normalizedWorkflow = String(record.workflow || legacyTemplate || '').trim().toLowerCase();
        const workflow: BuilderWorkflowKey = normalizedWorkflow === 'affinity'
          ? 'affinity'
          : (normalizedWorkflow === 'lead_optimization' || normalizedWorkflow === 'lead optimization' || normalizedWorkflow === 'leadopt')
            ? 'lead_optimization'
            : 'prediction';
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

export function fallbackCopyText(text: string): boolean {
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
