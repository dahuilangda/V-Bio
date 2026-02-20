import { useMemo } from 'react';
import type { InputComponent } from '../../types/models';
import { toneForPlddt } from './projectMetrics';

export function isSequenceLigandType(type: InputComponent['type'] | null): boolean {
  return type === 'protein' || type === 'dna' || type === 'rna';
}

function overviewResiduesPerLineForLength(length: number): number {
  if (length <= 40) return 12;
  if (length <= 120) return 14;
  if (length <= 220) return 16;
  return 18;
}

function splitOverviewSequenceNodesIntoBalancedLines<T>(nodes: T[]): T[][] {
  if (nodes.length === 0) return [];
  const preferredPerLine = overviewResiduesPerLineForLength(nodes.length);
  const lineCount = Math.max(1, Math.ceil(nodes.length / preferredPerLine));
  const baseSize = Math.floor(nodes.length / lineCount);
  const remainder = nodes.length % lineCount;
  const lines: T[][] = [];
  let cursor = 0;
  for (let i = 0; i < lineCount; i += 1) {
    const size = baseSize + (i < remainder ? 1 : 0);
    lines.push(nodes.slice(cursor, cursor + size));
    cursor += size;
  }
  return lines;
}

export function OverviewLigandSequencePreview({
  sequence,
  residuePlddts
}: {
  sequence: string;
  residuePlddts: number[] | null;
}) {
  const residues = sequence.trim().toUpperCase().split('');
  const nodes = useMemo(() => {
    return residues.map((rawResidue, index) => {
      const residue = /^[A-Z]$/.test(rawResidue) ? rawResidue : '?';
      const confidence = residuePlddts?.[index] ?? null;
      const tone = toneForPlddt(confidence);
      return {
        index,
        residue,
        confidence,
        tone
      };
    });
  }, [residues, residuePlddts]);
  const lines = useMemo(() => splitOverviewSequenceNodesIntoBalancedLines(nodes), [nodes]);

  if (residues.length === 0) {
    return <div className="ligand-preview-empty">No sequence.</div>;
  }

  return (
    <div className="overview-ligand-sequence">
      <div className="overview-ligand-sequence-lines">
        {lines.map((line, rowIndex) => (
          <div className="overview-ligand-sequence-line" key={`overview-seq-line-${rowIndex}`}>
            <span className="overview-ligand-sequence-line-index left" aria-hidden="true">
              {line[0]?.index + 1}
            </span>
            <span className="overview-ligand-sequence-line-track">
              {line.map((item, colIndex) => {
                const confidenceText = item.confidence === null ? '-' : item.confidence.toFixed(1);
                const linkTone = colIndex > 0 ? line[colIndex - 1].tone : item.tone;
                const position = item.index + 1;
                return (
                  <span className="overview-ligand-sequence-node" key={`overview-seq-${item.index}`}>
                    {colIndex > 0 && <span className={`overview-ligand-sequence-link tone-${linkTone}`} aria-hidden="true" />}
                    <span className={`overview-ligand-sequence-residue tone-${item.tone}`} title={`#${position} ${item.residue} | pLDDT ${confidenceText}`}>
                      {item.residue}
                    </span>
                  </span>
                );
              })}
            </span>
            <span className="overview-ligand-sequence-line-index right" aria-hidden="true">
              {line[line.length - 1]?.index + 1}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
