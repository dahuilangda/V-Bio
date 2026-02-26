import { useCallback, useState } from 'react';
import type { LigandFragmentItem } from '../../LigandFragmentSketcher';
import { resolveVariableSelection, type LigandAtomBond } from './fragmentVariableSelection';

export type LeadOptDirection = 'increase' | 'decrease' | '';
export type LeadOptVariableMode = 'substructure' | 'exact';
export type LeadOptQueryMode = 'one-to-many' | 'many-to-many';
export type LeadOptQueryProperty = string;
export type LeadOptGroupedByEnvironment = 'auto' | 'on' | 'off';

export interface LeadOptVariableItemInput {
  query: string;
  mode: LeadOptVariableMode;
  fragment_id?: string;
  atom_indices?: number[];
}

interface UseLeadOptMmpQueryFormParams {
  onError: (message: string | null) => void;
}

export function useLeadOptMmpQueryForm({ onError }: UseLeadOptMmpQueryFormParams) {
  const [variableQuery, setVariableQuery] = useState('');
  const [variableMode, setVariableMode] = useState<LeadOptVariableMode>('substructure');
  const [constantQuery, setConstantQuery] = useState('');
  const [queryMode, setQueryMode] = useState<LeadOptQueryMode>('one-to-many');
  const [direction, setDirection] = useState<LeadOptDirection>('');
  const [queryProperty, setQueryProperty] = useState<LeadOptQueryProperty>('');
  const [groupedByEnvironment, setGroupedByEnvironment] = useState<LeadOptGroupedByEnvironment>('auto');
  const [envRadius, setEnvRadius] = useState(1);
  const [minPairs, setMinPairs] = useState(1);
  const [showAdvanced, setShowAdvanced] = useState(false);

  const applyFragmentReplacement = useCallback(
    (selectedFragmentSmiles: string[], activeFragment: LigandFragmentItem | null): boolean => {
      const value = selectedFragmentSmiles.join(';;') || activeFragment?.smiles || '';
      if (!value) {
        onError('Please select a fragment first.');
        return false;
      }
      setVariableMode('substructure');
      setQueryMode('one-to-many');
      setVariableQuery(value);
      onError(null);
      return true;
    },
    [onError]
  );

  const applyScaffoldHopping = useCallback(
    (selectedFragmentSmiles: string[], fragment: LigandFragmentItem | null): boolean => {
      const value = selectedFragmentSmiles.join(';;') || fragment?.smiles || '';
      if (!value) {
        onError('Please select one fragment for scaffold hop.');
        return false;
      }
      setVariableMode('substructure');
      setQueryMode('many-to-many');
      setVariableQuery(value);
      setMinPairs((prev) => Math.max(prev, 2));
      onError(null);
      return true;
    },
    [onError]
  );

  const buildVariableItems = useCallback(
    (
      selectedFragmentItems: LigandFragmentItem[],
      allFragments: LigandFragmentItem[] = [],
      atomBonds?: LigandAtomBond[]
    ): LeadOptVariableItemInput[] => {
      const resolved = resolveVariableSelection(
        selectedFragmentItems,
        allFragments.length > 0 ? allFragments : selectedFragmentItems,
        atomBonds
      );
      const selectedItems = resolved.effectiveItems.map((item) => {
        const atomIndices = Array.isArray(item.atom_indices)
          ? item.atom_indices
              .map((value) => Number(value))
              .filter((value) => Number.isFinite(value) && value >= 0)
              .map((value) => Math.floor(value))
          : [];
        const normalizedQuery = String(item.smiles || '').trim() || String(item.display_smiles || '').trim();
        return {
          query: normalizedQuery,
          mode: 'substructure' as LeadOptVariableMode,
          fragment_id: String(item.fragment_id || '').trim(),
          atom_indices: atomIndices
        };
      });
      const manualItems = variableQuery
        .split(';;')
        .map((item) => item.trim())
        .filter(Boolean)
        .map((item) => ({
          query: item,
          mode: variableMode
        }));
      let variableItems = selectedItems.length > 0 ? selectedItems : manualItems;
      if (queryMode === 'many-to-many' && variableItems.length > 3) {
        variableItems = variableItems.slice(0, 3);
      }
      return variableItems;
    },
    [queryMode, variableMode, variableQuery]
  );

  return {
    variableQuery,
    setVariableQuery,
    variableMode,
    setVariableMode,
    constantQuery,
    setConstantQuery,
    queryMode,
    setQueryMode,
    direction,
    setDirection,
    queryProperty,
    setQueryProperty,
    groupedByEnvironment,
    setGroupedByEnvironment,
    envRadius,
    setEnvRadius,
    minPairs,
    setMinPairs,
    showAdvanced,
    setShowAdvanced,
    buildVariableItems,
    applyFragmentReplacement,
    applyScaffoldHopping
  };
}
