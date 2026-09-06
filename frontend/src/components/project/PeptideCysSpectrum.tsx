import { useMemo } from 'react';

export interface PeptideCysSpectrumProps {
  lengthMin: number;
  lengthMax: number;
  /** 1-based anchors per candidate length, or null when the layout cannot fit. */
  resolve: (length: number) => [number, number, number] | null;
  referenceLength?: number;
}

const CYS_COLORS: Record<1 | 2 | 3, string> = {
  1: '#6fa98a',
  2: '#77a3bf',
  3: '#bf936a'
};

/**
 * Length-spectrum preview: one slim "candidate peptide" per length across the
 * configured [min, max] range, with the three Cys anchors plotted at their
 * relative positions. Shows at a glance how each layout mode responds to the
 * length range (rigid ring block riding the C-term, scaling ratios, or fixed
 * absolute positions that fall off short candidates).
 */
export function PeptideCysSpectrum({
  lengthMin,
  lengthMax,
  resolve,
  referenceLength
}: PeptideCysSpectrumProps) {
  const columns = useMemo(() => {
    const rows: Array<{ length: number; anchors: [number, number, number] | null }> = [];
    const lo = Math.max(5, Math.floor(lengthMin));
    const hi = Math.min(120, Math.floor(lengthMax));
    for (let length = lo; length <= hi; length += 1) {
      rows.push({ length, anchors: resolve(length) });
    }
    return rows;
  }, [lengthMin, lengthMax, resolve]);

  if (columns.length === 0) return null;

  return (
    <div className="peptide-cys-spectrum" role="img"
      aria-label={`Cys anchor positions for candidate lengths ${lengthMin} to ${lengthMax}`}>
      <div className="peptide-cys-spectrum-head">
        <span className="peptide-cys-spectrum-title">
          Candidates {columns[0].length}–{columns[columns.length - 1].length} aa
        </span>
        <span className="peptide-cys-spectrum-legend">
          {([1, 2, 3] as const).map((slot) => (
            <span key={slot} className="peptide-cys-spectrum-legend-item">
              <i style={{ background: CYS_COLORS[slot] }} />
              Cys {slot}
            </span>
          ))}
        </span>
      </div>
      <div className="peptide-cys-spectrum-track">
        {columns.map(({ length, anchors }) => {
          const isReference = referenceLength != null && length === referenceLength;
          return (
            <div
              key={length}
              className={`peptide-cys-spectrum-col ${anchors ? '' : 'is-infeasible'} ${isReference ? 'is-reference' : ''}`}
              title={`Length ${length} aa${anchors ? ` — Cys at ${anchors.join(', ')}` : ' — layout does not fit'}`}
            >
              <div className="peptide-cys-spectrum-bar">
                {anchors?.map((pos, idx) => (
                  <i
                    key={`${idx}-${pos}`}
                    className="peptide-cys-spectrum-dot"
                    style={{
                      left: `${((pos / length) * 100).toFixed(2)}%`,
                      top: `${(((idx + 0.5) / 3) * 100).toFixed(2)}%`,
                      background: CYS_COLORS[(idx + 1) as 1 | 2 | 3]
                    }}
                  />
                ))}
              </div>
            </div>
          );
        })}
      </div>
      <div className="peptide-cys-spectrum-axis">
        <span>{columns[0].length}</span>
        <span>peptide length (aa)</span>
        <span>{columns[columns.length - 1].length}</span>
      </div>
    </div>
  );
}
