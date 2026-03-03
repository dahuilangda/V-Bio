import { apiUrl, ENV } from '../utils/env';

export const API_HEADERS: Record<string, string> = {};
if (ENV.apiToken) {
  API_HEADERS['X-API-Token'] = ENV.apiToken;
}

export const BACKEND_TIMEOUT_MS = 20000;

export async function fetchWithTimeout(url: string, init: RequestInit = {}, timeoutMs = BACKEND_TIMEOUT_MS): Promise<Response> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
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
      throw new Error(`Backend request timeout after ${timeoutMs}ms for ${url}`);
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export async function requestBackend(path: string, init: RequestInit, timeoutMs = BACKEND_TIMEOUT_MS): Promise<Response> {
  const url = apiUrl(path);
  try {
    return await fetchWithTimeout(url, init, timeoutMs);
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
