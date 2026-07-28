import { limitTaskSummary, TASK_SUMMARY_MAX_LENGTH } from '../../utils/taskMetadata';

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
  const limitedTaskSummary = limitTaskSummary(taskSummary);

  return (
    <section className="panel subtle basics-panel">
      <label className="field">
        <span>
          Task Name (optional)
        </span>
        <input value={taskName} onChange={(e) => onTaskNameChange(e.target.value)} disabled={!canEdit} />
      </label>

      <label className="field">
        <span className="task-summary-field-label">
          Task Summary
          <small className="task-summary-counter">{limitedTaskSummary.length}/{TASK_SUMMARY_MAX_LENGTH}</small>
        </span>
        <textarea
          value={limitedTaskSummary}
          rows={3}
          maxLength={TASK_SUMMARY_MAX_LENGTH}
          onChange={(e) => onTaskSummaryChange(limitTaskSummary(e.target.value))}
          disabled={!canEdit}
        />
      </label>
    </section>
  );
}
