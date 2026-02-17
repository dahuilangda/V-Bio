import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

function normalizeProxyTarget(raw?: string): string {
  const value = raw?.trim().replace(/^['"]|['"]$/g, '') || '';
  if (!value) {
    return '';
  }
  const candidate = /^https?:\/\//i.test(value) ? value : `http://${value}`;
  try {
    const parsed = new URL(candidate);
    if (!parsed.host) {
      return '';
    }
    return `${parsed.protocol}//${parsed.host}`;
  } catch {
    return '';
  }
}

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  const normalizedApiBase = normalizeProxyTarget(env.VITE_API_BASE_URL);
  const target = normalizedApiBase || 'http://127.0.0.1:5000';
  const supabaseTarget =
    normalizeProxyTarget(env.VITE_SUPABASE_REST_PROXY_TARGET) ||
    normalizeProxyTarget(env.VITE_SUPABASE_REST_URL) ||
    'http://127.0.0.1:54321';
  const managementTarget =
    normalizeProxyTarget(env.VITE_VBIO_MANAGEMENT_PROXY_TARGET) ||
    normalizeProxyTarget(env.VITE_VBIO_MANAGEMENT_API_BASE_URL) ||
    'http://127.0.0.1:5055';
  const proxy: Record<string, string | { target: string; changeOrigin: boolean; rewrite?: (path: string) => string }> = {
    '/supabase': {
      target: supabaseTarget,
      changeOrigin: true,
      rewrite: (path) => path.replace(/^\/supabase/, '')
    },
    '/vbio-api': {
      target: managementTarget,
      changeOrigin: true
    },
    '/predict': target,
    '/status': target,
    '/results': target,
    '/tasks': target,
    '/api': target,
    '/monitor': target
  };

  return {
    plugins: [react()],
    server: {
      port: 5173,
      host: true,
      proxy
    },
    preview: {
      host: true,
      port: 5173,
      proxy
    }
  };
});
