import { ENV } from '../../../utils/env';
import { loadScript, loadStylesheet } from '../../../utils/script';

const MOLSTAR_OVERRIDE_STYLE_ID = 'vbio-molstar-theme-overrides';

export function ensureMolstarThemeOverrides() {
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

export async function waitForMolstarReady(timeoutMs = 12000, intervalMs = 120) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const creator = (window as any).molstar?.Viewer?.create;
    if (creator) return creator;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  throw new Error('Mol* failed to initialize.');
}

// Overlay bookkeeping lives ON the viewer instance, not in module state: structure loads are
// async and outlive their component, so during a tab/page switch an outgoing viewer's queued
// loads interleave with an incoming viewer's — module-level refs let one viewer's begin/end
// pair clobber the other's, and a later removeOverlayOnly then deleted the wrong nodes.
interface OverlayTracking {
  refs: string[];
  before: Set<string> | null;
}

function overlayTrackingFor(viewer: any): OverlayTracking {
  const holder = viewer as { __vbioOverlayTracking?: OverlayTracking } | null;
  if (!holder) return { refs: [], before: null };
  if (!holder.__vbioOverlayTracking) holder.__vbioOverlayTracking = { refs: [], before: null };
  return holder.__vbioOverlayTracking;
}

/** Call BEFORE loading the overlay — records existing refs (primary's). */
export function beginOverlayLoad(viewer: any): void {
  const state = viewer?.plugin?.state?.data;
  overlayTrackingFor(viewer).before = state?.cells ? new Set(state.cells.keys()) : new Set();
}

/** Call AFTER loading the overlay — diffs to find ONLY the new overlay refs. */
export function endOverlayLoad(viewer: any): void {
  const tracking = overlayTrackingFor(viewer);
  const state = viewer?.plugin?.state?.data;
  if (!state?.cells || !tracking.before) {
    tracking.refs = [];
    tracking.before = null;
    return;
  }
  const before = tracking.before;
  tracking.refs = [...state.cells.keys()].filter(ref => !before.has(ref));
  tracking.before = null;
}

/** Remove ONLY the overlay component's state nodes, preserving the primary. */
export async function removeOverlayOnly(viewer: any): Promise<void> {
  const state = viewer?.plugin?.state?.data;
  if (!state?.cells || typeof state.build !== 'function') {
    throw new Error('removeOverlayOnly: state API unavailable');
  }
  const tracking = overlayTrackingFor(viewer);
  if (tracking.refs.length === 0) return;

  const builder = state.build();
  let deleted = 0;
  for (const ref of tracking.refs) {
    if (state.cells.has(ref)) {
      builder.delete(ref);
      deleted++;
    }
  }
  if (deleted > 0) {
    await builder.commit();
  }
  tracking.refs = [];
}

// Backwards-compatible aliases
export function snapshotPrimaryLoaded(viewer?: any): void {
  if (!viewer) return;
  overlayTrackingFor(viewer).refs = [];
}

export function trackOverlayLoaded(viewer: any): void {
  endOverlayLoad(viewer);
}

/**
 * Remove ALL loaded structures from the MolStar plugin state.
 */
export async function clearViewerStructures(viewer: any): Promise<void> {
  const state = viewer?.plugin?.state?.data;
  if (!state?.cells || typeof state.build !== 'function') {
    throw new Error(
      'clearViewerStructures: plugin.state.data.cells or .build unavailable. ' +
      `Got cells=${typeof state?.cells}, build=${typeof state?.build}. ` +
      'The MolStar Viewer API has changed.'
    );
  }

  overlayTrackingFor(viewer).refs = [];
  const rootRef = '-=root=-';
  const builder = state.build();
  let deleted = 0;
  for (const cell of state.cells.values()) {
    const ref = cell?.transform?.ref;
    if (ref && ref !== rootRef) {
      builder.delete(ref);
      deleted++;
    }
  }
  if (deleted > 0) {
    await builder.commit();
  }
}

export async function loadStructure(
  viewer: any,
  text: string,
  format: 'cif' | 'pdb',
  options?: { clearBefore?: boolean }
) {
  const clearBefore = options?.clearBefore !== false;
  if (clearBefore) {
    await clearViewerStructures(viewer);
  }
  // gemmi-serialized PDB previews carry SEQRES whose non-standard caps (ACE etc.)
  // break this Mol* build's polymer entity mapping — the whole protein then
  // falls back to an all-atom ball-and-stick instead of cartoon. SEQRES is
  // display-redundant here (Mol* derives the sequence from ATOM records).
  if (format === 'pdb' && /^SEQRES /m.test(text)) {
    text = text
      .split('\n')
      .filter((line) => !line.startsWith('SEQRES '))
      .join('\n');
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

export function getStructureSignature(text: string, format: 'cif' | 'pdb'): string {
  const trimmed = text.trim();
  const head = trimmed.slice(0, 128);
  const tail = trimmed.slice(-128);
  return `${format}:${trimmed.length}:${head}:${tail}`;
}

export async function bootstrapViewerHost(
  host: HTMLElement,
  showSequence: boolean
): Promise<any> {
  loadStylesheet(ENV.molstarCssUrl);
  await loadScript(ENV.molstarScriptUrl);
  ensureMolstarThemeOverrides();
  const creator = await waitForMolstarReady();
  return await creator(host, {
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
}
