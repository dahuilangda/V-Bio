import type { InputComponent, PredictionSubmitInput } from '../types/models';
import { normalizeComponentSequence } from '../utils/projectInputs';
import { buildVirtualScreeningYaml, validateVirtualScreeningSmiles } from '../utils/virtualScreening';
import { buildPredictionYamlFromComponents, collectCustomCcdMoleculesFromComponents } from '../utils/yaml';
import { API_HEADERS, requestBackend } from './backendClient';

export async function submitPrediction(input: PredictionSubmitInput): Promise<string> {
  const workflow =
    input.workflow === 'peptide_design'
      ? 'peptide_design'
      : input.workflow === 'virtual_screening'
        ? 'virtual_screening'
        : 'prediction';
  const rawBackend = String(input.backend || 'boltz').trim().toLowerCase();
  const backend = rawBackend === 'nesso1' || rawBackend === 'nesso-1' ? 'nesso' : rawBackend;
  if (backend === 'nesso' && workflow !== 'virtual_screening') {
    throw new Error('Nesso-1 is available through the independent Virtual Screening workflow only.');
  }
  if (workflow === 'virtual_screening' && backend !== 'nesso') {
    throw new Error('Virtual Screening currently requires the Nesso-1 backend.');
  }
  const constraintsForBackend =
    backend === 'nesso'
      ? []
      : (input.constraints || []).filter((constraint) =>
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
  let yaml = '';
  if (workflow === 'virtual_screening') {
    const unsupportedTypes = Array.from(
      new Set(
        componentsForYaml
          .filter((component) => component.type !== 'protein' && component.type !== 'ligand')
          .map((component) => component.type)
      )
    );
    if (unsupportedTypes.length > 0) {
      throw new Error('Nesso-1 Virtual Screening accepts protein and ligand components only; DNA/RNA are not supported.');
    }
    const proteinComponents = componentsForYaml.filter((component) => component.type === 'protein');
    if (proteinComponents.length === 0) {
      throw new Error('Virtual Screening requires at least one target protein component.');
    }
    if (proteinComponents.some((component) => component.cyclic)) {
      throw new Error('Nesso-1 virtual screening does not support cyclic proteins.');
    }
    if (proteinComponents.some((component) => (component.modifications || []).length > 0)) {
      throw new Error('Nesso-1 virtual screening does not support modified protein residues.');
    }
    const contextSmiles = componentsForYaml
      .map((component, index) => ({ component, index }))
      .filter(({ component }) => component.type === 'ligand' && component.inputMethod !== 'ccd')
      .map(({ component, index }) => ({
        id: component.id || `context-ligand-${index + 1}`,
        name: `Context ligand ${index + 1}`,
        smiles: component.sequence,
        sourceIndex: index + 1
      }));
    if (contextSmiles.length > 0) {
      const contextValidation = await validateVirtualScreeningSmiles(contextSmiles);
      if (contextValidation.invalid.length > 0) {
        throw new Error(`Invalid context ligand: ${contextValidation.invalid[0].message}`);
      }
    }
    const screening = buildVirtualScreeningYaml({
      components: componentsForYaml,
      rawInput: input.virtualScreeningInput || ''
    });
    const validation = await validateVirtualScreeningSmiles(screening.compounds);
    if (validation.invalid.length > 0) {
      throw new Error(validation.invalid.slice(0, 3).map((item) => item.message).join(' '));
    }
    yaml = screening.yaml;
  }
  const supportsTemplates = backend !== 'nesso';
  const templateUploads = supportsTemplates && Array.isArray(input.templateUploads) ? input.templateUploads : [];
  const yamlTemplates = templateUploads.map((item) => ({
    fileName: item.fileName,
    format: item.format,
    templateChainId: item.templateChainId,
    targetChainIds: item.targetChainIds
  }));
  const hasTemplateUploads = yamlTemplates.length > 0;
  const useMsaServer =
    backend === 'nesso'
      ? false
      : componentsForYaml.some((comp) => comp.type === 'protein' && comp.useMsa !== false);
  if (workflow !== 'virtual_screening') {
    yaml = buildPredictionYamlFromComponents(componentsForYaml, {
      constraints: constraintsForBackend,
      properties: input.properties,
      templates: yamlTemplates,
      preserveLigandSmiles: false
    });
  }

  const form = new FormData();
  const projectId = String(input.projectId || '').trim();
  if (!projectId) {
    throw new Error('Prediction requires the project id — the gateway rejects submits without it.');
  }
  form.append('project_id', projectId);
  const yamlFile = new File([yaml], 'config.yaml', { type: 'application/x-yaml' });
  form.append('yaml_file', yamlFile);
  form.append('backend', backend || 'boltz');
  form.append('workflow', workflow);
  form.append('use_msa_server', String(useMsaServer).toLowerCase());
  if (workflow !== 'virtual_screening' && input.properties) {
    form.append('properties', JSON.stringify(input.properties));
    if (backend !== 'nesso' && (input.properties.ligand || input.properties.binder)) {
      form.append('require_ipsae', 'true');
    }
  }
  if (workflow === 'peptide_design' && input.peptideDesignOptions) {
    form.append('peptide_design_options', JSON.stringify(input.peptideDesignOptions));
  }
  if (workflow === 'peptide_design' && input.peptideStructureUpload) {
    const up = input.peptideStructureUpload;
    form.append(
      'peptide_structure_file',
      new File([up.content], up.fileName, { type: 'application/octet-stream' })
    );
    form.append(
      'peptide_structure_meta',
      JSON.stringify({ format: up.format, chain_id: up.chainId || '' })
    );
  }
  const peptideTargetChainId = String(input.peptideDesignTargetChainId || '').trim();
  if (workflow === 'peptide_design' && peptideTargetChainId) {
    form.append('peptide_design_target_chain', peptideTargetChainId);
  }
  if (typeof input.seed === 'number' && Number.isFinite(input.seed)) {
    form.append('seed', String(Math.max(0, Math.floor(input.seed))));
  }
  form.append('low_vram', String(backend !== 'nesso' && input.lowVram === true));
  const customCcdMolecules = backend === 'nesso'
    ? []
    : input.customCcdMolecules || collectCustomCcdMoleculesFromComponents(componentsForYaml);
  if (customCcdMolecules.length > 0) {
    form.append('custom_ccd_molecules', JSON.stringify(customCcdMolecules));
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
