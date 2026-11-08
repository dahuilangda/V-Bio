/** 项目级任务状态 SSE 客户端：终态变更即时回调，异常由调用方回退轮询。 */

export interface TaskStatusEntry {
  task_id: string;
  state: string;
}

export interface TaskStatusStreamHandle {
  close: () => void;
}

interface StreamParams {
  projectId: string;
  headers: Record<string, string>;
  onEntries: (entries: TaskStatusEntry[]) => void;
  onError?: () => void;
}

/** 解析一段 SSE 文本：返回 changed 事件帧与剩余未完成缓冲。纯函数，便于测试。 */
export function parseStatusStreamChunk(
  buffer: string,
  chunk: string
): { entries: TaskStatusEntry[][]; buffer: string } {
  const combined = buffer + chunk;
  const parts = combined.split("\n\n");
  const bufferRest = parts.pop() ?? "";
  const entriesList: TaskStatusEntry[][] = [];
  for (const part of parts) {
    if (!part || part.startsWith(":")) continue;
    let event = "message";
    let data = "";
    for (const line of part.split("\n")) {
      if (line.startsWith("event:")) event = line.slice(6).trim();
      else if (line.startsWith("data:")) data += line.slice(5).trim();
    }
    if (event !== "changed" || !data) continue;
    try {
      const parsed = JSON.parse(data) as { entries?: unknown };
      if (!Array.isArray(parsed.entries)) continue;
      const entries: TaskStatusEntry[] = [];
      for (const item of parsed.entries) {
        const record = item as Record<string, unknown>;
        const taskId = String(record?.task_id || "").trim();
        const state = String(record?.state || "").trim();
        if (taskId && state) entries.push({ task_id: taskId, state });
      }
      if (entries.length > 0) entriesList.push(entries);
    } catch {
      // 单帧坏数据不影响流
    }
  }
  return { entries: entriesList, buffer: bufferRest };
}

export async function connectTaskStatusStream(params: StreamParams): Promise<TaskStatusStreamHandle> {
  const controller = new AbortController();
  let buffer = "";
  let closed = false;

  const close = () => {
    closed = true;
    controller.abort();
  };

  try {
    const response = await fetch(
      `/vbio-api/tasks/status/stream?project_id=${encodeURIComponent(params.projectId)}`,
      {
        headers: { ...params.headers, Accept: "text/event-stream" },
        signal: controller.signal,
        cache: "no-store"
      }
    );
    if (!response.ok || !response.body) {
      params.onError?.();
      return { close };
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    const pump = async (): Promise<void> => {
      for (;;) {
        const { done, value } = await reader.read();
        if (done || closed) return;
        const chunk = decoder.decode(value, { stream: true });
        const { entries: batches, buffer: rest } = parseStatusStreamChunk(buffer, chunk);
        buffer = rest;
        for (const entries of batches) params.onEntries(entries);
      }
    };
    void pump().catch(() => {
      if (!closed) {
        params.onError?.();
      }
    });
  } catch {
    params.onError?.();
  }
  return { close };
}
