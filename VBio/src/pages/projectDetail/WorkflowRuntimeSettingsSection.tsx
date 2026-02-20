export interface WorkflowRuntimeSettingsSectionProps {
  visible: boolean;
  canEdit: boolean;
  isPredictionWorkflow: boolean;
  isAffinityWorkflow: boolean;
  backend: string;
  seed: number | null;
  onBackendChange: (backend: string) => void;
  onSeedChange: (seed: number | null) => void;
}

export function WorkflowRuntimeSettingsSection({
  visible,
  canEdit,
  isPredictionWorkflow,
  isAffinityWorkflow,
  backend,
  seed,
  onBackendChange,
  onSeedChange
}: WorkflowRuntimeSettingsSectionProps) {
  if (!visible) return null;

  return (
    <section className="panel subtle component-runtime-settings">
      <div className="component-runtime-settings-row">
        <label className="field">
          <span>
            Backend <span className="required-mark">*</span>
          </span>
          <select required value={backend} onChange={(e) => onBackendChange(e.target.value)} disabled={!canEdit}>
            {(isAffinityWorkflow
              ? [
                  { value: 'boltz', label: 'Boltz-2' },
                  { value: 'protenix', label: 'Protenix2Score' }
                ]
              : [
                  { value: 'boltz', label: 'Boltz-2' },
                  { value: 'alphafold3', label: 'AlphaFold3' },
                  { value: 'protenix', label: 'Protenix' }
                ]
            ).map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>

        {isPredictionWorkflow && (
          <label className="field">
            <span>Random Seed (optional)</span>
            <input
              type="number"
              min={0}
              value={seed ?? ''}
              onChange={(e) => {
                const value = e.target.value;
                const nextSeed = value === '' ? null : Math.max(0, Math.floor(Number(value) || 0));
                onSeedChange(nextSeed);
              }}
              disabled={!canEdit}
              placeholder="Default: 42"
            />
          </label>
        )}
      </div>
    </section>
  );
}
