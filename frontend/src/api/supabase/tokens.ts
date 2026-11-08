import { ApiTokenUsage, ApiTokenUsageDaily } from '../../types/models';
import { buildSupabaseUrlCandidates, fetchWithTimeout, request } from './rest';



// API token 用量分页查询。

function parseTotalFromContentRange(value: string | null): number {
  if (!value) return 0;
  const slashIdx = value.lastIndexOf('/');
  if (slashIdx < 0) return 0;
  const raw = value.slice(slashIdx + 1).trim();
  const total = Number(raw);
  return Number.isFinite(total) && total >= 0 ? Math.floor(total) : 0;
}

export async function listApiTokenUsagePage(
  tokenId: string,
  options: {
    sinceIso?: string;
    limit: number;
    offset: number;
  }
): Promise<{ rows: ApiTokenUsage[]; total: number }> {
  const normalizedTokenId = String(tokenId || '').trim();
  if (!normalizedTokenId) {
    return { rows: [], total: 0 };
  }
  const limit = Math.max(1, Math.floor(options.limit || 1));
  const offset = Math.max(0, Math.floor(options.offset || 0));
  const query: Record<string, string | undefined> = {
    select: '*',
    token_id: `eq.${normalizedTokenId}`,
    order: 'created_at.desc',
    limit: String(limit),
    offset: String(offset)
  };
  if (options.sinceIso?.trim()) {
    query.created_at = `gte.${options.sinceIso.trim()}`;
  }

  const candidates = buildSupabaseUrlCandidates('/api_token_usage', query);
  let lastError: Error | null = null;
  for (let i = 0; i < candidates.length; i += 1) {
    const url = candidates[i];
    let res: Response;
    try {
      res = await fetchWithTimeout(url, {
        method: 'GET',
        headers: {
          'Content-Type': 'application/json',
          Prefer: 'count=exact'
        }
      });
    } catch (error) {
      lastError = error instanceof Error ? error : new Error('Unknown network error');
      continue;
    }

    if ((res.status === 404 || res.status === 405) && i < candidates.length - 1) {
      lastError = new Error(`Supabase candidate returned ${res.status}: ${url}`);
      continue;
    }

    if (!res.ok) {
      const text = await res.text();
      throw new Error(`PostgREST ${res.status}: ${text}`);
    }

    const apiText = await res.text();
    const rows = apiText.trim() ? (JSON.parse(apiText) as ApiTokenUsage[]) : [];
    const total = parseTotalFromContentRange(res.headers.get('content-range'));
    return { rows, total };
  }

  const detail = lastError ? ` Last error: ${lastError.message}` : '';
  throw new Error(`Supabase-lite request failed. Tried: ${candidates.join(', ')}.${detail}`);
}

export async function listApiTokenUsageDaily(tokenId: string, sinceIso?: string): Promise<ApiTokenUsageDaily[]> {
  const normalizedTokenId = String(tokenId || '').trim();
  if (!normalizedTokenId) return [];
  const query: Record<string, string | undefined> = {
    select: '*',
    token_id: `eq.${normalizedTokenId}`,
    order: 'usage_day.asc'
  };
  if (sinceIso?.trim()) {
    query.usage_day = `gte.${sinceIso.trim().slice(0, 10)}`;
  }
  return request<ApiTokenUsageDaily[]>('/api_token_usage_daily', undefined, query);
}

export async function listApiTokenUsageDailyByTokenIds(tokenIds: string[], sinceIso?: string): Promise<ApiTokenUsageDaily[]> {
  const ids = Array.from(new Set(tokenIds.map((id) => String(id || '').trim()).filter(Boolean)));
  if (ids.length === 0) return [];
  const chunkSize = 150;
  const rows = await Promise.all(
    Array.from({ length: Math.ceil(ids.length / chunkSize) }, (_, i) => ids.slice(i * chunkSize, (i + 1) * chunkSize)).map(
      (chunk) => {
        const query: Record<string, string | undefined> = {
          select: '*',
          token_id: `in.(${chunk.join(',')})`,
          order: 'usage_day.asc'
        };
        if (sinceIso?.trim()) {
          query.usage_day = `gte.${sinceIso.trim().slice(0, 10)}`;
        }
        return request<ApiTokenUsageDaily[]>('/api_token_usage_daily', undefined, query);
      }
    )
  );
  return rows.flat();
}
