import {
  clearStructureComponents,
  tryApplyElementSymbolThemeToCurrentScene,
  tryApplyLeadOptSceneStyle,
  tryApplyLeadOptResultsInteractionTheme,
  tryApplyAlphaFoldTheme,
  tryApplyCartoonPreset,
  tryBuildRepresentationsFromStructures,
  waitForStructureEntries
} from '../theme';

export type MolstarResolvedColorMode = 'alphafold' | 'default';

interface ApplyStructureAppearancePipelineArgs {
  viewer: any;
  colorMode: string;
  confidenceBackend?: string;
  scenePreset: 'default' | 'lead_opt';
  leadOptStyleVariant: 'default' | 'results';
  suppressAutoFocus: boolean;
  autoFocusLigand: boolean;
  focusLigandAnchor: (viewer: any) => boolean;
  isRequestCurrent: () => boolean;
}

const LEAD_OPT_RESULTS_POLYMER_ALPHA = 0.62;
const LEAD_OPT_RESULTS_POLYMER_ALPHA_AF = 0.66;

function resolveMolstarColorMode(mode: string): MolstarResolvedColorMode {
  return mode === 'alphafold' ? 'alphafold' : 'default';
}

function hasStaticRepresentationBuilder(viewer: any): boolean {
  return (
    typeof viewer?.plugin?.builders?.structure?.tryCreateComponentStatic === 'function' &&
    typeof viewer?.plugin?.builders?.structure?.representation?.addRepresentation === 'function'
  );
}

function getLeadOptColorTheme(
  resolvedColorMode: MolstarResolvedColorMode,
  leadOptStyleVariant: 'default' | 'results'
): string {
  if (resolvedColorMode === 'alphafold') return 'plddt-confidence';
  return leadOptStyleVariant === 'results' ? 'element-symbol' : 'chain-id';
}

function trySetElementSymbolStyle(viewer: any) {
  if (typeof viewer?.setStyle !== 'function') return;
  try {
    viewer.setStyle({ theme: 'element-symbol' });
  } catch {
    // no-op
  }
}

function clearMolstarFocusState(viewer: any) {
  try {
    viewer?.plugin?.managers?.structure?.focus?.clear?.();
  } catch {
    // no-op
  }
  try {
    viewer?.plugin?.managers?.interactivity?.lociHighlights?.clearHighlights?.();
  } catch {
    // no-op
  }
}

export async function applyStructureAppearancePipeline({
  viewer,
  colorMode,
  confidenceBackend,
  scenePreset,
  leadOptStyleVariant,
  suppressAutoFocus,
  autoFocusLigand,
  focusLigandAnchor,
  isRequestCurrent
}: ApplyStructureAppearancePipelineArgs): Promise<void> {
  const structureEntries = await waitForStructureEntries(viewer);
  if (!isRequestCurrent()) return;
  const resolvedColorMode = resolveMolstarColorMode(colorMode);

  if (scenePreset === 'lead_opt') {
    const isResultsVariant = leadOptStyleVariant === 'results';
    if (isResultsVariant) {
      if (resolvedColorMode === 'alphafold') {
        if (hasStaticRepresentationBuilder(viewer)) {
          await clearStructureComponents(viewer);
          await tryBuildRepresentationsFromStructures(viewer, 'plddt-confidence', structureEntries);
          if (!isRequestCurrent()) return;
        }
        await tryApplyAlphaFoldTheme(viewer, confidenceBackend);
        if (!isRequestCurrent()) return;
      } else {
        // De-flicker: immediately clear any lingering AF confidence tint on the
        // currently visible scene before async representation rebuild kicks in.
        await tryApplyElementSymbolThemeToCurrentScene(viewer);
        if (!isRequestCurrent()) return;
        await tryApplyLeadOptResultsInteractionTheme(viewer);
        if (!isRequestCurrent()) return;
        trySetElementSymbolStyle(viewer);
      }
      await tryApplyLeadOptSceneStyle(viewer, {
        polymerAlpha: resolvedColorMode === 'alphafold' ? LEAD_OPT_RESULTS_POLYMER_ALPHA_AF : LEAD_OPT_RESULTS_POLYMER_ALPHA,
        ligandAlpha: 1.0,
        pocketStickScale: 0.52
      });
      if (!isRequestCurrent()) return;
      if (autoFocusLigand && !suppressAutoFocus) {
        focusLigandAnchor(viewer);
        if (resolvedColorMode !== 'alphafold') {
          // Focus selection can create transient interaction sticks; clear them in fragment mode.
          clearMolstarFocusState(viewer);
          await tryApplyElementSymbolThemeToCurrentScene(viewer);
          if (!isRequestCurrent()) return;
        }
      }
      return;
    }

    const presetApplied = await tryApplyCartoonPreset(viewer, structureEntries);
    if (!isRequestCurrent()) return;
    if (hasStaticRepresentationBuilder(viewer)) {
      await clearStructureComponents(viewer);
      await tryBuildRepresentationsFromStructures(
        viewer,
        getLeadOptColorTheme(resolvedColorMode, leadOptStyleVariant),
        structureEntries,
        [
          { kind: 'polymer', type: 'cartoon' },
          { kind: 'ligand', type: 'ball-and-stick' },
          { kind: 'branched', type: 'ball-and-stick' },
          { kind: 'ion', type: 'ball-and-stick' }
        ]
      );
      if (!isRequestCurrent()) return;
    }
    if (!presetApplied && resolvedColorMode === 'alphafold' && hasStaticRepresentationBuilder(viewer)) {
      await clearStructureComponents(viewer);
      await tryBuildRepresentationsFromStructures(viewer, 'plddt-confidence', structureEntries);
      if (!isRequestCurrent()) return;
    }
    if (resolvedColorMode === 'alphafold') {
      await tryApplyAlphaFoldTheme(viewer, confidenceBackend);
      if (!isRequestCurrent()) return;
    }
    await tryApplyLeadOptSceneStyle(viewer, {
      polymerAlpha:
        resolvedColorMode === 'alphafold'
          ? isResultsVariant
            ? LEAD_OPT_RESULTS_POLYMER_ALPHA_AF
            : 0.36
          : isResultsVariant
            ? LEAD_OPT_RESULTS_POLYMER_ALPHA
            : 0.36,
      ligandAlpha: 1.0,
      pocketStickScale: isResultsVariant ? 0.52 : 0.62
    });
    if (!isRequestCurrent()) return;
    if (autoFocusLigand && !suppressAutoFocus) {
      focusLigandAnchor(viewer);
    }
    return;
  }

  const presetApplied = await tryApplyCartoonPreset(viewer, structureEntries);
  if (!isRequestCurrent()) return;
  if (!presetApplied && resolvedColorMode === 'alphafold' && hasStaticRepresentationBuilder(viewer)) {
    await clearStructureComponents(viewer);
    await tryBuildRepresentationsFromStructures(viewer, 'plddt-confidence', structureEntries);
    if (!isRequestCurrent()) return;
  }
  if (resolvedColorMode === 'alphafold') {
    await tryApplyAlphaFoldTheme(viewer, confidenceBackend);
    if (!isRequestCurrent()) return;
  } else {
    await tryApplyElementSymbolThemeToCurrentScene(viewer);
    if (!isRequestCurrent()) return;
  }
  if (autoFocusLigand && !suppressAutoFocus) {
    focusLigandAnchor(viewer);
  }
}
