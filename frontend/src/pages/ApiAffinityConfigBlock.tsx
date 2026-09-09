import { Field } from '../components/common/Field';
/**
 * Affinity workflow config block — extracted from ApiAccessPage CommandPanel.
 * 17 controlled pairs; self-contained per audit (only reads workflow gate flags).
 */
interface AffinityConfigBlockProps {
  isAffinityWorkflow: boolean;
  isDockBuilderMode: boolean;
  normalizeMode: (v: string) => string;
  builderAffinityConfidenceOnly: boolean;
  builderAffinityMode: string;  // AffinityScoringMode union at parent
  builderAffinitySeed: number | null;
  builderTargetPath: string;
  builderAffinityLigandSmiles: string;
  builderPocketMethod: string;
  builderDockCenterX: string;
  builderDockCenterY: string;
  builderDockCenterZ: string;
  builderDockSizeX: string;
  builderDockSizeY: string;
  builderDockSizeZ: string;
  builderPocketLigandPath: string;
  builderPocketResidues: string;
  builderLigandPath: string;
  builderAffinityTargetChain: string;
  builderAffinityLigandChain: string;
  onAffinityConfidenceOnlyChange: (v: boolean) => void;
  onAffinityModeChange: (v: string) => void;
  onAffinitySeedChange: (v: number | null) => void;
  onTargetPathChange: (v: string) => void;
  onAffinityLigandSmilesChange: (v: string) => void;
  onPocketMethodChange: (v: string) => void;
  onDockCenterXChange: (v: string) => void;
  onDockCenterYChange: (v: string) => void;
  onDockCenterZChange: (v: string) => void;
  onDockSizeXChange: (v: string) => void;
  onDockSizeYChange: (v: string) => void;
  onDockSizeZChange: (v: string) => void;
  onPocketLigandPathChange: (v: string) => void;
  onPocketResiduesChange: (v: string) => void;
  onLigandPathChange: (v: string) => void;
  onAffinityTargetChainChange: (v: string) => void;
  onAffinityLigandChainChange: (v: string) => void;
}

export function AffinityConfigBlock(p: AffinityConfigBlockProps) {
  if (!p.isAffinityWorkflow) return null;
  return (
    <>
    <div className="api-yaml-component-flags api-affinity-options">
      {/* No MSA toggle: boltz2score always runs with the MSA server (the backend
          force-enables use_msa_server), so the switch was a dead control. */}
      <label className="checkbox-inline">
        <input
          type="checkbox"
          checked={p.builderAffinityConfidenceOnly}
          onChange={(e) => p.onAffinityConfidenceOnlyChange(e.target.checked)}
        />
        <span>Confidence Only</span>
      </label>
    </div>
    <Field label="Mode">
      <select
        value={p.builderAffinityMode}
        onChange={(e) => p.onAffinityModeChange(p.normalizeMode(e.target.value))}
      >
        <option value="score">score</option>
        <option value="pose">pose</option>
        <option value="refine">refine</option>
        <option value="interface">interface</option>
        <option value="dock">dock</option>
      </select>
    </Field>
    <Field label="Seed (optional)">
      <input
        type="number"
        min={0}
        value={p.builderAffinitySeed ?? ''}
        onChange={(e) => {
          const value = e.target.value;
          p.onAffinitySeedChange(value === '' ? null : Math.max(0, Math.floor(Number(value) || 0)));
        }}
        placeholder="Default: 42"
      />
    </Field>
    <Field label="Target file path">
      <input value={p.builderTargetPath} onChange={(e) => p.onTargetPathChange(e.target.value)} placeholder="./protein.pdb" />
    </Field>
    {p.isDockBuilderMode ? (
      <>
        <Field label="Ligand SMILES (required by dock)">
          <input
            value={p.builderAffinityLigandSmiles}
            onChange={(e) => p.onAffinityLigandSmilesChange(e.target.value)}
            placeholder="e.g. CC(C)CC1=CC=C(C=C1)C(C)C(=O)O"
          />
        </Field>
        <Field label="Pocket definition method">
          <select
            value={p.builderPocketMethod}
            onChange={(e) => {
              const value = e.target.value;
              p.onPocketMethodChange(value === 'ligand' || value === 'residues' ? value : 'center');
            }}
          >
            <option value="center">Manual coordinates</option>
            <option value="ligand">Reference ligand (auto-detect pocket)</option>
            <option value="residues">Pocket residues</option>
          </select>
        </Field>
        {p.builderPocketMethod === 'center' ? (
          <Field label="Pocket center X / Y / Z (Å)">
            <div className="row gap-8">
              <input
                type="number"
                step="0.1"
                value={p.builderDockCenterX}
                onChange={(e) => p.onDockCenterXChange(e.target.value)}
                placeholder="x"
              />
              <input
                type="number"
                step="0.1"
                value={p.builderDockCenterY}
                onChange={(e) => p.onDockCenterYChange(e.target.value)}
                placeholder="y"
              />
              <input
                type="number"
                step="0.1"
                value={p.builderDockCenterZ}
                onChange={(e) => p.onDockCenterZChange(e.target.value)}
                placeholder="z"
              />
            </div>
          </Field>
        ) : p.builderPocketMethod === 'ligand' ? (
          <Field label="Reference ligand file path (pocket auto-detected server-side)">
            <input
              value={p.builderPocketLigandPath}
              onChange={(e) => p.onPocketLigandPathChange(e.target.value)}
              placeholder="./reference_ligand.sdf"
            />
          </Field>
        ) : (
          <Field label="Pocket residues (CHAIN:RESNUM, comma-separated)">
            <input
              value={p.builderPocketResidues}
              onChange={(e) => p.onPocketResiduesChange(e.target.value)}
              placeholder="A:100,A:101"
            />
          </Field>
        )}
        <Field label="Pocket size X / Y / Z (Å, default 22)">
          <div className="row gap-8">
            <input
              type="number"
              min="1"
              step="1"
              value={p.builderDockSizeX}
              onChange={(e) => p.onDockSizeXChange(e.target.value)}
              placeholder="22"
            />
            <input
              type="number"
              min="1"
              step="1"
              value={p.builderDockSizeY}
              onChange={(e) => p.onDockSizeYChange(e.target.value)}
              placeholder="22"
            />
            <input
              type="number"
              min="1"
              step="1"
              value={p.builderDockSizeZ}
              onChange={(e) => p.onDockSizeZChange(e.target.value)}
              placeholder="22"
            />
          </div>
        </Field>
      </>
    ) : (
      <Field label="Ligand file path">
        <input value={p.builderLigandPath} onChange={(e) => p.onLigandPathChange(e.target.value)} placeholder="./ligand.sdf" />
      </Field>
    )}
    {!p.builderAffinityConfidenceOnly && !p.isDockBuilderMode && (
      <>
        <Field label="Target chain">
          <input
            value={p.builderAffinityTargetChain}
            onChange={(e) => p.onAffinityTargetChainChange(e.target.value)}
            placeholder="A"
          />
        </Field>
        <Field label="Ligand chain">
          <input
            value={p.builderAffinityLigandChain}
            onChange={(e) => p.onAffinityLigandChainChange(e.target.value)}
            placeholder="L"
          />
        </Field>
        {!p.isDockBuilderMode && (
          <Field label="Ligand SMILES">
            <input
              value={p.builderAffinityLigandSmiles}
              onChange={(e) => p.onAffinityLigandSmilesChange(e.target.value)}
              placeholder="Required for affinity mode"
            />
          </Field>
        )}
      </>
    )}
    </>
  );
}
