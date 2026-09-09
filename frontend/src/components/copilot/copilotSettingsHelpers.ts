/**
 * Copilot settings helpers — the minimal set extracted for CopilotSettingsPanel.
 */
import type { CopilotTestSubResult } from '../../api/copilotApi';

export interface SettingsFormValues {
  proxy: string;
  api_url: string;
  api_key: string;
  model: string;
}

export function settingsTestState(result: CopilotTestSubResult): 'ok' | 'fail' | 'skipped' {
  if (result.skipped) return 'skipped';
  return result.ok ? 'ok' : 'fail';
}
