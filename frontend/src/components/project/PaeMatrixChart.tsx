import { useEffect, useRef } from 'react';

interface PaeMatrixChartProps {
  matrix: number[][];
  chainBounds?: Array<{ chain: number; start: number; end: number }>;
  maxPae?: number;
  size?: number;
}

const PAE_MAX_DEFAULT = 25;

function paeColor(value: number, max: number): string {
  // AF3 风格色带：0Å 蓝 → maxÅ 红
  const t = Math.min(1, Math.max(0, value / max));
  const r = Math.round(20 + 235 * t);
  const g = Math.round(80 + 60 * Math.abs(t - 0.5));
  const b = Math.round(240 - 210 * t);
  return `rgb(${r},${g},${b})`;
}

export function PaeMatrixChart({ matrix, chainBounds, maxPae = PAE_MAX_DEFAULT, size = 300 }: PaeMatrixChartProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || matrix.length === 0) return;
    const ctx = canvas.getContext('2d');
    if (!ctx) return;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = size * dpr;
    canvas.height = size * dpr;
    canvas.style.width = `${size}px`;
    canvas.style.height = `${size}px`;
    ctx.scale(dpr, dpr);

    const n = matrix.length;
    const cell = size / n;
    for (let i = 0; i < n; i += 1) {
      for (let j = 0; j < n; j += 1) {
        ctx.fillStyle = paeColor(matrix[i][j] ?? maxPae, maxPae);
        ctx.fillRect(j * cell, i * cell, cell + 0.5, cell + 0.5);
      }
    }
    if (chainBounds && chainBounds.length > 1) {
      ctx.strokeStyle = 'rgba(255,255,255,0.9)';
      ctx.lineWidth = 1.5;
      let acc = 0;
      chainBounds.forEach((b, idx) => {
        acc += b.end - b.start + 1;
        if (idx === chainBounds.length - 1) return;
        const pos = (acc / n) * size;
        ctx.beginPath();
        ctx.moveTo(pos, 0);
        ctx.lineTo(pos, size);
        ctx.moveTo(0, pos);
        ctx.lineTo(size, pos);
        ctx.stroke();
        ctx.fillStyle = 'rgba(255,255,255,0.95)';
        ctx.font = '11px sans-serif';
        ctx.fillText(`链${b.chain}`, acc - (b.end - b.start) / 2 + 2, 12);
      });
      ctx.fillStyle = 'rgba(255,255,255,0.95)';
      ctx.font = '11px sans-serif';
      const last = chainBounds[chainBounds.length - 1];
      ctx.fillText(`链${last.chain}`, size - 40, 12);
    }
  }, [matrix, chainBounds, maxPae, size]);

  if (!matrix.length) return null;
  return (
    <div className="pae-matrix-chart">
      <canvas ref={canvasRef} aria-label="PAE matrix heatmap" />
      <div className="pae-matrix-legend">
        <span>0Å</span>
        <span
          className="pae-matrix-legend-bar"
          style={{
            background: 'linear-gradient(90deg, rgb(20,80,240), rgb(120,140,140), rgb(255,140,30))',
          }}
        />
        <span>25Å</span>
      </div>
    </div>
  );
}
