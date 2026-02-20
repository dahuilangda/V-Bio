import { useMemo } from 'react';
import type { InputComponent } from '../../types/models';
import { componentTypeLabel } from '../../utils/projectInputs';

export interface ComponentTypeBucketEntry {
  id: string;
  typeLabel: string;
  typeOrder: number;
  globalOrder: number;
  filled: boolean;
}

export type ComponentTypeBuckets = Record<InputComponent['type'], ComponentTypeBucketEntry[]>;

export function useComponentTypeBuckets(normalizedDraftComponents: InputComponent[]): ComponentTypeBuckets {
  return useMemo(() => {
    const buckets: ComponentTypeBuckets = {
      protein: [],
      ligand: [],
      dna: [],
      rna: []
    };
    const typeCounters: Record<InputComponent['type'], number> = {
      protein: 0,
      ligand: 0,
      dna: 0,
      rna: 0
    };

    normalizedDraftComponents.forEach((component, index) => {
      typeCounters[component.type] += 1;
      buckets[component.type].push({
        id: component.id,
        typeLabel: componentTypeLabel(component.type),
        typeOrder: typeCounters[component.type],
        globalOrder: index + 1,
        filled: Boolean(component.sequence.trim())
      });
    });

    return buckets;
  }, [normalizedDraftComponents]);
}
