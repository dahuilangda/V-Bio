import type { InputComponent, PredictionConstraint } from '../../types/models';

export interface ChainInfoLite {
  id: string;
  type: InputComponent['type'];
  componentId: string;
}

export interface ComponentBucketEntry {
  id: string;
  typeLabel: string;
  typeOrder: number;
}

export function formatComponentChainLabel(
  chainId: string,
  chainInfoById: Map<string, ChainInfoLite>,
  componentTypeBuckets: Record<InputComponent['type'], ComponentBucketEntry[]>
): string {
  if (!chainId) return '-';
  const info = chainInfoById.get(chainId);
  if (!info) return chainId;
  const bucket = componentTypeBuckets[info.type];
  const entry = bucket.find((item) => item.id === info.componentId);
  if (!entry) return chainId;
  return `${entry.typeLabel} ${entry.typeOrder}`;
}

export function formatConstraintCombo(
  constraint: PredictionConstraint,
  chainInfoById: Map<string, ChainInfoLite>,
  componentTypeBuckets: Record<InputComponent['type'], ComponentBucketEntry[]>
): string {
  const format = (chainId: string) => formatComponentChainLabel(chainId, chainInfoById, componentTypeBuckets);
  if (constraint.type === 'contact') {
    return `${format(constraint.token1_chain)} ↔ ${format(constraint.token2_chain)}`;
  }
  if (constraint.type === 'bond') {
    return `${format(constraint.atom1_chain)} ↔ ${format(constraint.atom2_chain)}`;
  }
  const targetChain = constraint.contacts[0]?.[0] || '';
  return `${format(constraint.binder)} ↔ ${format(targetChain)}`;
}

export function formatConstraintDetail(constraint: PredictionConstraint): string {
  if (constraint.type === 'contact') {
    return `${constraint.token1_chain}:${constraint.token1_residue} ↔ ${constraint.token2_chain}:${constraint.token2_residue}`;
  }
  if (constraint.type === 'bond') {
    return `${constraint.atom1_chain}:${constraint.atom1_residue}:${constraint.atom1_atom} ↔ ${constraint.atom2_chain}:${constraint.atom2_residue}:${constraint.atom2_atom}`;
  }
  const first = constraint.contacts[0];
  if (first) {
    return `${constraint.binder} ↔ ${first[0]}:${first[1]}`;
  }
  return `${constraint.binder}`;
}

export function constraintLabel(type: string): string {
  if (type === 'contact') return 'Contact';
  if (type === 'bond') return 'Bond';
  if (type === 'pocket') return 'Pocket';
  return 'Constraint';
}

export function buildDefaultConstraint(params: {
  preferredType?: 'contact' | 'bond' | 'pocket';
  picked?: { chainId: string; residue: number };
  activeChainInfos: Array<{ id: string }>;
  ligandChainOptions: Array<{ id: string }>;
  isBondOnlyBackend: boolean;
}): PredictionConstraint {
  const { preferredType, picked, activeChainInfos, ligandChainOptions, isBondOnlyBackend } = params;
  const id = crypto.randomUUID ? crypto.randomUUID() : Math.random().toString(36).slice(2);
  const chainA = activeChainInfos[0]?.id || 'A';
  const chainB = activeChainInfos.find((item) => item.id !== chainA)?.id || chainA;
  const firstLigandChain = ligandChainOptions[0]?.id || null;
  const resolvedType = preferredType || (isBondOnlyBackend ? 'bond' : firstLigandChain ? 'pocket' : 'contact');

  if (resolvedType === 'bond') {
    return {
      id,
      type: 'bond',
      atom1_chain: chainA,
      atom1_residue: Math.max(1, Math.floor(Number(picked?.residue) || 1)),
      atom1_atom: 'CA',
      atom2_chain: picked?.chainId || chainB,
      atom2_residue: Math.max(1, Math.floor(Number(picked?.residue) || 1)),
      atom2_atom: 'CA',
    };
  }

  if (resolvedType === 'pocket') {
    const binder = firstLigandChain || chainA;
    return {
      id,
      type: 'pocket',
      binder,
      contacts:
        picked?.chainId && Number.isFinite(Number(picked?.residue)) && Number(picked?.residue) > 0
          ? [[picked.chainId, Math.floor(Number(picked.residue))]]
          : [],
      max_distance: 6,
      force: true,
    };
  }

  return {
    id,
    type: 'contact',
    token1_chain: chainA,
    token1_residue: Math.max(1, Math.floor(Number(picked?.residue) || 1)),
    token2_chain: picked?.chainId || chainB,
    token2_residue: Math.max(1, Math.floor(Number(picked?.residue) || 1)),
    max_distance: 5,
    force: true,
  };
}
