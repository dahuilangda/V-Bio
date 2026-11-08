// Worker 侧解析入口：整份结果包（JSZip 解压 + CIF/JSON 解析）移出主线程。
import { parseResultBundle } from './resultBundleParser';

interface WorkerParseRequest {
  id: number;
  blob: Blob;
  options?: {
    preservePeptideCandidateStructureText?: boolean;
    preferredStructureName?: string;
  };
}

interface WorkerParseResponse {
  id: number;
  ok: boolean;
  parsed?: unknown;
  error?: string;
}

self.addEventListener('message', (event: MessageEvent<WorkerParseRequest>) => {
  const { id, blob, options } = event.data;
  parseResultBundle(blob, options)
    .then((parsed) => {
      const response: WorkerParseResponse = { id, ok: true, parsed };
      (self as unknown as Worker).postMessage(response);
    })
    .catch((exc: unknown) => {
      const response: WorkerParseResponse = {
        id,
        ok: false,
        error: exc instanceof Error ? exc.message : String(exc)
      };
      (self as unknown as Worker).postMessage(response);
    });
});
