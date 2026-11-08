import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

// 每个用例重置模块状态（ver/lastStatus/缓存 map 都是模块级），用例之间互不泄漏。
async function loadModule() {
  vi.resetModules();
  return await import('./backendTaskApi');
}

const fetchCalls: Array<{ url: string; body?: unknown }> = [];
let stubResponse: { status: number; body: { stubs: { task_id: string; ver: string }[] } };
let batchResponse: { status: number; body: { statuses: { task_id: string; state: string }[] } };

function jsonResponse(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body)
  };
}

beforeEach(() => {
  fetchCalls.length = 0;
  stubResponse = { status: 200, body: { stubs: [] } };
  batchResponse = { status: 200, body: { statuses: [] } };
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string | URL, init?: { method?: string; body?: string }) => {
      const urlText = String(url);
      const body = init?.body ? JSON.parse(init.body) : undefined;
      fetchCalls.push({ url: urlText, body });
      if (urlText.includes('/status/stubs')) return jsonResponse(stubResponse.status, stubResponse.body);
      if (urlText.includes('/status/batch')) return jsonResponse(batchResponse.status, batchResponse.body);
      return jsonResponse(404, {});
    }) as unknown as typeof fetch
  );
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe('getTaskStatuses 两阶段增量', () => {
  it('首轮：取 stub 后对全部 id 发批量，并记录 ver', async () => {
    const { getTaskStatuses } = await loadModule();
    stubResponse.body.stubs = [
      { task_id: 't-1', ver: 'v1' },
      { task_id: 't-2', ver: 'v1' }
    ];
    batchResponse.body.statuses = [
      { task_id: 't-1', state: 'QUEUED' },
      { task_id: 't-2', state: 'QUEUED' }
    ];
    const statuses = await getTaskStatuses(['t-1', 't-2']);
    expect(Object.keys(statuses).sort()).toEqual(['t-1', 't-2']);
    const stubCalls = fetchCalls.filter((c) => c.url.includes('/status/stubs'));
    const batchCalls = fetchCalls.filter((c) => c.url.includes('/status/batch'));
    expect(stubCalls.length).toBe(1);
    expect(batchCalls.length).toBe(1);
    expect(batchCalls[0].body).toEqual({ task_ids: ['t-1', 't-2'] });
  });

  it('ver 未变化：不发批量请求，返回上一份全量', async () => {
    const { getTaskStatuses } = await loadModule();
    stubResponse.body.stubs = [
      { task_id: 't-3', ver: 'v1' },
      { task_id: 't-4', ver: 'v1' }
    ];
    batchResponse.body.statuses = [
      { task_id: 't-3', state: 'QUEUED' },
      { task_id: 't-4', state: 'QUEUED' }
    ];
    await getTaskStatuses(['t-3', 't-4']);

    stubResponse.body.stubs = [
      { task_id: 't-3', ver: 'v1' },
      { task_id: 't-4', ver: 'v1' }
    ];
    const statuses = await getTaskStatuses(['t-3', 't-4']);
    const batchCalls = fetchCalls.filter((c) => c.url.includes('/status/batch'));
    expect(batchCalls.length).toBe(1); // 只有首轮那次
    expect(statuses['t-3'].state).toBe('QUEUED');
    expect(statuses['t-4'].state).toBe('QUEUED');
  });

  it('ver 变化的 id 精确进入批量，未变化 id 不进', async () => {
    const { getTaskStatuses } = await loadModule();
    stubResponse.body.stubs = [
      { task_id: 't-5', ver: 'v1' },
      { task_id: 't-6', ver: 'v1' }
    ];
    batchResponse.body.statuses = [
      { task_id: 't-5', state: 'QUEUED' },
      { task_id: 't-6', state: 'RUNNING' }
    ];
    await getTaskStatuses(['t-5', 't-6']);

    // ACTIVE 缓存 TTL 1200ms：真实等待过期，让第二轮走到 stub→batch 路径
    await new Promise((resolve) => setTimeout(resolve, 1250));
    stubResponse.body.stubs = [
      { task_id: 't-5', ver: 'v1' },
      { task_id: 't-6', ver: 'v2' }
    ];
    batchResponse.body.statuses = [{ task_id: 't-6', state: 'SUCCESS' }];
    const statuses = await getTaskStatuses(['t-5', 't-6']);
    const batchCalls = fetchCalls.filter((c) => c.url.includes('/status/batch'));
    expect(batchCalls.length).toBe(2);
    expect(batchCalls[1].body).toEqual({ task_ids: ['t-6'] });
    expect(statuses['t-6'].state).toBe('SUCCESS');
    // 未变化 id 返回上一份全量（不缺席）
    expect(statuses['t-5'].state).toBe('QUEUED');
  });

  it('stub 端点不可用：退回全量批量路径', async () => {
    const { getTaskStatuses } = await loadModule();
    stubResponse.status = 404;
    batchResponse.body.statuses = [{ task_id: 't-7', state: 'QUEUED' }];
    const statuses = await getTaskStatuses(['t-7']);
    expect(statuses['t-7'].state).toBe('QUEUED');
    const batchCalls = fetchCalls.filter((c) => c.url.includes('/status/batch'));
    expect(batchCalls.length).toBe(1);
  });
});
