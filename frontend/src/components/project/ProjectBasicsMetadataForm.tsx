import { limitTaskSummary, TASK_SUMMARY_MAX_LENGTH } from '../../utils/taskMetadata';
import { Field } from '../common/Field';

interface ProjectBasicsMetadataFormProps {
  isEditable: boolean;
  taskName: string;
  taskSummary: string;
  onTaskNameChange: (value: string) => void;
  onTaskSummaryChange: (value: string) => void;
}

export function ProjectBasicsMetadataForm({
  isEditable,
  taskName,
  taskSummary,
  onTaskNameChange,
  onTaskSummaryChange
}: ProjectBasicsMetadataFormProps) {
  const limitedTaskSummary = limitTaskSummary(taskSummary);

  return (
    <section className="panel subtle basics-panel">
      <Field label="Task Name (optional)">
        <input value={taskName} onChange={(e) => onTaskNameChange(e.target.value)} disabled={!isEditable} />
      </Field>

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
          disabled={!isEditable}
        />
      </label>
    </section>
  );
}
