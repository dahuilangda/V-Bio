import { useEffect, useMemo, useRef, useState } from 'react';
import { ENV } from '../../utils/env';
import { loadScript } from '../../utils/script';

interface JSMEEditorProps {
  smiles: string;
  onSmilesChange: (smiles: string) => void;
  height?: number;
}

declare global {
  interface Window {
    jsmeOnLoad?: () => void;
  }
}

async function waitForJSMEReady(timeoutMs = 12000, intervalMs = 120): Promise<any> {
  const start = Date.now();

  while (Date.now() - start < timeoutMs) {
    const JSApplet = (window as any).JSApplet;
    if (JSApplet?.JSME) {
      return JSApplet;
    }
    await new Promise((resolve) => setTimeout(resolve, intervalMs));
  }

  throw new Error('JSME initialization timed out.');
}

export function JSMEEditor({ smiles, onSmilesChange, height = 340 }: JSMEEditorProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const appletRef = useRef<any>(null);
  const [error, setError] = useState<string | null>(null);

  const editorId = useMemo(() => `jsme-${Math.random().toString(36).slice(2)}`, []);

  useEffect(() => {
    let cancelled = false;

    const boot = async () => {
      try {
        setError(null);
        // Silence JSME runtime warning when callback is absent.
        if (typeof window !== 'undefined' && typeof window.jsmeOnLoad !== 'function') {
          window.jsmeOnLoad = () => {};
        }
        await loadScript(ENV.jsmeScriptUrl);

        if (cancelled || !hostRef.current) return;

        // JSME script may still bootstrap after script onload (GWT nocache flow).
        const JSApplet = await waitForJSMEReady();
        if (cancelled || !hostRef.current) return;

        hostRef.current.id = editorId;
        const applet = new JSApplet.JSME(editorId, '100%', `${height}px`, {
          options: 'oldlook,star'
        });

        appletRef.current = applet;

        if (smiles.trim()) {
          applet.readGenericMolecularInput(smiles.trim());
        }

        const sync = () => {
          try {
            const next = applet.smiles();
            onSmilesChange(next || '');
          } catch {
            // ignore callback parse errors
          }
        };

        applet.setCallBack('AfterStructureModified', sync);
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof Error ? e.message : 'Unable to load JSME editor.');
      }
    };

    void boot();

    return () => {
      cancelled = true;
    };
  }, [editorId, height, onSmilesChange]);

  useEffect(() => {
    if (!appletRef.current) return;
    const current = (appletRef.current.smiles?.() || '').trim();
    const incoming = smiles.trim();
    if (incoming !== current) {
      try {
        appletRef.current.readGenericMolecularInput(incoming);
      } catch {
        // ignore malformed incoming smiles
      }
    }
  }, [smiles]);

  if (error) {
    return <div className="alert error">{error}</div>;
  }

  return <div ref={hostRef} className="jsme-host" style={{ height: `${height}px` }} />;
}
