interface ProjectBasicsMetadataFormProps {
  canEdit: boolean;
  isPredictionWorkflow: boolean;
  showBackend: boolean;
  backendOptions?: Array<{ value: string; label: string }>;
  name: string;
  summary: string;
  backend: string;
  seed: number | null;
  onNameChange: (value: string) => void;
  onSummaryChange: (value: string) => void;
  onBackendChange: (backend: string) => void;
  onSeedChange: (seed: number | null) => void;
}

export function ProjectBasicsMetadataForm({
  canEdit,
  isPredictionWorkflow,
  showBackend,
  backendOptions,
  name,
  summary,
  backend,
  seed,
  onNameChange,
  onSummaryChange,
  onBackendChange,
  onSeedChange
}: ProjectBasicsMetadataFormProps) {
  const backendSelectOptions =
    backendOptions && backendOptions.length > 0
      ? backendOptions
      : [
          { value: 'boltz', label: 'Boltz-2' },
          { value: 'alphafold3', label: 'AlphaFold3' },
          { value: 'protenix', label: 'Protenix' }
        ];

  return (
    <section className="panel subtle basics-panel">
      <label className="field">
        <span>
          Project Name <span className="required-mark">*</span>
        </span>
        <input required value={name} onChange={(e) => onNameChange(e.target.value)} disabled={!canEdit} />
      </label>

      <label className="field">
        <span>Summary</span>
        <textarea value={summary} rows={3} onChange={(e) => onSummaryChange(e.target.value)} disabled={!canEdit} />
      </label>

      {showBackend && (
        <label className="field">
          <span>
            Backend <span className="required-mark">*</span>
          </span>
          <select required value={backend} onChange={(e) => onBackendChange(e.target.value)} disabled={!canEdit}>
            {backendSelectOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
        </label>
      )}

      {isPredictionWorkflow && (
        <label className="field">
          <span>Random Seed (optional)</span>
          <input
            type="number"
            min={0}
            value={seed ?? ''}
            onChange={(e) => onSeedChange(e.target.value === '' ? null : Math.max(0, Math.floor(Number(e.target.value) || 0)))}
            disabled={!canEdit}
            placeholder="Default: 42"
          />
        </label>
      )}
    </section>
  );
}
