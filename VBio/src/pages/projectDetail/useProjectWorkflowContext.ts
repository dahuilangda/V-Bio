import { useMemo } from 'react';
import type { InputComponent, ProjectInputConfig } from '../../types/models';
import { deriveLeadOptimizationChainContext } from '../../utils/leadOptimization';
import { extractPrimaryProteinAndLigand } from '../../utils/projectInputs';
import {
  allowedConstraintTypesForBackend,
  listIncompleteComponentOrders,
  normalizeComponents
} from './projectDraftUtils';

interface DraftLike {
  backend: string;
  inputConfig: ProjectInputConfig;
}

interface ComponentCompletionSummary {
  total: number;
  filledCount: number;
  incompleteCount: number;
}

interface UseProjectWorkflowContextInput {
  draft: DraftLike | null;
  fallbackBackend: string;
  isPeptideDesignWorkflow?: boolean;
}

interface UseProjectWorkflowContextOutput {
  normalizedDraftComponents: InputComponent[];
  leadOptPrimary: { proteinSequence: string; ligandSmiles: string };
  leadOptChainContext: { targetChain: string; ligandChain: string };
  incompleteComponentOrders: number[];
  componentCompletion: ComponentCompletionSummary;
  hasIncompleteComponents: boolean;
  allowedConstraintTypes: Array<'contact' | 'bond' | 'pocket'>;
  isBondOnlyBackend: boolean;
}

export function useProjectWorkflowContext({
  draft,
  fallbackBackend,
  isPeptideDesignWorkflow = false
}: UseProjectWorkflowContextInput): UseProjectWorkflowContextOutput {
  const normalizedDraftComponents = useMemo(() => {
    if (!draft) return [];
    return normalizeComponents(draft.inputConfig.components);
  }, [draft]);

  const leadOptPrimary = useMemo(() => {
    if (!draft) {
      return { proteinSequence: '', ligandSmiles: '' };
    }
    return extractPrimaryProteinAndLigand(draft.inputConfig);
  }, [draft]);

  const leadOptChainContext = useMemo(() => {
    if (!draft) {
      return { targetChain: 'A', ligandChain: 'L' };
    }
    const components = normalizeComponents(draft.inputConfig.components);
    return deriveLeadOptimizationChainContext(components);
  }, [draft]);

  const incompleteComponentOrders = useMemo(
    () =>
      listIncompleteComponentOrders(normalizedDraftComponents, {
        ignoreEmptyLigand: isPeptideDesignWorkflow
      }),
    [normalizedDraftComponents, isPeptideDesignWorkflow]
  );

  const componentCompletion = useMemo<ComponentCompletionSummary>(() => {
    const total = normalizedDraftComponents.length;
    const incompleteCount = incompleteComponentOrders.length;
    const filledCount = Math.max(0, total - incompleteCount);
    return {
      total,
      filledCount,
      incompleteCount
    };
  }, [normalizedDraftComponents, incompleteComponentOrders]);

  const hasIncompleteComponents = componentCompletion.incompleteCount > 0;

  const allowedConstraintTypes = useMemo(() => {
    return allowedConstraintTypesForBackend(draft?.backend || fallbackBackend || 'boltz');
  }, [draft?.backend, fallbackBackend]);

  const isBondOnlyBackend = useMemo(() => {
    const backend = String(draft?.backend || fallbackBackend || '').toLowerCase();
    return backend === 'alphafold3' || backend === 'protenix';
  }, [draft?.backend, fallbackBackend]);

  return {
    normalizedDraftComponents,
    leadOptPrimary,
    leadOptChainContext,
    incompleteComponentOrders,
    componentCompletion,
    hasIncompleteComponents,
    allowedConstraintTypes,
    isBondOnlyBackend
  };
}
