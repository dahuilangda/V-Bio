import type { InputComponent, PredictionSubmitInput } from '../types/models';
import { normalizeComponentSequence } from '../utils/projectInputs';
import { buildPredictionYaml, buildPredictionYamlFromComponents } from '../utils/yaml';
import { API_HEADERS, requestBackend } from './backendClient';

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
  const templateUploads = Array.isArray(input.templateUploads) ? input.templateUploads : [];
  const yamlTemplates = templateUploads.map((item) => ({
    fileName: item.fileName,
    format: item.format,
    templateChainId: item.templateChainId,
    targetChainIds: item.targetChainIds
  }));
  const hasTemplateUploads = yamlTemplates.length > 0;
  const useMsaServer = componentsForYaml.some((comp) => comp.type === 'protein' && comp.useMsa !== false);
  const hasConstraints = constraintsForBackend.length > 0;
  const hasAffinityProperty = Boolean(input.properties?.affinity && (input.properties?.ligand || input.properties?.binder));
  const useSimpleYaml =
    !hasTemplateUploads &&
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
        properties: input.properties,
        templates: yamlTemplates
      });

  const form = new FormData();
  const yamlFile = new File([yaml], 'config.yaml', { type: 'application/x-yaml' });
  form.append('yaml_file', yamlFile);
  form.append('backend', backend || 'boltz');
  form.append('use_msa_server', String(useMsaServer).toLowerCase());
  if (typeof input.seed === 'number' && Number.isFinite(input.seed)) {
    form.append('seed', String(Math.max(0, Math.floor(input.seed))));
  }
  if (hasTemplateUploads) {
    for (const item of templateUploads) {
      form.append(
        'template_files',
        new File([item.content], item.fileName, {
          type: 'application/octet-stream'
        })
      );
    }
  }
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
