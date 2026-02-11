import { ENV } from './env';
import { loadScript } from './script';

export interface RDKitMol {
  get_svg: (width: number, height: number) => string;
  get_svg_with_highlights?: (details: string) => string;
  get_num_atoms?: () => number;
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

declare global {
  interface Window {
    initRDKitModule?: (params?: { locateFile?: (path: string) => string }) => Promise<RDKitModule>;
    __rdkitModulePromise?: Promise<RDKitModule>;
  }
}

export async function loadRDKitModule(): Promise<RDKitModule> {
  if (window.__rdkitModulePromise) {
    return window.__rdkitModulePromise;
  }

  window.__rdkitModulePromise = (async () => {
    await loadScript(ENV.rdkitScriptUrl);

    if (typeof window.initRDKitModule !== 'function') {
      throw new Error('RDKit module init function is unavailable.');
    }

    return await window.initRDKitModule({
      locateFile: (path: string) => {
        if (path.endsWith('.wasm')) return ENV.rdkitWasmUrl;
        return path;
      }
    });
  })();

  return window.__rdkitModulePromise;
}
