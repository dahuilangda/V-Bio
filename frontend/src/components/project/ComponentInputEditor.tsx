import { useEffect, useRef, useState, type ReactNode } from 'react';
import { ChevronDown, ChevronRight, Dna, FlaskConical, Plus, Trash2 } from 'lucide-react';
import type { CustomCcdMoleculeInput, InputComponent, LigandInputMethod, MoleculeType, ProteinModification, ProteinModificationInputMethod, ProteinModificationTerminal, ProteinTemplateUpload } from '../../types/models';
import { componentTypeLabel, createInputComponent, normalizeComponentSequence, randomId } from '../../utils/projectInputs';
import { detectStructureFormat, extractProteinChainSequences } from '../../utils/structureParser';
import { JSMEEditor } from './JSMEEditor';
import { LigandPropertyGrid } from './LigandPropertyGrid';
import { loadRDKitModule } from '../../utils/rdkit';
import { renderLigand2DSvg } from '../../utils/ligand2d';
import { AMINO_ACID_BACKBONE_SMARTS, rdkitMolHasAminoAcidBackbone } from '../../utils/inputValidation';
import { BUILT_IN_PROTEIN_MODIFICATIONS } from './residueCatalog';

interface ComponentInputEditorProps {
  components: InputComponent[];
  onChange: (components: InputComponent[]) => void;
  proteinTemplates?: Record<string, ProteinTemplateUpload>;
  customResidueLibrary?: CustomCcdMoleculeInput[];
  onCustomResidueLibraryChange?: (library: CustomCcdMoleculeInput[]) => void;
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
const CUSTOM_RESIDUE_SCAFFOLD_SMILES = 'N[C@@H](C)C(=O)O';

function hashTextForCode(value: string): string {
  let hash = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    hash ^= value.charCodeAt(i);
    hash += (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24);
  }
  return (hash >>> 0).toString(36).toUpperCase().slice(0, 5);
}

function buildCustomModificationCode(componentId: string, position: number, smiles = ''): string {
  return `U${Math.max(1, Math.floor(position)).toString(36).toUpperCase()}${hashTextForCode(`${componentId}:${position}:${smiles}`)}`.slice(0, 5);
}

function normalizeModificationCcd(value: string): string {
  return value.replace(/[^A-Za-z0-9_-]/g, '').toUpperCase().slice(0, 12);
}

function clampModificationPosition(value: number, sequence: string): number {
  const max = Math.max(1, sequence.replace(/\s+/g, '').length || 1);
  if (!Number.isFinite(value) || value < 1) return 1;
  return Math.min(max, Math.floor(value));
}

function readResidueAt(sequence: string, position: number): string {
  const cleaned = sequence.replace(/\s+/g, '').toUpperCase();
  return cleaned[Math.max(0, position - 1)] || '';
}

function sequenceLength(sequence: string): number {
  return sequence.replace(/\s+/g, '').length;
}

function terminalForPosition(position: number, sequence: string, requested?: ProteinModificationTerminal): ProteinModificationTerminal {
  if (requested === 'n_term') return 'n_term';
  if (requested === 'c_term') return 'c_term';
  const length = sequenceLength(sequence);
  if (Math.floor(Number(position)) === 1) return 'n_term';
  if (length > 0 && Math.floor(Number(position)) === length) return 'c_term';
  return 'internal';
}

function positionForTerminal(terminal: ProteinModificationTerminal, currentPosition: number, sequence: string): number {
  if (terminal === 'n_term') return 1;
  if (terminal === 'c_term') return Math.max(1, sequenceLength(sequence) || 1);
  return clampModificationPosition(currentPosition, sequence);
}

function terminalLabel(terminal: ProteinModificationTerminal | undefined, position: number, sequence: string): string {
  const resolved = terminalForPosition(position, sequence, terminal);
  if (resolved === 'n_term') return 'N-term';
  if (resolved === 'c_term') return 'C-term';
  return 'Internal';
}


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



function modificationTone(index: number): string {
  return `tone-${(index % 6) + 1}`;
}

function ProteinSequenceModificationPreview({
  sequence,
  modifications,
  activeModificationId,
  disabled = false,
  onSelectModification,
  onPlaceActiveModification
}: {
  sequence: string;
  modifications: ProteinModification[];
  activeModificationId: string | null;
  disabled?: boolean;
  onSelectModification: (id: string, scrollToRow?: boolean) => void;
  onPlaceActiveModification: (position: number) => void;
}) {
  const clean = sequence.replace(/\s+/g, '').toUpperCase();
  if (!clean || modifications.length === 0) return null;
  const hasActiveModification = modifications.some((item) => item.id === activeModificationId);
  const byPosition = new Map<number, Array<{ mod: ProteinModification; index: number }>>();
  modifications.forEach((mod, index) => {
    const position = Math.max(1, Math.floor(Number(mod.position || 1)));
    const current = byPosition.get(position) || [];
    current.push({ mod, index });
    byPosition.set(position, current);
  });

  return (
    <div className="protein-sequence-mod-preview" aria-label="Protein sequence modification map">
      {clean.split('').map((residue, idx) => {
        const position = idx + 1;
        const mods = byPosition.get(position) || [];
        const primary = mods[0];
        const isActive = mods.some((item) => item.mod.id === activeModificationId);
        const activeIndexAtPosition = mods.findIndex((item) => item.mod.id === activeModificationId);
        const target = primary
          ? activeIndexAtPosition >= 0
            ? mods[(activeIndexAtPosition + 1) % mods.length]
            : primary
          : null;
        const canClick = Boolean(target || hasActiveModification);
        return (
          <button
            key={`seq-mod-${position}`}
            type="button"
            className={`protein-sequence-residue ${primary ? 'modified' : ''} ${primary ? modificationTone(primary.index) : ''} ${isActive ? 'active' : ''} ${
              canClick ? 'selectable' : ''
            }`}
            onClick={() => {
              if (disabled || !canClick) return;
              if (target) {
                onSelectModification(target.mod.id, true);
              } else {
                onPlaceActiveModification(position);
              }
            }}
            title={
              primary
                ? `#${primary.index + 1} ${primary.mod.ccd} at ${position}${mods.length > 1 ? ` · ${mods.length} modifications, click to cycle` : ''}`
                : hasActiveModification
                  ? `Move selected modification to residue ${position}`
                  : `Residue ${position}`
            }
            disabled={disabled || !canClick}
          >
            <span className="protein-sequence-residue-index">{position}</span>
            <span className="protein-sequence-residue-letter">{residue}</span>
            {primary ? <em>{primary.index + 1}</em> : null}
            {mods.length > 1 ? <b>+{mods.length - 1}</b> : null}
          </button>
        );
      })}
    </div>
  );
}

function CustomResiduePreview({ smiles, onValidityChange }: { smiles: string; onValidityChange: (valid: boolean) => void }) {
  const [svg, setSvg] = useState('');
  const [message, setMessage] = useState('Draw a complete amino-acid residue.');
  const [valid, setValid] = useState(false);

  useEffect(() => {
    let cancelled = false;
    const run = async () => {
      const text = smiles.trim();
      if (!text) {
        setSvg('');
        setMessage('Draw a complete amino-acid residue.');
        setValid(false);
        onValidityChange(false);
        return;
      }
      try {
        const rdkit = await loadRDKitModule();
        if (cancelled) return;
        const mol = rdkit.get_mol(text);
        if (!mol) throw new Error('Invalid SMILES.');
        mol.delete();
        const hasBackbone = rdkitMolHasAminoAcidBackbone(rdkit, text, true);
        const rendered = renderLigand2DSvg(rdkit, {
          smiles: text,
          width: 360,
          height: 220,
          highlightQuery: AMINO_ACID_BACKBONE_SMARTS
        });
        if (cancelled) return;
        setSvg(rendered);
        setValid(hasBackbone);
        setMessage(hasBackbone ? 'Amino-acid backbone detected.' : 'Missing amino-acid backbone: N-CA-C(=O).');
        onValidityChange(hasBackbone);
      } catch (error) {
        if (cancelled) return;
        setSvg('');
        setValid(false);
        setMessage(error instanceof Error ? error.message : 'Unable to validate residue.');
        onValidityChange(false);
      }
    };
    void run();
    return () => {
      cancelled = true;
    };
  }, [smiles, onValidityChange]);

  return (
    <div className={`custom-residue-preview ${valid ? 'valid' : 'invalid'}`}>
      <div className="custom-residue-preview-canvas" dangerouslySetInnerHTML={svg ? { __html: svg } : undefined}>
        {!svg ? <span className="muted small">No valid preview.</span> : null}
      </div>
      <span className="small custom-residue-preview-status">{message}</span>
    </div>
  );
}

export function ComponentInputEditor({
  components,
  onChange,
  proteinTemplates = {},
  customResidueLibrary = [],
  onCustomResidueLibraryChange,
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
  const [modificationsCollapsedById, setModificationsCollapsedById] = useState<Record<string, boolean>>({});
  const [customResidueValidity, setCustomResidueValidity] = useState<Record<string, boolean>>({});
  const [activeModificationId, setActiveModificationId] = useState<string | null>(null);
  const [numberDrafts, setNumberDrafts] = useState<Record<string, string>>({});
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

  const patchProteinModification = (componentId: string, modificationId: string, patch: Partial<ProteinModification>) => {
    const component = components.find((item) => item.id === componentId);
    if (!component || component.type !== 'protein') return;
    const modifications = (component.modifications || []).map((mod) => {
      if (mod.id !== modificationId) return mod;
      const next = { ...mod, ...patch };
      const terminal = terminalForPosition(Number(next.position), component.sequence, next.terminal);
      const position = positionForTerminal(terminal, Number(next.position), component.sequence);
      const residueAtPosition = readResidueAt(component.sequence, position);
      return {
        ...next,
        position,
        terminal,
        baseResidue: String(next.baseResidue || residueAtPosition || 'S').toUpperCase().slice(0, 1),
        ccd:
          next.inputMethod === 'jsme'
            ? buildCustomModificationCode(componentId, position, String(next.smiles || ''))
            : normalizeModificationCcd(String(next.ccd || '')),
        inputMethod: (next.inputMethod === 'jsme' ? 'jsme' : 'ccd') as ProteinModificationInputMethod
      };
    });
    patchOne(componentId, { modifications });
  };

  const addProteinModification = (componentId: string) => {
    const component = components.find((item) => item.id === componentId);
    if (!component || component.type !== 'protein') return;
    const defaultPosition = clampModificationPosition(1, component.sequence);
    const residueAtPosition = readResidueAt(component.sequence, defaultPosition);
    const builtin = BUILT_IN_PROTEIN_MODIFICATIONS.find((item) => item.baseResidue === residueAtPosition) || BUILT_IN_PROTEIN_MODIFICATIONS[0];
    const modification: ProteinModification = {
      id: randomId(),
      position: defaultPosition,
      terminal: terminalForPosition(defaultPosition, component.sequence),
      customEditorCollapsed: true,
      baseResidue: residueAtPosition || builtin.baseResidue,
      ccd: builtin.ccd,
      inputMethod: 'ccd',
      label: builtin.label
    };
    patchOne(componentId, { modifications: [...(component.modifications || []), modification] });
    setActiveModificationId(modification.id);
    setModificationsCollapsedById((prev) => ({ ...prev, [componentId]: false }));
  };

  const removeProteinModification = (componentId: string, modificationId: string) => {
    const component = components.find((item) => item.id === componentId);
    if (!component || component.type !== 'protein') return;
    patchOne(componentId, { modifications: (component.modifications || []).filter((mod) => mod.id !== modificationId) });
  };


  const saveModificationToLibrary = (mod: ProteinModification) => {
    if (!onCustomResidueLibraryChange || mod.inputMethod !== 'jsme') return;
    const ccd = normalizeModificationCcd(mod.ccd || '');
    const smiles = String(mod.smiles || '').trim();
    if (!ccd || !smiles || !customResidueValidity[mod.id]) return;
    const nextEntry: CustomCcdMoleculeInput = {
      ccd,
      smiles,
      baseResidue: String(mod.baseResidue || '').toUpperCase().slice(0, 1) || undefined,
      label: mod.label || 'Custom residue'
    };
    const nextLibrary = [nextEntry, ...customResidueLibrary.filter((item) => item.ccd !== ccd)].slice(0, 80);
    onCustomResidueLibraryChange(nextLibrary);
  };

  const applyLibraryResidueToModification = (componentId: string, modificationId: string, ccd: string) => {
    const entry = customResidueLibrary.find((item) => item.ccd === ccd);
    if (!entry) return;
    patchProteinModification(componentId, modificationId, {
      inputMethod: 'jsme',
      ccd: entry.ccd,
      smiles: entry.smiles,
      baseResidue: entry.baseResidue || undefined,
      label: entry.label || 'Custom residue'
    });
  };

  const removeLibraryResidue = (ccd: string) => {
    if (!onCustomResidueLibraryChange) return;
    onCustomResidueLibraryChange(customResidueLibrary.filter((item) => item.ccd !== ccd));
  };

  const selectProteinModification = (modificationId: string, scrollToRow = false) => {
    setActiveModificationId(modificationId);
    if (!scrollToRow) return;
    window.requestAnimationFrame(() => {
      document.getElementById(`protein-modification-row-${modificationId}`)?.scrollIntoView({
        behavior: 'smooth',
        block: 'nearest'
      });
    });
  };

  const setNumberDraft = (key: string, value: string) => {
    setNumberDrafts((prev) => ({ ...prev, [key]: value }));
  };

  const clearNumberDraft = (key: string) => {
    setNumberDrafts((prev) => {
      if (!Object.prototype.hasOwnProperty.call(prev, key)) return prev;
      const next = { ...prev };
      delete next[key];
      return next;
    });
  };

  const commitCopiesDraft = (component: InputComponent) => {
    const key = `copies:${component.id}`;
    const draft = numberDrafts[key];
    if (draft === undefined) return;
    patchOne(component.id, { numCopies: clampCopies(Number(draft)) });
    clearNumberDraft(key);
  };

  const commitModificationPositionDraft = (component: InputComponent, mod: ProteinModification) => {
    const key = `mod-position:${mod.id}`;
    const draft = numberDrafts[key];
    if (draft === undefined) return;
    const position = clampModificationPosition(Number(draft), component.sequence);
    patchProteinModification(component.id, mod.id, {
      position,
      terminal: terminalForPosition(position, component.sequence),
      baseResidue: readResidueAt(component.sequence, position) || mod.baseResidue
    });
    clearNumberDraft(key);
  };

  const toggleCollapsed = (id: string) => {
    setCollapsedById((prev) => ({ ...prev, [id]: !prev[id] }));
  };

  const toggleModificationsCollapsed = (id: string) => {
    setModificationsCollapsedById((prev) => ({ ...prev, [id]: !prev[id] }));
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

  const ligandJsmeHeight = compact ? 400 : 460;

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
          const proteinModifications = comp.modifications || [];
          const hasProteinModifications = comp.type === 'protein' && proteinModifications.length > 0;
          const areModificationsCollapsed = Boolean(modificationsCollapsedById[comp.id]);
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
                    value={numberDrafts[`copies:${comp.id}`] ?? String(comp.numCopies)}
                    disabled={disabled}
                    onChange={(e) => setNumberDraft(`copies:${comp.id}`, e.target.value)}
                    onBlur={() => commitCopiesDraft(comp)}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter') e.currentTarget.blur();
                      if (e.key === 'Escape') {
                        clearNumberDraft(`copies:${comp.id}`);
                        e.currentTarget.blur();
                      }
                    }}
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
                        className="file-input-unified"
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

                    {!hasProteinModifications ? (
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
                    ) : null}

                    <ProteinSequenceModificationPreview
                      sequence={comp.sequence}
                      modifications={proteinModifications}
                      activeModificationId={activeModificationId}
                      disabled={disabled}
                      onSelectModification={selectProteinModification}
                      onPlaceActiveModification={(position) => {
                        const active = (comp.modifications || []).find((item) => item.id === activeModificationId);
                        if (!active) return;
                        patchProteinModification(comp.id, active.id, {
                          position,
                          terminal: terminalForPosition(position, comp.sequence),
                          baseResidue: readResidueAt(comp.sequence, position) || active.baseResidue
                        });
                      }}
                    />

                    <div className={`protein-modifications ${areModificationsCollapsed ? 'collapsed' : ''}`}>
                      <div className="protein-modifications-head">
                        <button
                          type="button"
                          className="protein-modifications-title"
                          onClick={() => toggleModificationsCollapsed(comp.id)}
                          disabled={!hasProteinModifications}
                          aria-expanded={!areModificationsCollapsed}
                        >
                          {areModificationsCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                          <span>Residue Modifications</span>
                          {hasProteinModifications ? <em>{proteinModifications.length}</em> : null}
                        </button>
                        <button
                          type="button"
                          className="btn btn-ghost btn-compact"
                          disabled={disabled || !comp.sequence.trim()}
                          onClick={() => addProteinModification(comp.id)}
                        >
                          <Plus size={12} />
                          Add
                        </button>
                      </div>
                      {!hasProteinModifications ? (
                        <div className="protein-modifications-empty muted small">No residue modifications.</div>
                      ) : areModificationsCollapsed ? (
                        <div className="protein-modifications-summary muted small">
                          {proteinModifications.map((mod, modIndex) => `#${modIndex + 1} ${mod.ccd}@${mod.position}`).join(' · ')}
                        </div>
                      ) : (
                        <div className="protein-modification-list">
                          {proteinModifications.map((mod, modIndex) => {
                            const residueAtPosition = readResidueAt(comp.sequence, mod.position);
                            const builtinValue = BUILT_IN_PROTEIN_MODIFICATIONS.some((item) => item.ccd === mod.ccd)
                              ? mod.ccd
                              : '__custom_ccd__';
                            const terminal = terminalForPosition(mod.position, comp.sequence, mod.terminal);
                            const customCollapsed = mod.customEditorCollapsed !== false;
                            return (
                              <div
                                id={`protein-modification-row-${mod.id}`}
                                key={mod.id}
                                className={`protein-modification-row ${mod.inputMethod === 'jsme' ? 'is-custom' : ''} ${modificationTone(modIndex)} ${activeModificationId === mod.id ? 'active' : ''}`}
                                onFocusCapture={() => selectProteinModification(mod.id)}
                                onClick={() => selectProteinModification(mod.id)}
                              >
                                <div className={`protein-mod-number ${modificationTone(modIndex)}`}>#{modIndex + 1}</div>
                                <label className="field protein-mod-position">
                                  <span>Position</span>
                                  <input
                                    type="number"
                                    min={1}
                                    max={Math.max(1, comp.sequence.replace(/\s+/g, '').length || 1)}
                                    value={numberDrafts[`mod-position:${mod.id}`] ?? String(mod.position)}
                                    disabled={disabled}
                                    onChange={(e) => setNumberDraft(`mod-position:${mod.id}`, e.target.value)}
                                    onBlur={() => commitModificationPositionDraft(comp, mod)}
                                    onKeyDown={(e) => {
                                      if (e.key === 'Enter') e.currentTarget.blur();
                                      if (e.key === 'Escape') {
                                        clearNumberDraft(`mod-position:${mod.id}`);
                                        e.currentTarget.blur();
                                      }
                                    }}
                                  />
                                </label>
                                <label className="field protein-mod-terminal">
                                  <span>Site</span>
                                  <select
                                    value={terminal}
                                    disabled={disabled}
                                    onChange={(e) => {
                                      const nextTerminal = e.target.value as ProteinModificationTerminal;
                                      const position = positionForTerminal(nextTerminal, mod.position, comp.sequence);
                                      patchProteinModification(comp.id, mod.id, {
                                        terminal: nextTerminal,
                                        position,
                                        baseResidue: readResidueAt(comp.sequence, position) || mod.baseResidue
                                      });
                                    }}
                                  >
                                    <option value="internal">Internal</option>
                                    <option value="n_term">N-term</option>
                                    <option value="c_term">C-term</option>
                                  </select>
                                </label>
                                <label className="field protein-mod-residue">
                                  <span>Residue</span>
                                  <input value={residueAtPosition || mod.baseResidue || '-'} disabled readOnly />
                                </label>
                                <label className="field protein-mod-source">
                                  <span>Source</span>
                                  <select
                                    value={mod.inputMethod}
                                    disabled={disabled}
                                    onChange={(e) => {
                                      const nextMethod = e.target.value as ProteinModificationInputMethod;
                                      const builtin = BUILT_IN_PROTEIN_MODIFICATIONS.find((item) => item.baseResidue === (residueAtPosition || mod.baseResidue)) || BUILT_IN_PROTEIN_MODIFICATIONS[0];
                                      patchProteinModification(comp.id, mod.id, {
                                        inputMethod: nextMethod,
                                        ccd: nextMethod === 'jsme' ? buildCustomModificationCode(comp.id, mod.position, mod.smiles || '') : builtin.ccd,
                                        smiles: nextMethod === 'jsme' ? mod.smiles || CUSTOM_RESIDUE_SCAFFOLD_SMILES : undefined,
                                        label: nextMethod === 'jsme' ? 'Custom residue' : builtin.label,
                                        customEditorCollapsed: nextMethod === 'jsme' ? true : undefined
                                      });
                                    }}
                                  >
                                    <option value="ccd">Built-in CCD</option>
                                    <option value="jsme">Draw custom</option>
                                  </select>
                                </label>
                                {mod.inputMethod === 'ccd' ? (
                                  <label className="field protein-mod-ccd">
                                    <span>Modification</span>
                                    <select
                                      value={builtinValue}
                                      disabled={disabled}
                                      onChange={(e) => {
                                        const selected = BUILT_IN_PROTEIN_MODIFICATIONS.find((item) => item.ccd === e.target.value);
                                        if (selected) {
                                          patchProteinModification(comp.id, mod.id, {
                                            ccd: selected.ccd,
                                            baseResidue: residueAtPosition || selected.baseResidue,
                                            label: selected.label
                                          });
                                        } else {
                                          patchProteinModification(comp.id, mod.id, { ccd: mod.ccd });
                                        }
                                      }}
                                    >
                                      {Array.from(new Set(BUILT_IN_PROTEIN_MODIFICATIONS.map((item) => item.group))).map((group) => (
                                        <optgroup key={group} label={group}>
                                          {BUILT_IN_PROTEIN_MODIFICATIONS.filter((item) => item.group === group).map((item) => (
                                            <option key={item.ccd} value={item.ccd}>
                                              {item.label} ({item.ccd})
                                            </option>
                                          ))}
                                        </optgroup>
                                      ))}
                                      {builtinValue === '__custom_ccd__' && <option value="__custom_ccd__">{mod.ccd}</option>}
                                    </select>
                                  </label>
                                ) : (
                                  <label className="field protein-mod-ccd">
                                    <span>Code</span>
                                    <input value={mod.ccd || buildCustomModificationCode(comp.id, mod.position, mod.smiles || '')} disabled readOnly />
                                  </label>
                                )}
                                <button
                                  type="button"
                                  className="icon-btn protein-mod-remove"
                                  disabled={disabled}
                                  title="Remove modification"
                                  onClick={(e) => {
                                    e.stopPropagation();
                                    removeProteinModification(comp.id, mod.id);
                                  }}
                                >
                                  <Trash2 size={14} />
                                </button>
                                {mod.inputMethod === 'jsme' && (
                                  <div className={`protein-mod-jsme ${customCollapsed ? 'is-collapsed' : ''}`}>
                                    <div className="protein-mod-jsme-head">
                                      <button
                                        type="button"
                                        className="btn btn-ghost btn-compact protein-mod-jsme-toggle"
                                        disabled={disabled}
                                        aria-expanded={!customCollapsed}
                                        onClick={(e) => {
                                          e.stopPropagation();
                                          patchProteinModification(comp.id, mod.id, { customEditorCollapsed: !customCollapsed });
                                        }}
                                      >
                                        {customCollapsed ? <ChevronRight size={13} /> : <ChevronDown size={13} />}
                                        {customCollapsed ? 'Show custom editor' : 'Hide custom editor'}
                                      </button>
                                      <span className="muted small">{terminalLabel(terminal, mod.position, comp.sequence)} · {mod.ccd}</span>
                                    </div>
                                    {!customCollapsed && (
                                      <>
                                        <div className="protein-mod-jsme-main">
                                          <span className="protein-mod-jsme-title">JSME Molecule Editor</span>
                                          <div className="jsme-editor-container component-jsme-shell protein-mod-jsme-shell">
                                            <JSMEEditor
                                              smiles={mod.smiles || CUSTOM_RESIDUE_SCAFFOLD_SMILES}
                                              height={compact ? 420 : 500}
                                              onSmilesChange={(value) => patchProteinModification(comp.id, mod.id, { smiles: value })}
                                            />
                                          </div>
                                        </div>
                                        <div className="protein-mod-custom-side">
                                          {customResidueLibrary.length > 0 && (
                                            <div className="protein-mod-library">
                                              <label className="field">
                                                <span>Library</span>
                                                <select
                                                  value=""
                                                  disabled={disabled}
                                                  onChange={(e) => {
                                                    if (e.target.value) applyLibraryResidueToModification(comp.id, mod.id, e.target.value);
                                                  }}
                                                >
                                                  <option value="">Reuse saved residue</option>
                                                  {customResidueLibrary.map((item) => (
                                                    <option key={item.ccd} value={item.ccd}>
                                                      {item.label || 'Custom residue'} ({item.ccd})
                                                    </option>
                                                  ))}
                                                </select>
                                              </label>
                                              <div className="protein-mod-library-actions">
                                                {customResidueLibrary.slice(0, 4).map((item) => (
                                                  <button
                                                    key={`library-remove-${item.ccd}`}
                                                    type="button"
                                                    className="btn btn-ghost btn-compact"
                                                    disabled={disabled}
                                                    title={`Delete ${item.ccd} from project library`}
                                                    onClick={(e) => {
                                                      e.stopPropagation();
                                                      removeLibraryResidue(item.ccd);
                                                    }}
                                                  >
                                                    Delete {item.ccd}
                                                  </button>
                                                ))}
                                              </div>
                                            </div>
                                          )}
                                          <label className="field">
                                            <span>Name</span>
                                            <input
                                              value={mod.label || ''}
                                              disabled={disabled}
                                              placeholder="Custom residue"
                                              onChange={(e) => patchProteinModification(comp.id, mod.id, { label: e.target.value })}
                                            />
                                          </label>
                                          <label className="field">
                                            <span>Custom Residue SMILES</span>
                                            <input
                                              value={mod.smiles || CUSTOM_RESIDUE_SCAFFOLD_SMILES}
                                              disabled={disabled}
                                              placeholder="Draw or paste the complete modified residue SMILES"
                                              onChange={(e) => patchProteinModification(comp.id, mod.id, { smiles: e.target.value })}
                                            />
                                          </label>
                                          <CustomResiduePreview
                                            smiles={mod.smiles || CUSTOM_RESIDUE_SCAFFOLD_SMILES}
                                            onValidityChange={(isValid) =>
                                              setCustomResidueValidity((prev) => (prev[mod.id] === isValid ? prev : { ...prev, [mod.id]: isValid }))
                                            }
                                          />
                                          <div className="protein-mod-rules">
                                            <span>Rules</span>
                                            <ul>
                                              <li>Complete residue, not a standalone ligand</li>
                                              <li>Backbone N-CA-C(=O) must remain present</li>
                                              <li>Modify the side chain or terminal chemistry as needed</li>
                                            </ul>
                                          </div>
                                          <div className="protein-mod-jsme-actions">
                                            <button
                                              type="button"
                                              className="btn btn-ghost btn-compact"
                                              disabled={disabled || !String(mod.smiles || '').trim() || !customResidueValidity[mod.id]}
                                              onClick={() => saveModificationToLibrary(mod)}
                                            >
                                              Save to library
                                            </button>
                                          </div>
                                        </div>
                                      </>
                                    )}
                                  </div>
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
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
                      <LigandPropertyGrid smiles={comp.sequence} variant="radar" />
                    </div>
                  </div>
                  {method === 'jsme' && (
                    <aside className="component-content-side ligand-input-right">
                      <div className="field component-side-field">
                        <span>JSME Molecule Editor</span>
                        <div className="jsme-editor-container component-jsme-shell">
                          <JSMEEditor
                            smiles={comp.sequence}
                            height={ligandJsmeHeight}
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
