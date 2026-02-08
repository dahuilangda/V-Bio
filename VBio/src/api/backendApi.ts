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
  const hasConstraints = Boolean(input.constraints && input.constraints.length > 0);
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
        constraints: input.constraints,
        properties: input.properties
      });

  const form = new FormData();
  const yamlFile = new File([yaml], 'config.yaml', { type: 'application/x-yaml' });
  form.append('yaml_file', yamlFile);
  form.append('backend', input.backend || 'boltz');
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

function chooseBestStructureFile(names: string[]): string | null {
  const candidates = names
    .filter((n) => /\.(cif|pdb)$/i.test(n))
    .map((name) => {
      const lower = name.toLowerCase();
      let score = 100;
      if (lower.endsWith('.cif')) score -= 5;
      if (lower.includes('model_0') || lower.includes('ranked_0')) score -= 20;
      else if (lower.includes('model_') || lower.includes('ranked_')) score -= 5;
      if (lower.includes('af3/output')) score -= 10;
      if (lower.includes('seed-')) score += 4;
      return { name, score };
    })
    .sort((a, b) => a.score - b.score || a.name.length - b.name.length);

  return candidates[0]?.name ?? null;
}

function chooseJsonByKeyword(names: string[], keyword: string): string | null {
  const target = names
    .filter((n) => n.toLowerCase().endsWith('.json') && n.toLowerCase().includes(keyword))
    .sort((a, b) => a.length - b.length);
  return target[0] ?? null;
}

export async function parseResultBundle(blob: Blob): Promise<ParsedResultBundle | null> {
  const zip = await JSZip.loadAsync(blob);
  const names = Object.keys(zip.files);

  const structureFile = chooseBestStructureFile(names);
  if (!structureFile) {
    return null;
  }

  const structureText = await zip.file(structureFile)?.async('text');
  if (!structureText) {
    return null;
  }

  const confFile = chooseJsonByKeyword(names, 'confidence');
  const affinityFile = chooseJsonByKeyword(names, 'affinity');

  let confidence: Record<string, unknown> = {};
  let affinity: Record<string, unknown> = {};

  if (confFile) {
    const t = await zip.file(confFile)?.async('text');
    if (t) {
      try {
        confidence = JSON.parse(t) as Record<string, unknown>;
      } catch {
        confidence = {};
      }
    }
  }

  if (affinityFile) {
    const t = await zip.file(affinityFile)?.async('text');
    if (t) {
      try {
        affinity = JSON.parse(t) as Record<string, unknown>;
      } catch {
        affinity = {};
      }
    }
  }

  return {
    structureText,
    structureFormat: structureFile.toLowerCase().endsWith('.pdb') ? 'pdb' : 'cif',
    structureName: structureFile,
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
