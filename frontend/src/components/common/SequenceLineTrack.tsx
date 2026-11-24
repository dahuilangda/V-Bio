import { useRef } from 'react';
import { useShrinkToFit } from '../../hooks/useShrinkToFit';

export interface SequenceTrackNode {
  index: number;
  residue: string;
  displayResidue: string;
  modifiedLabel: string;
  confidence: number | null;
  tone: string;
}

interface SequenceLineTrackProps {
  nodes: SequenceTrackNode[];
  classPrefix: 'task-ligand-sequence' | 'overview-ligand-sequence';
}

export function SequenceLineTrack({ nodes, classPrefix }: SequenceLineTrackProps) {
  const trackRef = useRef<HTMLSpanElement | null>(null);
  useShrinkToFit(trackRef, nodes);
  return (
    <span ref={trackRef} className={`${classPrefix}-line-track`}>
      {nodes.map((item, colIndex) => {
        const confidenceText = item.confidence === null ? '-' : item.confidence.toFixed(1);
        const linkTone = colIndex > 0 ? nodes[colIndex - 1].tone : item.tone;
        const position = item.index + 1;
        const isModified = Boolean(item.modifiedLabel);
        const title = isModified
          ? `#${position} ${item.residue} -> ${item.modifiedLabel} | pLDDT ${confidenceText}`
          : `#${position} ${item.residue} | pLDDT ${confidenceText}`;
        return (
          <span className={`${classPrefix}-node`} key={`${classPrefix}-residue-${item.index}`}>
            {colIndex > 0 && <span className={`${classPrefix}-link tone-${linkTone}`} aria-hidden="true" />}
            <span
              className={`${classPrefix}-residue tone-${item.tone}${isModified ? ' is-modified' : ''}`}
              title={title}
            >
              {item.displayResidue}
            </span>
          </span>
        );
      })}
    </span>
  );
}
