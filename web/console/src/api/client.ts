import type {
  ActionsPayload,
  MemoryEntry,
  MemoryListPayload,
  MemoryWriteResult,
  NpcBrief,
  Persona,
  ProvidersPayload,
  RelationshipGraph,
} from '../types'

// Console 只跟一个后端说话: NPCSidekick Runtime。
// dev: vite 把 /api 代理到 127.0.0.1:8765；prod: 产物就挂在 Runtime 的 /console/ 下(同源)。

export class ApiError extends Error {
  status: number
  detail: string
  constructor(status: number, detail: string) {
    super(detail)
    this.status = status
    this.detail = detail
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...(init?.headers || {}) },
  })
  if (!res.ok) {
    let detail = `HTTP ${res.status}`
    try {
      const body = await res.json()
      detail = body?.detail || detail
    } catch {
      /* 非 JSON 错误体 */
    }
    throw new ApiError(res.status, detail)
  }
  return (await res.json()) as T
}

const post = <T>(path: string, body?: unknown) =>
  req<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) })

export const api = {
  npcs: () => req<{ npcs: NpcBrief[] }>('/api/npcs'),

  relationships: () => req<RelationshipGraph>('/api/relationships'),

  /** 动作名建议（routine 编辑器用）。game-agnostic: 值来自 Runtime，UI 不硬编码。 */
  actions: () => req<ActionsPayload>('/api/actions'),

  personas: () => req<{ personas: Persona[] }>('/api/personas'),
  persona: (id: string) => req<{ persona: Persona }>(`/api/personas/${id}`),
  createPersona: (p: Persona) =>
    post<{
      ok: boolean
      npc_id?: string
      hot_reloaded?: boolean
      error?: string
      detail?: string
    }>('/api/personas', p),
  updatePersona: (id: string, p: Persona) =>
    req<{ ok: boolean; persona: Persona }>(`/api/personas/${id}`, {
      method: 'PUT',
      body: JSON.stringify(p),
    }),
  deletePersona: (id: string, dropMemory = false) =>
    req<{ ok: boolean }>(`/api/personas/${id}?drop_memory=${dropMemory}`, {
      method: 'DELETE',
    }),

  /** 记忆列表。query 非空时走加权检索（后端返回召回 top-k，不是全量过滤）。 */
  memory: (id: string, query = '', topK = 20) => {
    const qs = new URLSearchParams()
    if (query.trim()) qs.set('query', query.trim())
    if (topK) qs.set('top_k', String(topK))
    const suffix = qs.toString()
    return req<MemoryListPayload>(`/api/npcs/${id}/memory${suffix ? `?${suffix}` : ''}`)
  },
  addMemory: (id: string, body: Partial<MemoryEntry>) =>
    post<MemoryWriteResult>(`/api/npcs/${id}/memory`, body),
  updateMemory: (id: string, mid: string, body: Partial<MemoryEntry>) =>
    req<{ ok: boolean; entry: MemoryEntry }>(`/api/npcs/${id}/memory/${mid}`, {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  deleteMemory: (id: string, mid: string) =>
    req<{ ok: boolean }>(`/api/npcs/${id}/memory/${mid}`, { method: 'DELETE' }),

  providers: () => req<ProvidersPayload>('/api/settings/providers'),
  upsertProvider: (body: Record<string, unknown>) =>
    post<ProvidersPayload & { ok: boolean }>('/api/settings/providers', body),
  deleteProvider: (id: string) =>
    req<{ ok: boolean }>(`/api/settings/providers/${id}`, { method: 'DELETE' }),
  activateProvider: (id: string) =>
    post<{ ok: boolean }>(`/api/settings/providers/${id}/activate`),
  testLlm: (body: Record<string, unknown>) =>
    post<{ ok: boolean; latency_ms?: number; model?: string; error?: string }>(
      '/api/llm/test',
      body,
    ),
}
