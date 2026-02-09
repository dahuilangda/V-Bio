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

interface JsmeThemeOptions {
  guicolor: string;
  guiAtomColor: string;
  markerIconColor: string;
}

function readThemeColor(name: string, fallback: string): string {
  if (typeof window === 'undefined' || typeof document === 'undefined') return fallback;
  const value = window.getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return value || fallback;
}

function getJsmeThemeOptions(): JsmeThemeOptions {
  return {
    guicolor: readThemeColor('--surface', '#ffffff'),
    guiAtomColor: readThemeColor('--primary', '#1f4f3f'),
    markerIconColor: readThemeColor('--primary', '#1f4f3f')
  };
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
  const mountRef = useRef<HTMLDivElement | null>(null);
  const appletRef = useRef<any>(null);
  const onSmilesChangeRef = useRef(onSmilesChange);
  const [error, setError] = useState<string | null>(null);

  const editorId = useMemo(() => `jsme-${Math.random().toString(36).slice(2)}`, []);

  useEffect(() => {
    onSmilesChangeRef.current = onSmilesChange;
  }, [onSmilesChange]);

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

        if (cancelled || !mountRef.current) return;

        // JSME script may still bootstrap after script onload (GWT nocache flow).
        const JSApplet = await waitForJSMEReady();
        if (cancelled || !mountRef.current) return;

        mountRef.current.innerHTML = '';
        mountRef.current.id = editorId;
        const themeOptions = getJsmeThemeOptions();
        const applet = new JSApplet.JSME(editorId, '100%', `${height}px`, {
          options: 'star',
          ...themeOptions
        });

        appletRef.current = applet;

        if (typeof applet.setUserInterfaceBackgroundColor === 'function') {
          applet.setUserInterfaceBackgroundColor(themeOptions.guicolor);
        }

        if (smiles.trim()) {
          applet.readGenericMolecularInput(smiles.trim());
        }

        const sync = () => {
          try {
            const next = applet.smiles();
            onSmilesChangeRef.current(next || '');
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
      appletRef.current = null;
      if (mountRef.current) {
        mountRef.current.innerHTML = '';
      }
    };
  }, [editorId, height]);

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

  return (
    <div className="jsme-host" style={{ height: `${height}px` }}>
      <div ref={mountRef} className="jsme-mount" />
    </div>
  );
}
