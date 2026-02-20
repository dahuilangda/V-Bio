import type { MolstarResiduePick } from './types';
import { readFirstIndex, readIndexedValue } from './loci';

const INVALID_CHAIN_TOKENS = new Set([
  'chain',
  'asym',
  'asymid',
  'label',
  'auth',
  'res',
  'residue',
  'residueid',
  'seq',
  'seqid',
  'index',
  'atom',
  'model',
  'unit',
  'element'
]);

function isLikelyChainId(value: string): boolean {
  const chain = value.trim();
  if (!chain) return false;
  if (chain.length > 16) return false;
  if (!/^[A-Za-z0-9_.-]+$/.test(chain)) return false;
  if (INVALID_CHAIN_TOKENS.has(chain.toLowerCase())) return false;
  return true;
}

function toPositiveInt(value: unknown): number | null {
  const parsed = Number.parseInt(String(value ?? '').trim(), 10);
  if (!Number.isFinite(parsed) || parsed <= 0) return null;
  return parsed;
}

function parsePickFromTypedFields(value: unknown): MolstarResiduePick | null {
  if (!value || typeof value !== 'object') return null;

  const queue: Array<{ node: unknown; depth: number }> = [{ node: value, depth: 0 }];
  const visited = new WeakSet<object>();
  const maxDepth = 5;
  const maxNodes = 180;
  let scanned = 0;

  const chainKeys = ['auth_asym_id', 'label_asym_id', 'chain_id', 'chainId', 'asym_id'];
  const residueKeys = [
    'auth_seq_id',
    'label_seq_id',
    'seq_id',
    'residue_number',
    'residueNumber',
    'residue_no',
    'resno'
  ];
  const atomKeys = ['label_atom_id', 'atom_name', 'atomName'];

  while (queue.length > 0 && scanned < maxNodes) {
    const { node, depth } = queue.shift()!;
    scanned += 1;
    if (!node || typeof node !== 'object') continue;
    const obj = node as Record<string, unknown>;
    if (visited.has(obj)) continue;
    visited.add(obj);

    let chainValue: string | null = null;
    let residueValue: number | null = null;
    let atomValue: string | undefined;

    for (const key of chainKeys) {
      const raw = obj[key];
      if (raw == null) continue;
      const candidate = String(raw).trim();
      if (isLikelyChainId(candidate)) {
        chainValue = candidate;
        break;
      }
    }

    for (const key of residueKeys) {
      const raw = obj[key];
      const candidate = toPositiveInt(raw);
      if (candidate !== null) {
        residueValue = candidate;
        break;
      }
    }

    for (const key of atomKeys) {
      const raw = obj[key];
      if (raw == null) continue;
      const candidate = String(raw).trim();
      if (!candidate) continue;
      atomValue = candidate;
      break;
    }

    if (chainValue && residueValue !== null) {
      return {
        chainId: chainValue,
        residue: residueValue,
        atomName: atomValue,
        label: `${chainValue}:${residueValue}${atomValue ? `:${atomValue}` : ''}`
      };
    }

    if (depth >= maxDepth) continue;
    for (const nested of Object.values(obj)) {
      if (nested && typeof nested === 'object') {
        queue.push({ node: nested, depth: depth + 1 });
      }
    }
  }

  return null;
}

function getSelectionEntries(viewer: any): any[] {
  const selection = viewer?.plugin?.managers?.structure?.selection;
  const entries = selection?.entries;
  if (!entries) return [];

  if (Array.isArray(entries)) return entries;

  if (typeof entries.values === 'function') {
    try {
      return Array.from(entries.values());
    } catch {
      // continue
    }
  }

  if (typeof entries[Symbol.iterator] === 'function') {
    try {
      return Array.from(entries as Iterable<any>);
    } catch {
      // continue
    }
  }

  if (typeof entries === 'object') {
    try {
      return Object.values(entries);
    } catch {
      // continue
    }
  }

  return [];
}

function parsePickFromSelectionEntry(entry: any): MolstarResiduePick | null {
  if (!entry) return null;

  const directLoci =
    entry?.loci ??
    entry?.selection?.loci ??
    entry?.cell?.obj?.data?.loci ??
    entry?.data?.loci;
  if (directLoci) {
    const parsed = parsePickFromEvent({ current: { loci: directLoci } });
    if (parsed) return parsed;
    const parsedWithLib = parsePickUsingMolstarLib(directLoci);
    if (parsedWithLib) return parsedWithLib;
  }

  const structureData =
    entry?.structure ??
    entry?.selection?.structure ??
    entry?.cell?.obj?.data?.structure ??
    entry?.obj?.data?.structure;
  const structurePick = parsePickFromStructureData(structureData);
  if (structurePick) return structurePick;

  return parsePickFromTypedFields(entry);
}

function parsePickFromSelectionManager(viewer: any): MolstarResiduePick | null {
  const selectionManager = viewer?.plugin?.managers?.structure?.selection;
  const entries = getSelectionEntries(viewer);
  for (const entry of entries) {
    const parsed = parsePickFromSelectionEntry(entry);
    if (parsed) {
      return parsed;
    }
  }
  if (selectionManager?.getLoci) {
    try {
      const loci = selectionManager.getLoci();
      const parsed = parsePickFromEvent({ current: { loci } });
      if (parsed) return parsed;
    } catch {
      // no-op
    }
  }
  return null;
}

function parsePickFromFocusManager(viewer: any): MolstarResiduePick | null {
  const current = viewer?.plugin?.managers?.structure?.focus?.current;
  if (!current) return null;

  const loci = current?.loci ?? current?.current?.loci ?? current?.focus?.loci;
  if (loci) {
    const parsed = parsePickFromEvent({ current: { loci } });
    if (parsed) return parsed;
  }

  const structurePick = parsePickFromStructureData(
    current?.structure ?? current?.current?.structure ?? current?.focus?.structure
  );
  if (structurePick) return structurePick;

  const label = String(
    current?.label ?? current?.description ?? current?.current?.label ?? current?.focus?.label ?? ''
  ).trim();
  if (label) {
    const parsed = parsePickFromLabelText(label);
    if (parsed) return parsed;
  }

  return null;
}

function parsePickFromFocusEvent(event: any): MolstarResiduePick | null {
  if (!event) return null;
  const parsedDirect = parsePickFromEvent(event);
  if (parsedDirect) return parsedDirect;

  const parsedCurrent = parsePickFromEvent({ current: event?.current ?? event?.focus ?? event?.data ?? event });
  if (parsedCurrent) return parsedCurrent;

  const structurePick = parsePickFromStructureData(
    event?.current?.structure ?? event?.focus?.structure ?? event?.structure ?? event?.data?.structure
  );
  if (structurePick) return structurePick;

  const label = String(
    event?.current?.label ?? event?.focus?.label ?? event?.label ?? event?.description ?? ''
  ).trim();
  if (!label) return null;
  return parsePickFromLabelText(label);
}

function parsePickFromStructureData(structure: any): MolstarResiduePick | null {
  if (!structure || typeof structure !== 'object') return null;
  const unit =
    structure?.units?.[0] ??
    structure?.unitSymmetryGroups?.[0]?.units?.[0] ??
    structure?.models?.[0]?.units?.[0];
  if (!unit) return null;

  const hierarchy = unit?.model?.atomicHierarchy;
  const unitElements = unit?.elements;
  if (!hierarchy || !unitElements) return null;

  const idxInUnit = readFirstIndex(unitElements);
  if (idxInUnit === null || idxInUnit < 0) return null;

  const atomIndex = Number(readIndexedValue(unitElements, idxInUnit) ?? idxInUnit);
  if (!Number.isFinite(atomIndex) || atomIndex < 0) return null;

  const residueIndex = Number(readIndexedValue(hierarchy?.residueAtomSegments?.index, atomIndex));
  const chainIndex = Number(readIndexedValue(hierarchy?.chainAtomSegments?.index, atomIndex));

  const chainFromChains =
    readIndexedValue(hierarchy?.chains?.auth_asym_id, chainIndex) ??
    readIndexedValue(hierarchy?.chains?.label_asym_id, chainIndex);
  const residueFromResidues =
    readIndexedValue(hierarchy?.residues?.auth_seq_id, residueIndex) ??
    readIndexedValue(hierarchy?.residues?.label_seq_id, residueIndex) ??
    readIndexedValue(hierarchy?.residues?.seq_id, residueIndex);
  const atomFromAtoms = readIndexedValue(hierarchy?.atoms?.label_atom_id, atomIndex);

  return pickFromChainResidue(chainFromChains, residueFromResidues, atomFromAtoms);
}

function unwrapLoci(input: any): any | null {
  if (!input || typeof input !== 'object') return null;
  const seen = new Set<any>();
  let current: any = input;

  while (current && typeof current === 'object' && !seen.has(current)) {
    seen.add(current);

    if (current.kind === 'empty-loci') return null;
    if (Array.isArray(current.elements)) return current;
    if (current.unit && current.indices) {
      return { kind: 'element-loci', elements: [current] };
    }
    if (typeof current.kind === 'string' && current.kind.toLowerCase().includes('loci')) return current;

    if (current.loci && typeof current.loci === 'object') {
      current = current.loci;
      continue;
    }

    if (current.current && typeof current.current === 'object') {
      current = current.current;
      continue;
    }

    break;
  }

  if (current && current.unit && current.indices) {
    return { kind: 'element-loci', elements: [current] };
  }
  return null;
}

function extractLociFromEvent(event: any): any | null {
  const candidates = [
    event?.loci,
    event?.data?.current,
    event?.current?.loci,
    event?.data?.loci,
    event?.current,
    event
  ];
  for (const candidate of candidates) {
    const loci = unwrapLoci(candidate);
    if (!loci) continue;
    if (String(loci.kind ?? '').toLowerCase() === 'empty-loci') continue;
    return loci;
  }
  return null;
}

function parsePickUsingMolstarLib(loci: any): MolstarResiduePick | null {
  const normalizedLoci = unwrapLoci(loci) ?? loci;
  const lib = (window as any)?.molstar?.lib;
  const modelStructure = lib?.molModelStructure;
  const structureElement = modelStructure?.StructureElement;
  const structureProps = modelStructure?.StructureProperties;
  const firstLocation = structureElement?.Loci?.getFirstLocation?.(normalizedLoci);
  if (!firstLocation || !structureProps) return null;

  const chainId = String(
    structureProps?.chain?.auth_asym_id?.(firstLocation) ??
      structureProps?.chain?.label_asym_id?.(firstLocation) ??
      ''
  ).trim();
  const residueRaw =
    structureProps?.residue?.auth_seq_id?.(firstLocation) ?? structureProps?.residue?.label_seq_id?.(firstLocation);
  const residue = Number.parseInt(String(residueRaw ?? ''), 10);
  const atomName = String(structureProps?.atom?.label_atom_id?.(firstLocation) ?? '').trim() || undefined;
  if (!chainId || !isLikelyChainId(chainId) || !Number.isFinite(residue) || residue <= 0) return null;

  return {
    chainId,
    residue,
    atomName,
    label: `${chainId}:${residue}${atomName ? `:${atomName}` : ''}`
  };
}

function parsePickFromLabelText(text: string): MolstarResiduePick | null {
  const cleaned = text.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  if (!cleaned) return null;
  const patterns = [
    /(?:auth|label)?[_\s-]*asym[_\s-]*id\s*[:=]\s*([A-Za-z0-9_.-]{1,16}).*?(?:auth|label)?[_\s-]*seq[_\s-]*id\s*[:=]\s*(-?\d+)/i,
    /chain\s*[:=]?\s*([A-Za-z0-9_.-]{1,16}).*?(?:res(?:idue)?|seq(?:_?id)?)\s*[:=]?\s*(-?\d+)/i,
    /\b([A-Za-z0-9_.-]{1,16})\s*[:/]\s*(?:[A-Za-z]{1,3}\s*)?(-?\d+)\b/,
    /\b([A-Za-z0-9_.-]{1,16})\s+([A-Za-z]{1,3})\s*(-?\d+)\b/,
    /\b([A-Za-z]{1,3})\s*(-?\d+)\s*\(\s*([A-Za-z0-9_.-]{1,16})\s*\)/i
  ];
  for (const pattern of patterns) {
    const match = cleaned.match(pattern);
    if (!match) continue;
    let chainId = '';
    let residue = Number.NaN;

    if (pattern === patterns[3]) {
      chainId = String(match[1] || '').trim();
      residue = Number.parseInt(String(match[3] || ''), 10);
    } else if (pattern === patterns[4]) {
      chainId = String(match[3] || '').trim();
      residue = Number.parseInt(String(match[2] || ''), 10);
    } else {
      chainId = String(match[1] || '').trim();
      residue = Number.parseInt(String(match[2] || ''), 10);
    }

    if (!chainId || !Number.isFinite(residue) || residue <= 0) continue;
    if (!isLikelyChainId(chainId)) continue;
    return {
      chainId,
      residue,
      label: `${chainId}:${residue}`
    };
  }
  return null;
}

function pickFromChainResidue(
  chainRaw: unknown,
  residueRaw: unknown,
  atomRaw?: unknown
): MolstarResiduePick | null {
  const chainId = String(chainRaw ?? '').trim();
  const residue = Number.parseInt(String(residueRaw ?? ''), 10);
  if (!chainId || !isLikelyChainId(chainId) || !Number.isFinite(residue) || residue <= 0) {
    return null;
  }
  const atomName = String(atomRaw ?? '').trim() || undefined;
  return {
    chainId,
    residue,
    atomName,
    label: `${chainId}:${residue}${atomName ? `:${atomName}` : ''}`
  };
}

function parseElementLociStrict(loci: any): MolstarResiduePick | null {
  if (!loci) return null;
  const fromMolstarLib = parsePickUsingMolstarLib(loci);
  if (fromMolstarLib) return fromMolstarLib;
  if (loci.kind !== 'element-loci') return null;
  const elementRef = loci?.elements?.[0];
  if (!elementRef) {
    pickDebugLog('strict parse failed: no element');
    return null;
  }

  const unit = elementRef.unit;
  const model = unit?.model;
  const hierarchy = model?.atomicHierarchy;
  const unitElements = unit?.elements;
  if (!hierarchy || !unitElements) {
    pickDebugLog('strict parse failed: no hierarchy');
    return null;
  }

  const idxInUnit = readFirstIndex(elementRef.indices);
  if (idxInUnit === null || idxInUnit < 0) {
    pickDebugLog('strict parse failed: invalid index', idxInUnit);
    return null;
  }

  const modelAtomIndexFromUnit = readIndexedValue(unitElements, idxInUnit);
  const atomCandidates = [
    modelAtomIndexFromUnit,
    idxInUnit,
    readFirstIndex(unitElements)
  ].filter((v) => Number.isFinite(Number(v)));

  for (const atomCandidate of atomCandidates) {
    const atomIndex = Number(atomCandidate);
    const residueIndexRaw = readIndexedValue(hierarchy?.residueAtomSegments?.index, atomIndex);
    const chainIndexRaw = readIndexedValue(hierarchy?.chainAtomSegments?.index, atomIndex);
    const residueIndex = Number(residueIndexRaw);
    const chainIndex = Number(chainIndexRaw);

    const chainFromChains =
      readIndexedValue(hierarchy?.chains?.auth_asym_id, chainIndex) ??
      readIndexedValue(hierarchy?.chains?.label_asym_id, chainIndex);
    const residueFromResidues =
      readIndexedValue(hierarchy?.residues?.auth_seq_id, residueIndex) ??
      readIndexedValue(hierarchy?.residues?.label_seq_id, residueIndex) ??
      readIndexedValue(hierarchy?.residues?.seq_id, residueIndex);
    const atomFromAtoms = readIndexedValue(hierarchy?.atoms?.label_atom_id, atomIndex);

    const chainFromAtoms =
      readIndexedValue(hierarchy?.atoms?.auth_asym_id, atomIndex) ??
      readIndexedValue(hierarchy?.atoms?.label_asym_id, atomIndex);
    const residueFromAtoms =
      readIndexedValue(hierarchy?.atoms?.auth_seq_id, atomIndex) ??
      readIndexedValue(hierarchy?.atoms?.label_seq_id, atomIndex);

    const pick =
      pickFromChainResidue(chainFromChains, residueFromResidues, atomFromAtoms) ??
      pickFromChainResidue(chainFromAtoms, residueFromAtoms, atomFromAtoms);
    if (pick) {
      return pick;
    }
  }

  pickDebugLog('strict parse failed: no chain/residue', { kind: loci.kind });
  return null;
}

function parsePickFromEvent(event: any): MolstarResiduePick | null {
  const loci = extractLociFromEvent(event);
  if (!loci) {
    return null;
  }

  const strict = parseElementLociStrict(loci);
  if (strict) return strict;

  const typed =
    parsePickFromTypedFields(loci) ??
    parsePickFromTypedFields(event?.current) ??
    parsePickFromTypedFields(event?.data) ??
    parsePickFromTypedFields(event);
  if (typed) return typed;

  const labelText = String(
    event?.current?.label ??
      event?.label ??
      event?.data?.label ??
      loci?.label ??
      (typeof loci?.getLabel === 'function' ? loci.getLabel?.() : '')
  ).trim();
  return parsePickFromLabelText(labelText);
}

function isPickDebugEnabled(): boolean {
  if (typeof window === 'undefined') return false;
  try {
    if (window.localStorage.getItem('vbio:molstar-pick-debug') === '0') return false;
    if (window.localStorage.getItem('vbio:molstar-pick-debug') === '1') return true;
    if (new URLSearchParams(window.location.search).has('molstarDebug')) return true;
  } catch {
    // no-op
  }
  return false;
}

function pickDebugLog(...args: unknown[]) {
  if (!isPickDebugEnabled()) return;
  // eslint-disable-next-line no-console
  console.log('[VBio][Mol* Pick]', ...args);
}

function describeEventForDebug(event: any): Record<string, unknown> {
  const current = event?.current;
  const currentLoci = current?.loci;
  return {
    hasCurrent: Boolean(event?.current),
    hasLoci: Boolean(event?.loci || event?.current?.loci || event?.data?.loci),
    label: event?.current?.label ?? event?.label ?? event?.data?.label ?? null,
    currentKeys: event?.current && typeof event.current === 'object' ? Object.keys(event.current).slice(0, 16) : [],
    eventKeys: event && typeof event === 'object' ? Object.keys(event).slice(0, 20) : [],
    currentLociType: typeof currentLoci,
    currentLociKind: currentLoci?.kind ?? null,
    eventLociType: typeof event?.loci,
    eventLociKind: event?.loci?.kind ?? null
  };
}

function readAltModifier(event: any): boolean | null {
  const candidates = [
    event,
    event?.current,
    event?.event,
    event?.data,
    event?.modifiers,
    event?.current?.event,
    event?.event?.sourceEvent,
    event?.current?.event?.sourceEvent,
    event?.srcEvent,
    event?.sourceEvent
  ];

  for (const value of candidates) {
    if (!value || typeof value !== 'object') continue;
    if (typeof value.altKey === 'boolean') return value.altKey;
    if (typeof value.alt === 'boolean') return value.alt;
    if (typeof value.modifiers?.alt === 'boolean') return value.modifiers.alt;
  }
  return null;
}

function readShiftModifier(event: any): boolean | null {
  const candidates = [
    event,
    event?.current,
    event?.event,
    event?.data,
    event?.modifiers,
    event?.current?.event,
    event?.event?.sourceEvent,
    event?.current?.event?.sourceEvent,
    event?.srcEvent,
    event?.sourceEvent
  ];

  for (const value of candidates) {
    if (!value || typeof value !== 'object') continue;
    if (typeof value.shiftKey === 'boolean') return value.shiftKey;
    if (typeof value.shift === 'boolean') return value.shift;
    if (typeof value.modifiers?.shift === 'boolean') return value.modifiers.shift;
  }
  return null;
}

function readCtrlModifier(event: any): boolean | null {
  const candidates = [
    event,
    event?.current,
    event?.event,
    event?.data,
    event?.modifiers,
    event?.current?.event,
    event?.event?.sourceEvent,
    event?.current?.event?.sourceEvent,
    event?.srcEvent,
    event?.sourceEvent
  ];

  for (const value of candidates) {
    if (!value || typeof value !== 'object') continue;
    if (typeof value.ctrlKey === 'boolean') return value.ctrlKey;
    if (typeof value.metaKey === 'boolean') return value.metaKey;
    if (typeof value.control === 'boolean') return value.control;
    if (typeof value.modifiers?.control === 'boolean') return value.modifiers.control;
    if (typeof value.modifiers?.ctrl === 'boolean') return value.modifiers.ctrl;
  }
  return null;
}

function readLeftButton(event: any): boolean | null {
  const candidates = [
    event?.current?.event?.sourceEvent,
    event?.current?.event?.srcEvent,
    event?.event?.sourceEvent,
    event?.event?.srcEvent,
    event?.sourceEvent,
    event?.srcEvent,
    event
  ];

  for (const sourceEvent of candidates) {
    if (!sourceEvent || typeof sourceEvent !== 'object') continue;
    if (typeof sourceEvent.button === 'number') {
      return sourceEvent.button === 0;
    }
    if (typeof sourceEvent.which === 'number') {
      return sourceEvent.which === 1;
    }
    if (typeof sourceEvent.buttons === 'number') {
      if (sourceEvent.buttons === 0) return null;
      return (sourceEvent.buttons & 1) === 1;
    }
  }

  return null;
}

function isAltLeftPick(event: any, isModifierPressed?: () => boolean): boolean {
  const altFromEvent = readAltModifier(event);
  const shiftFromEvent = readShiftModifier(event);
  const ctrlFromEvent = readCtrlModifier(event);
  const hasModifier =
    altFromEvent === true || shiftFromEvent === true || ctrlFromEvent === true || Boolean(isModifierPressed?.());
  if (!hasModifier) return false;

  const leftButton = readLeftButton(event);
  return leftButton !== false;
}

export function subscribePickEvents(
  viewer: any,
  onResiduePick?: (pick: MolstarResiduePick) => void,
  pickMode: 'click' | 'alt-left' = 'click',
  isModifierPressed?: () => boolean,
  shouldSuppress?: () => boolean
): (() => void) | null {
  if (!onResiduePick) return null;
  const subscriptions: Array<{ unsubscribe: () => void }> = [];
  let lastEmittedLabel = '';
  let lastEmittedAt = 0;

  const emitPick = (pick: MolstarResiduePick) => {
    const now = Date.now();
    if (pick.label === lastEmittedLabel && now - lastEmittedAt < 120) {
      pickDebugLog('emit skipped (dedupe)', pick.label);
      return;
    }
    lastEmittedLabel = pick.label;
    lastEmittedAt = now;
    pickDebugLog('emit', pick);
    onResiduePick(pick);
  };

  const parseAndEmit = (event: any, source: string): 'handled' | 'skip' | 'retry' => {
    if (shouldSuppress?.()) return 'skip';
    if (pickMode === 'alt-left' && !isAltLeftPick(event, isModifierPressed)) {
      pickDebugLog('skip (modifier)', source);
      return 'skip';
    }
    const parsed = parsePickFromEvent(event);
    if (parsed) {
      pickDebugLog('pick', source, parsed.label);
      emitPick(parsed);
      return 'handled';
    }

    const loci = extractLociFromEvent(event);
    if (!loci) return 'skip';

    try {
      viewer?.plugin?.managers?.structure?.selection?.fromLoci?.('set', loci);
    } catch {
      // no-op
    }

    const parsedFromManagers = parsePickFromFocusManager(viewer) ?? parsePickFromSelectionManager(viewer);
    if (!parsedFromManagers) return 'retry';
    pickDebugLog('pick (manager)', source, parsedFromManagers.label);
    emitPick(parsedFromManagers);
    return 'handled';
  };

  const retryFromSelection = (source: string) => {
    window.setTimeout(() => {
      if (shouldSuppress?.()) return;
      const parsed = parsePickFromSelectionManager(viewer) ?? parsePickFromFocusManager(viewer);
      if (!parsed) {
        pickDebugLog('selection/focus retry none', source);
        return;
      }
      pickDebugLog('selection/focus retry', source, parsed.label);
      emitPick(parsed);
    }, 0);
  };

  const behaviorClick = viewer?.plugin?.behaviors?.interaction?.click;
  if (behaviorClick && typeof behaviorClick.subscribe === 'function') {
    const sub = behaviorClick.subscribe((event: any) => {
      const result = parseAndEmit(event, 'behavior.click');
      if (result === 'retry') {
        pickDebugLog('event parse failed', 'behavior.click', describeEventForDebug(event));
        retryFromSelection('behavior.click');
      }
    });
    if (sub && typeof sub.unsubscribe === 'function') {
      subscriptions.push(sub);
    }
  }

  const managerClick = viewer?.plugin?.managers?.interactivity?.events?.click;
  if (managerClick && typeof managerClick.subscribe === 'function') {
    const sub = managerClick.subscribe((event: any) => {
      const result = parseAndEmit(event, 'manager.click');
      if (result === 'retry') {
        pickDebugLog('event parse failed', 'manager.click', describeEventForDebug(event));
        retryFromSelection('manager.click');
      }
    });
    if (sub && typeof sub.unsubscribe === 'function') {
      subscriptions.push(sub);
    }
  }

  const selectionChanged = viewer?.plugin?.managers?.structure?.selection?.events?.changed;
  if (selectionChanged && typeof selectionChanged.subscribe === 'function') {
    const sub = selectionChanged.subscribe((event: any) => {
      if (shouldSuppress?.()) return;
      if (pickMode !== 'click') return;
      const loci = extractLociFromEvent(event);
      if (!loci) return;
      const parsed = parsePickFromEvent({ current: { loci } }) ?? parsePickFromSelectionEntry(event);
      if (!parsed) return;
      pickDebugLog('selection.changed', parsed.label);
      emitPick(parsed);
    });
    if (sub && typeof sub.unsubscribe === 'function') {
      subscriptions.push(sub);
    }
  }

  const focusChanged = viewer?.plugin?.managers?.structure?.focus?.behaviors?.current;
  if (focusChanged && typeof focusChanged.subscribe === 'function') {
    const sub = focusChanged.subscribe((event: any) => {
      if (shouldSuppress?.()) return;
      if (pickMode !== 'click') return;
      const loci = extractLociFromEvent(event);
      if (!loci) return;
      const parsed = parsePickFromEvent({ current: { loci } }) ?? parsePickFromFocusEvent(event);
      if (!parsed) return;
      pickDebugLog('focus.changed', parsed.label);
      emitPick(parsed);
    });
    if (sub && typeof sub.unsubscribe === 'function') {
      subscriptions.push(sub);
    }
  }

  if (subscriptions.length === 0) return null;
  return () => {
    for (const sub of subscriptions) {
      sub.unsubscribe();
    }
  };
}
