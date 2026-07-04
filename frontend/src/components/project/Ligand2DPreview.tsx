import { memo, useEffect, useMemo, useRef, useState } from 'react';
import { renderLigand2DSvg } from '../../utils/ligand2d';
import { loadRDKitModule } from '../../utils/rdkit';

interface Ligand2DPreviewProps {
  smiles: string;
  width?: number;
  height?: number;
  atomConfidences?: number[] | null;
  confidenceHint?: number | null;
  highlightQuery?: string | null;
  highlightAtomIndices?: number[] | null;
  atomLabels?: string[] | null;
  onAtomClick?: (atomIndex: number) => void;
  onBackgroundClick?: () => void;
}

function injectInteractiveSvgStyle(svg: string): string {
  const svgTagStart = svg.indexOf('<svg');
  if (svgTagStart < 0) return svg;
  const svgTagEnd = svg.indexOf('>', svgTagStart);
  if (svgTagEnd < 0) return svg;
  const svgOpenTag = svg.slice(svgTagStart, svgTagEnd + 1);
  const scopedClass = 'rdkit-ligand-preview-svg';
  const classMatch = svgOpenTag.match(/\sclass=(['"])(.*?)\1/i);
  let patchedOpenTag = svgOpenTag;
  if (classMatch) {
    const existing = String(classMatch[2] || '').trim();
    if (!existing.split(/\s+/).includes(scopedClass)) {
      patchedOpenTag = svgOpenTag.replace(classMatch[0], ` class="${`${existing} ${scopedClass}`.trim()}"`);
    }
  } else {
    patchedOpenTag = svgOpenTag.replace('<svg', `<svg class="${scopedClass}"`);
  }
  const style = [
    '<style>',
    `.${scopedClass} { cursor: pointer; }`,
    `.${scopedClass} [class*="atom-"] { cursor: pointer; }`,
    '</style>'
  ].join('');
  return `${svg.slice(0, svgTagStart)}${patchedOpenTag}${style}${svg.slice(svgTagEnd + 1)}`;
}

// A click anywhere within an adaptive radius of an atom selects that atom (nearest wins), so
// the tiny rendered glyphs (a <text> symbol or a thin highlight path) are easy to hit. The
// radius scales with the depiction: sparse molecules get a generous hit zone, dense ones stay
// small enough not to snap onto a neighbour. Falls back to null (background) far from any atom.
function findNearestAtomIndex(host: HTMLElement, clientX: number, clientY: number): number | null {
  const nodes = host.querySelectorAll('[class*="atom-"]');
  if (nodes.length === 0) return null;

  type Candidate = { index: number; x: number; y: number };
  const candidates: Candidate[] = [];
  nodes.forEach((node) => {
    const el = node as Element;
    const indices = Array.from(
      new Set(
        Array.from(String(el.getAttribute('class') || '').matchAll(/atom-(\d+)/g))
          .map((match) => Number.parseInt(match[1], 10))
          .filter((atomIndex) => Number.isFinite(atomIndex) && atomIndex >= 0)
      )
    );
    if (indices.length !== 1) return;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) return;
    candidates.push({ index: indices[0], x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 });
  });
  if (candidates.length === 0) return null;

  // Adaptive threshold from the median nearest-neighbour distance, clamped so a single
  // residue (sparse) still gets a usable zone and a large ligand (dense) doesn't over-reach.
  const neighbourDistances = candidates.map((a) => {
    const others = candidates.filter((b) => b !== a).map((b) => Math.hypot(a.x - b.x, a.y - b.y));
    return others.length ? Math.min(...others) : Number.POSITIVE_INFINITY;
  });
  const sorted = [...neighbourDistances].sort((p, q) => p - q);
  const medianNeighbour = sorted[Math.floor(sorted.length / 2)] ?? Number.POSITIVE_INFINITY;
  const threshold = Math.max(12, Math.min(30, medianNeighbour * 0.5));

  let best: { index: number; dist: number } | null = null;
  for (const candidate of candidates) {
    const dist = Math.hypot(clientX - candidate.x, clientY - candidate.y);
    if (!best || dist < best.dist) best = { index: candidate.index, dist };
  }
  return best && best.dist <= threshold ? best.index : null;
}

export function Ligand2DPreview({
  smiles,
  width = 340,
  height = 210,
  atomConfidences = null,
  confidenceHint = null,
  highlightQuery = null,
  highlightAtomIndices = null,
  atomLabels = null,
  onAtomClick,
  onBackgroundClick
}: Ligand2DPreviewProps) {
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string>('');
  const hostRef = useRef<HTMLDivElement | null>(null);

  const atomConfidenceSignature = useMemo(() => {
    if (!Array.isArray(atomConfidences) || atomConfidences.length === 0) return '';
    return atomConfidences.map((value) => Number(value).toFixed(2)).join(',');
  }, [atomConfidences]);

  const highlightAtomSignature = useMemo(() => {
    if (!Array.isArray(highlightAtomIndices) || highlightAtomIndices.length === 0) return '';
    return highlightAtomIndices.map((value) => Math.floor(Number(value) || 0)).join(',');
  }, [highlightAtomIndices]);

  const atomLabelSignature = useMemo(() => {
    if (!Array.isArray(atomLabels) || atomLabels.length === 0) return '';
    return atomLabels.map((value) => String(value || '').trim()).join('\u001f');
  }, [atomLabels]);

  useEffect(() => {
    let cancelled = false;

    const render = async () => {
      const value = smiles.trim();
      if (!value) {
        setSvg('');
        setError('');
        return;
      }

      try {
        setError('');
        const rdkit = await loadRDKitModule();
        if (cancelled) return;

        const rendered = renderLigand2DSvg(rdkit, {
          smiles: value,
          width,
          height,
          atomConfidences,
          confidenceHint,
          highlightQuery,
          highlightAtomIndices,
          atomLabels
        });
        if (cancelled) return;
        setSvg(onAtomClick || onBackgroundClick ? injectInteractiveSvgStyle(rendered) : rendered);
      } catch (e) {
        if (cancelled) return;
        setSvg('');
        setError(e instanceof Error ? e.message : 'RDKit render failed.');
      }
    };

    void render();
    return () => {
      cancelled = true;
    };
  }, [
    smiles,
    width,
    height,
    confidenceHint,
    highlightQuery,
    atomConfidenceSignature,
    highlightAtomSignature,
    atomLabelSignature,
    onAtomClick,
    onBackgroundClick
  ]);

  if (!smiles.trim()) {
    return <div className="ligand-preview-empty">No ligand input.</div>;
  }

  if (error) {
    return <div className="ligand-preview-empty">2D preview unavailable for this ligand input.</div>;
  }

  if (!svg) {
    return <div className="ligand-preview-empty">Rendering ligand 2D...</div>;
  }

  return (
    <div className="ligand-preview-svg-wrap">
      <div
        ref={hostRef}
        className="ligand-preview-svg"
        dangerouslySetInnerHTML={{ __html: svg }}
        onClick={
          onAtomClick || onBackgroundClick
            ? (event) => {
                const host = hostRef.current;
                const atomIndex = host ? findNearestAtomIndex(host, event.clientX, event.clientY) : null;
                if (atomIndex === null) {
                  onBackgroundClick?.();
                  return;
                }
                onAtomClick?.(atomIndex);
              }
            : undefined
        }
      />
    </div>
  );
}

export const MemoLigand2DPreview = memo(Ligand2DPreview);
