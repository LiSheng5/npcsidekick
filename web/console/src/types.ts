// 数据模型 —— 与后端 API 一一对应。
// 铁律: UI 不假设任何游戏内容(关系类型/状态/动作一律当字符串渲染)。

export interface Persona {
  id: string
  name?: string
  identity?: string
  personality?: string
  speech_style?: string
  taboos?: string[]
  desires?: Record<string, number>
  goals?: Record<string, { progress?: number; target?: number } | number>
  rules?: { replies?: Record<string, string>; fallback?: string }
  routine?: RoutineItem[]
  system_prompt_override?: string
  context_extra?: string
  voice_samples?: string[]
  // 可选: 制作者手填的关系(非 schema 必需字段)
  relations?: { who?: string; how?: string; score?: string | number }[]
  [key: string]: unknown
}

export interface RoutineItem {
  action: string
  resource?: string
  count?: number
  ticks?: number
  weight?: number
  [key: string]: unknown
}

/** /api/npcs 列表项(id/name 为老字段, 其余为 Console 扩展) */
export interface NpcBrief {
  id: string
  name: string
  identity?: string
  state?: string
  activity?: string
  position?: string
  stamina?: number
  memory_count?: number
  has_memory_card?: boolean
  ephemeral?: boolean
  use_llm?: boolean
}

export interface MemoryEntry {
  id: string
  content: string
  importance: number
  category: string
  created_at: number
  mtype?: string
  count?: number
}

export interface GraphNode {
  id: string
  type: string
  name: string
  state?: string
  metadata?: Record<string, unknown> & { synthetic?: boolean }
}

export interface GraphEdge {
  source: string
  target: string
  type: string
  weight?: number | string | null
  metadata?: Record<string, unknown>
}

export interface RelationshipGraph {
  source: 'runtime' | 'persona' | 'none'
  nodes: GraphNode[]
  edges: GraphEdge[]
  note?: string
}

/** GET /api/actions —— 动作名建议（**不是白名单**）。 */
export interface ActionsPayload {
  runtime: string[]
  observed: string[]
  note?: string
}

export interface ProviderView {
  id: string
  name: string
  base_url: string
  model: string
  configured: boolean
  masked_key: string
  active: boolean
}

export interface ProvidersPayload {
  providers: ProviderView[]
  active: string | null
  encrypted: boolean
  env: {
    model: string
    review_model: string
    base_url: string
    configured: boolean
    masked_key: string
  }
}
