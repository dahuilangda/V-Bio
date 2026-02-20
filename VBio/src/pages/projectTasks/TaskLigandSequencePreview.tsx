import { useMemo } from 'react';
import { toneForPlddt } from './taskPresentation';

interface TaskLigandSequencePreviewProps {
  sequence: string;
  residuePlddts: number[] | null;
}

function ligandSequenceDensityClass(length: number): 'is-short' | 'is-medium' | 'is-long' | 'is-xlong' {
  if (length <= 26) return 'is-short';
  if (length <= 56) return 'is-medium';
  if (length <= 110) return 'is-long';
  return 'is-xlong';
}

function residuesPerLineForLength(length: number): number {
  if (length <= 30) return 10;
  if (length <= 90) return 11;
  if (length <= 180) return 12;
  return 13;
}

function splitSequenceNodesIntoBalancedLines<T>(nodes: T[]): T[][] {
  if (nodes.length === 0) return [];
  const preferredPerLine = residuesPerLineForLength(nodes.length);
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

function ligandSequenceHeightClass(lineCount: number): 'is-height-normal' | 'is-height-tall' | 'is-height-xtall' {
  if (lineCount <= 5) return 'is-height-normal';
  if (lineCount <= 10) return 'is-height-tall';
  return 'is-height-xtall';
}

export function TaskLigandSequencePreview({ sequence, residuePlddts }: TaskLigandSequencePreviewProps) {
  const residues = sequence.trim().toUpperCase().split('');
  const densityClass = ligandSequenceDensityClass(residues.length);
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
  const lines = useMemo(() => splitSequenceNodesIntoBalancedLines(nodes), [nodes]);
  const heightClass = ligandSequenceHeightClass(lines.length);

  if (nodes.length === 0) {
    return (
      <div className={`task-ligand-sequence ${densityClass}`}>
        <div className="task-ligand-thumb task-ligand-thumb-empty">
          <span className="muted small">No sequence</span>
        </div>
      </div>
    );
  }

  return (
    <div className={`task-ligand-sequence ${densityClass} ${heightClass}`}>
      <div className="task-ligand-sequence-lines">
        {lines.map((line, rowIndex) => (
          <div className="task-ligand-sequence-line" key={`ligand-seq-row-${rowIndex}`}>
            <span className="task-ligand-sequence-line-index left" aria-hidden="true">
              {line[0]?.index + 1}
            </span>
            <span className="task-ligand-sequence-line-track">
              {line.map((item, colIndex) => {
                const confidenceText = item.confidence === null ? '-' : item.confidence.toFixed(1);
                const linkTone = colIndex > 0 ? line[colIndex - 1].tone : item.tone;
                const position = item.index + 1;
                return (
                  <span className="task-ligand-sequence-node" key={`ligand-seq-residue-${item.index}`}>
                    {colIndex > 0 && <span className={`task-ligand-sequence-link tone-${linkTone}`} aria-hidden="true" />}
                    <span
                      className={`task-ligand-sequence-residue tone-${item.tone}`}
                      title={`#${position} ${item.residue} | pLDDT ${confidenceText}`}
                    >
                      {item.residue}
                    </span>
                  </span>
                );
              })}
            </span>
            <span className="task-ligand-sequence-line-index right" aria-hidden="true">
              {line[line.length - 1]?.index + 1}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
