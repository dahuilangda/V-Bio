/**
 * Lead Optimization options — extracted from ApiAccessPage CommandPanel.
 * Zero cross-block coupling; 6 controlled value/callback pairs.
 */
import { ShieldCheck } from 'lucide-react';
import { Field } from '../components/common/Field';

interface LeadOptOptionsProps {
  builderLeadOptTargetConfigPath: string;
  builderLeadOptInputCompound: string;
  builderLeadOptTargetChain: string;
  builderLeadOptLigandChain: string;
  builderLeadOptObjectiveProfile: string;
  builderLeadOptEnableAffinity: boolean;
  onTargetConfigPathChange: (v: string) => void;
  onInputCompoundChange: (v: string) => void;
  onTargetChainChange: (v: string) => void;
  onLigandChainChange: (v: string) => void;
  onObjectiveProfileChange: (v: string) => void;
  onEnableAffinityChange: (v: boolean) => void;
}

export function LeadOptOptions(p: LeadOptOptionsProps) {
  return (
    <>
<Field
  label="target_config path"
  hint="Must point to a valid lead-optimization YAML containing the protein sequence."
  hintAlign="end"
>
  <input
    value={p.builderLeadOptTargetConfigPath}
    onChange={(e) => p.onTargetConfigPathChange(e.target.value)}
    placeholder="./target.yaml"
  />
</Field>
<Field label="Input compound (SMILES)">
  <input
    value={p.builderLeadOptInputCompound}
    onChange={(e) => p.onInputCompoundChange(e.target.value)}
    placeholder="CCOc1ccc..."
  />
</Field>
<section className="api-prediction-affinity-panel api-leadopt-panel">
  <div className="api-prediction-affinity-head">
    <span className="api-prediction-affinity-title">
      <ShieldCheck size={14} />
      Lead Opt Options
    </span>
    <label className="checkbox-inline api-prediction-affinity-toggle">
      <input
        type="checkbox"
        checked={p.builderLeadOptEnableAffinity}
        onChange={(e) => p.onEnableAffinityChange(e.target.checked)}
      />
      <span>Enable affinity</span>
    </label>
  </div>
  <div className="api-prediction-affinity-grid">
    <Field label="Target chain">
      <input value={p.builderLeadOptTargetChain} onChange={(e) => p.onTargetChainChange(e.target.value)} placeholder="A" />
    </Field>
    <Field label="Ligand chain">
      <input value={p.builderLeadOptLigandChain} onChange={(e) => p.onLigandChainChange(e.target.value)} placeholder="L" />
    </Field>
    <Field label="Objective profile">
      <select value={p.builderLeadOptObjectiveProfile} onChange={(e) => p.onObjectiveProfileChange(e.target.value)}>
        <option value="balanced">balanced</option>
        <option value="potency_first">potency_first</option>
        <option value="admet_safe">admet_safe</option>
        <option value="cns_like">cns_like</option>
        <option value="custom">custom</option>
      </select>
    </Field>
  </div>
</section>
    </>
  );
}
