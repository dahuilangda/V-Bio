interface ProjectBasicsMetadataFormProps {
  canEdit: boolean;
  taskName: string;
  taskSummary: string;
  onTaskNameChange: (value: string) => void;
  onTaskSummaryChange: (value: string) => void;
}

export function ProjectBasicsMetadataForm({
  canEdit,
  taskName,
  taskSummary,
  onTaskNameChange,
  onTaskSummaryChange
}: ProjectBasicsMetadataFormProps) {
  return (
    <section className="panel subtle basics-panel">
      <label className="field">
        <span>
          Task Name (optional)
        </span>
        <input value={taskName} onChange={(e) => onTaskNameChange(e.target.value)} disabled={!canEdit} />
      </label>

      <label className="field">
        <span>Task Summary</span>
        <textarea value={taskSummary} rows={3} onChange={(e) => onTaskSummaryChange(e.target.value)} disabled={!canEdit} />
      </label>
    </section>
  );
}
