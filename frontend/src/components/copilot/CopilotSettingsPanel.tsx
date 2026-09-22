/**
 * Copilot settings panel (controlled). The parent owns the settings state.
 */
import { LoaderCircle, Settings, X } from 'lucide-react';
import type { CopilotTestResult } from '../../api/copilotApi';
import { settingsTestState as testState, type SettingsFormValues } from './copilotSettingsHelpers';

interface CopilotSettingsPanelProps {
  settingsForm: SettingsFormValues;
  settingsError: string;
  settingsHasKey: boolean;
  settingsMaskedKey: string;
  isSettingsSaved: boolean;
  isSettingsSaving: boolean;
  settingsTestResult: CopilotTestResult | null;
  isSettingsTesting: boolean;
  onFormChange: (v: SettingsFormValues | ((prev: SettingsFormValues) => SettingsFormValues)) => void;
  saveAction: () => void;
  testAction: () => void;
  onClose: () => void;
}

export function CopilotSettingsPanel(p: CopilotSettingsPanelProps) {
  return (
<div className="copilot-settings-overlay">
            <div className="copilot-settings-panel" onClick={(e) => e.stopPropagation()}>
              <div className="copilot-settings-head">
                <span className="copilot-settings-title">
                  <Settings size={15} />
                  Copilot Settings
                </span>
                <button type="button" className="copilot-settings-close" onClick={() => p.onClose()} aria-label="Close settings" title="Close settings">
                  <X size={16} />
                </button>
              </div>
              <div className="copilot-settings-body">
                <label className="copilot-settings-field">
                  <span className="copilot-settings-label">Outbound Proxy</span>
                  <input
                    type="text"
                    className="copilot-settings-input"
                    placeholder="http://172.16.34.31:2080"
                    value={p.settingsForm.proxy}
                    onChange={(e) => p.onFormChange((prev) => ({ ...prev, proxy: e.target.value }))}
                  />
                </label>
                <label className="copilot-settings-field">
                  <span className="copilot-settings-label">LLM Server URL</span>
                  <input
                    type="text"
                    className="copilot-settings-input"
                    placeholder="https://api.openai.com/v1/chat/completions"
                    value={p.settingsForm.api_url}
                    onChange={(e) => p.onFormChange((prev) => ({ ...prev, api_url: e.target.value }))}
                  />
                </label>
                <label className="copilot-settings-field">
                  <span className="copilot-settings-label">
                    API Key{p.settingsHasKey ? <em className="copilot-settings-current"> (current: {p.settingsMaskedKey})</em> : null}
                  </span>
                  <input
                    type="password"
                    className="copilot-settings-input"
                    placeholder={p.settingsHasKey ? 'Leave blank to keep current key' : 'Enter API key'}
                    value={p.settingsForm.api_key}
                    onChange={(e) => p.onFormChange((prev) => ({ ...prev, api_key: e.target.value }))}
                  />
                </label>
                <label className="copilot-settings-field">
                  <span className="copilot-settings-label">Model</span>
                  <input
                    type="text"
                    className="copilot-settings-input"
                    placeholder="e.g. gpt-4o"
                    value={p.settingsForm.model}
                    onChange={(e) => p.onFormChange((prev) => ({ ...prev, model: e.target.value }))}
                  />
                </label>
                {p.settingsError ? <div className="copilot-settings-error">{p.settingsError}</div> : null}
                {p.isSettingsSaved ? <div className="copilot-settings-success">Settings saved — applied live.</div> : null}
                {p.settingsTestResult ? (
                  <div className="copilot-settings-test-results">
                    <div className={`copilot-settings-test-item ${testState(p.settingsTestResult.proxy)}`}>
                      <span className="copilot-settings-test-name">Network</span>
                      <span className="copilot-settings-test-detail">{p.settingsTestResult.proxy.detail}</span>
                    </div>
                    <div className={`copilot-settings-test-item ${testState(p.settingsTestResult.llm)}`}>
                      <span className="copilot-settings-test-name">LLM</span>
                      <span className="copilot-settings-test-detail">{p.settingsTestResult.llm.detail}</span>
                    </div>
                  </div>
                ) : null}
              </div>
              <div className="copilot-settings-actions">
                <button
                  type="button"
                  className="copilot-settings-btn secondary"
                  onClick={() => void p.testAction()}
                  disabled={p.isSettingsTesting || p.isSettingsSaving}
                >
                  {p.isSettingsTesting ? <LoaderCircle size={14} className="spin" /> : null}
                  Test Connection
                </button>
                <button
                  type="button"
                  className="copilot-settings-btn primary"
                  onClick={() => void p.saveAction()}
                  disabled={p.isSettingsSaving || p.isSettingsTesting}
                >
                  {p.isSettingsSaving ? <LoaderCircle size={14} className="spin" /> : null}
                  Save Settings
                </button>
              </div>
            </div>
          </div>
  );
}
