// Facade for result-bundle parsing. The heavy parser (and jszip) run in a
// Worker by default so archive unzip + CIF/JSON parsing never block the main
// thread; a main-thread dynamic import stays as the fallback for environments
// without Worker support. Coloring and storage helpers come from small
// statically-shared modules so eager code never pulls the parser bundle.
import { downloadResultBlob } from './backendTaskApi';
import type { ParsedResultBundle } from '../types/models';

export { ensureStructureConfidenceColoringData, stripStructureConfidenceColoringData } from './resultParser/cifConfidenceColoring';
export { compactResultConfidenceForStorage, mergePeptideCandidateRowsPreferRicher } from './resultParser/peptideCandidateStorage';

interface ParseResultBundleOptions {
  preservePeptideCandidateStructureText?: boolean;
  preferredStructureName?: string;
}

class WorkerUnavailableError extends Error {}

interface WorkerParseRequest {
  id: number;
  blob: Blob;
  options?: ParseResultBundleOptions;
}

interface WorkerParseResponse {
  id: number;
  ok: boolean;
  parsed?: ParsedResultBundle | null;
  error?: string;
}

let worker: Worker | null = null;
let workerUnavailable = false;
let nextRequestId = 1;
const pending = new Map<
  number,
  { resolve: (value: ParsedResultBundle | null) => void; reject: (error: Error) => void }
>();

function ensureWorker(): Worker | null {
  if (workerUnavailable) return null;
  if (worker) return worker;
  try {
    worker = new Worker(new URL('./resultParser/resultBundleParser.worker.ts', import.meta.url), {
      type: 'module'
    });
  } catch {
    workerUnavailable = true;
    return null;
  }
  worker.addEventListener('message', (event: MessageEvent<WorkerParseResponse>) => {
    const entry = pending.get(event.data.id);
    if (!entry) return;
    pending.delete(event.data.id);
    if (event.data.ok) entry.resolve(event.data.parsed ?? null);
    else entry.reject(new Error(event.data.error || 'Result archive parse failed.'));
  });
  worker.addEventListener('error', () => {
    workerUnavailable = true;
    worker?.terminate();
    worker = null;
    for (const entry of pending.values()) entry.reject(new WorkerUnavailableError('Result parser worker crashed.'));
    pending.clear();
  });
  return worker;
}

function parseInWorker(
  blob: Blob,
  options?: ParseResultBundleOptions,
  timeoutMs = 120_000
): Promise<ParsedResultBundle | null> {
  const target = ensureWorker();
  if (!target) return Promise.reject(new WorkerUnavailableError('Worker unavailable.'));
  const id = nextRequestId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      if (pending.delete(id)) reject(new WorkerUnavailableError('Result parse timed out in worker.'));
    }, timeoutMs);
    pending.set(id, {
      resolve: (value) => {
        clearTimeout(timer);
        resolve(value);
      },
      reject: (error) => {
        clearTimeout(timer);
        reject(error);
      }
    });
    const request: WorkerParseRequest = { id, blob, options };
    target.postMessage(request);
  });
}

export async function parseResultBundle(
  blob: Blob,
  options?: ParseResultBundleOptions
): Promise<ParsedResultBundle | null> {
  if (!workerUnavailable) {
    try {
      return await parseInWorker(blob, options);
    } catch (error) {
      // 解析本身失败（ok:false）原样抛出；仅基础设施故障回退主线程。
      if (!(error instanceof WorkerUnavailableError)) throw error;
    }
  }
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
