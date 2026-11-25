import { loadBundle } from './registry';
import { ENV } from '../utils/env';

export interface MolstarBundle {
  createViewer: (host: HTMLElement, options: Record<string, unknown>) => Promise<any>;
}

export interface JsmeBundle {
  JSME: new (id: string, width: string, height: string, options: { options: string; guicolor: string }) => any;
}

function style(href: string): void {
  if (document.querySelector(`link[href="${href}"]`)) return;
  const link = document.createElement('link');
  link.rel = 'stylesheet';
  link.href = href;
  document.head.appendChild(link);
}

export function loadMolstar(): Promise<MolstarBundle> {
  style(ENV.molstarCssUrl);
  return loadBundle<MolstarBundle>('molstar', {
    load: () =>
      new Promise<MolstarBundle>((resolve, reject) => {
        const element = document.createElement('script');
        element.async = true;
        element.onload = () => {
          const creator = (globalThis as { molstar?: { Viewer?: { create?: unknown } } }).molstar?.Viewer?.create;
          if (typeof creator !== 'function') {
            reject(new Error('Mol* loaded without exposing Viewer.create.'));
            return;
          }
          resolve({ createViewer: creator as MolstarBundle['createViewer'] });
        };
        element.onerror = () => reject(new Error(`Failed to load ${ENV.molstarScriptUrl}`));
        element.src = ENV.molstarScriptUrl;
        document.head.appendChild(element);
      })
  });
}

interface JsmeGlobal {
  JSApplet?: { JSME?: unknown };
  jsmeOnLoad?: () => void;
}

export function loadJsme(): Promise<JsmeBundle> {
  return loadBundle<JsmeBundle>('jsme', {
    load: () =>
      new Promise<JsmeBundle>((resolve, reject) => {
        const global = globalThis as JsmeGlobal;
        if (global.JSApplet?.JSME) {
          resolve({ JSME: global.JSApplet.JSME as JsmeBundle['JSME'] });
          return;
        }
        global.jsmeOnLoad = () => {
          const JSME = global.JSApplet?.JSME;
          if (JSME) resolve({ JSME: JSME as JsmeBundle['JSME'] });
          else reject(new Error('JSME reported ready without exposing JSApplet.JSME.'));
        };
        const element = document.createElement('script');
        element.async = true;
        element.onerror = () => reject(new Error(`Failed to load ${ENV.jsmeScriptUrl}`));
        element.src = ENV.jsmeScriptUrl;
        document.head.appendChild(element);
      })
  });
}
