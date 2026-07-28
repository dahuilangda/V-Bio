import type { ProjectTask } from '../../types/models';
import { parseVirtualScreeningInput } from '../../utils/virtualScreening';
import { backendLabel } from './taskPresentation';
import type { TaskConfidenceMetrics } from './taskListTypes';

interface VirtualScreeningPredictionEntry {
  key: string;
  hitId: string;
  backend: string;
  state: string;
  ligandPlddt: number | null;
  interfaceMetricValue: number | null;
  interfaceMetricLabel: 'IPSAE' | 'ipTM';
  pairIptm: number | null;
  pairPae: number | null;
  updatedAt: number;
  ligandRenderSmiles: string;
  ligandRenderAtomPlddts: number[] | null;
}

export interface VirtualScreeningTaskRowSummary {
  metrics: TaskConfidenceMetrics;
  ligandSmiles: string;
  ligandRenderSmiles: string;
  ligandRenderAtomPlddts: number[] | null;
  modeValue: string;
}

function asObject(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};
}

function finiteNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === '' || typeof value === 'boolean') return null;
  const number = Number(value);
  return Number.isFinite(number) ? number : null;
}

function normalizedProbability(value: number | null): number | null {
  if (value === null) return null;
  return value > 1 && value <= 100 ? value / 100 : value;
}

function normalizedPlddt(value: number | null): number | null {
  if (value === null) return null;
  return value >= 0 && value <= 1 ? value * 100 : value;
}

function normalizeBackend(value: unknown): string {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'af3') return 'alphafold3';
  return token;
}

function normalizeHitToken(value: unknown): string {
  return String(value || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64);
}

function readVirtualScreeningOptions(task: ProjectTask): Record<string, unknown> {
  const properties = asObject(task.properties);
  return asObject(properties.__vbio_input_options_v1 ?? properties.vbio_input_options_v1);
}

function readPredictionRecordsValue(task: ProjectTask): unknown {
  const options = readVirtualScreeningOptions(task);
  return options.virtualScreeningPredictions ?? options.virtual_screening_predictions ?? null;
}

function readPredictionEntries(task: ProjectTask): VirtualScreeningPredictionEntry[] {
  const rawRecords = readPredictionRecordsValue(task);
  if (!rawRecords || typeof rawRecords !== 'object' || Array.isArray(rawRecords)) return [];

  const entries: VirtualScreeningPredictionEntry[] = [];
  for (const [key, value] of Object.entries(rawRecords as Record<string, unknown>)) {
    const record = asObject(value);
    const keyParts = key.split('::');
    const atomPlddtsRaw = record.ligandRenderAtomPlddts ?? record.ligand_render_atom_plddts;
    const ligandRenderAtomPlddts = Array.isArray(atomPlddtsRaw)
      ? atomPlddtsRaw.map(finiteNumber).filter((item): item is number => item !== null)
      : null;
    entries.push({
      key,
      hitId: normalizeHitToken(keyParts.length >= 2 ? keyParts[1] : ''),
      backend: normalizeBackend(record.backend || keyParts[0]),
      state: String(record.state || '').trim().toUpperCase(),
      ligandPlddt: finiteNumber(record.ligandPlddt ?? record.ligand_plddt),
      interfaceMetricValue: finiteNumber(
        record.interfaceMetricValue ?? record.interface_metric_value
      ),
      interfaceMetricLabel:
        String(record.interfaceMetricLabel ?? record.interface_metric_label) === 'ipTM'
          ? 'ipTM'
          : 'IPSAE',
      pairIptm: finiteNumber(record.pairIptm ?? record.pair_iptm),
      pairPae: finiteNumber(record.pairPae ?? record.pair_pae),
      updatedAt: finiteNumber(record.updatedAt ?? record.updated_at) ?? 0,
      ligandRenderSmiles: String(
        record.ligandRenderSmiles ?? record.ligand_render_smiles ?? ''
      ).trim(),
      ligandRenderAtomPlddts:
        ligandRenderAtomPlddts && ligandRenderAtomPlddts.length > 0
          ? ligandRenderAtomPlddts
          : null
    });
  }
  return entries;
}

function representativePrediction(task: ProjectTask): VirtualScreeningPredictionEntry | null {
  const successes = readPredictionEntries(task).filter((entry) => entry.state === 'SUCCESS');
  successes.sort((left, right) => {
    const leftScore = left.interfaceMetricValue ?? left.pairIptm ?? Number.NEGATIVE_INFINITY;
    const rightScore = right.interfaceMetricValue ?? right.pairIptm ?? Number.NEGATIVE_INFINITY;
    if (leftScore !== rightScore) return rightScore - leftScore;
    return right.updatedAt - left.updatedAt;
  });
  return successes[0] || null;
}

function affinityCompounds(task: ProjectTask): Record<string, unknown>[] {
  const compounds = asObject(task.affinity).compounds;
  return Array.isArray(compounds)
    ? compounds.map(asObject).filter((item) => Object.keys(item).length > 0)
    : [];
}

function compoundSmiles(compound: Record<string, unknown>): string {
  return String(
    compound.smiles ??
    compound.canonical_smiles ??
    compound.canonicalSmiles ??
    ''
  ).trim();
}

function compoundMatchesHit(compound: Record<string, unknown>, hitId: string): boolean {
  if (!hitId) return false;
  return ['id', 'record_id', 'recordId', 'source_id', 'sourceId', 'name']
    .some((field) => normalizeHitToken(compound[field]) === hitId);
}

function readInputCompounds(task: ProjectTask) {
  const options = readVirtualScreeningOptions(task);
  const rawInput = String(
    options.virtualScreeningInput ?? options.virtual_screening_input ?? ''
  );
  if (!rawInput.trim()) return [];
  return parseVirtualScreeningInput(rawInput).compounds;
}

function resolveRepresentativeSmiles(
  task: ProjectTask,
  prediction: VirtualScreeningPredictionEntry | null
): string {
  if (prediction?.ligandRenderSmiles) return prediction.ligandRenderSmiles;

  const compounds = affinityCompounds(task);
  if (prediction?.hitId) {
    const matchedAffinity = compounds.find((compound) => compoundMatchesHit(compound, prediction.hitId));
    const matchedAffinitySmiles = matchedAffinity ? compoundSmiles(matchedAffinity) : '';
    if (matchedAffinitySmiles) return matchedAffinitySmiles;

    const matchedInput = readInputCompounds(task).find(
      (compound) => normalizeHitToken(compound.id) === prediction.hitId
    );
    if (matchedInput?.smiles) return matchedInput.smiles;
  }

  const rankedCompounds = compounds
    .map((compound, index) => ({
      compound,
      rank: finiteNumber(compound.rank) ?? index + 1
    }))
    .sort((left, right) => left.rank - right.rank);
  for (const entry of rankedCompounds) {
    const smiles = compoundSmiles(entry.compound);
    if (smiles) return smiles;
  }
  return readInputCompounds(task)[0]?.smiles || '';
}

export function readVirtualScreeningRuntimeSignature(task: ProjectTask): string {
  return JSON.stringify(readPredictionRecordsValue(task));
}

export function readVirtualScreeningTaskRowSummary(
  task: ProjectTask
): VirtualScreeningTaskRowSummary {
  const prediction = representativePrediction(task);
  const ligandSmiles = resolveRepresentativeSmiles(task, prediction);
  const interfaceMetricValue = normalizedProbability(
    prediction?.interfaceMetricValue ?? null
  );
  const interfaceMetricLabel = prediction?.interfaceMetricLabel || 'IPSAE';
  const pairIptm = normalizedProbability(prediction?.pairIptm ?? null);
  const iptm =
    pairIptm ??
    (interfaceMetricLabel === 'ipTM' ? interfaceMetricValue : null);

  return {
    metrics: {
      plddt: normalizedPlddt(prediction?.ligandPlddt ?? null),
      ipsae: interfaceMetricLabel === 'IPSAE' ? interfaceMetricValue : null,
      iptm,
      interfaceMetricValue,
      interfaceMetricLabel,
      interfaceMetricSource: prediction
        ? interfaceMetricLabel === 'IPSAE'
          ? 'ipsae'
          : 'iptm'
        : 'none',
      pae: prediction?.pairPae ?? null
    },
    ligandSmiles,
    ligandRenderSmiles: ligandSmiles,
    ligandRenderAtomPlddts: prediction?.ligandRenderAtomPlddts ?? null,
    modeValue: prediction?.backend ? backendLabel(prediction.backend) : 'Screening'
  };
}
