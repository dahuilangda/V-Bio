import { apiUrl, ENV } from '../utils/env';

function getAuthHeaders(): Record<string, string> {
  return ENV.apiToken ? { 'X-API-Token': ENV.apiToken } : {};
}

export const API_HEADERS: Record<string, string> = new Proxy(
  {},
  {
    ownKeys: () => Reflect.ownKeys(getAuthHeaders()),
    getOwnPropertyDescriptor: (_target, key) => {
      const headers = getAuthHeaders();
      if (!Object.prototype.hasOwnProperty.call(headers, key)) return undefined;
      return { enumerable: true, configurable: true };
    },
    get: (_target, key) => {
      if (typeof key !== 'string') return undefined;
      return getAuthHeaders()[key];
    }
  }
);

export const BACKEND_TIMEOUT_MS = 20000;

// 以原始字符串为键缓存解析结果：每请求的 localStorage 读取保留（成本低），
// JSON.parse 只在会话真正变化时执行一次。
let sessionRawCache: string | null = null;
let sessionTokenCache: string | null = null;

function managementSessionHeader(): string | null {
  try {
    const raw = localStorage.getItem('vbio_session');
    if (raw === null) return null;
    if (raw === sessionRawCache) return sessionTokenCache;
    const parsed = JSON.parse(raw) as { managementToken?: string };
    sessionRawCache = raw;
    sessionTokenCache = parsed.managementToken || null;
    return sessionTokenCache;
  } catch {
    return null;
  }
}

export { managementSessionHeader };

export async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = BACKEND_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  // 内部 controller 兜底超时，外部 signal 手动转发（AbortSignal.any 兼容性不足）。
  const external = init.signal;
  const onExternalAbort = () => controller.abort();
  if (external) {
    if (external.aborted) onExternalAbort();
    else external.addEventListener('abort', onExternalAbort);
  }
  try {
    const method = String(init.method || 'GET').toUpperCase();
    const cacheMode: RequestCache | undefined = init.cache ?? (method === 'GET' ? 'no-store' : undefined);
    return await fetch(url, {
      ...init,
      ...(cacheMode ? { cache: cacheMode } : {}),
      signal: controller.signal
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') {
      if (external?.aborted) throw error; // 调用方主动取消，原样抛出
      throw new Error(`Backend request timeout after ${timeoutMs}ms for ${url}`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
    if (external) external.removeEventListener('abort', onExternalAbort);
  }
}

export async function requestBackend(path: string, init: RequestInit, timeoutMs = BACKEND_TIMEOUT_MS): Promise<Response> {
  const url = apiUrl(path);
  const headers = { ...(init.headers as Record<string, string> | undefined) };
  // Gateway admin passthrough routes authenticate with the management session, not the
  // shared runtime token.
  if (url.startsWith('/vbio-api/admin/') || url.startsWith('/vbio-api/api/admin/')) {
    const sessionHeader = managementSessionHeader();
    if (sessionHeader) headers['X-VBio-Session'] = sessionHeader;
  }
  try {
    return await fetchWithTimeout(url, { ...init, headers }, timeoutMs);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(`Backend request failed for ${path} (${url}): ${message}`);
  }
}

export function managementApiUrl(path: string): string {
  const normalizedPath = path.startsWith('/') ? path : `/${path}`;
  if (!ENV.managementApiBaseUrl) {
    return normalizedPath;
  }
  const base = ENV.managementApiBaseUrl.replace(/\/$/, '');
  if (base.endsWith('/vbio-api') && normalizedPath.startsWith('/vbio-api/')) {
    return `${base}${normalizedPath.slice('/vbio-api'.length)}`;
  }
  return `${base}${normalizedPath}`;
}

export async function requestManagement(path: string, init: RequestInit, timeoutMs = BACKEND_TIMEOUT_MS): Promise<Response> {
  const url = managementApiUrl(path);
  try {
    return await fetchWithTimeout(url, init, timeoutMs);
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    throw new Error(`Management API request failed for ${path} (${url}): ${message}`);
  }
}
