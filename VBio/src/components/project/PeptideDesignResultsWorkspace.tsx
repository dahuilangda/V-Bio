import { X } from 'lucide-react';
import { useEffect, useMemo, useState, type CSSProperties, type KeyboardEvent, type PointerEvent, type RefObject } from 'react';
import { MolstarViewer } from './MolstarViewer';

type ResultsGridStyle = CSSProperties & { '--results-main-width'?: string };
type RuntimeState = 'SUCCESS' | 'RUNNING' | 'QUEUED' | 'FAILURE' | 'UNSCORED';
type PeptideSortKey = 'rank' | 'generation' | 'score' | 'plddt' | 'iptm';
type ConfidenceTone = 'vhigh' | 'high' | 'low' | 'vlow' | 'na';

interface PeptideDesignResultsWorkspaceProps {
  projectTaskId: string;
  resultsGridRef: RefObject<HTMLDivElement>;
  isResultsResizing: boolean;
  resultsGridStyle: ResultsGridStyle;
  onResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  onResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  snapshotConfidence: Record<string, unknown>;
  statusInfo: Record<string, unknown>;
  projectTaskState: string;
  progressPercent: number;
  displayStructureText: string;
  displayStructureFormat: 'cif' | 'pdb';
  selectedResultTargetChainId: string | null;
  selectedResultLigandChainId: string | null;
  selectedResultLigandSequence: string;
  confidenceBackend: string;
  projectBackend: string;
  fallbackPlddt: number | null;
  fallbackIptm: number | null;
}

interface PeptideDesignCandidate {
  id: string;
  rank: number;
  sequence: string;
  score: number | null;
  plddt: number | null;
  residuePlddts: number[];
  iptm: number | null;
  generation: number | null;
  modelLabel: string;
  structureText: string;
  structureFormat: 'cif' | 'pdb';
  structureName: string;
  runtimeState: RuntimeState;
  source: 'result' | 'live';
}

interface PeptideRuntimeContext {
  state: RuntimeState;
  currentStatus: string;
  statusMessage: string;
  currentGeneration: number | null;
  totalGenerations: number | null;
  bestScore: number | null;
  progressPercent: number | null;
  completedTasks: number | null;
  pendingTasks: number | null;
  totalTasks: number | null;
  liveCandidateRows: Array<Record<string, unknown>>;
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : {};
}

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)));
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function readFiniteNumber(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function readObjectPath(payload: Record<string, unknown>, path: string): unknown {
  let current: unknown = payload;
  for (const token of path.split('.')) {
    if (!current || typeof current !== 'object' || Array.isArray(current)) return undefined;
    current = (current as Record<string, unknown>)[token];
  }
  return current;
}

function chainTokenEquals(a: string, b: string): boolean {
  return normalizeChainToken(a) === normalizeChainToken(b);
}

function chainVariants(chainId: string): string[] {
  const token = readText(chainId).trim();
  if (!token) return [];
  const variants: string[] = [];
  const push = (value: string) => {
    const normalized = value.trim();
    if (!normalized) return;
    if (!variants.some((item) => chainTokenEquals(item, normalized))) variants.push(normalized);
  };
  push(token);
  push(token.toUpperCase());
  push(token.toLowerCase());
  return variants;
}

function toChainList(value: unknown): string[] {
  if (Array.isArray(value)) {
    const rows: string[] = [];
    for (const item of value) {
      const text = readText(item).trim();
      if (!text) continue;
      rows.push(text);
    }
    return rows;
  }
  if (typeof value === 'string') {
    const text = value.trim();
    if (!text) return [];
    return text
      .split(/[\s,;|]+/)
      .map((item) => item.trim())
      .filter(Boolean);
  }
  const token = readText(value).trim();
  return token ? [token] : [];
}

function addChainHints(bucket: string[], value: unknown) {
  for (const token of toChainList(value)) {
    if (!bucket.some((entry) => chainTokenEquals(entry, token))) {
      bucket.push(token);
    }
  }
}

function isNumericToken(value: string): boolean {
  return /^\d+$/.test(value.trim());
}

function readMapValueByChainToken(record: Record<string, unknown>, token: string): unknown {
  if (Object.prototype.hasOwnProperty.call(record, token)) return record[token];
  const normalized = normalizeChainToken(token);
  for (const [key, value] of Object.entries(record)) {
    if (normalizeChainToken(key) === normalized) return value;
  }
  return undefined;
}

function readPairValueFromNestedMap(mapValue: unknown, chainA: string, chainB: string): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;

  const rowA = readMapValueByChainToken(byChain, chainA);
  const rowB = readMapValueByChainToken(byChain, chainB);
  const v1 =
    rowA && typeof rowA === 'object' && !Array.isArray(rowA)
      ? normalizeIptm(readFiniteNumber(readMapValueByChainToken(rowA as Record<string, unknown>, chainB)))
      : null;
  const v2 =
    rowB && typeof rowB === 'object' && !Array.isArray(rowB)
      ? normalizeIptm(readFiniteNumber(readMapValueByChainToken(rowB as Record<string, unknown>, chainA)))
      : null;
  if (v1 === null && v2 === null) return null;
  return Math.max(v1 ?? Number.NEGATIVE_INFINITY, v2 ?? Number.NEGATIVE_INFINITY);
}

function readPairValueFromNumericMap(
  mapValue: unknown,
  chainA: string,
  chainB: string,
  chainOrderHints: string[]
): number | null {
  if (!mapValue || typeof mapValue !== 'object' || Array.isArray(mapValue)) return null;
  const byChain = mapValue as Record<string, unknown>;
  const keys = Object.keys(byChain).map((item) => String(item || '').trim()).filter(Boolean);
  if (keys.length === 0 || !keys.every((item) => isNumericToken(item))) return null;

  const idxA = chainOrderHints.findIndex((item) => chainTokenEquals(item, chainA));
  const idxB = chainOrderHints.findIndex((item) => chainTokenEquals(item, chainB));
  if (idxA >= 0 && idxB >= 0 && idxA !== idxB) {
    const mapped = readPairValueFromNestedMap(byChain, String(idxA), String(idxB));
    if (mapped !== null) return mapped;
  }

  if (keys.length === 2) {
    const [first, second] = keys.sort((a, b) => Number(a) - Number(b));
    const inferred = readPairValueFromNestedMap(byChain, first, second);
    if (inferred !== null) return inferred;
  }
  return null;
}

function readPairIptmForChains(
  payload: Record<string, unknown>,
  chainA: string,
  chainB: string,
  chainOrderHints: string[]
): number | null {
  if (!chainA || !chainB || chainTokenEquals(chainA, chainB)) return null;
  const direct = readPairValueFromNestedMap(payload.pair_chains_iptm, chainA, chainB);
  if (direct !== null) return direct;

  const matrixRaw = payload.chain_pair_iptm ?? payload.chain_pair_iptm_global;
  if (Array.isArray(matrixRaw)) {
    const chainIdsRaw = toChainList(payload.chain_ids);
    const chainIds = chainIdsRaw.length > 0 ? chainIdsRaw : chainOrderHints;
    const i = chainIds.findIndex((item) => chainTokenEquals(item, chainA));
    const j = chainIds.findIndex((item) => chainTokenEquals(item, chainB));
    if (i >= 0 && j >= 0) {
      const rowI = matrixRaw[i];
      const rowJ = matrixRaw[j];
      const m1 = Array.isArray(rowI) ? normalizeIptm(readFiniteNumber(rowI[j])) : null;
      const m2 = Array.isArray(rowJ) ? normalizeIptm(readFiniteNumber(rowJ[i])) : null;
      if (m1 !== null || m2 !== null) {
        return Math.max(m1 ?? Number.NEGATIVE_INFINITY, m2 ?? Number.NEGATIVE_INFINITY);
      }
    }
  }

  const numericMapped = readPairValueFromNumericMap(payload.pair_chains_iptm, chainA, chainB, chainOrderHints);
  return numericMapped !== null ? normalizeIptm(numericMapped) : null;
}

function resolvePairIptmForCandidate(
  row: Record<string, unknown>,
  preferredTargetChainId: string | undefined,
  preferredLigandChainId: string | undefined
): number | null {
  const nested = [
    asRecord(row.result),
    asRecord(row.prediction),
    asRecord(row.metadata),
    asRecord(row.structure_payload),
    asRecord(row.confidence),
    asRecord(row.metrics),
    asRecord(row.affinity)
  ];
  const payloads = [row, ...nested];
  const targetHints: string[] = [];
  const ligandHints: string[] = [];
  const chainOrderHints: string[] = [];

  // Primary source for peptide design rows: candidate-level ipTM in results_summary/design_results.
  const directRowValue = normalizeIptm(
    firstFiniteMetric(row, ['pair_iptm', 'pairIptm', 'pair_iptm_resolved', 'pairIptmResolved'])
  );
  if (directRowValue !== null) return directRowValue;

  addChainHints(targetHints, preferredTargetChainId);
  addChainHints(ligandHints, preferredLigandChainId);

  for (const payload of payloads) {
    addChainHints(targetHints, payload.target_chain_id);
    addChainHints(targetHints, payload.requested_target_chain_id);
    addChainHints(targetHints, payload.protein_chain_id);
    addChainHints(targetHints, payload.peptide_design_target_chain);
    addChainHints(targetHints, payload.target_chain_ids);
    addChainHints(targetHints, payload.protein_chain_ids);

    addChainHints(ligandHints, payload.ligand_chain_id);
    addChainHints(ligandHints, payload.requested_ligand_chain_id);
    addChainHints(ligandHints, payload.model_ligand_chain_id);
    addChainHints(ligandHints, payload.binder_chain_id);
    addChainHints(ligandHints, payload.peptide_chain_id);
    addChainHints(ligandHints, payload.ligand_chain_ids);
    addChainHints(ligandHints, payload.binder_chain_ids);

    addChainHints(chainOrderHints, payload.chain_ids);
    addChainHints(chainOrderHints, payload.chain_order);
  }

  if (targetHints.length === 0 && ligandHints.length > 0 && chainOrderHints.length > 0) {
    for (const chainId of chainOrderHints) {
      if (!ligandHints.some((ligand) => chainTokenEquals(ligand, chainId))) addChainHints(targetHints, chainId);
    }
  }
  if (ligandHints.length === 0 && targetHints.length > 0 && chainOrderHints.length > 0) {
    for (const chainId of chainOrderHints) {
      if (!targetHints.some((target) => chainTokenEquals(target, chainId))) addChainHints(ligandHints, chainId);
    }
  }

  addChainHints(chainOrderHints, targetHints);
  addChainHints(chainOrderHints, ligandHints);

  if (targetHints.length > 0 && ligandHints.length > 0) {
    for (const targetHint of targetHints) {
      for (const ligandHint of ligandHints) {
        if (chainTokenEquals(targetHint, ligandHint)) continue;
        for (const targetCandidate of chainVariants(targetHint)) {
          for (const ligandCandidate of chainVariants(ligandHint)) {
            for (const payload of payloads) {
              const pairValue = readPairIptmForChains(payload, targetCandidate, ligandCandidate, chainOrderHints);
              if (pairValue !== null) return pairValue;
            }
          }
        }
      }
    }
  }

  for (const payload of payloads) {
    const pairScalar = normalizeIptm(firstFiniteMetric(payload, ['pair_iptm', 'pairIptm', 'pair_iptm_resolved', 'pairIptmResolved']));
    if (pairScalar !== null) return pairScalar;
  }
  return null;
}

function normalizeWeight(value: number | null): number | null {
  if (value === null || !Number.isFinite(value)) return null;
  if (value > 1 && value <= 100) return value / 100;
  if (value < 0) return null;
  return value;
}

function computePeptideCompositeScore(row: Record<string, unknown>, plddt: number | null, iptm: number | null): number | null {
  if (plddt === null || iptm === null) return null;
  const nested = [row, asRecord(row.result), asRecord(row.prediction), asRecord(row.metadata), asRecord(row.scoring)];
  let wPlddt = normalizeWeight(
    readFirstFiniteFromPaths(nested, ['w1', 'weight_plddt', 'plddt_weight', 'score_weight_plddt', 'weights.plddt'])
  );
  let wIptm = normalizeWeight(
    readFirstFiniteFromPaths(nested, ['w2', 'weight_iptm', 'iptm_weight', 'score_weight_iptm', 'weights.iptm'])
  );
  if (wPlddt === null && wIptm === null) {
    wPlddt = 0.3;
    wIptm = 0.7;
  } else if (wPlddt === null) {
    wIptm = wIptm ?? 0.7;
    wPlddt = Math.max(0, 1 - wIptm);
  } else if (wIptm === null) {
    wPlddt = wPlddt ?? 0.3;
    wIptm = Math.max(0, 1 - wPlddt);
  }
  const sum = (wPlddt ?? 0) + (wIptm ?? 0);
  if (!Number.isFinite(sum) || sum <= 0) return null;
  const wp = (wPlddt ?? 0) / sum;
  const wi = (wIptm ?? 0) / sum;
  return wp * (plddt / 100) + wi * iptm;
}

function normalizePlddt(value: number | null): number | null {
  if (value === null) return null;
  if (value >= 0 && value <= 1) return value * 100;
  return value;
}

function normalizeIptm(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

function detectStructureFormat(text: string, hinted: unknown): 'cif' | 'pdb' {
  const hint = readText(hinted).trim().toLowerCase();
  if (hint === 'pdb' || hint === 'cif') return hint;
  const head = text.trim().slice(0, 20).toUpperCase();
  if (head.startsWith('ATOM') || head.startsWith('HETATM') || head.startsWith('HEADER')) return 'pdb';
  return 'cif';
}

function firstNonEmptyText(source: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const value = readText(source[key]).trim();
    if (value) return value;
  }
  return '';
}

function firstFiniteMetric(source: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = readFiniteNumber(source[key]);
    if (value !== null) return value;
  }
  return null;
}

function parseNumberList(value: unknown): number[] {
  if (Array.isArray(value)) {
    return value
      .map((item) => readFiniteNumber(item))
      .filter((item): item is number => item !== null);
  }
  if (typeof value === 'string') {
    const token = value.trim();
    if (!token) return [];
    if (token.startsWith('[') && token.endsWith(']')) {
      try {
        const parsed = JSON.parse(token) as unknown;
        return parseNumberList(parsed);
      } catch {
        // Fall through to split parsing.
      }
    }
    return token
      .split(/[\s,;]+/)
      .map((item) => readFiniteNumber(item))
      .filter((item): item is number => item !== null);
  }
  if (value && typeof value === 'object') {
    const record = value as Record<string, unknown>;
    if (Array.isArray(record.values)) return parseNumberList(record.values);
    if (Array.isArray(record.scores)) return parseNumberList(record.scores);
    if (Array.isArray(record.plddt)) return parseNumberList(record.plddt);
    for (const entry of Object.values(record)) {
      const nested = parseNumberList(entry);
      if (nested.length > 0) return nested;
    }
  }
  return [];
}

function normalizePlddtList(values: number[]): number[] {
  return values
    .map((item) => normalizePlddt(item))
    .filter((item): item is number => item !== null && Number.isFinite(item));
}

interface ResidueConfidenceBucket {
  seq: number;
  ins: string;
  residueName: string;
  ca: number | null;
  sum: number;
  count: number;
}

function updateResidueBucket(
  chains: Map<string, Map<string, ResidueConfidenceBucket>>,
  chainId: string,
  residueKey: string,
  seq: number,
  ins: string,
  residueName: string,
  atomName: string,
  bIso: number
) {
  let chain = chains.get(chainId);
  if (!chain) {
    chain = new Map<string, ResidueConfidenceBucket>();
    chains.set(chainId, chain);
  }
  const existing = chain.get(residueKey);
  if (!existing) {
    chain.set(residueKey, {
      seq,
      ins,
      residueName,
      ca: atomName === 'CA' ? bIso : null,
      sum: bIso,
      count: 1
    });
    return;
  }
  existing.sum += bIso;
  existing.count += 1;
  if (!existing.residueName && residueName) existing.residueName = residueName;
  if (atomName === 'CA') existing.ca = bIso;
}

function normalizeChainToken(chainId: string): string {
  return chainId.trim().toUpperCase();
}

function residueToOneLetter(name: string): string {
  const token = name.trim().toUpperCase();
  if (token === 'ALA') return 'A';
  if (token === 'ARG') return 'R';
  if (token === 'ASN') return 'N';
  if (token === 'ASP') return 'D';
  if (token === 'CYS') return 'C';
  if (token === 'GLN') return 'Q';
  if (token === 'GLU') return 'E';
  if (token === 'GLY') return 'G';
  if (token === 'HIS') return 'H';
  if (token === 'ILE') return 'I';
  if (token === 'LEU') return 'L';
  if (token === 'LYS') return 'K';
  if (token === 'MET') return 'M';
  if (token === 'PHE') return 'F';
  if (token === 'PRO') return 'P';
  if (token === 'SER') return 'S';
  if (token === 'THR') return 'T';
  if (token === 'TRP') return 'W';
  if (token === 'TYR') return 'Y';
  if (token === 'VAL') return 'V';
  if (token === 'SEC') return 'U';
  if (token === 'PYL') return 'O';
  return 'X';
}

function sequenceMatchScore(chainSequence: string, peptideSequence: string): number {
  const chain = chainSequence.trim().toUpperCase();
  const peptide = peptideSequence.trim().toUpperCase();
  if (!chain || !peptide) return Number.NEGATIVE_INFINITY;
  if (chain === peptide) return 10_000;
  if (chain.includes(peptide)) return 9_000 - Math.abs(chain.length - peptide.length);

  let bestMatches = 0;
  if (chain.length >= peptide.length) {
    for (let start = 0; start <= chain.length - peptide.length; start += 1) {
      let matches = 0;
      for (let idx = 0; idx < peptide.length; idx += 1) {
        if (chain[start + idx] === peptide[idx]) matches += 1;
      }
      if (matches > bestMatches) bestMatches = matches;
    }
  } else {
    for (let idx = 0; idx < chain.length; idx += 1) {
      if (chain[idx] === peptide[idx]) bestMatches += 1;
    }
  }
  return (bestMatches / peptide.length) * 1000 - Math.abs(chain.length - peptide.length) * 2;
}

function resolveBestChainId(
  chains: Map<string, Map<string, ResidueConfidenceBucket>>,
  preferredChainId: string | undefined,
  candidateSequence: string
): string | null {
  if (chains.size === 0) return null;

  const chainEntries = [...chains.entries()].map(([chainId, residueMap]) => {
    const residues = [...residueMap.values()].sort((a, b) => {
      if (a.seq !== b.seq) return a.seq - b.seq;
      return a.ins.localeCompare(b.ins);
    });
    const chainSequence = residues.map((item) => residueToOneLetter(item.residueName)).join('');
    return { chainId, residues, chainSequence };
  });

  const preferredToken = normalizeChainToken(readText(preferredChainId));
  const hasCandidateSequence = candidateSequence.trim().length > 0;
  if (hasCandidateSequence) {
    let best: { chainId: string; score: number } | null = null;
    for (const entry of chainEntries) {
      const score = sequenceMatchScore(entry.chainSequence, candidateSequence);
      if (!Number.isFinite(score)) continue;
      if (!best || score > best.score) best = { chainId: entry.chainId, score };
    }
    if (best) {
      if (!preferredToken) return best.chainId;
      const preferredEntry = chainEntries.find((entry) => normalizeChainToken(entry.chainId) === preferredToken);
      if (!preferredEntry) return best.chainId;
      const preferredScore = sequenceMatchScore(preferredEntry.chainSequence, candidateSequence);
      if (!Number.isFinite(preferredScore) || best.score > preferredScore + 20) return best.chainId;
      return preferredEntry.chainId;
    }
  }

  if (preferredToken) {
    const preferredEntry = chainEntries.find((entry) => normalizeChainToken(entry.chainId) === preferredToken);
    if (preferredEntry) return preferredEntry.chainId;
  }

  let longest = chainEntries[0];
  for (const entry of chainEntries) {
    if (entry.residues.length > longest.residues.length) longest = entry;
  }
  return longest.chainId;
}

function selectResidueConfidenceSeries(
  chains: Map<string, Map<string, ResidueConfidenceBucket>>,
  sequenceLength: number,
  preferredChainId?: string,
  candidateSequence?: string
): number[] {
  const selectedChainId = resolveBestChainId(chains, preferredChainId, candidateSequence || '');
  if (!selectedChainId) return [];
  const selected = chains.get(selectedChainId);
  if (!selected) return [];
  const rows = [...selected.values()].sort((a, b) => {
    if (a.seq !== b.seq) return a.seq - b.seq;
    return a.ins.localeCompare(b.ins);
  });
  const values = rows
    .map((row) => (row.ca !== null ? row.ca : row.count > 0 ? row.sum / row.count : null))
    .map((item) => normalizePlddt(item))
    .filter((item): item is number => item !== null && Number.isFinite(item));
  return sequenceLength > 0 ? values.slice(0, sequenceLength) : values;
}

function parseResidueConfidenceFromPdb(
  structureText: string,
  sequenceLength: number,
  preferredChainId?: string,
  candidateSequence?: string
): number[] {
  const lines = structureText.split(/\r?\n/);
  const chains = new Map<string, Map<string, ResidueConfidenceBucket>>();
  for (const line of lines) {
    if (!line.startsWith('ATOM')) continue;
    const atomName = line.slice(12, 16).trim().toUpperCase();
    const chainId = line.slice(21, 22).trim() || '_';
    const residueName = line.slice(17, 20).trim().toUpperCase();
    const seqRaw = line.slice(22, 26).trim();
    const ins = line.slice(26, 27).trim();
    const bIsoRaw = line.slice(60, 66).trim();
    const seq = Number(seqRaw);
    const bIso = Number(bIsoRaw);
    if (!Number.isFinite(seq) || !Number.isFinite(bIso)) continue;
    const residueKey = `${seqRaw}:${ins || '_'}`;
    updateResidueBucket(chains, chainId, residueKey, Math.floor(seq), ins, residueName, atomName, bIso);
  }
  return selectResidueConfidenceSeries(chains, sequenceLength, preferredChainId, candidateSequence);
}

function tokenizeCifRow(line: string): string[] {
  const tokens: string[] = [];
  const re = /'([^']*)'|"([^"]*)"|(\S+)/g;
  let match: RegExpExecArray | null = null;
  while ((match = re.exec(line)) !== null) {
    tokens.push(match[1] ?? match[2] ?? match[3]);
  }
  return tokens;
}

function parseResidueConfidenceFromCif(
  structureText: string,
  sequenceLength: number,
  preferredChainId?: string,
  candidateSequence?: string
): number[] {
  const lines = structureText.split(/\r?\n/);
  const chains = new Map<string, Map<string, ResidueConfidenceBucket>>();
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (line !== 'loop_') continue;
    const headers: string[] = [];
    let j = i + 1;
    while (j < lines.length) {
      const headerLine = lines[j].trim();
      if (!headerLine.startsWith('_')) break;
      headers.push(headerLine);
      j += 1;
    }
    if (!headers.some((header) => header.startsWith('_atom_site.'))) {
      i = j;
      continue;
    }
    const atomIdIdx = headers.findIndex((header) => header === '_atom_site.label_atom_id' || header === '_atom_site.auth_atom_id');
    const seqIdx = headers.findIndex((header) => header === '_atom_site.label_seq_id' || header === '_atom_site.auth_seq_id');
    const chainIdx = headers.findIndex((header) => header === '_atom_site.label_asym_id' || header === '_atom_site.auth_asym_id');
    const compIdx = headers.findIndex((header) => header === '_atom_site.label_comp_id' || header === '_atom_site.auth_comp_id');
    const bIsoIdx = headers.findIndex((header) => header === '_atom_site.B_iso_or_equiv');
    const insIdx = headers.findIndex((header) => header === '_atom_site.pdbx_PDB_ins_code');
    if (atomIdIdx < 0 || seqIdx < 0 || chainIdx < 0 || compIdx < 0 || bIsoIdx < 0) {
      i = j;
      continue;
    }
    while (j < lines.length) {
      const rowLine = lines[j].trim();
      if (!rowLine || rowLine === '#') {
        j += 1;
        continue;
      }
      if (rowLine === 'loop_' || rowLine.startsWith('_')) {
        j -= 1;
        break;
      }
      const tokens = tokenizeCifRow(rowLine);
      if (tokens.length <= Math.max(atomIdIdx, seqIdx, chainIdx, bIsoIdx)) {
        j += 1;
        continue;
      }
      const atomName = readText(tokens[atomIdIdx]).trim().toUpperCase();
      const seqToken = readText(tokens[seqIdx]).trim();
      const seq = Number(seqToken);
      const chainId = readText(tokens[chainIdx]).trim() || '_';
      const residueName = readText(tokens[compIdx]).trim().toUpperCase();
      const bIso = Number(readText(tokens[bIsoIdx]).trim());
      const ins = insIdx >= 0 ? readText(tokens[insIdx]).trim() : '';
      if (!Number.isFinite(seq) || !Number.isFinite(bIso)) {
        j += 1;
        continue;
      }
      const residueKey = `${seqToken}:${ins || '_'}`;
      updateResidueBucket(chains, chainId, residueKey, Math.floor(seq), ins, residueName, atomName, bIso);
      j += 1;
    }
    i = j;
  }
  return selectResidueConfidenceSeries(chains, sequenceLength, preferredChainId, candidateSequence);
}

function parseResidueConfidenceFromStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  sequenceLength: number,
  preferredChainId?: string,
  candidateSequence?: string
): number[] {
  const text = structureText.trim();
  if (!text) return [];
  if (structureFormat === 'pdb') {
    return parseResidueConfidenceFromPdb(text, sequenceLength, preferredChainId, candidateSequence);
  }
  const cifValues = parseResidueConfidenceFromCif(text, sequenceLength, preferredChainId, candidateSequence);
  if (cifValues.length > 0) return cifValues;
  return parseResidueConfidenceFromPdb(text, sequenceLength, preferredChainId, candidateSequence);
}

interface PolymerResidueEntry {
  seq: number;
  ins: string;
  residueName: string;
}

function pushPolymerResidue(
  chains: Map<string, Map<string, PolymerResidueEntry>>,
  chainId: string,
  residueKey: string,
  seq: number,
  ins: string,
  residueName: string
) {
  let chain = chains.get(chainId);
  if (!chain) {
    chain = new Map<string, PolymerResidueEntry>();
    chains.set(chainId, chain);
  }
  if (chain.has(residueKey)) return;
  chain.set(residueKey, { seq, ins, residueName });
}

function extractPolymerChainsFromPdb(structureText: string): Map<string, Map<string, PolymerResidueEntry>> {
  const chains = new Map<string, Map<string, PolymerResidueEntry>>();
  for (const line of structureText.split(/\r?\n/)) {
    if (!line.startsWith('ATOM')) continue;
    const chainId = line.slice(21, 22).trim() || '_';
    const residueName = line.slice(17, 20).trim().toUpperCase();
    const seqRaw = line.slice(22, 26).trim();
    const ins = line.slice(26, 27).trim();
    const seq = Number(seqRaw);
    if (!Number.isFinite(seq)) continue;
    const residueKey = `${seqRaw}:${ins || '_'}`;
    pushPolymerResidue(chains, chainId, residueKey, Math.floor(seq), ins, residueName);
  }
  return chains;
}

function extractPolymerChainsFromCif(structureText: string): Map<string, Map<string, PolymerResidueEntry>> {
  const chains = new Map<string, Map<string, PolymerResidueEntry>>();
  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (line !== 'loop_') continue;
    const headers: string[] = [];
    let j = i + 1;
    while (j < lines.length) {
      const headerLine = lines[j].trim();
      if (!headerLine.startsWith('_')) break;
      headers.push(headerLine);
      j += 1;
    }
    if (!headers.some((header) => header.startsWith('_atom_site.'))) {
      i = j;
      continue;
    }
    const groupIdx = headers.findIndex((header) => header === '_atom_site.group_PDB');
    const chainIdx = headers.findIndex((header) => header === '_atom_site.label_asym_id' || header === '_atom_site.auth_asym_id');
    const seqIdx = headers.findIndex((header) => header === '_atom_site.label_seq_id' || header === '_atom_site.auth_seq_id');
    const compIdx = headers.findIndex((header) => header === '_atom_site.label_comp_id' || header === '_atom_site.auth_comp_id');
    const insIdx = headers.findIndex((header) => header === '_atom_site.pdbx_PDB_ins_code');
    if (chainIdx < 0 || seqIdx < 0 || compIdx < 0) {
      i = j;
      continue;
    }

    while (j < lines.length) {
      const rowLine = lines[j].trim();
      if (!rowLine || rowLine === '#') {
        j += 1;
        continue;
      }
      if (rowLine === 'loop_' || rowLine.startsWith('_')) {
        j -= 1;
        break;
      }
      const tokens = tokenizeCifRow(rowLine);
      if (tokens.length <= Math.max(chainIdx, seqIdx, compIdx)) {
        j += 1;
        continue;
      }
      const group = groupIdx >= 0 ? readText(tokens[groupIdx]).trim().toUpperCase() : 'ATOM';
      if (group && group !== 'ATOM') {
        j += 1;
        continue;
      }
      const chainId = readText(tokens[chainIdx]).trim() || '_';
      const seqToken = readText(tokens[seqIdx]).trim();
      const residueName = readText(tokens[compIdx]).trim().toUpperCase();
      const seq = Number(seqToken);
      const ins = insIdx >= 0 ? readText(tokens[insIdx]).trim() : '';
      if (!Number.isFinite(seq)) {
        j += 1;
        continue;
      }
      const residueKey = `${seqToken}:${ins || '_'}`;
      pushPolymerResidue(chains, chainId, residueKey, Math.floor(seq), ins, residueName);
      j += 1;
    }
    i = j;
  }
  return chains;
}

function extractPolymerChainsFromStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb'
): Map<string, Map<string, PolymerResidueEntry>> {
  const text = structureText.trim();
  if (!text) return new Map();
  if (structureFormat === 'pdb') {
    return extractPolymerChainsFromPdb(text);
  }
  const cifChains = extractPolymerChainsFromCif(text);
  return cifChains.size > 0 ? cifChains : extractPolymerChainsFromPdb(text);
}

function resolvePeptideFocusChainId(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  candidateSequence: string,
  preferredChainId?: string
): string | null {
  const chains = extractPolymerChainsFromStructure(structureText, structureFormat);
  if (chains.size === 0) return preferredChainId || null;

  const chainEntries = [...chains.entries()].map(([chainId, residueMap]) => {
    const residues = [...residueMap.values()].sort((a, b) => {
      if (a.seq !== b.seq) return a.seq - b.seq;
      return a.ins.localeCompare(b.ins);
    });
    const chainSequence = residues.map((item) => residueToOneLetter(item.residueName)).join('');
    return { chainId, residues, chainSequence };
  });

  const peptide = candidateSequence.trim().toUpperCase();
  const preferredToken = normalizeChainToken(readText(preferredChainId));

  if (peptide) {
    let best: { chainId: string; score: number } | null = null;
    for (const entry of chainEntries) {
      const score = sequenceMatchScore(entry.chainSequence, peptide);
      if (!Number.isFinite(score)) continue;
      if (!best || score > best.score) best = { chainId: entry.chainId, score };
    }
    if (best) {
      if (!preferredToken) return best.chainId;
      const preferredEntry = chainEntries.find((entry) => normalizeChainToken(entry.chainId) === preferredToken);
      if (!preferredEntry) return best.chainId;
      const preferredScore = sequenceMatchScore(preferredEntry.chainSequence, peptide);
      if (!Number.isFinite(preferredScore) || best.score > preferredScore + 20) return best.chainId;
      return preferredEntry.chainId;
    }
  }

  if (preferredToken) {
    const preferredEntry = chainEntries.find((entry) => normalizeChainToken(entry.chainId) === preferredToken);
    if (preferredEntry) return preferredEntry.chainId;
  }

  let longest = chainEntries[0];
  for (const entry of chainEntries) {
    if (entry.residues.length > longest.residues.length) longest = entry;
  }
  return longest.chainId;
}

function parseCandidateResiduePlddts(
  row: Record<string, unknown>,
  sequence: string,
  sequenceLength: number,
  structure: { structureText: string; structureFormat: 'cif' | 'pdb' },
  preferredChainId?: string
): number[] {
  const nested = [row, asRecord(row.result), asRecord(row.prediction), asRecord(row.metadata), asRecord(row.structure_payload)];
  const directKeys = [
    'residue_plddt',
    'residue_plddts',
    'residue_confidence',
    'residue_confidences',
    'residue_scores',
    'per_residue_plddt',
    'per_residue_confidence',
    'binder_residue_plddt',
    'binder_residue_plddts',
    'binder_plddt_per_residue',
    'binder_plddt',
    'plddt_per_residue',
    'plddt_by_residue',
    'aa_plddt',
    'aa_plddts',
    'token_plddt',
    'token_plddts',
    'ligand_residue_plddt',
    'ligand_residue_plddts',
    'chain_plddt',
    'chain_plddts'
  ];
  const pathKeys = [
    'confidence.residue_plddt',
    'confidence.residue_plddts',
    'confidence.per_residue_plddt',
    'confidence.binder_residue_plddt',
    'metrics.residue_plddt',
    'metrics.per_residue_plddt',
    'scores.residue_plddt',
    'scores.per_residue_plddt'
  ];

  if (structure.structureText.trim()) {
    const structureValues = parseResidueConfidenceFromStructure(
      structure.structureText,
      structure.structureFormat,
      sequenceLength,
      preferredChainId,
      sequence
    );
    if (structureValues.length > 0) return structureValues;
  }

  for (const source of nested) {
    for (const key of directKeys) {
      const parsed = normalizePlddtList(parseNumberList(source[key]));
      if (parsed.length > 0) {
        return sequenceLength > 0 ? parsed.slice(0, sequenceLength) : parsed;
      }
    }
    for (const path of pathKeys) {
      const parsed = normalizePlddtList(parseNumberList(readObjectPath(source, path)));
      if (parsed.length > 0) {
        return sequenceLength > 0 ? parsed.slice(0, sequenceLength) : parsed;
      }
    }
  }

  return [];
}

function readFirstFiniteFromPaths(payloads: Record<string, unknown>[], paths: string[]): number | null {
  for (const payload of payloads) {
    for (const path of paths) {
      const value = readFiniteNumber(readObjectPath(payload, path));
      if (value !== null) return value;
    }
  }
  return null;
}

function readFirstTextFromPaths(payloads: Record<string, unknown>[], paths: string[]): string {
  for (const payload of payloads) {
    for (const path of paths) {
      const text = readText(readObjectPath(payload, path)).trim();
      if (text) return text;
    }
  }
  return '';
}

function readFirstRecordArrayFromPaths(payloads: Record<string, unknown>[], paths: string[]): Array<Record<string, unknown>> {
  for (const payload of payloads) {
    for (const path of paths) {
      const rows = asRecordArray(readObjectPath(payload, path));
      if (rows.length > 0) return rows;
    }
  }
  return [];
}

function normalizeRuntimeState(raw: unknown): RuntimeState {
  const token = readText(raw).trim().toUpperCase();
  if (token === 'SUCCESS' || token === 'COMPLETED' || token === 'DONE') return 'SUCCESS';
  if (token === 'RUNNING' || token === 'STARTED' || token === 'PROGRESS') return 'RUNNING';
  if (token === 'QUEUED' || token === 'PENDING' || token === 'WAITING') return 'QUEUED';
  if (token === 'FAILURE' || token === 'FAILED' || token === 'ERROR') return 'FAILURE';
  return 'UNSCORED';
}

function parseCandidateStructure(row: Record<string, unknown>): { structureText: string; structureFormat: 'cif' | 'pdb'; structureName: string } {
  const nested = [asRecord(row.result), asRecord(row.prediction), asRecord(row.structure_payload), asRecord(row.structure)];
  const candidates = [row, ...nested];
  for (const source of candidates) {
    const structureText = firstNonEmptyText(source, ['structureText', 'structure_text', 'cif_text', 'pdb_text', 'content']);
    if (!structureText) continue;
    const structureFormat = detectStructureFormat(
      structureText,
      source.structureFormat ?? source.structure_format ?? source.format
    );
    const structureName = firstNonEmptyText(source, ['structureName', 'structure_name', 'name']) || `design.${structureFormat}`;
    return { structureText, structureFormat, structureName };
  }
  return { structureText: '', structureFormat: 'cif', structureName: '' };
}

function normalizeModelLabel(raw: string): string {
  const token = raw.trim();
  if (!token) return '';
  const lower = token.toLowerCase();
  if (lower === 'alphafold3' || lower === 'af3') return 'AF3';
  if (lower === 'protenix') return 'Protenix';
  if (lower === 'boltz') return 'Boltz';
  if (lower === 'live' || lower === 'final' || lower === 'result') return '';
  return token;
}

function parseCandidateModelLabel(row: Record<string, unknown>, fallback: string): string {
  const nested = [asRecord(row.result), asRecord(row.prediction), asRecord(row.metadata), asRecord(row.structure_payload)];
  const candidates = [row, ...nested];
  for (const source of candidates) {
    const normalized = normalizeModelLabel(
      firstNonEmptyText(source, [
        'model',
        'backend',
        'engine',
        'model_backend',
        'prediction_backend',
        'backend_name',
        'backendLabel',
        'backend_label'
      ])
    );
    if (normalized) return normalized;
  }
  return normalizeModelLabel(fallback) || '-';
}

function extractRawCandidates(snapshotConfidence: Record<string, unknown>): Array<Record<string, unknown>> {
  const candidatePaths = [
    'peptide_design.best_sequences',
    'peptide_design.current_best_sequences',
    'peptide_design.candidates',
    'designer.best_sequences',
    'designer.current_best_sequences',
    'designer.candidates',
    'results.best_sequences',
    'results.current_best_sequences',
    'results.candidates',
    'best_sequences',
    'current_best_sequences',
    'peptide_candidates',
    'designed_peptides',
    'design_candidates',
    'candidates'
  ];
  for (const path of candidatePaths) {
    const rows = asRecordArray(readObjectPath(snapshotConfidence, path));
    if (rows.length > 0) return rows;
  }
  return [];
}

function candidateIdentity(row: PeptideDesignCandidate): string {
  if (row.sequence) {
    if (row.generation !== null) return `seq:${row.sequence}|gen:${row.generation}`;
    return `seq:${row.sequence}`;
  }
  if (row.structureName) return `structure:${row.structureName}`;
  return row.id;
}

function statePriority(state: RuntimeState): number {
  if (state === 'SUCCESS') return 5;
  if (state === 'RUNNING') return 4;
  if (state === 'QUEUED') return 3;
  if (state === 'UNSCORED') return 2;
  return 1;
}

function mergeCandidateRows(rows: PeptideDesignCandidate[]): PeptideDesignCandidate[] {
  const merged = new Map<string, PeptideDesignCandidate>();
  for (const row of rows) {
    const key = candidateIdentity(row);
    const prev = merged.get(key);
    if (!prev) {
      merged.set(key, row);
      continue;
    }
    const prevHasStructure = Boolean(prev.structureText.trim());
    const rowHasStructure = Boolean(row.structureText.trim());
    const next: PeptideDesignCandidate = {
      ...prev,
      id: prev.id,
      rank: Math.min(prev.rank, row.rank),
      sequence: prev.sequence || row.sequence,
      score:
        prev.score === null
          ? row.score
          : row.score === null
            ? prev.score
            : Math.max(prev.score, row.score),
      plddt:
        prev.plddt === null
          ? row.plddt
          : row.plddt === null
            ? prev.plddt
            : Math.max(prev.plddt, row.plddt),
      residuePlddts:
        row.residuePlddts.length > prev.residuePlddts.length ? row.residuePlddts : prev.residuePlddts,
      iptm:
        prev.iptm === null
          ? row.iptm
          : row.iptm === null
            ? prev.iptm
            : Math.max(prev.iptm, row.iptm),
      generation: prev.generation ?? row.generation,
      modelLabel: prev.modelLabel || row.modelLabel,
      structureText: rowHasStructure && !prevHasStructure ? row.structureText : prev.structureText,
      structureFormat: rowHasStructure && !prevHasStructure ? row.structureFormat : prev.structureFormat,
      structureName: rowHasStructure && !prevHasStructure ? row.structureName : prev.structureName,
      runtimeState: statePriority(row.runtimeState) > statePriority(prev.runtimeState) ? row.runtimeState : prev.runtimeState,
      source: prev.source === 'result' || row.source === 'result' ? 'result' : 'live'
    };
    merged.set(key, next);
  }
  return [...merged.values()];
}

function parseCandidateRows(
  rows: Array<Record<string, unknown>>,
  source: 'result' | 'live',
  defaultState: RuntimeState,
  defaultModelLabel: string,
  preferredLigandChainId?: string,
  preferredTargetChainId?: string
): PeptideDesignCandidate[] {
  return rows
    .map((row, index) => {
      const sequence = firstNonEmptyText(row, [
        'peptide_sequence',
        'binder_sequence',
        'candidate_sequence',
        'designed_sequence',
        'sequence'
      ])
        .replace(/\s+/g, '')
        .trim()
        .toUpperCase();
      const plddt = normalizePlddt(
        firstFiniteMetric(row, ['plddt', 'binder_avg_plddt', 'ligand_mean_plddt', 'mean_plddt'])
      );
      const iptm = resolvePairIptmForCandidate(row, preferredTargetChainId, preferredLigandChainId);
      const score = computePeptideCompositeScore(row, plddt, iptm);
      const generation = firstFiniteMetric(row, ['generation', 'iteration', 'iter']);
      const rankRaw = firstFiniteMetric(row, ['rank', 'ranking', 'order']);
      const structure = parseCandidateStructure(row);
      const residuePlddts = parseCandidateResiduePlddts(row, sequence, sequence.length, structure, preferredLigandChainId);
      const modelLabel = parseCandidateModelLabel(row, defaultModelLabel);
      const hasStructure = Boolean(structure.structureText.trim());
      const rowState = normalizeRuntimeState(
        row.runtime_state ?? row.state ?? row.status ?? row.prediction_state ?? row.task_state
      );
      const runtimeState = source === 'result'
        ? 'SUCCESS'
        : hasStructure
          ? 'SUCCESS'
          : rowState !== 'UNSCORED'
            ? rowState
            : defaultState;
      const idBase = readText(row.id).trim() || sequence || readText(rankRaw).trim() || `${index + 1}`;
      return {
        id: `peptide-design-${source}-${idBase}-${index + 1}`,
        rank: rankRaw === null ? index + 1 : Math.max(1, Math.floor(rankRaw)),
        sequence,
        score,
        plddt,
        residuePlddts,
        iptm,
        generation: generation === null ? null : Math.max(0, Math.floor(generation)),
        modelLabel,
        structureText: structure.structureText,
        structureFormat: structure.structureFormat,
        structureName: structure.structureName,
        runtimeState,
        source
      } as PeptideDesignCandidate;
    })
    .filter((row) => Boolean(row.sequence || row.structureText));
}

function parseProgressPercent(value: number | null): number | null {
  if (value === null) return null;
  const normalized = value <= 1 ? value * 100 : value;
  if (!Number.isFinite(normalized)) return null;
  return Math.max(0, Math.min(100, normalized));
}

function extractRuntimeContext(params: {
  statusInfo: Record<string, unknown>;
  snapshotConfidence: Record<string, unknown>;
  projectTaskState: string;
  fallbackProgressPercent: number;
}): PeptideRuntimeContext {
  const { statusInfo, snapshotConfidence, projectTaskState, fallbackProgressPercent } = params;
  const statusPayload = asRecord(statusInfo);
  const statusProgress = asRecord(statusPayload.progress);
  const statusPeptide = asRecord(statusPayload.peptide_design);
  const statusPeptideProgress = asRecord(statusPeptide.progress);
  const confidencePeptide = asRecord(snapshotConfidence.peptide_design);
  const confidencePeptideProgress = asRecord(confidencePeptide.progress);

  const payloads = [
    statusPayload,
    statusProgress,
    statusPeptide,
    statusPeptideProgress,
    confidencePeptide,
    confidencePeptideProgress
  ];

  const currentStatus = readFirstTextFromPaths(payloads, [
    'current_status',
    'status_stage',
    'stage',
    'progress.current_status'
  ]);

  const statusMessage = readFirstTextFromPaths(payloads, [
    'status_message',
    'message',
    'status',
    'progress.status_message'
  ]);

  const currentGeneration = readFirstFiniteFromPaths(payloads, [
    'current_generation',
    'generation',
    'iter',
    'progress.current_generation'
  ]);

  const totalGenerations = readFirstFiniteFromPaths(payloads, [
    'total_generations',
    'generations',
    'max_generation',
    'progress.total_generations'
  ]);

  const bestScore = readFirstFiniteFromPaths(payloads, ['best_score', 'current_best_score', 'score']);

  const completedTasks = readFirstFiniteFromPaths(payloads, ['completed_tasks', 'done_tasks', 'finished_tasks']);
  const pendingTasks = readFirstFiniteFromPaths(payloads, ['pending_tasks']);
  const totalTasks =
    readFirstFiniteFromPaths(payloads, ['total_tasks', 'task_total']) ??
    (completedTasks !== null && pendingTasks !== null ? completedTasks + pendingTasks : null);

  let progress = parseProgressPercent(
    readFirstFiniteFromPaths(payloads, [
      'estimated_progress',
      'progress_percent',
      'overall_progress',
      'progress_info.overall_progress'
    ])
  );
  if (progress === null && currentGeneration !== null && totalGenerations !== null && totalGenerations > 0) {
    progress = parseProgressPercent(currentGeneration / totalGenerations);
  }
  if (progress === null && totalTasks !== null && totalTasks > 0 && completedTasks !== null) {
    progress = parseProgressPercent(completedTasks / totalTasks);
  }
  if (progress === null && Number.isFinite(fallbackProgressPercent) && fallbackProgressPercent > 0) {
    progress = parseProgressPercent(fallbackProgressPercent);
  }

  const liveCandidateRows = readFirstRecordArrayFromPaths(
    [statusPayload, statusProgress, statusPeptide, statusPeptideProgress],
    [
      'progress.current_best_sequences',
      'progress.best_sequences',
      'current_best_sequences',
      'best_sequences',
      'current_candidates',
      'candidates'
    ]
  );

  const taskState = normalizeRuntimeState(projectTaskState);

  return {
    state: taskState,
    currentStatus,
    statusMessage,
    currentGeneration: currentGeneration === null ? null : Math.max(0, Math.floor(currentGeneration)),
    totalGenerations: totalGenerations === null ? null : Math.max(0, Math.floor(totalGenerations)),
    bestScore,
    progressPercent: progress,
    completedTasks: completedTasks === null ? null : Math.max(0, Math.floor(completedTasks)),
    pendingTasks: pendingTasks === null ? null : Math.max(0, Math.floor(pendingTasks)),
    totalTasks: totalTasks === null ? null : Math.max(0, Math.floor(totalTasks)),
    liveCandidateRows
  };
}

function formatScore(value: number | null): string {
  if (value === null) return '-';
  return value.toFixed(3);
}

function formatPlddt(value: number | null): string {
  if (value === null) return '-';
  return `${value.toFixed(1)}`;
}

function formatIptm(value: number | null): string {
  if (value === null) return '-';
  return value.toFixed(3);
}

function toneForPlddtValue(value: number | null): 'excellent' | 'good' | 'medium' | 'low' | 'neutral' {
  if (value === null) return 'neutral';
  if (value >= 90) return 'excellent';
  if (value >= 70) return 'good';
  if (value >= 50) return 'medium';
  return 'low';
}

function confidenceTone(value: number | null): ConfidenceTone {
  if (value === null || !Number.isFinite(value)) return 'na';
  if (value >= 90) return 'vhigh';
  if (value >= 70) return 'high';
  if (value >= 50) return 'low';
  return 'vlow';
}

function scoreConfidencePercent(value: number | null, minScore: number | null, maxScore: number | null): number | null {
  if (value === null || minScore === null || maxScore === null) return null;
  if (!Number.isFinite(value) || !Number.isFinite(minScore) || !Number.isFinite(maxScore)) return null;
  const span = maxScore - minScore;
  if (span <= 1e-9) return 75;
  const normalized = ((value - minScore) / span) * 100;
  return Math.max(0, Math.min(100, normalized));
}

function buildPeptideLigandViewTokens(sequence: string): string[] {
  const normalized = sequence.trim().toUpperCase().replace(/[^A-Z]/g, '');
  if (!normalized) return [];
  return normalized.split('');
}

export function PeptideDesignResultsWorkspace({
  projectTaskId,
  resultsGridRef,
  isResultsResizing,
  resultsGridStyle,
  onResizerPointerDown,
  onResizerKeyDown,
  snapshotConfidence,
  statusInfo,
  projectTaskState,
  progressPercent,
  displayStructureText,
  displayStructureFormat,
  selectedResultTargetChainId,
  selectedResultLigandChainId,
  selectedResultLigandSequence,
  confidenceBackend,
  projectBackend,
  fallbackPlddt,
  fallbackIptm
}: PeptideDesignResultsWorkspaceProps) {
  void selectedResultLigandSequence;
  void fallbackIptm;
  const runtimeContext = useMemo(
    () =>
      extractRuntimeContext({
        statusInfo: statusInfo || {},
        snapshotConfidence: snapshotConfidence || {},
        projectTaskState,
        fallbackProgressPercent: progressPercent
      }),
    [snapshotConfidence, statusInfo, projectTaskState, progressPercent]
  );

  const candidates = useMemo<PeptideDesignCandidate[]>(() => {
    const runtimeModelLabel = normalizeModelLabel(confidenceBackend) || normalizeModelLabel(projectBackend) || 'Boltz';
    const finalizedRows = extractRawCandidates(snapshotConfidence || {});
    const liveRows = runtimeContext.liveCandidateRows;
    const liveDefaultState = runtimeContext.state === 'UNSCORED' ? 'RUNNING' : runtimeContext.state;

    const fallbackPairIptm = resolvePairIptmForCandidate(
      snapshotConfidence || {},
      selectedResultTargetChainId || undefined,
      selectedResultLigandChainId || undefined
    );

    const merged = mergeCandidateRows([
      ...parseCandidateRows(
        finalizedRows,
        'result',
        'SUCCESS',
        runtimeModelLabel,
        selectedResultLigandChainId || undefined,
        selectedResultTargetChainId || undefined
      ),
      ...parseCandidateRows(
        liveRows,
        'live',
        liveDefaultState,
        runtimeModelLabel,
        selectedResultLigandChainId || undefined,
        selectedResultTargetChainId || undefined
      )
    ])
      .sort((a, b) => {
        if (a.score !== null && b.score !== null && a.score !== b.score) return b.score - a.score;
        if (a.plddt !== null && b.plddt !== null && a.plddt !== b.plddt) return b.plddt - a.plddt;
        if (a.generation !== null && b.generation !== null && a.generation !== b.generation) return b.generation - a.generation;
        return a.rank - b.rank;
      })
      .map((item, index) => ({ ...item, rank: index + 1 }));

    if (merged.length > 0) return merged;

    const fallbackStructure = readText(displayStructureText).trim();
    if (!fallbackStructure) return [];
    return [
      {
        id: 'peptide-design-fallback',
        rank: 1,
        sequence: '',
        score: null,
        plddt: normalizePlddt(fallbackPlddt),
        residuePlddts: [],
        iptm: fallbackPairIptm,
        generation: null,
        modelLabel: runtimeModelLabel,
        structureText: fallbackStructure,
        structureFormat: displayStructureFormat,
        structureName: 'Current result',
        runtimeState: runtimeContext.state,
        source: 'result'
      }
    ];
  }, [
    snapshotConfidence,
    selectedResultTargetChainId,
    selectedResultLigandSequence,
    selectedResultLigandChainId,
    displayStructureText,
    displayStructureFormat,
    fallbackPlddt,
    fallbackIptm,
    runtimeContext
  ]);

  const [selectedCandidateId, setSelectedCandidateId] = useState('');
  const initialViewerColorMode: 'default' | 'alphafold' =
    confidenceBackend === 'alphafold3' ||
    confidenceBackend === 'protenix' ||
    projectBackend === 'alphafold3' ||
    projectBackend === 'protenix'
      ? 'alphafold'
      : 'default';
  const [viewerColorMode, setViewerColorMode] = useState<'default' | 'alphafold'>(initialViewerColorMode);
  const [cardMode, setCardMode] = useState(false);
  const [sortKey, setSortKey] = useState<PeptideSortKey>('score');
  const [sortDirection, setSortDirection] = useState<'asc' | 'desc'>('desc');

  const sortedCandidates = useMemo(() => {
    const sorted = [...candidates];
    const dir = sortDirection === 'asc' ? 1 : -1;
    const score = (value: number | null) => (value === null ? Number.NEGATIVE_INFINITY : value);

    sorted.sort((a, b) => {
      let diff = 0;
      if (sortKey === 'rank') diff = a.rank - b.rank;
      if (sortKey === 'generation') diff = score(a.generation) - score(b.generation);
      if (sortKey === 'score') diff = score(a.score) - score(b.score);
      if (sortKey === 'plddt') diff = score(a.plddt) - score(b.plddt);
      if (sortKey === 'iptm') diff = score(a.iptm) - score(b.iptm);

      if (diff !== 0) return diff * dir;
      return a.rank - b.rank;
    });
    return sorted;
  }, [candidates, sortDirection, sortKey]);

  const scoreRange = useMemo(() => {
    const values = sortedCandidates
      .map((candidate) => candidate.score)
      .filter((value): value is number => value !== null && Number.isFinite(value));
    if (values.length === 0) {
      return { min: null as number | null, max: null as number | null };
    }
    return { min: Math.min(...values), max: Math.max(...values) };
  }, [sortedCandidates]);

  useEffect(() => {
    setViewerColorMode(initialViewerColorMode);
  }, [initialViewerColorMode]);

  useEffect(() => {
    if (!sortedCandidates.length) {
      setSelectedCandidateId('');
      return;
    }
    if (!selectedCandidateId || !sortedCandidates.some((item) => item.id === selectedCandidateId)) {
      setSelectedCandidateId(sortedCandidates[0].id);
    }
  }, [sortedCandidates, selectedCandidateId]);

  const selectedCandidate = useMemo(() => {
    if (!sortedCandidates.length) return null;
    return sortedCandidates.find((item) => item.id === selectedCandidateId) || sortedCandidates[0];
  }, [sortedCandidates, selectedCandidateId]);

  useEffect(() => {
    if (sortedCandidates.length > 0) return;
    setCardMode(false);
  }, [sortedCandidates.length]);

  useEffect(() => {
    setCardMode(false);
    setSelectedCandidateId('');
  }, [projectTaskId]);

  const hasCandidateRows = sortedCandidates.length > 0;
  const viewerStructureText = hasCandidateRows ? selectedCandidate?.structureText || '' : displayStructureText;
  const viewerStructureFormat = selectedCandidate?.structureText ? selectedCandidate.structureFormat : displayStructureFormat;
  const viewerLigandFocusChainId = useMemo(() => {
    const preferredChain = selectedResultLigandChainId || undefined;
    const candidateSequence = readText(selectedCandidate?.sequence || '').trim().toUpperCase();
    const structureTextForFocus = readText(viewerStructureText).trim();
    if (!structureTextForFocus) return selectedResultLigandChainId || '';
    const focusChain = resolvePeptideFocusChainId(
      structureTextForFocus,
      viewerStructureFormat,
      candidateSequence,
      preferredChain
    );
    return focusChain || selectedResultLigandChainId || '';
  }, [selectedCandidate?.sequence, selectedResultLigandChainId, viewerStructureFormat, viewerStructureText]);

  const openCandidateCard = (candidateId: string) => {
    setSelectedCandidateId(candidateId);
    setCardMode(true);
  };

  const onSort = (key: PeptideSortKey) => {
    if (sortKey === key) {
      setSortDirection((prev) => (prev === 'asc' ? 'desc' : 'asc'));
      return;
    }
    setSortKey(key);
    setSortDirection(key === 'rank' ? 'asc' : 'desc');
  };

  const sortMark = (key: PeptideSortKey) => {
    if (sortKey !== key) return '';
    return sortDirection === 'asc' ? ' \u2191' : ' \u2193';
  };

  const renderViewerModeSwitch = () => (
    <div className="prediction-render-mode-switch" role="tablist" aria-label="3D color mode">
      <button
        type="button"
        role="tab"
        aria-selected={viewerColorMode === 'alphafold'}
        className={`prediction-render-mode-btn ${viewerColorMode === 'alphafold' ? 'active' : ''}`}
        onClick={() => setViewerColorMode('alphafold')}
        title="Color structure by model confidence"
      >
        AF
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={viewerColorMode === 'default'}
        className={`prediction-render-mode-btn ${viewerColorMode === 'default' ? 'active' : ''}`}
        onClick={() => setViewerColorMode('default')}
        title="Use standard element colors"
      >
        Std
      </button>
    </div>
  );

  const renderCandidateTable = (standalone = false) => (
    <section className={`peptide-result-list-panel${standalone ? ' peptide-result-list-panel--standalone' : ''}`}>
      <div className="lead-opt-result-table-wrap peptide-result-table-wrap">
        <table className="lead-opt-candidate-table lead-opt-result-table peptide-result-table">
          <thead>
            <tr>
              <th className="col-rank">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('rank')}>
                  #{sortMark('rank')}
                </button>
              </th>
              <th className="col-actions peptide-col-open">2D</th>
              <th className="col-n">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('generation')}>
                  Gen{sortMark('generation')}
                </button>
              </th>
              <th className="col-delta">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('score')}>
                  Score{sortMark('score')}
                </button>
              </th>
              <th className="col-insights peptide-col-metric">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('plddt')}>
                  pLDDT{sortMark('plddt')}
                </button>
              </th>
              <th className="col-insights peptide-col-metric">
                <button type="button" className="peptide-sort-btn" onClick={() => onSort('iptm')}>
                  ipTM{sortMark('iptm')}
                </button>
              </th>
            </tr>
          </thead>
          <tbody>
            {sortedCandidates.map((candidate) => {
              const isActive = candidate.id === selectedCandidate?.id;
              const scoreTone = confidenceTone(scoreConfidencePercent(candidate.score, scoreRange.min, scoreRange.max));
              const plddtTone = confidenceTone(candidate.plddt);
              const iptmTone = confidenceTone(candidate.iptm === null ? null : candidate.iptm * 100);
              const sequenceTokens = buildPeptideLigandViewTokens(candidate.sequence);
              const sequenceRows = Array.from(
                { length: Math.ceil(sequenceTokens.length / 10) },
                (_, rowIdx) => sequenceTokens.slice(rowIdx * 10, rowIdx * 10 + 10)
              );
              return (
                <tr
                  key={candidate.id}
                  className={isActive ? 'selected' : ''}
                  onClick={() => {
                    if (cardMode) setSelectedCandidateId(candidate.id);
                  }}
                >
                  <td className="col-rank">{candidate.rank}</td>
                  <td className="col-actions peptide-col-open">
                    <button
                      type="button"
                      className="peptide-ligand-preview-btn"
                      title="Open in 3D card view"
                      aria-label="Open in 3D card view"
                      onClick={(event) => {
                        event.stopPropagation();
                        openCandidateCard(candidate.id);
                      }}
                    >
                      <span className="peptide-ligand-preview-track">
                        {sequenceRows.length > 0 ? (
                          sequenceRows.map((rowTokens, rowIdx) => (
                            <span className="peptide-ligand-preview-row" key={`${candidate.id}-ligand-view-row-${rowIdx}`}>
                              {rowTokens.map((residue, idx) => {
                                const residueIdx = rowIdx * 10 + idx;
                                const residuePlddt = candidate.residuePlddts[residueIdx] ?? null;
                                const residueTone = toneForPlddtValue(residuePlddt);
                                return (
                                  <span className="peptide-ligand-preview-node-wrap" key={`${candidate.id}-ligand-view-${residueIdx}`}>
                                    {idx > 0 ? (
                                      <span className={`peptide-ligand-preview-link tone-${residueTone}`} aria-hidden="true" />
                                    ) : null}
                                    <span
                                      className={`peptide-ligand-preview-node tone-${residueTone}`}
                                      title={`#${residueIdx + 1} ${residue} | pLDDT ${residuePlddt === null ? '-' : residuePlddt.toFixed(1)}`}
                                    >
                                      {residue}
                                    </span>
                                  </span>
                                );
                              })}
                            </span>
                          ))
                        ) : (
                          <span className="peptide-ligand-preview-empty">-</span>
                        )}
                      </span>
                    </button>
                  </td>
                  <td className="col-n">{candidate.generation !== null ? candidate.generation : '-'}</td>
                  <td className="col-delta">
                    <span className={`peptide-table-value conf-tone-${scoreTone}`}>{formatScore(candidate.score)}</span>
                  </td>
                  <td className="col-insights peptide-col-metric">
                    <span className={`peptide-table-value conf-tone-${plddtTone}`}>{formatPlddt(candidate.plddt)}</span>
                  </td>
                  <td className="col-insights peptide-col-metric">
                    <span className={`peptide-table-value conf-tone-${iptmTone}`}>{formatIptm(candidate.iptm)}</span>
                  </td>
                </tr>
              );
            })}
            {sortedCandidates.length === 0 ? (
              <tr>
                <td colSpan={6}>
                  <div className="ligand-preview-empty">No designed peptide records yet.</div>
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </section>
  );

  const renderCandidateCards = () => (
    <section className="peptide-result-card-panel">
      <div className="lead-opt-query-toolbar lead-opt-query-toolbar--single-row peptide-result-toolbar peptide-result-toolbar--card">
        <div className="peptide-result-toolbar-left">
          <button
            type="button"
            className="lead-opt-row-action-btn lead-opt-card-exit-btn"
            onClick={() => setCardMode(false)}
            title="Exit cards"
            aria-label="Exit cards"
          >
            <X size={14} />
          </button>
        </div>
        <span className="lead-opt-query-toolbar-spacer" />
        <div className="lead-opt-query-toolbar-right">
          {renderViewerModeSwitch()}
        </div>
      </div>
      {sortedCandidates.length === 0 ? (
        <section className="result-aside-block peptide-selected-card">
          <div className="ligand-preview-empty">No designed peptide cards yet.</div>
        </section>
      ) : (
        <div className="peptide-card-list-wrap">
          <div className="lead-opt-card-list peptide-card-list">
            {sortedCandidates.map((candidate) => {
              const scoreTone = confidenceTone(scoreConfidencePercent(candidate.score, scoreRange.min, scoreRange.max));
              const plddtTone = confidenceTone(candidate.plddt);
              const iptmTone = confidenceTone(candidate.iptm === null ? null : candidate.iptm * 100);
              const sequenceTokens = buildPeptideLigandViewTokens(candidate.sequence);
              const sequenceRows = Array.from(
                { length: Math.ceil(sequenceTokens.length / 5) },
                (_, rowIdx) => sequenceTokens.slice(rowIdx * 5, rowIdx * 5 + 5)
              );
              const isActive = candidate.id === selectedCandidate?.id;
              return (
                <article
                  key={candidate.id}
                  className={`lead-opt-result-card peptide-result-card${isActive ? ' selected' : ''}`}
                  onClick={() => setSelectedCandidateId(candidate.id)}
                  onKeyDown={(event) => {
                    if (event.key !== 'Enter' && event.key !== ' ') return;
                    event.preventDefault();
                    setSelectedCandidateId(candidate.id);
                  }}
                  role="button"
                  tabIndex={0}
                  aria-label={`Open peptide card ${candidate.rank}`}
                >
                  <div className="lead-opt-result-card-head">
                    <strong>#{candidate.rank}</strong>
                    <span className="muted small">Gen {candidate.generation !== null ? candidate.generation : '-'}</span>
                  </div>
                  <div className="lead-opt-result-card-media peptide-result-card-media">
                    <span className="peptide-ligand-preview-track peptide-ligand-preview-track--card">
                      {sequenceRows.length > 0 ? (
                        sequenceRows.map((rowTokens, rowIdx) => (
                          <span className="peptide-ligand-preview-row peptide-ligand-preview-row--card" key={`${candidate.id}-card-row-${rowIdx}`}>
                            {rowTokens.map((residue, idx) => {
                              const residueIdx = rowIdx * 5 + idx;
                              const residuePlddt = candidate.residuePlddts[residueIdx] ?? null;
                              const residueTone = toneForPlddtValue(residuePlddt);
                              return (
                                <span className="peptide-ligand-preview-node-wrap" key={`${candidate.id}-card-${residueIdx}`}>
                                  {idx > 0 ? (
                                    <span className={`peptide-ligand-preview-link tone-${residueTone}`} aria-hidden="true" />
                                  ) : null}
                                  <span
                                    className={`peptide-ligand-preview-node peptide-ligand-preview-node--card tone-${residueTone}`}
                                    title={`#${residueIdx + 1} ${residue} | pLDDT ${residuePlddt === null ? '-' : residuePlddt.toFixed(1)}`}
                                  >
                                    {residue}
                                  </span>
                                </span>
                              );
                            })}
                          </span>
                        ))
                      ) : (
                        <span className="peptide-ligand-preview-empty">-</span>
                      )}
                    </span>
                  </div>
                  <div className="lead-opt-card-metric-strip peptide-card-metric-strip">
                    <span className={`lead-opt-card-pill conf-tone-${scoreTone}`}>
                      <span className="lead-opt-card-pill-key">Score</span>
                      <strong>{formatScore(candidate.score)}</strong>
                    </span>
                    <span className={`lead-opt-card-pill conf-tone-${plddtTone}`}>
                      <span className="lead-opt-card-pill-key">pLDDT</span>
                      <strong>{formatPlddt(candidate.plddt)}</strong>
                    </span>
                    <span className={`lead-opt-card-pill conf-tone-${iptmTone}`}>
                      <span className="lead-opt-card-pill-key">ipTM</span>
                      <strong>{formatIptm(candidate.iptm)}</strong>
                    </span>
                    <span className="lead-opt-card-pill">
                      <span className="lead-opt-card-pill-key">Gen</span>
                      <strong>{candidate.generation !== null ? candidate.generation : '-'}</strong>
                    </span>
                  </div>
                </article>
              );
            })}
          </div>
        </div>
      )}
    </section>
  );

  if (!cardMode) {
    return renderCandidateTable(true);
  }

  return (
    <div
      ref={resultsGridRef}
      className={`results-grid peptide-results-grid--card ${isResultsResizing ? 'is-resizing' : ''}`}
      style={resultsGridStyle}
    >
      <section className="structure-panel structure-panel--results-compact peptide-results-structure-panel">
        {viewerStructureText.trim() ? (
          <MolstarViewer
            key={`peptide-results-viewer:${selectedCandidate?.id || 'none'}:${viewerStructureFormat}:${viewerLigandFocusChainId || '-'}`}
            structureText={viewerStructureText}
            format={viewerStructureFormat}
            colorMode={viewerColorMode}
            confidenceBackend={confidenceBackend || projectBackend}
            scenePreset="lead_opt"
            leadOptStyleVariant="results"
            ligandFocusChainId={viewerLigandFocusChainId || ''}
            interactionGranularity="element"
            suppressAutoFocus={false}
            showSequence={false}
          />
        ) : (
          <div className="ligand-preview-empty">Selected peptide has no precomputed structure yet.</div>
        )}
      </section>

      <div
        className={`results-resizer ${isResultsResizing ? 'dragging' : ''}`}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize structure and peptide result panels"
        tabIndex={0}
        onPointerDown={onResizerPointerDown}
        onKeyDown={onResizerKeyDown}
      />

      <aside className="info-panel">{renderCandidateCards()}</aside>
    </div>
  );
}
