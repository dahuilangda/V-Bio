import type { InputComponent } from '../../types/models';
import { chainKeysMatch, normalizeChainKey } from './projectConfidence';
import { readFirstNonEmptyStringMetric, readStringListMetric, splitChainTokens } from './projectMetrics';

export interface ResultComponentOption {
  id: string;
  type: InputComponent['type'];
  sequence: string;
  chainId: string | null;
  isSmiles: boolean;
  label: string;
}

function resolveChainCandidate(
  candidate: string,
  resultChainInfoById: Map<string, unknown>,
  knownChainIdByKey: Map<string, string>,
  knownChainIds: string[],
  resultComponentOptions: ResultComponentOption[]
): string | null {
  const raw = String(candidate || '').trim();
  if (!raw) return null;
  const direct = resultChainInfoById.has(raw) ? raw : null;
  if (direct) return direct;

  const byKnown = knownChainIdByKey.get(normalizeChainKey(raw));
  if (byKnown) return byKnown;

  const byComponent = resultComponentOptions.find((item) => item.id === raw);
  if (byComponent?.chainId) return byComponent.chainId;

  const normalized = raw.toUpperCase();
  const byNormalized = resultComponentOptions.find((item) => String(item.chainId || '').toUpperCase() === normalized);
  if (byNormalized?.chainId) return byNormalized.chainId;

  for (const chainId of knownChainIds) {
    if (chainKeysMatch(chainId, normalized) || chainKeysMatch(normalized, chainId)) {
      return chainId;
    }
  }
  return null;
}

function buildKnownChainMaps(resultChainIds: string[], confidenceData: Record<string, unknown> | null): {
  knownChainIdByKey: Map<string, string>;
  knownChainIds: string[];
} {
  const confidenceChainIds = readStringListMetric(confidenceData, ['chain_ids']);
  const knownChainIdByKey = new Map<string, string>();
  for (const chainId of [...resultChainIds, ...confidenceChainIds]) {
    const key = normalizeChainKey(chainId);
    if (!key || knownChainIdByKey.has(key)) continue;
    knownChainIdByKey.set(key, chainId);
  }
  return {
    knownChainIdByKey,
    knownChainIds: Array.from(knownChainIdByKey.values())
  };
}

export function resolveSelectedResultTargetChainId(params: {
  resultPairPreference: Record<string, unknown> | null | undefined;
  resultChainInfoById: Map<string, unknown>;
  resultComponentOptions: ResultComponentOption[];
  resultChainIds: string[];
  affinityData: Record<string, unknown> | null;
  confidenceData: Record<string, unknown> | null;
}): string | null {
  const { resultPairPreference, resultChainInfoById, resultComponentOptions, resultChainIds, affinityData, confidenceData } = params;
  const preferred = typeof resultPairPreference?.target === 'string' ? resultPairPreference.target.trim() : '';
  const affinityTarget = readFirstNonEmptyStringMetric(affinityData, ['requested_target_chain', 'target_chain', 'binder_chain']);
  const { knownChainIdByKey, knownChainIds } = buildKnownChainMaps(resultChainIds, confidenceData);

  const targetCandidates = [preferred, affinityTarget];
  for (const candidate of targetCandidates) {
    const chain = resolveChainCandidate(candidate, resultChainInfoById, knownChainIdByKey, knownChainIds, resultComponentOptions);
    if (chain) return chain;
    const split = splitChainTokens(candidate);
    for (const part of split) {
      const partChain = resolveChainCandidate(part, resultChainInfoById, knownChainIdByKey, knownChainIds, resultComponentOptions);
      if (partChain) return partChain;
    }
  }

  const ligandHintKeys = new Set(
    [
      ...splitChainTokens(readFirstNonEmptyStringMetric(affinityData, ['model_ligand_chain_id'])),
      ...splitChainTokens(readFirstNonEmptyStringMetric(affinityData, ['requested_ligand_chain', 'ligand_chain'])),
      ...splitChainTokens(readFirstNonEmptyStringMetric(confidenceData, ['model_ligand_chain_id'])),
      ...splitChainTokens(readFirstNonEmptyStringMetric(confidenceData, ['requested_ligand_chain_id', 'ligand_chain_id'])),
      ...readStringListMetric(confidenceData, ['ligand_chain_ids'])
    ]
      .map((item) => normalizeChainKey(item))
      .filter(Boolean)
  );

  const firstNonLigand = resultComponentOptions.find((item) => item.type !== 'ligand' && item.chainId);
  return (
    firstNonLigand?.chainId ||
    knownChainIds.find((chainId) => !ligandHintKeys.has(normalizeChainKey(chainId))) ||
    resultComponentOptions[0]?.chainId ||
    knownChainIds[0] ||
    null
  );
}

export function resolveSelectedResultLigandChainId(params: {
  resultPairPreference: Record<string, unknown> | null | undefined;
  resultChainInfoById: Map<string, unknown>;
  resultComponentOptions: ResultComponentOption[];
  resultChainIds: string[];
  selectedResultTargetChainId: string | null;
  affinityData: Record<string, unknown> | null;
  confidenceData: Record<string, unknown> | null;
  preferSequenceLigand?: boolean;
}): string | null {
  const {
    resultPairPreference,
    resultChainInfoById,
    resultComponentOptions,
    resultChainIds,
    selectedResultTargetChainId,
    affinityData,
    confidenceData,
    preferSequenceLigand
  } = params;
  const shouldPreferSequenceLigand = Boolean(preferSequenceLigand);

  const preferred = typeof resultPairPreference?.ligand === 'string' ? resultPairPreference.ligand.trim() : '';
  const affinityModelLigand = readFirstNonEmptyStringMetric(affinityData, ['model_ligand_chain_id']);
  const affinityLigand = readFirstNonEmptyStringMetric(affinityData, ['requested_ligand_chain', 'ligand_chain']);
  const confidenceModelLigand = readFirstNonEmptyStringMetric(confidenceData, ['model_ligand_chain_id']);
  const confidenceLigand = readFirstNonEmptyStringMetric(confidenceData, ['ligand_chain_id']);
  const { knownChainIdByKey, knownChainIds } = buildKnownChainMaps(resultChainIds, confidenceData);

  const confidenceLigandIds =
    confidenceData?.ligand_chain_ids && Array.isArray(confidenceData.ligand_chain_ids)
      ? (confidenceData.ligand_chain_ids as unknown[])
          .filter((item): item is string => typeof item === 'string')
          .map((item) => item.trim())
          .filter(Boolean)
      : [];
  const confidenceByChain =
    confidenceData?.ligand_atom_plddts_by_chain &&
    typeof confidenceData.ligand_atom_plddts_by_chain === 'object' &&
    !Array.isArray(confidenceData.ligand_atom_plddts_by_chain)
      ? Object.keys(confidenceData.ligand_atom_plddts_by_chain as Record<string, unknown>)
          .map((item) => item.trim())
          .filter(Boolean)
      : [];

  const preferredCandidates = [
    preferred,
    affinityModelLigand,
    confidenceModelLigand,
    affinityLigand,
    confidenceLigand,
    ...confidenceLigandIds,
    ...confidenceByChain
  ];

  for (const candidate of preferredCandidates) {
    const chain = resolveChainCandidate(candidate, resultChainInfoById, knownChainIdByKey, knownChainIds, resultComponentOptions);
    if (chain && chain !== selectedResultTargetChainId) {
      if (!shouldPreferSequenceLigand) return chain;
      const option = resultComponentOptions.find((item) => item.chainId === chain) || null;
      if (option && option.type === 'ligand') continue;
      return chain;
    }
  }

  const optionsWithoutTarget = resultComponentOptions.filter((item) => item.chainId && item.chainId !== selectedResultTargetChainId);
  const sequenceOptionsWithoutTarget = optionsWithoutTarget.filter((item) => item.type !== 'ligand');
  const inferredFallbackLigand =
    knownChainIds.find((chainId) => chainId !== selectedResultTargetChainId && confidenceLigandIds.includes(chainId)) ||
    knownChainIds.find((chainId) => chainId !== selectedResultTargetChainId) ||
    null;
  const defaultSequenceOption =
    sequenceOptionsWithoutTarget[0] ||
    resultComponentOptions.find((item) => item.chainId && item.type !== 'ligand') ||
    null;
  const defaultOption =
    optionsWithoutTarget.find((item) => item.isSmiles) ||
    resultComponentOptions.find((item) => item.isSmiles) ||
    optionsWithoutTarget[0] ||
    resultComponentOptions[0] ||
    null;

  if (shouldPreferSequenceLigand && defaultSequenceOption?.chainId) {
    return defaultSequenceOption.chainId;
  }
  return defaultOption?.chainId || inferredFallbackLigand;
}

export function buildResultChainConsistencyWarning(params: {
  workflowKey: string;
  snapshotConfidence: Record<string, unknown> | null;
  resultChainIds: string[];
  resultOverviewActiveComponents: InputComponent[];
}): string | null {
  const { workflowKey, snapshotConfidence, resultChainIds, resultOverviewActiveComponents } = params;
  if (workflowKey !== 'affinity') return null;
  if (!snapshotConfidence) return null;
  const confidenceChainIdsRaw = snapshotConfidence.chain_ids;
  if (!Array.isArray(confidenceChainIdsRaw)) return null;
  const confidenceChainIds = confidenceChainIdsRaw
    .filter((item): item is string => typeof item === 'string')
    .map((item) => item.trim())
    .filter(Boolean);
  if (confidenceChainIds.length <= resultChainIds.length) return null;

  const proteinComponents = resultOverviewActiveComponents.filter((item) => item.type === 'protein').length;
  const ligandComponents = resultOverviewActiveComponents.filter((item) => item.type === 'ligand').length;
  if (proteinComponents !== 1 || ligandComponents !== 1 || resultChainIds.length !== 2) return null;

  const modelLigandChain = readFirstNonEmptyStringMetric(snapshotConfidence, ['model_ligand_chain_id']);
  const requestedLigandChain = readFirstNonEmptyStringMetric(snapshotConfidence, ['requested_ligand_chain_id', 'ligand_chain_id']);
  const mappingText =
    modelLigandChain && requestedLigandChain
      ? ` (model ligand chain: ${modelLigandChain}, requested: ${requestedLigandChain})`
      : '';
  return (
    `Result artifact chain count is inconsistent with task components: expected 2 chains from components, ` +
    `but confidence reports ${confidenceChainIds.length} (${confidenceChainIds.join(', ')}). ` +
    `This usually means a malformed Protenix chain split in an older run; rerun the task with current backend.${mappingText}`
  );
}
