import type { AffinityPreviewPayload, AffinitySubmitInput } from '../types/models';
import { API_HEADERS, requestBackend } from './backendClient';

const BOLTZ2SCORE_AFFINITY_PROFILE = Object.freeze({
  // Affinity scoring should keep input complex geometry by default (score-only mode).
  structureRefine: false,
  recyclingSteps: 20,
  samplingSteps: 1,
  diffusionSamples: 1,
  maxParallelSamples: 1
});

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
  const targetFile = input.targetFile instanceof File ? input.targetFile : null;
  const ligandFile = input.ligandFile instanceof File ? input.ligandFile : null;
  const useSeparateBoltzInputs = Boolean(targetFile && ligandFile);
  if (!useSeparateBoltzInputs && !structureText) {
    throw new Error('Affinity scoring requires a prepared input structure.');
  }
  const normalizedSeed =
    typeof input.seed === 'number' && Number.isFinite(input.seed) ? Math.max(0, Math.floor(input.seed)) : null;

  const form = new FormData();
  if (useSeparateBoltzInputs && targetFile && ligandFile) {
    form.append('protein_file', targetFile);
    form.append('ligand_file', ligandFile);
  } else {
    form.append(
      'input_file',
      new File([structureText], input.inputStructureName || 'affinity_input.cif', { type: 'chemical/x-cif' })
    );
  }
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
  if (ligandSmiles) {
    const ligandMapChainId = ligandChainId || (useSeparateBoltzInputs ? 'L' : '');
    if (ligandMapChainId) {
      form.append('ligand_smiles_map', JSON.stringify({ [ligandMapChainId]: ligandSmiles }));
    }
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
  const useMsaServer = input.useMsa === true;
  form.append('use_msa_server', String(useMsaServer).toLowerCase());
  form.append('structure_refine', String(BOLTZ2SCORE_AFFINITY_PROFILE.structureRefine).toLowerCase());
  form.append('recycling_steps', String(BOLTZ2SCORE_AFFINITY_PROFILE.recyclingSteps));
  form.append('sampling_steps', String(BOLTZ2SCORE_AFFINITY_PROFILE.samplingSteps));
  form.append('diffusion_samples', String(BOLTZ2SCORE_AFFINITY_PROFILE.diffusionSamples));
  form.append('max_parallel_samples', String(BOLTZ2SCORE_AFFINITY_PROFILE.maxParallelSamples));
  form.append('priority', 'high');
  const endpoint = '/api/boltz2score';

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
