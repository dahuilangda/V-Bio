import { useEffect, useState } from 'react';
import { loadRDKitModule } from '../../utils/rdkit';

interface LigandPropertyGridProps {
  smiles: string;
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

export function LigandPropertyGrid({ smiles }: LigandPropertyGridProps) {
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
