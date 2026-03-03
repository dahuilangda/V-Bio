import type { InputComponent } from '../../types/models';
import type { MolstarResiduePick } from '../../components/project/MolstarViewer';

export interface ConstraintPickChainInfo {
  id: string;
  type: InputComponent['type'];
  componentId: string;
  residueCount?: number;
}

export interface SelectedTemplatePreviewLite {
  componentId: string;
  chainId?: string | null;
}

export interface NormalizedConstraintPick {
  chainId: string;
  residue: number;
  atomName?: string;
}

export function normalizeConstraintResiduePick(params: {
  pick: MolstarResiduePick;
  activeChainInfos: ConstraintPickChainInfo[];
  selectedTemplatePreview: SelectedTemplatePreviewLite | null;
  selectedTemplateResidueIndexMap: Record<string, Record<number, number>>;
}): NormalizedConstraintPick | null {
  const { pick, activeChainInfos, selectedTemplatePreview, selectedTemplateResidueIndexMap } = params;

  const chainExists = activeChainInfos.some((item) => item.id === pick.chainId);
  const selectedTemplateOwnerChain = selectedTemplatePreview
    ? activeChainInfos.find((item) => item.componentId === selectedTemplatePreview.componentId)?.id || null
    : null;
  const fallbackProteinChain =
    selectedTemplateOwnerChain ||
    activeChainInfos.find((item) => item.type === 'protein')?.id ||
    activeChainInfos[0]?.id ||
    pick.chainId ||
    'A';

  const resolvedChainId = selectedTemplateOwnerChain || (chainExists ? pick.chainId : fallbackProteinChain);
  const residueLimit = activeChainInfos.find((item) => item.id === resolvedChainId)?.residueCount || 0;

  const pickedResidueNumber = Number(pick.residue);
  if (!Number.isFinite(pickedResidueNumber) || pickedResidueNumber <= 0) {
    return null;
  }

  let normalizedResidue = Math.floor(pickedResidueNumber);
  const pickedTemplateChain = String(pick.chainId || '').trim();
  const selectedTemplateChain = String(selectedTemplatePreview?.chainId || '').trim();
  const pickedTemplateMap = pickedTemplateChain ? selectedTemplateResidueIndexMap[pickedTemplateChain] : undefined;
  const selectedTemplateMap = selectedTemplateChain ? selectedTemplateResidueIndexMap[selectedTemplateChain] : undefined;
  const mappedFromPickedChain = pickedTemplateMap?.[normalizedResidue];
  const mappedFromSelectedChain = selectedTemplateMap?.[normalizedResidue];
  const mappedResidueCandidate = [mappedFromPickedChain, mappedFromSelectedChain].find(
    (value) => typeof value === 'number' && Number.isFinite(value) && value > 0
  );

  const mappedResidue =
    typeof mappedResidueCandidate === 'number' && Number.isFinite(mappedResidueCandidate) && mappedResidueCandidate > 0
      ? mappedResidueCandidate
      : normalizedResidue;

  if (selectedTemplatePreview) {
    if (!(typeof mappedResidue === 'number' && Number.isFinite(mappedResidue) && mappedResidue > 0)) {
      return null;
    }
    normalizedResidue = Math.floor(mappedResidue);
  } else if (typeof mappedResidue === 'number' && Number.isFinite(mappedResidue) && mappedResidue > 0) {
    normalizedResidue = Math.floor(mappedResidue);
  }

  if (!selectedTemplatePreview && residueLimit > 0 && normalizedResidue > residueLimit) {
    return null;
  }

  return {
    chainId: resolvedChainId,
    residue: normalizedResidue,
    atomName: pick.atomName,
  };
}
