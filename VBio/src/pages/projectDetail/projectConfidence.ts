import { normalizePlddtValue, readFirstNonEmptyStringMetric, readObjectPath } from './projectMetrics';

export function normalizeChainKey(value: string): string {
  return value.trim().toUpperCase();
}

function pickLongestConfidenceSeries(valuesByChain: number[][]): number[] {
  if (valuesByChain.length === 0) return [];
  return valuesByChain.reduce((best, current) => (current.length > best.length ? current : best), valuesByChain[0]);
}

function readLigandCoverageChainKeys(confidence: Record<string, unknown> | null): Set<string> {
  const keys = new Set<string>();
  if (!confidence) return keys;
  const addChain = (value: unknown) => {
    if (typeof value !== 'string') return;
    const normalized = normalizeChainKey(value);
    if (normalized) keys.add(normalized);
  };

  const ligandCoverage = confidence.ligand_atom_coverage;
  if (Array.isArray(ligandCoverage)) {
    for (const row of ligandCoverage) {
      if (!row || typeof row !== 'object') continue;
      addChain((row as Record<string, unknown>).chain);
    }
  }
  const chainCoverage = confidence.chain_atom_coverage;
  if (Array.isArray(chainCoverage)) {
    for (const row of chainCoverage) {
      if (!row || typeof row !== 'object') continue;
      const entry = row as Record<string, unknown>;
      const molType = String(entry.mol_type || '').trim().toLowerCase();
      if (!molType) continue;
      if (molType.includes('nonpolymer') || molType.includes('ligand')) {
        addChain(entry.chain);
      }
    }
  }
  return keys;
}

function toFiniteNumberArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      if (typeof item === 'number') return Number.isFinite(item) ? item : null;
      if (typeof item === 'string') {
        const parsed = Number(item.trim());
        return Number.isFinite(parsed) ? parsed : null;
      }
      return null;
    })
    .filter((item): item is number => item !== null);
}

export function chainKeysMatch(candidate: string, preferred: string): boolean {
  const normalizedCandidate = normalizeChainKey(candidate);
  const normalizedPreferred = normalizeChainKey(preferred);
  if (!normalizedCandidate || !normalizedPreferred) return false;
  if (normalizedCandidate === normalizedPreferred) return true;
  const compactCandidate = normalizedCandidate.replace(/[^A-Z0-9]/g, '');
  const compactPreferred = normalizedPreferred.replace(/[^A-Z0-9]/g, '');
  if (compactCandidate && compactPreferred && compactCandidate === compactPreferred) return true;
  if (compactCandidate && compactPreferred) {
    if (compactCandidate.startsWith(compactPreferred) || compactCandidate.endsWith(compactPreferred)) return true;
    if (compactPreferred.startsWith(compactCandidate) || compactPreferred.endsWith(compactCandidate)) return true;
  }
  const candidateTokens = normalizedCandidate.split(/[^A-Z0-9]+/).filter(Boolean);
  if (candidateTokens.includes(normalizedPreferred) || (compactPreferred && candidateTokens.includes(compactPreferred))) {
    return true;
  }
  return false;
}

function collectPreferredLigandChainKeys(
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

function readResiduePlddtsFromChainMap(value: unknown, preferredChainKeys: Set<string>): number[] | null {
  if (!value || typeof value !== 'object' || Array.isArray(value) || preferredChainKeys.size === 0) return null;
  const entries = Object.entries(value as Record<string, unknown>);
  for (const [key, chainValues] of entries) {
    const matched = Array.from(preferredChainKeys).some((preferred) => chainKeysMatch(key, preferred));
    if (!matched) continue;
    const parsed = toFiniteNumberArray(chainValues).map((item) => normalizePlddtValue(item));
    if (parsed.length > 0) return parsed;
  }
  return null;
}

function readTokenPlddtsForChain(
  confidence: Record<string, unknown> | null,
  preferredChainKeys: Set<string>
): number[] | null {
  if (!confidence || preferredChainKeys.size === 0) return null;
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

  for (const plddtCandidate of tokenPlddtCandidates) {
    const tokenPlddts = toFiniteNumberArray(plddtCandidate).map((item) => normalizePlddtValue(item));
    if (tokenPlddts.length === 0) continue;
    for (const chainCandidate of tokenChainCandidates) {
      if (!Array.isArray(chainCandidate) || chainCandidate.length !== tokenPlddts.length) continue;
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

export function readResiduePlddtsForChain(
  confidence: Record<string, unknown> | null,
  preferredLigandChainId: string | null
): number[] | null {
  if (!confidence) return null;
  const preferredChainKeys = collectPreferredLigandChainKeys(confidence, preferredLigandChainId);
  if (preferredChainKeys.size === 0) return null;
  const chainMapCandidates: unknown[] = [
    confidence.residue_plddt_by_chain,
    confidence.chain_residue_plddt,
    confidence.chain_plddt,
    confidence.chain_plddts,
    confidence.plddt_by_chain,
    readObjectPath(confidence, 'residue_plddt_by_chain'),
    readObjectPath(confidence, 'chain_residue_plddt'),
    readObjectPath(confidence, 'chain_plddt'),
    readObjectPath(confidence, 'chain_plddts'),
    readObjectPath(confidence, 'plddt.by_chain')
  ];

  for (const candidate of chainMapCandidates) {
    const parsed = readResiduePlddtsFromChainMap(candidate, preferredChainKeys);
    if (parsed && parsed.length > 0) return parsed;
  }
  return readTokenPlddtsForChain(confidence, preferredChainKeys);
}

export function readLigandAtomPlddtsFromConfidence(
  confidence: Record<string, unknown> | null,
  preferredLigandChainId: string | null = null
): number[] {
  if (!confidence) return [];
  const byChainCandidates: unknown[] = [
    confidence.ligand_atom_plddts_by_chain,
    readObjectPath(confidence, 'ligand.atom_plddts_by_chain'),
    readObjectPath(confidence, 'ligand_confidence.atom_plddts_by_chain')
  ];
  const preferredKeys = collectPreferredLigandChainKeys(confidence, preferredLigandChainId);
  const ligandCoverageKeys = readLigandCoverageChainKeys(confidence);
  for (const candidate of byChainCandidates) {
    if (!candidate || typeof candidate !== 'object' || Array.isArray(candidate)) continue;
    const parsedEntries = Object.entries(candidate as Record<string, unknown>)
      .map(([chainId, chainValues]) => {
        if (!Array.isArray(chainValues)) return null;
        const values = chainValues
          .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
          .map((value) => normalizePlddtValue(value));
        if (values.length === 0) return null;
        return { chainId: normalizeChainKey(chainId), values };
      })
      .filter((entry): entry is { chainId: string; values: number[] } => entry !== null);
    if (parsedEntries.length === 0) continue;

    if (preferredKeys.size > 0) {
      const matched = parsedEntries
        .filter((entry) =>
          Array.from(preferredKeys).some((preferred) => chainKeysMatch(entry.chainId, preferred) || chainKeysMatch(preferred, entry.chainId))
        )
        .map((entry) => entry.values);
      if (matched.length > 0) return pickLongestConfidenceSeries(matched);
    }

    if (ligandCoverageKeys.size > 0) {
      const matchedByCoverage = parsedEntries
        .filter((entry) =>
          Array.from(ligandCoverageKeys).some((preferred) => chainKeysMatch(entry.chainId, preferred) || chainKeysMatch(preferred, entry.chainId))
        )
        .map((entry) => entry.values);
      if (matchedByCoverage.length > 0) return pickLongestConfidenceSeries(matchedByCoverage);
    }

    return pickLongestConfidenceSeries(parsedEntries.map((entry) => entry.values));
  }

  const candidates: unknown[] = [
    confidence.ligand_atom_plddts,
    confidence.ligand_atom_plddt,
    readObjectPath(confidence, 'ligand.atom_plddts'),
    readObjectPath(confidence, 'ligand.atom_plddt'),
    readObjectPath(confidence, 'ligand_confidence.atom_plddts')
  ];
  for (const candidate of candidates) {
    if (!Array.isArray(candidate)) continue;
    const values = candidate
      .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
      .map((value) => normalizePlddtValue(value));
    if (values.length > 0) return values;
  }
  return [];
}

export function alignConfidenceSeriesToLength(values: number[] | null, sequenceLength: number): number[] | null {
  if (!values || values.length === 0 || sequenceLength <= 0) return null;
  if (values.length === sequenceLength) return values;
  if (values.length > sequenceLength) {
    const reduced: number[] = [];
    for (let i = 0; i < sequenceLength; i += 1) {
      const start = Math.floor((i * values.length) / sequenceLength);
      const end = Math.max(start + 1, Math.floor(((i + 1) * values.length) / sequenceLength));
      const chunk = values.slice(start, end);
      const avg = chunk.reduce((sum, value) => sum + value, 0) / chunk.length;
      reduced.push(normalizePlddtValue(avg));
    }
    return reduced;
  }
  const expanded: number[] = [];
  for (let i = 0; i < sequenceLength; i += 1) {
    const mapped = Math.floor((i * values.length) / sequenceLength);
    expanded.push(values[Math.min(values.length - 1, Math.max(0, mapped))]);
  }
  return expanded;
}
