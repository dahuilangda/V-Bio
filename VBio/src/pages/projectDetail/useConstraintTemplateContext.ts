import { useCallback, useEffect, useMemo } from 'react';
import type { Dispatch, SetStateAction } from 'react';
import type { InputComponent, PredictionConstraint, ProteinTemplateUpload } from '../../types/models';
import type { MolstarResidueHighlight } from '../../components/project/MolstarViewer';
import { extractProteinChainResidueIndexMap } from '../../utils/structureParser';
import { listConstraintResidues } from './projectDraftUtils';

type ChainInfoLite = {
  id: string;
  componentId: string;
  type: InputComponent['type'];
};

type ConstraintDraftLike = {
  inputConfig: {
    components: InputComponent[];
    constraints: PredictionConstraint[];
  };
};

type ConstraintTemplateOption = {
  componentId: string;
  label: string;
  fileName: string;
  format: 'cif' | 'pdb';
  chainId: string;
  content: string;
};

interface UseConstraintTemplateContextInput {
  draft: ConstraintDraftLike | null;
  proteinTemplates: Record<string, ProteinTemplateUpload>;
  selectedConstraintTemplateComponentId: string | null;
  setSelectedConstraintTemplateComponentId: Dispatch<SetStateAction<string | null>>;
  activeConstraintId: string | null;
  selectedContactConstraintIds: string[];
  chainInfoById: Map<string, ChainInfoLite>;
  activeChainInfos: ChainInfoLite[];
}

interface UseConstraintTemplateContextResult {
  constraintTemplateOptions: ConstraintTemplateOption[] | null;
  selectedTemplatePreview: ConstraintTemplateOption | null;
  selectedTemplateResidueIndexMap: Record<string, Record<number, number>>;
  selectedTemplateSequenceToStructureResidueMap: Record<number, number>;
  resolveTemplateComponentIdForConstraint: (constraint: PredictionConstraint | null | undefined) => string | null;
  constraintHighlightResidues: MolstarResidueHighlight[];
  constraintViewerHighlightResidues: MolstarResidueHighlight[];
  constraintViewerActiveResidue: MolstarResidueHighlight | null;
}

export function useConstraintTemplateContext({
  draft,
  proteinTemplates,
  selectedConstraintTemplateComponentId,
  setSelectedConstraintTemplateComponentId,
  activeConstraintId,
  selectedContactConstraintIds,
  chainInfoById,
  activeChainInfos
}: UseConstraintTemplateContextInput): UseConstraintTemplateContextResult {
  const constraintTemplateOptions = useMemo(() => {
    if (!draft) return null;
    let proteinOrder = 0;
    const options: ConstraintTemplateOption[] = [];
    for (const component of draft.inputConfig.components) {
      if (component.type !== 'protein') continue;
      proteinOrder += 1;
      const upload = proteinTemplates[component.id];
      if (!upload) continue;
      options.push({
        componentId: component.id,
        label: `Protein ${proteinOrder}`,
        fileName: upload.fileName,
        format: upload.format,
        chainId: upload.chainId,
        content: upload.content
      });
    }
    return options;
  }, [draft, proteinTemplates]);

  const selectedTemplatePreview = useMemo(() => {
    if (!constraintTemplateOptions || constraintTemplateOptions.length === 0) return null;
    if (selectedConstraintTemplateComponentId) {
      const selected = constraintTemplateOptions.find((item) => item.componentId === selectedConstraintTemplateComponentId);
      if (selected) return selected;
    }
    return constraintTemplateOptions[0];
  }, [constraintTemplateOptions, selectedConstraintTemplateComponentId]);

  const selectedTemplateResidueIndexMap = useMemo(() => {
    if (!selectedTemplatePreview) return {};
    try {
      return extractProteinChainResidueIndexMap(selectedTemplatePreview.content, selectedTemplatePreview.format);
    } catch {
      return {};
    }
  }, [selectedTemplatePreview]);

  const selectedTemplateSequenceToStructureResidueMap = useMemo<Record<number, number>>(() => {
    if (!selectedTemplatePreview) return {};
    const templateChainId = String(selectedTemplatePreview.chainId || '').trim();
    if (!templateChainId) return {};
    const forwardMap = selectedTemplateResidueIndexMap[templateChainId] || {};
    const inverseMap: Record<number, number> = {};

    for (const [structureResidueRaw, sequenceResidueRaw] of Object.entries(forwardMap)) {
      const structureResidue = Math.floor(Number(structureResidueRaw));
      const sequenceResidue = Math.floor(Number(sequenceResidueRaw));
      if (!Number.isFinite(structureResidue) || structureResidue <= 0) continue;
      if (!Number.isFinite(sequenceResidue) || sequenceResidue <= 0) continue;
      if (inverseMap[sequenceResidue] === undefined || structureResidue < inverseMap[sequenceResidue]) {
        inverseMap[sequenceResidue] = structureResidue;
      }
    }

    return inverseMap;
  }, [selectedTemplatePreview, selectedTemplateResidueIndexMap]);

  useEffect(() => {
    const ids = (constraintTemplateOptions || []).map((item) => item.componentId);
    if (ids.length === 0) {
      if (selectedConstraintTemplateComponentId !== null) {
        setSelectedConstraintTemplateComponentId(null);
      }
      return;
    }
    if (!selectedConstraintTemplateComponentId || !ids.includes(selectedConstraintTemplateComponentId)) {
      setSelectedConstraintTemplateComponentId(ids[0]);
    }
  }, [constraintTemplateOptions, selectedConstraintTemplateComponentId, setSelectedConstraintTemplateComponentId]);

  const constraintTemplateComponentIdSet = useMemo(() => {
    return new Set((constraintTemplateOptions || []).map((item) => item.componentId));
  }, [constraintTemplateOptions]);

  const resolveTemplateComponentIdForConstraint = useCallback(
    (constraint: PredictionConstraint | null | undefined): string | null => {
      if (!constraint) return null;
      for (const residueRef of listConstraintResidues(constraint)) {
        const chainId = String(residueRef.chainId || '').trim();
        if (!chainId) continue;
        const info = chainInfoById.get(chainId);
        if (!info || info.type !== 'protein') continue;
        if (constraintTemplateComponentIdSet.has(info.componentId)) {
          return info.componentId;
        }
      }
      return null;
    },
    [chainInfoById, constraintTemplateComponentIdSet]
  );

  useEffect(() => {
    if (!draft) return;
    const constraintsById = new Map(draft.inputConfig.constraints.map((item) => [item.id, item] as const));
    const preferredIds: string[] = [];
    if (activeConstraintId) preferredIds.push(activeConstraintId);
    for (let i = selectedContactConstraintIds.length - 1; i >= 0; i -= 1) {
      const id = selectedContactConstraintIds[i];
      if (!preferredIds.includes(id)) preferredIds.push(id);
    }

    let suggested: string | null = null;
    for (const constraintId of preferredIds) {
      const candidate = resolveTemplateComponentIdForConstraint(constraintsById.get(constraintId));
      if (!candidate) continue;
      suggested = candidate;
      break;
    }

    if (suggested && suggested !== selectedConstraintTemplateComponentId) {
      setSelectedConstraintTemplateComponentId(suggested);
    }
  }, [
    draft,
    activeConstraintId,
    selectedContactConstraintIds,
    resolveTemplateComponentIdForConstraint,
    selectedConstraintTemplateComponentId,
    setSelectedConstraintTemplateComponentId
  ]);

  const constraintHighlightResidues = useMemo<MolstarResidueHighlight[]>(() => {
    if (!draft) return [];
    const byKey = new Map<string, MolstarResidueHighlight>();
    const highlightConstraintIds = new Set<string>(selectedContactConstraintIds);
    if (activeConstraintId) {
      highlightConstraintIds.add(activeConstraintId);
    }
    if (highlightConstraintIds.size === 0) return [];

    for (const constraint of draft.inputConfig.constraints) {
      if (!highlightConstraintIds.has(constraint.id)) continue;
      const isActive = constraint.id === activeConstraintId;
      for (const residueRef of listConstraintResidues(constraint)) {
        const chainId = String(residueRef.chainId || '').trim();
        const residue = Math.max(1, Math.floor(Number(residueRef.residue) || 0));
        if (!chainId || !Number.isFinite(residue) || residue <= 0) continue;
        const chainInfo = chainInfoById.get(chainId);
        if (chainInfo && chainInfo.type !== 'protein') continue;
        const key = `${chainId}:${residue}`;
        const existing = byKey.get(key);
        if (!existing) {
          byKey.set(key, { chainId, residue, emphasis: isActive ? 'active' : 'default' });
          continue;
        }
        if (isActive && existing.emphasis !== 'active') {
          byKey.set(key, { ...existing, emphasis: 'active' });
        }
      }
    }

    return Array.from(byKey.values());
  }, [draft, activeConstraintId, chainInfoById, selectedContactConstraintIds]);

  const activeConstraintResidue = useMemo<MolstarResidueHighlight | null>(() => {
    if (!draft || !activeConstraintId) return null;
    const validChains = new Set(activeChainInfos.map((item) => item.id));
    const activeConstraint = draft.inputConfig.constraints.find((item) => item.id === activeConstraintId);
    if (!activeConstraint) return null;
    const first = listConstraintResidues(activeConstraint).find(
      (item) => validChains.has(item.chainId) && Number.isFinite(Number(item.residue)) && Number(item.residue) > 0
    );
    if (!first) return null;
    return {
      chainId: first.chainId,
      residue: Math.max(1, Math.floor(Number(first.residue))),
      emphasis: 'active'
    };
  }, [draft, activeConstraintId, activeChainInfos]);

  const constraintViewerHighlightResidues = useMemo<MolstarResidueHighlight[]>(() => {
    if (!selectedTemplatePreview) return constraintHighlightResidues;
    const ownerProteinChains = activeChainInfos.filter(
      (info) => info.componentId === selectedTemplatePreview.componentId && info.type === 'protein'
    );
    if (ownerProteinChains.length === 0) return [];
    const ownerChainIds = new Set(ownerProteinChains.map((info) => info.id));
    const viewerChainId = String(selectedTemplatePreview.chainId || '').trim() || ownerProteinChains[0]?.id || '';
    if (!viewerChainId) return [];

    const hasSequenceMapping = Object.keys(selectedTemplateSequenceToStructureResidueMap).length > 0;
    const byMappedResidue = new Map<string, MolstarResidueHighlight>();

    for (const item of constraintHighlightResidues) {
      const sourceChainInfo = chainInfoById.get(item.chainId);
      if (sourceChainInfo) {
        if (sourceChainInfo.type !== 'protein') continue;
        if (!ownerChainIds.has(item.chainId)) continue;
      }
      const mappedResidueRaw = selectedTemplateSequenceToStructureResidueMap[item.residue];
      const mappedResidue =
        typeof mappedResidueRaw === 'number' && Number.isFinite(mappedResidueRaw)
          ? Math.floor(mappedResidueRaw)
          : hasSequenceMapping
            ? Number.NaN
            : item.residue;
      if (!Number.isFinite(mappedResidue) || mappedResidue <= 0) continue;

      const key = `${viewerChainId}:${mappedResidue}`;
      const existing = byMappedResidue.get(key);
      if (!existing) {
        byMappedResidue.set(key, {
          chainId: viewerChainId,
          residue: mappedResidue,
          emphasis: item.emphasis
        });
        continue;
      }
      if (item.emphasis === 'active' && existing.emphasis !== 'active') {
        byMappedResidue.set(key, { ...existing, emphasis: 'active' });
      }
    }

    return Array.from(byMappedResidue.values());
  }, [
    selectedTemplatePreview,
    activeChainInfos,
    chainInfoById,
    constraintHighlightResidues,
    selectedTemplateSequenceToStructureResidueMap
  ]);

  const constraintViewerActiveResidue = useMemo<MolstarResidueHighlight | null>(() => {
    if (!selectedTemplatePreview) return activeConstraintResidue;
    return constraintViewerHighlightResidues.find((item) => item.emphasis === 'active') || null;
  }, [selectedTemplatePreview, activeConstraintResidue, constraintViewerHighlightResidues]);

  return {
    constraintTemplateOptions,
    selectedTemplatePreview,
    selectedTemplateResidueIndexMap,
    selectedTemplateSequenceToStructureResidueMap,
    resolveTemplateComponentIdForConstraint,
    constraintHighlightResidues,
    constraintViewerHighlightResidues,
    constraintViewerActiveResidue
  };
}
