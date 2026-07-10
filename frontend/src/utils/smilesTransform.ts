import { loadRDKitModule } from './rdkit';
import type { RDKitModule, RDKitMol } from './rdkit';
import { detectCustomResidueBackbone } from './constraintAtomOptions';
import type { CustomResidueBackbone } from '../types/models';

// V2000 atom block: element symbol occupies columns 32-34, i.e. string indices 31-33.
const ATOM_SYMBOL_START = 31;

// Toggle a custom-residue SMILES between -COOH and -CONH2 by rewriting the backbone's terminal
// atom (OXT oxygen <-> NXT nitrogen). The atom is the one the backbone picker already recognizes
// as the terminal slot — the user's OXT assignment if present, else the auto-detected terminal
// under the pre-toggle chemistry — not an arbitrary terminal atom. Returns null when the terminal
// can't be resolved, so the caller leaves the SMILES unchanged.
//
// The minimal RDKit WASM has no atom-editing API or reactions, so the symbol is rewritten in the
// molblock and re-parsed.
export async function toggleTerminalAmide(
  smiles: string,
  backbone: Partial<CustomResidueBackbone> | undefined,
  toAmide: boolean
): Promise<string | null> {
  const rdkit = await loadRDKitModule();
  const terminal = detectCustomResidueBackbone(rdkit, smiles, backbone, !toAmide)?.oxt;
  if (terminal === undefined || terminal < 0) return null;
  return replaceAtomElement(rdkit, smiles, terminal, toAmide ? 'N' : 'O');
}

function replaceAtomElement(rdkit: RDKitModule, smiles: string, atomIdx: number, element: string): string | null {
  const mol = rdkit.get_mol(smiles);
  if (!mol) return null;
  try {
    const molblock = mol.get_molblock?.() ?? null;
    if (!molblock) return null;
    const rewritten = rewriteAtomSymbol(molblock, atomIdx, element);
    if (!rewritten) return null;
    const next = rdkit.get_mol(rewritten) as RDKitMol | null;
    if (!next) return null;
    try {
      return next.get_smiles?.().trim() || null;
    } finally {
      next.delete();
    }
  } finally {
    mol.delete();
  }
}

function rewriteAtomSymbol(molblock: string, atomIdx: number, element: string): string | null {
  const lines = molblock.split('\n');
  const numAtoms = parseInt(lines[3]?.slice(0, 3), 10);
  if (!Number.isInteger(numAtoms) || atomIdx < 0 || atomIdx >= numAtoms) return null;
  const lineIdx = 4 + atomIdx;
  const line = lines[lineIdx];
  if (!line || line.length < ATOM_SYMBOL_START + 3) return null;
  const symbol = element.padEnd(3, ' ').slice(0, 3);
  lines[lineIdx] = line.slice(0, ATOM_SYMBOL_START) + symbol + line.slice(ATOM_SYMBOL_START + 3);
  return lines.join('\n');
}
