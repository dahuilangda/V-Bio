import { createRpc } from '../bundles/rpc';
import type { CustomResidueBackbone, InputComponent } from '../types/models';
import type { BackboneSlotErrors } from './constraintAtomOptions';
import type { Ligand2DRenderOptions } from './ligand2d';
import type { StructureAtomOptionsByChain } from './structureParser';
import type { Envelope } from '../bundles/rpc';
import type { RdkitCall, RdkitOps, RdkitOp, RdkitResult } from './rdkitContract';

const rpc = createRpc<RdkitCall, RdkitResult>({
  open: (reply, crash) => {
    const worker = new Worker(new URL('./rdkit.worker.ts', import.meta.url), { type: 'module' });
    worker.addEventListener('message', (event: MessageEvent<Envelope<RdkitResult>>) => reply(event.data));
    worker.addEventListener('error', (event) => crash(new Error(event.message || 'RDKit worker failed.')));
    return { send: (id, call) => worker.postMessage({ id, call }), close: () => worker.terminate() };
  },
  idleMs: 60_000
});

function invoke<K extends RdkitOp>(op: K, ...args: RdkitOps[K]['args']): Promise<RdkitOps[K]['result']> {
  return rpc.call({ op, args } as RdkitCall) as Promise<RdkitOps[K]['result']>;
}

export function renderLigand2DSvg(options: Ligand2DRenderOptions): Promise<string> {
  return invoke('renderLigand2DSvg', options);
}

export function renderMolSvg(
  smiles: string,
  options: { width: number; height: number; details?: Record<string, unknown> }
): Promise<string> {
  return invoke('renderMolSvg', smiles, options);
}

export function substructureFilter(query: string, smilesList: string[]): Promise<boolean[]> {
  return invoke('substructureFilter', query, smilesList);
}

export function molHasAminoAcidBackbone(smiles: string, allowTerminal = true): Promise<boolean> {
  return invoke('molHasAminoAcidBackbone', smiles, allowTerminal);
}

export function detectCustomResidueBackbone(
  smiles: string,
  anchors?: Partial<CustomResidueBackbone>,
  amidated = false
): Promise<CustomResidueBackbone | null> {
  return invoke('detectCustomResidueBackbone', smiles, anchors, amidated);
}

export function validateCustomResidueBackbone(
  smiles: string,
  backbone: CustomResidueBackbone,
  amidated: boolean
): Promise<BackboneSlotErrors> {
  return invoke('validateCustomResidueBackbone', smiles, backbone, amidated);
}

export function validateBackboneSlots(
  smiles: string,
  slots: Partial<CustomResidueBackbone>,
  amidated: boolean
): Promise<Partial<CustomResidueBackbone>> {
  return invoke('validateBackboneSlots', smiles, slots, amidated);
}

export function customResidueAtomNamesFromSmiles(
  smiles: string,
  backboneOverride?: CustomResidueBackbone,
  amidated = false
): Promise<string[] | null> {
  return invoke('customResidueAtomNamesFromSmiles', smiles, backboneOverride, amidated);
}

export function resolveComponentAtomOptions(components: InputComponent[]): Promise<StructureAtomOptionsByChain> {
  return invoke('buildComponentAtomOptionsByChain', components);
}

export function toggleTerminalAmide(
  smiles: string,
  backbone: Partial<CustomResidueBackbone> | undefined,
  toAmide: boolean
): Promise<string | null> {
  return invoke('toggleTerminalAmide', smiles, backbone, toAmide);
}

export function molDescriptors(smiles: string): Promise<Record<string, unknown> | null> {
  return invoke('molDescriptors', smiles);
}

export function canonicalSmilesList(smilesList: string[]): Promise<Array<string | null>> {
  return invoke('canonicalSmilesList', smilesList);
}
