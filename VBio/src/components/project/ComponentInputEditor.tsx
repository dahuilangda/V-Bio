import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ChevronDown, ChevronRight, Dna, FlaskConical, Plus, Trash2 } from 'lucide-react';
import type { InputComponent, LigandInputMethod, MoleculeType, ProteinTemplateUpload } from '../../types/models';
import { componentTypeLabel, createInputComponent, normalizeComponentSequence } from '../../utils/projectInputs';
import { detectStructureFormat, extractProteinChainSequences } from '../../utils/structureParser';
import { JSMEEditor } from './JSMEEditor';
import { LigandPropertyGrid } from './LigandPropertyGrid';

interface ComponentInputEditorProps {
  components: InputComponent[];
  onChange: (components: InputComponent[]) => void;
  proteinTemplates?: Record<string, ProteinTemplateUpload>;
  onProteinTemplateChange?: (componentId: string, upload: ProteinTemplateUpload | null) => void;
  renderProteinTemplateViewer?: (args: { component: InputComponent; upload: ProteinTemplateUpload }) => ReactNode;
  selectedComponentId?: string | null;
  onSelectedComponentIdChange?: (id: string) => void;
  showQuickAdd?: boolean;
  disabled?: boolean;
  compact?: boolean;
}

const TYPE_OPTIONS: MoleculeType[] = ['protein', 'ligand', 'dna', 'rna'];
const LIGAND_INPUT_OPTIONS: LigandInputMethod[] = ['smiles', 'ccd', 'jsme'];
const QUICK_ADD_TYPES: MoleculeType[] = ['protein', 'ligand', 'dna', 'rna'];

function clampCopies(value: number): number {
  if (!Number.isFinite(value) || value < 1) return 1;
  if (value > 20) return 20;
  return Math.floor(value);
}

function summarizeText(value: string, limit = 42): string {
  const text = value.trim();
  if (!text) return 'No sequence/input yet';
  if (text.length <= limit) return text;
  return `${text.slice(0, limit)}...`;
}

export function ComponentInputEditor({
  components,
  onChange,
  proteinTemplates = {},
  onProteinTemplateChange,
  renderProteinTemplateViewer,
  selectedComponentId = null,
  onSelectedComponentIdChange,
  showQuickAdd = true,
  disabled = false,
  compact = false
}: ComponentInputEditorProps) {
  const [templateErrors, setTemplateErrors] = useState<Record<string, string>>({});
  const [collapsedById, setCollapsedById] = useState<Record<string, boolean>>({});
  const hasMountedSelectionEffectRef = useRef(false);

  useEffect(() => {
    if (!hasMountedSelectionEffectRef.current) {
      hasMountedSelectionEffectRef.current = true;
      return;
    }
    if (!selectedComponentId) return;
    const target = document.getElementById(`component-card-${selectedComponentId}`);
    if (target) {
      const targetTop = target.getBoundingClientRect().top + window.scrollY - 132;
      window.scrollTo({ top: Math.max(0, targetTop), behavior: 'smooth' });
    }
  }, [selectedComponentId]);

  useEffect(() => {
    setCollapsedById((prev) => {
      const next: Record<string, boolean> = {};
      let changed = false;
      for (const comp of components) {
        if (Object.prototype.hasOwnProperty.call(prev, comp.id)) {
          next[comp.id] = prev[comp.id];
        } else {
          next[comp.id] = false;
          changed = true;
        }
      }
      if (Object.keys(prev).length !== components.length) {
        changed = true;
      }
      return changed ? next : prev;
    });
  }, [components]);

  const patchOne = (id: string, patch: Partial<InputComponent>) => {
    onChange(
      components.map((comp) => {
        if (comp.id !== id) return comp;
        const nextType = patch.type ?? comp.type;
        const base = { ...comp, ...patch };
        return {
          ...base,
          sequence: normalizeComponentSequence(nextType, base.sequence || ''),
          useMsa: nextType === 'protein' ? Boolean(base.useMsa ?? true) : undefined,
          cyclic: nextType === 'protein' ? Boolean(base.cyclic ?? false) : undefined,
          inputMethod: nextType === 'ligand' ? (base.inputMethod ?? 'jsme') : undefined
        };
      })
    );
  };

  const removeOne = (id: string) => {
    if (components.length <= 1) return;
    if (onProteinTemplateChange) {
      onProteinTemplateChange(id, null);
    }
    onChange(components.filter((comp) => comp.id !== id));
  };

  const addComponent = (type: MoleculeType) => onChange([...components, createInputComponent(type)]);

  const toggleCollapsed = (id: string) => {
    setCollapsedById((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const typeBadge = (type: MoleculeType) => {
    if (type === 'protein') {
      return <Dna size={13} aria-hidden />;
    }
    if (type === 'ligand') {
      return <FlaskConical size={13} aria-hidden />;
    }
    return <Dna size={13} aria-hidden />;
  };

  const patchTemplate = (componentId: string, upload: ProteinTemplateUpload | null) => {
    if (!onProteinTemplateChange) return;
    onProteinTemplateChange(componentId, upload);
  };

  const applyTemplateChain = (componentId: string, chainId: string) => {
    const current = proteinTemplates[componentId];
    if (!current) return;
    const sequence = current.chainSequences[chainId] || '';
    patchTemplate(componentId, { ...current, chainId });
    if (sequence) {
      patchOne(componentId, { sequence });
    }
  };

  const handleProteinTemplateUpload = async (componentId: string, file: File | null) => {
    if (!file) {
      setTemplateErrors((prev) => ({ ...prev, [componentId]: '' }));
      patchTemplate(componentId, null);
      return;
    }
    const format = detectStructureFormat(file.name);
    if (!format) {
      setTemplateErrors((prev) => ({
        ...prev,
        [componentId]: 'Only .pdb, .cif or .mmcif files are supported.'
      }));
      patchTemplate(componentId, null);
      return;
    }

    try {
      const content = await file.text();
      const chainSequences = extractProteinChainSequences(content, format);
      const chainIds = Object.keys(chainSequences).sort((a, b) => a.localeCompare(b));
      if (!chainIds.length) {
        throw new Error('No protein chain could be parsed from the uploaded structure.');
      }
      const chainId = chainIds[0];
      const upload: ProteinTemplateUpload = {
        fileName: file.name,
        format,
        content,
        chainId,
        chainSequences
      };
      patchTemplate(componentId, upload);
      setTemplateErrors((prev) => ({ ...prev, [componentId]: '' }));
      const sequence = chainSequences[chainId];
      if (sequence) {
        patchOne(componentId, { sequence });
      }
    } catch (error) {
      setTemplateErrors((prev) => ({
        ...prev,
        [componentId]:
          error instanceof Error ? error.message : 'Failed to parse structure file. Please check the file content.'
      }));
      patchTemplate(componentId, null);
    }
  };

  return (
    <div className="component-editor">
      {showQuickAdd && (
        <div className="component-editor-head">
          <div className="component-add-quick">
            {QUICK_ADD_TYPES.map((type) => (
              <button
                key={type}
                type="button"
                className={`btn btn-ghost btn-compact component-add-kind type-${type}`}
                disabled={disabled}
                onClick={() => addComponent(type)}
                title={`Add ${componentTypeLabel(type)}`}
              >
                {typeBadge(type)}
                {componentTypeLabel(type)}
                <Plus size={12} />
              </button>
            ))}
          </div>
        </div>
      )}

      <div className="component-list">
        {components.map((comp, index) => {
          const method = comp.inputMethod ?? 'jsme';
          const componentLabel = componentTypeLabel(comp.type);
          const isLigand = comp.type === 'ligand';
          const isCollapsed = Boolean(collapsedById[comp.id]);
          const templateUpload = proteinTemplates[comp.id];
          const templateChainIds = Object.keys(templateUpload?.chainSequences || {}).sort((a, b) => a.localeCompare(b));
          const selectedTemplateSequence =
            templateUpload && templateUpload.chainId ? templateUpload.chainSequences[templateUpload.chainId] || '' : '';
          const hasLigandJsmeViewer = isLigand && method === 'jsme';
          const collapsedSummary =
            comp.type === 'ligand'
              ? `${method.toUpperCase()} · ${summarizeText(comp.sequence)}`
              : `${componentLabel} · ${summarizeText(comp.sequence)}`;

          return (
            <section
              id={`component-card-${comp.id}`}
              key={comp.id}
              className={`component-card component-card-${comp.type} component-tone-${index % 2 === 0 ? 'green' : 'slate'} panel subtle ${
                selectedComponentId === comp.id ? 'active' : ''
              } ${isCollapsed ? 'collapsed' : ''}`}
              onClick={() => onSelectedComponentIdChange?.(comp.id)}
            >
              <div className="component-card-head">
                <strong className="component-card-title">
                  <span className={`component-type-pill type-${comp.type}`}>{typeBadge(comp.type)}</span>
                  Component {index + 1}: {componentLabel}
                </strong>
                <div className="component-card-actions">
                  <button
                    type="button"
                    className="icon-btn"
                    onClick={(e) => {
                      e.stopPropagation();
                      toggleCollapsed(comp.id);
                    }}
                    disabled={disabled}
                    title={isCollapsed ? 'Expand component' : 'Collapse component'}
                    aria-label={isCollapsed ? 'Expand component' : 'Collapse component'}
                    aria-expanded={!isCollapsed}
                  >
                    {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    onClick={(e) => {
                      e.stopPropagation();
                      removeOne(comp.id);
                    }}
                    disabled={disabled || components.length <= 1}
                    title="Remove component"
                  >
                    <Trash2 size={14} />
                  </button>
                </div>
              </div>

              {isCollapsed && <div className="component-card-summary muted small">{collapsedSummary}</div>}

              {!isCollapsed && (
                <>
              <div className={`component-meta ${compact ? 'component-meta-compact' : ''}`}>
                <label className="field">
                  <span>Type</span>
                  <select
                    value={comp.type}
                    disabled={disabled}
                    onChange={(e) =>
                      patchOne(comp.id, {
                        type: e.target.value as MoleculeType,
                        sequence: '',
                        inputMethod: e.target.value === 'ligand' ? 'jsme' : undefined
                      })
                    }
                  >
                    {TYPE_OPTIONS.map((type) => (
                      <option key={type} value={type}>
                        {componentTypeLabel(type)}
                      </option>
                    ))}
                  </select>
                </label>

                <label className="field">
                  <span>Copies</span>
                  <input
                    type="number"
                    min={1}
                    max={20}
                    value={comp.numCopies}
                    disabled={disabled}
                    onChange={(e) => patchOne(comp.id, { numCopies: clampCopies(Number(e.target.value)) })}
                  />
                </label>

                {comp.type === 'protein' && (
                  <>
                    <label className="switch-field switch-tight">
                      <input
                        type="checkbox"
                        checked={comp.useMsa !== false}
                        disabled={disabled}
                        onChange={(e) => patchOne(comp.id, { useMsa: e.target.checked })}
                      />
                      <span>Use MSA</span>
                    </label>
                    <label className="switch-field switch-tight">
                      <input
                        type="checkbox"
                        checked={Boolean(comp.cyclic)}
                        disabled={disabled}
                        onChange={(e) => patchOne(comp.id, { cyclic: e.target.checked })}
                      />
                      <span>Cyclic</span>
                    </label>
                  </>
                )}
              </div>

              {comp.type === 'protein' && (
                <>
                <div className="component-content-split">
                  <div className="component-content-main">
                    <div className="field protein-template-upload">
                      <span>Protein Structure (optional)</span>
                      <input
                        type="file"
                        accept=".pdb,.cif,.mmcif"
                        disabled={disabled}
                        onChange={(e) => void handleProteinTemplateUpload(comp.id, e.target.files?.[0] || null)}
                      />
                      {templateUpload && (
                        <div className="template-upload-meta">
                          <span>
                            {templateUpload.fileName} · {templateUpload.format.toUpperCase()}
                          </span>
                          <button
                            type="button"
                            className="btn btn-ghost btn-compact"
                            disabled={disabled}
                            onClick={() => patchTemplate(comp.id, null)}
                          >
                            Remove
                          </button>
                        </div>
                      )}
                      {templateErrors[comp.id] && <span className="field-error">{templateErrors[comp.id]}</span>}
                    </div>

                    {templateUpload && (
                      <label className="field">
                        <span>Template Chain</span>
                        <select
                          value={templateUpload.chainId}
                          disabled={disabled}
                          onChange={(e) => applyTemplateChain(comp.id, e.target.value)}
                        >
                          {templateChainIds.map((chainId) => (
                            <option key={`${comp.id}-template-chain-${chainId}`} value={chainId}>
                              Chain {chainId} · {(templateUpload.chainSequences[chainId] || '').length} aa
                            </option>
                          ))}
                        </select>
                        {selectedTemplateSequence && (
                          <span className="muted small">
                            Sequence auto-filled from chain {templateUpload.chainId} ({selectedTemplateSequence.length} aa).
                          </span>
                        )}
                      </label>
                    )}

                    <label className="field">
                      <span>Protein Sequence</span>
                      <textarea
                        rows={compact ? 4 : 6}
                        placeholder="Example: MKTIIALSYIFCLVFA..."
                        value={comp.sequence}
                        disabled={disabled}
                        onChange={(e) => patchOne(comp.id, { sequence: e.target.value })}
                      />
                    </label>
                  </div>
                </div>
                {templateUpload && renderProteinTemplateViewer && (
                  <div className="field component-template-full">
                    {renderProteinTemplateViewer({ component: comp, upload: templateUpload })}
                  </div>
                )}
                </>
              )}

              {comp.type === 'dna' && (
                <label className="field">
                  <span>DNA Sequence</span>
                  <textarea
                    rows={compact ? 4 : 6}
                    placeholder="Example: ATGGCC..."
                    value={comp.sequence}
                    disabled={disabled}
                    onChange={(e) => patchOne(comp.id, { sequence: e.target.value })}
                  />
                </label>
              )}

              {comp.type === 'rna' && (
                <label className="field">
                  <span>RNA Sequence</span>
                  <textarea
                    rows={compact ? 4 : 6}
                    placeholder="Example: AUGGCC..."
                    value={comp.sequence}
                    disabled={disabled}
                    onChange={(e) => patchOne(comp.id, { sequence: e.target.value })}
                  />
                </label>
              )}

              {comp.type === 'ligand' && method === 'ccd' && (
                <div className="component-content-main ligand-input-left">
                  <label className="field">
                    <span>Ligand Input Mode</span>
                    <select
                      value={method}
                      disabled={disabled}
                      onChange={(e) => patchOne(comp.id, { inputMethod: e.target.value as LigandInputMethod, sequence: '' })}
                    >
                      {LIGAND_INPUT_OPTIONS.map((item) => (
                        <option key={item} value={item}>
                          {item.toUpperCase()}
                        </option>
                      ))}
                    </select>
                  </label>

                  <label className="field">
                    <span>CCD Code</span>
                    <input
                      placeholder="Example: ATP, NAD, HEM"
                      value={comp.sequence}
                      disabled={disabled}
                      onChange={(e) => patchOne(comp.id, { sequence: e.target.value })}
                    />
                  </label>
                </div>
              )}

              {comp.type === 'ligand' && method !== 'ccd' && (
                <div className={`component-content-split component-content-split-ligand ${hasLigandJsmeViewer ? 'has-side' : ''}`}>
                  <div className="component-content-main ligand-input-left">
                    <label className="field">
                      <span>Ligand Input Mode</span>
                      <select
                        value={method}
                        disabled={disabled}
                        onChange={(e) => patchOne(comp.id, { inputMethod: e.target.value as LigandInputMethod, sequence: '' })}
                      >
                        {LIGAND_INPUT_OPTIONS.map((item) => (
                          <option key={item} value={item}>
                            {item.toUpperCase()}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label className="field">
                      <span>SMILES</span>
                      <input
                        placeholder="Example: CC(=O)NC1=CC=C(C=C1)O"
                        value={comp.sequence}
                        disabled={disabled}
                        onChange={(e) => patchOne(comp.id, { sequence: e.target.value })}
                      />
                    </label>
                    <div className={`ligand-live-props ${hasLigandJsmeViewer ? 'align-bottom' : ''}`}>
                      <span>Live Ligand Properties</span>
                      <LigandPropertyGrid smiles={comp.sequence} />
                    </div>
                  </div>
                  {method === 'jsme' && (
                    <aside className="component-content-side ligand-input-right">
                      <div className="field component-side-field">
                        <span>JSME Molecule Editor</span>
                        <div className="jsme-editor-container component-jsme-shell">
                          <JSMEEditor
                            smiles={comp.sequence}
                            height={compact ? 320 : 380}
                            onSmilesChange={(value) => patchOne(comp.id, { sequence: value })}
                          />
                        </div>
                      </div>
                    </aside>
                  )}
                </div>
              )}
                </>
              )}
            </section>
          );
        })}
      </div>
    </div>
  );
}
