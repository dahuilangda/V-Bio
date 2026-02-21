import { memo, useEffect, useMemo, useState } from 'react';
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
  templateSmiles?: string | null;
  strictTemplateAlignment?: boolean;
}

export function Ligand2DPreview({
  smiles,
  width = 340,
  height = 210,
  atomConfidences = null,
  confidenceHint = null,
  highlightQuery = null,
  highlightAtomIndices = null,
  templateSmiles = null,
  strictTemplateAlignment = false
}: Ligand2DPreviewProps) {
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string>('');

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
          highlightAtomIndices,
          templateSmiles,
          strictTemplateAlignment
        });
        if (cancelled) return;
        setSvg(rendered);
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
    templateSmiles,
    strictTemplateAlignment,
    atomConfidenceSignature,
    highlightAtomSignature
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
      <div className="ligand-preview-svg" dangerouslySetInnerHTML={{ __html: svg }} />
    </div>
  );
}

export const MemoLigand2DPreview = memo(Ligand2DPreview);
