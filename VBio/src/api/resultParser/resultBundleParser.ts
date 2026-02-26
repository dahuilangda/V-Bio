import type JSZip from 'jszip';
import type { ParsedResultBundle } from '../../types/models';
import { downloadResultBlob } from '../backendTaskApi';
import {
  ensureCifHasMaQaMetricLocal,
  ensureStructureConfidenceColoringData
} from './cifConfidenceColoring';

function getBaseName(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function parseJsonObject(text: string | null | undefined): Record<string, unknown> | null {
  if (!text) return null;
  try {
    const parsed = JSON.parse(text) as unknown;
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

function choosePreferredPath(candidates: string[]): string | null {
  if (!candidates.length) return null;
  return [...candidates].sort((a, b) => {
    const aSeed = a.toLowerCase().includes('seed-');
    const bSeed = b.toLowerCase().includes('seed-');
    if (aSeed !== bSeed) return aSeed ? 1 : -1;
    return a.length - b.length;
  })[0];
}

function chooseBestBoltzStructureFile(names: string[]): string | null {
  const candidates = names
    .filter((name) => /\.(cif|mmcif|pdb)$/i.test(name) && !name.toLowerCase().includes('af3/output/'))
    .map((name) => {
      const lower = name.toLowerCase();
      let score = 100;
      if (lower.endsWith('.cif')) score -= 5;
      if (lower.includes('model_0') || lower.includes('ranked_0')) score -= 20;
      else if (lower.includes('model_') || lower.includes('ranked_')) score -= 5;
      return { name, score };
    })
    .sort((a, b) => a.score - b.score || a.name.length - b.name.length);
  return candidates[0]?.name ?? null;
}

function boltzConfidenceHeuristicScore(path: string): number {
  const lower = path.toLowerCase();
  let score = 100;
  if (lower.includes('confidence_')) score -= 5;
  if (lower.includes('model_0') || lower.includes('ranked_0')) score -= 20;
  else if (lower.includes('model_') || lower.includes('ranked_')) score -= 5;
  return score;
}

function resolveBoltzStructureForConfidence(names: string[], confidencePath: string): string | null {
  const base = getBaseName(confidencePath);
  if (!base.toLowerCase().startsWith('confidence_') || !base.toLowerCase().endsWith('.json')) {
    return null;
  }
  const structureStem = base.slice('confidence_'.length, -'.json'.length);
  if (!structureStem.trim()) return null;

  const dir = confidencePath.includes('/') ? confidencePath.slice(0, confidencePath.lastIndexOf('/')) : '';
  const withDir = (file: string) => (dir ? `${dir}/${file}` : file);
  const candidates = [withDir(`${structureStem}.cif`), withDir(`${structureStem}.mmcif`), withDir(`${structureStem}.pdb`)];
  return candidates.find((candidate) => names.includes(candidate)) || null;
}

async function chooseBestBoltzStructureAndConfidence(
  zip: JSZip,
  names: string[]
): Promise<{ structureFile: string | null; confidenceFile: string | null }> {
  const confidenceCandidates = names.filter((name) => {
    const lower = name.toLowerCase();
    return lower.endsWith('.json') && lower.includes('confidence') && !lower.includes('af3/output/');
  });

  if (confidenceCandidates.length === 0) {
    return {
      structureFile: chooseBestBoltzStructureFile(names),
      confidenceFile: null
    };
  }

  const ligandMeanByStructure = new Map<string, Promise<number | null>>();
  const readLigandMeanForStructure = (structureFile: string | null): Promise<number | null> => {
    if (!structureFile) return Promise.resolve(null);
    const cached = ligandMeanByStructure.get(structureFile);
    if (cached) return cached;
    const pending = (async () => {
      const structureText = await zip.file(structureFile)?.async('text');
      if (!structureText) return null;
      const structureFormat: 'cif' | 'pdb' = getBaseName(structureFile).toLowerCase().endsWith('.pdb') ? 'pdb' : 'cif';
      const byChain = extractLigandAtomPlddtsByChainFromStructure(structureText, structureFormat);
      const values = Object.values(byChain)
        .flat()
        .filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
      const avg = mean(values);
      return avg === null ? null : normalizePlddt(avg);
    })();
    ligandMeanByStructure.set(structureFile, pending);
    return pending;
  };

  const scoredCandidates = await Promise.all(
    confidenceCandidates.map(async (file) => {
      const payload = await readZipJson(zip, file);
      const matchedStructure = resolveBoltzStructureForConfidence(names, file);
      const ligandMeanFromPayload = payload ? toFiniteNumber(payload.ligand_mean_plddt) : null;
      const ligandMeanPlddt =
        ligandMeanFromPayload !== null
          ? normalizePlddt(ligandMeanFromPayload)
          : await readLigandMeanForStructure(matchedStructure);
      return {
        file,
        matchedStructure,
        ligandMeanPlddt,
        confidenceScore: payload ? toFiniteNumber(payload.confidence_score) : null,
        complexPlddt: payload ? toFiniteNumber(payload.complex_plddt) : null,
        iptm: payload ? toFiniteNumber(payload.iptm) : null,
        heuristicScore: boltzConfidenceHeuristicScore(file)
      };
    })
  );

  scoredCandidates.sort((a, b) => {
    const aHasLigandMean = a.ligandMeanPlddt !== null ? 1 : 0;
    const bHasLigandMean = b.ligandMeanPlddt !== null ? 1 : 0;
    if (aHasLigandMean !== bHasLigandMean) return bHasLigandMean - aHasLigandMean;
    if (a.ligandMeanPlddt !== null && b.ligandMeanPlddt !== null && a.ligandMeanPlddt !== b.ligandMeanPlddt) {
      return b.ligandMeanPlddt - a.ligandMeanPlddt;
    }

    const aHasConfidence = a.confidenceScore !== null ? 1 : 0;
    const bHasConfidence = b.confidenceScore !== null ? 1 : 0;
    if (aHasConfidence !== bHasConfidence) return bHasConfidence - aHasConfidence;
    if (a.confidenceScore !== null && b.confidenceScore !== null && a.confidenceScore !== b.confidenceScore) {
      return b.confidenceScore - a.confidenceScore;
    }

    const aHasPlddt = a.complexPlddt !== null ? 1 : 0;
    const bHasPlddt = b.complexPlddt !== null ? 1 : 0;
    if (aHasPlddt !== bHasPlddt) return bHasPlddt - aHasPlddt;
    if (a.complexPlddt !== null && b.complexPlddt !== null && a.complexPlddt !== b.complexPlddt) {
      return b.complexPlddt - a.complexPlddt;
    }

    const aHasIptm = a.iptm !== null ? 1 : 0;
    const bHasIptm = b.iptm !== null ? 1 : 0;
    if (aHasIptm !== bHasIptm) return bHasIptm - aHasIptm;
    if (a.iptm !== null && b.iptm !== null && a.iptm !== b.iptm) {
      return b.iptm - a.iptm;
    }

    if (a.heuristicScore !== b.heuristicScore) return a.heuristicScore - b.heuristicScore;
    return a.file.length - b.file.length;
  });

  const selectedConfidence = scoredCandidates[0]?.file || null;
  const matchedStructure = scoredCandidates[0]?.matchedStructure || null;
  return {
    structureFile: matchedStructure || chooseBestBoltzStructureFile(names),
    confidenceFile: selectedConfidence
  };
}

function chooseBestAf3StructureFile(names: string[]): string | null {
  const candidates = names
    .filter((name) => /\.(cif|mmcif|pdb)$/i.test(name) && name.toLowerCase().includes('af3/output/'))
    .map((name) => {
      const lower = name.toLowerCase();
      let score = 100;
      if (lower.endsWith('.cif')) score -= 5;
      // Prefer AF3's consolidated best model over per-seed samples.
      if (getBaseName(lower) === 'boltz_af3_model.cif') score -= 30;
      if (lower.includes('/model.cif') || lower.endsWith('model.cif')) score -= 8;
      if (lower.includes('seed-')) score += 8;
      else score -= 6;
      if (lower.includes('predicted')) score -= 1;
      if (lower.includes('model')) score -= 1;
      return { name, score };
    })
    .sort((a, b) => a.score - b.score || a.name.length - b.name.length);
  return candidates[0]?.name ?? null;
}

const PROTENIX_SUMMARY_SAMPLE_RE = /_summary_confidence_sample_(\d+)\.json$/i;

async function chooseBestProtenixStructureAndConfidence(
  zip: JSZip,
  names: string[]
): Promise<{ structureFile: string | null; confidenceFile: string | null }> {
  const canonicalStructures = [
    'protenix/output/protenix_model_0.cif',
    'protenix/output/protenix_model_0.mmcif',
    'protenix/output/protenix_model_0.pdb'
  ];
  const canonicalConfidence = 'protenix/output/confidence_protenix_model_0.json';
  const canonicalStructure = canonicalStructures.find((candidate) => names.includes(candidate)) || null;
  if (canonicalStructure && names.includes(canonicalConfidence)) {
    return { structureFile: canonicalStructure, confidenceFile: canonicalConfidence };
  }

  const summaryCandidates = names.filter((name) => {
    const lower = name.toLowerCase();
    if (!lower.startsWith('protenix/output/')) return false;
    if (!lower.endsWith('.json')) return false;
    return PROTENIX_SUMMARY_SAMPLE_RE.test(getBaseName(name));
  });
  if (summaryCandidates.length === 0) {
    return { structureFile: null, confidenceFile: null };
  }

  const scored: Array<{ file: string; score: number }> = [];
  for (const file of summaryCandidates) {
    const payload = await readZipJson(zip, file);
    if (!payload) continue;
    const score = toFiniteNumber(payload.ranking_score);
    if (score === null) continue;
    scored.push({ file, score });
  }
  if (scored.length === 0) {
    throw new Error('Protenix summary confidence exists but ranking_score is missing.');
  }
  scored.sort((a, b) => b.score - a.score || a.file.length - b.file.length);
  const selectedSummary = scored[0].file;
  const sampleMatch = PROTENIX_SUMMARY_SAMPLE_RE.exec(getBaseName(selectedSummary));
  if (!sampleMatch) {
    throw new Error(`Cannot parse sample index from Protenix summary: ${selectedSummary}`);
  }
  const sampleIndex = sampleMatch[1];
  const summaryDir = selectedSummary.includes('/') ? selectedSummary.slice(0, selectedSummary.lastIndexOf('/')) : '';
  const base = getBaseName(selectedSummary);
  const structureBase = base.replace(/_summary_confidence_sample_\d+\.json$/i, `_sample_${sampleIndex}`);
  const withSummaryDir = (file: string) => (summaryDir ? `${summaryDir}/${file}` : file);
  const structureCandidates = [
    withSummaryDir(`${structureBase}.cif`),
    withSummaryDir(`${structureBase}.mmcif`),
    withSummaryDir(`${structureBase}.pdb`)
  ];
  const structureFile = structureCandidates.find((candidate) => names.includes(candidate)) || null;
  if (!structureFile) {
    throw new Error(
      `Cannot locate Protenix structure for summary ${selectedSummary} (sample ${sampleIndex}).`
    );
  }
  return { structureFile, confidenceFile: selectedSummary };
}

function flattenNumberMatrix(values: unknown): number[] {
  if (!Array.isArray(values)) return [];
  const out: number[] = [];
  for (const row of values) {
    if (!Array.isArray(row)) continue;
    for (const value of row) {
      if (typeof value === 'number' && Number.isFinite(value)) {
        out.push(value);
      }
    }
  }
  return out;
}

function mean(values: number[]): number | null {
  if (!values.length) return null;
  return values.reduce((sum, value) => sum + value, 0) / values.length;
}

async function readZipJson(zip: JSZip, path: string | null): Promise<Record<string, unknown> | null> {
  if (!path) return null;
  const text = await zip.file(path)?.async('text');
  return parseJsonObject(text);
}

function toFiniteNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}

function normalizePlddt(value: number): number {
  if (!Number.isFinite(value)) return 0;
  if (value >= 0 && value <= 1) return value * 100;
  return value;
}

function formatPlddtNumber(value: number): string {
  return normalizePlddt(value).toFixed(2);
}

function tokenizeCifRow(row: string): string[] {
  const matcher = /'(?:[^']*)'|"(?:[^"]*)"|[^\s]+/g;
  const tokens: string[] = [];
  let match: RegExpExecArray | null = matcher.exec(row);
  while (match) {
    tokens.push(match[0]);
    match = matcher.exec(row);
  }
  return tokens;
}

function stripCifTokenQuotes(value: string): string {
  if (value.length >= 2) {
    if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
      return value.slice(1, -1);
    }
  }
  return value;
}

function applyAtomPlddtToCifStructure(structureText: string, atomPlddts: number[]): string {
  if (!structureText || atomPlddts.length === 0) return structureText;
  const lines = structureText.split(/\r?\n/);
  let atomIndex = 0;

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i].trim();
    if (line !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    if (!headers.some((header) => header.startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const bFactorCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.b_iso_or_equiv');
    if (bFactorCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length && atomIndex < atomPlddts.length) {
        tokens[bFactorCol] = formatPlddtNumber(atomPlddts[atomIndex]);
        lines[rowIndex] = tokens.join(' ');
        atomIndex += 1;
      }
      rowIndex += 1;
    }

    i = rowIndex - 1;
    if (atomIndex >= atomPlddts.length) break;
  }

  return lines.join('\n');
}

function applyAtomPlddtToPdbStructure(structureText: string, atomPlddts: number[]): string {
  if (!structureText || atomPlddts.length === 0) return structureText;
  const lines = structureText.split(/\r?\n/);
  let atomIndex = 0;

  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!(line.startsWith('ATOM') || line.startsWith('HETATM'))) continue;
    if (atomIndex >= atomPlddts.length) break;

    const bFactor = normalizePlddt(atomPlddts[atomIndex]).toFixed(2).padStart(6);
    const padded = line.length >= 66 ? line : line.padEnd(66, ' ');
    lines[i] = `${padded.slice(0, 60)}${bFactor}${padded.slice(66)}`;
    atomIndex += 1;
  }

  return lines.join('\n');
}

function applyAtomPlddtToStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  atomPlddts: number[]
): string {
  if (!atomPlddts.length) return structureText;
  if (structureFormat === 'pdb') return applyAtomPlddtToPdbStructure(structureText, atomPlddts);
  return applyAtomPlddtToCifStructure(structureText, atomPlddts);
}

function applyLigandAtomPlddtsByChainAndNameToCifStructure(
  structureText: string,
  atomPlddtsByChainAndName: Record<string, Record<string, number>>
): string {
  if (!structureText) return structureText;
  if (Object.keys(atomPlddtsByChainAndName).length === 0) return structureText;

  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    if (!headers.some((header) => header.toLowerCase().startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const groupCol = findHeaderIndex(headers, ['_atom_site.group_pdb']);
    const chainCol = findHeaderIndex(headers, ['_atom_site.label_asym_id', '_atom_site.auth_asym_id']);
    const seqCol = findHeaderIndex(headers, ['_atom_site.label_seq_id', '_atom_site.auth_seq_id']);
    const compCol = findHeaderIndex(headers, ['_atom_site.label_comp_id', '_atom_site.auth_comp_id']);
    const bFactorCol = findHeaderIndex(headers, ['_atom_site.b_iso_or_equiv']);
    const typeCol = findHeaderIndex(headers, ['_atom_site.type_symbol']);
    const atomIdCol = findHeaderIndex(headers, ['_atom_site.label_atom_id', '_atom_site.auth_atom_id']);
    if (compCol < 0 || chainCol < 0 || bFactorCol < 0 || atomIdCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    const firstResidueKeyByChain = new Map<string, string>();
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
        const groupPdb = groupCol >= 0 ? stripCifTokenQuotes(tokens[groupCol]).trim().toUpperCase() : '';
        const element = typeCol >= 0 ? stripCifTokenQuotes(tokens[typeCol]) : '';
        const atomName = stripCifTokenQuotes(tokens[atomIdCol]).trim();
        if (!compId || WATER_COMP_IDS.has(compId)) {
          rowIndex += 1;
          continue;
        }
        if (groupPdb) {
          if (groupPdb !== 'HETATM') {
            rowIndex += 1;
            continue;
          }
        } else if (POLYMER_COMP_IDS.has(compId)) {
          rowIndex += 1;
          continue;
        }
        if (isHydrogenLikeElement(element || atomName)) {
          rowIndex += 1;
          continue;
        }

        const chainIdRaw = stripCifTokenQuotes(tokens[chainCol]).trim();
        const chainId = chainIdRaw.toUpperCase();
        if (!chainId) {
          rowIndex += 1;
          continue;
        }
        const seqId = seqCol >= 0 ? stripCifTokenQuotes(tokens[seqCol]).trim() : '';
        const residueKey = `${chainId}|${seqId}|${compId}`;
        if (!firstResidueKeyByChain.has(chainId)) {
          firstResidueKeyByChain.set(chainId, residueKey);
        }
        if (firstResidueKeyByChain.get(chainId) !== residueKey) {
          rowIndex += 1;
          continue;
        }

        const byName = atomPlddtsByChainAndName[chainId];
        if (!byName || typeof byName !== 'object') {
          rowIndex += 1;
          continue;
        }
        const atomKey = normalizeAtomNameKey(atomName);
        if (!atomKey || !Object.prototype.hasOwnProperty.call(byName, atomKey)) {
          rowIndex += 1;
          continue;
        }
        tokens[bFactorCol] = formatPlddtNumber(byName[atomKey]);
        lines[rowIndex] = tokens.join(' ');
      }
      rowIndex += 1;
    }

    i = rowIndex - 1;
  }

  return lines.join('\n');
}

function applyLigandAtomPlddtsByChainAndNameToPdbStructure(
  structureText: string,
  atomPlddtsByChainAndName: Record<string, Record<string, number>>
): string {
  if (!structureText) return structureText;
  if (Object.keys(atomPlddtsByChainAndName).length === 0) return structureText;

  const lines = structureText.split(/\r?\n/);
  const firstResidueKeyByChain = new Map<string, string>();
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    if (!line.startsWith('HETATM')) continue;

    const compId = line.slice(17, 20).trim().toUpperCase();
    if (!compId || WATER_COMP_IDS.has(compId)) continue;

    const atomName = line.slice(12, 16).trim();
    const rawElement = line.length >= 78 ? line.slice(76, 78).trim() : '';
    const element = rawElement || atomName;
    if (isHydrogenLikeElement(element)) continue;

    const chainId = line.slice(21, 22).trim().toUpperCase();
    if (!chainId) continue;

    const seqId = `${line.slice(22, 26).trim()}${line.slice(26, 27).trim()}`;
    const residueKey = `${chainId}|${seqId}|${compId}`;
    if (!firstResidueKeyByChain.has(chainId)) {
      firstResidueKeyByChain.set(chainId, residueKey);
    }
    if (firstResidueKeyByChain.get(chainId) !== residueKey) continue;

    const byName = atomPlddtsByChainAndName[chainId];
    if (!byName || typeof byName !== 'object') continue;
    const atomKey = normalizeAtomNameKey(atomName);
    if (!atomKey || !Object.prototype.hasOwnProperty.call(byName, atomKey)) continue;

    const bFactor = normalizePlddt(byName[atomKey]).toFixed(2).padStart(6);
    const padded = line.length >= 66 ? line : line.padEnd(66, ' ');
    lines[i] = `${padded.slice(0, 60)}${bFactor}${padded.slice(66)}`;
  }

  return lines.join('\n');
}

function applyLigandAtomPlddtsByChainAndNameToStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  atomPlddtsByChainAndName: Record<string, Record<string, number>>
): string {
  if (Object.keys(atomPlddtsByChainAndName).length === 0) return structureText;
  return structureFormat === 'pdb'
    ? applyLigandAtomPlddtsByChainAndNameToPdbStructure(structureText, atomPlddtsByChainAndName)
    : applyLigandAtomPlddtsByChainAndNameToCifStructure(structureText, atomPlddtsByChainAndName);
}

const WATER_COMP_IDS = new Set(['HOH', 'WAT', 'DOD', 'SOL']);
const POLYMER_COMP_IDS = new Set([
  'ACE',
  'NME',
  'NMA',
  'NH2',
  'ALA',
  'ARG',
  'ASN',
  'ASP',
  'CYS',
  'GLN',
  'GLU',
  'GLY',
  'HIS',
  'ILE',
  'LEU',
  'LYS',
  'MET',
  'PHE',
  'PRO',
  'SER',
  'THR',
  'TRP',
  'TYR',
  'VAL',
  'SEC',
  'PYL',
  'ASX',
  'GLX',
  'UNK',
  'A',
  'C',
  'G',
  'U',
  'I',
  'DA',
  'DC',
  'DG',
  'DT',
  'DI',
  'DU'
]);

function isLikelyLigandCompId(compId: string): boolean {
  const normalized = compId.trim().toUpperCase();
  if (!normalized) return false;
  if (WATER_COMP_IDS.has(normalized)) return false;
  return !POLYMER_COMP_IDS.has(normalized);
}

function isLikelyLigandAtomRow(groupPdb: string, compId: string): boolean {
  const normalizedGroup = groupPdb.trim().toUpperCase();
  if (!isLikelyLigandCompId(compId)) return false;
  if (!normalizedGroup) return true;
  // Some runtimes may emit ligand atoms as ATOM instead of HETATM.
  if (normalizedGroup === 'HETATM') return true;
  if (normalizedGroup === 'ATOM') return true;
  return false;
}

function isHydrogenLikeElement(raw: string): boolean {
  const value = raw.trim().toUpperCase();
  if (!value) return false;
  const head = value.replace(/[^A-Z]/g, '').slice(0, 1);
  return head === 'H' || head === 'D' || head === 'T';
}

function normalizeAtomNameKey(raw: string): string {
  return String(raw || '')
    .trim()
    .toUpperCase()
    .replace(/[^A-Z0-9]/g, '');
}

function toFiniteNumberArray(value: unknown): number[] {
  if (!Array.isArray(value)) return [];
  return value.filter((item): item is number => typeof item === 'number' && Number.isFinite(item));
}

function normalizeLigandAtomPlddts(values: number[]): number[] {
  const normalized = values
    .filter((value) => Number.isFinite(value))
    .map((value) => normalizePlddt(value))
    .filter((value) => Number.isFinite(value));
  if (!normalized.length) return [];
  return normalized;
}

function normalizeChainToken(value: string): string {
  return value.trim().toUpperCase();
}

function chainIdMatches(candidate: string, preferred: string): boolean {
  const normalizedCandidate = normalizeChainToken(candidate);
  const normalizedPreferred = normalizeChainToken(preferred);
  if (!normalizedCandidate || !normalizedPreferred) return false;
  if (normalizedCandidate === normalizedPreferred) return true;

  const compactCandidate = normalizedCandidate.replace(/[^A-Z0-9]/g, '');
  const compactPreferred = normalizedPreferred.replace(/[^A-Z0-9]/g, '');
  if (!compactCandidate || !compactPreferred) return false;
  if (compactCandidate === compactPreferred) return true;
  if (compactCandidate.startsWith(compactPreferred) || compactCandidate.endsWith(compactPreferred)) return true;
  if (compactPreferred.startsWith(compactCandidate) || compactPreferred.endsWith(compactCandidate)) return true;
  return false;
}

function normalizeLigandAtomPlddtsByChain(value: unknown): Record<string, number[]> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const byChain: Record<string, number[]> = {};
  for (const [rawChainId, rawValues] of Object.entries(value as Record<string, unknown>)) {
    const chainId = rawChainId.trim().toUpperCase();
    if (!chainId) continue;
    const parsed = normalizeLigandAtomPlddts(toFiniteNumberArray(rawValues));
    if (parsed.length === 0) continue;
    byChain[chainId] = parsed;
  }
  return byChain;
}

function normalizeLigandAtomPlddtsByChainAndName(value: unknown): Record<string, Record<string, number>> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const byChainAndName: Record<string, Record<string, number>> = {};
  for (const [rawChainId, rawNameMap] of Object.entries(value as Record<string, unknown>)) {
    const chainId = normalizeChainToken(rawChainId);
    if (!chainId || !rawNameMap || typeof rawNameMap !== 'object' || Array.isArray(rawNameMap)) continue;
    const nextMap: Record<string, number> = {};
    for (const [rawAtomName, rawPlddt] of Object.entries(rawNameMap as Record<string, unknown>)) {
      const atomKey = normalizeAtomNameKey(rawAtomName);
      if (!atomKey) continue;
      const value = Number(rawPlddt);
      if (!Number.isFinite(value)) continue;
      nextMap[atomKey] = normalizePlddt(value);
    }
    if (Object.keys(nextMap).length > 0) {
      byChainAndName[chainId] = nextMap;
    }
  }
  return byChainAndName;
}

function normalizeLigandAtomNameKeys(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  const keys: string[] = [];
  const used = new Set<string>();
  for (const item of value) {
    const key = normalizeAtomNameKey(String(item || ''));
    if (!key || used.has(key)) continue;
    used.add(key);
    keys.push(key);
  }
  return keys;
}

function normalizeLigandAtomNameKeysByChain(value: unknown): Record<string, string[]> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const byChain: Record<string, string[]> = {};
  for (const [rawChainId, rawKeys] of Object.entries(value as Record<string, unknown>)) {
    const chainId = normalizeChainToken(rawChainId);
    if (!chainId) continue;
    const keys = normalizeLigandAtomNameKeys(rawKeys);
    if (keys.length === 0) continue;
    byChain[chainId] = keys;
  }
  return byChain;
}

function collectLigandCoverageChainIds(confidence: Record<string, unknown>): Set<string> {
  const ids = new Set<string>();
  const add = (value: unknown) => {
    if (typeof value !== 'string') return;
    const normalized = normalizeChainToken(value);
    if (normalized) ids.add(normalized);
  };

  const ligandCoverage = confidence.ligand_atom_coverage;
  if (Array.isArray(ligandCoverage)) {
    for (const row of ligandCoverage) {
      if (!row || typeof row !== 'object') continue;
      add((row as Record<string, unknown>).chain);
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
        add(entry.chain);
      }
    }
  }
  return ids;
}

function selectLigandAtomPlddtsByChain(
  confidence: Record<string, unknown>,
  byChain: Record<string, number[]>
): Record<string, number[]> {
  const entries = Object.entries(byChain);
  if (entries.length <= 1) return byChain;

  const selectByHints = (hints: Set<string>): Record<string, number[]> | null => {
    if (hints.size === 0) return null;
    const filtered = Object.fromEntries(
      entries.filter(([chainId]) =>
        Array.from(hints).some((hint) => chainIdMatches(chainId, hint) || chainIdMatches(hint, chainId))
      )
    ) as Record<string, number[]>;
    return Object.keys(filtered).length > 0 ? filtered : null;
  };

  const coverageSelected = selectByHints(collectLigandCoverageChainIds(confidence));
  if (coverageSelected) return coverageSelected;

  const preferredHints = new Set<string>();
  for (const value of [
    confidence.requested_ligand_chain_id,
    confidence.ligand_chain_id,
    confidence.model_ligand_chain_id
  ]) {
    if (typeof value !== 'string') continue;
    const normalized = normalizeChainToken(value);
    if (normalized) preferredHints.add(normalized);
  }
  const preferredSelected = selectByHints(preferredHints);
  if (preferredSelected) return preferredSelected;

  return byChain;
}

function selectLigandAtomPlddtsByChainAndName(
  confidence: Record<string, unknown>,
  byChainAndName: Record<string, Record<string, number>>
): Record<string, Record<string, number>> {
  const entries = Object.entries(byChainAndName);
  if (entries.length <= 1) return byChainAndName;

  const selectByHints = (hints: Set<string>): Record<string, Record<string, number>> | null => {
    if (hints.size === 0) return null;
    const filtered = Object.fromEntries(
      entries.filter(([chainId]) =>
        Array.from(hints).some((hint) => chainIdMatches(chainId, hint) || chainIdMatches(hint, chainId))
      )
    ) as Record<string, Record<string, number>>;
    return Object.keys(filtered).length > 0 ? filtered : null;
  };

  const coverageSelected = selectByHints(collectLigandCoverageChainIds(confidence));
  if (coverageSelected) return coverageSelected;

  const preferredHints = new Set<string>();
  for (const value of [
    confidence.requested_ligand_chain_id,
    confidence.ligand_chain_id,
    confidence.model_ligand_chain_id
  ]) {
    if (typeof value !== 'string') continue;
    const normalized = normalizeChainToken(value);
    if (normalized) preferredHints.add(normalized);
  }
  const preferredSelected = selectByHints(preferredHints);
  if (preferredSelected) return preferredSelected;

  return byChainAndName;
}

function selectLigandAtomNameKeysByChain(
  confidence: Record<string, unknown>,
  nameKeysByChain: Record<string, string[]>
): Record<string, string[]> {
  const entries = Object.entries(nameKeysByChain);
  if (entries.length <= 1) return nameKeysByChain;

  const selectByHints = (hints: Set<string>): Record<string, string[]> | null => {
    if (hints.size === 0) return null;
    const filtered = Object.fromEntries(
      entries.filter(([chainId]) =>
        Array.from(hints).some((hint) => chainIdMatches(chainId, hint) || chainIdMatches(hint, chainId))
      )
    ) as Record<string, string[]>;
    return Object.keys(filtered).length > 0 ? filtered : null;
  };

  const coverageSelected = selectByHints(collectLigandCoverageChainIds(confidence));
  if (coverageSelected) return coverageSelected;

  const preferredHints = new Set<string>();
  for (const value of [
    confidence.requested_ligand_chain_id,
    confidence.ligand_chain_id,
    confidence.model_ligand_chain_id
  ]) {
    if (typeof value !== 'string') continue;
    const normalized = normalizeChainToken(value);
    if (normalized) preferredHints.add(normalized);
  }
  const preferredSelected = selectByHints(preferredHints);
  if (preferredSelected) return preferredSelected;

  return nameKeysByChain;
}

function buildLigandAtomPlddtsFromNameMap(nameMap: Record<string, number>, orderedNameKeys: string[]): number[] {
  const values: number[] = [];
  const used = new Set<string>();

  for (const rawKey of orderedNameKeys) {
    const key = normalizeAtomNameKey(rawKey);
    if (!key || used.has(key) || !Object.prototype.hasOwnProperty.call(nameMap, key)) continue;
    const value = Number(nameMap[key]);
    if (!Number.isFinite(value)) continue;
    used.add(key);
    values.push(normalizePlddt(value));
  }

  for (const [rawKey, rawValue] of Object.entries(nameMap)) {
    const key = normalizeAtomNameKey(rawKey);
    if (!key || used.has(key)) continue;
    const value = Number(rawValue);
    if (!Number.isFinite(value)) continue;
    used.add(key);
    values.push(normalizePlddt(value));
  }

  return values;
}

function buildLigandAtomPlddtsByChainFromNameMaps(
  byChainAndName: Record<string, Record<string, number>>,
  nameKeysByChain: Record<string, string[]>,
  fallbackNameKeys: string[]
): Record<string, number[]> {
  const byChain: Record<string, number[]> = {};
  const chainIds = Object.keys(byChainAndName);
  const fallbackKeys = chainIds.length === 1 ? fallbackNameKeys : [];

  for (const [chainId, nameMap] of Object.entries(byChainAndName)) {
    const chainNameKeys = nameKeysByChain[chainId] || fallbackKeys;
    const values = buildLigandAtomPlddtsFromNameMap(nameMap, chainNameKeys);
    if (values.length === 0) continue;
    byChain[chainId] = values;
  }

  return byChain;
}

function inferSingleLigandChainIdFromCif(structureText: string): string | null {
  if (!structureText) return null;
  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    if (!headers.some((header) => header.toLowerCase().startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const groupCol = findHeaderIndex(headers, ['_atom_site.group_pdb']);
    const chainCol = findHeaderIndex(headers, ['_atom_site.label_asym_id', '_atom_site.auth_asym_id']);
    const compCol = findHeaderIndex(headers, ['_atom_site.label_comp_id', '_atom_site.auth_comp_id']);
    const typeCol = findHeaderIndex(headers, ['_atom_site.type_symbol']);
    const atomIdCol = findHeaderIndex(headers, ['_atom_site.label_atom_id', '_atom_site.auth_atom_id']);
    if (chainCol < 0 || compCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    const chainSet = new Set<string>();
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
        const groupPdb = groupCol >= 0 ? stripCifTokenQuotes(tokens[groupCol]).trim().toUpperCase() : '';
        const element = typeCol >= 0 ? stripCifTokenQuotes(tokens[typeCol]) : '';
        const atomName = atomIdCol >= 0 ? stripCifTokenQuotes(tokens[atomIdCol]) : '';
        if (!compId || WATER_COMP_IDS.has(compId)) {
          rowIndex += 1;
          continue;
        }
        if (!isLikelyLigandAtomRow(groupPdb, compId)) {
          rowIndex += 1;
          continue;
        }
        if (isHydrogenLikeElement(element || atomName)) {
          rowIndex += 1;
          continue;
        }

        const chainIdRaw = stripCifTokenQuotes(tokens[chainCol]).trim();
        const chainId = chainIdRaw.toUpperCase();
        if (chainId) {
          chainSet.add(chainId);
        }
      }
      rowIndex += 1;
    }

    if (chainSet.size === 1) {
      return Array.from(chainSet)[0] || null;
    }

    i = rowIndex - 1;
  }

  return null;
}

function inferSingleLigandChainIdFromPdb(structureText: string): string | null {
  if (!structureText) return null;
  const lines = structureText.split(/\r?\n/);
  const chainSet = new Set<string>();
  for (const line of lines) {
    if (!line.startsWith('HETATM')) continue;
    const compId = line.slice(17, 20).trim().toUpperCase();
    if (!compId || WATER_COMP_IDS.has(compId)) continue;
    const atomName = line.slice(12, 16).trim();
    const rawElement = line.length >= 78 ? line.slice(76, 78).trim() : '';
    const element = rawElement || atomName;
    if (isHydrogenLikeElement(element)) continue;
    const chainId = line.slice(21, 22).trim().toUpperCase();
    if (!chainId) continue;
    chainSet.add(chainId);
  }
  if (chainSet.size === 1) {
    return Array.from(chainSet)[0] || null;
  }
  return null;
}

function inferSingleLigandChainIdFromStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb'
): string | null {
  return structureFormat === 'pdb'
    ? inferSingleLigandChainIdFromPdb(structureText)
    : inferSingleLigandChainIdFromCif(structureText);
}

function normalizeResiduePlddtsByChain(value: unknown): Record<string, number[]> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  const byChain: Record<string, number[]> = {};
  for (const [rawChainId, rawValues] of Object.entries(value as Record<string, unknown>)) {
    const chainId = rawChainId.trim().toUpperCase();
    if (!chainId) continue;
    const parsed = normalizeLigandAtomPlddts(toFiniteNumberArray(rawValues));
    if (parsed.length === 0) continue;
    byChain[chainId] = parsed;
  }
  return byChain;
}

function pickFirstLigandAtomPlddts(byChain: Record<string, number[]>): number[] {
  for (const values of Object.values(byChain)) {
    if (values.length > 0) return values;
  }
  return [];
}

function findHeaderIndex(headers: string[], names: string[]): number {
  const lowered = headers.map((header) => header.toLowerCase());
  for (const name of names) {
    const idx = lowered.indexOf(name.toLowerCase());
    if (idx >= 0) return idx;
  }
  return -1;
}

function extractLigandAtomPlddtsByChainFromCif(structureText: string): Record<string, number[]> {
  if (!structureText) return {};
  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    if (!headers.some((header) => header.toLowerCase().startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const groupCol = findHeaderIndex(headers, ['_atom_site.group_pdb']);
    const chainCol = findHeaderIndex(headers, ['_atom_site.label_asym_id', '_atom_site.auth_asym_id']);
    const seqCol = findHeaderIndex(headers, ['_atom_site.label_seq_id', '_atom_site.auth_seq_id']);
    const compCol = findHeaderIndex(headers, ['_atom_site.label_comp_id', '_atom_site.auth_comp_id']);
    const bFactorCol = findHeaderIndex(headers, ['_atom_site.b_iso_or_equiv']);
    const typeCol = findHeaderIndex(headers, ['_atom_site.type_symbol']);
    const atomIdCol = findHeaderIndex(headers, ['_atom_site.label_atom_id', '_atom_site.auth_atom_id']);

    if (compCol < 0 || bFactorCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    const residueChainByKey = new Map<string, string>();
    const ligandByResidue = new Map<string, number[]>();
    const firstResidueKeyByChain = new Map<string, string>();
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
        const groupPdb = groupCol >= 0 ? stripCifTokenQuotes(tokens[groupCol]).trim().toUpperCase() : '';
        const bFactor = Number(stripCifTokenQuotes(tokens[bFactorCol]));
        const element = typeCol >= 0 ? stripCifTokenQuotes(tokens[typeCol]) : '';
        const atomName = atomIdCol >= 0 ? stripCifTokenQuotes(tokens[atomIdCol]) : '';
        if (!compId || !Number.isFinite(bFactor)) {
          rowIndex += 1;
          continue;
        }
        if (!isLikelyLigandAtomRow(groupPdb, compId)) {
          rowIndex += 1;
          continue;
        }
        if (isHydrogenLikeElement(element || atomName)) {
          rowIndex += 1;
          continue;
        }

        const chainIdRaw = chainCol >= 0 ? stripCifTokenQuotes(tokens[chainCol]).trim() : '';
        const chainId = chainIdRaw.toUpperCase();
        if (!chainId) {
          rowIndex += 1;
          continue;
        }
        const seqId = seqCol >= 0 ? stripCifTokenQuotes(tokens[seqCol]).trim() : '';
        const residueKey = `${chainId}|${seqId}|${compId}`;
        if (!firstResidueKeyByChain.has(chainId)) {
          firstResidueKeyByChain.set(chainId, residueKey);
          residueChainByKey.set(residueKey, chainId);
          ligandByResidue.set(residueKey, []);
        }
        if (firstResidueKeyByChain.get(chainId) === residueKey) {
          const rowValues = ligandByResidue.get(residueKey);
          if (rowValues) {
            rowValues.push(bFactor);
          }
        }
      }
      rowIndex += 1;
    }

    const byChain: Record<string, number[]> = {};
    for (const [residueKey, values] of ligandByResidue.entries()) {
      const chainId = residueChainByKey.get(residueKey);
      if (!chainId) continue;
      const normalized = normalizeLigandAtomPlddts(values);
      if (normalized.length === 0) continue;
      byChain[chainId] = normalized;
    }
    if (Object.keys(byChain).length > 0) {
      return byChain;
    }
    i = rowIndex - 1;
  }
  return {};
}

function extractLigandAtomPlddtsByChainFromPdb(structureText: string): Record<string, number[]> {
  if (!structureText) return {};
  const lines = structureText.split(/\r?\n/);
  const firstResidueKeyByChain = new Map<string, string>();
  const ligandByResidue = new Map<string, number[]>();

  for (const line of lines) {
    if (!line.startsWith('HETATM')) continue;
    const compId = line.slice(17, 20).trim().toUpperCase();
    if (!compId || WATER_COMP_IDS.has(compId)) continue;

    const atomName = line.slice(12, 16).trim();
    const rawElement = line.length >= 78 ? line.slice(76, 78).trim() : '';
    const element = rawElement || atomName;
    if (isHydrogenLikeElement(element)) continue;

    const bFactorToken = line.slice(60, 66).trim();
    const bFactor = Number.parseFloat(bFactorToken);
    if (!Number.isFinite(bFactor)) continue;

    const chainId = line.slice(21, 22).trim().toUpperCase();
    if (!chainId) continue;
    const seqId = `${line.slice(22, 26).trim()}${line.slice(26, 27).trim()}`;
    const residueKey = `${chainId}|${seqId}|${compId}`;
    if (!firstResidueKeyByChain.has(chainId)) {
      firstResidueKeyByChain.set(chainId, residueKey);
      ligandByResidue.set(residueKey, []);
    }
    if (firstResidueKeyByChain.get(chainId) === residueKey) {
      const rowValues = ligandByResidue.get(residueKey);
      if (rowValues) {
        rowValues.push(bFactor);
      }
    }
  }

  const byChain: Record<string, number[]> = {};
  for (const [chainId, residueKey] of firstResidueKeyByChain.entries()) {
    const values = ligandByResidue.get(residueKey) || [];
    const normalized = normalizeLigandAtomPlddts(values);
    if (normalized.length === 0) continue;
    byChain[chainId] = normalized;
  }
  return byChain;
}

function extractLigandAtomPlddtsByChainFromStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb'
): Record<string, number[]> {
  if (!structureText) return {};
  return structureFormat === 'pdb'
    ? extractLigandAtomPlddtsByChainFromPdb(structureText)
    : extractLigandAtomPlddtsByChainFromCif(structureText);
}

function extractChainMeanPlddtFromCif(structureText: string): Record<string, number> {
  if (!structureText) return {};
  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    if (!headers.some((header) => header.toLowerCase().startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const chainCol = findHeaderIndex(headers, ['_atom_site.label_asym_id', '_atom_site.auth_asym_id']);
    const bFactorCol = findHeaderIndex(headers, ['_atom_site.b_iso_or_equiv']);
    const typeCol = findHeaderIndex(headers, ['_atom_site.type_symbol']);
    const atomIdCol = findHeaderIndex(headers, ['_atom_site.label_atom_id', '_atom_site.auth_atom_id']);
    if (chainCol < 0 || bFactorCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    const sums = new Map<string, { sum: number; count: number }>();
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const chainId = stripCifTokenQuotes(tokens[chainCol]).trim();
        const bFactor = Number(stripCifTokenQuotes(tokens[bFactorCol]));
        const element = typeCol >= 0 ? stripCifTokenQuotes(tokens[typeCol]) : '';
        const atomName = atomIdCol >= 0 ? stripCifTokenQuotes(tokens[atomIdCol]) : '';
        if (!chainId || !Number.isFinite(bFactor) || isHydrogenLikeElement(element || atomName)) {
          rowIndex += 1;
          continue;
        }
        const current = sums.get(chainId);
        if (current) {
          current.sum += bFactor;
          current.count += 1;
        } else {
          sums.set(chainId, { sum: bFactor, count: 1 });
        }
      }
      rowIndex += 1;
    }

    const output: Record<string, number> = {};
    for (const [chainId, item] of sums.entries()) {
      if (item.count > 0) {
        output[chainId] = normalizePlddt(item.sum / item.count);
      }
    }
    return output;
  }
  return {};
}

function extractChainMeanPlddtFromPdb(structureText: string): Record<string, number> {
  if (!structureText) return {};
  const lines = structureText.split(/\r?\n/);
  const sums = new Map<string, { sum: number; count: number }>();

  for (const line of lines) {
    if (!(line.startsWith('ATOM') || line.startsWith('HETATM'))) continue;
    const chainId = line.slice(21, 22).trim();
    if (!chainId) continue;
    const atomName = line.slice(12, 16).trim();
    const rawElement = line.length >= 78 ? line.slice(76, 78).trim() : '';
    const element = rawElement || atomName;
    if (isHydrogenLikeElement(element)) continue;

    const bFactor = Number.parseFloat(line.slice(60, 66).trim());
    if (!Number.isFinite(bFactor)) continue;

    const current = sums.get(chainId);
    if (current) {
      current.sum += bFactor;
      current.count += 1;
    } else {
      sums.set(chainId, { sum: bFactor, count: 1 });
    }
  }

  const output: Record<string, number> = {};
  for (const [chainId, item] of sums.entries()) {
    if (item.count > 0) {
      output[chainId] = normalizePlddt(item.sum / item.count);
    }
  }
  return output;
}

function extractChainMeanPlddtFromStructure(structureText: string, structureFormat: 'cif' | 'pdb'): Record<string, number> {
  return structureFormat === 'pdb'
    ? extractChainMeanPlddtFromPdb(structureText)
    : extractChainMeanPlddtFromCif(structureText);
}

function extractResiduePlddtsByChainFromCif(structureText: string): Record<string, number[]> {
  if (!structureText) return {};
  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    if (!headers.some((header) => header.toLowerCase().startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const groupCol = findHeaderIndex(headers, ['_atom_site.group_pdb']);
    const chainCol = findHeaderIndex(headers, ['_atom_site.label_asym_id', '_atom_site.auth_asym_id']);
    const seqCol = findHeaderIndex(headers, ['_atom_site.label_seq_id', '_atom_site.auth_seq_id']);
    const compCol = findHeaderIndex(headers, ['_atom_site.label_comp_id', '_atom_site.auth_comp_id']);
    const bFactorCol = findHeaderIndex(headers, ['_atom_site.b_iso_or_equiv']);
    const typeCol = findHeaderIndex(headers, ['_atom_site.type_symbol']);
    const atomIdCol = findHeaderIndex(headers, ['_atom_site.label_atom_id', '_atom_site.auth_atom_id']);

    if (chainCol < 0 || seqCol < 0 || compCol < 0 || bFactorCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    const residueStats = new Map<string, { chainId: string; sum: number; count: number }>();
    const residueOrderByChain = new Map<string, string[]>();
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
        const groupPdb = groupCol >= 0 ? stripCifTokenQuotes(tokens[groupCol]).trim().toUpperCase() : '';
        const chainId = stripCifTokenQuotes(tokens[chainCol]).trim().toUpperCase();
        const seqId = stripCifTokenQuotes(tokens[seqCol]).trim();
        const bFactor = Number(stripCifTokenQuotes(tokens[bFactorCol]));
        const element = typeCol >= 0 ? stripCifTokenQuotes(tokens[typeCol]) : '';
        const atomName = atomIdCol >= 0 ? stripCifTokenQuotes(tokens[atomIdCol]) : '';
        if (!chainId || !seqId || !compId || !Number.isFinite(bFactor)) {
          rowIndex += 1;
          continue;
        }
        if (seqId === '.' || seqId === '?') {
          rowIndex += 1;
          continue;
        }
        if (isHydrogenLikeElement(element || atomName)) {
          rowIndex += 1;
          continue;
        }
        if (groupPdb) {
          if (groupPdb !== 'ATOM' && !POLYMER_COMP_IDS.has(compId)) {
            rowIndex += 1;
            continue;
          }
        } else if (!POLYMER_COMP_IDS.has(compId)) {
          rowIndex += 1;
          continue;
        }

        const residueKey = `${chainId}|${seqId}|${compId}`;
        const current = residueStats.get(residueKey);
        if (current) {
          current.sum += bFactor;
          current.count += 1;
        } else {
          residueStats.set(residueKey, { chainId, sum: bFactor, count: 1 });
          const chainOrder = residueOrderByChain.get(chainId) || [];
          chainOrder.push(residueKey);
          residueOrderByChain.set(chainId, chainOrder);
        }
      }
      rowIndex += 1;
    }

    const byChain: Record<string, number[]> = {};
    for (const [chainId, residueOrder] of residueOrderByChain.entries()) {
      const values: number[] = [];
      for (const key of residueOrder) {
        const stat = residueStats.get(key);
        if (!stat || stat.count <= 0) continue;
        values.push(normalizePlddt(stat.sum / stat.count));
      }
      const normalized = normalizeLigandAtomPlddts(values);
      if (normalized.length > 0) {
        byChain[chainId] = normalized;
      }
    }
    if (Object.keys(byChain).length > 0) {
      return byChain;
    }
    i = rowIndex - 1;
  }
  return {};
}

function extractResiduePlddtsByChainFromPdb(structureText: string): Record<string, number[]> {
  if (!structureText) return {};
  const lines = structureText.split(/\r?\n/);
  const residueStats = new Map<string, { chainId: string; sum: number; count: number }>();
  const residueOrderByChain = new Map<string, string[]>();

  for (const line of lines) {
    if (!line.startsWith('ATOM')) continue;
    const chainId = line.slice(21, 22).trim().toUpperCase();
    const seqId = `${line.slice(22, 26).trim()}${line.slice(26, 27).trim()}`;
    const compId = line.slice(17, 20).trim().toUpperCase();
    const atomName = line.slice(12, 16).trim();
    const rawElement = line.length >= 78 ? line.slice(76, 78).trim() : '';
    const element = rawElement || atomName;
    const bFactor = Number.parseFloat(line.slice(60, 66).trim());
    if (!chainId || !seqId || !compId || !Number.isFinite(bFactor) || isHydrogenLikeElement(element)) continue;

    const residueKey = `${chainId}|${seqId}|${compId}`;
    const current = residueStats.get(residueKey);
    if (current) {
      current.sum += bFactor;
      current.count += 1;
    } else {
      residueStats.set(residueKey, { chainId, sum: bFactor, count: 1 });
      const chainOrder = residueOrderByChain.get(chainId) || [];
      chainOrder.push(residueKey);
      residueOrderByChain.set(chainId, chainOrder);
    }
  }

  const byChain: Record<string, number[]> = {};
  for (const [chainId, residueOrder] of residueOrderByChain.entries()) {
    const values: number[] = [];
    for (const key of residueOrder) {
      const stat = residueStats.get(key);
      if (!stat || stat.count <= 0) continue;
      values.push(normalizePlddt(stat.sum / stat.count));
    }
    const normalized = normalizeLigandAtomPlddts(values);
    if (normalized.length > 0) {
      byChain[chainId] = normalized;
    }
  }
  return byChain;
}

function extractResiduePlddtsByChainFromStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb'
): Record<string, number[]> {
  if (!structureText) return {};
  return structureFormat === 'pdb'
    ? extractResiduePlddtsByChainFromPdb(structureText)
    : extractResiduePlddtsByChainFromCif(structureText);
}

const DESIGN_RANK_RE = /(?:^|\/)rank_(\d+)(?:_|\.|$)/i;
const MAX_PEPTIDE_CANDIDATES_FOR_UI = 24;

function asRecordArray(value: unknown): Array<Record<string, unknown>> {
  if (!Array.isArray(value)) return [];
  return value.filter((item) => item && typeof item === 'object' && !Array.isArray(item)) as Array<Record<string, unknown>>;
}

function readFirstObjectArrayPath(payload: Record<string, unknown>, paths: string[]): Array<Record<string, unknown>> {
  for (const path of paths) {
    let current: unknown = payload;
    let matched = true;
    for (const token of path.split('.')) {
      if (!current || typeof current !== 'object' || Array.isArray(current)) {
        matched = false;
        break;
      }
      current = (current as Record<string, unknown>)[token];
    }
    if (!matched) continue;
    const rows = asRecordArray(current);
    if (rows.length > 0) return rows;
  }
  return [];
}

function readFiniteNumberLoose(value: unknown): number | null {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return null;
}

function firstNonEmptyTextByKeys(row: Record<string, unknown>, keys: string[]): string {
  for (const key of keys) {
    const text = readText(row[key]).trim();
    if (text) return text;
  }
  return '';
}

function resolveDesignStructurePathFromRow(
  row: Record<string, unknown>,
  structureByName: Set<string>,
  structurePathByBaseName: Map<string, string>
): string {
  const directCandidates = [
    'structure_path',
    'structure_file',
    'structurePath',
    'structureFile',
    'structure_name',
    'structureName',
    'path',
    'file',
    'model_path',
    'model_file'
  ];
  for (const key of directCandidates) {
    const raw = readText(row[key]).trim();
    if (!raw) continue;
    if (structureByName.has(raw)) return raw;
    const byBaseName = structurePathByBaseName.get(getBaseName(raw).toLowerCase());
    if (byBaseName) return byBaseName;
  }
  return '';
}

async function parsePeptideDesignCandidatesFromBundle(
  zip: JSZip,
  names: string[]
): Promise<Array<Record<string, unknown>>> {
  const summaryCandidates = names
    .filter((name) => {
      const base = getBaseName(name).toLowerCase();
      if (!base.endsWith('.json')) return false;
      if (base === 'results_summary.json') return true;
      return base.includes('design_results') || base.includes('best_sequences');
    })
    .sort((a, b) => a.length - b.length);
  if (summaryCandidates.length === 0) return [];

  let rows: Array<Record<string, unknown>> = [];
  for (const summaryPath of summaryCandidates) {
    const payload = await readZipJson(zip, summaryPath);
    if (!payload) continue;
    rows = readFirstObjectArrayPath(payload, [
      'top_results',
      'best_sequences',
      'peptide_design.best_sequences',
      'peptide_design.candidates',
      'results.best_sequences',
      'results.candidates',
      'peptide_results',
      'candidates'
    ]);
    if (rows.length > 0) break;
  }
  if (rows.length === 0) return [];

  const structureFiles = names
    .filter((name) => {
      const lower = name.toLowerCase();
      if (!/\.(cif|mmcif|pdb)$/i.test(lower)) return false;
      if (lower.includes('af3/output/')) return false;
      if (lower.includes('confidence_') || lower.includes('summary_confidence')) return false;
      // Prefer design structure folders, but still allow rank_* files elsewhere.
      return (
        lower.startsWith('structures/') ||
        lower.includes('/structures/') ||
        lower.includes('/designs/') ||
        lower.includes('/results/') ||
        DESIGN_RANK_RE.test(lower)
      );
    })
    .map((name) => {
      const match = DESIGN_RANK_RE.exec(name);
      const rank = match ? Number(match[1]) : Number.NaN;
      return {
        name,
        rank: Number.isFinite(rank) ? rank : Number.MAX_SAFE_INTEGER,
        hasRank: Number.isFinite(rank)
      };
    })
    .sort((a, b) => {
      if (a.hasRank !== b.hasRank) return a.hasRank ? -1 : 1;
      return a.rank - b.rank || a.name.localeCompare(b.name);
    });

  const byRank = new Map<number, string>();
  structureFiles.forEach((item, index) => {
    const resolvedRank = Number.isFinite(item.rank) && item.rank > 0 ? item.rank : index + 1;
    if (!byRank.has(resolvedRank)) byRank.set(resolvedRank, item.name);
  });
  const structureByName = new Set(structureFiles.map((item) => item.name));
  const structurePathByBaseName = new Map<string, string>();
  structureFiles.forEach((item) => {
    const base = getBaseName(item.name).toLowerCase();
    if (!structurePathByBaseName.has(base)) {
      structurePathByBaseName.set(base, item.name);
    }
  });

  const candidates: Array<Record<string, unknown>> = [];
  for (let index = 0; index < rows.length && candidates.length < MAX_PEPTIDE_CANDIDATES_FOR_UI; index += 1) {
    const row = rows[index];
    const rankRaw =
      readFiniteNumberLoose(row.rank) ??
      readFiniteNumberLoose(row.ranking) ??
      readFiniteNumberLoose(row.order) ??
      index + 1;
    const rank = Math.max(1, Math.floor(rankRaw));
    const structurePath =
      resolveDesignStructurePathFromRow(row, structureByName, structurePathByBaseName) ||
      byRank.get(rank) ||
      structureFiles[index]?.name ||
      '';
    const structureText = structurePath ? (await zip.file(structurePath)?.async('text')) || '' : '';
    const structureFormat: 'cif' | 'pdb' = getBaseName(structurePath).toLowerCase().endsWith('.pdb') ? 'pdb' : 'cif';
    const sequence = firstNonEmptyTextByKeys(row, [
      'peptide_sequence',
      'binder_sequence',
      'candidate_sequence',
      'designed_sequence',
      'sequence'
    ]);
    candidates.push({
      rank,
      generation: readFiniteNumberLoose(row.generation) ?? readFiniteNumberLoose(row.iteration),
      sequence,
      score:
        readFiniteNumberLoose(row.composite_score) ??
        readFiniteNumberLoose(row.score) ??
        readFiniteNumberLoose(row.fitness) ??
        readFiniteNumberLoose(row.objective),
      iptm: readFiniteNumberLoose(row.pair_iptm_target_binder) ?? readFiniteNumberLoose(row.pair_iptm) ?? readFiniteNumberLoose(row.iptm),
      pair_iptm: readFiniteNumberLoose(row.pair_iptm),
      pair_iptm_target_binder: readFiniteNumberLoose(row.pair_iptm_target_binder),
      pair_iptm_target_linker: readFiniteNumberLoose(row.pair_iptm_target_linker),
      pair_iptm_formula: readText(row.pair_iptm_formula),
      plddt:
        readFiniteNumberLoose(row.binder_avg_plddt) ??
        readFiniteNumberLoose(row.plddt) ??
        readFiniteNumberLoose(row.ligand_mean_plddt) ??
        readFiniteNumberLoose(row.mean_plddt),
      target_chain_id: readText(row.target_chain_id),
      binder_chain_id: readText(row.binder_chain_id),
      linker_chain_id: readText(row.linker_chain_id),
      structure_name: getBaseName(structurePath),
      structure_format: structureFormat,
      structure_text: structureText
    });
  }

  return candidates.filter((item) => {
    const sequence = readText(item.sequence).trim();
    const structureText = readText(item.structure_text).trim();
    return Boolean(sequence || structureText);
  });
}

function compactConfidenceForStorage(
  input: Record<string, unknown>,
  options?: { preservePeptideCandidateStructureText?: boolean }
): Record<string, unknown> {
  const next = { ...input };
  const preservePeptideCandidateStructureText = Boolean(options?.preservePeptideCandidateStructureText);
  const pae = next.pae;
  // Full PAE matrices dominate payload size but UI reads compact summary metrics (complex_pde/complex_pae).
  if (Array.isArray(pae) || (pae && typeof pae === 'object')) {
    delete next.pae;
  }

  const compactCandidateRows = (value: unknown): Array<Record<string, unknown>> => {
    if (!Array.isArray(value)) return [];
    return value
      .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object' && !Array.isArray(item)))
      .slice(0, MAX_PEPTIDE_CANDIDATES_FOR_UI)
      .map((row) => {
        const compact = { ...row };
        if (!preservePeptideCandidateStructureText) {
          delete compact.structure_text;
          delete compact.structureText;
          delete compact.cif_text;
          delete compact.pdb_text;
          delete compact.content;
        }
        return compact;
      });
  };

  const bestSequences = compactCandidateRows(next.best_sequences);
  if (bestSequences.length > 0) {
    next.best_sequences = bestSequences;
  }

  const currentBestSequences = compactCandidateRows(next.current_best_sequences);
  if (currentBestSequences.length > 0) {
    next.current_best_sequences = currentBestSequences;
  }

  const peptideDesignRaw = next.peptide_design;
  if (peptideDesignRaw && typeof peptideDesignRaw === 'object' && !Array.isArray(peptideDesignRaw)) {
    const peptideDesign = { ...(peptideDesignRaw as Record<string, unknown>) };
    const peptideBest = compactCandidateRows(peptideDesign.best_sequences);
    if (peptideBest.length > 0) {
      peptideDesign.best_sequences = peptideBest;
      if (!Number.isFinite(Number(peptideDesign.candidate_count))) {
        peptideDesign.candidate_count = peptideBest.length;
      }
    }
    const peptideCurrentBest = compactCandidateRows(peptideDesign.current_best_sequences);
    if (peptideCurrentBest.length > 0) {
      peptideDesign.current_best_sequences = peptideCurrentBest;
    }
    delete peptideDesign.candidates;
    next.peptide_design = peptideDesign;
  }

  return next;
}

export function compactResultConfidenceForStorage(
  input: Record<string, unknown>,
  options?: { preservePeptideCandidateStructureText?: boolean }
): Record<string, unknown> {
  return compactConfidenceForStorage(input, options);
}

interface ParseResultBundleOptions {
  preservePeptideCandidateStructureText?: boolean;
}

export async function parseResultBundle(blob: Blob, options?: ParseResultBundleOptions): Promise<ParsedResultBundle | null> {
  const preservePeptideCandidateStructureText = Boolean(options?.preservePeptideCandidateStructureText);
  const { default: JSZipLib } = await import('jszip');
  const zip = await JSZipLib.loadAsync(blob);
  const names = Object.keys(zip.files).filter((name) => !zip.files[name]?.dir);
  const isAf3 = names.some((name) => name.toLowerCase().includes('af3/output/'));
  const isProtenix = names.some((name) => name.toLowerCase().includes('protenix/output/'));
  const protenixSelection = isProtenix ? await chooseBestProtenixStructureAndConfidence(zip, names) : null;
  const boltzSelection = !isAf3 && !isProtenix ? await chooseBestBoltzStructureAndConfidence(zip, names) : null;

  const structureFile = isAf3
    ? chooseBestAf3StructureFile(names)
    : isProtenix
      ? protenixSelection?.structureFile || null
      : boltzSelection?.structureFile || null;
  if (!structureFile) return null;
  const structureFormat: 'cif' | 'pdb' = getBaseName(structureFile).toLowerCase().endsWith('.pdb') ? 'pdb' : 'cif';

  const structureTextRaw = await zip.file(structureFile)?.async('text');
  if (!structureTextRaw) return null;
  let structureText = structureTextRaw;

  let confidence: Record<string, unknown> = {};
  let affinity: Record<string, unknown> = {};

  if (isAf3) {
    const summaryCandidates = names.filter((name) => {
      const lower = name.toLowerCase();
      return lower.endsWith('.json') && lower.includes('af3/output/') && lower.includes('summary_confidences');
    });
    const confCandidates = names.filter((name) => {
      const lower = name.toLowerCase();
      return lower.endsWith('.json') && lower.includes('af3/output/') && getBaseName(lower) === 'confidences.json';
    });

    const summaryPath = choosePreferredPath(summaryCandidates);
    const confidencesPath = choosePreferredPath(confCandidates);
    const summary = await readZipJson(zip, summaryPath);
    const confidencesRaw = await readZipJson(zip, confidencesPath);

    if (summary) confidence = { ...summary };

    const af3Metrics: Record<string, unknown> = {};
    if (confidencesRaw) {
      const atomPlddtsRaw = confidencesRaw.atom_plddts;
      if (Array.isArray(atomPlddtsRaw)) {
        const atomPlddts = atomPlddtsRaw.filter((value): value is number => typeof value === 'number' && Number.isFinite(value));
        const avgPlddt = mean(atomPlddts);
        if (avgPlddt !== null) {
          af3Metrics.complex_plddt = avgPlddt;
        }
        if (atomPlddts.length > 0) {
          structureText = applyAtomPlddtToStructure(structureText, structureFormat, atomPlddts);
        }
      }
      if (structureFormat === 'cif') {
        // AF3 CIF can miss `_ma_qa_metric_local`; synthesize it from B-factor pLDDT values for Mol* AF coloring.
        structureText = ensureCifHasMaQaMetricLocal(structureText);
      }

      const paeValues = flattenNumberMatrix(confidencesRaw.pae);
      const avgPae = mean(paeValues);
      if (avgPae !== null) {
        af3Metrics.complex_pde = avgPae;
      }
    }

    const ptm = summary ? toFiniteNumber(summary.ptm) : null;
    if (ptm !== null) af3Metrics.ptm = ptm;

    const rankingScore = summary ? toFiniteNumber(summary.ranking_score) : null;
    if (rankingScore !== null) af3Metrics.ranking_score = rankingScore;

    const fractionDisordered = summary ? toFiniteNumber(summary.fraction_disordered) : null;
    if (fractionDisordered !== null) af3Metrics.fraction_disordered = fractionDisordered;

    const chainPairIptm = summary?.chain_pair_iptm;
    if (Array.isArray(chainPairIptm) && chainPairIptm.length > 0 && Array.isArray(chainPairIptm[0])) {
      af3Metrics.chain_pair_iptm = chainPairIptm;
    }

    const iptm = summary ? toFiniteNumber(summary.iptm) : null;
    if (iptm !== null) {
      af3Metrics.iptm = iptm;
    }

    confidence = {
      ...confidence,
      ...af3Metrics,
      backend: 'alphafold3'
    };
  } else {
    const confFile = isProtenix
      ? protenixSelection?.confidenceFile || null
      : boltzSelection?.confidenceFile || null;
    const parsedConfidence = await readZipJson(zip, confFile);
    if (parsedConfidence) {
      confidence = compactConfidenceForStorage({
        ...parsedConfidence,
        backend: isProtenix ? 'protenix' : 'boltz'
      }, { preservePeptideCandidateStructureText });
    } else {
      confidence = {
        backend: isProtenix ? 'protenix' : 'boltz'
      };
    }
    if (isProtenix) {
      const raw = confidence as Record<string, unknown>;
      const paeScalar =
        toFiniteNumber(raw.complex_pde) ??
        toFiniteNumber(raw.complex_pae) ??
        toFiniteNumber(raw.gpde) ??
        toFiniteNumber(raw.pae);
      if (paeScalar !== null) {
        confidence = {
          ...confidence,
          complex_pde: paeScalar,
          complex_pae: paeScalar,
          pae: paeScalar
        };
      }
      const chainPairGpde = raw.chain_pair_gpde;
      if (Array.isArray(chainPairGpde) && !Array.isArray(raw.chain_pair_pae)) {
        confidence = {
          ...confidence,
          chain_pair_pae: chainPairGpde
        };
      }
    }
  }

  const confidenceRecord = confidence as Record<string, unknown>;
  const existingLigandAtomPlddtsFlat = normalizeLigandAtomPlddts(toFiniteNumberArray(confidenceRecord.ligand_atom_plddts));
  const existingLigandAtomPlddtsByChainRaw = normalizeLigandAtomPlddtsByChain(confidenceRecord.ligand_atom_plddts_by_chain);
  const existingLigandAtomPlddtsByChainAndNameRaw = normalizeLigandAtomPlddtsByChainAndName(
    confidenceRecord.ligand_atom_plddts_by_chain_and_name
  );
  const existingLigandAtomNameKeys = normalizeLigandAtomNameKeys(confidenceRecord.ligand_atom_name_keys);
  const existingLigandAtomNameKeysByChainRaw = normalizeLigandAtomNameKeysByChain(confidenceRecord.ligand_atom_name_keys_by_chain);

  const existingLigandAtomPlddtsByChainAndName = selectLigandAtomPlddtsByChainAndName(
    confidenceRecord,
    existingLigandAtomPlddtsByChainAndNameRaw
  );
  const existingLigandAtomNameKeysByChain = selectLigandAtomNameKeysByChain(
    confidenceRecord,
    existingLigandAtomNameKeysByChainRaw
  );
  const alignedLigandAtomPlddtsByChain = buildLigandAtomPlddtsByChainFromNameMaps(
    existingLigandAtomPlddtsByChainAndName,
    existingLigandAtomNameKeysByChain,
    existingLigandAtomNameKeys
  );
  const existingLigandAtomPlddtsByChain = selectLigandAtomPlddtsByChain(confidenceRecord, {
    ...existingLigandAtomPlddtsByChainRaw,
    ...alignedLigandAtomPlddtsByChain
  });
  const existingLigandAtomPlddtsFromChainMap = pickFirstLigandAtomPlddts(existingLigandAtomPlddtsByChain);
  const existingLigandAtomPlddts =
    existingLigandAtomPlddtsFromChainMap.length > 0 ? existingLigandAtomPlddtsFromChainMap : existingLigandAtomPlddtsFlat;

  let ligandAtomPlddtsByChainForRender = existingLigandAtomPlddtsByChain;
  if (Object.keys(ligandAtomPlddtsByChainForRender).length === 0 && existingLigandAtomPlddts.length > 0) {
    const singleLigandChainId = inferSingleLigandChainIdFromStructure(structureText, structureFormat);
    if (singleLigandChainId) {
      ligandAtomPlddtsByChainForRender = {
        [singleLigandChainId]: existingLigandAtomPlddts
      };
    }
  }
  if (Object.keys(existingLigandAtomPlddtsByChainAndName).length > 0) {
    // Apply atom-level confidence to structure only when an atom-name map exists.
    // Writing by raw atom order can drift from aligned 2D confidence ordering.
    structureText = applyLigandAtomPlddtsByChainAndNameToStructure(
      structureText,
      structureFormat,
      existingLigandAtomPlddtsByChainAndName
    );
  }
  if (isProtenix && structureFormat === 'cif') {
    // Keep Protenix rendering aligned with AF3/Boltz2 AlphaFold palette requirements.
    structureText = ensureStructureConfidenceColoringData(structureText, structureFormat, 'protenix');
  }
  const extractedLigandAtomPlddtsByChain =
    Object.keys(ligandAtomPlddtsByChainForRender).length > 0
      ? ligandAtomPlddtsByChainForRender
      : extractLigandAtomPlddtsByChainFromStructure(structureText, structureFormat);
  const extractedLigandAtomPlddts =
    existingLigandAtomPlddts.length > 0
      ? existingLigandAtomPlddts
      : pickFirstLigandAtomPlddts(extractedLigandAtomPlddtsByChain);
  const residuePlddtByChainCandidates: unknown[] = [
    (confidence as Record<string, unknown>).residue_plddt_by_chain,
    (confidence as Record<string, unknown>).chain_residue_plddt,
    (confidence as Record<string, unknown>).chain_plddt,
    (confidence as Record<string, unknown>).chain_plddts
  ];
  let existingResiduePlddtByChain: Record<string, number[]> = {};
  for (const candidate of residuePlddtByChainCandidates) {
    const parsed = normalizeResiduePlddtsByChain(candidate);
    if (Object.keys(parsed).length > 0) {
      existingResiduePlddtByChain = parsed;
      break;
    }
  }
  const extractedResiduePlddtByChain =
    Object.keys(existingResiduePlddtByChain).length > 0
      ? existingResiduePlddtByChain
      : extractResiduePlddtsByChainFromStructure(structureText, structureFormat);
  const chainMeanPlddt = extractChainMeanPlddtFromStructure(structureText, structureFormat);
  if (Object.keys(extractedLigandAtomPlddtsByChain).length > 0) {
    confidence = {
      ...confidence,
      ligand_atom_plddts_by_chain: extractedLigandAtomPlddtsByChain
    };
  }
  if (extractedLigandAtomPlddts.length > 0) {
    confidence = {
      ...confidence,
      ligand_atom_plddts: extractedLigandAtomPlddts
    };
  }
  if (Object.keys(chainMeanPlddt).length > 0) {
    confidence = {
      ...confidence,
      chain_mean_plddt: chainMeanPlddt
    };
  }
  if (Object.keys(extractedResiduePlddtByChain).length > 0) {
    confidence = {
      ...confidence,
      residue_plddt_by_chain: extractedResiduePlddtByChain
    };
  }
  const peptideDesignCandidates = await parsePeptideDesignCandidatesFromBundle(zip, names);
  if (peptideDesignCandidates.length > 0) {
    const confidenceRecordLocal = confidence as Record<string, unknown>;
    const existingPeptideDesign =
      confidenceRecordLocal.peptide_design &&
      typeof confidenceRecordLocal.peptide_design === 'object' &&
      !Array.isArray(confidenceRecordLocal.peptide_design)
        ? (confidenceRecordLocal.peptide_design as Record<string, unknown>)
        : {};
    confidence = {
      ...confidence,
      peptide_design: {
        ...existingPeptideDesign,
        best_sequences: peptideDesignCandidates,
        candidate_count: peptideDesignCandidates.length
      },
      best_sequences: peptideDesignCandidates
    };
  }
  confidence = compactConfidenceForStorage(confidence, { preservePeptideCandidateStructureText });

  const affinityFile = names
    .filter((name) => name.toLowerCase().endsWith('.json') && name.toLowerCase().includes('affinity'))
    .sort((a, b) => a.length - b.length)[0];
  const parsedAffinity = await readZipJson(zip, affinityFile || null);
  if (parsedAffinity) affinity = parsedAffinity;

  const structureName = getBaseName(structureFile);
  return {
    structureText,
    structureFormat,
    structureName,
    confidence,
    affinity
  };
}

export async function downloadResultFile(taskId: string): Promise<void> {
  const blob = await downloadResultBlob(taskId, { mode: 'full' });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = `${taskId}_results.zip`;
  anchor.click();
  URL.revokeObjectURL(href);
}

export { ensureStructureConfidenceColoringData };
