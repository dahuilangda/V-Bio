// Facade for result-bundle parsing. The heavy parser (and jszip) load on
// demand; the coloring and storage helpers come from small statically-shared
// modules so eager code never pulls the parser bundle.
import { downloadResultBlob } from './backendTaskApi';
import type { ParsedResultBundle } from '../types/models';

export { ensureStructureConfidenceColoringData, stripStructureConfidenceColoringData } from './resultParser/cifConfidenceColoring';
export { compactResultConfidenceForStorage, mergePeptideCandidateRowsPreferRicher } from './resultParser/peptideCandidateStorage';

interface ParseResultBundleOptions {
  preservePeptideCandidateStructureText?: boolean;
  preferredStructureName?: string;
}

export async function parseResultBundle(
  blob: Blob,
  options?: ParseResultBundleOptions
): Promise<ParsedResultBundle | null> {
  const m = await import('./resultParser/resultBundleParser');
  return m.parseResultBundle(blob, options);
}

export async function downloadResultFile(taskId: string): Promise<void> {
  const blob = await downloadResultBlob(taskId, { mode: 'full' });
  const href = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = href;
  anchor.download = `${taskId}_results.zip`;
  anchor.click();
  URL.revokeObjectURL(href);
}
