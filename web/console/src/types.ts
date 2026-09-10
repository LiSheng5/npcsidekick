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

/** 编辑器下拉框的可选值 —— 后端从实际数据观察得出，UI 不硬编码任何分类。 */
export interface MemoryFacets {
  categories: string[]
  mtypes: string[]
}

export interface MemoryListPayload {
  npc_id: string
  entries: MemoryEntry[]
  /** 卡上总条数（检索时 entries 只是召回的 top-k，比 total 少） */
  total: number
  facets: MemoryFacets
}

/** 写入结果三态: 新增 / 折叠进同文条目 / 被安全闸拒收。 */
export interface MemoryWriteResult {
  ok: boolean
  status: 'added' | 'merged' | 'rejected'
  entry: MemoryEntry | null
  total: number
  message?: string
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

/** GET /api/state —— 世界运行时快照。
 *  `panels` 是世界声明"该显示哪些字段"的名单（数据驱动，UI 不假定有哪些面板）。 */
export interface ActorState {
  position?: string
  inventory?: Record<string, number>
  state?: string
  stamina?: number
  activity?: string
}

export interface LiveState {
  world_id?: string
  panels: string[]
  actors: Record<string, ActorState>
  delivered?: Record<string, unknown>
  tick: number
  log_tail?: string[]
  pending_tasks?: unknown[]
}

/** 结构化事件（由 world.log 文本解析而来）。

    ⚠ 事件**没有时间戳字段** —— 只能按到达顺序展示，别假装能排序/显示时间。
    字段随 type 变化；未知字段原样保留，未知 type 走 fallback 渲染（换游戏不崩）。 */
export interface LiveEvent {
  type: string
  npc?: string
  text?: string
  dest?: string
  resource?: string
  product?: string
  to?: string
  status?: string
  desc?: string
  agent?: string
  [key: string]: unknown
}

export interface EventsPayload {
  events: LiveEvent[]
  /** 事件流游标（绝对位置），下次请求的 since */
  log_count: number
  tick: number
}

/** GET /api/stats —— 观测计数（内存态，重启清零）。 */
export interface LiveStats {
  uptime_sec: number
  tick: number
  mode: string
  npcs: number
  sse_clients: number
  talk: {
    total: number
    rules: number
    llm: number
    errors: number
    last_latency_ms?: number
  }
  task?: { total: number }
  [key: string]: unknown
}

/** GET/POST /api/mode —— 全局对话模式（rules ↔ llm），作用于所有 NPC。 */
export interface ModePayload {
  mode: string
  requested: string
}

/** POST /api/talk —— 游戏协议端点（**禁止改动**，Playground 只是消费它）。

    ⚠ 响应里**没有** latency / mode / 记忆上下文字段 —— Debug 面板的延迟由前端实测，
    模式取自 /api/mode，"召回预览"是按这句话检索的近似结果（不是 talk 内部真实召回）。 */
export interface TalkResult {
  reply: string
  thinking_text?: string
  audio?: string
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
