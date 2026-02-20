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
  const merged = [...fromGroups, ...fromHierarchy, ...fromSelectionHierarchy, ...fromManager, ...fromStructureEntries].filter(Boolean);
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

export async function waitForStructureEntries(viewer: any, timeoutMs = 6000, intervalMs = 80): Promise<any[]> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const entries = collectStructureEntries(viewer);
    if (entries.length > 0) return entries;
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }
  return collectStructureEntries(viewer);
}

export async function clearStructureComponents(viewer: any): Promise<void> {
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

export async function tryBuildRepresentationsFromStructures(
  viewer: any,
  colorTheme: string,
  structures?: any[],
  planOverride?: Array<{ kind: string; type: string }>
): Promise<boolean> {
  const plugin = viewer?.plugin;
  const tryCreateComponentStatic = plugin?.builders?.structure?.tryCreateComponentStatic;
  const addRepresentation = plugin?.builders?.structure?.representation?.addRepresentation;
  const entries = Array.isArray(structures) ? structures : collectStructureEntries(viewer);
  if (typeof addRepresentation !== 'function' || entries.length === 0 || typeof tryCreateComponentStatic !== 'function') {
    return false;
  }

  const staticPlan = Array.isArray(planOverride) && planOverride.length > 0
    ? planOverride
    : [
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

function isLikelyLigandComponent(component: any): boolean {
  const label = String(
    component?.cell?.obj?.label ??
      component?.label ??
      component?.key ??
      component?.params?.values?.type?.name ??
      ''
  )
    .toLowerCase()
    .trim();
  if (!label) return false;
  return (
    label.includes('ligand') ||
    label.includes('small molecule') ||
    label.includes('non-polymer') ||
    label.includes('het')
  );
}

async function updateRepresentationAlpha(components: any[], manager: any, alpha: number): Promise<boolean> {
  if (!manager || !Array.isArray(components) || components.length === 0) return false;
  const alphaValue = Math.max(0.02, Math.min(1, alpha));
  const variants = [
    { typeParams: { alpha: alphaValue } },
    { type: { params: { alpha: alphaValue } } },
    { alpha: alphaValue },
    { reprParams: { alpha: alphaValue } }
  ];
  const fns = [
    manager.updateRepresentations,
    manager.updateRepresentation,
    manager.setRepresentations,
    manager.setRepresentation
  ].filter((fn) => typeof fn === 'function');
  for (const fn of fns) {
    for (const payload of variants) {
      try {
        await fn.call(manager, components, payload);
        return true;
      } catch {
        // try next
      }
    }
  }
  return false;
}

export async function tryApplyLeadOptSceneStyle(
  viewer: any,
  options?: { polymerAlpha?: number; ligandAlpha?: number }
): Promise<void> {
  const plugin = viewer?.plugin;
  const manager = plugin?.managers?.structure?.component;
  const components = collectStructureComponents(viewer);
  if (!manager || components.length === 0) return;

  const ligandComponents = components.filter(isLikelyLigandComponent);
  const proteinComponents = ligandComponents.length > 0
    ? components.filter((component) => !isLikelyLigandComponent(component))
    : components;

  const polymerAlpha = Number.isFinite(Number(options?.polymerAlpha)) ? Number(options?.polymerAlpha) : 0.3;
  const ligandAlpha = Number.isFinite(Number(options?.ligandAlpha)) ? Number(options?.ligandAlpha) : 1.0;
  if (proteinComponents.length > 0) {
    await updateRepresentationAlpha(proteinComponents, manager, polymerAlpha);
  }
  if (ligandComponents.length > 0) {
    await updateRepresentationAlpha(ligandComponents, manager, ligandAlpha);
  }

  const canvas = plugin?.canvas3d;
  if (canvas?.setProps) {
    try {
      canvas.setProps({
        renderer: { backgroundColor: 0xffffff },
        marking: {
          highlightColor: 0x748d84,
          selectColor: 0x5f7f74,
          edgeScale: 0.65
        }
      });
    } catch {
      // no-op
    }
  }
}

export function tryFocusLikelyLigand(viewer: any): boolean {
  const focusManager = viewer?.plugin?.managers?.structure?.focus;
  if (!focusManager?.setFromLoci) return false;
  const components = collectStructureComponents(viewer);
  if (!Array.isArray(components) || components.length === 0) return false;
  const ligandComponent = components.find(isLikelyLigandComponent);
  if (!ligandComponent) return false;
  const loci =
    ligandComponent?.cell?.obj?.data?.loci ??
    ligandComponent?.obj?.data?.loci ??
    ligandComponent?.loci;
  if (!loci) return false;
  try {
    focusManager.setFromLoci(loci);
    return true;
  } catch {
    return false;
  }
}

export async function tryApplyAlphaFoldTheme(
  viewer: any,
  confidenceBackend?: string
): Promise<void> {
  const plugin = viewer?.plugin;
  const manager = plugin?.managers?.structure?.component;
  const hasManagerTheme = typeof manager?.updateRepresentationsTheme === 'function';
  const backend = String(confidenceBackend || '').trim().toLowerCase();
  const primaryTheme = 'plddt-confidence';
  const structures = collectStructureEntries(viewer);

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

export async function tryApplyCartoonPreset(viewer: any, structures?: any[]): Promise<boolean> {
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

export async function tryApplyWhiteTheme(viewer: any): Promise<void> {
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
