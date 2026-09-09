/**
 * Static "API Docs" panel — the seven-step usage guide of the API access
 * page. Zero props: pure presentation extracted verbatim from ApiAccessPage.
 */
import { Info } from 'lucide-react';
import { InfoTip } from '../components/common/InfoTip';
import './ApiDocsPanel.css';

export function ApiDocsPanel() {
  return (
    <section className="panel api-docs-panel">
      <div className="api-section-head">
        <h2><Info size={16} /> API Docs</h2>
      </div>
      <ol className="api-doc-steps">
        <li>
          <strong>Create project in V-Bio web</strong>
          <InfoTip text="API does not create project. Pick existing project in the builder." />
        </li>
        <li>
          <strong>Create token and bind project</strong>
          <InfoTip text="Token registry controls submit/cancel/delete permissions per project." />
        </li>
        <li>
          <strong>Use generated submit command</strong>
          <InfoTip text="Workflow is fixed by project type; select backend where applicable before copying." />
        </li>
        <li>
          <strong>YAML format (prediction)</strong>
          <InfoTip text="Use `version + sequences`; ligand entries can use `smiles` or `ccd`; add constraints/properties/templates only when needed." />
        </li>
        <li>
          <strong>Track and download</strong>
          <InfoTip text="Use status and result commands with the same `project_id` and token." />
        </li>
        <li>
          <strong>Cancel or delete safely</strong>
          <InfoTip text="`operation_mode=cancel|delete`, permission checked by gateway." />
        </li>
        <li>
          <strong>Reuse from history</strong>
          <InfoTip text="Recent copied commands are saved below builder for one-click reuse." />
        </li>
      </ol>
    </section>
  );
}
