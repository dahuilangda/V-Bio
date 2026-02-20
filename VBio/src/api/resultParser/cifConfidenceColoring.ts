function tokenizeCifRow(row: string): string[] {
  const matcher = /'(?:[^']*)'|"(?:[^"]*)"|[^\s]+/g;
  const tokens: string[] = [];
  let match: RegExpExecArray | null = matcher.exec(row);
  while (match) {
    tokens.push(match[0]);
    match = matcher.exec(row);
  }
  return tokens;
}

function stripCifTokenQuotes(value: string): string {
  if (value.length >= 2) {
    if ((value.startsWith("'") && value.endsWith("'")) || (value.startsWith('"') && value.endsWith('"'))) {
      return value.slice(1, -1);
    }
  }
  return value;
}

const POLYMER_COMP_IDS = new Set([
  'ACE',
  'NME',
  'NMA',
  'NH2',
  'ALA',
  'ARG',
  'ASN',
  'ASP',
  'CYS',
  'GLN',
  'GLU',
  'GLY',
  'HIS',
  'ILE',
  'LEU',
  'LYS',
  'MET',
  'PHE',
  'PRO',
  'SER',
  'THR',
  'TRP',
  'TYR',
  'VAL',
  'SEC',
  'PYL',
  'ASX',
  'GLX',
  'UNK',
  'A',
  'C',
  'G',
  'U',
  'I',
  'DA',
  'DC',
  'DG',
  'DT',
  'DI',
  'DU'
]);

function parseMaQaLocalMetricIdFromCif(structureText: string): string | null {
  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    const idCol = headers.findIndex((header) => header === '_ma_qa_metric.id');
    const modeCol = headers.findIndex((header) => header === '_ma_qa_metric.mode');
    if (idCol < 0 || modeCol < 0) {
      i = headerIndex - 1;
      continue;
    }

    let rowIndex = headerIndex;
    let fallbackMetricId: string | null = null;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const metricId = stripCifTokenQuotes(tokens[idCol]).trim();
        const mode = stripCifTokenQuotes(tokens[modeCol]).trim().toLowerCase();
        if (!fallbackMetricId && metricId) {
          fallbackMetricId = metricId;
        }
        if (metricId && mode === 'local') {
          return metricId;
        }
      }
      rowIndex += 1;
    }
    return fallbackMetricId;
  }
  return null;
}

type CifResidueConfidenceRow = {
  chainId: string;
  seqId: number;
  compId: string;
  value: number;
};

function buildResidueConfidenceRowsFromCif(structureText: string): CifResidueConfidenceRow[] {
  const lines = structureText.split(/\r?\n/);
  for (let i = 0; i < lines.length; i += 1) {
    if (lines[i].trim() !== 'loop_') continue;

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    if (!headers.some((header) => header.startsWith('_atom_site.'))) {
      i = headerIndex - 1;
      continue;
    }

    const groupCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.group_pdb');
    const chainCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.label_asym_id');
    const seqCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.label_seq_id');
    const compCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.label_comp_id');
    const bFactorCol = headers.findIndex((header) => header.toLowerCase() === '_atom_site.b_iso_or_equiv');
    if (chainCol < 0 || seqCol < 0 || compCol < 0 || bFactorCol < 0) {
      return [];
    }

    const residueMap = new Map<string, { chainId: string; seqId: number; compId: string; sum: number; count: number }>();
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      if (row === 'loop_' || row.startsWith('_')) break;

      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const groupPdb = groupCol >= 0 ? stripCifTokenQuotes(tokens[groupCol]).trim().toUpperCase() : '';
        const chainId = stripCifTokenQuotes(tokens[chainCol]).trim();
        const seqToken = stripCifTokenQuotes(tokens[seqCol]).trim();
        const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
        const bFactor = Number(tokens[bFactorCol]);
        const seqId = Number.parseInt(seqToken, 10);
        if (!chainId || !compId || !Number.isFinite(seqId) || seqId <= 0 || !Number.isFinite(bFactor)) {
          rowIndex += 1;
          continue;
        }
        if (groupPdb) {
          if (groupPdb !== 'ATOM') {
            rowIndex += 1;
            continue;
          }
        } else if (!POLYMER_COMP_IDS.has(compId)) {
          rowIndex += 1;
          continue;
        }

        const key = `${chainId}|${seqId}|${compId}`;
        const current = residueMap.get(key);
        if (current) {
          current.sum += bFactor;
          current.count += 1;
        } else {
          residueMap.set(key, { chainId, seqId, compId, sum: bFactor, count: 1 });
        }
      }
      rowIndex += 1;
    }

    return Array.from(residueMap.values()).map((item) => ({
      chainId: item.chainId,
      seqId: item.seqId,
      compId: item.compId,
      value: item.sum / item.count
    }));
  }

  return [];
}

export function ensureCifHasMaQaMetricLocal(structureText: string): string {
  if (!structureText) return structureText;
  if (structureText.includes('_ma_qa_metric_local.metric_value')) return structureText;

  const residueRows = buildResidueConfidenceRowsFromCif(structureText);
  if (!residueRows.length) return structureText;

  const hasMetricCategory = structureText.includes('_ma_qa_metric.id');
  const localMetricId = parseMaQaLocalMetricIdFromCif(structureText) || '1';

  const appendBlocks: string[] = [];
  if (!hasMetricCategory) {
    appendBlocks.push(
      [
        'loop_',
        '_ma_qa_metric.id',
        '_ma_qa_metric.name',
        '_ma_qa_metric.description',
        '_ma_qa_metric.type',
        '_ma_qa_metric.mode',
        '_ma_qa_metric.type_other_details',
        '_ma_qa_metric.software_group_id',
        `${localMetricId} pLDDT 'Predicted lddt' pLDDT local . .`,
        '#'
      ].join('\n')
    );
  }

  const localRows = residueRows.map(
    (item, index) =>
      `${index + 1} 1 ${item.chainId} ${item.seqId} ${item.compId} ${localMetricId} ${item.value.toFixed(3)}`
  );
  appendBlocks.push(
    [
      'loop_',
      '_ma_qa_metric_local.ordinal_id',
      '_ma_qa_metric_local.model_id',
      '_ma_qa_metric_local.label_asym_id',
      '_ma_qa_metric_local.label_seq_id',
      '_ma_qa_metric_local.label_comp_id',
      '_ma_qa_metric_local.metric_id',
      '_ma_qa_metric_local.metric_value',
      ...localRows,
      '#'
    ].join('\n')
  );

  const suffix = structureText.endsWith('\n') ? '' : '\n';
  return `${structureText}${suffix}#\n${appendBlocks.join('\n#\n')}\n`;
}

function pruneCifMaQaMetricLocalToPolymer(structureText: string): string {
  if (!structureText) return structureText;
  if (!structureText.includes('_ma_qa_metric_local.label_comp_id')) return structureText;
  const lines = structureText.split(/\r?\n/);
  const output: string[] = [];
  let changed = false;

  let i = 0;
  while (i < lines.length) {
    if (lines[i].trim() !== 'loop_') {
      output.push(lines[i]);
      i += 1;
      continue;
    }

    let headerIndex = i + 1;
    const headers: string[] = [];
    while (headerIndex < lines.length) {
      const rawHeader = lines[headerIndex].trim();
      if (!rawHeader.startsWith('_')) break;
      headers.push(rawHeader);
      headerIndex += 1;
    }

    const isQaLocalLoop = headers.some((header) => header.toLowerCase().startsWith('_ma_qa_metric_local.'));
    const compCol = headers.findIndex((header) => header.toLowerCase() === '_ma_qa_metric_local.label_comp_id');
    if (!isQaLocalLoop || compCol < 0) {
      let rowIndex = headerIndex;
      while (rowIndex < lines.length) {
        const row = lines[rowIndex].trim();
        if (row === 'loop_' || row.startsWith('_')) break;
        rowIndex += 1;
      }
      output.push(...lines.slice(i, rowIndex));
      i = rowIndex;
      continue;
    }

    output.push(lines[i]);
    output.push(...lines.slice(i + 1, headerIndex));
    let rowIndex = headerIndex;
    while (rowIndex < lines.length) {
      const rawRow = lines[rowIndex];
      const row = rawRow.trim();
      if (row === 'loop_' || row.startsWith('_')) break;
      if (!row || row === '#') {
        rowIndex += 1;
        continue;
      }
      const tokens = tokenizeCifRow(rawRow);
      if (tokens.length >= headers.length) {
        const compId = stripCifTokenQuotes(tokens[compCol]).trim().toUpperCase();
        if (POLYMER_COMP_IDS.has(compId)) {
          output.push(tokens.join(' '));
        } else {
          changed = true;
        }
      }
      rowIndex += 1;
    }
    output.push('#');
    i = rowIndex;
  }

  if (!changed) return structureText;
  return output.join('\n');
}

export function ensureStructureConfidenceColoringData(
  structureText: string,
  structureFormat: 'cif' | 'pdb',
  backend: string
): string {
  if (!structureText) return structureText;
  if (structureFormat !== 'cif') return structureText;
  const normalizedBackend = String(backend || '').trim().toLowerCase();
  if (normalizedBackend !== 'protenix') return structureText;
  const pruned = pruneCifMaQaMetricLocalToPolymer(structureText);
  return ensureCifHasMaQaMetricLocal(pruned);
}
