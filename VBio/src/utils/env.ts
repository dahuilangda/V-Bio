function normalizeBaseUrl(raw?: string): string {
  const value = raw?.trim().replace(/^['"]|['"]$/g, '') || '';
  if (!value) {
    return '';
  }
  if (value.startsWith('/')) {
    return value.replace(/\/$/, '');
  }
  if (/^https?:\/\//i.test(value)) {
    try {
      const parsed = new URL(value);
      if (!parsed.host) {
        return '';
      }
      return value.replace(/\/$/, '');
    } catch {
      return '';
    }
  }
  if (/^(localhost|127\.0\.0\.1)(:\d+)?$/i.test(value) || /^[a-z0-9.-]+:\d+$/i.test(value)) {
    return `http://${value}`.replace(/\/$/, '');
  }
  return '';
}

const apiBase = normalizeBaseUrl(import.meta.env.VITE_API_BASE_URL);
const supabaseConfigured = normalizeBaseUrl(import.meta.env.VITE_SUPABASE_REST_URL);

const runningOnLocalhost =
  typeof window !== 'undefined' &&
  (window.location.hostname === '127.0.0.1' || window.location.hostname === 'localhost');

const supabasePointsToLocalhost = /(^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$)|(^127\.0\.0\.1:\d+$)|(^localhost:\d+$)/i.test(
  supabaseConfigured
);
const apiPointsToLocalhost = /(^https?:\/\/(127\.0\.0\.1|localhost)(:\d+)?$)|(^127\.0\.0\.1:\d+$)|(^localhost:\d+$)/i.test(
  apiBase
);

const resolvedApiBaseUrl =
  apiBase && !(apiPointsToLocalhost && !runningOnLocalhost)
    ? apiBase
    : '';

// If frontend is opened from another machine, localhost in env points to the user's own browser host.
const resolvedSupabaseRestUrl =
  supabaseConfigured && !(supabasePointsToLocalhost && !runningOnLocalhost)
    ? supabaseConfigured
    : '/supabase';

export const ENV = {
  apiBaseUrl: resolvedApiBaseUrl,
  apiToken: import.meta.env.VITE_API_TOKEN?.trim() || 'development-api-token',
  supabaseRestUrl: resolvedSupabaseRestUrl,
  jsmeScriptUrl:
    import.meta.env.VITE_JSME_SCRIPT_URL?.trim() ||
    'https://jsme-editor.github.io/dist/jsme/jsme.nocache.js',
  molstarScriptUrl:
    import.meta.env.VITE_MOLSTAR_SCRIPT_URL?.trim() ||
    'https://unpkg.com/molstar/build/viewer/molstar.js',
  molstarCssUrl:
    import.meta.env.VITE_MOLSTAR_CSS_URL?.trim() ||
    'https://unpkg.com/molstar/build/viewer/molstar.css',
  rdkitScriptUrl:
    import.meta.env.VITE_RDKIT_SCRIPT_URL?.trim() ||
    'https://cdn.jsdelivr.net/npm/@rdkit/rdkit/dist/RDKit_minimal.js',
  rdkitWasmUrl:
    import.meta.env.VITE_RDKIT_WASM_URL?.trim() ||
    'https://cdn.jsdelivr.net/npm/@rdkit/rdkit/dist/RDKit_minimal.wasm'
} as const;

export const apiUrl = (path: string) => {
  if (!ENV.apiBaseUrl) {
    return path;
  }
  return `${ENV.apiBaseUrl.replace(/\/$/, '')}${path}`;
};
