import { useCallback, useEffect, useRef, useState } from 'react';
import { ENV } from '../../utils/env';
import { loadScript, loadStylesheet } from '../../utils/script';
import { alphafoldLegend } from '../../utils/alphafold';

declare global {
  interface Window {
    molstar?: {
      Viewer?: {
        create?: (target: HTMLElement, options: Record<string, unknown>) => Promise<any>;
      };
    };
  }
}

interface MolstarViewerProps {
  structureText: string;
  format: 'cif' | 'pdb';
  overlayStructureText?: string;
  overlayFormat?: 'cif' | 'pdb';
  colorMode?: string;
  confidenceBackend?: string;
  onResiduePick?: (pick: MolstarResiduePick) => void;
  pickMode?: 'click' | 'alt-left';
  highlightResidues?: MolstarResidueHighlight[];
  activeResidue?: MolstarResidueHighlight | null;
  lockView?: boolean;
  suppressAutoFocus?: boolean;
  showSequence?: boolean;
}

const MOLSTAR_OVERRIDE_STYLE_ID = 'vbio-molstar-theme-overrides';

export interface MolstarResiduePick {
  chainId: string;
  residue: number;
  atomName?: string;
  label: string;
}

export interface MolstarResidueHighlight {
  chainId: string;
  residue: number;
  emphasis?: 'default' | 'active';
}

const EMPTY_HIGHLIGHTS: MolstarResidueHighlight[] = [];

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

function ensureMolstarThemeOverrides() {
  if (typeof document === 'undefined') return;
  if (document.getElementById(MOLSTAR_OVERRIDE_STYLE_ID)) return;

  const style = document.createElement('style');
  style.id = MOLSTAR_OVERRIDE_STYLE_ID;
  style.textContent = `
.molstar-host .msp-plugin .msp-control-group-header>button,
.molstar-host .msp-plugin .msp-control-group-header div {
  background: #ffffff !important;
  border-color: #d8e0e7 !important;
  color: #2f4150 !important;
}

.molstar-host .msp-plugin .msp-control-group-header>button:hover,
.molstar-host .msp-plugin .msp-control-group-header div:hover {
  background: #f3f6f8 !important;
}

.molstar-host .msp-plugin .msp-control-group-header>button:focus,
.molstar-host .msp-plugin .msp-control-group-header>button:active {
  background: #edf2f6 !important;
  outline: 1px solid #afc0d0 !important;
  box-shadow: none !important;
}

.molstar-host .msp-plugin .msp-form-control,
.molstar-host .msp-plugin input.msp-form-control,
.molstar-host .msp-plugin select.msp-form-control,
.molstar-host .msp-plugin textarea.msp-form-control,
.molstar-host .msp-plugin .msp-form-control input,
.molstar-host .msp-plugin .msp-form-control select,
.molstar-host .msp-plugin .msp-form-control textarea {
  background: #ffffff !important;
  border-color: #d8e0e7 !important;
  color: #2f4150 !important;
  box-shadow: none !important;
}

.molstar-host .msp-plugin .msp-form-control:focus,
.molstar-host .msp-plugin input.msp-form-control:focus,
.molstar-host .msp-plugin select.msp-form-control:focus,
.molstar-host .msp-plugin textarea.msp-form-control:focus,
.molstar-host .msp-plugin .msp-form-control input:focus,
.molstar-host .msp-plugin .msp-form-control select:focus,
.molstar-host .msp-plugin .msp-form-control textarea:focus {
  outline: 1px solid #afc0d0 !important;
  box-shadow: none !important;
}

.molstar-host .msp-plugin .msp-form-control option,
.molstar-host .msp-plugin select.msp-form-control option,
.molstar-host .msp-plugin .msp-form-control select option {
  background: #ffffff !important;
  color: #2f4150 !important;
}

.molstar-host .msp-plugin .msp-section-header {
  background: #ffffff !important;
  border-color: #d8e0e7 !important;
  color: #2f4150 !important;
}

.molstar-host .msp-plugin .msp-section-header:hover {
  background: #f3f6f8 !important;
}

.molstar-host .msp-plugin .msp-section-header:focus,
.molstar-host .msp-plugin .msp-section-header:active {
  background: #edf2f6 !important;
  outline: 1px solid #afc0d0 !important;
  box-shadow: none !important;
}
`;
  document.head.appendChild(style);
}

async function waitForMolstarReady(timeoutMs = 12000, intervalMs = 120) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const creator = window.molstar?.Viewer?.create;
    if (creator) return creator;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error('Mol* failed to initialize.');
}

async function loadStructure(
  viewer: any,
  text: string,
  format: 'cif' | 'pdb',
  options?: { clearBefore?: boolean }
) {
  const clearBefore = options?.clearBefore !== false;
  if (clearBefore && typeof viewer?.clear === 'function') {
    await viewer.clear();
  }
  const formats = format === 'cif' ? ['mmcif', 'pdb'] : ['pdb', 'mmcif'];
  const errors: string[] = [];

  for (const candidate of formats) {
    try {
      if (typeof viewer?.loadStructureFromData === 'function') {
        await viewer.loadStructureFromData(text, candidate, false);
        return;
      }

      if (typeof viewer?.loadStructureFromUrl === 'function') {
        const blob = new Blob([text], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        try {
          await viewer.loadStructureFromUrl(url, candidate, false);
          return;
        } finally {
          URL.revokeObjectURL(url);
        }
      }
    } catch (error) {
      errors.push(error instanceof Error ? error.message : String(error));
    }
  }

  if (errors.length > 0) {
    throw new Error(`Unable to load structure in Mol* (${errors[0]}).`);
  }
  throw new Error('Mol* Viewer API is unavailable for loading this structure.');
}

function collectStructureComponents(viewer: any): any[] {
  const plugin = viewer?.plugin;
  const hierarchyCurrent = plugin?.managers?.structure?.hierarchy?.current;
  const hierarchySelection = plugin?.managers?.structure?.hierarchy?.selection;
  const componentManager = plugin?.managers?.structure?.component;
  const fromGroups = Array.isArray(hierarchyCurrent?.componentGroups) ? hierarchyCurrent.componentGroups.flat() : [];
  const fromHierarchy = Array.isArray(hierarchyCurrent?.components) ? hierarchyCurrent.components : [];
  const fromSelectionHierarchy = Array.isArray(hierarchySelection?.components) ? hierarchySelection.components : [];
  const fromManager = Array.isArray(componentManager?.components) ? componentManager.components : [];
  const collectFromStructureEntries = (entries: any[]): any[] => {
    const acc: any[] = [];
    for (const entry of entries) {
      if (Array.isArray(entry?.components)) {
        acc.push(...entry.components);
      }
    }
    return acc;
  };
  const currentStructures = Array.isArray(hierarchyCurrent?.structures) ? hierarchyCurrent.structures : [];
  const selectionStructures = Array.isArray(hierarchySelection?.structures) ? hierarchySelection.structures : [];
  const managerStructures = Array.isArray(componentManager?.currentStructures) ? componentManager.currentStructures : [];
  const fromStructureEntries = collectFromStructureEntries([...currentStructures, ...selectionStructures, ...managerStructures]);
  const merged = [
    ...fromGroups,
    ...fromHierarchy,
    ...fromSelectionHierarchy,
    ...fromManager,
    ...fromStructureEntries
  ].filter(Boolean);
  return Array.from(new Set(merged));
}

function collectStructureEntries(viewer: any): any[] {
  const plugin = viewer?.plugin;
  const hierarchyCurrent = plugin?.managers?.structure?.hierarchy?.current;
  const hierarchySelection = plugin?.managers?.structure?.hierarchy?.selection;
  const componentManager = plugin?.managers?.structure?.component;
  const currentStructures = Array.isArray(hierarchyCurrent?.structures) ? hierarchyCurrent.structures : [];
  const selectionStructures = Array.isArray(hierarchySelection?.structures) ? hierarchySelection.structures : [];
  const managerStructures = Array.isArray(componentManager?.currentStructures) ? componentManager.currentStructures : [];
  return Array.from(new Set([...currentStructures, ...selectionStructures, ...managerStructures].filter(Boolean)));
}

async function waitForStructureEntries(viewer: any, timeoutMs = 6000, intervalMs = 80): Promise<any[]> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const entries = collectStructureEntries(viewer);
    if (entries.length > 0) return entries;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return collectStructureEntries(viewer);
}

function getStructureSignature(text: string, format: 'cif' | 'pdb'): string {
  const trimmed = text.trim();
  const head = trimmed.slice(0, 128);
  const tail = trimmed.slice(-128);
  return `${format}:${trimmed.length}:${head}:${tail}`;
}

async function clearStructureComponents(viewer: any): Promise<void> {
  const plugin = viewer?.plugin;
  const manager = plugin?.managers?.structure?.component;
  const components = collectStructureComponents(viewer);
  if (!manager) return;

  if (typeof manager.clear === 'function') {
    try {
      await manager.clear();
      return;
    } catch {
      // fallback to explicit remove
    }
  }

  if (components.length > 0 && typeof manager.remove === 'function') {
    try {
      await manager.remove(components);
    } catch {
      // no-op
    }
  }
}

function describeThemeCapabilities(viewer: any): string {
  const plugin = viewer?.plugin;
  const manager = plugin?.managers?.structure?.component;
  const hierarchyCurrent = plugin?.managers?.structure?.hierarchy?.current;
  const hierarchySelection = plugin?.managers?.structure?.hierarchy?.selection;
  const hasManagerTheme = typeof manager?.updateRepresentationsTheme === 'function';
  const hasSetStyle = typeof viewer?.setStyle === 'function';
  const groupsLen = Array.isArray(hierarchyCurrent?.componentGroups) ? hierarchyCurrent.componentGroups.length : 0;
  const componentsLen = collectStructureComponents(viewer).length;
  const currentStructuresLen = Array.isArray(hierarchyCurrent?.structures) ? hierarchyCurrent.structures.length : 0;
  const selectionStructuresLen = Array.isArray(hierarchySelection?.structures) ? hierarchySelection.structures.length : 0;
  const managerStructuresLen = Array.isArray(manager?.currentStructures) ? manager.currentStructures.length : 0;
  const managerKeys = manager && typeof manager === 'object' ? Object.keys(manager).slice(0, 12).join(',') : '';
  const builder = plugin?.builders?.structure;
  const builderKeys = builder && typeof builder === 'object' ? Object.keys(builder).slice(0, 16).join(',') : '';
  return `managerTheme=${hasManagerTheme} setStyle=${hasSetStyle} groups=${groupsLen} components=${componentsLen} currentStructures=${currentStructuresLen} selectionStructures=${selectionStructuresLen} managerStructures=${managerStructuresLen} managerKeys=[${managerKeys}] builderKeys=[${builderKeys}]`;
}

async function waitForThemeComponents(viewer: any, timeoutMs = 6000, intervalMs = 80): Promise<any[]> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const components = collectStructureComponents(viewer);
    if (components.length > 0) return components;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return collectStructureComponents(viewer);
}

async function tryCreateThemeComponentsFromStructures(viewer: any): Promise<void> {
  const plugin = viewer?.plugin;
  const builders = plugin?.builders?.structure;
  const structures = collectStructureEntries(viewer);
  if (!builders || structures.length === 0) return;

  const tryCreateComponentStatic = builders?.tryCreateComponentStatic;
  if (typeof tryCreateComponentStatic !== 'function') return;

  const staticKinds = ['polymer', 'ligand', 'branched', 'water', 'all'];
  for (const entry of structures) {
    const targets = [entry, entry?.cell, entry?.structure, entry?.structure?.cell].filter(Boolean);
    for (const target of targets) {
      for (const kind of staticKinds) {
        try {
          await tryCreateComponentStatic(target, kind);
        } catch {
          // try next kind/target
        }
      }
    }
  }
}

async function tryBuildRepresentationsFromStructures(
  viewer: any,
  colorTheme: string,
  structures?: any[]
): Promise<boolean> {
  const plugin = viewer?.plugin;
  const tryCreateComponentStatic = plugin?.builders?.structure?.tryCreateComponentStatic;
  const addRepresentation = plugin?.builders?.structure?.representation?.addRepresentation;
  const entries = Array.isArray(structures) ? structures : collectStructureEntries(viewer);
  if (typeof addRepresentation !== 'function' || entries.length === 0 || typeof tryCreateComponentStatic !== 'function') {
    return false;
  }

  const staticPlan = [
    { kind: 'polymer', type: 'cartoon' },
    { kind: 'ligand', type: 'ball-and-stick' },
    { kind: 'branched', type: 'ball-and-stick' },
    { kind: 'ion', type: 'ball-and-stick' }
  ];

  const representationParamVariants = (type: string, color: string) => [
    { type, color },
    { type: { name: type }, color: { name: color } },
    { type, colorTheme: { name: color } },
    { type: { name: type }, colorTheme: { name: color } },
    { type }
  ];

  let createdAnyRepresentation = false;
  for (const entry of entries) {
    const targets = [entry, entry?.cell].filter(Boolean);
    for (const target of targets) {
      for (const plan of staticPlan) {
        let component: any = null;
        try {
          component = await tryCreateComponentStatic(target, plan.kind);
        } catch {
          component = null;
        }
        if (!component) continue;
        const componentTargets = Array.isArray(component) ? component : [component];
        for (const componentTarget of componentTargets) {
          const reprTargets = [componentTarget, componentTarget?.cell].filter(Boolean);
          for (const reprTarget of reprTargets) {
            let created = false;
            for (const params of representationParamVariants(plan.type, colorTheme)) {
              try {
                await addRepresentation(reprTarget, params);
                created = true;
                break;
              } catch {
                // try next param variant
              }
            }
            if (created) {
              createdAnyRepresentation = true;
              break;
            }
          }
        }
      }
    }
  }
  return createdAnyRepresentation;
}

async function tryApplyAlphaFoldTheme(
  viewer: any,
  confidenceBackend?: string
) {
  const plugin = viewer?.plugin;
  const manager = plugin?.managers?.structure?.component;
  const hasManagerTheme = typeof manager?.updateRepresentationsTheme === 'function';
  const backend = String(confidenceBackend || '').trim().toLowerCase();
  // Align Protenix with AF3/Boltz2: primary AlphaFold palette is always pLDDT.
  const primaryTheme = 'plddt-confidence';
  const structures = collectStructureEntries(viewer);

  // Strict mode: apply one deterministic theme per backend.
  if (hasManagerTheme) {
    let components = await waitForThemeComponents(viewer);
    if (components.length === 0) {
      await tryApplyCartoonPreset(viewer);
      components = await waitForThemeComponents(viewer, 6000, 80);
    }
    if (components.length === 0) {
      await tryCreateThemeComponentsFromStructures(viewer);
      components = await waitForThemeComponents(viewer, 3000, 80);
    }
    if (components.length === 0) {
      await tryBuildRepresentationsFromStructures(viewer, primaryTheme);
      components = await waitForThemeComponents(viewer, 2500, 80);
    }
    if (components.length === 0) {
      if (structures.length > 0) {
        try {
          await manager.updateRepresentationsTheme(structures, { color: primaryTheme });
          return;
        } catch {
          // keep strict failure with diagnostics
        }
      }
      throw new Error(
        `Unable to apply ${primaryTheme} theme: no structure components (backend=${backend || '-'}). ${describeThemeCapabilities(viewer)}`
      );
    }
    await manager.updateRepresentationsTheme(components, { color: primaryTheme });
    return;
  }

  if (typeof viewer?.setStyle === 'function') {
    viewer.setStyle({ theme: primaryTheme });
    return;
  }
  throw new Error(
    `Unable to apply ${primaryTheme} theme: missing manager theme API and viewer.setStyle API (backend=${backend || '-'}). ${describeThemeCapabilities(viewer)}`
  );
}

async function tryApplyCartoonPreset(viewer: any, structures?: any[]): Promise<boolean> {
  const plugin = viewer?.plugin;
  const entries = Array.isArray(structures) ? structures : plugin?.managers?.structure?.hierarchy?.current?.structures;
  if (!Array.isArray(entries) || entries.length === 0) {
    return false;
  }

  const applyPreset = plugin?.builders?.structure?.representation?.applyPreset;
  const hierarchyApplyPreset = plugin?.managers?.structure?.hierarchy?.applyPreset;
  const presetIds = ['polymer-and-ligand', 'polymer-cartoon'];
  let anyApplied = false;

  for (const entry of entries) {
    const target = entry?.cell ?? entry;
    if (!target) continue;
    let applied = false;
    if (typeof applyPreset === 'function') {
      for (const presetId of presetIds) {
        try {
          await applyPreset(target, presetId);
          applied = true;
          anyApplied = true;
          break;
        } catch {
          // try next preset id
        }
      }
    }
    if (applied) continue;
    if (typeof hierarchyApplyPreset === 'function') {
      for (const presetId of presetIds) {
        try {
          await hierarchyApplyPreset(target, presetId);
          applied = true;
          anyApplied = true;
          break;
        } catch {
          // try next preset id
        }
      }
    }
  }
  return anyApplied;
}

async function tryApplyWhiteTheme(viewer: any) {
  const plugin = viewer?.plugin;
  const manager = plugin?.managers?.structure?.component;
  const groups = plugin?.managers?.structure?.hierarchy?.current?.componentGroups;
  const components = Array.isArray(groups) ? groups.flat() : [];

  if (manager?.updateRepresentationsTheme && components.length > 0) {
    try {
      await manager.updateRepresentationsTheme(components, {
        color: 'uniform',
        colorParams: {
          value: 0xe8edf2
        }
      });
    } catch {
      // no-op
    }
  }

  if (typeof viewer?.setStyle === 'function') {
    try {
      viewer.setStyle({
        theme: 'uniform',
        themeParams: {
          value: 0xe8edf2
        }
      });
    } catch {
      // no-op
    }
  }

  const canvas = plugin?.canvas3d;
  if (canvas?.setProps) {
    try {
      canvas.setProps({
        renderer: { backgroundColor: 0xffffff },
        marking: {
          highlightColor: 0x6b9b84,
          selectColor: 0x2f6f57,
          edgeScale: 0.8
        }
      });
    } catch {
      // no-op
    }
  }
}

function readFirstIndex(indices: any): number | null {
  if (!indices) return null;
  const molstarLib = (window as any)?.molstar?.lib;
  const orderedSet =
    molstarLib?.molDataInt?.OrderedSet ??
    molstarLib?.molData?.int?.OrderedSet ??
    molstarLib?.molDataIntOrderedSet;

  if (orderedSet) {
    try {
      const hasSize = typeof orderedSet.size === 'function';
      const size = hasSize ? Number(orderedSet.size(indices)) : NaN;
      if (Number.isFinite(size) && size > 0) {
        if (typeof orderedSet.getAt === 'function') {
          const value = Number(orderedSet.getAt(indices, 0));
          if (Number.isFinite(value)) return value;
        }
        if (typeof orderedSet.min === 'function') {
          const value = Number(orderedSet.min(indices));
          if (Number.isFinite(value)) return value;
        }
      }
    } catch {
      // Fallback to generic index readers below.
    }
  }

  if (Array.isArray(indices) && indices.length > 0 && Number.isFinite(indices[0])) {
    return Number(indices[0]);
  }
  if (typeof indices.getAt === 'function') {
    try {
      const value = Number(indices.getAt(0));
      if (Number.isFinite(value)) return value;
    } catch {
      // no-op
    }
  }
  if (typeof indices.value === 'function') {
    try {
      const value = Number(indices.value(0));
      if (Number.isFinite(value)) return value;
    } catch {
      // no-op
    }
  }
  if (typeof indices[Symbol.iterator] === 'function') {
    const iter = indices[Symbol.iterator]();
    const first = iter.next();
    if (!first.done && Number.isFinite(first.value)) {
      return Number(first.value);
    }
  }
  if (typeof indices.start === 'number' && Number.isFinite(indices.start) && indices.start >= 0) {
    return Number(indices.start);
  }
  if (typeof indices.min === 'number' && Number.isFinite(indices.min) && indices.min >= 0) {
    return Number(indices.min);
  }
  return null;
}

function createOrderedSet(indices: number[]): any {
  const uniqueSorted = Array.from(new Set(indices.filter((value) => Number.isFinite(value) && value >= 0))).sort(
    (a, b) => a - b
  );
  if (!uniqueSorted.length) return null;

  const molstarLib = (window as any)?.molstar?.lib;
  const orderedSet =
    molstarLib?.molDataInt?.OrderedSet ??
    molstarLib?.molData?.int?.OrderedSet ??
    molstarLib?.molDataIntOrderedSet;

  try {
    if (orderedSet?.ofSortedArray) {
      return orderedSet.ofSortedArray(Int32Array.from(uniqueSorted));
    }
    if (orderedSet?.ofSingleton && uniqueSorted.length === 1) {
      return orderedSet.ofSingleton(uniqueSorted[0]);
    }
  } catch {
    // fallback below
  }

  return Int32Array.from(uniqueSorted);
}

function getStructureDataCandidates(viewer: any): any[] {
  const structures = viewer?.plugin?.managers?.structure?.hierarchy?.current?.structures;
  if (!Array.isArray(structures)) return [];

  const result: any[] = [];
  for (const entry of structures) {
    const structure = entry?.cell?.obj?.data ?? entry?.obj?.data ?? entry?.data ?? entry?.structure ?? null;
    if (structure) result.push(structure);
  }
  return result;
}

function buildResidueLociForStructure(structure: any, chainId: string, residue: number): any | null {
  if (!structure || !Array.isArray(structure.units)) return null;
  const elements: Array<{ unit: any; indices: any }> = [];

  for (const unit of structure.units) {
    const hierarchy = unit?.model?.atomicHierarchy;
    const unitElements = unit?.elements;
    if (!hierarchy || !unitElements) continue;

    const matches: number[] = [];
    const unitLength =
      typeof unitElements.length === 'number' && unitElements.length > 0
        ? unitElements.length
        : Math.max(0, Number(readFirstIndex(unitElements)) + 1);
    for (let indexInUnit = 0; indexInUnit < unitLength; indexInUnit += 1) {
      const atomIndex = Number(readIndexedValue(unitElements, indexInUnit) ?? indexInUnit);
      if (!Number.isFinite(atomIndex) || atomIndex < 0) continue;

      const residueIndex = Number(readIndexedValue(hierarchy?.residueAtomSegments?.index, atomIndex));
      const chainIndex = Number(readIndexedValue(hierarchy?.chainAtomSegments?.index, atomIndex));

      const chain =
        String(
          readIndexedValue(hierarchy?.chains?.auth_asym_id, chainIndex) ??
            readIndexedValue(hierarchy?.chains?.label_asym_id, chainIndex) ??
            ''
        ).trim() || null;
      if (!chain || chain !== chainId) continue;

      const seq = Number.parseInt(
        String(
          readIndexedValue(hierarchy?.residues?.auth_seq_id, residueIndex) ??
            readIndexedValue(hierarchy?.residues?.label_seq_id, residueIndex) ??
            readIndexedValue(hierarchy?.residues?.seq_id, residueIndex) ??
            ''
        ),
        10
      );
      if (!Number.isFinite(seq) || seq !== residue) continue;
      matches.push(indexInUnit);
    }

    if (!matches.length) continue;
    const indices = createOrderedSet(matches);
    if (!indices) continue;
    elements.push({ unit, indices });
  }

  if (!elements.length) return null;
  return {
    kind: 'element-loci',
    structure,
    elements
  };
}

function buildResidueLoci(viewer: any, chainId: string, residue: number): any | null {
  const structures = getStructureDataCandidates(viewer);
  for (const structure of structures) {
    const loci = buildResidueLociForStructure(structure, chainId, residue);
    if (loci) return loci;
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

function readIndexedValue(source: any, index: number): any {
  if (source == null || !Number.isFinite(index) || index < 0) return undefined;
  if (typeof source.value === 'function') {
    try {
      return source.value(index);
    } catch {
      // no-op
    }
  }
  if (typeof source.get === 'function') {
    try {
      return source.get(index);
    } catch {
      // no-op
    }
  }
  if (Array.isArray(source) || ArrayBuffer.isView(source)) {
    return (source as any)[index];
  }
  if (typeof source === 'object') {
    if (Object.prototype.hasOwnProperty.call(source, index)) {
      return source[index];
    }
    if ('data' in source) {
      const nested = readIndexedValue((source as Record<string, unknown>).data, index);
      if (nested !== undefined) return nested;
    }
    if ('array' in source) {
      const nested = readIndexedValue((source as Record<string, unknown>).array, index);
      if (nested !== undefined) return nested;
    }
  }
  return undefined;
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

function subscribePickEvents(
  viewer: any,
  _host: HTMLElement | null,
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

export function MolstarViewer({
  structureText,
  format,
  overlayStructureText,
  overlayFormat,
  colorMode = 'white',
  confidenceBackend,
  onResiduePick,
  pickMode = 'click',
  highlightResidues,
  activeResidue,
  lockView = false,
  suppressAutoFocus = false,
  showSequence = true
}: MolstarViewerProps) {
  const ref = useRef<HTMLDivElement | null>(null);
  const viewerRef = useRef<any>(null);
  const pickUnsubscribeRef = useRef<(() => void) | null>(null);
  const onResiduePickRef = useRef<typeof onResiduePick>(onResiduePick);
  const suppressPickEventsRef = useRef(false);
  const hadExternalHighlightsRef = useRef(false);
  const structureApplyQueueRef = useRef<Promise<void>>(Promise.resolve());
  const structureRequestIdRef = useRef(0);
  const loadedPrimarySignatureRef = useRef('');
  const loadedOverlaySignatureRef = useRef('');
  const altPressedRef = useRef(false);
  const shiftPressedRef = useRef(false);
  const ctrlPressedRef = useRef(false);
  const recentModifiedPrimaryDownRef = useRef(0);
  const [error, setError] = useState<string | null>(null);
  const [ready, setReady] = useState(false);
  const [structureReadyVersion, setStructureReadyVersion] = useState(0);
  const hasResiduePickHandler = Boolean(onResiduePick);

  useEffect(() => {
    onResiduePickRef.current = onResiduePick;
  }, [onResiduePick]);

  const emitResiduePick = useCallback((pick: MolstarResiduePick) => {
    onResiduePickRef.current?.(pick);
  }, []);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.altKey || event.key === 'Alt') {
        altPressedRef.current = true;
      }
      if (event.shiftKey || event.key === 'Shift') {
        shiftPressedRef.current = true;
      }
      if (event.ctrlKey || event.metaKey || event.key === 'Control' || event.key === 'Meta') {
        ctrlPressedRef.current = true;
      }
    };
    const onKeyUp = (event: KeyboardEvent) => {
      if (!event.altKey || event.key === 'Alt') {
        altPressedRef.current = false;
      }
      if (!event.shiftKey || event.key === 'Shift') {
        shiftPressedRef.current = false;
      }
      if ((!event.ctrlKey && !event.metaKey) || event.key === 'Control' || event.key === 'Meta') {
        ctrlPressedRef.current = false;
      }
    };
    const onPointerDown = (event: MouseEvent | PointerEvent) => {
      const isPrimary =
        event.button === 0 ||
        event.which === 1 ||
        (typeof event.buttons === 'number' && (event.buttons & 1) === 1);
      if (isPrimary && (event.altKey || event.shiftKey || event.ctrlKey || event.metaKey)) {
        recentModifiedPrimaryDownRef.current = Date.now();
      }
    };
    const onBlur = () => {
      altPressedRef.current = false;
      shiftPressedRef.current = false;
      ctrlPressedRef.current = false;
      recentModifiedPrimaryDownRef.current = 0;
    };

    window.addEventListener('keydown', onKeyDown);
    window.addEventListener('keyup', onKeyUp);
    window.addEventListener('pointerdown', onPointerDown, { capture: true, passive: true });
    window.addEventListener('blur', onBlur);
    document.addEventListener('visibilitychange', onBlur);

    return () => {
      window.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('keyup', onKeyUp);
      window.removeEventListener('pointerdown', onPointerDown, { capture: true });
      window.removeEventListener('blur', onBlur);
      document.removeEventListener('visibilitychange', onBlur);
    };
  }, []);

  useEffect(() => {
    let cancelled = false;

    const bootstrap = async () => {
      try {
        loadStylesheet(ENV.molstarCssUrl);
        await loadScript(ENV.molstarScriptUrl);
        // Mol* CSS is loaded at runtime; inject overrides after it to win style order.
        ensureMolstarThemeOverrides();

        if (cancelled || !ref.current) return;

        const creator = await waitForMolstarReady();

        viewerRef.current = await creator(ref.current, {
          layoutIsExpanded: false,
          layoutShowControls: true,
          layoutShowSequence: showSequence,
          layoutShowLog: false,
          viewportShowExpand: true,
          viewportShowSelectionMode: true,
          collapseLeftPanel: true,
          collapseRightPanel: true,
          pdbProvider: 'rcsb',
          emdbProvider: 'rcsb'
        });
        try {
          viewerRef.current?.plugin?.managers?.interactivity?.setProps?.({ granularity: 'residue' });
          if (typeof viewerRef.current?.setSelectionMode === 'function') {
            viewerRef.current.setSelectionMode(true);
          } else if ('selectionMode' in (viewerRef.current || {})) {
            viewerRef.current.selectionMode = true;
          } else {
            viewerRef.current?.plugin?.behaviors?.interaction?.selectionMode?.next?.(true);
          }
        } catch {
          // no-op
        }
        pickUnsubscribeRef.current = subscribePickEvents(
          viewerRef.current,
          ref.current,
          hasResiduePickHandler ? emitResiduePick : undefined,
          pickMode,
          () =>
            altPressedRef.current ||
            shiftPressedRef.current ||
            ctrlPressedRef.current ||
            Date.now() - recentModifiedPrimaryDownRef.current < 450,
          () => suppressPickEventsRef.current
        );
        setReady(true);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Unable to load Mol* viewer.');
      }
    };

    void bootstrap();

    return () => {
      cancelled = true;
      const viewer = viewerRef.current;
      if (pickUnsubscribeRef.current) {
        pickUnsubscribeRef.current();
        pickUnsubscribeRef.current = null;
      }
      if (viewer?.plugin?.dispose) {
        viewer.plugin.dispose();
      }
      viewerRef.current = null;
      loadedPrimarySignatureRef.current = '';
      loadedOverlaySignatureRef.current = '';
      setReady(false);
    };
  }, [showSequence]);

  useEffect(() => {
    if (!viewerRef.current) return;
    try {
      const shouldEnableSelection = true;
      if (typeof viewerRef.current?.setSelectionMode === 'function') {
        viewerRef.current.setSelectionMode(shouldEnableSelection);
      } else if ('selectionMode' in (viewerRef.current || {})) {
        viewerRef.current.selectionMode = shouldEnableSelection;
      } else {
        viewerRef.current?.plugin?.behaviors?.interaction?.selectionMode?.next?.(shouldEnableSelection);
      }
    } catch {
      // no-op
    }
    if (pickUnsubscribeRef.current) {
      pickUnsubscribeRef.current();
      pickUnsubscribeRef.current = null;
    }
    pickUnsubscribeRef.current = subscribePickEvents(
      viewerRef.current,
      ref.current,
      hasResiduePickHandler ? emitResiduePick : undefined,
      pickMode,
      () => {
        return (
          altPressedRef.current ||
          shiftPressedRef.current ||
          ctrlPressedRef.current ||
          Date.now() - recentModifiedPrimaryDownRef.current < 450
        );
      },
      () => suppressPickEventsRef.current
    );
    return () => {
      if (pickUnsubscribeRef.current) {
        pickUnsubscribeRef.current();
        pickUnsubscribeRef.current = null;
      }
    };
  }, [emitResiduePick, hasResiduePickHandler, pickMode]);

  useEffect(() => {
    if (!ready || !viewerRef.current) return;
    const requestId = structureRequestIdRef.current + 1;
    structureRequestIdRef.current = requestId;

    const run = async () => {
      if (requestId !== structureRequestIdRef.current) return;
      try {
        setError(null);
        const viewer = viewerRef.current;
        if (!viewer) return;
        const primaryText = structureText.trim();
        const overlayText = String(overlayStructureText || '').trim();
        const resolvedOverlayFormat: 'cif' | 'pdb' = overlayFormat === 'pdb' ? 'pdb' : 'cif';

        if (!primaryText) {
          if (typeof viewer.clear === 'function') {
            await viewer.clear();
          }
          loadedPrimarySignatureRef.current = '';
          loadedOverlaySignatureRef.current = '';
          if (requestId === structureRequestIdRef.current) {
            setStructureReadyVersion((prev) => prev + 1);
          }
          return;
        }

        const nextPrimarySignature = getStructureSignature(primaryText, format);
        const nextOverlaySignature = overlayText ? getStructureSignature(overlayText, resolvedOverlayFormat) : '';
        const previousPrimarySignature = loadedPrimarySignatureRef.current;
        const previousOverlaySignature = loadedOverlaySignatureRef.current;
        const primaryChanged = nextPrimarySignature !== previousPrimarySignature;
        const overlayChanged = nextOverlaySignature !== previousOverlaySignature;

        if (primaryChanged) {
          await loadStructure(viewer, primaryText, format, { clearBefore: true });
          loadedPrimarySignatureRef.current = nextPrimarySignature;
          loadedOverlaySignatureRef.current = '';
          if (requestId !== structureRequestIdRef.current) return;
          if (overlayText) {
            await loadStructure(viewer, overlayText, resolvedOverlayFormat, { clearBefore: false });
            loadedOverlaySignatureRef.current = nextOverlaySignature;
            if (requestId !== structureRequestIdRef.current) return;
          }
        } else if (overlayChanged) {
          if (!overlayText) {
            await loadStructure(viewer, primaryText, format, { clearBefore: true });
            loadedOverlaySignatureRef.current = '';
          } else if (!previousOverlaySignature) {
            await loadStructure(viewer, overlayText, resolvedOverlayFormat, { clearBefore: false });
            loadedOverlaySignatureRef.current = nextOverlaySignature;
          } else {
            await loadStructure(viewer, primaryText, format, { clearBefore: true });
            if (requestId !== structureRequestIdRef.current) return;
            await loadStructure(viewer, overlayText, resolvedOverlayFormat, { clearBefore: false });
            loadedOverlaySignatureRef.current = nextOverlaySignature;
          }
          if (requestId !== structureRequestIdRef.current) return;
        }

        const structureEntries = await waitForStructureEntries(viewer);
        if (requestId !== structureRequestIdRef.current) return;
        const presetApplied = await tryApplyCartoonPreset(viewer, structureEntries);
        if (requestId !== structureRequestIdRef.current) return;
        if (!presetApplied && colorMode === 'alphafold') {
          const supportsStaticBuilder =
            typeof viewer?.plugin?.builders?.structure?.tryCreateComponentStatic === 'function' &&
            typeof viewer?.plugin?.builders?.structure?.representation?.addRepresentation === 'function';
          if (supportsStaticBuilder) {
            await clearStructureComponents(viewer);
            await tryBuildRepresentationsFromStructures(viewer, 'plddt-confidence', structureEntries);
            if (requestId !== structureRequestIdRef.current) return;
          }
        }

        if (colorMode === 'alphafold') {
          await tryApplyAlphaFoldTheme(viewer, confidenceBackend);
        } else {
          await tryApplyWhiteTheme(viewer);
        }
        if (requestId === structureRequestIdRef.current) {
          setStructureReadyVersion((prev) => prev + 1);
        }
      } catch (e) {
        if (requestId !== structureRequestIdRef.current) return;
        setError(e instanceof Error ? e.message : 'Unable to update Mol* viewer.');
      }
    };

    structureApplyQueueRef.current = structureApplyQueueRef.current.then(run);

    return () => {
      if (structureRequestIdRef.current === requestId) {
        structureRequestIdRef.current += 1;
      }
    };
  }, [ready, structureText, format, overlayStructureText, overlayFormat, colorMode, confidenceBackend]);

  useEffect(() => {
    if (!ready || !viewerRef.current || !structureText.trim()) return;
    const viewer = viewerRef.current;
    const selectionManager = viewer?.plugin?.managers?.structure?.selection;
    const focusManager = viewer?.plugin?.managers?.structure?.focus;
    if (!selectionManager?.fromLoci || !selectionManager?.clear) return;
    const highlightSource = highlightResidues ?? EMPTY_HIGHLIGHTS;

    const normalized = Array.from(
      new Map(
        highlightSource
          .filter(
            (item) =>
              Boolean(item?.chainId) &&
              Number.isFinite(item?.residue) &&
              Math.floor(Number(item.residue)) > 0
          )
          .map((item) => [`${item.chainId}:${Math.floor(Number(item.residue))}`, { ...item, residue: Math.floor(Number(item.residue)) }])
      ).values()
    ) as MolstarResidueHighlight[];
    const hasExternalHighlights = normalized.length > 0 || Boolean(activeResidue);

    if (!hasExternalHighlights) {
      if (!hadExternalHighlightsRef.current) return;
      hadExternalHighlightsRef.current = false;
      suppressPickEventsRef.current = true;
      try {
        selectionManager.clear();
        focusManager?.clear?.();
      } catch {
        // no-op
      } finally {
        window.setTimeout(() => {
          suppressPickEventsRef.current = false;
        }, 0);
      }
      return;
    }

    hadExternalHighlightsRef.current = true;

    suppressPickEventsRef.current = true;
    try {
      selectionManager.clear();
      for (const item of normalized) {
        const loci = buildResidueLoci(viewer, item.chainId, item.residue);
        if (!loci) continue;
        selectionManager.fromLoci('add', loci);
      }

      const focusTarget = activeResidue || normalized.find((item) => item.emphasis === 'active') || null;
      if (!suppressAutoFocus && focusTarget && focusManager?.setFromLoci) {
        const focusLoci = buildResidueLoci(viewer, focusTarget.chainId, focusTarget.residue);
        if (focusLoci) {
          focusManager.setFromLoci(focusLoci);
        }
      }
    } catch {
      // no-op
    } finally {
      window.setTimeout(() => {
        suppressPickEventsRef.current = false;
      }, 0);
    }
  }, [ready, structureText, structureReadyVersion, highlightResidues, activeResidue, suppressAutoFocus]);

  if (error) {
    return <div className="alert error">{error}</div>;
  }

  return (
    <div>
      <div ref={ref} className={`molstar-host ${lockView ? 'molstar-host-locked' : ''}`} />
      {!structureText.trim() && (
        <div className="muted small top-margin">No structure loaded yet. Run prediction and refresh status after completion.</div>
      )}
      {structureText.trim() && colorMode === 'alphafold' && (
        <div className="legend-row">
          {alphafoldLegend.map((item) => (
            <div key={item.key} className="legend-item">
              <span className="legend-dot" style={{ backgroundColor: item.color }} />
              <span>{item.label}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
