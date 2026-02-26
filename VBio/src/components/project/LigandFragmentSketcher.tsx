import { useEffect, useMemo, useRef, useState } from 'react';
import { loadRDKitModule } from '../../utils/rdkit';
import { MemoLigand2DPreview } from './Ligand2DPreview';

export interface LigandFragmentItem {
  fragment_id: string;
  smiles: string;
  display_smiles?: string;
  atom_indices: number[];
  heavy_atoms: number;
  attachment_count?: number;
  num_frags?: number;
  recommended_action: string;
  color: string;
  rule_coverage: number;
  quality_score: number;
}

interface LigandFragmentSketcherProps {
  smiles: string;
  fragments: LigandFragmentItem[];
  selectedFragmentIds: string[];
  activeFragmentId: string;
  onAtomClick: (atomIndex: number, options?: { additive?: boolean; preferredFragmentId?: string }) => void;
  onFragmentClick?: (fragmentId: string, options?: { additive?: boolean }) => void;
  onBackgroundClick?: () => void;
  width?: number;
  height?: number;
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function hexToRgb01(value: string): [number, number, number] {
  const hex = String(value || '').trim().replace('#', '');
  if (hex.length === 6) {
    const r = Number.parseInt(hex.slice(0, 2), 16);
    const g = Number.parseInt(hex.slice(2, 4), 16);
    const b = Number.parseInt(hex.slice(4, 6), 16);
    if (Number.isFinite(r) && Number.isFinite(g) && Number.isFinite(b)) {
      return [clamp01(r / 255), clamp01(g / 255), clamp01(b / 255)];
    }
  }
  return [0.57, 0.65, 0.62];
}

function blendWithWhite(color: [number, number, number], amount: number): [number, number, number] {
  const a = clamp01(amount);
  const mix = (channel: number) => clamp01(channel * (1 - a) + a);
  return [mix(color[0]), mix(color[1]), mix(color[2])];
}

function injectSvgStyle(svg: string): string {
  const svgTagStart = svg.indexOf('<svg');
  if (svgTagStart < 0) return svg;
  const svgTagEnd = svg.indexOf('>', svgTagStart);
  if (svgTagEnd < 0) return svg;
  const svgOpenTag = svg.slice(svgTagStart, svgTagEnd + 1);
  const scopedClass = 'rdkit-fragment-map-svg';
  const classMatch = svgOpenTag.match(/\sclass=(['"])(.*?)\1/i);
  let patchedOpenTag = svgOpenTag;
  if (classMatch) {
    const existing = String(classMatch[2] || '').trim();
    if (!existing.split(/\s+/).includes(scopedClass)) {
      const nextClass = `${existing} ${scopedClass}`.trim();
      patchedOpenTag = svgOpenTag.replace(classMatch[0], ` class="${nextClass}"`);
    }
  } else {
    patchedOpenTag = svgOpenTag.replace('<svg', `<svg class="${scopedClass}"`);
  }
  const style = [
    '<style>',
    `.${scopedClass} { background: #fff; }`,
    `.${scopedClass} .bond-0, .${scopedClass} .bond-1, .${scopedClass} .bond-2, .${scopedClass} .bond-3, .${scopedClass} .bond-4 { cursor: pointer; }`,
    `.${scopedClass} [class*="atom-"] { cursor: pointer; }`,
    '</style>'
  ].join('');
  return `${svg.slice(0, svgTagStart)}${patchedOpenTag}${style}${svg.slice(svgTagEnd + 1)}`;
}

function extractAtomIndexFromElement(target: EventTarget | null, boundary: HTMLElement | null): number | null {
  let node = target as HTMLElement | null;
  while (node && node !== boundary) {
    const className = String(node.getAttribute('class') || '');
    const match = className.match(/atom-(\d+)/);
    if (match) {
      const atomIndex = Number.parseInt(match[1], 10);
      if (Number.isFinite(atomIndex) && atomIndex >= 0) return atomIndex;
    }
    node = node.parentElement;
  }
  return null;
}

export function LigandFragmentSketcher({
  smiles,
  fragments,
  selectedFragmentIds,
  activeFragmentId,
  onAtomClick,
  onFragmentClick,
  onBackgroundClick,
  width,
  height = 300
}: LigandFragmentSketcherProps) {
  const [svg, setSvg] = useState('');
  const [renderError, setRenderError] = useState('');
  const rootRef = useRef<HTMLDivElement | null>(null);
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [autoWidth, setAutoWidth] = useState(460);
  const renderWidth =
    typeof width === 'number' && Number.isFinite(width) && width > 0 ? Math.floor(width) : autoWidth;

  const fragmentMap = useMemo(() => {
    const map = new Map<string, LigandFragmentItem>();
    fragments.forEach((item) => map.set(String(item.fragment_id || ''), item));
    return map;
  }, [fragments]);

  const selectedSet = useMemo(
    () => new Set(selectedFragmentIds.map((item) => String(item || '').trim()).filter(Boolean)),
    [selectedFragmentIds]
  );

  const atomDisplayMap = useMemo(() => {
    const map = new Map<number, { color: [number, number, number]; radius: number; priority: number; fragmentId: string }>();
    const hasSelection = selectedSet.size > 0;
    fragments.forEach((fragment, fragmentOrder) => {
      const fragmentId = String(fragment.fragment_id || '').trim();
      if (!fragmentId) return;
      const rawColor = hexToRgb01(fragment.color || '#93a8a0');
      const isActive = fragmentId === activeFragmentId;
      const isSelected = selectedSet.has(fragmentId);
      const color: [number, number, number] = isActive
        ? rawColor
        : isSelected
          ? blendWithWhite(rawColor, 0.08)
          : hasSelection
            ? blendWithWhite(rawColor, 0.82)
            : blendWithWhite(rawColor, 0.56);
      const radius = isActive ? 0.32 : isSelected ? 0.24 : hasSelection ? 0.11 : 0.16;
      const priority =
        (isActive ? 4000 : isSelected ? 2600 : 1000) +
        (fragment.quality_score || 0) * 10 -
        fragmentOrder * 0.01;
      fragment.atom_indices.forEach((atomIndexRaw) => {
        const atomIndex = Number(atomIndexRaw);
        if (!Number.isFinite(atomIndex) || atomIndex < 0) return;
        const prev = map.get(atomIndex);
        if (!prev || priority > prev.priority) {
          map.set(atomIndex, { color, radius, priority, fragmentId });
        }
      });
    });
    return map;
  }, [activeFragmentId, fragments, selectedSet]);

  useEffect(() => {
    if (typeof width === 'number' && Number.isFinite(width) && width > 0) return;
    const element = rootRef.current;
    if (!element) return;
    const updateWidth = () => {
      const next = Math.max(260, Math.floor(element.clientWidth) - 2);
      setAutoWidth((prev) => (Math.abs(prev - next) > 1 ? next : prev));
    };
    updateWidth();
    if (typeof ResizeObserver !== 'undefined') {
      const observer = new ResizeObserver(() => updateWidth());
      observer.observe(element);
      return () => observer.disconnect();
    }
    window.addEventListener('resize', updateWidth);
    return () => window.removeEventListener('resize', updateWidth);
  }, [width]);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      const value = smiles.trim();
      if (!value) {
        setSvg('');
        setRenderError('');
        return;
      }
      try {
        setRenderError('');
        const rdkit = await loadRDKitModule();
        if (cancelled) return;
        const mol = rdkit.get_mol(value);
        if (!mol) {
          throw new Error('Invalid ligand SMILES.');
        }
        try {
          mol.set_new_coords?.();
          mol.normalize_depiction?.();
          let rendered = '';
          if (typeof mol.get_svg_with_highlights === 'function') {
            const details: Record<string, unknown> = {
              width: renderWidth,
              height,
              minFontSize: 9,
              fixedFontSize: 12,
              maxFontSize: 32,
              fillHighlights: true,
              atomHighlightsAreCircles: true
            };
            const highlightAtoms: number[] = [];
            const highlightAtomColors: Record<number, [number, number, number]> = {};
            const highlightAtomRadii: Record<number, number> = {};
            atomDisplayMap.forEach((entry, atomIndex) => {
              highlightAtoms.push(atomIndex);
              highlightAtomColors[atomIndex] = entry.color;
              highlightAtomRadii[atomIndex] = entry.radius;
            });

            if (highlightAtoms.length > 0) {
              details.atoms = highlightAtoms;
              details.highlightAtoms = highlightAtoms;
              details.highlightAtomColors = highlightAtomColors;
              details.highlightAtomRadii = highlightAtomRadii;
              details.highlightRadii = highlightAtomRadii;
              details.highlightBonds = [];
            }
            rendered = mol.get_svg_with_highlights(JSON.stringify(details)) || '';
          }
          if (!rendered) {
            rendered = mol.get_svg(renderWidth, height);
          }
          if (!rendered) {
            throw new Error('RDKit returned empty SVG.');
          }
          if (!cancelled) {
            setSvg(injectSvgStyle(rendered));
          }
        } finally {
          mol.delete();
        }
      } catch (e) {
        if (cancelled) return;
        setSvg('');
        setRenderError(e instanceof Error ? e.message : 'RDKit render failed.');
      }
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [smiles, renderWidth, height, atomDisplayMap]);

  if (!smiles.trim()) {
    return <div className="ligand-preview-empty">No ligand SMILES available. Upload a small-molecule reference ligand (SDF/MOL2) or provide ligand SMILES.</div>;
  }

  if (renderError) {
    return <div className="ligand-preview-empty">{renderError}</div>;
  }

  return (
    <div ref={rootRef} className="lead-opt-fragment-sketcher">
      {svg ? (
        <div
          ref={hostRef}
          className="lead-opt-fragment-sketcher-svg"
          dangerouslySetInnerHTML={{ __html: svg }}
          onClick={(event) => {
            const atomIndex = extractAtomIndexFromElement(event.target, hostRef.current);
            if (atomIndex === null) {
              onBackgroundClick?.();
              return;
            }
            const additive = event.altKey ? false : event.metaKey || event.ctrlKey || event.shiftKey ? true : undefined;
            const preferredFragmentId = atomDisplayMap.get(atomIndex)?.fragmentId;
            onAtomClick(atomIndex, { additive, preferredFragmentId });
          }}
        />
      ) : (
        <div className="ligand-preview-empty">Rendering ligand fragments...</div>
      )}
      <div className="lead-opt-fragment-legend">
        {fragments.slice(0, 10).map((fragment) => {
          const fragmentId = String(fragment.fragment_id || '');
          const isSelected = selectedFragmentIds.includes(fragmentId);
          const isActive = fragmentId && fragmentId === activeFragmentId;
          const swatchColor = fragment.color || '#93a8a0';
          const item = fragmentMap.get(fragmentId);
          const tooltipSmiles = item?.display_smiles || item?.smiles || '';
          const previewSmiles = String(item?.display_smiles || item?.smiles || '').trim();
          return (
            <button
              key={fragmentId || tooltipSmiles}
              type="button"
              className={`lead-opt-fragment-legend-item ${isSelected ? 'selected' : ''} ${isActive ? 'active' : ''}`}
              title={tooltipSmiles}
              style={
                isActive
                  ? { borderColor: swatchColor, boxShadow: `0 0 0 2px ${swatchColor}33 inset` }
                  : isSelected
                    ? { borderColor: swatchColor }
                    : undefined
              }
              onClick={(event) => {
                const additive = event.altKey ? false : event.metaKey || event.ctrlKey || event.shiftKey ? true : undefined;
                onFragmentClick?.(fragmentId, { additive });
              }}
            >
              <span className="lead-opt-fragment-legend-preview" aria-hidden="true">
                {previewSmiles ? (
                  <MemoLigand2DPreview smiles={previewSmiles} width={56} height={30} templateSmiles={smiles} />
                ) : (
                  <span className="lead-opt-fragment-legend-preview-empty" />
                )}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
