import {
  type CSSProperties,
  type KeyboardEvent as ReactKeyboardEvent,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
  useEffect,
  useMemo,
  useRef,
  useState
} from 'react';
import {
  ChevronDown,
  ChevronRight,
  ClipboardPaste,
  Dna,
  FileUp,
  FlaskConical,
  Plus,
  RotateCcw,
  Target,
  Trash2
} from 'lucide-react';
import { ComponentInputEditor } from '../../components/project/ComponentInputEditor';
import type { InputComponent, MoleculeType } from '../../types/models';
import { componentTypeLabel, createInputComponent } from '../../utils/projectInputs';
import {
  buildVirtualScreeningChainPlan,
  parseVirtualScreeningInput,
  validateVirtualScreeningSmiles,
  VIRTUAL_SCREENING_EXAMPLE
} from '../../utils/virtualScreening';

export type VirtualScreeningInputMode = 'upload' | 'paste';

export interface VirtualScreeningLibraryChange {
  value: string;
  mode: VirtualScreeningInputMode;
  fileName: string;
}

export interface VirtualScreeningWorkflowSectionProps {
  visible: boolean;
  canEdit: boolean;
  componentsWorkspaceRef: RefObject<HTMLDivElement | null>;
  isComponentsResizing: boolean;
  componentsGridStyle: CSSProperties;
  onComponentsResizerPointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onComponentsResizerKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
  components: InputComponent[];
  onComponentsChange: (components: InputComponent[]) => void;
  activeComponentId: string | null;
  onActiveComponentIdChange: (id: string | null) => void;
  screeningInput: string;
  screeningInputMode: VirtualScreeningInputMode;
  screeningInputFileName: string;
  onScreeningLibraryChange: (change: VirtualScreeningLibraryChange) => void;
}

interface SidebarComponentEntry {
  component: InputComponent;
  globalOrder: number;
  typeOrder: number;
}

const COMPONENT_TYPES: MoleculeType[] = ['protein', 'ligand', 'dna', 'rna'];
const DISABLED_COMPONENT_TYPES: MoleculeType[] = ['dna', 'rna'];
const NESSO_PROTEIN_ALPHABET = /^[ACDEFGHIKLMNPQRSTVWY]+$/;

function cleanSequence(value: string): string {
  return value.replace(/\s+/g, '').toUpperCase();
}

function isComponentReady(component: InputComponent): boolean {
  if (component.type === 'protein') {
    const sequence = cleanSequence(component.sequence || '');
    return Boolean(sequence)
      && NESSO_PROTEIN_ALPHABET.test(sequence)
      && !component.cyclic
      && (component.modifications || []).length === 0;
  }
  if (component.type === 'ligand') return Boolean(String(component.sequence || '').trim());
  return false;
}

function componentTypeIcon(type: MoleculeType) {
  return type === 'ligand'
    ? <FlaskConical size={13} aria-hidden />
    : <Dna size={13} aria-hidden />;
}

export function VirtualScreeningWorkflowSection({
  visible,
  canEdit,
  componentsWorkspaceRef,
  isComponentsResizing,
  componentsGridStyle,
  onComponentsResizerPointerDown,
  onComponentsResizerKeyDown,
  components,
  onComponentsChange,
  activeComponentId,
  onActiveComponentIdChange,
  screeningInput,
  screeningInputMode,
  screeningInputFileName,
  onScreeningLibraryChange
}: VirtualScreeningWorkflowSectionProps) {
  const uploadRef = useRef<HTMLInputElement | null>(null);
  const [validationState, setValidationState] = useState<'idle' | 'checking' | 'ready' | 'unavailable'>('idle');
  const [invalidIndexes, setInvalidIndexes] = useState<Set<number>>(new Set());
  const [validationWarnings, setValidationWarnings] = useState<string[]>([]);
  const [libraryError, setLibraryError] = useState('');
  const [sidebarTypeOpen, setSidebarTypeOpen] = useState<Record<MoleculeType, boolean>>({
    protein: true,
    ligand: true,
    dna: true,
    rna: true
  });

  const parsed = useMemo(() => parseVirtualScreeningInput(screeningInput), [screeningInput]);
  const supportedComponents = useMemo(
    () => components.filter((component) => component.type === 'protein' || component.type === 'ligand'),
    [components]
  );
  const proteinComponents = useMemo(
    () => supportedComponents.filter((component) => component.type === 'protein'),
    [supportedComponents]
  );
  const chainPlan = useMemo(
    () => buildVirtualScreeningChainPlan(supportedComponents),
    [supportedComponents]
  );
  const chainIdsByComponent = useMemo(() => {
    const result = new Map<string, string[]>();
    supportedComponents.forEach((component, index) => {
      result.set(component.id, chainPlan.assignments[index] || []);
    });
    return result;
  }, [chainPlan.assignments, supportedComponents]);
  const componentBuckets = useMemo<Record<MoleculeType, SidebarComponentEntry[]>>(() => {
    const result: Record<MoleculeType, SidebarComponentEntry[]> = {
      protein: [],
      ligand: [],
      dna: [],
      rna: []
    };
    components.forEach((component, index) => {
      const bucket = result[component.type];
      bucket.push({
        component,
        globalOrder: index + 1,
        typeOrder: bucket.length + 1
      });
    });
    return result;
  }, [components]);

  const incompleteCount = useMemo(() => {
    const incompleteComponents = components.filter((component) => !isComponentReady(component)).length;
    return incompleteComponents + (proteinComponents.length === 0 ? 1 : 0);
  }, [components, proteinComponents.length]);
  const allComponentsReady = components.length > 0 && incompleteCount === 0;

  useEffect(() => {
    let cancelled = false;
    if (!parsed.compounds.length || parsed.errors.length) {
      setValidationState('idle');
      setInvalidIndexes(new Set());
      setValidationWarnings([]);
      return () => {
        cancelled = true;
      };
    }
    setValidationState('checking');
    const timer = window.setTimeout(() => {
      void validateVirtualScreeningSmiles(parsed.compounds)
        .then((result) => {
          if (cancelled) return;
          setInvalidIndexes(new Set(result.invalid.map((item) => item.index)));
          setValidationWarnings(result.warnings);
          setValidationState('ready');
        })
        .catch(() => {
          if (cancelled) return;
          setInvalidIndexes(new Set());
          setValidationWarnings([]);
          setValidationState('unavailable');
        });
    }, 350);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [parsed.compounds, parsed.errors.length]);

  if (!visible) return null;

  const handleComponentsChange = (nextComponents: InputComponent[]) => {
    onComponentsChange(nextComponents.map((component) => (
      component.type === 'protein'
        ? { ...component, useMsa: false, cyclic: false, modifications: [] }
        : component
    )));
  };

  const addComponent = (type: MoleculeType) => {
    if (!canEdit || DISABLED_COMPONENT_TYPES.includes(type)) return;
    const created = createInputComponent(type);
    const next = type === 'protein'
      ? { ...created, useMsa: false, cyclic: false, modifications: [] }
      : created;
    onComponentsChange([...components, next]);
    onActiveComponentIdChange(next.id);
    setSidebarTypeOpen((current) => ({ ...current, [type]: true }));
  };

  const switchLibraryMode = (mode: VirtualScreeningInputMode): boolean => {
    if (!canEdit || mode === screeningInputMode) return mode === screeningInputMode;
    if (
      screeningInput.trim()
      && typeof window !== 'undefined'
      && !window.confirm('Switching the ligand-library source clears the current library. Continue?')
    ) {
      return false;
    }
    onScreeningLibraryChange({ value: '', mode, fileName: '' });
    setLibraryError('');
    return true;
  };

  const importLibraryFile = async (file: File | null) => {
    if (!file) return;
    try {
      onScreeningLibraryChange({ value: await file.text(), mode: 'upload', fileName: file.name });
      setLibraryError('');
    } catch {
      setLibraryError('Unable to read this ligand-library file.');
    } finally {
      if (uploadRef.current) uploadRef.current.value = '';
    }
  };

  const clearLibrary = () => {
    onScreeningLibraryChange({ value: '', mode: screeningInputMode, fileName: '' });
    setLibraryError('');
  };

  const loadExample = () => {
    if (screeningInputMode !== 'paste' && !switchLibraryMode('paste')) return;
    onScreeningLibraryChange({ value: VIRTUAL_SCREENING_EXAMPLE, mode: 'paste', fileName: '' });
  };

  const validCount = Math.max(0, parsed.compounds.length - invalidIndexes.size);
  const workspaceClassName = [
    'inputs-workspace',
    'components-resizable',
    'virtual-screening-workspace',
    isComponentsResizing ? 'is-resizing' : ''
  ].filter(Boolean).join(' ');

  return (
    <div
      ref={componentsWorkspaceRef as RefObject<HTMLDivElement>}
      className={workspaceClassName}
      style={componentsGridStyle}
    >
      <div className="inputs-main virtual-screening-components-main">
        <ComponentInputEditor
          components={components}
          onChange={handleComponentsChange}
          allowProteinMsa={false}
          allowProteinTemplates={false}
          allowProteinCyclic={false}
          allowProteinModifications={false}
          disabledComponentTypes={DISABLED_COMPONENT_TYPES}
          selectedComponentId={activeComponentId}
          onSelectedComponentIdChange={onActiveComponentIdChange}
          showQuickAdd={false}
          disabled={!canEdit}
          compact
        />

        <section className="component-card component-tone-slate panel subtle virtual-screening-library-card">
          <div className="component-card-head">
            <strong className="component-card-title">
              <span className="component-type-pill type-ligand">
                <FlaskConical size={13} aria-hidden />
              </span>
              Ligand library
            </strong>
            <div className="component-card-actions">
              <span className="component-count-chip">
                {parsed.compounds.length.toLocaleString()} ligands
              </span>
              <button
                type="button"
                className="icon-btn"
                title="Load example ligands"
                aria-label="Load example ligands"
                onClick={loadExample}
                disabled={!canEdit}
              >
                <RotateCcw size={14} />
              </button>
              <button
                type="button"
                className="icon-btn"
                title="Clear ligand library"
                aria-label="Clear ligand library"
                onClick={clearLibrary}
                disabled={!canEdit || !screeningInput}
              >
                <Trash2 size={14} />
              </button>
            </div>
          </div>

          <div className="virtual-screening-source-switch" role="tablist" aria-label="Ligand library source">
            <button
              type="button"
              role="tab"
              aria-selected={screeningInputMode === 'upload'}
              className={[
                'virtual-screening-source-tab',
                screeningInputMode === 'upload' ? 'active' : ''
              ].filter(Boolean).join(' ')}
              onClick={() => switchLibraryMode('upload')}
              disabled={!canEdit}
            >
              <FileUp size={14} />
              Upload file
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={screeningInputMode === 'paste'}
              className={[
                'virtual-screening-source-tab',
                screeningInputMode === 'paste' ? 'active' : ''
              ].filter(Boolean).join(' ')}
              onClick={() => switchLibraryMode('paste')}
              disabled={!canEdit}
            >
              <ClipboardPaste size={14} />
              Paste SMILES
            </button>
          </div>

          {screeningInputMode === 'upload' ? (
            <div className="virtual-screening-library-source-panel">
              <label className="field">
                <span>SMILES / CSV / TSV file</span>
                <input
                  ref={uploadRef}
                  type="file"
                  className="file-input-unified"
                  accept=".smi,.smiles,.txt,.csv,.tsv,.tab,text/plain,text/csv,text/tab-separated-values"
                  onChange={(event) => {
                    void importLibraryFile(event.currentTarget.files?.[0] || null);
                  }}
                  disabled={!canEdit}
                />
              </label>
              <p className="small muted virtual-screening-source-caption">
                {screeningInputFileName
                  ? 'Loaded: ' + screeningInputFileName
                  : screeningInput
                    ? 'Library file loaded'
                    : 'No library file selected'}
              </p>
            </div>
          ) : (
            <label className="field virtual-screening-library-editor-field">
              <span>SMILES / CSV / TSV text</span>
              <textarea
                className="virtual-screening-editor"
                rows={9}
                value={screeningInput}
                onChange={(event) => onScreeningLibraryChange({
                  value: event.target.value,
                  mode: 'paste',
                  fileName: ''
                })}
                placeholder={VIRTUAL_SCREENING_EXAMPLE}
                spellCheck={false}
                disabled={!canEdit}
              />
            </label>
          )}

          {libraryError ? <p className="lead-opt-error">{libraryError}</p> : null}
          <div className="virtual-screening-validation" aria-live="polite">
            {parsed.errors[0] ? (
              <span className="text-error">{parsed.errors[0]}</span>
            ) : parsed.compounds.length > 200 ? (
              <span className="text-warning">Maximum batch size is 200 ligands.</span>
            ) : validationState === 'checking' ? (
              <span className="muted">Checking SMILES...</span>
            ) : validationState === 'ready' && invalidIndexes.size ? (
              <span className="text-error">
                {invalidIndexes.size} invalid / {validCount} valid
              </span>
            ) : validationState === 'ready' && parsed.compounds.length ? (
              <span className="text-success">{validCount} valid SMILES</span>
            ) : validationState === 'unavailable' ? (
              <span className="muted">Validation pending</span>
            ) : (
              <span className="muted">No screening library loaded.</span>
            )}
            {[...parsed.warnings, ...validationWarnings].slice(0, 1).map((warning) => (
              <span key={warning} className="text-warning">{warning}</span>
            ))}
          </div>

          {parsed.compounds.length > 0 ? (
            <>
              <div className="lead-opt-result-table-wrap virtual-screening-library-table-wrap">
                <table className="lead-opt-result-table virtual-screening-library-table">
                  <thead>
                    <tr>
                      <th className="col-rank">#</th>
                      <th>Name</th>
                      <th>SMILES (binder)</th>
                    </tr>
                  </thead>
                  <tbody>
                    {parsed.compounds.slice(0, 12).map((compound, index) => (
                      <tr
                        key={compound.id + '-' + index}
                        className={invalidIndexes.has(index) ? 'is-invalid' : ''}
                      >
                        <td className="col-rank">{index + 1}</td>
                        <td title={compound.name}>{compound.name}</td>
                        <td className="col-smiles" title={compound.smiles}>{compound.smiles}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              {parsed.compounds.length > 12 ? (
                <p className="small muted virtual-screening-library-more">
                  +{parsed.compounds.length - 12} ligands
                </p>
              ) : null}
            </>
          ) : null}
        </section>
      </div>

      <div
        className={['panel-resizer', isComponentsResizing ? 'dragging' : ''].filter(Boolean).join(' ')}
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize components and component navigation"
        tabIndex={0}
        onPointerDown={onComponentsResizerPointerDown}
        onKeyDown={onComponentsResizerKeyDown}
      />

      <aside className="component-sidebar virtual-screening-components-sidebar">
        <div className="component-sidebar-head">
          <div className="component-sidebar-head-meta">
            <span className="component-count-chip">{components.length} items</span>
            <span className={[
              'component-readiness-chip',
              allComponentsReady ? 'complete' : 'incomplete'
            ].join(' ')}>
              {allComponentsReady ? 'All ready' : incompleteCount + ' missing'}
            </span>
          </div>
        </div>

        {COMPONENT_TYPES.map((type) => {
          const entries = componentBuckets[type];
          const unsupported = DISABLED_COMPONENT_TYPES.includes(type);
          return (
            <section
              className={[
                'component-sidebar-section',
                unsupported ? 'virtual-screening-type-unsupported' : ''
              ].filter(Boolean).join(' ')}
              key={'virtual-screening-type-' + type}
            >
              <div className="component-tree-row">
                <button
                  type="button"
                  className="component-sidebar-toggle"
                  onClick={() => setSidebarTypeOpen((current) => ({
                    ...current,
                    [type]: !current[type]
                  }))}
                >
                  <span className="component-tree-label">
                    {sidebarTypeOpen[type]
                      ? <ChevronDown size={14} />
                      : <ChevronRight size={14} />}
                    {componentTypeIcon(type)}
                    <strong>{componentTypeLabel(type)}</strong>
                  </span>
                  <span className="muted small">{entries.length}</span>
                </button>
                <button
                  type="button"
                  className="icon-btn component-tree-add"
                  onClick={() => addComponent(type)}
                  disabled={!canEdit || unsupported}
                  title={unsupported
                    ? 'Nesso-1 does not support ' + componentTypeLabel(type)
                    : 'Add ' + componentTypeLabel(type)}
                  aria-label={unsupported
                    ? componentTypeLabel(type) + ' is not supported by Nesso-1'
                    : 'Add ' + componentTypeLabel(type)}
                >
                  <Plus size={14} />
                </button>
              </div>

              {sidebarTypeOpen[type] ? (
                <div className="component-sidebar-list component-sidebar-list-components">
                  {entries.length === 0 ? (
                    <div className="component-sidebar-empty muted small">
                      {unsupported
                        ? 'Not supported by Nesso-1.'
                        : 'No ' + componentTypeLabel(type).toLowerCase() + ' yet.'}
                    </div>
                  ) : entries.map((entry) => {
                    const ready = isComponentReady(entry.component);
                    const chainIds = chainIdsByComponent.get(entry.component.id) || [];
                    const entryClassName = [
                      'component-sidebar-link',
                      activeComponentId === entry.component.id ? 'active' : '',
                      ready ? 'is-complete' : 'is-incomplete'
                    ].filter(Boolean).join(' ');
                    return (
                      <button
                        key={entry.component.id}
                        type="button"
                        className={entryClassName}
                        onClick={() => onActiveComponentIdChange(entry.component.id)}
                      >
                        <span>
                          {componentTypeLabel(type)} {entry.typeOrder}
                        </span>
                        <span className={[
                          'small',
                          'component-sidebar-link-meta',
                          'component-sidebar-link-status',
                          ready ? 'is-complete' : 'is-incomplete'
                        ].join(' ')}>
                          {chainIds.length
                            ? 'Chain ' + chainIds.join(', ')
                            : 'Comp ' + entry.globalOrder + ' - ' + (ready ? 'ready' : 'incomplete')}
                        </span>
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </section>
          );
        })}

        <section className="component-sidebar-section">
          <div className="component-sidebar-toggle component-sidebar-toggle-static">
            <span className="component-tree-label">
              <Target size={13} />
              <strong>Affinity scoring</strong>
            </span>
          </div>
          <div className="component-sidebar-list component-sidebar-list-nested virtual-screening-scoring-map">
            <div className="virtual-screening-scoring-row">
              <span>Protein chains</span>
              <strong>{chainPlan.targetChainIds.join(', ') || '-'}</strong>
            </div>
            <div className="virtual-screening-scoring-row">
              <span>Fixed ligand chains</span>
              <strong>{chainPlan.contextLigandChainIds.join(', ') || '-'}</strong>
            </div>
            <div className="virtual-screening-scoring-row virtual-screening-scoring-binder">
              <span>Library binder</span>
              <strong>Each row -&gt; {chainPlan.binderChainId}</strong>
            </div>
          </div>
        </section>
      </aside>
    </div>
  );
}
