import type { ProjectTask } from '../../types/models';
import type {
  TaskConfidenceMetrics,
  TaskMetricContext
} from './taskListTypes';
import {
  readObjectPath,
  readFirstFiniteMetric,
  readFirstNonEmptyStringMetric,
  normalizeProbability,
  normalizeChainKey,
  chainKeysMatch,
  resolveTaskSelectionContext
} from './taskDataCore';

function toFiniteNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}

function isNumericToken(value: string): boolean {
  return /^\d+$/.test(String(value || '').trim());
}

function readPairValueFromNestedMap(
  mapValue: unknown,
  chainA: string,
  chainB: string
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const rowA =
    byChain[chainA] && typeof byChain[chainA] === 'object' && !Array.isArray(byChain[chainA])
      ? (byChain[chainA] as Record<string, unknown>)
      : (() => {
          for (const [key, value] of Object.entries(byChain)) {
            if (!chainKeysMatch(key, chainA) && !chainKeysMatch(chainA, key)) continue;
            if (value && typeof value === 'object' && !Array.isArray(value)) {
              return value as Record<string, unknown>;
            }
          }
          return null;
        })();
  if (!rowA) return null;
  const directValue = rowA[chainB];
  if (directValue !== undefined) {
    return normalizeProbability(toFiniteNumber(directValue));
  }
  for (const [key, value] of Object.entries(rowA)) {
    if (!chainKeysMatch(key, chainB) && !chainKeysMatch(chainB, key)) continue;
    return normalizeProbability(toFiniteNumber(value));
  }
  return null;
}

function readPairValueFromNumericMap(
  mapValue: unknown,
  chainA: string,
  chainB: string,
  chainOrderHints: string[],
  preferredDirectionalIptm: number | null
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length === 0 || !keys.every((item) => isNumericToken(item))) return null;

  const idxA = chainOrderHints.findIndex(
    (hint) => chainKeysMatch(hint, chainA) || chainKeysMatch(chainA, hint)
  );
  const idxB = chainOrderHints.findIndex(
    (hint) => chainKeysMatch(hint, chainB) || chainKeysMatch(chainB, hint)
  );
  if (idxA >= 0 && idxB >= 0 && idxA !== idxB) {
    const ligandToTarget = readPairValueFromNestedMap(byChain, String(idxB), String(idxA));
    const targetToLigand = readPairValueFromNestedMap(byChain, String(idxA), String(idxB));
    if (ligandToTarget !== null && targetToLigand !== null && preferredDirectionalIptm !== null) {
      const ligandDelta = Math.abs(ligandToTarget - preferredDirectionalIptm);
      const targetDelta = Math.abs(targetToLigand - preferredDirectionalIptm);
      return ligandDelta <= targetDelta ? ligandToTarget : targetToLigand;
    }
    if (ligandToTarget !== null) return ligandToTarget;
    if (targetToLigand !== null) return targetToLigand;
  }

  if (keys.length === 2 && preferredDirectionalIptm !== null) {
    const [first, second] = keys.sort((a, b) => Number(a) - Number(b));
    const forward = readPairValueFromNestedMap(byChain, first, second);
    const backward = readPairValueFromNestedMap(byChain, second, first);
    if (forward !== null && backward !== null) {
      const forwardDelta = Math.abs(forward - preferredDirectionalIptm);
      const backwardDelta = Math.abs(backward - preferredDirectionalIptm);
      return forwardDelta <= backwardDelta ? forward : backward;
    }
    if (forward !== null) return forward;
    if (backward !== null) return backward;
  }
  return null;
}

function readPairValueFromAnyTwoKeyMap(
  mapValue: unknown,
  preferredDirectionalIptm: number | null
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  if (preferredDirectionalIptm === null) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length !== 2) return null;
  const [first, second] = keys;
  const forward = readPairValueFromNestedMap(byChain, first, second);
  const backward = readPairValueFromNestedMap(byChain, second, first);
  if (forward === null && backward === null) return null;
  if (forward !== null && backward !== null) {
    const forwardDelta = Math.abs(forward - preferredDirectionalIptm);
    const backwardDelta = Math.abs(backward - preferredDirectionalIptm);
    return forwardDelta <= backwardDelta ? forward : backward;
  }
  return forward ?? backward;
}

function readPairIptmForChains(
  confidence: Record<string, unknown>,
  chainA: string | null,
  chainB: string | null,
  fallbackChainIds: string[]
): number | null {
  if (!chainA || !chainB) return null;
  const sameChain = chainKeysMatch(chainA, chainB);

  const chainIdsRaw = readObjectPath(confidence, 'chain_ids');
  const chainIds =
    Array.isArray(chainIdsRaw) && chainIdsRaw.every((item) => typeof item === 'string')
      ? (chainIdsRaw as string[])
      : fallbackChainIds;
  const preferredDirectionalIptm = normalizeProbability(
    readFirstFiniteMetric(confidence, ['ligand_iptm', 'iptm'])
  );

  const pairMap = readObjectPath(confidence, 'pair_chains_iptm');
  if (!sameChain) {
    // Drug-design view: prefer ligand->target directional ipTM when available.
    const ligandToTarget = readPairValueFromNestedMap(pairMap, chainB, chainA);
    if (ligandToTarget !== null) return ligandToTarget;
    const targetToLigand = readPairValueFromNestedMap(pairMap, chainA, chainB);
    if (targetToLigand !== null) return targetToLigand;
  }
  const numericMapped = readPairValueFromNumericMap(
    pairMap,
    chainA,
    chainB,
    chainIds,
    preferredDirectionalIptm
  );
  if (numericMapped !== null) return numericMapped;
  const twoKeyMapped = readPairValueFromAnyTwoKeyMap(pairMap, preferredDirectionalIptm);
  if (twoKeyMapped !== null) return twoKeyMapped;

  const pairMatrixRaw =
    readObjectPath(confidence, 'chain_pair_iptm') ??
    readObjectPath(confidence, 'chain_pair_iptm_global');
  if (!Array.isArray(pairMatrixRaw)) return null;
  const pairMatrix = pairMatrixRaw;
  const i = chainIds.findIndex((item) => chainKeysMatch(item, chainA) || chainKeysMatch(chainA, item));
  const j = chainIds.findIndex((item) => chainKeysMatch(item, chainB) || chainKeysMatch(chainB, item));
  if (i >= 0 && j >= 0 && i !== j) {
    const rowI = pairMatrix[i];
    const rowJ = pairMatrix[j];
    const ligandToTarget = Array.isArray(rowJ) ? normalizeProbability(toFiniteNumber(rowJ[i])) : null;
    const targetToLigand = Array.isArray(rowI) ? normalizeProbability(toFiniteNumber(rowI[j])) : null;
    if (ligandToTarget !== null && targetToLigand !== null && preferredDirectionalIptm !== null) {
      const ligandDelta = Math.abs(ligandToTarget - preferredDirectionalIptm);
      const targetDelta = Math.abs(targetToLigand - preferredDirectionalIptm);
      return ligandDelta <= targetDelta ? ligandToTarget : targetToLigand;
    }
    if (ligandToTarget !== null) return ligandToTarget;
    if (targetToLigand !== null) return targetToLigand;
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

function readIpsaeDomMetric(confidence: Record<string, unknown>): number | null {
  return normalizeProbability(readFirstFiniteMetric(confidence, ['ipsae_dom', 'ipsaeDom']));
}

function readLigandIpsaeMaxMetric(confidence: Record<string, unknown>): number | null {
  return normalizeProbability(readFirstFiniteMetric(confidence, ['ligand_ipsae_max', 'ligandIpsaeMax']));
}

function readInterfaceIpsaeMetric(confidence: Record<string, unknown>): number | null {
  const source = readFirstNonEmptyStringMetric(confidence, ['interface_metric_source', 'interfaceMetricSource']).toLowerCase();
  const label = readFirstNonEmptyStringMetric(confidence, ['interface_metric_label', 'interfaceMetricLabel']).toLowerCase();
  if (source !== 'ipsae' && label !== 'ipsae') return null;
  return normalizeProbability(
    readFirstFiniteMetric(confidence, ['interface_metric', 'interface_metric_value', 'interfaceMetricValue'])
  );
}

function resolvePreferredInterfaceMetric(
  confidence: Record<string, unknown>,
  context?: TaskMetricContext
): { value: number | null; label: 'IPSAE' | 'ipTM'; source: 'ipsae' | 'iptm' | 'none'; pairIptm: number | null } {
  const pairIptm = context
    ? readPairIptmForChains(confidence, context.targetChainId, context.ligandChainId, context.chainIds)
    : null;
  const ligandIpsaeMax = readLigandIpsaeMaxMetric(confidence);
  if (ligandIpsaeMax !== null) {
    return { value: ligandIpsaeMax, label: 'IPSAE', source: 'ipsae', pairIptm };
  }
  const ipsaeDom = readIpsaeDomMetric(confidence);
  if (ipsaeDom !== null) {
    return { value: ipsaeDom, label: 'IPSAE', source: 'ipsae', pairIptm };
  }
  const interfaceIpsae = readInterfaceIpsaeMetric(confidence);
  if (interfaceIpsae !== null) {
    return { value: interfaceIpsae, label: 'IPSAE', source: 'ipsae', pairIptm };
  }
  const scalarIptm = normalizeProbability(readFirstFiniteMetric(confidence, ['iptm', 'ligand_iptm', 'protein_iptm']));
  const preferredIptm = pairIptm ?? scalarIptm;
  if (preferredIptm !== null) {
    return { value: preferredIptm, label: 'ipTM', source: 'iptm', pairIptm };
  }
  return { value: null, label: 'IPSAE', source: 'none', pairIptm };
}

function readTaskConfidenceMetrics(task: ProjectTask, context?: TaskMetricContext): TaskConfidenceMetrics {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const strictPairIptm = context ? context.strictPairIptm !== false : false;
  const selectedLigandPlddt = context
    ? readChainMeanPlddtForChain(confidence, context.ligandChainId)
    : null;
  const ligandIpsaeMax = readLigandIpsaeMaxMetric(confidence);
  const ipsaeDom = readIpsaeDomMetric(confidence);
  const interfaceIpsae = readInterfaceIpsaeMetric(confidence);
  const preferredInterfaceMetric = resolvePreferredInterfaceMetric(confidence, context);
  const selectedPairIptm = preferredInterfaceMetric.pairIptm;
  const plddtRaw = readFirstFiniteMetric(confidence, [
    'ligand_plddt',
    'ligand_mean_plddt',
    'complex_iplddt',
    'complex_plddt_protein',
    'complex_plddt',
    'plddt'
  ]);
  const iptmRaw = strictPairIptm
    ? selectedPairIptm
    : selectedPairIptm ?? readFirstFiniteMetric(confidence, ['iptm', 'ligand_iptm', 'protein_iptm']);
  const paeRaw = readFirstFiniteMetric(confidence, [
    'complex_pde',
    'complex_pae',
    'pair_pae',
    'pairPae',
    'pair_pde',
    'pair_gpde',
    'gpde',
    'pae'
  ]);
  const mergedPlddt = selectedLigandPlddt ?? plddtRaw;
  return {
    plddt: mergedPlddt === null ? null : mergedPlddt <= 1 ? mergedPlddt * 100 : mergedPlddt,
    ipsae: ligandIpsaeMax ?? ipsaeDom ?? interfaceIpsae,
    iptm: normalizeProbability(iptmRaw),
    interfaceMetricValue: preferredInterfaceMetric.value,
    interfaceMetricLabel: preferredInterfaceMetric.label,
    interfaceMetricSource: preferredInterfaceMetric.source,
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
    const scalarEntries = Object.entries(obj)
      .map(([key, item]) => ({
        key,
        keyNumber: Number(key),
        value:
          typeof item === 'number'
            ? (Number.isFinite(item) ? item : null)
            : typeof item === 'string'
              ? (() => {
                  const parsed = Number(item.trim());
                  return Number.isFinite(parsed) ? parsed : null;
                })()
              : null
      }))
      .filter((entry) => entry.value !== null);
    const numericKeyEntries = scalarEntries.filter((entry) => Number.isFinite(entry.keyNumber));
    if (numericKeyEntries.length >= 3 && numericKeyEntries.length >= Math.floor(scalarEntries.length * 0.6)) {
      numericKeyEntries.sort((a, b) => a.keyNumber - b.keyNumber);
      return numericKeyEntries.map((entry) => entry.value as number);
    }
    const nestedCandidates: unknown[] = [
      obj.values,
      obj.value,
      obj.plddt,
      obj.plddts,
      obj.residue_plddt,
      obj.residue_plddts,
      obj.per_residue_plddt,
      obj.per_residue_confidence,
      obj.binder_residue_plddt,
      obj.binder_residue_plddts,
      obj.binder_plddt_per_residue,
      obj.plddt_per_residue,
      obj.plddt_by_residue,
      obj.aa_plddt,
      obj.aa_plddts,
      obj.ligand_residue_plddt,
      obj.ligand_residue_plddts,
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

function readTaskLigandAtomPlddtsFromChainMap(
  value: unknown,
  preferredChainKeys: Set<string>,
  allowFallbackToAnyChain: boolean
): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const parsedEntries = Object.entries(value as Record<string, unknown>)
    .map(([key, chainValues]) => {
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length === 0) return null;
      return {
        chainId: normalizeChainKey(key),
        values: parsed
      };
    })
    .filter((entry): entry is { chainId: string; values: number[] } => entry !== null);
  if (parsedEntries.length === 0) return null;

  const pickLongest = (values: number[][]): number[] | null => {
    if (values.length === 0) return null;
    return values.reduce((best, current) => (current.length > best.length ? current : best), values[0]);
  };

  if (preferredChainKeys.size > 0) {
    const matched = parsedEntries
      .filter((entry) =>
        Array.from(preferredChainKeys).some(
          (preferred) => chainKeysMatch(entry.chainId, preferred) || chainKeysMatch(preferred, entry.chainId)
        )
      )
      .map((entry) => entry.values);
    const selected = pickLongest(matched);
    if (selected) return selected;
  }

  if (allowFallbackToAnyChain) {
    return pickLongest(parsedEntries.map((entry) => entry.values));
  }
  return null;
}

function readTaskResiduePlddtsFromChainMap(
  value: unknown,
  preferredChainKeys: Set<string>
): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null;
  const map = value as Record<string, unknown>;
  const entries = Object.entries(map);
  if (entries.length === 0) return null;

  if (preferredChainKeys.size > 0) {
    for (const [key, chainValues] of entries) {
      const matched = Array.from(preferredChainKeys).some((preferred) => chainKeysMatch(key, preferred));
      if (!matched) continue;
      const parsed = normalizeAtomPlddts(toFiniteNumberArray(chainValues));
      if (parsed.length > 0) return parsed;
    }
  }
  return null;
}

function collectTaskPreferredLigandChainKeys(
  confidence: Record<string, unknown>,
  preferredLigandChainId: string | null
): Set<string> {
  const keys = new Set<string>();
  if (preferredLigandChainId) {
    keys.add(normalizeChainKey(preferredLigandChainId));
  }
  const modelLigandChain = readFirstNonEmptyStringMetric(confidence, ['model_ligand_chain_id']);
  if (modelLigandChain) {
    keys.add(normalizeChainKey(modelLigandChain));
  }
  const requestedLigandChain = readFirstNonEmptyStringMetric(confidence, ['requested_ligand_chain_id', 'ligand_chain_id']);
  if (requestedLigandChain) {
    keys.add(normalizeChainKey(requestedLigandChain));
  }
  return keys;
}

function readTaskTokenPlddtsForChain(
  confidence: Record<string, unknown>,
  preferredChainKeys: Set<string>
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

  if (preferredChainKeys.size === 0) return null;
  for (const plddtCandidate of tokenPlddtCandidates) {
    const tokenPlddts = normalizeAtomPlddts(toFiniteNumberArray(plddtCandidate));
    if (tokenPlddts.length === 0) continue;

    for (const chainCandidate of tokenChainCandidates) {
      if (!Array.isArray(chainCandidate)) continue;
      if (chainCandidate.length !== tokenPlddts.length) continue;
      const tokenChains = chainCandidate.map((value) => normalizeChainKey(String(value || '')));
      if (tokenChains.some((value) => !value)) continue;

      const byChain = tokenPlddts.filter((_, index) => {
        return Array.from(preferredChainKeys).some((preferred) => chainKeysMatch(tokenChains[index], preferred));
      });
      if (byChain.length > 0) return byChain;
    }
  }
  return null;
}

function readTaskLigandResiduePlddts(task: ProjectTask, preferredLigandChainId: string | null): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const preferredChainKeys = collectTaskPreferredLigandChainKeys(confidence, preferredLigandChainId);
  if (preferredChainKeys.size === 0) return null;
  const chainMapCandidates: unknown[] = [
    confidence.residue_plddt_by_chain,
    confidence.chain_residue_plddt,
    confidence.plddt_by_chain,
    readObjectPath(confidence, 'residue_plddt_by_chain'),
    readObjectPath(confidence, 'chain_residue_plddt'),
    readObjectPath(confidence, 'plddt.by_chain')
  ];
  for (const candidate of chainMapCandidates) {
    const parsed = readTaskResiduePlddtsFromChainMap(candidate, preferredChainKeys);
    if (parsed && parsed.length > 0) return parsed;
  }

  const tokenPlddts = readTaskTokenPlddtsForChain(confidence, preferredChainKeys);
  if (tokenPlddts && tokenPlddts.length > 0) return tokenPlddts;

  return null;
}

function readTaskLigandAtomPlddts(
  task: ProjectTask,
  preferredLigandChainId: string | null = null,
  allowFlatFallback = true
): number[] | null {
  const confidence = (task.confidence || {}) as Record<string, unknown>;
  const preferredChainKeys = collectTaskPreferredLigandChainKeys(confidence, preferredLigandChainId);
  const byChainCandidates: unknown[] = [
    confidence.ligand_display_atom_plddts_by_chain,
    readObjectPath(confidence, 'ligand.display_atom_plddts_by_chain'),
    readObjectPath(confidence, 'ligand_display.atom_plddts_by_chain'),
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
      preferredChainKeys,
      preferredChainKeys.size === 0
    );
    if (parsed && parsed.length > 0) {
      return parsed;
    }
  }
  if (preferredChainKeys.size > 0 && hasChainMap && !allowFlatFallback) return null;
  if (!allowFlatFallback) return null;

  const candidates: unknown[] = [
    confidence.ligand_display_atom_plddts,
    readObjectPath(confidence, 'ligand.display_atom_plddts'),
    readObjectPath(confidence, 'ligand_display.atom_plddts'),
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

function hasNessoAffinitySummary(task: ProjectTask): boolean {
  const confidence =
    task.confidence && typeof task.confidence === 'object' && !Array.isArray(task.confidence)
      ? (task.confidence as Record<string, unknown>)
      : {};
  const backend = String(task.backend || confidence.backend || '').trim().toLowerCase();
  if (backend !== 'nesso' && backend !== 'nesso1' && backend !== 'nesso-1') return false;

  const affinity =
    task.affinity && typeof task.affinity === 'object' && !Array.isArray(task.affinity)
      ? (task.affinity as Record<string, unknown>)
      : {};
  if (readFirstFiniteMetric(affinity, [
    'affinity_pred_value',
    'affinity_pred_value1',
    'affinity_pred_value2',
    'affinity_probability_binary'
  ]) !== null) {
    return true;
  }
  const compounds = Array.isArray(affinity.compounds) ? affinity.compounds : [];
  return compounds.some((item) => {
    if (!item || typeof item !== 'object' || Array.isArray(item)) return false;
    return readFirstFiniteMetric(item as Record<string, unknown>, [
      'affinity_pred_value',
      'affinity_pred_value1',
      'affinity_pred_value2',
      'affinity_probability_binary'
    ]) !== null;
  });
}

function hasTaskSummaryMetrics(task: ProjectTask): boolean {
  // Nesso is affinity-only, so structural confidence metrics are intentionally absent.
  if (hasNessoAffinitySummary(task)) return true;
  const context = resolveTaskSelectionContext(task);
  const metrics = readTaskConfidenceMetrics(task, context);
  const hasAnyMetric = metrics.plddt !== null || metrics.ipsae !== null || metrics.iptm !== null || metrics.pae !== null;
  if (!hasAnyMetric) return false;
  // Prediction/affinity result bundles now provide IPSAE across supported backends.
  // If other summary metrics exist but IPSAE is still missing, keep this row eligible for the shared result hydration path.
  if (metrics.ipsae === null && (metrics.plddt !== null || metrics.iptm !== null || metrics.pae !== null)) {
    return false;
  }
  return true;
}

export {
  toFiniteNumber,
  isNumericToken,
  readPairValueFromNestedMap,
  readPairValueFromNumericMap,
  readPairValueFromAnyTwoKeyMap,
  readPairIptmForChains,
  readChainMeanPlddtForChain,
  readIpsaeDomMetric,
  readLigandIpsaeMaxMetric,
  readInterfaceIpsaeMetric,
  resolvePreferredInterfaceMetric,
  readTaskConfidenceMetrics,
  toFiniteNumberArray,
  normalizeAtomPlddts,
  mean,
  readTaskLigandAtomPlddtsFromChainMap,
  readTaskResiduePlddtsFromChainMap,
  collectTaskPreferredLigandChainKeys,
  readTaskTokenPlddtsForChain,
  readTaskLigandResiduePlddts,
  readTaskLigandAtomPlddts,
  hasTaskLigandAtomPlddts,
  hasNessoAffinitySummary,
  hasTaskSummaryMetrics
};
