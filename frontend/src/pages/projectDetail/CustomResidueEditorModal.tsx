/**
 * Custom residue editor modal (controlled; draft state lives in the parent):
 * JSME editor, 2D preview with backbone-atom highlighting, slot assignment,
 * amidation toggle, validation status.
 */
import { JSMEEditor } from '../../components/project/JSMEEditor';
import { CUSTOM_RESIDUE_SCAFFOLD_SMILES } from './peptideCustomResidues';
import { MemoLigand2DPreview } from '../../components/project/Ligand2DPreview';
import { firstBackboneSlotError, generateCustomResidueCode } from '../../utils/constraintAtomOptions';
import { toggleTerminalAmide } from '../../utils/smilesTransform';

const CUSTOM_BACKBONE_SLOTS = ['n', 'ca', 'c', 'o', 'oxt'] as const;

const CUSTOM_BACKBONE_SLOT_LABELS: Record<string, string> = { n: 'N', ca: 'CA', c: 'C', o: 'O', oxt: 'OXT' };
const slotLabel = (slot: string) => CUSTOM_BACKBONE_SLOT_LABELS[slot] ?? slot.toUpperCase();

import type { CustomResidueBackbone } from '../../types/models';
import { Field } from '../../components/common/Field';

interface Props {
  isOpen: boolean;
  userId: string;
  editingCcd: string;
  isDisabled: boolean;
  draftSmiles: string;
  draftName: string;
  draftBaseResidue: string;
  draftBackbone: Partial<CustomResidueBackbone>;
  isDraftAmidated: boolean;
  isDraftValid: boolean;
  autoStatus: 'idle' | 'failed';
  slotErrors: Record<string, string>;
  armedSlot: string | null;
  assignedIndices: number[];
  backboneAtomLabels: string[] | null;
  onAtomClick: (atomIndex: number) => void;
  onResetBackbone: () => void;
  highlightColors: Record<number, [number, number, number]> | null;
  onSmilesChange: (v: string) => void;
  onNameChange: (v: string) => void;
  onBaseResidueChange: (v: string) => void;
  onAmidatedChange: (v: boolean) => void;
  onArmSlot: (v: string | null | ((prev: string | null) => string | null)) => void;
  onSave: () => void;
  onClose: () => void;
}

export function CustomResidueEditorModal(p: Props) {
  if (!p.isOpen) return null;
  return (
                  <div className="peptide-custom-editor">
                <div className="peptide-custom-editor-head">
                  <strong>{p.editingCcd ? 'Edit custom residue' : 'Add custom residue'}</strong>
                  <button type="button" className="btn btn-ghost btn-compact" onClick={p.onClose}>
                    Close
                  </button>
                </div>
                <div className="peptide-custom-editor-grid">
                  <Field label="CCD">
                    <input
                      value={p.editingCcd || generateCustomResidueCode(p.userId, p.draftSmiles.trim())}
                      readOnly
                      title="Auto-generated per user + residue; must be unique."
                    />
                  </Field>
                  <Field label="Name">
                    <input
                      value={p.draftName}
                      disabled={p.isDisabled}
                      onChange={(event) => p.onNameChange(event.target.value)}
                      placeholder="Custom residue"
                    />
                  </Field>
                  <Field label="Base residue">
                    <select
                      value={p.draftBaseResidue}
                      disabled={p.isDisabled}
                      onChange={(event) => p.onBaseResidueChange(event.target.value)}
                    >
                      {'ARNDCQEGHILKMFPSTWYV'.split('').map((aa) => (
                        <option key={aa} value={aa}>
                          {aa}
                        </option>
                      ))}
                    </select>
                  </Field>
                </div>
                <div className="peptide-custom-editor-main">
                  <div className="jsme-editor-container component-jsme-shell peptide-custom-jsme">
                    <JSMEEditor smiles={p.draftSmiles} height={360} onSmilesChange={p.onSmilesChange} />
                  </div>
                  <div className="peptide-custom-preview">
                    <MemoLigand2DPreview
                      smiles={p.draftSmiles}
                      width={240}
                      height={160}
                      highlightAtomIndices={p.assignedIndices.length ? p.assignedIndices : undefined}
                      highlightAtomColorsOverride={p.highlightColors}
                      atomLabels={p.backboneAtomLabels}
                      onAtomClick={p.armedSlot ? p.onAtomClick : undefined}
                    />
                    <div className="peptide-custom-backbone-slots" role="group" aria-label="Backbone atom slots">
                      {CUSTOM_BACKBONE_SLOTS.map((slot) => {
                        const idx = p.draftBackbone[slot];
                        const armed = p.armedSlot === slot;
                        return (
                          <button
                            key={slot}
                            type="button"
                            className={`peptide-custom-backbone-slot${armed ? ' armed' : ''}${idx === undefined ? ' empty' : ''}${p.slotErrors[slot] ? ' error' : ''}`}
                            onClick={() => p.onArmSlot((prev) => (prev === slot ? null : slot))}
                            title={
                              armed
                                ? `Click an atom in the 2D to assign ${slotLabel(slot)}`
                                : `Set ${slotLabel(slot)}${idx === undefined ? '' : ` (atom #${idx + 1})`}`
                            }
                          >
                            <span className="peptide-custom-backbone-slot-label">{slotLabel(slot)}</span>
                            <span className="peptide-custom-backbone-slot-value">{idx === undefined ? '—' : `#${idx + 1}`}</span>
                          </button>
                        );
                      })}
                    </div>
                    {firstBackboneSlotError(p.slotErrors) ? (
                      <span className="peptide-custom-invalid">{firstBackboneSlotError(p.slotErrors)}</span>
                    ) : null}
                    <div className="peptide-custom-backbone-foot">
                      <button
                        type="button"
                        className="peptide-custom-backbone-reset"
                        onClick={() => void p.onResetBackbone()}
                        title="Re-run auto backbone detection"
                      >
                        Auto
                      </button>
                      {!p.isDraftValid ? (
                        <span className="peptide-custom-invalid">Backbone N-CA-C(=O) is required.</span>
                      ) : null}
                      {p.autoStatus === 'failed' ? (
                        <span className="peptide-custom-invalid">
                          Auto could not identify the full backbone. Click atoms to set N/CA/C/O/OXT manually — for a C-terminal amide, enable amidation first.
                        </span>
                      ) : null}
                    </div>
                  </div>
                </div>
                <label className="field peptide-custom-smiles">
                  <span>Custom Residue SMILES</span>
                  <input
                    value={p.draftSmiles}
                    disabled={p.isDisabled}
                    onChange={(event) => p.onSmilesChange(event.target.value)}
                  />
                </label>
                <label className="switch-field peptide-custom-amidation">
                  <input
                    type="checkbox"
                    checked={p.isDraftAmidated}
                    disabled={p.isDisabled}
                    onChange={async (event) => {
                      const nextAmidated = event.target.checked;
                      const currentSmiles = String(p.draftSmiles || '').trim() || CUSTOM_RESIDUE_SCAFFOLD_SMILES;
                      // Flip the backbone's terminal atom (OXT <-> NXT), honoring the user's OXT pick.
                      // Atomic: if the terminal can't be resolved, leave flag and SMILES unchanged.
                      const transformed = await toggleTerminalAmide(currentSmiles, p.draftBackbone, nextAmidated);
                      if (!transformed || transformed === currentSmiles) return;
                      p.onAmidatedChange(nextAmidated);
                      p.onSmilesChange(transformed);
                    }}
                  />
                  <span>C-terminal amidation</span>
                </label>
                <div className="peptide-custom-editor-actions">
                  <button
                    type="button"
                    className="btn btn-primary btn-compact"
                    disabled={p.isDisabled || !p.draftSmiles.trim() || !p.isDraftValid || Boolean(firstBackboneSlotError(p.slotErrors))}
                    onClick={p.onSave}
                  >
                    Save residue
                  </button>
                </div>
              </div>
  );
}
