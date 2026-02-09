import { useEffect, useMemo, useState } from 'react';
import type { MouseEvent } from 'react';
import { ChevronDown, ChevronRight, ChevronsDown, ChevronsUp, Link2, Plus, Radar, Target, Trash2 } from 'lucide-react';
import type {
  BondConstraint,
  ContactConstraint,
  InputComponent,
  PocketConstraint,
  PredictionConstraint,
  PredictionConstraintType,
  PredictionProperties
} from '../../types/models';
import { randomId } from '../../utils/projectInputs';
import { buildChainInfos } from '../../utils/chainAssignments';

interface ConstraintEditorProps {
  components: InputComponent[];
  constraints: PredictionConstraint[];
  properties: PredictionProperties;
  pickedResidue?: ConstraintResiduePick | null;
  selectedConstraintId?: string | null;
  selectedConstraintIds?: string[];
  onSelectedConstraintIdChange?: (id: string | null) => void;
  onConstraintClick?: (id: string, options?: { toggle: boolean; range: boolean }) => void;
  onClearSelection?: () => void;
  showAffinitySection?: boolean;
  allowedConstraintTypes?: PredictionConstraintType[];
  compatibilityHint?: string;
  onConstraintsChange: (constraints: PredictionConstraint[]) => void;
  onPropertiesChange: (properties: PredictionProperties) => void;
  disabled?: boolean;
}

export interface ConstraintResiduePick {
  chainId: string;
  residue: number;
  atomName?: string;
}

function clampPositiveInt(value: number): number {
  if (!Number.isFinite(value)) return 1;
  return Math.max(1, Math.floor(value));
}

function chainLabel(type: InputComponent['type']): string {
  if (type === 'protein') return 'Protein';
  if (type === 'dna') return 'DNA';
  if (type === 'rna') return 'RNA';
  return 'Ligand';
}

function defaultChain(chainIds: string[], index = 0): string {
  if (!chainIds.length) return index === 0 ? 'A' : 'B';
  if (index < chainIds.length) return chainIds[index];
  return chainIds[0];
}

function defaultConstraint(
  type: PredictionConstraintType,
  chainIds: string[],
  ligandChainIds: string[]
): PredictionConstraint {
  if (type === 'bond') {
    return {
      id: randomId(),
      type: 'bond',
      atom1_chain: defaultChain(chainIds, 0),
      atom1_residue: 1,
      atom1_atom: 'CA',
      atom2_chain: defaultChain(chainIds, 1),
      atom2_residue: 1,
      atom2_atom: 'CA'
    };
  }

  if (type === 'pocket') {
    const binder = ligandChainIds[0] || defaultChain(chainIds, 0);
    return {
      id: randomId(),
      type: 'pocket',
      binder,
      contacts: [],
      max_distance: 6,
      force: true
    };
  }

  return {
    id: randomId(),
    type: 'contact',
    token1_chain: defaultChain(chainIds, 0),
    token1_residue: 1,
    token2_chain: defaultChain(chainIds, 1),
    token2_residue: 1,
    max_distance: 5,
    force: true
  };
}

const ALL_CONSTRAINT_TYPES: PredictionConstraintType[] = ['contact', 'bond', 'pocket'];

function constraintTypeLabel(type: PredictionConstraintType): string {
  if (type === 'contact') return 'Contact';
  if (type === 'bond') return 'Bond';
  return 'Pocket';
}

function summarizeConstraint(constraint: PredictionConstraint): string {
  if (constraint.type === 'contact') {
    return `${constraint.token1_chain}:${constraint.token1_residue} ↔ ${constraint.token2_chain}:${constraint.token2_residue} · ≤${constraint.max_distance} Å${constraint.force ? ' · force' : ''}`;
  }
  if (constraint.type === 'bond') {
    return `${constraint.atom1_chain}:${constraint.atom1_residue}:${constraint.atom1_atom} ↔ ${constraint.atom2_chain}:${constraint.atom2_residue}:${constraint.atom2_atom}`;
  }
  const first = constraint.contacts[0];
  if (!first) {
    return `${constraint.binder} · no contacts · ≤${constraint.max_distance} Å${constraint.force ? ' · force' : ''}`;
  }
  const more = constraint.contacts.length > 1 ? ` +${constraint.contacts.length - 1}` : '';
  return `${constraint.binder} ↔ ${first[0]}:${first[1]}${more} · ≤${constraint.max_distance} Å${constraint.force ? ' · force' : ''}`;
}

export function ConstraintEditor({
  components,
  constraints,
  properties,
  pickedResidue = null,
  selectedConstraintId = null,
  selectedConstraintIds = [],
  onSelectedConstraintIdChange,
  onConstraintClick,
  onClearSelection,
  showAffinitySection = true,
  allowedConstraintTypes = ALL_CONSTRAINT_TYPES,
  compatibilityHint,
  onConstraintsChange,
  onPropertiesChange,
  disabled = false
}: ConstraintEditorProps) {
  const activeComponents = components.filter((item) => item.sequence.trim());
  const chainInfos = buildChainInfos(activeComponents);
  const chainIds = chainInfos.map((item) => item.id);
  const ligandChainIds = chainInfos.filter((item) => item.type === 'ligand').map((item) => item.id);
  const selectedConstraintIdSet = useMemo(() => new Set(selectedConstraintIds), [selectedConstraintIds]);
  const [collapsedById, setCollapsedById] = useState<Record<string, boolean>>({});
  const allowedTypes = useMemo(
    () => (allowedConstraintTypes.length > 0 ? allowedConstraintTypes : ALL_CONSTRAINT_TYPES),
    [allowedConstraintTypes]
  );
  const allowedTypeSet = useMemo(() => new Set(allowedTypes), [allowedTypes]);
  const blockedTypes = useMemo(
    () => ALL_CONSTRAINT_TYPES.filter((type) => !allowedTypeSet.has(type)),
    [allowedTypeSet]
  );
  const collapsedCount = useMemo(() => constraints.filter((item) => collapsedById[item.id]).length, [constraints, collapsedById]);
  const allCollapsed = constraints.length > 0 && collapsedCount === constraints.length;

  useEffect(() => {
    setCollapsedById((prev) => {
      const next: Record<string, boolean> = {};
      let changed = false;
      for (const item of constraints) {
        if (Object.prototype.hasOwnProperty.call(prev, item.id)) {
          next[item.id] = Boolean(prev[item.id]);
        } else {
          next[item.id] = false;
          changed = true;
        }
      }
      if (Object.keys(prev).length !== constraints.length) {
        changed = true;
      }
      return changed ? next : prev;
    });
  }, [constraints]);

  const replaceAt = (id: string, next: PredictionConstraint) => {
    onConstraintsChange(constraints.map((item) => (item.id === id ? next : item)));
  };

  const addConstraint = (type: PredictionConstraintType) => {
    if (!allowedTypeSet.has(type)) return;
    const next = defaultConstraint(type, chainIds, ligandChainIds);
    onConstraintsChange([...constraints, next]);
    onSelectedConstraintIdChange?.(next.id);
  };

  const removeConstraint = (id: string) => {
    const currentIndex = constraints.findIndex((item) => item.id === id);
    const nextConstraints = constraints.filter((item) => item.id !== id);
    onConstraintsChange(nextConstraints);
    if (selectedConstraintId === id) {
      const fallback = nextConstraints[Math.min(currentIndex, nextConstraints.length - 1)]?.id ?? null;
      onSelectedConstraintIdChange?.(fallback);
    }
  };
  const toggleCollapsed = (id: string) => {
    setCollapsedById((prev) => ({ ...prev, [id]: !prev[id] }));
  };
  const setAllCollapsed = (collapsed: boolean) => {
    const next = constraints.reduce<Record<string, boolean>>((acc, item) => {
      acc[item.id] = collapsed;
      return acc;
    }, {});
    setCollapsedById(next);
  };

  return (
    <section
      className="constraint-editor"
      onClick={(event: MouseEvent<HTMLElement>) => {
        if (event.target === event.currentTarget) {
          onClearSelection?.();
        }
      }}
    >
      <div className="constraint-head">
        <h3>Constraints</h3>
        {constraints.length > 1 && (
          <div className="constraint-head-actions">
            <button
              type="button"
              className="icon-btn constraint-head-icon-btn"
              onClick={() => setAllCollapsed(!allCollapsed)}
              disabled={disabled}
              title={allCollapsed ? 'Expand all constraints' : 'Collapse all constraints'}
              aria-label={allCollapsed ? 'Expand all constraints' : 'Collapse all constraints'}
            >
              {allCollapsed ? <ChevronsDown size={14} /> : <ChevronsUp size={14} />}
            </button>
          </div>
        )}
      </div>
      {(compatibilityHint || blockedTypes.length > 0) && (
        <div className="muted small">
          {compatibilityHint || `Current backend does not support: ${blockedTypes.map((type) => constraintTypeLabel(type)).join(', ')}.`}
        </div>
      )}

      {showAffinitySection && (
        <div className="constraint-section panel subtle">
        <div className="constraint-section-title">
          <Radar size={14} />
          <strong>Affinity Scoring</strong>
        </div>
        <span className="muted small">
          {pickedResidue
            ? `Picked in Mol*: ${pickedResidue.chainId}:${pickedResidue.residue}${pickedResidue.atomName ? `:${pickedResidue.atomName}` : ''}`
            : 'Tip: click a residue in Mol* preview, then use "Use pick" buttons below.'}
        </span>

        <label className="switch-field">
          <input
            type="checkbox"
            checked={properties.affinity}
            disabled={disabled}
            onChange={(e) => {
              const nextAffinity = e.target.checked;
              const nextBinder =
                nextAffinity && !properties.binder ? ligandChainIds[0] || chainIds[0] || null : properties.binder;
              onPropertiesChange({
                ...properties,
                affinity: nextAffinity,
                binder: nextAffinity ? nextBinder : null
              });
            }}
          />
          <span>Enable affinity property</span>
        </label>

        {properties.affinity && (
          <label className="field">
            <span>Binder Chain</span>
            <select
              value={properties.binder || ''}
              disabled={disabled || chainIds.length === 0}
              onChange={(e) =>
                onPropertiesChange({
                  ...properties,
                  binder: e.target.value || null
                })
              }
            >
              {chainInfos.length === 0 && <option value="">No chain available</option>}
              {chainInfos.map((info) => (
                <option key={info.id} value={info.id}>
                  {info.id} · {chainLabel(info.type)}
                </option>
              ))}
            </select>
          </label>
        )}
        </div>
      )}

      <div
        className="constraint-list"
        onClick={(event: MouseEvent<HTMLDivElement>) => {
          if (event.target === event.currentTarget) {
            onClearSelection?.();
          }
        }}
      >
        {constraints.map((item, index) => {
          const isCollapsed = Boolean(collapsedById[item.id]);
          const typeOptions = allowedTypeSet.has(item.type) ? allowedTypes : ([item.type, ...allowedTypes] as PredictionConstraintType[]);
          const setType = (nextType: PredictionConstraintType) => {
            if (item.type === nextType) return;
            if (!allowedTypeSet.has(nextType)) return;
            const next = defaultConstraint(nextType, chainIds, ligandChainIds);
            replaceAt(item.id, { ...next, id: item.id });
          };
          const isSelected = selectedConstraintIdSet.has(item.id) || selectedConstraintId === item.id;

          return (
            <article
              id={`constraint-card-${item.id}`}
              key={item.id}
              className={`constraint-item panel subtle ${isSelected ? 'active' : ''}`}
              onClick={(event: MouseEvent<HTMLElement>) => {
                if (onConstraintClick) {
                  onConstraintClick(item.id, { toggle: event.metaKey || event.ctrlKey, range: event.shiftKey });
                  return;
                }
                onSelectedConstraintIdChange?.(item.id);
              }}
              onFocusCapture={(event) => {
                if (event.target === event.currentTarget) return;
                if (isSelected) return;
                if (onConstraintClick) {
                  onConstraintClick(item.id, { toggle: false, range: false });
                  return;
                }
                onSelectedConstraintIdChange?.(item.id);
              }}
            >
              <div className="constraint-item-head">
                <button
                  type="button"
                  className="icon-btn constraint-item-toggle"
                  onClick={(event) => {
                    event.stopPropagation();
                    toggleCollapsed(item.id);
                  }}
                  disabled={disabled}
                  title={isCollapsed ? 'Expand constraint' : 'Collapse constraint'}
                  aria-label={isCollapsed ? 'Expand constraint' : 'Collapse constraint'}
                  aria-expanded={!isCollapsed}
                >
                  {isCollapsed ? <ChevronRight size={14} /> : <ChevronDown size={14} />}
                </button>
                <strong className="constraint-item-title">
                  {index + 1}. {item.type === 'contact' ? 'Contact' : item.type === 'bond' ? 'Bond' : 'Pocket'}
                </strong>
                <button
                  type="button"
                  className="icon-btn"
                  onClick={(event) => {
                    event.stopPropagation();
                    removeConstraint(item.id);
                  }}
                  disabled={disabled}
                  title="Delete constraint"
                >
                  <Trash2 size={14} />
                </button>
              </div>

              {isCollapsed && <div className="constraint-item-summary muted small">{summarizeConstraint(item)}</div>}

              {!isCollapsed && (
                <>
                  <label className="field">
                    <span>Constraint Type</span>
                    <select value={item.type} disabled={disabled} onChange={(e) => setType(e.target.value as PredictionConstraintType)}>
                      {typeOptions.map((type) => (
                        <option key={`constraint-type-${item.id}-${type}`} value={type}>
                          {constraintTypeLabel(type)}
                        </option>
                      ))}
                    </select>
                  </label>

                  {item.type === 'contact' && (
                    <ContactConstraintFields
                      value={item}
                      chainInfos={chainInfos}
                      pickedResidue={pickedResidue}
                      disabled={disabled}
                      onChange={(next) => replaceAt(item.id, next)}
                    />
                  )}

                  {item.type === 'bond' && (
                    <BondConstraintFields
                      value={item}
                      chainInfos={chainInfos}
                      pickedResidue={pickedResidue}
                      disabled={disabled}
                      onChange={(next) => replaceAt(item.id, next)}
                    />
                  )}

                  {item.type === 'pocket' && (
                    <PocketConstraintFields
                      value={item}
                      chainInfos={chainInfos}
                      pickedResidue={pickedResidue}
                      disabled={disabled}
                      onChange={(next) => replaceAt(item.id, next)}
                    />
                  )}
                </>
              )}
            </article>
          );
        })}
      </div>

      <div className="constraint-actions">
        {allowedTypeSet.has('contact') && (
          <button type="button" className="btn btn-ghost" onClick={() => addConstraint('contact')} disabled={disabled}>
            <Plus size={14} />
            Contact
          </button>
        )}
        {allowedTypeSet.has('bond') && (
          <button type="button" className="btn btn-ghost" onClick={() => addConstraint('bond')} disabled={disabled}>
            <Link2 size={14} />
            Bond
          </button>
        )}
        {allowedTypeSet.has('pocket') && (
          <button type="button" className="btn btn-ghost" onClick={() => addConstraint('pocket')} disabled={disabled}>
            <Target size={14} />
            Pocket
          </button>
        )}
      </div>
    </section>
  );
}

interface SharedFieldsProps<T> {
  value: T;
  chainInfos: ReturnType<typeof buildChainInfos>;
  pickedResidue?: ConstraintResiduePick | null;
  disabled: boolean;
  onChange: (next: T) => void;
}

function ChainSelect({
  value,
  chainInfos,
  disabled,
  onChange
}: {
  value: string;
  chainInfos: ReturnType<typeof buildChainInfos>;
  disabled: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <select value={value} disabled={disabled || chainInfos.length === 0} onChange={(e) => onChange(e.target.value)}>
      {chainInfos.length === 0 && <option value="">No chain available</option>}
      {chainInfos.map((info) => (
        <option key={info.id} value={info.id}>
          {info.id} · {chainLabel(info.type)} · {info.residueCount}
        </option>
      ))}
    </select>
  );
}

function ContactConstraintFields({ value, chainInfos, pickedResidue, disabled, onChange }: SharedFieldsProps<ContactConstraint>) {
  return (
    <div className="constraint-grid">
      <label className="field">
        <span>Token 1 Chain</span>
        <ChainSelect value={value.token1_chain} chainInfos={chainInfos} disabled={disabled} onChange={(next) => onChange({ ...value, token1_chain: next })} />
      </label>
      <label className="field">
        <span>Token 1 Residue</span>
        <input
          type="number"
          min={1}
          value={value.token1_residue}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, token1_residue: clampPositiveInt(Number(e.target.value)) })}
        />
      </label>
      <label className="field">
        <span>Token 2 Chain</span>
        <ChainSelect value={value.token2_chain} chainInfos={chainInfos} disabled={disabled} onChange={(next) => onChange({ ...value, token2_chain: next })} />
      </label>
      <label className="field">
        <span>Token 2 Residue</span>
        <input
          type="number"
          min={1}
          value={value.token2_residue}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, token2_residue: clampPositiveInt(Number(e.target.value)) })}
        />
      </label>
      <label className="field">
        <span>Max Distance (Å)</span>
        <input
          type="number"
          min={1}
          step={0.5}
          value={value.max_distance}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, max_distance: Math.max(1, Number(e.target.value) || 5) })}
        />
      </label>
      <label className="switch-field switch-tight">
        <input
          type="checkbox"
          checked={value.force}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, force: e.target.checked })}
        />
        <span>Force</span>
      </label>
      {pickedResidue && (
        <>
          <button
            type="button"
            className="btn btn-ghost btn-compact"
            disabled={disabled}
            onClick={() =>
              onChange({
                ...value,
                token1_chain: pickedResidue.chainId,
                token1_residue: pickedResidue.residue
              })
            }
          >
            Use pick → Token 1
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-compact"
            disabled={disabled}
            onClick={() =>
              onChange({
                ...value,
                token2_chain: pickedResidue.chainId,
                token2_residue: pickedResidue.residue
              })
            }
          >
            Use pick → Token 2
          </button>
        </>
      )}
    </div>
  );
}

function BondConstraintFields({ value, chainInfos, pickedResidue, disabled, onChange }: SharedFieldsProps<BondConstraint>) {
  return (
    <div className="constraint-grid">
      <label className="field">
        <span>Atom 1 Chain</span>
        <ChainSelect value={value.atom1_chain} chainInfos={chainInfos} disabled={disabled} onChange={(next) => onChange({ ...value, atom1_chain: next })} />
      </label>
      <label className="field">
        <span>Atom 1 Residue</span>
        <input
          type="number"
          min={1}
          value={value.atom1_residue}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, atom1_residue: clampPositiveInt(Number(e.target.value)) })}
        />
      </label>
      <label className="field">
        <span>Atom 1 Name</span>
        <input
          value={value.atom1_atom}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, atom1_atom: e.target.value.toUpperCase() })}
        />
      </label>
      <label className="field">
        <span>Atom 2 Chain</span>
        <ChainSelect value={value.atom2_chain} chainInfos={chainInfos} disabled={disabled} onChange={(next) => onChange({ ...value, atom2_chain: next })} />
      </label>
      <label className="field">
        <span>Atom 2 Residue</span>
        <input
          type="number"
          min={1}
          value={value.atom2_residue}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, atom2_residue: clampPositiveInt(Number(e.target.value)) })}
        />
      </label>
      <label className="field">
        <span>Atom 2 Name</span>
        <input
          value={value.atom2_atom}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, atom2_atom: e.target.value.toUpperCase() })}
        />
      </label>
      {pickedResidue && (
        <>
          <button
            type="button"
            className="btn btn-ghost btn-compact"
            disabled={disabled}
            onClick={() =>
              onChange({
                ...value,
                atom1_chain: pickedResidue.chainId,
                atom1_residue: pickedResidue.residue,
                atom1_atom: (pickedResidue.atomName || value.atom1_atom || 'CA').toUpperCase()
              })
            }
          >
            Use pick → Atom 1
          </button>
          <button
            type="button"
            className="btn btn-ghost btn-compact"
            disabled={disabled}
            onClick={() =>
              onChange({
                ...value,
                atom2_chain: pickedResidue.chainId,
                atom2_residue: pickedResidue.residue,
                atom2_atom: (pickedResidue.atomName || value.atom2_atom || 'CA').toUpperCase()
              })
            }
          >
            Use pick → Atom 2
          </button>
        </>
      )}
    </div>
  );
}

function PocketConstraintFields({ value, chainInfos, disabled, onChange }: SharedFieldsProps<PocketConstraint>) {
  return (
    <div className="constraint-grid constraint-grid-pocket">
      <label className="field">
        <span>Binder Chain</span>
        <ChainSelect value={value.binder} chainInfos={chainInfos} disabled={disabled} onChange={(next) => onChange({ ...value, binder: next })} />
      </label>

      <label className="field">
        <span>Max Distance (Å)</span>
        <input
          type="number"
          min={1}
          step={0.5}
          value={value.max_distance}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, max_distance: Math.max(1, Number(e.target.value) || 6) })}
        />
      </label>

      <label className="switch-field switch-tight">
        <input
          type="checkbox"
          checked={value.force}
          disabled={disabled}
          onChange={(e) => onChange({ ...value, force: e.target.checked })}
        />
        <span>Force</span>
      </label>

      <div className="field">
        <span>Contacts</span>
        <div className="pocket-contact-list">
          {value.contacts.length === 0 && <div className="muted small">No contacts yet.</div>}
          {value.contacts.map((contact, index) => (
            <div className="pocket-contact-item" key={`${value.id}-contact-${index}`}>
              <ChainSelect
                value={contact[0]}
                chainInfos={chainInfos}
                disabled={disabled}
                onChange={(nextChain) => {
                  const nextContacts = value.contacts.map((row, rowIndex) =>
                    rowIndex === index ? ([nextChain, row[1]] as [string, number]) : row
                  );
                  onChange({ ...value, contacts: nextContacts });
                }}
              />
              <input
                type="number"
                min={1}
                value={contact[1]}
                disabled={disabled}
                onChange={(e) => {
                  const nextResidue = clampPositiveInt(Number(e.target.value));
                  const nextContacts = value.contacts.map((row, rowIndex) =>
                    rowIndex === index ? ([row[0], nextResidue] as [string, number]) : row
                  );
                  onChange({ ...value, contacts: nextContacts });
                }}
              />
              <button
                type="button"
                className="icon-btn"
                disabled={disabled}
                onClick={() => {
                  const nextContacts = value.contacts.filter((_, rowIndex) => rowIndex !== index);
                  onChange({ ...value, contacts: nextContacts });
                }}
                title="Delete contact"
              >
                <Trash2 size={13} />
              </button>
            </div>
          ))}
        </div>

        <button
          type="button"
          className="btn btn-ghost top-margin"
          disabled={disabled}
          onClick={() => {
            const fallback = chainInfos[0]?.id || 'A';
            onChange({ ...value, contacts: [...value.contacts, [fallback, 1]] });
          }}
        >
          <Plus size={13} />
          Add contact
        </button>
      </div>
    </div>
  );
}
