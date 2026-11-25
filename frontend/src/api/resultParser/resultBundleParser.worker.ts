import type { Envelope } from '../../bundles/rpc';
import { parseResultBundle } from './resultBundleParser';
import type { ParseResultBundleOptions } from './resultBundleContract';

const scope = self as unknown as {
  postMessage: (message: Envelope) => void;
  onmessage: ((event: MessageEvent<{ id: number; payload: { blob: Blob; options?: ParseResultBundleOptions } }>) => void) | null;
};

scope.onmessage = (event) => {
  const { id, payload } = event.data;
  void parseResultBundle(payload.blob, payload.options).then(
    (parsed) => scope.postMessage({ id, ok: true, result: parsed }),
    (error: unknown) => scope.postMessage({ id, ok: false, error: error instanceof Error ? error.message : String(error) })
  );
};
