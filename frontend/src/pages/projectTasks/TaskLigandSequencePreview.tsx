import { useMemo } from 'react';
import type { ProteinModification } from '../../types/models';
import { toneForPlddt } from './taskPresentation';

interface TaskLigandSequencePreviewProps {
  sequence: string;
  residuePlddts: number[] | null;
  modifications?: ProteinModification[];
}

function ligandSequenceDensityClass(length: number): 'is-short' | 'is-medium' | 'is-long' | 'is-xlong' {
  if (length <= 26) return 'is-short';
  if (length <= 56) return 'is-medium';
  if (length <= 110) return 'is-long';
  return 'is-xlong';
}

const RESIDUES_PER_LINE = 10;

function splitSequenceNodesIntoBalancedLines<T>(nodes: T[]): T[][] {
  if (nodes.length === 0) return [];
  const lineCount = Math.max(1, Math.ceil(nodes.length / RESIDUES_PER_LINE));
  const lines: T[][] = [];
  for (let i = 0; i < lineCount; i += 1) {
    const start = i * RESIDUES_PER_LINE;
    lines.push(nodes.slice(start, start + RESIDUES_PER_LINE));
  }
  return lines;
}

function ligandSequenceHeightClass(lineCount: number): 'is-height-normal' | 'is-height-tall' | 'is-height-xtall' {
  if (lineCount <= 5) return 'is-height-normal';
  if (lineCount <= 10) return 'is-height-tall';
  return 'is-height-xtall';
}

export function TaskLigandSequencePreview({ sequence, residuePlddts, modifications }: TaskLigandSequencePreviewProps) {
  const residues = sequence.trim().toUpperCase().split('');
  const densityClass = ligandSequenceDensityClass(residues.length);
  const modificationByPosition = useMemo(() => {
    const byPosition = new Map<number, ProteinModification>();
    for (const mod of modifications || []) {
      const position = Math.floor(Number(mod.position));
      const ccd = String(mod.ccd || '').trim().toUpperCase();
      if (!Number.isFinite(position) || position < 1 || !ccd) continue;
      byPosition.set(position, { ...mod, ccd });
    }
    return byPosition;
  }, [modifications]);
  const nodes = useMemo(() => {
    return residues.map((rawResidue, index) => {
      const residue = /^[A-Z]$/.test(rawResidue) ? rawResidue : '?';
      const confidence = residuePlddts?.[index] ?? null;
      const tone = toneForPlddt(confidence);
      const modification = modificationByPosition.get(index + 1) || null;
      const modifiedLabel = String(modification?.ccd || '').trim().toUpperCase();
      return {
        index,
        residue,
        displayResidue: modifiedLabel || residue,
        modifiedLabel,
        confidence,
        tone
      };
    });
  }, [residues, residuePlddts, modificationByPosition]);
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
                const isModified = Boolean(item.modifiedLabel);
                const title = isModified
                  ? `#${position} ${item.residue} -> ${item.modifiedLabel} | pLDDT ${confidenceText}`
                  : `#${position} ${item.residue} | pLDDT ${confidenceText}`;
                return (
                  <span className="task-ligand-sequence-node" key={`ligand-seq-residue-${item.index}`}>
                    {colIndex > 0 && <span className={`task-ligand-sequence-link tone-${linkTone}`} aria-hidden="true" />}
                    <span
                      className={`task-ligand-sequence-residue tone-${item.tone}${isModified ? ' is-modified' : ''}`}
                      title={title}
                    >
                      {item.displayResidue}
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
