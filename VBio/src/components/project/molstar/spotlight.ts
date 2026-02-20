function collectStructureComponents(viewer: any): any[] {
  const plugin = viewer?.plugin;
  const hierarchyCurrent = plugin?.managers?.structure?.hierarchy?.current;
  const hierarchySelection = plugin?.managers?.structure?.hierarchy?.selection;
  const componentManager = plugin?.managers?.structure?.component;
  const fromGroups = Array.isArray(hierarchyCurrent?.componentGroups) ? hierarchyCurrent.componentGroups.flat() : [];
  const fromHierarchy = Array.isArray(hierarchyCurrent?.components) ? hierarchyCurrent.components : [];
  const fromSelectionHierarchy = Array.isArray(hierarchySelection?.components) ? hierarchySelection.components : [];
  const fromManager = Array.isArray(componentManager?.components) ? componentManager.components : [];
  const merged = [...fromGroups, ...fromHierarchy, ...fromSelectionHierarchy, ...fromManager].filter(Boolean);
  return Array.from(new Set(merged));
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

async function updateRepresentationAlpha(manager: any, components: any[], alpha: number): Promise<boolean> {
  if (!manager || !Array.isArray(components) || components.length === 0) return false;
  const alphaValue = Math.max(0.02, Math.min(1, alpha));
  const variants = [
    { typeParams: { alpha: alphaValue } },
    { type: { params: { alpha: alphaValue } } },
    { alpha: alphaValue },
    { reprParams: { alpha: alphaValue } }
  ];
  const funcs = [
    manager.updateRepresentations,
    manager.updateRepresentation,
    manager.setRepresentations,
    manager.setRepresentation
  ].filter((fn) => typeof fn === 'function');
  for (const fn of funcs) {
    for (const payload of variants) {
      try {
        await fn.call(manager, components, payload);
        return true;
      } catch {
        // try next variant
      }
    }
  }
  return false;
}

export async function applyLigandSpotlight(viewer: any, enabled: boolean, dimAlpha = 0.18): Promise<void> {
  const plugin = viewer?.plugin;
  const canvas = plugin?.canvas3d;
  const interactivity = plugin?.managers?.interactivity;
  const lociHighlights = interactivity?.lociHighlights;
  const componentManager = plugin?.managers?.structure?.component;
  const allComponents = collectStructureComponents(viewer);
  const ligandComponents = allComponents.filter(isLikelyLigandComponent);
  const dimComponents = ligandComponents.length > 0
    ? allComponents.filter((component) => !isLikelyLigandComponent(component))
    : allComponents;
  const baseMarking = {
    highlightColor: 0x6b9b84,
    selectColor: 0x2f6f57,
    edgeScale: 0.8
  };
  if (!enabled) {
    if (canvas?.setProps) {
      try {
        canvas.setProps({
          renderer: { backgroundColor: 0xffffff },
          marking: baseMarking
        });
      } catch {
        // no-op
      }
    }
    if (allComponents.length > 0) {
      await updateRepresentationAlpha(componentManager, allComponents, 1.0);
    }
    if (lociHighlights?.clearHighlights) {
      try {
        lociHighlights.clearHighlights();
      } catch {
        // no-op
      }
    }
    return;
  }

  if (dimComponents.length > 0) {
    await updateRepresentationAlpha(componentManager, dimComponents, dimAlpha);
  }

  if (canvas?.setProps) {
    try {
      canvas.setProps({
        renderer: { backgroundColor: 0xffffff },
        marking: {
          ...baseMarking,
          edgeScale: 1.3,
          ghostEdgeStrength: 1.0
        }
      });
    } catch {
      // no-op
    }
  }
}
