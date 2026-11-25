import type { CustomResidueBackbone, InputComponent } from '../types/models';
import type { BackboneSlotErrors } from './constraintAtomOptions';
import type { Ligand2DRenderOptions } from './ligand2d';
import type { StructureAtomOptionsByChain } from './structureParser';

export interface MolSvgOptions {
  width: number;
  height: number;
  details?: Record<string, unknown>;
}

export interface RdkitOps {
  renderLigand2DSvg: { args: [options: Ligand2DRenderOptions]; result: string };
  renderMolSvg: { args: [smiles: string, options: MolSvgOptions]; result: string };
  substructureFilter: { args: [query: string, smilesList: string[]]; result: boolean[] };
  molHasAminoAcidBackbone: { args: [smiles: string, allowTerminal: boolean]; result: boolean };
  detectCustomResidueBackbone: {
    args: [smiles: string, anchors: Partial<CustomResidueBackbone> | undefined, amidated: boolean];
    result: CustomResidueBackbone | null;
  };
  validateCustomResidueBackbone: {
    args: [smiles: string, backbone: CustomResidueBackbone, amidated: boolean];
    result: BackboneSlotErrors;
  };
  validateBackboneSlots: {
    args: [smiles: string, slots: Partial<CustomResidueBackbone>, amidated: boolean];
    result: Partial<CustomResidueBackbone>;
  };
  customResidueAtomNamesFromSmiles: {
    args: [smiles: string, backboneOverride: CustomResidueBackbone | undefined, amidated: boolean];
    result: string[] | null;
  };
  buildComponentAtomOptionsByChain: { args: [components: InputComponent[]]; result: StructureAtomOptionsByChain };
  toggleTerminalAmide: {
    args: [smiles: string, backbone: Partial<CustomResidueBackbone> | undefined, toAmide: boolean];
    result: string | null;
  };
  molDescriptors: { args: [smiles: string]; result: Record<string, unknown> | null };
  canonicalSmilesList: { args: [smilesList: string[]]; result: Array<string | null> };
}

export type RdkitOp = keyof RdkitOps;

export type RdkitCall = {
  [K in RdkitOp]: { op: K; args: RdkitOps[K]['args'] };
}[RdkitOp];

export type RdkitResult = {
  [K in RdkitOp]: RdkitOps[K]['result'];
}[RdkitOp];
