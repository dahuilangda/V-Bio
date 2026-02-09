import JSZip from 'jszip';
import type { InputComponent, ParsedResultBundle, PredictionSubmitInput, TaskStatusResponse } from '../types/models';
import { apiUrl, ENV } from '../utils/env';
import { normalizeComponentSequence } from '../utils/projectInputs';
import { buildPredictionYaml, buildPredictionYamlFromComponents } from '../utils/yaml';

const API_HEADERS: Record<string, string> = {};
if (ENV.apiToken) {
  API_HEADERS['X-API-Token'] = ENV.apiToken;
}

const BACKEND_TIMEOUT_MS = 20000;

async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = BACKEND_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, {
      ...init,
      signal: controller.signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      throw new Error(`Backend request timeout after ${timeoutMs}ms for ${url}`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

function unique<T>(items: T[]): T[] {
  return Array.from(new Set(items));
}

function buildApiUrlCandidates(path: string): string[] {
  const primary = apiUrl(path);
  const candidates = [primary];
  if (typeof window !== 'undefined') {
    const normalizedPath = path.startsWith('/') ? path : `/${path}`;
    candidates.push(`${window.location.origin}${normalizedPath}`);
    // Remote-access fallback: bypass Vite proxy and call backend host directly.
    candidates.push(`${window.location.protocol}//${window.location.hostname}:5000${normalizedPath}`);
  }
  return unique(candidates.filter(Boolean));
}

async function requestWithFallback(path: string, init: RequestInit): Promise<Response> {
  const candidates = buildApiUrlCandidates(path);
  let lastError: Error | null = null;

  for (let i = 0; i < candidates.length; i += 1) {
    const url = candidates[i];
    try {
      const response = await fetchWithTimeout(url, init);

      // If this host doesn't serve backend routes, try the next candidate.
      if ((response.status === 404 || response.status === 405) && i < candidates.length - 1) {
        continue;
      }

      return response;
    } catch (error) {
      lastError = error instanceof Error ? error : new Error(String(error));
    }
  }

  if (lastError) {
    throw new Error(`All backend endpoints failed for ${path}. Tried: ${candidates.join(', ')}. Last error: ${lastError.message}`);
  }
  throw new Error(`All backend endpoints failed for ${path}. Tried: ${candidates.join(', ')}`);
}

export async function submitPrediction(input: PredictionSubmitInput): Promise<string> {
  const backend = String(input.backend || 'boltz').trim().toLowerCase();
  const constraintsForBackend = (input.constraints || []).filter((constraint) =>
    backend === 'alphafold3' ? constraint.type === 'bond' : true
  );
  const normalizedComponents = (input.components || [])
    .map((comp) => ({
      ...comp,
      sequence: normalizeComponentSequence(comp.type, comp.sequence)
    }))
    .filter((comp) => Boolean(comp.sequence));

  const fallbackComponents: InputComponent[] = [];
  const proteinSequence = normalizeComponentSequence('protein', input.proteinSequence || '');
  const ligandSmiles = normalizeComponentSequence('ligand', input.ligandSmiles || '');
  if (proteinSequence) {
    fallbackComponents.push({
      id: 'A',
      type: 'protein',
      numCopies: 1,
      sequence: proteinSequence,
      useMsa: Boolean(input.useMsa),
      cyclic: false
    });
  }
  if (ligandSmiles) {
    fallbackComponents.push({
      id: 'B',
      type: 'ligand',
      numCopies: 1,
      sequence: ligandSmiles,
      inputMethod: 'smiles'
    });
  }

  const componentsForYaml = normalizedComponents.length > 0 ? normalizedComponents : fallbackComponents;
  if (!componentsForYaml.length) {
    throw new Error('Please provide at least one non-empty component sequence before submitting.');
  }
  const useMsaServer = componentsForYaml.some((comp) => comp.type === 'protein' && comp.useMsa !== false);
  const hasConstraints = constraintsForBackend.length > 0;
  const hasAffinityProperty = Boolean(input.properties?.affinity && (input.properties?.ligand || input.properties?.binder));
  const useSimpleYaml =
    !hasConstraints &&
    !hasAffinityProperty &&
    componentsForYaml.length === 2 &&
    componentsForYaml[0].type === 'protein' &&
    componentsForYaml[1].type === 'ligand' &&
    componentsForYaml[0].numCopies === 1 &&
    componentsForYaml[1].numCopies === 1;

  const yaml = useSimpleYaml
    ? buildPredictionYaml(componentsForYaml[0].sequence, componentsForYaml[1].sequence)
    : buildPredictionYamlFromComponents(componentsForYaml, {
        constraints: constraintsForBackend,
        properties: input.properties
      });

  const form = new FormData();
  const yamlFile = new File([yaml], 'config.yaml', { type: 'application/x-yaml' });
  form.append('yaml_file', yamlFile);
  form.append('backend', backend || 'boltz');
  form.append('use_msa_server', String(useMsaServer).toLowerCase());
  if (typeof input.seed === 'number' && Number.isFinite(input.seed)) {
    form.append('seed', String(Math.max(0, Math.floor(input.seed))));
  }
  if (Array.isArray(input.templateUploads) && input.templateUploads.length > 0) {
    const templateMeta = input.templateUploads.map((item) => ({
      file_name: item.fileName,
      format: item.format,
      template_chain_id: item.templateChainId,
      target_chain_ids: item.targetChainIds
    }));
    form.append('template_meta', JSON.stringify(templateMeta));
    for (const item of input.templateUploads) {
      form.append(
        'template_files',
        new File([item.content], item.fileName, {
          type: 'application/octet-stream'
        })
      );
    }
  }
  // Keep the same queue behavior as legacy frontend (`frontend/prediction_client.py`)
  form.append('priority', 'high');

  let res: Response;
  try {
    res = await requestWithFallback('/predict', {
      method: 'POST',
      headers: {
        ...API_HEADERS,
        Accept: 'application/json'
      },
      body: form
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : 'Unknown error';
    throw new Error(
      `Failed to reach backend /predict endpoint. Check VITE_API_BASE_URL or Vite proxy setup. Original error: ${message}`
    );
  }

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to submit prediction (${res.status}): ${text}`);
  }

  const data = (await res.json()) as { task_id?: string };
  if (!data.task_id) {
    throw new Error('Backend response did not include task_id.');
  }
  return data.task_id;
}

export async function getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
  const res = await requestWithFallback(`/status/${taskId}`, {
    headers: {
      Accept: 'application/json'
    }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to fetch task status (${res.status}): ${text}`);
  }
  return (await res.json()) as TaskStatusResponse;
}

export async function downloadResultBlob(taskId: string): Promise<Blob> {
  const res = await requestWithFallback(`/results/${taskId}`, {});
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to download result (${res.status}): ${text}`);
  }
  return await res.blob();
}

function getBaseName(path: string): string {
  const parts = path.split('/');
  return parts[parts.length - 1] || path;
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

function extractRecordIdFromStructureName(name: string): string | null {
  const base = getBaseName(name);
  if (!base.includes('_model_')) return null;
  return base.split('_model_')[0] || null;
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
    .filter((name) => /\.(cif|pdb)$/i.test(name) && !name.toLowerCase().includes('af3/output/'))
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

function chooseBestBoltzConfidenceFile(names: string[], selectedStructure: string | null): string | null {
  const candidates = names.filter((name) => {
    const lower = name.toLowerCase();
    return lower.endsWith('.json') && lower.includes('confidence') && !lower.includes('af3/output/');
  });
  if (!candidates.length) return null;

  const selectedRecordId = selectedStructure ? extractRecordIdFromStructureName(selectedStructure) : null;
  if (selectedRecordId) {
    const preferredSuffix = `confidence_${selectedRecordId}_model_0.json`;
    const preferred = candidates.find((name) => name.toLowerCase().endsWith(preferredSuffix.toLowerCase()));
    if (preferred) return preferred;
  }

  const scored = candidates
    .map((name) => {
      const lower = name.toLowerCase();
      let score = 100;
      if (lower.includes('confidence_')) score -= 5;
      if (lower.includes('model_0') || lower.includes('ranked_0')) score -= 20;
      else if (lower.includes('model_') || lower.includes('ranked_')) score -= 5;
      return { name, score };
    })
    .sort((a, b) => a.score - b.score || a.name.length - b.name.length);

  return scored[0]?.name ?? null;
}

function chooseBestAf3StructureFile(names: string[]): string | null {
  const candidates = names
    .filter((name) => /\.(cif|pdb)$/i.test(name) && name.toLowerCase().includes('af3/output/'))
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

function parseMaQaLocalMetricIdFromCif(structureText: string): string | null {
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

    const idCol = headers.findIndex((header) => header === '_ma_qa_metric.id');
    const modeCol = headers.findIndex((header) => header === '_ma_qa_metric.mode');
    if (idCol < 0 || modeCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    let rowIndex = headerIndex;
    let fallbackMetricId: string | null = null;
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
        const metricId = stripCifTokenQuotes(tokens[idCol]).trim();
        const mode = stripCifTokenQuotes(tokens[modeCol]).trim().toLowerCase();
        if (!fallbackMetricId && metricId) {
          fallbackMetricId = metricId;
        }
        if (metricId && mode === 'local') {
          return metricId;
        }
      }
      rowIndex += 1;
    }
    return fallbackMetricId;
  }
  return null;
}

type CifResidueConfidenceRow = {
  chainId: string;
  seqId: number;
  compId: string;
  value: number;
};

function buildResidueConfidenceRowsFromCif(structureText: string): CifResidueConfidenceRow[] {
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

    if (!headers.some((header) => header.startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const chainCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.label_asym_id');
    const seqCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.label_seq_id');
    const compCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.label_comp_id');
    const bFactorCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.b_iso_or_equiv');
    if (chainCol < 0 || seqCol < 0 || compCol < 0 || bFactorCol < 0) {
      return [];
    }

    const residueMap = new Map<string, { chainId: string; seqId: number; compId: string; sum: number; count: number }>();
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
        const seqToken = stripCifTokenQuotes(tokens[seqCol]).trim();
        const compId = stripCifTokenQuotes(tokens[compCol]).trim();
        const bFactor = Number(tokens[bFactorCol]);
        const seqId = Number.parseInt(seqToken, 10);
        if (chainId && compId && Number.isFinite(seqId) && seqId > 0 && Number.isFinite(bFactor)) {
          const key = `${chainId}|${seqId}|${compId}`;
          const current = residueMap.get(key);
          if (current) {
            current.sum += bFactor;
            current.count += 1;
          } else {
            residueMap.set(key, { chainId, seqId, compId, sum: bFactor, count: 1 });
          }
        }
      }
      rowIndex += 1;
    }

    return Array.from(residueMap.values()).map((item) => ({
      chainId: item.chainId,
      seqId: item.seqId,
      compId: item.compId,
      value: item.sum / item.count
    }));
  }

  return [];
}

function ensureCifHasMaQaMetricLocal(structureText: string): string {
  if (!structureText) return structureText;
  if (structureText.includes('_ma_qa_metric_local.metric_value')) return structureText;

  const residueRows = buildResidueConfidenceRowsFromCif(structureText);
  if (!residueRows.length) return structureText;

  const hasMetricCategory = structureText.includes('_ma_qa_metric.id');
  const localMetricId = parseMaQaLocalMetricIdFromCif(structureText) || '1';

  const appendBlocks: string[] = [];
  if (!hasMetricCategory) {
    appendBlocks.push(
      [
        'loop_',
        '_ma_qa_metric.id',
        '_ma_qa_metric.name',
        '_ma_qa_metric.description',
        '_ma_qa_metric.type',
        '_ma_qa_metric.mode',
        '_ma_qa_metric.type_other_details',
        '_ma_qa_metric.software_group_id',
        `${localMetricId} pLDDT 'Predicted lddt' pLDDT local . .`,
        '#'
      ].join('\n')
    );
  }

  const localRows = residueRows.map(
    (item, index) =>
      `${index + 1} 1 ${item.chainId} ${item.seqId} ${item.compId} ${localMetricId} ${item.value.toFixed(3)}`
  );
  appendBlocks.push(
    [
      'loop_',
      '_ma_qa_metric_local.ordinal_id',
      '_ma_qa_metric_local.model_id',
      '_ma_qa_metric_local.label_asym_id',
      '_ma_qa_metric_local.label_seq_id',
      '_ma_qa_metric_local.label_comp_id',
      '_ma_qa_metric_local.metric_id',
      '_ma_qa_metric_local.metric_value',
      ...localRows,
      '#'
    ].join('\n')
  );

  const suffix = structureText.endsWith('\n') ? '' : '\n';
  return `${structureText}${suffix}#\n${appendBlocks.join('\n#\n')}\n`;
}

export async function parseResultBundle(blob: Blob): Promise<ParsedResultBundle | null> {
  const zip = await JSZip.loadAsync(blob);
  const names = Object.keys(zip.files).filter((name) => !zip.files[name]?.dir);
  const isAf3 = names.some((name) => name.toLowerCase().includes('af3/output/'));

  const structureFile = isAf3 ? chooseBestAf3StructureFile(names) : chooseBestBoltzStructureFile(names);
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
      if (Array.isArray(confidencesRaw.pae) && confidencesRaw.pae.length > 0) {
        af3Metrics.pae = confidencesRaw.pae;
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

    let iptm = summary ? toFiniteNumber(summary.iptm) : null;
    if (iptm === null && Array.isArray(chainPairIptm) && Array.isArray(chainPairIptm[0])) {
      iptm = toFiniteNumber(chainPairIptm[0][0]);
    }
    if (iptm !== null) {
      af3Metrics.iptm = iptm;
    }

    confidence = {
      ...confidence,
      ...af3Metrics,
      backend: 'alphafold3'
    };
  } else {
    const confFile = chooseBestBoltzConfidenceFile(names, structureFile);
    const parsedConfidence = await readZipJson(zip, confFile);
    if (parsedConfidence) {
      confidence = {
        ...parsedConfidence,
        backend: 'boltz'
      };
    }
  }

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
  const blob = await downloadResultBlob(taskId);
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = `${taskId}_results.zip`;
  anchor.click();
  URL.revokeObjectURL(href);
}
