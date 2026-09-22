/**
 * The YAML Builder modal of the API access page: component cards (with
 * per-component templates and residue modifications), the constraints &
 * properties editor, and the generated-YAML preview. All state stays in
 * ApiAccessPage; the values arrive as five named prop groups (chrome,
 * components, templates, constraints, preview).
 */
import type {
  Dispatch,
  KeyboardEvent as ReactKeyboardEvent,
  PointerEvent as ReactPointerEvent,
  RefObject,
  SetStateAction
} from 'react';
import { ChevronDown, ChevronRight, Info, Plus, Trash2, X } from 'lucide-react';
import { Field } from '../components/common/Field';
import { ConstraintEditor } from '../components/project/ConstraintEditor';
import { JSMEEditor } from '../components/project/JSMEEditor';
import type { ModalDialogProps } from '../components/ui/useModalDialog';
import { CommandItem } from './ApiCommandItem';
import type {
  InputComponent,
  PredictionConstraint,
  PredictionProperties,
  ProteinModification,
  ProteinModificationInputMethod,
  ProteinModificationTerminal
} from '../types/models';
import { componentTypeLabel } from '../utils/projectInputs';
import { looksLikeAminoAcidBackboneSmiles } from '../utils/inputValidation';
import type { ApiBuilderGridStyle, YamlProteinTemplateConfig } from './apiAccessHelpers';
import {
  BUILDER_BUILT_IN_MODIFICATIONS,
  BUILDER_CUSTOM_RESIDUE_SCAFFOLD,
  buildBuilderCustomCcd,
  builderPositionForTerminal,
  builderResidueAt,
  builderSequenceLength,
  builderTerminalForPosition,
  clampBuilderModPosition
} from './apiAccessHelpers';
import './ApiYamlBuilderModal.css';

export interface ApiYamlBuilderChromeProps {
  dialogProps: ModalDialogProps;
  setYamlBuilderOpen: Dispatch<SetStateAction<boolean>>;
  gridRef: RefObject<HTMLDivElement | null>;
  gridStyle: ApiBuilderGridStyle;
  isResizing: boolean;
  onResizePointerDown: (event: ReactPointerEvent<HTMLDivElement>) => void;
  onResizeKeyDown: (event: ReactKeyboardEvent<HTMLDivElement>) => void;
}

export interface ApiYamlBuilderComponentsProps {
  builderYamlComponents: InputComponent[];
  builderYamlCollapsed: Record<string, boolean>;
  builderCustomResidueValidity: Record<string, boolean>;
  onAddComponent: (type: InputComponent['type']) => void;
  onUpdateComponent: (
    componentId: string,
    updater: (component: InputComponent) => InputComponent
  ) => void;
  onToggleComponentCollapsed: (componentId: string) => void;
  onRemoveComponent: (componentId: string) => void;
  onAddModification: (componentId: string) => void;
  onPatchModification: (
    componentId: string,
    modificationId: string,
    patch: Partial<ProteinModification>
  ) => void;
  onRemoveModification: (componentId: string, modificationId: string) => void;
  onValidateCustomSmiles: (modificationId: string, smiles: string) => void;
}

export interface ApiYamlBuilderTemplatesProps {
  builderYamlTemplates: Record<string, YamlProteinTemplateConfig>;
  setBuilderYamlTemplates: Dispatch<SetStateAction<Record<string, YamlProteinTemplateConfig>>>;
  updateYamlBuilderTemplate: (
    componentId: string,
    updater: (config: YamlProteinTemplateConfig) => YamlProteinTemplateConfig
  ) => void;
}

export interface ApiYamlBuilderConstraintsProps {
  builderYamlConstraints: PredictionConstraint[];
  setBuilderYamlConstraints: Dispatch<SetStateAction<PredictionConstraint[]>>;
  builderYamlProperties: PredictionProperties;
  setBuilderYamlProperties: Dispatch<SetStateAction<PredictionProperties>>;
  isBuilderYamlConstraintsOpen: boolean;
  setBuilderYamlConstraintsOpen: Dispatch<SetStateAction<boolean>>;
  normalizedYamlBuilderComponents: InputComponent[];
}

export interface ApiYamlBuilderPreviewProps {
  yamlComponentStats: Record<InputComponent['type'], number>;
  yamlBuilderText: string;
  copiedActionId: string | null;
  copyTextAction: (text: string, okMessage: string, historyLabel?: string, copyId?: string) => Promise<void>;
  onDownloadGeneratedYaml: () => void;
}

interface ApiYamlBuilderModalProps {
  chrome: ApiYamlBuilderChromeProps;
  components: ApiYamlBuilderComponentsProps;
  templates: ApiYamlBuilderTemplatesProps;
  constraints: ApiYamlBuilderConstraintsProps;
  preview: ApiYamlBuilderPreviewProps;
}

export function ApiYamlBuilderModal({
  chrome,
  components,
  templates,
  constraints,
  preview
}: ApiYamlBuilderModalProps) {
  const {
    dialogProps: yamlBuilderDialogProps,
    setYamlBuilderOpen,
    gridRef: yamlBuilderGridRef,
    gridStyle: yamlBuilderGridStyle,
    isResizing: isYamlBuilderResizing,
    onResizePointerDown: handleYamlBuilderResizerPointerDown,
    onResizeKeyDown: handleYamlBuilderResizerKeyDown
  } = chrome;
  const {
    builderYamlComponents,
    builderYamlCollapsed,
    builderCustomResidueValidity,
    onAddComponent,
    onUpdateComponent,
    onToggleComponentCollapsed,
    onRemoveComponent,
    onAddModification,
    onPatchModification,
    onRemoveModification,
    onValidateCustomSmiles
  } = components;
  const {
    builderYamlTemplates,
    setBuilderYamlTemplates,
    updateYamlBuilderTemplate
  } = templates;
  const {
    builderYamlConstraints,
    setBuilderYamlConstraints,
    builderYamlProperties,
    setBuilderYamlProperties,
    isBuilderYamlConstraintsOpen,
    setBuilderYamlConstraintsOpen,
    normalizedYamlBuilderComponents
  } = constraints;
  const {
    yamlComponentStats,
    yamlBuilderText,
    copiedActionId,
    copyTextAction,
    onDownloadGeneratedYaml
  } = preview;

  return (
    <div className="modal-mask" onClick={() => setYamlBuilderOpen(false)}>
      <div
        className="modal modal-wide api-yaml-modal"
        onClick={(e) => e.stopPropagation()}
        {...yamlBuilderDialogProps}
        aria-label="YAML Builder"
      >
        <div className="api-token-modal-head">
          <h2><Info size={17} /> YAML Builder</h2>
          <button
            className="icon-btn"
            type="button"
            aria-label="Close yaml builder"
            onClick={() => setYamlBuilderOpen(false)}
          >
            <X size={16} />
          </button>
        </div>

        <div
          ref={yamlBuilderGridRef as RefObject<HTMLDivElement>}
          className={`api-yaml-modal-body api-yaml-modal-body-resizable ${isYamlBuilderResizing ? 'is-resizing' : ''}`}
          style={yamlBuilderGridStyle}
        >
          <section className="api-yaml-modal-editor">
            <div className="api-builder-meta">
              <span className="badge">Protein {yamlComponentStats.protein}</span>
              <span className="badge">DNA {yamlComponentStats.dna}</span>
              <span className="badge">RNA {yamlComponentStats.rna}</span>
              <span className="badge">Ligand {yamlComponentStats.ligand}</span>
              <span className="badge">Constraints {builderYamlConstraints.length}</span>
              <span className="badge">Affinity {builderYamlProperties.affinity ? 'on' : 'off'}</span>
            </div>
            <div className="component-sidebar-list api-yaml-components api-yaml-components-flat">
              {builderYamlComponents.map((component, index) => (
                <article
                  key={component.id}
                  className={`api-yaml-component-card ${index % 2 === 0 ? 'api-yaml-component-card-odd' : 'api-yaml-component-card-even'}`}
                >
                  <header>
                    <button
                      className="btn btn-ghost api-yaml-collapse-btn"
                      type="button"
                      onClick={() => onToggleComponentCollapsed(component.id)}
                      aria-label="Toggle component details"
                    >
                      <ChevronRight size={13} className={builderYamlCollapsed[component.id] ? '' : 'api-icon-rotated'} />
                      <strong>Component {index + 1}</strong>
                      <span className="muted small">({componentTypeLabel(component.type)}, x{component.numCopies})</span>
                    </button>
                    <button className="icon-btn danger" type="button" aria-label="Remove component" onClick={() => onRemoveComponent(component.id)}>
                      <Trash2 size={14} />
                    </button>
                  </header>

                  {!builderYamlCollapsed[component.id] && (
                    <>
                      <div className="api-yaml-component-grid">
                                                      <Field label="Type">
                          <select
                            value={component.type}
                            onChange={(e) => {
                              const nextType = e.target.value === 'dna' || e.target.value === 'rna' || e.target.value === 'ligand' ? e.target.value : 'protein';
                              onUpdateComponent(component.id, (current) => {
                                const next: InputComponent = { ...current, type: nextType };
                                if (nextType === 'ligand') {
                                  next.inputMethod = current.inputMethod === 'ccd' ? 'ccd' : current.inputMethod === 'jsme' ? 'jsme' : 'smiles';
                                  delete next.useMsa;
                                  delete next.cyclic;
                                } else {
                                  next.useMsa = current.useMsa !== false;
                                  next.cyclic = Boolean(current.cyclic);
                                  delete next.inputMethod;
                                }
                                return next;
                              });
                              if (nextType !== 'protein') {
                                setBuilderYamlTemplates((prev) => {
                                  const next = { ...prev };
                                  delete next[component.id];
                                  return next;
                                });
                              }
                            }}
                          >
                            <option value="protein">protein</option>
                            <option value="dna">dna</option>
                            <option value="rna">rna</option>
                            <option value="ligand">ligand</option>
                          </select>
                          </Field>

                                                          <Field label="Copies">
                            <input
                            type="number"
                            min={1}
                            value={component.numCopies}
                            onChange={(e) => {
                              const copies = Math.max(1, Math.floor(Number(e.target.value) || 1));
                              onUpdateComponent(component.id, (current) => ({ ...current, numCopies: copies }));
                            }}
                          />
                          </Field>
                      </div>

                      {component.type === 'ligand' && (
                        <>
                                                          <Field label="Ligand input">
                            <select
                              value={component.inputMethod === 'ccd' ? 'ccd' : component.inputMethod === 'jsme' ? 'jsme' : 'smiles'}
                              onChange={(e) =>
                                onUpdateComponent(component.id, (current) => ({
                                  ...current,
                                  inputMethod: e.target.value === 'ccd' ? 'ccd' : e.target.value === 'jsme' ? 'jsme' : 'smiles'
                                }))
                              }
                            >
                              <option value="smiles">smiles</option>
                              <option value="jsme">jsme</option>
                              <option value="ccd">ccd</option>
                            </select>
                            </Field>
                          <Field label={component.inputMethod === 'ccd' ? 'CCD Code' : 'SMILES'}>
                            <input
                              value={component.sequence}
                              onChange={(e) => onUpdateComponent(component.id, (current) => ({ ...current, sequence: e.target.value }))}
                              placeholder={component.inputMethod === 'ccd' ? 'Example: ATP' : 'Example: CC(=O)NC1=CC=C(C=C1)O'}
                            />
                          </Field>
                          {component.inputMethod === 'jsme' && (
                            <div className="field">
                              <span>JSME Molecule Editor</span>
                              <div className="jsme-editor-container component-jsme-shell api-yaml-jsme-shell">
                                <JSMEEditor
                                  smiles={component.sequence}
                                  height={320}
                                  onSmilesChange={(value) =>
                                    onUpdateComponent(component.id, (current) => ({ ...current, sequence: value }))
                                  }
                                />
                              </div>
                            </div>
                          )}
                        </>
                      )}

                      {component.type !== 'ligand' && (
                                                  <Field label="Sequence">
                          <textarea
                            rows={3}
                            value={component.sequence}
                            onChange={(e) => onUpdateComponent(component.id, (current) => ({ ...current, sequence: e.target.value }))}
                            placeholder="Component sequence"
                          />
                          </Field>
                      )}

                      {component.type === 'protein' && (
                        <>
                          <div className="api-yaml-component-flags">
                            <label className="checkbox-inline">
                              <input
                                type="checkbox"
                                checked={component.useMsa !== false}
                                onChange={(e) => onUpdateComponent(component.id, (current) => ({ ...current, useMsa: e.target.checked }))}
                              />
                              <span>MSA</span>
                            </label>
                            <label className="checkbox-inline">
                              <input
                                type="checkbox"
                                checked={Boolean(component.cyclic)}
                                onChange={(e) => onUpdateComponent(component.id, (current) => ({ ...current, cyclic: e.target.checked }))}
                              />
                              <span>Cyclic</span>
                            </label>
                          </div>

                          <div className="api-yaml-builder api-yaml-modifications">
                            <div className="api-builder-meta">
                              <span className="badge">Residue Modifications {(component.modifications || []).length}</span>
                              <button type="button" className="btn btn-secondary btn-compact" onClick={() => onAddModification(component.id)}>
                                <Plus size={12} />
                                Add
                              </button>
                            </div>
                            {(component.modifications || []).map((mod, modIndex) => {
                              const terminal = builderTerminalForPosition(mod.position, component.sequence, mod.terminal);
                              const residue = builderResidueAt(component.sequence, mod.position) || mod.baseResidue || '-';
                              const customValid = mod.inputMethod !== 'jsme' || Boolean(builderCustomResidueValidity[mod.id] ?? looksLikeAminoAcidBackboneSmiles(mod.smiles || ''));
                              return (
                                <div key={mod.id} className="api-yaml-mod-row">
                                  <strong>#{modIndex + 1}</strong>
                                                                              <Field label="Position">
                                    <input
                                      type="number"
                                      min={1}
                                      max={Math.max(1, builderSequenceLength(component.sequence) || 1)}
                                      value={mod.position}
                                      onChange={(e) => {
                                        const position = clampBuilderModPosition(Number(e.target.value), component.sequence);
                                        onPatchModification(component.id, mod.id, {
                                          position,
                                          terminal: builderTerminalForPosition(position, component.sequence),
                                          baseResidue: builderResidueAt(component.sequence, position) || mod.baseResidue
                                        });
                                      }}
                                    />
                                    </Field>
                                                                              <Field label="Site">
                                    <select
                                      value={terminal}
                                      onChange={(e) => {
                                        const nextTerminal = e.target.value as ProteinModificationTerminal;
                                        const position = builderPositionForTerminal(nextTerminal, mod.position, component.sequence);
                                        onPatchModification(component.id, mod.id, {
                                          terminal: nextTerminal,
                                          position,
                                          baseResidue: builderResidueAt(component.sequence, position) || mod.baseResidue
                                        });
                                      }}
                                    >
                                      <option value="internal">Internal</option>
                                      <option value="n_term">N-term</option>
                                      <option value="c_term">C-term</option>
                                    </select>
                                    </Field>
                                                                              <Field label="Residue">
                                      <input value={residue} readOnly />
                                    </Field>
                                    <Field label="Source">
                                      <select
                                        value={mod.inputMethod}
                                        onChange={(e) => {
                                          const inputMethod = (e.target.value === 'jsme' ? 'jsme' : 'ccd') as ProteinModificationInputMethod;
                                          const fallback = BUILDER_BUILT_IN_MODIFICATIONS.find((item) => item.baseResidue === residue) || BUILDER_BUILT_IN_MODIFICATIONS[0];
                                          const smiles = mod.smiles || BUILDER_CUSTOM_RESIDUE_SCAFFOLD;
                                          onPatchModification(component.id, mod.id, {
                                            inputMethod,
                                            ccd: inputMethod === 'jsme' ? buildBuilderCustomCcd(component.id, mod.position, smiles) : fallback.ccd,
                                            smiles: inputMethod === 'jsme' ? smiles : undefined,
                                            label: inputMethod === 'jsme' ? 'Custom residue' : fallback.label,
                                            customEditorCollapsed: true
                                          });
                                          if (inputMethod === 'jsme') onValidateCustomSmiles(mod.id, smiles);
                                        }}
                                      >
                                        <option value="ccd">Built-in CCD</option>
                                        <option value="jsme">Custom SMILES</option>
                                      </select>
                                    </Field>
                                  {mod.inputMethod === 'ccd' ? (
                                                                                  <Field label="CCD">
                                      <select
                                        value={BUILDER_BUILT_IN_MODIFICATIONS.some((item) => item.ccd === mod.ccd) ? mod.ccd : BUILDER_BUILT_IN_MODIFICATIONS[0].ccd}
                                        onChange={(e) => {
                                          const selected = BUILDER_BUILT_IN_MODIFICATIONS.find((item) => item.ccd === e.target.value) || BUILDER_BUILT_IN_MODIFICATIONS[0];
                                          onPatchModification(component.id, mod.id, { ccd: selected.ccd, label: selected.label, baseResidue: residue });
                                        }}
                                      >
                                        {BUILDER_BUILT_IN_MODIFICATIONS.map((item) => (
                                          <option key={item.ccd} value={item.ccd}>{item.label} ({item.ccd})</option>
                                        ))}
                                      </select>
                                      </Field>
                                  ) : (
                                    <>
                                      <label className="field api-yaml-mod-smiles">
                                        <span>Residue SMILES</span>
                                        <input
                                          value={mod.smiles || BUILDER_CUSTOM_RESIDUE_SCAFFOLD}
                                          onChange={(e) => {
                                            const smiles = e.target.value;
                                            onPatchModification(component.id, mod.id, { smiles, ccd: buildBuilderCustomCcd(component.id, mod.position, smiles) });
                                            onValidateCustomSmiles(mod.id, smiles);
                                          }}
                                        />
                                      </label>
                                      <span className={`api-yaml-mod-status ${customValid ? 'valid' : 'invalid'}`}>
                                        {customValid ? 'Backbone OK' : 'Needs N-CA-C(=O) backbone'}
                                      </span>
                                    </>
                                  )}
                                  <button type="button" className="icon-btn danger" aria-label="Remove residue modification" onClick={() => onRemoveModification(component.id, mod.id)}>
                                    <Trash2 size={13} />
                                  </button>
                                </div>
                              );
                            })}
                          </div>

                          <div className="api-yaml-builder">
                                                              <Field label="Template absolute path (optional)">
                              <input
                                value={builderYamlTemplates[component.id]?.path || ''}
                                onChange={(e) => updateYamlBuilderTemplate(component.id, (current) => ({ ...current, path: e.target.value }))}
                                placeholder="/abs/path/template.cif"
                              />
                              </Field>
                            <div className="api-yaml-builder-grid api-yaml-template-grid">
                                                                      <Field label="Template format">
                                <select
                                  value={builderYamlTemplates[component.id]?.format || 'auto'}
                                  onChange={(e) =>
                                    updateYamlBuilderTemplate(component.id, (current) => ({
                                      ...current,
                                      format: e.target.value === 'pdb' ? 'pdb' : e.target.value === 'cif' ? 'cif' : 'auto'
                                    }))
                                  }
                                >
                                  <option value="auto">auto</option>
                                  <option value="pdb">pdb</option>
                                  <option value="cif">cif</option>
                                </select>
                                </Field>
                                                                      <Field label="Template chain">
                                  <input
                                    value={builderYamlTemplates[component.id]?.templateChain || ''}
                                    onChange={(e) => updateYamlBuilderTemplate(component.id, (current) => ({ ...current, templateChain: e.target.value }))}
                                    placeholder="A"
                                  />
                                  </Field>
                                                                      <Field label="Target chains">
                                  <input
                                    value={builderYamlTemplates[component.id]?.targetChains || ''}
                                    onChange={(e) => updateYamlBuilderTemplate(component.id, (current) => ({ ...current, targetChains: e.target.value }))}
                                    placeholder="A,B"
                                  />
                                  </Field>
                            </div>
                          </div>
                        </>
                      )}
                    </>
                  )}
                </article>
              ))}
            </div>
            <div className="api-yaml-component-toolbar api-yaml-component-toolbar-bottom">
              <span className="muted small">Add component below</span>
              <div className="api-yaml-component-toolbar-actions">
                <button type="button" className="btn btn-secondary btn-compact" onClick={() => onAddComponent('protein')}>
                  <Plus size={13} /> Protein
                </button>
                <button type="button" className="btn btn-secondary btn-compact" onClick={() => onAddComponent('ligand')}>
                  <Plus size={13} /> Ligand
                </button>
                <button type="button" className="btn btn-secondary btn-compact" onClick={() => onAddComponent('dna')}>
                  <Plus size={13} /> DNA
                </button>
                <button type="button" className="btn btn-secondary btn-compact" onClick={() => onAddComponent('rna')}>
                  <Plus size={13} /> RNA
                </button>
              </div>
            </div>
            <section className="api-yaml-constraints">
              <button
                className="btn btn-ghost api-yaml-collapse-btn api-yaml-constraints-toggle"
                type="button"
                onClick={() => setBuilderYamlConstraintsOpen((prev) => !prev)}
                aria-expanded={isBuilderYamlConstraintsOpen}
                aria-label="Toggle constraints and properties editor"
              >
                {isBuilderYamlConstraintsOpen ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
                <strong>Constraints &amp; Properties</strong>
                <span className="muted small">
                  {builderYamlConstraints.length} constraint{builderYamlConstraints.length === 1 ? '' : 's'}
                </span>
              </button>
              {isBuilderYamlConstraintsOpen && (
                <div className="api-yaml-constraints-body">
                  <ConstraintEditor
                    components={normalizedYamlBuilderComponents}
                    constraints={builderYamlConstraints}
                    properties={builderYamlProperties}
                    onConstraintsChange={setBuilderYamlConstraints}
                    onPropertiesChange={setBuilderYamlProperties}
                    isAffinitySectionVisible
                  />
                </div>
              )}
            </section>
          </section>

          <div
            className={`panel-resizer ${isYamlBuilderResizing ? 'dragging' : ''}`}
            role="separator"
            aria-orientation="vertical"
            aria-label="Resize YAML Builder panels"
            tabIndex={0}
            onPointerDown={handleYamlBuilderResizerPointerDown}
            onKeyDown={handleYamlBuilderResizerKeyDown}
          />

          <section className="api-yaml-modal-preview">
            <CommandItem
              index=""
              title="Generated YAML"
              command={yamlBuilderText}
              isCopied={copiedActionId === 'copy-yaml-modal'}
              onCopy={() => { void copyTextAction(yamlBuilderText, 'Generated YAML copied.', 'YAML Builder', 'copy-yaml-modal'); }}
              extraAction={{ label: 'Download generated YAML', onClick: onDownloadGeneratedYaml }}
            />
          </section>
        </div>
      </div>
    </div>
  );
}
