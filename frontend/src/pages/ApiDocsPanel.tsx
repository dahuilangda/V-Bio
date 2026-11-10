/**
 * Static "API Docs" panel — the seven-step usage guide of the API access
 * page. Each step renders its guidance inline as an expandable entry; no
 * hidden tooltips.
 */
import { Info } from 'lucide-react';
import './ApiDocsPanel.css';

interface DocStep {
  title: string;
  body: string;
  code?: string;
}

const DOC_STEPS: DocStep[] = [
  {
    title: 'Create project in V-Bio web',
    body: 'The API submits into an existing project — it never creates one. Set up the project (workflow, backend, components) in the web UI first, then pick it in the command builder.'
  },
  {
    title: 'Create token and bind project',
    body: 'Tokens are per-user. Bind a token to a project and grant submit / cancel / delete permissions; the gateway checks every request against them.',
    code: 'POST /vbio-api/tokens\n{ "project_id": "<id>", "allow_submit": true }'
  },
  {
    title: 'Use generated submit command',
    body: 'The builder generates a ready-to-run curl. The workflow is fixed by the project type; select the backend where applicable before copying.'
  },
  {
    title: 'YAML format (prediction)',
    body: 'Use version + sequences as the skeleton. Ligand entries accept smiles or ccd codes; add constraints, properties, and templates only when needed.'
  },
  {
    title: 'Track and download',
    body: 'Poll status, then fetch the result ZIP with the result command — same project_id and token as the submit call.',
    code: 'GET /vbio-api/status/<task_id>\nGET /vbio-api/result/<task_id>'
  },
  {
    title: 'Cancel or delete safely',
    body: 'Cancel stops a running task and keeps its row; delete removes the record. Both use operation_mode and are permission-checked per token.',
    code: 'POST /vbio-api/task-action\n{ "operation_mode": "cancel" }'
  },
  {
    title: 'Reuse from history',
    body: 'Recently copied commands are kept under the builder for one-click reuse, so repeat submissions need only a sequence edit.'
  }
];

export function ApiDocsPanel() {
  return (
    <section className="panel api-docs-panel">
      <div className="api-section-head">
        <h2><Info size={16} /> API Docs</h2>
        <span className="muted small">Seven steps from project setup to result download.</span>
      </div>
      <div className="api-doc-steps">
        {DOC_STEPS.map((step, index) => (
          <details key={step.title} className="api-doc-step" open={index === 0}>
            <summary>
              <span className="api-doc-step-num">{index + 1}</span>
              <strong>{step.title}</strong>
            </summary>
            <div className="api-doc-step-body">
              <p>{step.body}</p>
              {step.code ? (
                <pre className="api-doc-step-code"><code>{step.code}</code></pre>
              ) : null}
            </div>
          </details>
        ))}
      </div>
    </section>
  );
}
