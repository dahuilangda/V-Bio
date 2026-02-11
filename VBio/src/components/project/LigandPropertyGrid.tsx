import { useEffect, useState } from 'react';
import { loadRDKitModule } from '../../utils/rdkit';

interface LigandPropertyGridProps {
  smiles: string;
  variant?: 'grid' | 'radar';
}

interface LigandPropsState {
  mw: number | null;
  logP: number | null;
  tpsa: number | null;
  hba: number | null;
  hbd: number | null;
  rotB: number | null;
}

const EMPTY_PROPS: LigandPropsState = {
  mw: null,
  logP: null,
  tpsa: null,
  hba: null,
  hbd: null,
  rotB: null
};

type Tone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';

function pickNumber(obj: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = obj[key];
    if (typeof value === 'number' && Number.isFinite(value)) return value;
  }
  return null;
}

function formatValue(value: number | null, digits = 2): string {
  if (value === null) return '-';
  return value.toFixed(digits);
}

function inRange(value: number, min: number, max: number): boolean {
  return value >= min && value <= max;
}

function toneForMw(value: number | null): Tone {
  if (value === null) return 'neutral';
  if (inRange(value, 200, 500)) return 'excellent';
  if (inRange(value, 150, 550)) return 'good';
  if (inRange(value, 100, 650)) return 'medium';
  return 'low';
}

function toneForLogP(value: number | null): Tone {
  if (value === null) return 'neutral';
  if (inRange(value, 1, 3.5)) return 'excellent';
  if (inRange(value, 0, 5)) return 'good';
  if (inRange(value, -1, 6)) return 'medium';
  return 'low';
}

function toneForTpsa(value: number | null): Tone {
  if (value === null) return 'neutral';
  if (inRange(value, 20, 120)) return 'excellent';
  if (inRange(value, 10, 140)) return 'good';
  if (inRange(value, 0, 160)) return 'medium';
  return 'low';
}

function toneForHba(value: number | null): Tone {
  if (value === null) return 'neutral';
  if (value <= 8) return 'excellent';
  if (value <= 10) return 'good';
  if (value <= 12) return 'medium';
  return 'low';
}

function toneForHbd(value: number | null): Tone {
  if (value === null) return 'neutral';
  if (value <= 3) return 'excellent';
  if (value <= 5) return 'good';
  if (value <= 7) return 'medium';
  return 'low';
}

function toneForRotB(value: number | null): Tone {
  if (value === null) return 'neutral';
  if (value <= 6) return 'excellent';
  if (value <= 10) return 'good';
  if (value <= 14) return 'medium';
  return 'low';
}

function clamp01(value: number): number {
  if (!Number.isFinite(value)) return 0;
  return Math.max(0, Math.min(1, value));
}

function scoreBand(value: number | null, excellent: [number, number], good: [number, number], medium: [number, number]): number {
  if (value === null || !Number.isFinite(value)) return 0.18;
  const [eMin, eMax] = excellent;
  const [gMin, gMax] = good;
  const [mMin, mMax] = medium;
  if (value >= eMin && value <= eMax) return 1;
  if (value >= gMin && value <= gMax) return 0.78;
  if (value >= mMin && value <= mMax) return 0.55;
  return 0.28;
}

function scoreUpperBound(value: number | null, excellentMax: number, goodMax: number, mediumMax: number): number {
  if (value === null || !Number.isFinite(value)) return 0.18;
  if (value <= excellentMax) return 1;
  if (value <= goodMax) return 0.78;
  if (value <= mediumMax) return 0.55;
  return 0.28;
}

function pointForRadar(center: number, radius: number, ratio: number, index: number, total: number) {
  const angle = -Math.PI / 2 + (index * Math.PI * 2) / total;
  const x = center + Math.cos(angle) * radius * clamp01(ratio);
  const y = center + Math.sin(angle) * radius * clamp01(ratio);
  return `${x.toFixed(2)},${y.toFixed(2)}`;
}

function axisLabelPosition(center: number, radius: number, index: number, total: number) {
  const angle = -Math.PI / 2 + (index * Math.PI * 2) / total;
  const x = center + Math.cos(angle) * radius * 1.15;
  const y = center + Math.sin(angle) * radius * 1.15;
  const c = Math.cos(angle);
  const s = Math.sin(angle);
  const textAnchor: 'start' | 'middle' | 'end' = c > 0.25 ? 'start' : c < -0.25 ? 'end' : 'middle';
  const dominantBaseline: 'alphabetic' | 'middle' | 'hanging' = s > 0.35 ? 'hanging' : s < -0.35 ? 'alphabetic' : 'middle';
  return {
    x,
    y,
    textAnchor,
    dominantBaseline
  };
}

export function LigandPropertyGrid({ smiles, variant = 'grid' }: LigandPropertyGridProps) {
  const [props, setProps] = useState<LigandPropsState>(EMPTY_PROPS);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const text = smiles.trim();
    if (!text) {
      setProps(EMPTY_PROPS);
      setLoading(false);
      return;
    }

    const run = async () => {
      setLoading(true);
      try {
        const rdkit = await loadRDKitModule();
        if (cancelled) return;
        const mol = rdkit.get_mol(text);
        if (!mol) {
          setProps(EMPTY_PROPS);
          return;
        }
        try {
          const raw = mol.get_descriptors?.();
          const parsed =
            typeof raw === 'string'
              ? (JSON.parse(raw) as Record<string, unknown>)
              : raw && typeof raw === 'object'
                ? (raw as Record<string, unknown>)
                : {};

          setProps({
            mw: pickNumber(parsed, ['amw', 'MolWt', 'molwt', 'mw']),
            logP: pickNumber(parsed, ['CrippenClogP', 'MolLogP', 'logp', 'LogP']),
            tpsa: pickNumber(parsed, ['tpsa', 'TPSA']),
            hba: pickNumber(parsed, ['NumHBA', 'hba', 'HBA']),
            hbd: pickNumber(parsed, ['NumHBD', 'hbd', 'HBD']),
            rotB: pickNumber(parsed, ['NumRotatableBonds', 'nRotB', 'rotB', 'rotatable_bonds'])
          });
        } catch {
          setProps(EMPTY_PROPS);
        } finally {
          mol.delete();
        }
      } catch {
        if (!cancelled) {
          setProps(EMPTY_PROPS);
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    };

    void run();
    return () => {
      cancelled = true;
    };
  }, [smiles]);

  if (!smiles.trim()) {
    return <div className="ligand-prop-empty">No ligand SMILES.</div>;
  }

  if (loading) {
    return <div className="ligand-prop-empty">Calculating properties...</div>;
  }

  const cards = [
    { label: 'MW', value: props.mw, digits: 2, tone: toneForMw(props.mw) },
    { label: 'LogP', value: props.logP, digits: 2, tone: toneForLogP(props.logP) },
    { label: 'TPSA', value: props.tpsa, digits: 2, tone: toneForTpsa(props.tpsa) },
    { label: 'HBA', value: props.hba, digits: 0, tone: toneForHba(props.hba) },
    { label: 'HBD', value: props.hbd, digits: 0, tone: toneForHbd(props.hbd) },
    { label: 'RotB', value: props.rotB, digits: 0, tone: toneForRotB(props.rotB) }
  ];

  if (variant === 'radar') {
    const size = 236;
    const center = size / 2;
    const radius = 82;
    const radarScores = [
      scoreBand(props.mw, [200, 500], [150, 550], [100, 650]),
      scoreBand(props.logP, [1, 3.5], [0, 5], [-1, 6]),
      scoreBand(props.tpsa, [20, 120], [10, 140], [0, 160]),
      scoreUpperBound(props.hba, 8, 10, 12),
      scoreUpperBound(props.hbd, 3, 5, 7),
      scoreUpperBound(props.rotB, 6, 10, 14)
    ];
    const points = radarScores.map((score, index) => pointForRadar(center, radius, score, index, radarScores.length)).join(' ');
    const axisPoints = radarScores.map((_, index) => pointForRadar(center, radius, 1, index, radarScores.length));
    const ringScales = [1, 0.75, 0.5, 0.25];

    return (
      <div className="ligand-radar-shell">
        <svg className="ligand-radar" viewBox={`0 0 ${size} ${size}`} role="img" aria-label="Ligand property radar chart">
          <defs>
            <linearGradient id="ligand-radar-fill" x1="0" y1="0" x2="1" y2="1">
              <stop offset="0%" stopColor="rgba(0, 83, 214, 0.35)" />
              <stop offset="100%" stopColor="rgba(101, 203, 243, 0.2)" />
            </linearGradient>
          </defs>
          {ringScales.map((scale) => (
            <polygon
              key={`ring-${scale}`}
              className="ligand-radar-ring"
              points={axisPoints
                .map((_, index) => pointForRadar(center, radius, scale, index, radarScores.length))
                .join(' ')}
            />
          ))}
          {axisPoints.map((p, index) => {
            const [x, y] = p.split(',').map((v) => Number(v));
            return <line key={`axis-${cards[index].label}`} className="ligand-radar-axis" x1={center} y1={center} x2={x} y2={y} />;
          })}
          {cards.map((card, index) => {
            const pos = axisLabelPosition(center, radius, index, radarScores.length);
            return (
              <text
                key={`label-${card.label}`}
                className="ligand-radar-axis-label"
                x={pos.x}
                y={pos.y}
                textAnchor={pos.textAnchor}
                dominantBaseline={pos.dominantBaseline}
              >
                {card.label}
              </text>
            );
          })}
          <polygon className="ligand-radar-area" points={points} />
          <polyline className="ligand-radar-stroke" points={points} />
          {radarScores.map((score, index) => {
            const [x, y] = pointForRadar(center, radius, score, index, radarScores.length).split(',').map((v) => Number(v));
            return <circle key={`dot-${cards[index].label}`} className="ligand-radar-dot" cx={x} cy={y} r={3.8} />;
          })}
        </svg>
        <div className="ligand-radar-chips">
          {cards.map((card) => (
            <div key={card.label} className="ligand-radar-chip">
              <span>{card.label}</span>
              <strong className={`ligand-prop-value metric-value-${card.tone}`}>{formatValue(card.value, card.digits)}</strong>
            </div>
          ))}
        </div>
      </div>
    );
  }

  return (
    <div className="ligand-prop-grid">
      {cards.map((card) => (
        <div key={card.label} className="ligand-prop-item">
          <span>{card.label}</span>
          <strong className={`ligand-prop-value metric-value-${card.tone}`}>{formatValue(card.value, card.digits)}</strong>
        </div>
      ))}
    </div>
  );
}
