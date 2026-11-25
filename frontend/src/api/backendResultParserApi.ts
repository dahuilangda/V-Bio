import { createRpc } from '../bundles/rpc';
import type { Envelope } from '../bundles/rpc';
import { downloadResultBlob } from './backendTaskApi';
import type {
  ParseResultBundleOptions,
  ParseResultBundlePayload,
  ParseResultBundleResult
} from './resultParser/resultBundleContract';

export { ensureStructureConfidenceColoringData, stripStructureConfidenceColoringData } from './resultParser/cifConfidenceColoring';
export { compactResultConfidenceForStorage, mergePeptideCandidateRowsPreferRicher } from './resultParser/peptideCandidateStorage';
export type { ParseResultBundleOptions } from './resultParser/resultBundleContract';

const rpc = createRpc<ParseResultBundlePayload, ParseResultBundleResult>({
  open: (reply, crash) => {
    const worker = new Worker(new URL('./resultParser/resultBundleParser.worker.ts', import.meta.url), { type: 'module' });
    worker.addEventListener('message', (event: MessageEvent<Envelope<ParseResultBundleResult>>) => reply(event.data));
    worker.addEventListener('error', (event) => crash(new Error(event.message || 'Result parser worker failed.')));
    return { send: (id, payload) => worker.postMessage({ id, payload }), close: () => worker.terminate() };
  },
  idleMs: 60_000
});

export function parseResultBundle(blob: Blob, options?: ParseResultBundleOptions): Promise<ParseResultBundleResult> {
  return rpc.call({ blob, options });
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
