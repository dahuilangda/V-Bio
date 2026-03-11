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

export function Ligand2DPreview({
  smiles,
  width = 340,
  height = 210,
  atomConfidences = null,
  confidenceHint = null,
  highlightQuery = null,
  highlightAtomIndices = null,
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
          highlightAtomIndices
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
                const atomIndex = extractAtomIndexFromElement(event.target, hostRef.current);
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
