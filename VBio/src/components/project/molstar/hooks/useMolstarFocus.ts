import { useCallback } from 'react';
import { buildResidueLoci } from '../loci';
import { tryFocusLikelyLigand } from '../theme';

interface UseMolstarFocusArgs {
  format: 'cif' | 'pdb';
  structureText: string;
  overlayFormat?: 'cif' | 'pdb';
  overlayStructureText?: string;
  ligandFocusChainId?: string;
}

function parseCifTokens(line: string): string[] {
  const tokens = line.match(/(?:[^\s"']+|"[^"]*"|'[^']*')+/g) || [];
  return tokens.map((token) => token.replace(/^['"]|['"]$/g, ''));
}

function inferLigandAnchorFromPdb(
  pdbText: string,
  preferredChain: string
): { chain: string; residue: number; atom: string } | null {
  const preferred = String(preferredChain || '').trim();
  const lines = String(pdbText || '').split(/\r?\n/);
  let fallback: { chain: string; residue: number; atom: string } | null = null;
  for (const line of lines) {
    if (!line.startsWith('HETATM')) continue;
    const residueName = line.slice(17, 20).trim().toUpperCase();
    if (!residueName || residueName === 'HOH' || residueName === 'WAT') continue;
    const chain = line.slice(21, 22).trim();
    const residue = Number.parseInt(line.slice(22, 26).trim(), 10);
    const atom = line.slice(12, 16).trim();
    if (!chain || !Number.isFinite(residue) || residue <= 0 || !atom) continue;
    const current = { chain, residue, atom };
    if (preferred && chain === preferred) return current;
    if (!fallback) fallback = current;
  }
  return fallback;
}

function inferLigandAnchorFromCif(
  cifText: string,
  preferredChain: string
): { chain: string; residue: number; atom: string } | null {
  const preferred = String(preferredChain || '').trim();
  const lines = String(cifText || '').split(/\r?\n/);
  let fallback: { chain: string; residue: number; atom: string } | null = null;
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;
    const headers: string[] = [];
    let j = i + 1;
    while (j < lines.length && lines[j].trim().startsWith('_')) {
      headers.push(lines[j].trim());
      j += 1;
    }
    if (headers.length === 0 || !headers.some((item) => item.startsWith('_atom_site.'))) continue;
    const idx = (name: string) => headers.findIndex((item) => item === name);
    const groupIdx = idx('_atom_site.group_PDB');
    const authCompIdx = idx('_atom_site.auth_comp_id');
    const labelCompIdx = idx('_atom_site.label_comp_id');
    const authAsymIdx = idx('_atom_site.auth_asym_id');
    const labelAsymIdx = idx('_atom_site.label_asym_id');
    const authSeqIdx = idx('_atom_site.auth_seq_id');
    const labelSeqIdx = idx('_atom_site.label_seq_id');
    const authAtomIdx = idx('_atom_site.auth_atom_id');
    const labelAtomIdx = idx('_atom_site.label_atom_id');

    for (; j < lines.length; j += 1) {
      const raw = lines[j].trim();
      if (!raw) continue;
      if (raw === '#' || raw === 'loop_' || raw.startsWith('data_') || raw.startsWith('_')) break;
      const tokens = parseCifTokens(raw);
      if (tokens.length < headers.length) continue;
      const group = String(groupIdx >= 0 ? tokens[groupIdx] : 'HETATM').trim().toUpperCase();
      if (group && group !== 'HETATM') continue;
      const residueName = String((authCompIdx >= 0 ? tokens[authCompIdx] : tokens[labelCompIdx] || '') || '')
        .trim()
        .toUpperCase();
      if (!residueName || residueName === 'HOH' || residueName === 'WAT') continue;
      const chain = String((authAsymIdx >= 0 ? tokens[authAsymIdx] : tokens[labelAsymIdx] || '') || '').trim();
      const residueToken = String((authSeqIdx >= 0 ? tokens[authSeqIdx] : tokens[labelSeqIdx] || '') || '').trim();
      const atom = String((authAtomIdx >= 0 ? tokens[authAtomIdx] : tokens[labelAtomIdx] || '') || '').trim();
      const residue = Number.parseInt(residueToken, 10);
      if (!chain || !Number.isFinite(residue) || residue <= 0 || !atom || atom === '?') continue;
      const current = { chain, residue, atom };
      if (preferred && chain === preferred) return current;
      if (!fallback) fallback = current;
    }
  }
  return fallback;
}

function inferLigandAnchor(
  text: string,
  format: 'cif' | 'pdb',
  preferredChain: string
): { chain: string; residue: number; atom: string } | null {
  return format === 'pdb'
    ? inferLigandAnchorFromPdb(text, preferredChain)
    : inferLigandAnchorFromCif(text, preferredChain);
}

export function useMolstarFocus({
  format,
  structureText,
  overlayFormat,
  overlayStructureText,
  ligandFocusChainId = ''
}: UseMolstarFocusArgs) {
  return useCallback(
    (viewer: any): boolean => {
      const focusManager = viewer?.plugin?.managers?.structure?.focus;
      if (!focusManager?.setFromLoci) return false;
      const applyFocusLoci = (loci: any): boolean => {
        if (!loci) return false;
        try {
          focusManager.setFromLoci(loci);
        } catch {
          return false;
        }
        const cameraManager = viewer?.plugin?.managers?.camera;
        const focusFns = [cameraManager?.focusLoci, cameraManager?.focusRenderObjects].filter(
          (fn) => typeof fn === 'function'
        );
        for (const fn of focusFns) {
          try {
            fn.call(cameraManager, loci, { durationMs: 180, extraRadius: 4, minRadius: 6 });
            break;
          } catch {
            try {
              fn.call(cameraManager, loci);
              break;
            } catch {
              // try next focus API
            }
          }
        }
        return true;
      };
      const preferredChain = String(ligandFocusChainId || '').trim();
      const parsedAnchor =
        inferLigandAnchor(structureText, format, preferredChain) ||
        inferLigandAnchor(
          String(overlayStructureText || ''),
          overlayFormat === 'pdb' ? 'pdb' : format,
          preferredChain
        );
      if (parsedAnchor) {
        const loci = buildResidueLoci(viewer, parsedAnchor.chain, parsedAnchor.residue);
        if (applyFocusLoci(loci)) return true;
      }
      if (tryFocusLikelyLigand(viewer)) {
        const likelyLigandLoci =
          viewer?.plugin?.managers?.structure?.focus?.current?.loci ??
          viewer?.plugin?.managers?.structure?.focus?.current?.current?.loci;
        if (likelyLigandLoci) {
          return applyFocusLoci(likelyLigandLoci);
        }
      }
      return false;
    },
    [format, ligandFocusChainId, overlayFormat, overlayStructureText, structureText]
  );
}
