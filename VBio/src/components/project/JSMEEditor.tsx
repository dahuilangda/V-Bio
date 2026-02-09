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
  const hostRef = useRef<HTMLDivElement | null>(null);
  const mountRef = useRef<HTMLDivElement | null>(null);
  const appletRef = useRef<any>(null);
  const onSmilesChangeRef = useRef(onSmilesChange);
  const [error, setError] = useState<string | null>(null);

  const editorId = useMemo(() => `jsme-${Math.random().toString(36).slice(2)}`, []);

  useEffect(() => {
    onSmilesChangeRef.current = onSmilesChange;
  }, [onSmilesChange]);

  useEffect(() => {
    const applySize = () => {
      const host = hostRef.current;
      const mount = mountRef.current;
      if (!host || !mount) return;

      const widthPx = Math.max(1, Math.floor(host.clientWidth));
      const heightPx = Math.max(1, Math.floor(host.clientHeight || height));
      mount.style.width = '100%';
      mount.style.height = '100%';

      const applet = appletRef.current;
      if (applet && typeof applet.setSize === 'function') {
        try {
          applet.setSize('100%', '100%');
        } catch {
          try {
            applet.setSize(widthPx, heightPx);
          } catch {
            try {
              applet.setSize(`${widthPx}`, `${heightPx}`);
            } catch {
              // Ignore unsupported signatures.
            }
          }
        }
      }
    };

    applySize();

    if (typeof ResizeObserver === 'undefined') {
      window.addEventListener('resize', applySize);
      return () => {
        window.removeEventListener('resize', applySize);
      };
    }

    const observer = new ResizeObserver(() => {
      applySize();
    });
    if (hostRef.current) {
      observer.observe(hostRef.current);
    }
    return () => {
      observer.disconnect();
    };
  }, [height]);

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
        const applet = new JSApplet.JSME(editorId, '100%', '100%', {
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

        // Re-apply fit after initial paint to avoid transient overflow.
        if (typeof window !== 'undefined' && typeof window.requestAnimationFrame === 'function') {
          window.requestAnimationFrame(() => {
            const host = hostRef.current;
            if (!host || typeof applet.setSize !== 'function') return;
            try {
              applet.setSize('100%', '100%');
            } catch {
              const width = Math.max(1, Math.floor(host.clientWidth));
              const h = Math.max(1, Math.floor(host.clientHeight || height));
              try {
                applet.setSize(width, h);
              } catch {
                // Ignore unsupported signatures.
              }
            }
          });
        }
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
    <div ref={hostRef} className="jsme-host" style={{ height: `${height}px` }}>
      <div ref={mountRef} className="jsme-mount" />
    </div>
  );
}
