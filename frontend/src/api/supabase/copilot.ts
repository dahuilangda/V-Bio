import { appendCopilotMessageCache, copilotMessageCacheKey, filterCopilotMessageCache, readCachedCopilotMessages, writeCopilotMessageCache } from '../copilotLocalCache';
import { ProjectCopilotMessage, ProjectCopilotState } from '../../types/models';
import { asObjectRecord, buildInFilter, listUsersByIds, request } from './rest';



// Copilot 会话状态与消息持久化。

function normalizeChatMessageRole(value: unknown): ProjectCopilotMessage['role'] {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'assistant') return 'assistant';
  if (token === 'system') return 'system';
  return 'user';
}

function isMissingRelationError(error: unknown, relationName: string): boolean {
  const message = error instanceof Error ? error.message : String(error || '');
  return message.includes('42P01') || message.includes(`relation "public.${relationName}" does not exist`);
}

// Fire-and-forget cache writes, logged so a broken cache stays visible.
function trackCacheWrite(promise: Promise<unknown>): void {
  promise.catch((error) => {
    console.error('[copilot] transcript cache write failed', error);
  });
}

function copilotCacheKeyFor(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
  userId?: string | null;
  conversationScope?: string | null;
}): string {
  return copilotMessageCacheKey({
    contextType: normalizeCopilotContextType(input.contextType),
    projectId: input.projectId,
    projectTaskId: input.projectTaskId,
    userId: input.userId,
    conversationScope: input.conversationScope
  });
}

function copilotStateLocalStorageKey(userId: string, stateKey: string): string {
  return [
    'vbio:project-copilot-state:v1',
    String(userId || 'anonymous').trim().toLowerCase() || 'anonymous',
    String(stateKey || 'default').trim() || 'default'
  ].join(':');
}

function normalizeCopilotContextType(value: unknown): ProjectCopilotMessage['context_type'] {
  const token = String(value || '').trim().toLowerCase();
  if (token === 'project_list') return 'project_list';
  if (token === 'task_detail') return 'task_detail';
  return 'task_list';
}

function normalizeProjectCopilotState(row: Partial<ProjectCopilotState>): ProjectCopilotState {
  return {
    id: String(row.id || ''),
    user_id: String(row.user_id || ''),
    state_key: String(row.state_key || ''),
    data: asObjectRecord(row.data),
    created_at: String(row.created_at || ''),
    updated_at: String(row.updated_at || '')
  };
}

function normalizeProjectCopilotMessage(row: Partial<ProjectCopilotMessage>): ProjectCopilotMessage {
  return {
    id: String(row.id || ''),
    context_type: normalizeCopilotContextType(row.context_type),
    project_id: row.project_id ? String(row.project_id) : null,
    project_task_id: row.project_task_id ? String(row.project_task_id) : null,
    user_id: row.user_id ? String(row.user_id) : null,
    role: normalizeChatMessageRole(row.role),
    content: String(row.content || ''),
    metadata: asObjectRecord(row.metadata),
    created_at: String(row.created_at || ''),
    updated_at: String(row.updated_at || ''),
    username: String(row.username || '').trim(),
    user_name: String(row.user_name || '').trim()
  };
}

export async function getProjectCopilotState(userId: string, stateKey: string): Promise<Record<string, unknown> | null> {
  const normalizedUserId = String(userId || '').trim();
  const normalizedStateKey = String(stateKey || '').trim();
  if (!normalizedUserId || !normalizedStateKey) return null;
  try {
    const rows = await request<ProjectCopilotState[]>('/project_copilot_states', undefined, {
      select: '*',
      user_id: `eq.${normalizedUserId}`,
      state_key: `eq.${normalizedStateKey}`,
      limit: '1'
    });
    const row = rows[0] ? normalizeProjectCopilotState(rows[0]) : null;
    if (row) {
      if (typeof window !== 'undefined') {
        window.localStorage.setItem(copilotStateLocalStorageKey(normalizedUserId, normalizedStateKey), JSON.stringify(row.data));
      }
      return row.data;
    }
    return null;
  } catch (error) {
    if (!isMissingRelationError(error, 'project_copilot_states')) throw error;
    if (typeof window === 'undefined') return null;
    try {
      const parsed = JSON.parse(window.localStorage.getItem(copilotStateLocalStorageKey(normalizedUserId, normalizedStateKey)) || 'null');
      return asObjectRecord(parsed);
    } catch {
      return null;
    }
  }
}

export async function upsertProjectCopilotState(userId: string, stateKey: string, data: Record<string, unknown>): Promise<void> {
  const normalizedUserId = String(userId || '').trim();
  const normalizedStateKey = String(stateKey || '').trim();
  if (!normalizedUserId || !normalizedStateKey) return;
  const compactData = asObjectRecord(data);
  if (typeof window !== 'undefined') {
    window.localStorage.setItem(copilotStateLocalStorageKey(normalizedUserId, normalizedStateKey), JSON.stringify(compactData));
  }
  try {
    await request<ProjectCopilotState[]>(
      '/project_copilot_states',
      {
        method: 'POST',
        headers: { Prefer: 'resolution=merge-duplicates,return=minimal' },
        body: JSON.stringify({
          user_id: normalizedUserId,
          state_key: normalizedStateKey,
          data: compactData
        })
      },
      { on_conflict: 'user_id,state_key' }
    );
  } catch (error) {
    if (!isMissingRelationError(error, 'project_copilot_states')) throw error;
  }
}

export async function deleteProjectCopilotState(userId: string, stateKey: string): Promise<void> {
  const normalizedUserId = String(userId || '').trim();
  const normalizedStateKey = String(stateKey || '').trim();
  if (!normalizedUserId || !normalizedStateKey) return;
  if (typeof window !== 'undefined') {
    window.localStorage.removeItem(copilotStateLocalStorageKey(normalizedUserId, normalizedStateKey));
  }
  try {
    await request<unknown>('/project_copilot_states', { method: 'DELETE' }, {
      user_id: `eq.${normalizedUserId}`,
      state_key: `eq.${normalizedStateKey}`
    });
  } catch (error) {
    if (!isMissingRelationError(error, 'project_copilot_states')) throw error;
  }
}

// planner_trace is excluded here (the heaviest field; fetched per message on first expand
// via fetchProjectCopilotMessageTrace) so the list stays one round trip.
const COPILOT_MESSAGE_LIST_SELECT = [
  'id',
  'context_type',
  'project_id',
  'project_task_id',
  'user_id',
  'role',
  'content',
  'created_at',
  'updated_at',
  'meta_session_id:metadata->>session_id',
  'meta_owner_user_id:metadata->>owner_user_id',
  'meta_conversation_scope:metadata->>conversation_scope',
  'meta_source_context_type:metadata->>source_context_type',
  'meta_source_project_id:metadata->>source_project_id',
  'meta_source_project_task_id:metadata->>source_project_task_id',
  'meta_planner_state:metadata->>planner_state',
  'meta_plan_id:metadata->>plan_id',
  'meta_planner_questions:metadata->planner_questions',
  'meta_planner_observations:metadata->planner_observations',
  'meta_candidate_plan_actions:metadata->candidate_plan_actions',
  'meta_action_resolutions:metadata->action_resolutions',
  'meta_attachments:metadata->attachments'
].join(',');

// Rebuild metadata from projected columns; undefined planner_trace means lazily loadable.
function rebuildCopilotMessageMetadata(row: Partial<ProjectCopilotMessage> & Record<string, unknown>): Record<string, unknown> {
  const metadata: Record<string, unknown> = {};
  const pairs: Array<[string, unknown]> = [
    ['session_id', row.meta_session_id],
    ['owner_user_id', row.meta_owner_user_id],
    ['conversation_scope', row.meta_conversation_scope],
    ['source_context_type', row.meta_source_context_type],
    ['source_project_id', row.meta_source_project_id],
    ['source_project_task_id', row.meta_source_project_task_id],
    ['planner_state', row.meta_planner_state],
    ['plan_id', row.meta_plan_id],
    ['planner_questions', row.meta_planner_questions],
    ['planner_observations', row.meta_planner_observations],
    ['candidate_plan_actions', row.meta_candidate_plan_actions],
    ['action_resolutions', row.meta_action_resolutions],
    ['attachments', row.meta_attachments]
  ];
  for (const [key, value] of pairs) {
    if (value !== null && value !== undefined) metadata[key] = value;
  }
  return metadata;
}

export async function fetchProjectCopilotMessageTrace(messageId: string): Promise<unknown> {
  const id = String(messageId || '').trim();
  if (!id) return null;
  const rows = await request<Array<{ planner_trace?: unknown }>>('/project_copilot_messages', undefined, {
    select: 'planner_trace:metadata->planner_trace',
    id: `eq.${id}`,
    limit: '1'
  });
  const trace = rows[0]?.planner_trace;
  return trace && typeof trace === 'object' ? trace : [];
}

export async function listProjectCopilotMessages(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
  userId?: string | null;
  conversationScope?: string | null;
}): Promise<ProjectCopilotMessage[]> {
  const contextType = normalizeCopilotContextType(input.contextType);
  const conversationScope = String(input.conversationScope || '').trim();
  const normalizedUserId = String(input.userId || '').trim();
  if (!normalizedUserId) return [];
  const query: Record<string, string> = {
    select: COPILOT_MESSAGE_LIST_SELECT,
    context_type: `eq.${contextType}`,
    'metadata->>owner_user_id': `eq.${normalizedUserId}`,
    order: 'created_at.desc',
    limit: '200'
  };
  const projectId = String(input.projectId || '').trim();
  const projectTaskId = String(input.projectTaskId || '').trim();
  query.project_id = projectId ? `eq.${projectId}` : 'is.null';
  query.project_task_id = projectTaskId ? `eq.${projectTaskId}` : 'is.null';
  if (conversationScope) {
    query['metadata->>conversation_scope'] = `eq.${conversationScope}`;
  }
  const rows = await request<Array<Partial<ProjectCopilotMessage>>>('/project_copilot_messages', undefined, query);
  const ownedRows = rows.filter((row) => {
    const projected = row as Partial<ProjectCopilotMessage> & Record<string, unknown>;
    const ownerFromProjection = String(projected.meta_owner_user_id ?? asObjectRecord(row.metadata).owner_user_id ?? '').trim();
    return String(row.user_id || '') === normalizedUserId || ownerFromProjection === normalizedUserId;
  });
  const userIds = Array.from(new Set(ownedRows.map((row) => String(row.user_id || '').trim()).filter(Boolean)));
  const users = await listUsersByIds(userIds);
  const userById = new Map(users.map((user) => [user.id, user] as const));
  const normalizedRows = ownedRows.map((row) => {
    const user = row.user_id ? userById.get(String(row.user_id)) : null;
    const projected = row as Partial<ProjectCopilotMessage> & Record<string, unknown>;
    return normalizeProjectCopilotMessage({
      ...row,
      metadata: row.metadata ?? rebuildCopilotMessageMetadata(projected),
      username: user?.username || '',
      user_name: user?.name || ''
    });
  }).sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  trackCacheWrite(writeCopilotMessageCache(
    copilotCacheKeyFor({ contextType, projectId, projectTaskId, userId: normalizedUserId, conversationScope }),
    normalizedRows
  ));
  return normalizedRows;
}

export async function readCachedProjectCopilotMessages(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
  userId?: string | null;
  conversationScope?: string | null;
}): Promise<ProjectCopilotMessage[]> {
  const contextType = normalizeCopilotContextType(input.contextType);
  const projectId = String(input.projectId || '').trim();
  const projectTaskId = String(input.projectTaskId || '').trim();
  const userId = String(input.userId || '').trim();
  const conversationScope = String(input.conversationScope || '').trim();
  if (!userId) return [];
  return (await readCachedCopilotMessages(copilotCacheKeyFor({ contextType, projectId, projectTaskId, userId, conversationScope })))
    .map(normalizeProjectCopilotMessage)
    .filter((row) => row.user_id === userId || String(row.metadata?.owner_user_id || '') === userId);
}

export async function insertProjectCopilotMessage(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
  userId?: string | null;
  conversationScope?: string | null;
  role?: ProjectCopilotMessage['role'];
  content: string;
  metadata?: Record<string, unknown>;
}): Promise<ProjectCopilotMessage> {
  const content = String(input.content || '').trim();
  if (!content) throw new Error('Message content is required.');
  const contextType = normalizeCopilotContextType(input.contextType);
  const conversationScope = String(input.conversationScope || '').trim();
  const ownerUserId = String(input.userId || input.metadata?.owner_user_id || '').trim();
  const metadata = {
    ...(input.metadata || {}),
    ...(conversationScope ? { conversation_scope: conversationScope } : {}),
    ...(ownerUserId ? { owner_user_id: ownerUserId } : {})
  };
  const rows = await request<ProjectCopilotMessage[]>(
    '/project_copilot_messages',
    {
      method: 'POST',
      headers: { Prefer: 'return=representation' },
      body: JSON.stringify({
        context_type: contextType,
        project_id: input.projectId || null,
        project_task_id: input.projectTaskId || null,
        user_id: input.userId || null,
        role: normalizeChatMessageRole(input.role),
        content,
        metadata
      })
    },
    { select: '*' }
  );
  // A 2xx with an empty body is a hard error; an id-less row breaks React keys and deletion.
  const returned = rows.length > 0 ? rows[0] : null;
  if (!returned || !String((returned as unknown as Record<string, unknown>).id || '').trim()) {
    throw new Error('project_copilot_messages insert returned no row');
  }
  const row = normalizeProjectCopilotMessage(returned);
  trackCacheWrite(appendCopilotMessageCache(
    copilotCacheKeyFor({
      contextType,
      projectId: input.projectId || null,
      projectTaskId: input.projectTaskId || null,
      userId: ownerUserId,
      conversationScope
    }),
    row
  ));
  return row;
}

export async function deleteProjectCopilotMessagesBySession(input: {
  contextType: ProjectCopilotMessage['context_type'];
  projectId?: string | null;
  projectTaskId?: string | null;
  sessionId: string;
  userId?: string | null;
  messageIds?: string[];
  conversationScope?: string | null;
}): Promise<void> {
  const sessionId = String(input.sessionId || '').trim();
  if (!sessionId) return;
  const contextType = normalizeCopilotContextType(input.contextType);
  const projectId = String(input.projectId || '').trim();
  const projectTaskId = String(input.projectTaskId || '').trim();
  const userId = String(input.userId || '').trim();
  const conversationScope = String(input.conversationScope || '').trim();
  const messageIds = Array.from(
    new Set((input.messageIds || []).map((value) => String(value || '').trim()).filter(Boolean))
  );
  if (messageIds.length > 0) {
    await request<unknown>('/project_copilot_messages', { method: 'DELETE' }, { id: buildInFilter(messageIds) });
  }
  const query: Record<string, string> = {
    context_type: `eq.${contextType}`,
    project_id: projectId ? `eq.${projectId}` : 'is.null',
    project_task_id: projectTaskId ? `eq.${projectTaskId}` : 'is.null',
    'metadata->>session_id': `eq.${sessionId}`,
    'metadata->>owner_user_id': `eq.${userId}`
  };
  if (conversationScope) {
    query['metadata->>conversation_scope'] = `eq.${conversationScope}`;
  }
  await request<unknown>('/project_copilot_messages', { method: 'DELETE' }, query);
  if (userId) {
    await request<unknown>('/project_copilot_messages', { method: 'DELETE' }, {
      context_type: `eq.${contextType}`,
      project_id: projectId ? `eq.${projectId}` : 'is.null',
      project_task_id: projectTaskId ? `eq.${projectTaskId}` : 'is.null',
      'metadata->>session_id': `eq.${sessionId}`,
      ...(conversationScope ? { 'metadata->>conversation_scope': `eq.${conversationScope}` } : {}),
      user_id: `eq.${userId}`
    });
  }
  trackCacheWrite(filterCopilotMessageCache(
    copilotCacheKeyFor({ contextType, projectId, projectTaskId, userId, conversationScope }),
    (row) => String(row.metadata?.session_id || 'default') !== sessionId || String(row.metadata?.owner_user_id || row.user_id || '') !== userId
  ));
}

