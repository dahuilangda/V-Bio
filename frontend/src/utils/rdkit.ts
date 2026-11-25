import { ENV } from './env';

export interface RDKitMol {
  get_svg: (width: number, height: number) => string;
  get_svg_with_highlights?: (details: string) => string;
  get_molblock?: () => string;
  get_num_atoms?: () => number;
  get_smiles?: (details?: string) => string;
  get_descriptors?: () => string | Record<string, unknown>;
  get_substruct_match?: (query: RDKitMol) => string | unknown;
  get_substruct_matches?: (query: RDKitMol) => string | unknown;
  normalize_depiction?: () => void;
  set_new_coords?: () => void;
  delete: () => void;
}

export interface RDKitModule {
  get_mol: (smiles: string) => RDKitMol | null;
  get_qmol?: (smarts: string) => RDKitMol | null;
}

type RDKitModuleFactory = (params?: { locateFile?: (path: string) => string }) => Promise<RDKitModule>;

let modulePromise: Promise<RDKitModule> | null = null;

export function loadRDKitModule(): Promise<RDKitModule> {
  if (modulePromise) return modulePromise;
  modulePromise = import(/* @vite-ignore */ ENV.rdkitModuleUrl)
    .then((module) => (module as { default: RDKitModuleFactory }).default({
      locateFile: (path: string) => (path.endsWith('.wasm') ? ENV.rdkitWasmUrl : path)
    }))
    .catch((error) => {
      modulePromise = null;
      throw error;
    });
  return modulePromise;
}
