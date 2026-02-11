import { useEffect, useState } from 'react';
import { loadRDKitModule } from '../../utils/rdkit';

interface Ligand2DPreviewProps {
  smiles: string;
  width?: number;
  height?: number;
  atomConfidences?: number[] | null;
}

function normalizePlddt(value: number): number {
  if (!Number.isFinite(value)) return 0;
  const normalized = value >= 0 && value <= 1 ? value * 100 : value;
  return Math.max(0, Math.min(100, normalized));
}

function colorForConfidence(value: number): [number, number, number] {
  const v = normalizePlddt(value);
  // AlphaFold confidence colors:
  // very low: #FF7D45, low: #FFDB13, confident: #65CBF3, very high: #0053D6.
  if (v < 50) return [1.0, 0.49, 0.27];
  if (v < 70) return [1.0, 0.86, 0.07];
  if (v < 90) return [0.40, 0.80, 0.95];
  return [0.00, 0.33, 0.84];
}

function buildPerAtomConfidence(values: number[] | null | undefined, atomCount: number): number[] {
  if (!Array.isArray(values) || values.length === 0 || atomCount <= 0) return [];
  const normalized = values
    .filter((value): value is number => typeof value === 'number' && Number.isFinite(value))
    .map((value) => normalizePlddt(value));
  if (normalized.length === 0) return [];
  if (normalized.length === atomCount) return normalized;
  if (normalized.length > atomCount) return normalized.slice(0, atomCount);
  const avg = normalized.reduce((sum, value) => sum + value, 0) / normalized.length;
  return [...normalized, ...Array.from({ length: atomCount - normalized.length }, () => avg)];
}

export function Ligand2DPreview({ smiles, width = 340, height = 210, atomConfidences = null }: Ligand2DPreviewProps) {
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
          let rawSvg = '';
          const atomCount = typeof mol.get_num_atoms === 'function' ? Math.max(0, Math.floor(mol.get_num_atoms())) : 0;
          const perAtomConfidence = buildPerAtomConfidence(atomConfidences, atomCount);
          if (typeof mol.get_svg_with_highlights === 'function') {
            try {
              const details: Record<string, unknown> = {
                width,
                height,
                minFontSize: 9,
                fixedFontSize: 13,
                maxFontSize: 32
              };

              if (perAtomConfidence.length > 0) {
                const highlightAtoms: number[] = [];
                const highlightAtomColors: Record<number, [number, number, number]> = {};
                const highlightAtomRadii: Record<number, number> = {};
                for (let i = 0; i < perAtomConfidence.length; i += 1) {
                  highlightAtoms.push(i);
                  highlightAtomColors[i] = colorForConfidence(perAtomConfidence[i]);
                  highlightAtomRadii[i] = 0.16;
                }
                details.atoms = highlightAtoms;
                details.highlightAtoms = highlightAtoms;
                details.highlightAtomColors = highlightAtomColors;
                details.highlightAtomRadii = highlightAtomRadii;
                details.highlightRadii = highlightAtomRadii;
                details.fillHighlights = false;
                details.atomHighlightsAreCircles = false;
                details.highlightBonds = [];
              }

              // RDKit official MolDrawOptions via get_svg_with_highlights(details JSON).
              // Carbon atoms are usually unlabeled, so increasing label font mainly affects hetero atoms.
              rawSvg = mol.get_svg_with_highlights(JSON.stringify(details));
            } catch {
              rawSvg = '';
            }
          }
          if (!rawSvg) {
            rawSvg = mol.get_svg(width, height);
          }
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
  }, [smiles, width, height, atomConfidences]);

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
