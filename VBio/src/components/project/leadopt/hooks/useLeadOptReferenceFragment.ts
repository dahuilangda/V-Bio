import { useEffect, useMemo, useRef, useState } from 'react';
import {
  previewLeadOptimizationFragments,
  previewLeadOptimizationReference
} from '../../../../api/backendApi';
import { type LigandFragmentItem } from '../../LigandFragmentSketcher';
import { type MolstarAtomHighlight, type MolstarResidueHighlight, type MolstarResiduePick } from '../../MolstarViewer';
import { resolveVariableSelection } from './fragmentVariableSelection';

type PocketResidue = {
  chain_id: string;
  residue_name: string;
  residue_number: number;
  min_distance?: number;
  interaction_types?: string[];
};

type LigandAtomContact = {
  atom_index: number;
  chain_id?: string;
  residue_name?: string;
  residue_number?: number;
  atom_name?: string;
  residues: PocketResidue[];
};

interface UseLeadOptReferenceFragmentParams {
  ligandSmiles: string;
  onLigandSmilesChange: (value: string) => void;
  currentVariableQuery: string;
  onAutoVariableQuery: (value: string) => void;
  onError: (message: string | null) => void;
  scopeKey?: string | null;
  persistedUploads?: LeadOptPersistedUploads;
  onPersistedUploadsChange?: (uploads: LeadOptPersistedUploads) => void;
  initialSelection?: {
    fragmentIds?: string[];
    atomIndices?: number[];
    variableQueries?: string[];
  } | null;
}

export interface LeadOptPersistedUpload {
  fileName: string;
  content: string;
}

export interface LeadOptPersistedUploads {
  target: LeadOptPersistedUpload | null;
  ligand: LeadOptPersistedUpload | null;
}

function readText(value: unknown): string {
  if (value === null || value === undefined) return '';
  return String(value);
}

function readNumber(value: unknown): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string') {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return 0;
}

function normalizeAtomName(value: string): string {
  return String(value || '').replace(/\s+/g, '').trim().toUpperCase();
}

function normalizeFragments(rows: unknown): LigandFragmentItem[] {
  if (!Array.isArray(rows)) return [];
  const result: LigandFragmentItem[] = [];
  rows.forEach((item) => {
    const row = (item as Record<string, unknown>) || {};
    const atomIndicesRaw = Array.isArray(row.atom_indices) ? row.atom_indices : [];
    const atomIndices = atomIndicesRaw
      .map((value) => Number(value))
      .filter((value) => Number.isFinite(value) && value >= 0)
      .map((value) => Math.floor(value));
    const fragmentId = readText(row.fragment_id);
    const displaySmiles = readText(row.display_smiles) || readText(row.smiles);
    const querySmiles = readText(row.query_smiles) || readText(row.smiles);
    if (!fragmentId || !displaySmiles) return;
    result.push({
      fragment_id: fragmentId,
      smiles: querySmiles,
      display_smiles: displaySmiles,
      atom_indices: atomIndices,
      heavy_atoms: readNumber(row.heavy_atoms),
      attachment_count: readNumber(row.attachment_count),
      num_frags: readNumber(row.num_frags || row.attachment_count || 0),
      recommended_action: readText(row.recommended_action) || 'unassigned',
      color: readText(row.color) || '#95a5a6',
      rule_coverage: readNumber(row.rule_coverage),
      quality_score: readNumber(row.quality_score)
    });
  });
  return result;
}

function normalizePocketResidues(rows: unknown): PocketResidue[] {
  if (!Array.isArray(rows)) return [];
  const result: PocketResidue[] = [];
  rows.forEach((item) => {
    const row = (item as Record<string, unknown>) || {};
    const chainId = readText(row.chain_id);
    const residueNumber = Math.floor(readNumber(row.residue_number));
    if (!chainId || residueNumber <= 0) return;
    result.push({
      chain_id: chainId,
      residue_name: readText(row.residue_name),
      residue_number: residueNumber,
      min_distance: readNumber(row.min_distance),
      interaction_types: Array.isArray(row.interaction_types)
        ? row.interaction_types.map((x) => readText(x)).filter(Boolean)
        : []
    });
  });
  return result;
}

function normalizeAtomContacts(rows: unknown): LigandAtomContact[] {
  if (!Array.isArray(rows)) return [];
  const result: LigandAtomContact[] = [];
  rows.forEach((item) => {
    const row = (item as Record<string, unknown>) || {};
    const atomIndex = Math.floor(readNumber(row.atom_index));
    if (atomIndex < 0) return;
    const residues = normalizePocketResidues(row.residues);
    result.push({
      atom_index: atomIndex,
      chain_id: readText(row.chain_id),
      residue_name: readText(row.residue_name),
      residue_number: Math.floor(readNumber(row.residue_number)),
      atom_name: readText(row.atom_name),
      residues
    });
  });
  return result;
}

function normalizeAtomMap(rows: unknown): LigandAtomContact[] {
  if (!Array.isArray(rows)) return [];
  const result: LigandAtomContact[] = [];
  rows.forEach((item) => {
    const row = (item as Record<string, unknown>) || {};
    const atomIndex = Math.floor(readNumber(row.atom_index));
    if (atomIndex < 0) return;
    result.push({
      atom_index: atomIndex,
      chain_id: readText(row.chain_id),
      residue_name: readText(row.residue_name),
      residue_number: Math.floor(readNumber(row.residue_number)),
      atom_name: readText(row.atom_name),
      residues: []
    });
  });
  return result;
}

function pickReasonableDefaultFragment(fragments: LigandFragmentItem[]): string {
  if (fragments.length === 0) return '';
  const sorted = [...fragments].sort((a, b) => {
    const aAttach = a.attachment_count || 0;
    const bAttach = b.attachment_count || 0;
    const aScore = (a.quality_score || 0) + (a.rule_coverage || 0) * 0.35 + aAttach * 0.7;
    const bScore = (b.quality_score || 0) + (b.rule_coverage || 0) * 0.35 + bAttach * 0.7;
    if (bScore !== aScore) return bScore - aScore;
    return (a.heavy_atoms || 0) - (b.heavy_atoms || 0);
  });
  return sorted[0]?.fragment_id || '';
}

function uniqueFragmentIds(values: string[]): string[] {
  const seen = new Set<string>();
  const result: string[] = [];
  values.forEach((value) => {
    const next = String(value || '').trim();
    if (!next || seen.has(next)) return;
    seen.add(next);
    result.push(next);
  });
  return result;
}

export function useLeadOptReferenceFragment({
  ligandSmiles,
  onLigandSmilesChange,
  currentVariableQuery,
  onAutoVariableQuery,
  onError,
  scopeKey,
  persistedUploads,
  onPersistedUploadsChange,
  initialSelection
}: UseLeadOptReferenceFragmentParams) {
  const [busyCount, setBusyCount] = useState(0);
  const [referenceTargetFile, setReferenceTargetFile] = useState<File | null>(null);
  const [referenceLigandFile, setReferenceLigandFile] = useState<File | null>(null);
  const [persistedTargetUpload, setPersistedTargetUpload] = useState<LeadOptPersistedUpload | null>(null);
  const [persistedLigandUpload, setPersistedLigandUpload] = useState<LeadOptPersistedUpload | null>(null);
  const [pocketResidues, setPocketResidues] = useState<PocketResidue[]>([]);
  const [ligandAtomContacts, setLigandAtomContacts] = useState<LigandAtomContact[]>([]);
  const [referenceReady, setReferenceReady] = useState(false);
  const [targetChainSequences, setTargetChainSequences] = useState<Record<string, string>>({});
  const [referenceTargetChainId, setReferenceTargetChainId] = useState('');
  const [referenceLigandChainId, setReferenceLigandChainId] = useState('');

  const [previewStructureText, setPreviewStructureText] = useState('');
  const [previewStructureFormat, setPreviewStructureFormat] = useState<'cif' | 'pdb'>('cif');
  const [previewOverlayStructureText, setPreviewOverlayStructureText] = useState('');
  const [previewOverlayStructureFormat, setPreviewOverlayStructureFormat] = useState<'cif' | 'pdb'>('cif');
  const [referenceLigandSmilesResolved, setReferenceLigandSmilesResolved] = useState('');
  const [fragmentSourceSmiles, setFragmentSourceSmiles] = useState('');

  const [fragments, setFragments] = useState<LigandFragmentItem[]>([]);
  const [activeFragmentId, setActiveFragmentId] = useState('');
  const [selectedFragmentIds, setSelectedFragmentIds] = useState<string[]>([]);
  const hydratedUploadKeyRef = useRef('');
  const hydratedSelectionKeyRef = useRef('');

  const busy = busyCount > 0;
  const beginBusy = () => setBusyCount((prev) => prev + 1);
  const endBusy = () => setBusyCount((prev) => Math.max(0, prev - 1));

  const effectiveLigandSmiles = useMemo(() => {
    const primary = ligandSmiles.trim();
    if (primary) return primary;
    return referenceLigandSmilesResolved.trim();
  }, [ligandSmiles, referenceLigandSmilesResolved]);

  const uploadHydrationKey = useMemo(() => {
    const targetName = String(persistedUploads?.target?.fileName || '').trim();
    const targetContent = String(persistedUploads?.target?.content || '');
    const ligandName = String(persistedUploads?.ligand?.fileName || '').trim();
    const ligandContent = String(persistedUploads?.ligand?.content || '');
    return `${String(scopeKey || '')}|${targetName}:${targetContent.length}|${ligandName}:${ligandContent.length}`;
  }, [persistedUploads, scopeKey]);

  useEffect(() => {
    const next = ligandSmiles.trim();
    if (!next) return;
    setReferenceLigandSmilesResolved(next);
  }, [ligandSmiles]);

  useEffect(() => {
    hydratedUploadKeyRef.current = '';
    hydratedSelectionKeyRef.current = '';
    const targetName = String(persistedUploads?.target?.fileName || '').trim();
    const targetContent = String(persistedUploads?.target?.content || '').trim();
    if (targetName && targetContent) return;
    setReferenceTargetFile(null);
    setReferenceLigandFile(null);
    setPersistedTargetUpload(null);
    setPersistedLigandUpload(null);
    setPocketResidues([]);
    setLigandAtomContacts([]);
    setReferenceReady(false);
    setTargetChainSequences({});
    setReferenceTargetChainId('');
    setReferenceLigandChainId('');
    setPreviewStructureText('');
    setPreviewOverlayStructureText('');
    setFragments([]);
    setActiveFragmentId('');
    setSelectedFragmentIds([]);
  }, [scopeKey]);

  const initialSelectionKey = useMemo(() => {
    const ids = Array.isArray(initialSelection?.fragmentIds) ? initialSelection?.fragmentIds : [];
    const atoms = Array.isArray(initialSelection?.atomIndices) ? initialSelection?.atomIndices : [];
    const queries = Array.isArray(initialSelection?.variableQueries) ? initialSelection?.variableQueries : [];
    return `${String(scopeKey || '')}|${ids.join(',')}|${atoms.join(',')}|${queries.join(';;')}`;
  }, [initialSelection?.atomIndices, initialSelection?.fragmentIds, initialSelection?.variableQueries, scopeKey]);

  useEffect(() => {
    if (typeof onPersistedUploadsChange !== 'function') return;
    onPersistedUploadsChange({
      target: persistedTargetUpload ? { ...persistedTargetUpload } : null,
      ligand: persistedLigandUpload ? { ...persistedLigandUpload } : null
    });
  }, [onPersistedUploadsChange, persistedLigandUpload, persistedTargetUpload]);

  const fragmentById = useMemo(() => {
    const map = new Map<string, LigandFragmentItem>();
    fragments.forEach((item) => map.set(item.fragment_id, item));
    return map;
  }, [fragments]);

  const atomToFragmentId = useMemo(() => {
    const map = new Map<number, string>();
    const ranked = [...fragments].sort((a, b) => {
      const aAtoms = Array.isArray(a.atom_indices) ? a.atom_indices.length : 0;
      const bAtoms = Array.isArray(b.atom_indices) ? b.atom_indices.length : 0;
      if (aAtoms !== bAtoms) return aAtoms - bAtoms;
      const aAttach = Number(a.attachment_count || 0);
      const bAttach = Number(b.attachment_count || 0);
      if (bAttach !== aAttach) return bAttach - aAttach;
      return String(a.fragment_id || '').localeCompare(String(b.fragment_id || ''));
    });
    ranked.forEach((fragment) => {
      const fragmentId = String(fragment.fragment_id || '').trim();
      if (!fragmentId) return;
      fragment.atom_indices.forEach((atomIndexRaw) => {
        const atomIndex = Number(atomIndexRaw);
        if (!Number.isFinite(atomIndex) || atomIndex < 0) return;
        if (!map.has(atomIndex)) {
          map.set(atomIndex, fragmentId);
        }
      });
    });
    return map;
  }, [fragments]);

  const activeFragment = useMemo(() => fragmentById.get(activeFragmentId) || null, [fragmentById, activeFragmentId]);

  const selectedFragmentItems = useMemo(
    () =>
      selectedFragmentIds
        .map((fragmentId) => fragmentById.get(fragmentId))
        .filter((item): item is LigandFragmentItem => Boolean(item)),
    [selectedFragmentIds, fragmentById]
  );

  useEffect(() => {
    if (!initialSelection) return;
    if (fragments.length === 0) return;
    if (hydratedSelectionKeyRef.current === initialSelectionKey) return;

    const requestedIds = uniqueFragmentIds(Array.isArray(initialSelection.fragmentIds) ? initialSelection.fragmentIds : []);
    const variableQueries = Array.isArray(initialSelection.variableQueries)
      ? initialSelection.variableQueries.map((value) => readText(value).trim()).filter(Boolean)
      : [];
    const selectedAtoms = Array.isArray(initialSelection.atomIndices)
      ? Array.from(
          new Set(
            initialSelection.atomIndices
              .map((value) => Number(value))
              .filter((value) => Number.isFinite(value) && value >= 0)
              .map((value) => Math.floor(value))
          )
        )
      : [];

    let nextIds = requestedIds.filter((id) => fragmentById.has(id));

    if (nextIds.length === 0 && selectedAtoms.length > 0) {
      nextIds = [...fragments]
        .map((fragment) => {
          const overlap = fragment.atom_indices.filter((atomIndex) => selectedAtoms.includes(atomIndex)).length;
          return { fragmentId: fragment.fragment_id, overlap };
        })
        .filter((item) => item.overlap > 0)
        .sort((a, b) => b.overlap - a.overlap)
        .map((item) => item.fragmentId);
    }

    if (nextIds.length === 0 && variableQueries.length > 0) {
      const querySet = new Set(variableQueries);
      nextIds = fragments
        .filter((fragment) => {
          const query = readText(fragment.smiles).trim();
          const display = readText(fragment.display_smiles).trim();
          return querySet.has(query) || querySet.has(display);
        })
        .map((fragment) => fragment.fragment_id);
    }

    if (nextIds.length === 0) return;
    const normalized = uniqueFragmentIds(nextIds).slice(0, 6);
    if (normalized.length === 0) return;
    hydratedSelectionKeyRef.current = initialSelectionKey;
    setActiveFragmentId(normalized[0]);
    setSelectedFragmentIds(normalized);
  }, [fragmentById, fragments, initialSelection, initialSelectionKey]);

  const resolvedVariableSelection = useMemo(
    () => resolveVariableSelection(selectedFragmentItems, fragments),
    [selectedFragmentItems, fragments]
  );

  const selectedFragmentSmiles = useMemo(
    () => resolvedVariableSelection.variableSmilesList,
    [resolvedVariableSelection]
  );

  useEffect(() => {
    const fragmentDrivenQuery = selectedFragmentSmiles.join(';;') || activeFragment?.smiles || '';
    if (!fragmentDrivenQuery) return;
    onAutoVariableQuery(fragmentDrivenQuery);
  }, [activeFragment?.smiles, onAutoVariableQuery, selectedFragmentSmiles]);

  const atomContactDetailMap = useMemo(() => {
    const map = new Map<number, LigandAtomContact>();
    ligandAtomContacts.forEach((contact) => {
      map.set(contact.atom_index, contact);
    });
    return map;
  }, [ligandAtomContacts]);

  const ligandAtomsByOrdinal = useMemo(() => {
    return ligandAtomContacts
      .filter((item) => Math.floor(readNumber(item.atom_index)) >= 0)
      .slice()
      .sort((a, b) => Math.floor(readNumber(a.atom_index)) - Math.floor(readNumber(b.atom_index)));
  }, [ligandAtomContacts]);

  const ligandAtomPickMap = useMemo(() => {
    const map = new Map<string, number>();
    ligandAtomContacts.forEach((contact) => {
      const chainId = readText(contact.chain_id).trim();
      const residueNumber = Math.floor(readNumber(contact.residue_number));
      const atomName = normalizeAtomName(readText(contact.atom_name));
      if (!chainId || residueNumber <= 0 || !atomName) return;
      map.set(`${chainId}:${residueNumber}:${atomName}`, contact.atom_index);
    });
    return map;
  }, [ligandAtomContacts]);

  const highlightedLigandAtoms = useMemo(() => {
    const dedup = new Map<string, MolstarAtomHighlight>();
    const sourceFragments = activeFragment
      ? [activeFragment]
      : selectedFragmentIds.length > 0
        ? selectedFragmentIds.map((id) => fragmentById.get(id)).filter((item): item is LigandFragmentItem => Boolean(item))
        : [];
    sourceFragments.forEach((fragment) => {
      fragment.atom_indices.forEach((atomIndex) => {
        const contact = atomContactDetailMap.get(atomIndex) || ligandAtomsByOrdinal[atomIndex] || null;
        if (!contact) return;
        const chainId = readText(contact.chain_id).trim();
        const residueNumber = Math.floor(readNumber(contact.residue_number));
        const atomName = readText(contact.atom_name).trim();
        if (!chainId || residueNumber <= 0 || !atomName) return;
        const key = `${chainId}:${residueNumber}:${atomName}`;
        if (!dedup.has(key)) {
          dedup.set(key, {
            chainId,
            residue: residueNumber,
            atomName,
            emphasis: 'default'
          });
        }
      });
    });
    const rows = Array.from(dedup.values());
    if (rows.length > 0) rows[0] = { ...rows[0], emphasis: 'active' };
    return rows;
  }, [activeFragment, atomContactDetailMap, fragmentById, ligandAtomsByOrdinal, selectedFragmentIds]);

  const defaultLigandFocusAtom = useMemo(() => {
    for (const atom of ligandAtomContacts) {
      const chainId = readText(atom.chain_id).trim();
      const residueNumber = Math.floor(readNumber(atom.residue_number));
      const atomName = readText(atom.atom_name).trim();
      if (!chainId || residueNumber <= 0 || !atomName) continue;
      return {
        chainId,
        residue: residueNumber,
        atomName,
        emphasis: 'active' as const
      };
    }
    return null;
  }, [ligandAtomContacts]);

  const activeMolstarAtom = highlightedLigandAtoms.length > 0 ? highlightedLigandAtoms[0] : defaultLigandFocusAtom;
  const ligandAtomContactCount = useMemo(
    () => ligandAtomContacts.filter((item) => (item.residues || []).length > 0).length,
    [ligandAtomContacts]
  );

  const selectedPocketResidues = useMemo(() => {
    const dedup = new Map<string, { chain_id: string; residue_number: number; min_distance: number }>();
    const sourceFragments = activeFragment
      ? [activeFragment]
      : selectedFragmentIds.length > 0
        ? selectedFragmentIds.map((id) => fragmentById.get(id)).filter((item): item is LigandFragmentItem => Boolean(item))
        : [];
    sourceFragments.forEach((fragment) => {
      fragment.atom_indices.forEach((atomIndex) => {
        const contact = atomContactDetailMap.get(atomIndex) || ligandAtomsByOrdinal[atomIndex] || null;
        if (!contact) return;
        (contact.residues || []).forEach((residue) => {
          const chainId = readText(residue.chain_id).trim();
          const residueNumber = Math.floor(readNumber(residue.residue_number));
          if (!chainId || residueNumber <= 0) return;
          const minDistance = readNumber(residue.min_distance) || 99;
          const key = `${chainId}:${residueNumber}`;
          const prev = dedup.get(key);
          if (!prev || minDistance < prev.min_distance) {
            dedup.set(key, { chain_id: chainId, residue_number: residueNumber, min_distance: minDistance });
          }
        });
      });
    });
    return Array.from(dedup.values())
      .sort((a, b) => a.min_distance - b.min_distance)
      .slice(0, 48)
      .map((item) => ({ chain_id: item.chain_id, residue_number: item.residue_number }));
  }, [activeFragment, atomContactDetailMap, fragmentById, ligandAtomsByOrdinal, selectedFragmentIds]);

  const highlightedPocketResidues = useMemo<MolstarResidueHighlight[]>(() => {
    return selectedPocketResidues.map((item, index) => ({
      chainId: String(item.chain_id || '').trim(),
      residue: Math.floor(Number(item.residue_number) || 0),
      emphasis: index === 0 ? 'active' : 'default'
    }));
  }, [selectedPocketResidues]);

  const fetchFragmentPreview = async (smilesValue: string) => {
    const response = await previewLeadOptimizationFragments(smilesValue.trim());
    setFragmentSourceSmiles(readText(response.smiles).trim() || smilesValue.trim());
    const nextFragments = normalizeFragments(response.fragments);
    setFragments(nextFragments);
    const recommendedIds = Array.isArray(response.recommended_variable_fragment_ids)
      ? response.recommended_variable_fragment_ids.map((id) => readText(id)).filter(Boolean)
      : [];
    const defaultFragmentId = pickReasonableDefaultFragment(nextFragments);
    const defaultIds = uniqueFragmentIds([
      ...recommendedIds.slice(0, 1),
      defaultFragmentId,
      nextFragments[0]?.fragment_id || ''
    ]).slice(0, 1);
    const firstRecommended = defaultIds[0] || '';
    if (firstRecommended) setActiveFragmentId(firstRecommended);
    setSelectedFragmentIds(defaultIds);
    if (!currentVariableQuery.trim() && response.auto_generated_rules?.variable_smarts) {
      const first = response.auto_generated_rules.variable_smarts.split(';;')[0] || '';
      if (first) onAutoVariableQuery(first);
    }
  };

  const runFragmentPreview = async () => {
    const smilesValue = effectiveLigandSmiles;
    if (!smilesValue) {
      onError('Upload reference and confirm ligand SMILES first.');
      return;
    }
    onError(null);
    beginBusy();
    try {
      await fetchFragmentPreview(smilesValue);
    } catch (e) {
      onError(e instanceof Error ? e.message : 'Fragment preview failed.');
    } finally {
      endBusy();
    }
  };

  const runReferencePreviewWithFiles = async (targetFile: File | null, ligandFile: File | null) => {
    if (!targetFile || !ligandFile) return;
    onError(null);
    beginBusy();
    try {
      const response = await previewLeadOptimizationReference(targetFile, ligandFile);
      const nextPocket = normalizePocketResidues(response.pocket_residues);
      const nextTargetChainSequences = (() => {
        const out: Record<string, string> = {};
        const raw = response.target_chain_sequences;
        if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return out;
        for (const [chainIdRaw, seqRaw] of Object.entries(raw as Record<string, unknown>)) {
          const chainId = readText(chainIdRaw).trim();
          const sequence = readText(seqRaw).replace(/\s+/g, '').trim();
          if (!chainId || !sequence) continue;
          out[chainId] = sequence;
        }
        return out;
      })();
      const responseTargetChainIds = Array.isArray(response.target_chain_ids)
        ? response.target_chain_ids.map((value) => readText(value).trim()).filter(Boolean)
        : [];
      const responseLigandChains = readText(response.ligand_chain_id)
        .split(',')
        .map((value) => value.trim())
        .filter(Boolean);
      const resolvedLigandChain = responseLigandChains[0] || '';
      const residueCountByChain = new Map<string, number>();
      nextPocket.forEach((item) => {
        const chainId = readText(item.chain_id).trim();
        if (!chainId) return;
        residueCountByChain.set(chainId, (residueCountByChain.get(chainId) || 0) + 1);
      });
      const rankedPocketChains = Array.from(residueCountByChain.entries())
        .sort((a, b) => b[1] - a[1])
        .map((entry) => entry[0]);
      const sequenceChains = Object.keys(nextTargetChainSequences);
      const resolvedTargetChain =
        rankedPocketChains.find((chainId) => chainId !== resolvedLigandChain) ||
        responseTargetChainIds.find((chainId) => chainId !== resolvedLigandChain) ||
        sequenceChains.find((chainId) => chainId !== resolvedLigandChain) ||
        responseTargetChainIds[0] ||
        sequenceChains[0] ||
        '';
      const mappedAtoms = normalizeAtomMap(response.ligand_atom_map);
      const contactAtoms = normalizeAtomContacts(response.ligand_atom_contacts);
      const mergedAtomMap = new Map<number, LigandAtomContact>();
      mappedAtoms.forEach((item) => {
        mergedAtomMap.set(item.atom_index, item);
      });
      contactAtoms.forEach((item) => {
        const prev = mergedAtomMap.get(item.atom_index);
        mergedAtomMap.set(item.atom_index, {
          atom_index: item.atom_index,
          chain_id: readText(item.chain_id) || readText(prev?.chain_id),
          residue_name: readText(item.residue_name) || readText(prev?.residue_name),
          residue_number: Math.floor(readNumber(item.residue_number)) || Math.floor(readNumber(prev?.residue_number)),
          atom_name: readText(item.atom_name) || readText(prev?.atom_name),
          residues: item.residues
        });
      });
      setPocketResidues(nextPocket);
      setTargetChainSequences(nextTargetChainSequences);
      setReferenceTargetChainId(resolvedTargetChain);
      setReferenceLigandChainId(resolvedLigandChain);
      setLigandAtomContacts(Array.from(mergedAtomMap.values()).sort((a, b) => a.atom_index - b.atom_index));

      const structureText = readText(response.structure_text);
      const structureFormat = readText(response.structure_format).toLowerCase() === 'pdb' ? 'pdb' : 'cif';
      const overlayText = readText(response.overlay_structure_text);
      const overlayFormat = readText(response.overlay_structure_format).toLowerCase() === 'pdb' ? 'pdb' : 'cif';
      setPreviewStructureText(structureText);
      setPreviewStructureFormat(structureFormat);
      setPreviewOverlayStructureText(overlayText);
      setPreviewOverlayStructureFormat(overlayFormat);
      setReferenceReady(true);

      const referenceSmiles = readText(response.ligand_smiles).trim();
      if (referenceSmiles) setReferenceLigandSmilesResolved(referenceSmiles);
      const nextSmiles = referenceSmiles || effectiveLigandSmiles;
      if (referenceSmiles && referenceSmiles !== ligandSmiles.trim()) {
        onLigandSmilesChange(referenceSmiles);
      }
      if (nextSmiles) {
        await fetchFragmentPreview(nextSmiles);
      } else {
        onError('Reference ligand uploaded but no small-molecule SMILES could be resolved. Please use SDF/MOL2 or input SMILES.');
      }
    } catch (e) {
      setReferenceReady(false);
      setTargetChainSequences({});
      setReferenceTargetChainId('');
      setReferenceLigandChainId('');
      onError(e instanceof Error ? e.message : 'Reference preview failed.');
    } finally {
      endBusy();
    }
  };

  const handleTargetFileChange = async (file: File | null) => {
    setReferenceTargetFile(file);
    if (!file) {
      setPersistedTargetUpload(null);
      setPersistedLigandUpload(null);
      setReferenceLigandFile(null);
      setPocketResidues([]);
      setLigandAtomContacts([]);
      setReferenceReady(false);
      setTargetChainSequences({});
      setReferenceTargetChainId('');
      setReferenceLigandChainId('');
      setPreviewStructureText('');
      setPreviewOverlayStructureText('');
      setFragments([]);
      setActiveFragmentId('');
      setSelectedFragmentIds([]);
      return;
    }
    const targetText = await file
      .text()
      .then((text) => text)
      .catch(() => '');
    setPersistedTargetUpload({
      fileName: file.name,
      content: targetText
    });
    await runReferencePreviewWithFiles(file, referenceLigandFile);
  };

  const handleLigandFileChange = async (file: File | null) => {
    setReferenceLigandFile(file);
    if (!file) {
      setPersistedLigandUpload(null);
      setPocketResidues([]);
      setLigandAtomContacts([]);
      setReferenceReady(false);
      setTargetChainSequences({});
      setReferenceTargetChainId('');
      setReferenceLigandChainId('');
      setPreviewOverlayStructureText('');
      setFragments([]);
      setActiveFragmentId('');
      setSelectedFragmentIds([]);
      return;
    }
    const ligandText = await file
      .text()
      .then((text) => text)
      .catch(() => '');
    setPersistedLigandUpload({
      fileName: file.name,
      content: ligandText
    });
    await runReferencePreviewWithFiles(referenceTargetFile, file);
  };

  useEffect(() => {
    const targetName = String(persistedUploads?.target?.fileName || '').trim();
    const targetContent = String(persistedUploads?.target?.content || '').trim();
    if (!targetName || !targetContent) return;
    if (hydratedUploadKeyRef.current === uploadHydrationKey) return;
    hydratedUploadKeyRef.current = uploadHydrationKey;
    const restoredTarget = new File([targetContent], targetName, { type: 'text/plain' });
    const ligandName = String(persistedUploads?.ligand?.fileName || '').trim();
    const ligandContent = String(persistedUploads?.ligand?.content || '').trim();
    const restoredLigand =
      ligandName && ligandContent ? new File([ligandContent], ligandName, { type: 'text/plain' }) : null;
    setReferenceTargetFile(restoredTarget);
    setReferenceLigandFile(restoredLigand);
    setPersistedTargetUpload({ fileName: targetName, content: targetContent });
    setPersistedLigandUpload(restoredLigand ? { fileName: ligandName, content: ligandContent } : null);
    void runReferencePreviewWithFiles(restoredTarget, restoredLigand);
  }, [persistedUploads, uploadHydrationKey]);

  const toggleFragmentSelection = (fragmentId: string, options?: { additive?: boolean }) => {
    if (!fragmentId) return;
    const additive = Boolean(options?.additive);
    setActiveFragmentId(fragmentId);
    setSelectedFragmentIds((prev) => {
      if (!additive) return [fragmentId];
      const exists = prev.includes(fragmentId);
      if (exists) {
        const next = prev.filter((item) => item !== fragmentId);
        return next.length > 0 ? next : [fragmentId];
      }
      return uniqueFragmentIds([...prev, fragmentId]).slice(0, 6);
    });
  };

  const clearFragmentSelection = () => {
    setActiveFragmentId('');
    setSelectedFragmentIds([]);
    onAutoVariableQuery('');
  };

  const handleFragmentAtomClick = (
    atomIndex: number,
    options?: { additive?: boolean; preferredFragmentId?: string }
  ) => {
    const preferredFragmentId = String(options?.preferredFragmentId || '').trim();
    if (preferredFragmentId && fragmentById.has(preferredFragmentId)) {
      toggleFragmentSelection(preferredFragmentId, options);
      return;
    }
    const mappedFragmentId = atomToFragmentId.get(atomIndex);
    if (mappedFragmentId && fragmentById.has(mappedFragmentId)) {
      toggleFragmentSelection(mappedFragmentId, options);
      return;
    }
    const candidates = fragments.filter((fragment) => fragment.atom_indices.includes(atomIndex));
    if (candidates.length === 0) return;
    const selectedSet = new Set(selectedFragmentIds);
    candidates.sort((a, b) => {
      const aActive = a.fragment_id === activeFragmentId ? 1 : 0;
      const bActive = b.fragment_id === activeFragmentId ? 1 : 0;
      if (bActive !== aActive) return bActive - aActive;
      const aSelected = selectedSet.has(a.fragment_id) ? 1 : 0;
      const bSelected = selectedSet.has(b.fragment_id) ? 1 : 0;
      if (bSelected !== aSelected) return bSelected - aSelected;
      if (b.quality_score !== a.quality_score) return b.quality_score - a.quality_score;
      return a.heavy_atoms - b.heavy_atoms;
    });
    toggleFragmentSelection(candidates[0].fragment_id, options);
  };

  const handleMolstarResiduePick = (pick: MolstarResiduePick) => {
    const chainId = String(pick.chainId || '').trim();
    const residueNumber = Math.floor(Number(pick.residue));
    const atomName = normalizeAtomName(String(pick.atomName || ''));
    if (!chainId || residueNumber <= 0 || !atomName) return;
    const atomIndex = ligandAtomPickMap.get(`${chainId}:${residueNumber}:${atomName}`);
    if (typeof atomIndex !== 'number' || atomIndex < 0) return;
    handleFragmentAtomClick(atomIndex);
  };

  return {
    busy,
    referenceTargetFile,
    referenceLigandFile,
    persistedUploads: {
      target: persistedTargetUpload ? { ...persistedTargetUpload } : null,
      ligand: persistedLigandUpload ? { ...persistedLigandUpload } : null
    } as LeadOptPersistedUploads,
    pocketResidues,
    targetChainSequences,
    referenceTargetChainId,
    referenceLigandChainId,
    ligandAtomContacts,
    referenceReady,
    previewStructureText,
    previewStructureFormat,
    previewOverlayStructureText,
    previewOverlayStructureFormat,
    effectiveLigandSmiles,
    fragments,
    activeFragmentId,
    activeFragment,
    selectedFragmentIds,
    selectedFragmentSmiles,
    highlightedLigandAtoms,
    highlightedPocketResidues,
    activeMolstarAtom,
    ligandAtomContactCount,
    fragmentSourceSmiles,
    runFragmentPreview,
    handleTargetFileChange,
    handleLigandFileChange,
    handleMolstarResiduePick,
    handleFragmentAtomClick,
    toggleFragmentSelection,
    clearFragmentSelection
  };
}

export type { LigandAtomContact, PocketResidue };
