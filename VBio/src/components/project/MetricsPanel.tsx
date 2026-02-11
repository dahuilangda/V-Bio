import { CircleAlert } from 'lucide-react';
import { useMemo } from 'react';

interface MetricsPanelProps {
  title: string;
  data: Record<string, unknown>;
  chainIds?: string[];
  selectedTargetChainId?: string | null;
  selectedLigandChainId?: string | null;
}

function flattenObject(obj: Record<string, unknown>, prefix = ''): Array<{ key: string; value: string }> {
  const out: Array<{ key: string; value: string }> = [];

  Object.entries(obj).forEach(([key, value]) => {
    const nextKey = prefix ? `${prefix}.${key}` : key;

    if (typeof value === 'number') {
      out.push({ key: nextKey, value: Number.isFinite(value) ? value.toFixed(4) : String(value) });
      return;
    }

    if (typeof value === 'string' || typeof value === 'boolean') {
      out.push({ key: nextKey, value: String(value) });
      return;
    }

    if (Array.isArray(value)) {
      const serialized = value
        .slice(0, 4)
        .map((v) => (typeof v === 'number' ? v.toFixed(3) : String(v)))
        .join(', ');
      out.push({ key: nextKey, value: value.length > 4 ? `${serialized} ...` : serialized });
      return;
    }

    if (value && typeof value === 'object') {
      out.push(...flattenObject(value as Record<string, unknown>, nextKey));
    }
  });

  return out;
}

function toNumber(value: unknown): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return value;
}

function readByPath(data: Record<string, unknown>, path: string): unknown {
  const segments = path.split('.');
  let current: unknown = data;
  for (const segment of segments) {
    if (!current || typeof current !== 'object') return undefined;
    current = (current as Record<string, unknown>)[segment];
  }
  return current;
}

function pickNumber(data: Record<string, unknown>, keys: string[]): number | null {
  for (const key of keys) {
    const value = toNumber(readByPath(data, key));
    if (value !== null) return value;
  }
  return null;
}

type Tone = 'excellent' | 'good' | 'medium' | 'low' | 'neutral';

function normalizePlddt(value: number | null): number | null {
  if (value === null) return null;
  if (value >= 0 && value <= 1) return value * 100;
  return value;
}

function toneForScale(value: number | null, scale: 'plddt' | 'probability' | 'iptm' | 'pae' | 'fraction'): Tone {
  if (value === null) return 'neutral';

  if (scale === 'plddt') {
    const normalized = normalizePlddt(value);
    if (normalized === null) return 'neutral';
    if (normalized >= 90) return 'excellent';
    if (normalized >= 70) return 'good';
    if (normalized >= 50) return 'medium';
    return 'low';
  }

  if (scale === 'iptm') {
    const normalized = value <= 1 ? value : value / 100;
    if (normalized >= 0.8) return 'excellent';
    if (normalized >= 0.6) return 'good';
    if (normalized >= 0.4) return 'medium';
    return 'low';
  }

  if (scale === 'probability') {
    const pct = value <= 1 ? value * 100 : value;
    if (pct >= 90) return 'excellent';
    if (pct >= 70) return 'good';
    if (pct >= 50) return 'medium';
    return 'low';
  }

  if (scale === 'pae') {
    if (value <= 5) return 'excellent';
    if (value <= 10) return 'good';
    if (value <= 20) return 'medium';
    return 'low';
  }

  if (value <= 0.1) return 'excellent';
  if (value <= 0.2) return 'good';
  if (value <= 0.4) return 'medium';
  return 'low';
}

function formatMetricValue(value: number | null, digits = 3): string {
  if (value === null) return '-';
  return value.toFixed(digits);
}

function mean(values: number[]): number {
  if (!values.length) return Number.NaN;
  return values.reduce((acc, value) => acc + value, 0) / values.length;
}

function std(values: number[]): number {
  if (values.length <= 1) return 0;
  const m = mean(values);
  const variance = values.reduce((acc, value) => acc + (value - m) ** 2, 0) / values.length;
  return Math.sqrt(Math.max(0, variance));
}

function collectNumericMetrics(data: Record<string, unknown>, keys: string[]): number[] {
  const values: number[] = [];
  for (const key of keys) {
    const value = toNumber(readByPath(data, key));
    if (value !== null) values.push(value);
  }
  return values;
}

function pickNumberArray(data: Record<string, unknown>, keys: string[]): number[] {
  for (const key of keys) {
    const value = readByPath(data, key);
    if (!Array.isArray(value)) continue;
    const numbers = value.filter((item): item is number => typeof item === 'number' && Number.isFinite(item));
    if (numbers.length > 0) return numbers;
  }
  return [];
}

function normalizeProbability(value: number | null): number | null {
  if (value === null) return null;
  if (value > 1 && value <= 100) return value / 100;
  return value;
}

function readPairIptmForChains(
  data: Record<string, unknown>,
  chainA: string | null | undefined,
  chainB: string | null | undefined,
  fallbackChainIds: string[]
): number | null {
  if (!chainA || !chainB) return null;

  const pairMap = readByPath(data, 'pair_chains_iptm');
  if (pairMap && typeof pairMap === 'object' && !Array.isArray(pairMap)) {
    const byChain = pairMap as Record<string, unknown>;
    const rowA = byChain[chainA];
    const rowB = byChain[chainB];
    const v1 =
      rowA && typeof rowA === 'object' && !Array.isArray(rowA)
        ? normalizeProbability(toNumber((rowA as Record<string, unknown>)[chainB]))
        : null;
    const v2 =
      rowB && typeof rowB === 'object' && !Array.isArray(rowB)
        ? normalizeProbability(toNumber((rowB as Record<string, unknown>)[chainA]))
        : null;
    if (v1 !== null || v2 !== null) {
      return Math.max(v1 ?? Number.NEGATIVE_INFINITY, v2 ?? Number.NEGATIVE_INFINITY);
    }
  }

  const pairMatrix = readByPath(data, 'chain_pair_iptm');
  if (!Array.isArray(pairMatrix)) return null;

  const chainsFromData = readByPath(data, 'chain_ids');
  const chainIds =
    Array.isArray(chainsFromData) && chainsFromData.every((x) => typeof x === 'string')
      ? (chainsFromData as string[])
      : fallbackChainIds;
  const i = chainIds.findIndex((value) => value === chainA);
  const j = chainIds.findIndex((value) => value === chainB);
  if (i < 0 || j < 0) return null;
  const rowI = pairMatrix[i];
  const rowJ = pairMatrix[j];
  const m1 = Array.isArray(rowI) ? normalizeProbability(toNumber(rowI[j])) : null;
  const m2 = Array.isArray(rowJ) ? normalizeProbability(toNumber(rowJ[i])) : null;
  if (m1 !== null || m2 !== null) {
    return Math.max(m1 ?? Number.NEGATIVE_INFINITY, m2 ?? Number.NEGATIVE_INFINITY);
  }
  return null;
}

function readChainMeanPlddtForChain(data: Record<string, unknown>, chainId: string | null | undefined): number | null {
  if (!chainId) return null;
  const map = readByPath(data, 'chain_mean_plddt');
  if (!map || typeof map !== 'object' || Array.isArray(map)) return null;
  const value = toNumber((map as Record<string, unknown>)[chainId]);
  if (value === null) return null;
  return value >= 0 && value <= 1 ? value * 100 : value;
}

function toneForIc50(value: number | null): Tone {
  if (value === null) return 'neutral';
  if (value <= 0.1) return 'excellent';
  if (value <= 1) return 'good';
  if (value <= 10) return 'medium';
  return 'low';
}

function formatIc50(value: number | null): string {
  if (value === null) return '-';
  if (value >= 1000) return `${value.toFixed(0)} uM`;
  if (value >= 10) return `${value.toFixed(1)} uM`;
  return `${value.toFixed(2)} uM`;
}

function BindingPanel({ data }: { data: Record<string, unknown> }) {
  const logIc50Values = collectNumericMetrics(data, ['affinity_pred_value', 'affinity_pred_value1', 'affinity_pred_value2']);
  const bindingProbValuesRaw = collectNumericMetrics(data, [
    'affinity_probability_binary',
    'affinity_probability_binary1',
    'affinity_probability_binary2'
  ]);
  const bindingProbValues = bindingProbValuesRaw
    .map((value) => (value > 1 && value <= 100 ? value / 100 : value))
    .filter((value) => Number.isFinite(value) && value >= 0 && value <= 1);

  const meanLogIc50 = logIc50Values.length > 0 ? mean(logIc50Values) : null;
  const logIc50Std = logIc50Values.length > 1 ? std(logIc50Values) : 0;
  const ic50Um = meanLogIc50 === null ? null : 10 ** meanLogIc50;
  const ic50Lower = meanLogIc50 === null || logIc50Std <= 0 ? null : 10 ** (meanLogIc50 - logIc50Std);
  const ic50Upper = meanLogIc50 === null || logIc50Std <= 0 ? null : 10 ** (meanLogIc50 + logIc50Std);
  const ic50Plus = ic50Um !== null && ic50Upper !== null ? Math.max(0, ic50Upper - ic50Um) : null;
  const ic50Minus = ic50Um !== null && ic50Lower !== null ? Math.max(0, ic50Um - ic50Lower) : null;
  const bindingProbability = bindingProbValues.length > 0 ? Math.max(0, Math.min(1, mean(bindingProbValues))) : null;
  const bindingStd = bindingProbValues.length > 1 ? std(bindingProbValues) : null;

  return (
    <div className="confidence-grid">
      <div className="metric-item">
        <div className="metric-key">
          {renderHelp('IC50 (uM)', 'Estimated inhibitory concentration. Lower values indicate stronger predicted binding.')}
        </div>
        <div className={`metric-value metric-value-${toneForIc50(ic50Um)}`}>{formatIc50(ic50Um)}</div>
        {ic50Plus !== null && ic50Minus !== null && (
          <div className="muted small">{`+${formatIc50(ic50Plus)} / -${formatIc50(ic50Minus)}`}</div>
        )}
      </div>
      <div className="metric-item">
        <div className="metric-key">
          {renderHelp('Binding Probability', 'Predicted chance of target-ligand binding. Higher values are better.')}
        </div>
        <div className={`metric-value metric-value-${toneForScale(bindingProbability, 'probability')}`}>
          {bindingProbability === null ? '-' : `${(bindingProbability * 100).toFixed(1)}%`}
        </div>
        <div className="muted small">{bindingStd === null ? '± -' : `± ${(bindingStd * 100).toFixed(1)}%`}</div>
      </div>
    </div>
  );
}

interface PairRow {
  chainA: string;
  chainB: string;
  value: number;
}

function extractPairIptmRows(data: Record<string, unknown>, fallbackChainIds: string[]): PairRow[] {
  const rows: PairRow[] = [];
  const pairMap = readByPath(data, 'pair_chains_iptm');
  if (pairMap && typeof pairMap === 'object' && !Array.isArray(pairMap)) {
    const best = new Map<string, PairRow>();
    for (const [chainA, chainBMap] of Object.entries(pairMap as Record<string, unknown>)) {
      if (!chainBMap || typeof chainBMap !== 'object' || Array.isArray(chainBMap)) continue;
      for (const [chainB, raw] of Object.entries(chainBMap as Record<string, unknown>)) {
        const value = toNumber(raw);
        if (value === null || chainA === chainB) continue;
        const [a, b] = chainA < chainB ? [chainA, chainB] : [chainB, chainA];
        const key = `${a}|${b}`;
        const current = best.get(key);
        if (!current || value > current.value) {
          best.set(key, { chainA: a, chainB: b, value });
        }
      }
    }
    return Array.from(best.values()).sort((a, b) => b.value - a.value);
  }

  const pairMatrix = readByPath(data, 'chain_pair_iptm');
  if (!Array.isArray(pairMatrix)) return rows;

  const chainsFromData = readByPath(data, 'chain_ids');
  const chainIds =
    Array.isArray(chainsFromData) && chainsFromData.every((x) => typeof x === 'string')
      ? (chainsFromData as string[])
      : fallbackChainIds;

  for (let i = 0; i < pairMatrix.length; i += 1) {
    const row = pairMatrix[i];
    if (!Array.isArray(row)) continue;
    for (let j = i + 1; j < row.length; j += 1) {
      const value = toNumber(row[j]);
      if (value === null) continue;
      const chainA = chainIds[i] || `Chain ${i + 1}`;
      const chainB = chainIds[j] || `Chain ${j + 1}`;
      rows.push({ chainA, chainB, value });
    }
  }

  return rows.sort((a, b) => b.value - a.value);
}

function renderHelp(label: string, description: string) {
  return (
    <span className="metric-label">
      {label}
      <span className="metric-help-wrap">
        <span className="metric-help" aria-label={description}>
          <CircleAlert size={13} />
        </span>
        <span className="metric-tooltip">{description}</span>
      </span>
    </span>
  );
}

function ConfidencePanel({
  data,
  chainIds,
  selectedTargetChainId,
  selectedLigandChainId
}: {
  data: Record<string, unknown>;
  chainIds: string[];
  selectedTargetChainId?: string | null;
  selectedLigandChainId?: string | null;
}) {
  const ligandAtomPlddts = pickNumberArray(data, [
    'ligand_atom_plddts',
    'ligand_atom_plddt',
    'ligand.atom_plddts',
    'ligand.atom_plddt',
    'ligand_confidence.atom_plddts'
  ]).map((value) => (value >= 0 && value <= 1 ? value * 100 : value));
  const ligandMeanPlddt = ligandAtomPlddts.length > 0 ? mean(ligandAtomPlddts) : null;
  const selectedLigandChainPlddt = readChainMeanPlddtForChain(data, selectedLigandChainId);
  const meanPlddt =
    selectedLigandChainPlddt ??
    ligandMeanPlddt ??
    normalizePlddt(
      pickNumber(data, ['ligand_mean_plddt', 'ligand_plddt', 'complex_iplddt', 'complex_plddt_protein', 'complex_plddt'])
    );
  const selectedPairIptm = readPairIptmForChains(data, selectedTargetChainId, selectedLigandChainId, chainIds);
  const iptm = selectedPairIptm ?? pickNumber(data, ['iptm']);
  const ptm = pickNumber(data, ['ptm']);
  const pae = pickNumber(data, ['complex_pde', 'complex_pae']);
  const rankingScore = pickNumber(data, ['ranking_score']);
  const fractionDisordered = pickNumber(data, ['fraction_disordered']);

  const pairRows = useMemo(() => extractPairIptmRows(data, chainIds), [data, chainIds]);

  const cards = [
    {
      label: 'Mean pLDDT',
      help:
        selectedLigandChainPlddt !== null
          ? 'Selected ligand component confidence from 0 to 100. Blue is higher confidence.'
          : ligandMeanPlddt !== null
            ? 'Ligand atom confidence from 0 to 100. Blue is higher confidence.'
            : 'Per-residue confidence from 0 to 100. Blue is higher confidence.',
      value: meanPlddt,
      display: meanPlddt === null ? '-' : `${meanPlddt.toFixed(2)}`,
      scale: 'plddt' as const
    },
    {
      label: 'ipTM',
      help:
        selectedPairIptm !== null
          ? 'Selected target-ligand component pair interface confidence (0 to 1). Higher is better.'
          : 'Interface confidence between chains (0 to 1). Higher is better.',
      value: iptm,
      display: formatMetricValue(iptm, 4),
      scale: 'iptm' as const
    },
    {
      label: 'pTM',
      help: 'Global fold confidence (0 to 1). Higher is better.',
      value: ptm,
      display: formatMetricValue(ptm, 4),
      scale: 'probability' as const
    },
    {
      label: 'PAE (A)',
      help: 'Expected alignment error in angstrom. Lower is better.',
      value: pae,
      display: formatMetricValue(pae, 2),
      scale: 'pae' as const
    },
    {
      label: 'Ranking Score',
      help: 'AlphaFold3 ranking score. Higher values generally rank better.',
      value: rankingScore,
      display: formatMetricValue(rankingScore, 4),
      scale: 'probability' as const
    },
    {
      label: 'Fraction Disordered',
      help: 'Predicted disordered proportion (0 to 1). Lower means more ordered structure.',
      value: fractionDisordered,
      display: formatMetricValue(fractionDisordered, 3),
      scale: 'fraction' as const
    }
  ].filter((item) => item.value !== null || ['Mean pLDDT', 'ipTM', 'pTM', 'PAE (A)'].includes(item.label));

  return (
    <>
      <div className="confidence-legend">
        <span className="confidence-legend-item">
          <span className="confidence-dot confidence-dot-excellent" />
          Very high
        </span>
        <span className="confidence-legend-item">
          <span className="confidence-dot confidence-dot-good" />
          High
        </span>
        <span className="confidence-legend-item">
          <span className="confidence-dot confidence-dot-medium" />
          Medium
        </span>
        <span className="confidence-legend-item">
          <span className="confidence-dot confidence-dot-low" />
          Low
        </span>
      </div>

      <div className="confidence-grid">
        {cards.map((card) => {
          const tone = toneForScale(card.value, card.scale);
          const toneClass = tone === 'neutral' ? 'confidence-dot-neutral' : `confidence-dot-${tone}`;
          return (
            <div key={card.label} className="confidence-kv-row">
              <div className="confidence-kv-key">
                <span className={`confidence-dot ${toneClass}`} />
                {renderHelp(card.label, card.help)}
              </div>
              <div className={`metric-value metric-value-${tone}`}>{card.display}</div>
            </div>
          );
        })}
      </div>

      {pairRows.length > 0 && (
        <div className="confidence-pair-block">
          <div className="confidence-pair-head">
            {renderHelp('Pair ipTM', 'Interface confidence for each chain pair. Higher is better.')}
            <span className="muted small">{pairRows.length} pairs</span>
          </div>
          <div className="confidence-pair-list">
            {pairRows.slice(0, 24).map((row) => {
              const toneForPair = toneForScale(row.value, 'iptm');
              const pct = Math.max(0, Math.min(100, (row.value <= 1 ? row.value : row.value / 100) * 100));
              return (
                <div key={`${row.chainA}-${row.chainB}`} className={`confidence-pair-line tone-${toneForPair}`}>
                  <span className="confidence-pair-label">{`${row.chainA} ↔ ${row.chainB}`}</span>
                  <div className="confidence-pair-meter">
                    <span style={{ width: `${pct.toFixed(1)}%` }} />
                  </div>
                  <strong className="confidence-pair-value">{row.value.toFixed(4)}</strong>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </>
  );
}

export function MetricsPanel({
  title,
  data,
  chainIds = [],
  selectedTargetChainId = null,
  selectedLigandChainId = null
}: MetricsPanelProps) {
  const rows = useMemo(() => flattenObject(data).slice(0, 16), [data]);

  if (title === 'Confidence') {
    return (
      <section className="panel metrics-panel">
        <h3>{title}</h3>
        {Object.keys(data || {}).length === 0 ? (
          <div className="muted">No metrics available yet.</div>
        ) : (
          <ConfidencePanel
            data={data}
            chainIds={chainIds}
            selectedTargetChainId={selectedTargetChainId}
            selectedLigandChainId={selectedLigandChainId}
          />
        )}
      </section>
    );
  }

  if (title === 'Affinity' || title === 'Binding') {
    return (
      <section className="panel metrics-panel">
        <h3>Binding</h3>
        {Object.keys(data || {}).length === 0 ? <div className="muted">No metrics available yet.</div> : <BindingPanel data={data} />}
      </section>
    );
  }

  return (
    <section className="panel metrics-panel">
      <h3>{title}</h3>
      {rows.length === 0 ? (
        <div className="muted">No metrics available yet.</div>
      ) : (
        <div className="metrics-grid">
          {rows.map((row) => (
            <div key={row.key} className="metric-item">
              <div className="metric-key">{row.key}</div>
              <div className="metric-value">{row.value}</div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
