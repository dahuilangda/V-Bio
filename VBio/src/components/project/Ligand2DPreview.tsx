import { useEffect, useState } from 'react';
import { loadRDKitModule } from '../../utils/rdkit';

interface Ligand2DPreviewProps {
  smiles: string;
  width?: number;
  height?: number;
}

export function Ligand2DPreview({ smiles, width = 340, height = 210 }: Ligand2DPreviewProps) {
  const [svg, setSvg] = useState<string>('');
  const [error, setError] = useState<string>('');

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

        const mol = rdkit.get_mol(value);
        if (!mol) {
          throw new Error('Invalid SMILES for RDKit.');
        }

        try {
          mol.set_new_coords?.();
          mol.normalize_depiction?.();
          const rawSvg = mol.get_svg(width, height);
          if (!rawSvg) {
            throw new Error('RDKit returned empty SVG.');
          }
          if (cancelled) return;
          setSvg(rawSvg);
        } finally {
          mol.delete();
        }
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
  }, [smiles, width, height]);

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
