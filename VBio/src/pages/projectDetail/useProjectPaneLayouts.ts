import { useMemo } from 'react';
import type { CSSProperties, KeyboardEvent, PointerEvent, RefObject } from 'react';
import { useResizablePane } from './useResizablePane';

type ResultsGridStyle = CSSProperties & { '--results-main-width'?: string };
type InputsGridStyle = CSSProperties & { '--inputs-main-width'?: string };
type ConstraintsGridStyle = CSSProperties & { '--constraints-main-width'?: string };

const RESULTS_MAIN_WIDTH_STORAGE_KEY = 'vbio:results-main-width';
const DEFAULT_RESULTS_MAIN_WIDTH = 67;
const MIN_RESULTS_MAIN_WIDTH = 56;
const MAX_RESULTS_MAIN_WIDTH = 80;
const COMPONENTS_MAIN_WIDTH_STORAGE_KEY = 'vbio:components-main-width';
const DEFAULT_COMPONENTS_MAIN_WIDTH = 74;
const MIN_COMPONENTS_MAIN_WIDTH = 55;
const MAX_COMPONENTS_MAIN_WIDTH = 84;
const CONSTRAINTS_MAIN_WIDTH_STORAGE_KEY = 'vbio:constraints-main-width';
const DEFAULT_CONSTRAINTS_MAIN_WIDTH = 63;
const MIN_CONSTRAINTS_MAIN_WIDTH = 52;
const MAX_CONSTRAINTS_MAIN_WIDTH = 82;

interface UseProjectPaneLayoutsResult {
  resultsMainWidth: number;
  isResultsResizing: boolean;
  resultsGridRef: RefObject<HTMLDivElement>;
  handleResultsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  handleResultsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  componentsMainWidth: number;
  isComponentsResizing: boolean;
  componentsWorkspaceRef: RefObject<HTMLDivElement>;
  handleComponentsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  handleComponentsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  constraintsMainWidth: number;
  isConstraintsResizing: boolean;
  constraintsWorkspaceRef: RefObject<HTMLDivElement>;
  handleConstraintsResizerPointerDown: (event: PointerEvent<HTMLDivElement>) => void;
  handleConstraintsResizerKeyDown: (event: KeyboardEvent<HTMLDivElement>) => void;
  resultsGridStyle: ResultsGridStyle;
  componentsGridStyle: InputsGridStyle;
  constraintsGridStyle: ConstraintsGridStyle;
}

export function useProjectPaneLayouts(): UseProjectPaneLayoutsResult {
  const {
    mainWidth: resultsMainWidth,
    isResizing: isResultsResizing,
    containerRef: resultsGridRef,
    onResizerPointerDown: handleResultsResizerPointerDown,
    onResizerKeyDown: handleResultsResizerKeyDown,
  } = useResizablePane({
    storageKey: RESULTS_MAIN_WIDTH_STORAGE_KEY,
    defaultWidth: DEFAULT_RESULTS_MAIN_WIDTH,
    minWidth: MIN_RESULTS_MAIN_WIDTH,
    maxWidth: MAX_RESULTS_MAIN_WIDTH,
    minAsideWidth: 280,
  });
  const {
    mainWidth: componentsMainWidth,
    isResizing: isComponentsResizing,
    containerRef: componentsWorkspaceRef,
    onResizerPointerDown: handleComponentsResizerPointerDown,
    onResizerKeyDown: handleComponentsResizerKeyDown,
  } = useResizablePane({
    storageKey: COMPONENTS_MAIN_WIDTH_STORAGE_KEY,
    defaultWidth: DEFAULT_COMPONENTS_MAIN_WIDTH,
    minWidth: MIN_COMPONENTS_MAIN_WIDTH,
    maxWidth: MAX_COMPONENTS_MAIN_WIDTH,
    minAsideWidth: 250,
  });
  const {
    mainWidth: constraintsMainWidth,
    isResizing: isConstraintsResizing,
    containerRef: constraintsWorkspaceRef,
    onResizerPointerDown: handleConstraintsResizerPointerDown,
    onResizerKeyDown: handleConstraintsResizerKeyDown,
  } = useResizablePane({
    storageKey: CONSTRAINTS_MAIN_WIDTH_STORAGE_KEY,
    defaultWidth: DEFAULT_CONSTRAINTS_MAIN_WIDTH,
    minWidth: MIN_CONSTRAINTS_MAIN_WIDTH,
    maxWidth: MAX_CONSTRAINTS_MAIN_WIDTH,
    minAsideWidth: 300,
  });

  const resultsGridStyle = useMemo<ResultsGridStyle>(
    () => ({
      '--results-main-width': `${resultsMainWidth.toFixed(2)}%`
    }),
    [resultsMainWidth]
  );
  const componentsGridStyle = useMemo<InputsGridStyle>(
    () => ({
      '--inputs-main-width': `${componentsMainWidth.toFixed(2)}%`
    }),
    [componentsMainWidth]
  );
  const constraintsGridStyle = useMemo<ConstraintsGridStyle>(
    () => ({
      '--constraints-main-width': `${constraintsMainWidth.toFixed(2)}%`
    }),
    [constraintsMainWidth]
  );

  return {
    resultsMainWidth,
    isResultsResizing,
    resultsGridRef,
    handleResultsResizerPointerDown,
    handleResultsResizerKeyDown,
    componentsMainWidth,
    isComponentsResizing,
    componentsWorkspaceRef,
    handleComponentsResizerPointerDown,
    handleComponentsResizerKeyDown,
    constraintsMainWidth,
    isConstraintsResizing,
    constraintsWorkspaceRef,
    handleConstraintsResizerPointerDown,
    handleConstraintsResizerKeyDown,
    resultsGridStyle,
    componentsGridStyle,
    constraintsGridStyle
  };
}
