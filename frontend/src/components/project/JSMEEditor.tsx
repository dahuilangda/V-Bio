import { useEffect, useMemo, useRef, useState } from 'react';
import { loadJsme } from '../../bundles/vendor';

interface JSMEEditorProps {
  smiles: string;
  onSmilesChange: (smiles: string) => void;
  height?: number;
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

      if (appletRef.current) appletRef.current.setSize(widthPx, heightPx);
    };

    applySize();

    const observer = new ResizeObserver(applySize);
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
        const { JSME } = await loadJsme();
        if (cancelled || !mountRef.current) return;

        mountRef.current.innerHTML = '';
        mountRef.current.id = editorId;
        const themeOptions = getJsmeThemeOptions();
        const applet = new JSME(editorId, '100%', '100%', {
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
            const width = Math.max(1, Math.floor(host.clientWidth));
            const h = Math.max(1, Math.floor(host.clientHeight || height));
            try {
              applet.setSize(width, h);
            } catch {
              try {
                applet.setSize(`${width}`, `${h}`);
              } catch {
                try {
                  applet.setSize('100%', '100%');
                } catch {
                  // Ignore unsupported signatures.
                }
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
        const parsed = (appletRef.current.smiles?.() || '').trim();
        if (parsed && parsed !== incoming) {
          onSmilesChangeRef.current(parsed);
        }
      } catch {
        // ignore malformed incoming smiles
      }
    }
  }, [smiles]);

  if (error) {
    return <div className="alert error" role="alert">{error}</div>;
  }

  return (
    <div ref={hostRef} className="jsme-host" style={{ height: `${height}px` }}>
      <div ref={mountRef} className="jsme-mount" />
    </div>
  );
}
