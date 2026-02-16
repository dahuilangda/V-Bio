import type JSZip from 'jszip';
import type {
  AffinityPreviewPayload,
  AffinitySubmitInput,
  InputComponent,
  ParsedResultBundle,
  PredictionSubmitInput,
  TaskStatusResponse
} from '../types/models';
import { apiUrl, ENV } from '../utils/env';
import { normalizeComponentSequence } from '../utils/projectInputs';
import { buildPredictionYaml, buildPredictionYamlFromComponents } from '../utils/yaml';

const API_HEADERS: Record<string, string> = {};
if (ENV.apiToken) {
  API_HEADERS['X-API-Token'] = ENV.apiToken;
}

const BACKEND_TIMEOUT_MS = 20000;
const BOLTZ2SCORE_AFFINITY_PROFILE = Object.freeze({
  structureRefine: false,
  // Empirically more stable for affinity confidence than 7 in input-structure mode.
  recyclingSteps: 20,
  samplingSteps: 1,
  diffusionSamples: 1,
  maxParallelSamples: 1
});

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

async function requestBackend(path: string, init: RequestInit, timeoutMs = BACKEND_TIMEOUT_MS): Promise<Response> {
  const url = apiUrl(path);
  try {
    return await fetchWithTimeout(url, init, timeoutMs);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(`Backend request failed for ${path} (${url}): ${message}`);
  }
}

export async function submitPrediction(input: PredictionSubmitInput): Promise<string> {
  const backend = String(input.backend || 'boltz').trim().toLowerCase();
  const constraintsForBackend = (input.constraints || []).filter((constraint) =>
    backend === 'alphafold3' || backend === 'protenix' ? constraint.type === 'bond' : true
  );
  const normalizedComponents = (input.components || [])
    .map((comp) => ({
      ...comp,
      sequence: normalizeComponentSequence(comp.type, comp.sequence)
    }))
    .filter((comp) => Boolean(comp.sequence));

  const compatComponents: InputComponent[] = [];
  const proteinSequence = normalizeComponentSequence('protein', input.proteinSequence || '');
  const ligandSmiles = normalizeComponentSequence('ligand', input.ligandSmiles || '');
  if (proteinSequence) {
    compatComponents.push({
      id: 'A',
      type: 'protein',
      numCopies: 1,
      sequence: proteinSequence,
      useMsa: Boolean(input.useMsa),
      cyclic: false
    });
  }
  if (ligandSmiles) {
    compatComponents.push({
      id: 'B',
      type: 'ligand',
      numCopies: 1,
      sequence: ligandSmiles,
      inputMethod: 'smiles'
    });
  }

  const componentsForYaml = normalizedComponents.length > 0 ? normalizedComponents : compatComponents;
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
    res = await requestBackend('/predict', {
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

export async function previewAffinityComplex(input: {
  targetFile: File;
  ligandFile?: File | null;
}): Promise<AffinityPreviewPayload> {
  const form = new FormData();
  form.append('protein_file', input.targetFile);
  if (input.ligandFile) {
    form.append('ligand_file', input.ligandFile);
  }

  const res = await requestBackend('/api/affinity/preview', {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    },
    body: form
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to generate affinity preview (${res.status}): ${text}`);
  }

  const data = (await res.json()) as {
    structure_text?: string;
    structure_format?: string;
    structure_name?: string;
    target_structure_text?: string;
    target_structure_format?: string;
    ligand_structure_text?: string;
    ligand_structure_format?: string;
    ligand_smiles?: string;
    target_chain_ids?: unknown;
    ligand_chain_id?: string;
    has_ligand?: boolean;
    ligand_is_small_molecule?: boolean;
    supports_activity?: boolean;
    protein_filename?: string;
    ligand_filename?: string;
  };

  const structureText = typeof data.structure_text === 'string' ? data.structure_text : '';
  if (!structureText.trim()) {
    throw new Error('Affinity preview response did not include structure_text.');
  }

  const structureFormat = data.structure_format === 'pdb' ? 'pdb' : 'cif';
  const targetChainIds = Array.isArray(data.target_chain_ids)
    ? data.target_chain_ids
        .filter((item): item is string => typeof item === 'string')
        .map((item) => item.trim())
        .filter(Boolean)
    : [];

  return {
    structureText,
    structureFormat,
    structureName:
      typeof data.structure_name === 'string' && data.structure_name.trim() ? data.structure_name : input.targetFile.name,
    targetStructureText:
      typeof data.target_structure_text === 'string' && data.target_structure_text.trim()
        ? data.target_structure_text
        : structureText,
    targetStructureFormat: data.target_structure_format === 'pdb' ? 'pdb' : structureFormat,
    ligandStructureText:
      typeof data.ligand_structure_text === 'string' && data.ligand_structure_text.trim() ? data.ligand_structure_text : '',
    ligandStructureFormat: data.ligand_structure_format === 'pdb' ? 'pdb' : 'cif',
    ligandSmiles: typeof data.ligand_smiles === 'string' ? data.ligand_smiles.trim() : '',
    targetChainIds,
    hasLigand: Boolean(data.has_ligand),
    ligandIsSmallMolecule: Boolean(data.ligand_is_small_molecule),
    supportsActivity: Boolean(data.supports_activity),
    ligandChainId:
      typeof data.ligand_chain_id === 'string' ? data.ligand_chain_id.trim() : '',
    proteinFileName: typeof data.protein_filename === 'string' ? data.protein_filename.trim() : input.targetFile.name,
    ligandFileName: typeof data.ligand_filename === 'string' ? data.ligand_filename.trim() : input.ligandFile?.name || ''
  };
}

export async function submitAffinityScoring(input: AffinitySubmitInput): Promise<string> {
  const structureText = String(input.inputStructureText || '').trim();
  if (!structureText) {
    throw new Error('Affinity scoring requires a prepared input structure.');
  }
  const backend = String(input.backend || 'boltz').trim().toLowerCase();
  const useProtenix = backend === 'protenix';
  const normalizedSeed =
    typeof input.seed === 'number' && Number.isFinite(input.seed) ? Math.max(0, Math.floor(input.seed)) : null;

  const form = new FormData();
  form.append(
    'input_file',
    new File([structureText], input.inputStructureName || 'affinity_input.cif', { type: 'chemical/x-cif' })
  );
  const targetChainIds = Array.isArray(input.targetChainIds)
    ? input.targetChainIds.map((item) => String(item || '').trim()).filter(Boolean)
    : [];
  const ligandChainId = String(input.ligandChainId || '').trim();
  const ligandSmiles = String(input.ligandSmiles || '').trim();
  if (targetChainIds.length > 0) {
    form.append('target_chain', targetChainIds.join(','));
  }
  if (ligandChainId) {
    form.append('ligand_chain', ligandChainId);
  }
  if (ligandChainId && ligandSmiles) {
    form.append('ligand_smiles_map', JSON.stringify({ [ligandChainId]: ligandSmiles }));
  }

  const enableAffinity = Boolean(input.enableAffinity);
  if (enableAffinity) {
    if (!targetChainIds.length || !ligandChainId || !ligandSmiles) {
      throw new Error('Affinity mode needs target chain(s), ligand chain, and ligand SMILES.');
    }
    form.append('enable_affinity', 'true');
    form.append('auto_enable_affinity', 'true');
  }
  if (input.affinityRefine) {
    form.append('affinity_refine', 'true');
  }
  if (normalizedSeed !== null) {
    form.append('seed', String(normalizedSeed));
  }
  if (!useProtenix) {
    const useMsaServer = input.useMsa === true;
    form.append('use_msa_server', String(useMsaServer).toLowerCase());
    form.append('structure_refine', String(BOLTZ2SCORE_AFFINITY_PROFILE.structureRefine).toLowerCase());
    form.append('recycling_steps', String(BOLTZ2SCORE_AFFINITY_PROFILE.recyclingSteps));
    form.append('sampling_steps', String(BOLTZ2SCORE_AFFINITY_PROFILE.samplingSteps));
    form.append('diffusion_samples', String(BOLTZ2SCORE_AFFINITY_PROFILE.diffusionSamples));
    form.append('max_parallel_samples', String(BOLTZ2SCORE_AFFINITY_PROFILE.maxParallelSamples));
  }
  form.append('priority', 'high');
  const endpoint = useProtenix ? '/api/protenix2score' : '/api/boltz2score';
  if (useProtenix) {
    const useMsa = input.useMsa !== false;
    const useTemplate = Boolean(input.useTemplate);
    form.append('use_msa', String(useMsa).toLowerCase());
    form.append('use_template', String(useTemplate).toLowerCase());
  }

  const res = await requestBackend(endpoint, {
    method: 'POST',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    },
    body: form
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to submit affinity scoring (${res.status}): ${text}`);
  }

  const data = (await res.json()) as { task_id?: string };
  if (!data.task_id) {
    throw new Error('Affinity submit response did not include task_id.');
  }
  return data.task_id;
}

export async function getTaskStatus(taskId: string): Promise<TaskStatusResponse> {
  const res = await requestBackend(`/status/${taskId}`, {
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

export async function terminateTask(taskId: string): Promise<{
  status?: string;
  task_id?: string;
  terminated?: boolean;
  details?: Record<string, unknown>;
}> {
  const normalizedTaskId = String(taskId || '').trim();
  if (!normalizedTaskId) {
    throw new Error('Missing task_id for termination.');
  }
  const res = await requestBackend(`/tasks/${encodeURIComponent(normalizedTaskId)}`, {
    method: 'DELETE',
    headers: {
      ...API_HEADERS,
      Accept: 'application/json'
    }
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to terminate task (${res.status}): ${text}`);
  }
  const payload = (await res.json().catch(() => ({}))) as {
    status?: string;
    task_id?: string;
    terminated?: boolean;
    details?: Record<string, unknown>;
  };
  return payload;
}

type DownloadResultMode = 'view' | 'full';

export async function downloadResultBlob(taskId: string, options?: { mode?: DownloadResultMode }): Promise<Blob> {
  const mode = options?.mode || 'full';
  const path = mode === 'view' ? `/results/${taskId}/view` : `/results/${taskId}`;
  const url = apiUrl(path);
  const res = await fetchWithTimeout(url, {
    cache: 'no-store'
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Failed to download result (${res.status}) from ${url}: ${text}`);
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

  const scoredCandidates = await Promise.all(
    confidenceCandidates.map(async (file) => {
      const payload = await readZipJson(zip, file);
      return {
        file,
        confidenceScore: payload ? toFiniteNumber(payload.confidence_score) : null,
        complexPlddt: payload ? toFiniteNumber(payload.complex_plddt) : null,
        iptm: payload ? toFiniteNumber(payload.iptm) : null,
        heuristicScore: boltzConfidenceHeuristicScore(file)
      };
    })
  );

  scoredCandidates.sort((a, b) => {
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
  const matchedStructure = selectedConfidence ? resolveBoltzStructureForConfidence(names, selectedConfidence) : null;
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

function applyLigandAtomPlddtsByChainToCifStructure(
  structureText: string,
  atomPlddtsByChain: Record<string, number[]>
): string {
  if (!structureText) return structureText;
  if (Object.keys(atomPlddtsByChain).length === 0) return structureText;

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
    if (compCol < 0 || chainCol < 0 || bFactorCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    const firstResidueKeyByChain = new Map<string, string>();
    const chainAtomCursor = new Map<string, number>();
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

        const chainValues = atomPlddtsByChain[chainId];
        if (!Array.isArray(chainValues) || chainValues.length === 0) {
          rowIndex += 1;
          continue;
        }
        const cursor = chainAtomCursor.get(chainId) || 0;
        if (cursor >= chainValues.length) {
          rowIndex += 1;
          continue;
        }

        tokens[bFactorCol] = formatPlddtNumber(chainValues[cursor]);
        lines[rowIndex] = tokens.join(' ');
        chainAtomCursor.set(chainId, cursor + 1);
      }
      rowIndex += 1;
    }

    i = rowIndex - 1;
  }

  return lines.join('\n');
}

function applyLigandAtomPlddtsByChainToPdbStructure(
  structureText: string,
  atomPlddtsByChain: Record<string, number[]>
): string {
  if (!structureText) return structureText;
  if (Object.keys(atomPlddtsByChain).length === 0) return structureText;

  const lines = structureText.split(/\r?\n/);
  const firstResidueKeyByChain = new Map<string, string>();
  const chainAtomCursor = new Map<string, number>();

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

    const chainValues = atomPlddtsByChain[chainId];
    if (!Array.isArray(chainValues) || chainValues.length === 0) continue;
    const cursor = chainAtomCursor.get(chainId) || 0;
    if (cursor >= chainValues.length) continue;

    const bFactor = normalizePlddt(chainValues[cursor]).toFixed(2).padStart(6);
    const padded = line.length >= 66 ? line : line.padEnd(66, ' ');
    lines[i] = `${padded.slice(0, 60)}${bFactor}${padded.slice(66)}`;
    chainAtomCursor.set(chainId, cursor + 1);
  }

  return lines.join('\n');
}

function applyLigandAtomPlddtsByChainToStructure(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  atomPlddtsByChain: Record<string, number[]>
): string {
  if (Object.keys(atomPlddtsByChain).length === 0) return structureText;
  return structureFormat === 'pdb'
    ? applyLigandAtomPlddtsByChainToPdbStructure(structureText, atomPlddtsByChain)
    : applyLigandAtomPlddtsByChainToCifStructure(structureText, atomPlddtsByChain);
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

    const groupCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.group_pdb');
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
        const groupPdb = groupCol >= 0 ? stripCifTokenQuotes(tokens[groupCol]).trim().toUpperCase() : '';
        const chainId = stripCifTokenQuotes(tokens[chainCol]).trim();
        const seqToken = stripCifTokenQuotes(tokens[seqCol]).trim();
        const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
        const bFactor = Number(tokens[bFactorCol]);
        const seqId = Number.parseInt(seqToken, 10);
        if (!chainId || !compId || !Number.isFinite(seqId) || seqId <= 0 || !Number.isFinite(bFactor)) {
          rowIndex += 1;
          continue;
        }
        // Keep local QA rows polymer-only, so ligands can retain atom-level B-factor confidence coloring.
        if (groupPdb) {
          if (groupPdb !== 'ATOM') {
            rowIndex += 1;
            continue;
          }
        } else if (!POLYMER_COMP_IDS.has(compId)) {
          rowIndex += 1;
          continue;
        }
        {
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

function pruneCifMaQaMetricLocalToPolymer(structureText: string): string {
  if (!structureText) return structureText;
  if (!structureText.includes('_ma_qa_metric_local.label_comp_id')) return structureText;
  const lines = structureText.split(/\r?\n/);
  const output: string[] = [];
  let changed = false;

  let i = 0;
  while (i < lines.length) {
    if (lines[i].trim() !== 'loop_') {
      output.push(lines[i]);
      i += 1;
      continue;
    }

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    const isQaLocalLoop = headers.some((header) => header.toLowerCase().startsWith('_ma_qa_metric_local.'));
    const compCol = headers.findIndex((header) => header.toLowerCase() === '_ma_qa_metric_local.label_comp_id');
    if (!isQaLocalLoop || compCol < 0) {
      let rowIndex = headerIndex;
      while (rowIndex < lines.length) {
        const row = lines[rowIndex].trim();
        if (row === 'loop_' || row.startsWith('_')) break;
        rowIndex += 1;
      }
      output.push(...lines.slice(i, rowIndex));
      i = rowIndex;
      continue;
    }

    output.push(lines[i]);
    output.push(...lines.slice(i + 1, headerIndex));
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (row === 'loop_' || row.startsWith('_')) break;
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
        if (POLYMER_COMP_IDS.has(compId)) {
          output.push(tokens.join(' '));
        } else {
          changed = true;
        }
      }
      rowIndex += 1;
    }
    output.push('#');
    i = rowIndex;
  }

  if (!changed) return structureText;
  return output.join('\n');
}

export function ensureStructureConfidenceColoringData(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  backend: string
): string {
  if (!structureText) return structureText;
  if (structureFormat !== 'cif') return structureText;
  const normalizedBackend = String(backend || '').trim().toLowerCase();
  if (normalizedBackend !== 'protenix') return structureText;
  const pruned = pruneCifMaQaMetricLocalToPolymer(structureText);
  return ensureCifHasMaQaMetricLocal(pruned);
}

function compactConfidenceForStorage(input: Record<string, unknown>): Record<string, unknown> {
  const next = { ...input };
  const pae = next.pae;
  // Full PAE matrices dominate payload size but UI reads compact summary metrics (complex_pde/complex_pae).
  if (Array.isArray(pae) || (pae && typeof pae === 'object')) {
    delete next.pae;
  }
  return next;
}

export async function parseResultBundle(blob: Blob): Promise<ParsedResultBundle | null> {
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
    const confFile = isProtenix
      ? protenixSelection?.confidenceFile || null
      : boltzSelection?.confidenceFile || null;
    const parsedConfidence = await readZipJson(zip, confFile);
    if (parsedConfidence) {
      confidence = compactConfidenceForStorage({
        ...parsedConfidence,
        backend: isProtenix ? 'protenix' : 'boltz'
      });
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

  const existingLigandAtomPlddtsFlat = normalizeLigandAtomPlddts(
    toFiniteNumberArray((confidence as Record<string, unknown>).ligand_atom_plddts)
  );
  const existingLigandAtomPlddtsByChainRaw = normalizeLigandAtomPlddtsByChain(
    (confidence as Record<string, unknown>).ligand_atom_plddts_by_chain
  );
  const existingLigandAtomPlddtsByChain = selectLigandAtomPlddtsByChain(
    confidence as Record<string, unknown>,
    existingLigandAtomPlddtsByChainRaw
  );
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
  if (Object.keys(ligandAtomPlddtsByChainForRender).length > 0) {
    structureText = applyLigandAtomPlddtsByChainToStructure(
      structureText,
      structureFormat,
      ligandAtomPlddtsByChainForRender
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
  confidence = compactConfidenceForStorage(confidence);

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
