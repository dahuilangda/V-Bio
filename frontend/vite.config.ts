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
  const centralApiBase = normalizeProxyTarget(env.CENTRAL_API_URL);
  const target = normalizedApiBase || centralApiBase || 'http://127.0.0.1:5000';
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
    }
    // Direct runtime proxies removed (F1): every runtime call now goes through
    // /vbio-api (the authenticated gateway). The runtime backend itself binds loopback.
  };

  return {
    plugins: [react()],
    build: {
      chunkSizeWarningLimit: 1000,
      // No <link rel="modulepreload"> injection. WebKit (Safari) has a bug where
      // dynamic import() of a modulepreload-ed but unused URL never settles: the
      // route transition then freezes the previous view forever while the URL bar
      // has already changed (observed on the components tab -> nav navigation).
      // On-demand imports fetch reliably in every browser; the preloads were
      // also flagged as wasted by Safari itself.
      modulePreload: false,
      rollupOptions: {
        output: {
          manualChunks(id) {
            if (!id.includes('/node_modules/')) return undefined;
            if (id.includes('/node_modules/jszip/')) return 'vendor-zip';
            if (id.includes('/node_modules/js-yaml/')) return 'vendor-yaml';
            if (
              id.includes('/node_modules/react/') ||
              id.includes('/node_modules/react-dom/') ||
              id.includes('/node_modules/scheduler/')
            ) {
              return 'vendor-react';
            }
            // Markdown 渲染链（unified/micromark 全家桶）只有 copilot 用，
            // 独立成桶避免抬进所有路由的首屏 vendor。
            const markdownMarkers = [
              '/node_modules/react-markdown/',
              '/node_modules/remark',
              '/node_modules/micromark',
              '/node_modules/mdast',
              '/node_modules/unified',
              '/node_modules/unist',
              '/node_modules/hast',
              '/node_modules/vfile',
              '/node_modules/property-information',
              '/node_modules/character-entities',
              '/node_modules/decode-named-character-reference',
              '/node_modules/space-separated-tokens',
              '/node_modules/comma-separated-tokens',
              '/node_modules/web-namespaces',
              '/node_modules/zwitch',
              '/node_modules/bail',
              '/node_modules/trough',
              '/node_modules/is-plain-obj'
            ];
            if (markdownMarkers.some((marker) => id.includes(marker))) return 'vendor-markdown';
            return 'vendor';
          }
        }
      }
    },
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
