import {
  buildComponentAtomOptionsByChain,
  customResidueAtomNamesFromSmiles,
  detectCustomResidueBackbone,
  validateBackboneSlots,
  validateCustomResidueBackbone
} from './constraintAtomOptions';
import { molMatchesQuery, rdkitMolHasAminoAcidBackbone } from './inputValidation';
import { renderLigand2DSvg } from './ligand2d';
import type { Envelope } from '../bundles/rpc';
import type { MolSvgOptions, RdkitCall, RdkitOps, RdkitOp, RdkitResult } from './rdkitContract';
import { loadRDKitModule, type RDKitMol, type RDKitModule } from './rdkit';
import { toggleTerminalAmide } from './smilesTransform';

type Handler<K extends RdkitOp> = (rdkit: RDKitModule, ...args: RdkitOps[K]['args']) => RdkitOps[K]['result'];

function optionalMol(rdkit: RDKitModule, smiles: string): RDKitMol | null {
  try {
    return rdkit.get_mol(smiles);
  } catch {
    return null;
  }
}

const handlers: { [K in RdkitOp]: Handler<K> } = {
  renderLigand2DSvg: (rdkit, options) => renderLigand2DSvg(rdkit, options),

  renderMolSvg: (rdkit, smiles, options: MolSvgOptions) => {
    const mol = rdkit.get_mol(smiles);
    if (!mol) {
      throw new Error('Invalid ligand SMILES.');
    }
    try {
      mol.set_new_coords?.();
      mol.normalize_depiction?.();
      let rendered = '';
      if (options.details && typeof mol.get_svg_with_highlights === 'function') {
        rendered = mol.get_svg_with_highlights(JSON.stringify(options.details)) || '';
      }
      if (!rendered) {
        rendered = mol.get_svg(options.width, options.height);
      }
      if (!rendered) {
        throw new Error('RDKit returned empty SVG.');
      }
      return rendered;
    } finally {
      mol.delete();
    }
  },

  substructureFilter: (rdkit, query, smilesList) => {
    const queryMol = (typeof rdkit.get_qmol === 'function' ? rdkit.get_qmol(query) : null) || rdkit.get_mol(query);
    if (!queryMol) {
      throw new Error('Invalid SMARTS/SMILES query.');
    }
    try {
      return smilesList.map((smiles) => {
        const mol = smiles.trim() ? rdkit.get_mol(smiles) : null;
        if (!mol) return false;
        try {
          return molMatchesQuery(mol, queryMol);
        } finally {
          mol.delete();
        }
      });
    } finally {
      queryMol.delete();
    }
  },

  molHasAminoAcidBackbone: rdkitMolHasAminoAcidBackbone,
  detectCustomResidueBackbone,
  validateCustomResidueBackbone,
  validateBackboneSlots,
  customResidueAtomNamesFromSmiles,

  buildComponentAtomOptionsByChain: (rdkit, components) => buildComponentAtomOptionsByChain(components, rdkit),

  toggleTerminalAmide,

  molDescriptors: (rdkit, smiles) => {
    const mol = rdkit.get_mol(smiles);
    if (!mol) return null;
    try {
      const raw = mol.get_descriptors?.();
      if (typeof raw === 'string') {
        return JSON.parse(raw) as Record<string, unknown>;
      }
      return raw && typeof raw === 'object' ? raw : {};
    } finally {
      mol.delete();
    }
  },

  canonicalSmilesList: (rdkit, smilesList) =>
    smilesList.map((smiles) => {
      const mol = optionalMol(rdkit, smiles);
      if (!mol) return null;
      try {
        return mol.get_smiles?.() ?? '';
      } finally {
        mol.delete();
      }
    })
};

function dispatch(rdkit: RDKitModule, call: RdkitCall): RdkitResult {
  const handler = (handlers as Record<string, (rdkit: RDKitModule, ...args: unknown[]) => unknown>)[call.op];
  if (typeof handler !== 'function') {
    throw new Error(`Unknown RDKit operation: ${String(call.op)}`);
  }
  return handler(rdkit, ...call.args) as RdkitResult;
}

const scope = self as unknown as {
  postMessage: (message: Envelope<RdkitResult>) => void;
  onmessage: ((event: MessageEvent<{ id: number; call: RdkitCall }>) => void) | null;
};

scope.onmessage = (event) => {
  const { id, call } = event.data;
  void (async () => {
    try {
      scope.postMessage({ id, ok: true, result: dispatch(await loadRDKitModule(), call) });
    } catch (error) {
      scope.postMessage({ id, ok: false, error: error instanceof Error ? error.message : String(error) });
    }
  })();
};
