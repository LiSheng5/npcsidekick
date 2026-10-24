/* 失控NPC v4 Console —— 零构建原生 JS。
   styles.css 与本文件同期维护：死样式已按 app.js 实际 class 清零，v4 专属块
   直接写在里面（并非旧版原样复用）；
   面板按 v4 真实数据重设计（无 world/scheduler 这些旧概念；关系网只由
   personas/*.json 的 relations 字段驱动，没有数据就显示空态）。 */
'use strict'

// ── 小工具 ────────────────────────────────────────────────

const $ = (sel, root = document) => root.querySelector(sel)

/** h() 与 svgEl() 共用的属性应用。svg=true 跳过 HTML 专属快捷键
 *  （SVG 的 className 是只读的 SVGAnimatedString，只能走 setAttribute）。 */
function applyAttrs(node, attrs, svg = false) {
  if (!attrs) return
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined || v === false) continue
    // on* 必须走 addEventListener：写成属性只会得到一个永不调用的函数表达式
    if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v)
    else if (!svg && k === 'class') node.className = v
    else if (!svg && k === 'text') node.textContent = v
    else if (!svg && k === 'value') node.value = v
    else if (!svg && k === 'checked') node.checked = Boolean(v)
    else node.setAttribute(k, v)
  }
}

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag)
  applyAttrs(node, attrs)
  for (const kid of kids.flat()) {
    if (kid === null || kid === undefined || kid === false) continue
    node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)))
  }
  return node
}

const fmtClock = (ts) => (ts ? new Date(ts * 1000).toLocaleString('zh-CN', { hour12: false }) : '—')

function fmtAgo(sec) {
  if (sec === null || sec === undefined) return '—'
  if (sec < 60) return `${sec.toFixed(0)}s 前`
  if (sec < 3600) return `${(sec / 60).toFixed(0)}m 前`
  return `${(sec / 3600).toFixed(1)}h 前`
}

// ── API ──────────────────────────────────────────────────

async function api(path, { method = 'GET', body } = {}) {
  const opts = { method, headers: {} }
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json'
    opts.body = JSON.stringify(body)
  }
  const res = await fetch(path, opts)
  const text = await res.text()
  let data = null
  try { data = text ? JSON.parse(text) : null } catch (_) { data = { raw: text } }
  if (!res.ok) {
    const detail = (data && (data.detail || data.error)) || `HTTP ${res.status}`
    const err = new Error(typeof detail === 'string' ? detail : JSON.stringify(detail))
    err.status = res.status
    throw err
  }
  return data
}

/** POST + SSE 逐帧回调（/api/talk 专用）：帧切分/解析集中在这里，
 *  单帧 JSON 坏了跳过该帧不中断整流；非 2xx 抛错（与 api() 同风格）。 */
async function apiSSE(path, body, onFrame) {
  const res = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  if (!res.ok) {
    const t = await res.text()
    throw new Error(`HTTP ${res.status} ${t.slice(0, 200)}`)
  }
  const reader = res.body.getReader()
  const dec = new TextDecoder()
  let buf = ''
  for (;;) {
    const { done, value } = await reader.read()
    if (done) break
    buf += dec.decode(value, { stream: true })
    let idx
    while ((idx = buf.indexOf('\n\n')) >= 0) {
      const raw = buf.slice(0, idx)
      buf = buf.slice(idx + 2)
      for (const line of raw.split('\n')) {
        if (!line.startsWith('data: ')) continue
        let frame = null
        try { frame = JSON.parse(line.slice(6)) } catch (_) { continue }
        onFrame(frame)
      }
    }
  }
}

// ── 全局状态（跨页共享）───────────────────────────────────

// flash = 跨页一次性提示（改完配置重渲染后还能看见结果）
const store = { state: null, personas: [], npcId: null, flash: null }

// 模型与端点由用户自配（AGENT_MODEL / NPC_API_KEY / NPC_BASE_URL）：
// 配齐 → 显示模型名（悬浮看端点）；没配齐 → 显示"未配置"并说清缺哪几项。
function llmSummary(state) {
  const llm = (state && state.llm) || null
  if (llm && llm.ready === false) {
    return {
      ok: false,
      text: '未配置',
      title: `缺 ${(llm.missing || []).join('、')} —— 配齐 AGENT_MODEL / NPC_API_KEY / NPC_BASE_URL 后重启`,
    }
  }
  const model = (llm && llm.model) || (state && state.model) || ''
  if (model) {
    const inferred = llm && llm.source === 'inferred' ? '（端点按模型名推断，建议显式配 NPC_BASE_URL）' : ''
    return { ok: true, text: model, title: (llm && llm.base_url) ? llm.base_url + inferred : '' }
  }
  return { ok: false, text: '（无 key，规则回复）', title: '' }
}

function llmEndpointText(state) {
  const llm = (state && state.llm) || null
  if (!llm) return '—'
  if (!llm.base_url) return `未配置（缺 ${(llm.missing || []).join('、')}）`
  return llm.source === 'inferred'
    ? `${llm.base_url}（按模型名推断，建议显式配 NPC_BASE_URL）`
    : llm.base_url
}

async function refreshGlobals() {
  try { store.state = await api('/api/state') } catch (_) { store.state = null }
  try { store.personas = (await api('/api/personas')).personas || [] } catch (_) { store.personas = [] }
  if (!store.npcId && store.personas.length) store.npcId = store.personas[0].id
  const s = store.state
  const footModel = $('#foot-model')
  if (!s) {
    footModel.textContent = '离线'
  } else {
    const llm = llmSummary(s)
    footModel.textContent = llm.text
    if (llm.title) footModel.title = llm.title
  }
  $('#foot-effort').textContent = s ? (s.reasoning_effort || '默认') : '—'
  const mods = s && s.mods ? Object.keys(s.mods) : []
  const online = mods.filter((m) => s.mods[m].online)
  $('#foot-mod').textContent = mods.length ? `${online.length}/${mods.length} 在线` : '未报到'
}

// ── 通用组件 ──────────────────────────────────────────────

/** 标准内边距的卡片容器（全站同款样板的单一定义点；窄内边距的动作记录卡自带样式）。 */
function card(...kids) {
  return h('div', { class: 'card', style: 'padding:16px 18px' }, ...kids)
}

/** 列表页加载失败的统一占位。 */
const loadFail = (e) => banner(`加载失败：${e.message}`, 'error')

function pageHead(title, desc, ...right) {
  return h('div', { class: 'page-head' },
    h('div', null, h('h1', { text: title }), h('p', { text: desc })),
    h('span', { class: 'spacer' }), ...right)
}

function stateBlock(kind, text) {
  return h('div', { class: kind === 'error' ? 'state error' : 'state' }, h('p', { text }))
}

function banner(text, kind) {
  return h('div', { class: `banner ${kind || 'ok'}`, text })
}

function npcSelect(onChange, value) {
  if (!store.personas.length) {
    return h('div', { class: 'hint', text: '还没有角色卡 —— 去「角色卡」页新建一个 personas/{id}.json' })
  }
  const sel = h('select', { class: 'input narrow' },
    store.personas.map((p) => h('option', {
      value: p.id, text: p.name ? `${p.id} · ${p.name}` : p.id,
    })))
  sel.value = value || store.npcId || store.personas[0].id
  sel.addEventListener('change', () => { store.npcId = sel.value; onChange(sel.value) })
  return sel
}

function impClass(n) { return n >= 8 ? 'imp hi' : n >= 5 ? 'imp mid' : 'imp lo' }

// ── 页面：总览 ────────────────────────────────────────────

async function renderOverview(view) {
  view.replaceChildren(pageHead('总览', '服务器概况、NPC 与记忆/聊天体量',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  view.append(box)
  try {
    const state = store.state                      // route() 已 refreshGlobals() 拉过 /api/state
    if (!state) throw new Error('服务器状态不可用（/api/state 获取失败）')
    const npcs = await api('/api/npcs')
    const mods = Object.entries(state.mods || {})
    const totalMem = npcs.npcs.reduce((a, n) => a + n.memory_entries, 0)
    const totalMsg = npcs.npcs.reduce((a, n) => a + n.chat_messages, 0)
    const llm = llmSummary(state)
    const cards = [
      h('div', { class: 'metrics' },
        h('div', null, '模型', h('b', { text: llm.text, title: llm.title || null })),
        h('div', null, '思考档位', h('b', { text: state.reasoning_effort || '默认' })),
        h('div', null, 'NPC 数', h('b', { text: String(state.npcs) })),
        h('div', null, '记忆条数', h('b', { text: String(totalMem) })),
        h('div', null, '聊天消息', h('b', { text: String(totalMsg) })),
        h('div', null, '心跳超时', h('b', { text: `${state.heartbeat_timeout_s}s` }))),
      card(
        h('div', { class: 'sec-title', text: '运行环境' }),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'store' }), h('span', { class: 'v', text: state.store_dir })),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'personas' }), h('span', { class: 'v', text: state.persona_dir })),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'LLM 端点' }), h('span', { class: 'v', text: llmEndpointText(state) })),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: '已运行' }), h('span', { class: 'v', text: `${state.uptime_s}s` })),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'mod' }),
          h('span', { class: 'v', text: mods.length ? mods.map(([m, v]) => `${m}（${v.online ? '在线' : '离线'}，${v.actions} 动作）`).join('　') : '未报到' }))),
      card(
        h('div', { class: 'sec-title', text: '各 NPC' }),
        npcs.npcs.length
          ? npcs.npcs.map((n) => h('div', { class: 'kv' },
              h('span', { class: 'k', text: n.npc_id }),
              h('span', { class: 'v', text: `记忆 ${n.memory_entries} 条　聊天 ${n.chat_messages} 条　最后活动 ${fmtClock(n.last_activity)}` })))
          : h('div', { class: 'hint', text: '还没有 NPC —— personas/ 下加一个 JSON 就多一个' }))
    ]
    // 没配齐三件套 → 顶部一条红字说清缺什么（/api/talk 此时走角色卡 rules 兜底）
    if (!llm.ok) {
      cards.unshift(banner(
        `未接上大脑：${llm.title || llm.text}。未配齐时 /api/talk 走角色卡 rules 回复、不调用动作。`,
        'error'))
    }
    box.replaceChildren(...cards)
  } catch (e) {
    box.replaceChildren(loadFail(e))
  }
}

// ── 页面：关系网（persona 的 relations 字段驱动，零依赖 SVG）──

const SVG_NS = 'http://www.w3.org/2000/svg'
const NODE_R = 36

function svgEl(tag, attrs, ...kids) {
  const node = document.createElementNS(SVG_NS, tag)
  applyAttrs(node, attrs, { svg: true })
  for (const kid of kids) if (kid) node.append(kid)
  return node
}

/** 分层布局：从度数最高的节点做 BFS 分层，同层成行（一行最多 6 个，超了自动换行）。
 *  位置完全确定、不抖动；节点多了也不重叠，配合滚轮缩放/拖拽平移看。
 *  无关系数据的孤立节点会各自成为一层，不会挤在一起。 */
function layeredLayout(nodes, edges) {
  const adj = {}
  nodes.forEach((n) => { adj[n.id] = [] })
  for (const e of edges) {
    if (e.source !== e.target && adj[e.source] && adj[e.target]) {
      adj[e.source].push(e.target)
      adj[e.target].push(e.source)
    }
  }
  const depth = {}
  // 从度数最高的开始（并列时按 id 定序，保证每次刷新位置一样）
  const seeds = nodes.map((n) => n.id).sort((a, b) => adj[b].length - adj[a].length || a.localeCompare(b))
  for (const seed of seeds) {
    if (depth[seed] !== undefined) continue
    depth[seed] = 0
    const queue = [seed]
    for (let i = 0; i < queue.length; i++) {
      for (const nb of adj[queue[i]]) {
        if (depth[nb] === undefined) { depth[nb] = depth[queue[i]] + 1; queue.push(nb) }
      }
    }
  }

  const COL_W = 168
  const ROW_H = 140
  const PER_ROW = 6
  const layers = new Map()
  for (const n of nodes) {
    const d = depth[n.id] || 0
    if (!layers.has(d)) layers.set(d, [])
    layers.get(d).push(n.id)
  }

  const pos = {}
  let y = 0
  for (const d of [...layers.keys()].sort((a, b) => a - b)) {
    const ids = layers.get(d).sort()
    ids.forEach((id, i) => {
      const row = Math.floor(i / PER_ROW)
      const col = i % PER_ROW
      const inRow = Math.min(PER_ROW, ids.length - row * PER_ROW)
      pos[id] = { x: (col - (inRow - 1) / 2) * COL_W, y: y + row * ROW_H }
    })
    y += Math.ceil(ids.length / PER_ROW) * ROW_H + 46      // 层间距
  }
  return pos
}

const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v))

/** 按节点包围盒收紧视图 —— 否则图会缩在画布中间一小团，标签小到看不清。
 *  pad 要盖住节点半径 + 节点下方的名字。 */
function tightViewBox(nodes, pos, pad = 80) {
  const xs = nodes.map((n) => pos[n.id].x)
  const ys = nodes.map((n) => pos[n.id].y)
  const x = Math.min(...xs) - pad
  const y = Math.min(...ys) - pad
  return {
    x, y,
    w: Math.max(...xs) + pad - x,
    h: Math.max(...ys) + pad - y,
  }
}

async function renderGraph(view) {
  view.replaceChildren(pageHead('关系网', 'personas/*.json 的 relations 字段（制作者手填）—— 没填就显示空态，大脑不编造关系',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'graph-slot' }, stateBlock('', '加载中…'))
  view.append(box)
  let data
  try {
    data = await api('/api/relationships')
  } catch (e) {
    box.replaceChildren(loadFail(e))
    return
  }
  if (!data.nodes.length) {
    box.replaceChildren(h('div', { class: 'state' },
      h('p', { text: '还没有角色卡 —— personas/ 下加一个 JSON 就多一个角色' })))
    return
  }
  drawGraph(box, data)
}

// ── 关系图主体：drawGraph 的三个半边 ──────────────────────
// g = 一次关系图的可变状态袋（视图 / 节点位置 / 选中 / 聚焦 / 搜索词 / 指针拖拽），
// 交互绑定、内容渲染、检查器三个半边只依赖它 —— drawGraph 本体只做装配。

const GRAPH_MIN_SCALE = 0.3
const GRAPH_MAX_SCALE = 4
const ZOOM_STEP = 1.15            // 每次滚轮缩放系数
const DRAG_SLOP_PX = 4            // 小于这个位移算手抖，不算拖拽
const EDGE_LBL_CHAR_W = 7         // 关系标签估宽（每字符 px）
const EDGE_LBL_PAD = 12

/** 搜索命中：名字 / id / 身份任一处包含搜索词（q 已小写）。 */
function graphHit(n, q) {
  return !q || `${n.name} ${n.id} ${n.identity || ''}`.toLowerCase().includes(q)
}

/** 聚焦时只留焦点节点和它的 1-hop 邻居。 */
function graphInFocus(id, focusId, edges) {
  if (!focusId) return true
  if (id === focusId) return true
  return edges.some((e) => (e.source === focusId && e.target === id)
    || (e.target === focusId && e.source === id))
}

function applyGraphView(g) {
  g.svg.setAttribute('viewBox', `${g.view.x} ${g.view.y} ${g.view.w} ${g.view.h}`)
}

/** 屏幕坐标 → 图坐标 + 归一化锚点 + 当前像素缩放比。
 *  必须还原 preserveAspectRatio=meet 留的黑边，否则缩放锚点会漂、拖动会跟手不准。 */
function graphAnchorAt(g, clientX, clientY) {
  const r = g.svg.getBoundingClientRect()
  const s = Math.min(r.width / g.view.w, r.height / g.view.h) || 1
  const nx = clamp((clientX - r.left - (r.width - g.view.w * s) / 2) / (g.view.w * s), 0, 1)
  const ny = clamp((clientY - r.top - (r.height - g.view.h * s) / 2) / (g.view.h * s), 0, 1)
  return { gx: g.view.x + nx * g.view.w, gy: g.view.y + ny * g.view.h, nx, ny, s }
}

function graphZoomAt(g, clientX, clientY, factor) {
  const a = graphAnchorAt(g, clientX, clientY)
  const scale = clamp((g.base.w / g.view.w) * factor, GRAPH_MIN_SCALE, GRAPH_MAX_SCALE)
  const w = g.base.w / scale
  const h = g.base.h / scale
  g.view = { x: a.gx - a.nx * w, y: a.gy - a.ny * h, w, h }
  applyGraphView(g)
}

/** 几何：边的端点随节点坐标实时算，节点一拖线就跟着动。 */
function graphEdgeGeom(g, e) {
  const a = g.pos[e.source]
  const b = g.pos[e.target]
  if (!a || !b) return null
  const dx = b.x - a.x
  const dy = b.y - a.y
  const len = Math.hypot(dx, dy) || 1
  const ux = dx / len
  const uy = dy / len
  const x1 = a.x + ux * NODE_R
  const y1 = a.y + uy * NODE_R
  const x2 = b.x - ux * (NODE_R + 4)
  const y2 = b.y - uy * (NODE_R + 4)
  return { x1, y1, x2, y2, mx: (x1 + x2) / 2, my: (y1 + y2) / 2 }
}

function graphPlaceEdge(g, ref) {
  const geom = graphEdgeGeom(g, ref.e)
  if (!geom) return
  ref.line.setAttribute('x1', geom.x1)
  ref.line.setAttribute('y1', geom.y1)
  ref.line.setAttribute('x2', geom.x2)
  ref.line.setAttribute('y2', geom.y2)
  ref.rect.setAttribute('x', geom.mx - ref.tw / 2)
  ref.rect.setAttribute('y', geom.my - 10)
  ref.text.setAttribute('x', geom.mx)
  ref.text.setAttribute('y', geom.my + 4)
}

function graphPlaceNode(g, id) {
  const el = g.nodeEls.get(id)
  if (el && g.pos[id]) el.setAttribute('transform', `translate(${g.pos[id].x},${g.pos[id].y})`)
}

// ── 头像：读文件 → data URI → POST → 节点改成图片 ──────────
// 上限由 /api/state 下发（后端单源）；拿不到就跳过预检，交给服务端拒绝。

function pickAvatar(g, npcId) {
  const input = h('input', {
    type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif',
    style: 'display:none',
  })
  const cleanup = () => input.remove()
  input.addEventListener('change', () => {
    const file = input.files && input.files[0]
    cleanup()
    if (!file) return
    const cap = store.state && store.state.avatar_max_bytes
    if (cap && file.size > cap) {
      bannerFlash(g.canvas,
        `图片太大（${(file.size / 1048576).toFixed(1)} MB，上限 ${(cap / 1048576).toFixed(0)} MB）`, 'error')
      return
    }
    const reader = new FileReader()
    reader.onload = async () => {
      try {
        const r = await api(`/api/npcs/${encodeURIComponent(npcId)}/avatar`,
          { method: 'POST', body: { image: String(reader.result) } })
        if (g.byId[npcId]) g.byId[npcId].avatar = r.avatar
        renderGraphContent(g)
        bannerFlash(g.canvas, `已设置「${(g.byId[npcId] || {}).name || npcId}」的头像`, 'ok')
      } catch (e) { bannerFlash(g.canvas, e.message, 'error') }
    }
    reader.onerror = () => bannerFlash(g.canvas, '读取图片失败', 'error')
    reader.readAsDataURL(file)
  })
  input.addEventListener('cancel', cleanup)      // 用户取消时 change 不触发
  document.body.append(input)
  input.click()
}

async function removeAvatar(g, npcId) {
  try {
    await api(`/api/npcs/${encodeURIComponent(npcId)}/avatar`, { method: 'DELETE' })
    if (g.byId[npcId]) g.byId[npcId].avatar = null
    renderGraphContent(g)
    bannerFlash(g.canvas, '已移除头像', 'ok')
  } catch (e) { bannerFlash(g.canvas, e.message, 'error') }
}

/** 指针：空白处 = 平移画布；节点上 = 拖节点；双击空白 = 取消聚焦。 */
function bindGraphInteractions(g) {
  const svg = g.svg

  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault()
    graphZoomAt(g, ev.clientX, ev.clientY, ev.deltaY < 0 ? ZOOM_STEP : 1 / ZOOM_STEP)
  }, { passive: false })

  svg.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return
    g.suppressClick = false
    const nodeG = ev.target.closest && ev.target.closest('g.node')
    g.drag = nodeG
      ? { kind: 'node', id: nodeG.dataset.id, x: ev.clientX, y: ev.clientY,
          origin: { ...g.pos[nodeG.dataset.id] }, moved: false }
      : { kind: 'pan', x: ev.clientX, y: ev.clientY, vx: g.view.x, vy: g.view.y, moved: false }
    svg.setPointerCapture(ev.pointerId)
  })

  svg.addEventListener('pointermove', (ev) => {
    const drag = g.drag
    if (!drag) return
    const dx = ev.clientX - drag.x
    const dy = ev.clientY - drag.y
    if (!drag.moved && Math.hypot(dx, dy) < DRAG_SLOP_PX) return        // 手抖不算拖拽
    drag.moved = true
    const s = graphAnchorAt(g, ev.clientX, ev.clientY).s
    if (drag.kind === 'pan') {
      svg.classList.add('panning')
      g.view.x = drag.vx - dx / s
      g.view.y = drag.vy - dy / s
      applyGraphView(g)
      return
    }
    if (!g.pos[drag.id]) return
    g.pos[drag.id] = { x: drag.origin.x + dx / s, y: drag.origin.y + dy / s }
    graphPlaceNode(g, drag.id)
    for (const ref of g.edgeRefs) {
      if (ref.e.source === drag.id || ref.e.target === drag.id) graphPlaceEdge(g, ref)
    }
  })

  const endDrag = (ev) => {
    if (!g.drag) return
    g.suppressClick = g.drag.moved
    g.drag = null
    svg.classList.remove('panning')
    if (ev && svg.hasPointerCapture(ev.pointerId)) svg.releasePointerCapture(ev.pointerId)
  }
  svg.addEventListener('pointerup', endDrag)
  svg.addEventListener('pointercancel', endDrag)

  // 双击节点 = 聚焦（只留它和它的邻居）；双击空白 = 取消聚焦
  svg.addEventListener('dblclick', (ev) => {
    if (ev.target.closest && ev.target.closest('g.node')) return
    if (!g.focusId) return
    g.focusId = null
    renderGraphContent(g)
  })
}

/** 只重建图内容，**不动 view** —— 选中节点/搜索/聚焦时不该把缩放平移重置掉。 */
function renderGraphContent(g) {
  const { data } = g
  g.nodeEls = new Map()
  g.edgeRefs = []
  const q = g.query.trim().toLowerCase()
  const qRaw = g.query.trim()

  const defs = svgEl('defs')
  defs.append(
    svgEl('marker', {
      id: 'rel-arrow', viewBox: '0 0 10 10', refX: '9', refY: '5',
      markerWidth: '6', markerHeight: '6', orient: 'auto-start-reverse',
    }, svgEl('path', { d: 'M 0 0 L 10 5 L 0 10 z' })),
    svgEl('clipPath', { id: 'avatar-clip' }, svgEl('circle', { r: NODE_R })),
  )
  const parts = [defs]

  // 先画边（压在节点下面）
  for (const e of data.edges) {
    if (!graphEdgeGeom(g, e)) continue
    const queryDim = Boolean(qRaw) && !(graphHit(g.byId[e.source] || {}, q) && graphHit(g.byId[e.target] || {}, q))
    const focusDim = Boolean(g.focusId) && e.source !== g.focusId && e.target !== g.focusId
    const grp = svgEl('g', { class: `edge${queryDim || focusDim ? ' dimmed' : ''}` })
    const line = svgEl('line', { 'marker-end': 'url(#rel-arrow)' })
    const label = e.weight === null || e.weight === undefined
      ? e.type : `${e.type} ${e.weight}`
    const tw = label.length * EDGE_LBL_CHAR_W + EDGE_LBL_PAD
    const rect = svgEl('rect', { class: 'lbl-bg', width: tw, height: 20, rx: 6 })
    const text = svgEl('text', { class: 'lbl' })
    text.textContent = label
    grp.append(line, rect, text)
    const ref = { e, line, rect, text, tw }
    graphPlaceEdge(g, ref)
    g.edgeRefs.push(ref)
    parts.push(grp)
  }

  // 再画节点
  for (const n of data.nodes) {
    const dim = !graphHit(n, q) || !graphInFocus(n.id, g.focusId, data.edges)
    const grp = svgEl('g', {
      class: `node${n.synthetic ? ' synthetic' : ''}${dim ? ' dimmed' : ''}`
        + `${n.id === g.selected ? ' on' : ''}${n.id === g.focusId ? ' focus' : ''}`,
      'data-id': n.id,
      onclick: () => {
        if (g.suppressClick) { g.suppressClick = false; return }   // 刚才是拖拽，不是点击
        g.selected = n.id === g.selected ? null : n.id
        renderGraphContent(g)
      },
      ondblclick: () => {
        g.selected = n.id
        g.focusId = g.focusId === n.id ? null : n.id
        renderGraphContent(g)
      },
    })
    const tip = svgEl('title')
    tip.textContent = `${n.name}（${n.id}）${n.identity ? ' · ' + n.identity : ''}`
    grp.append(tip, svgEl('circle', { r: NODE_R }))

    if (n.avatar) {
      grp.append(svgEl('image', {
        class: 'face', href: n.avatar,
        x: -NODE_R, y: -NODE_R, width: NODE_R * 2, height: NODE_R * 2,
        preserveAspectRatio: 'xMidYMid slice', 'clip-path': 'url(#avatar-clip)',
      }))
    } else {
      // 没头像 → 圆里一个「+」+ 一行小字，点开文件选择
      const plus = svgEl('text', { class: 'plus', y: -5 })
      plus.textContent = '+'
      const hint = svgEl('text', { class: 'add-hint', y: 15 })
      hint.textContent = '点击添加图片'
      grp.append(svgEl('g', {
        class: 'add-avatar',
        onclick: (ev) => {
          ev.stopPropagation()
          if (g.suppressClick) { g.suppressClick = false; return }
          pickAvatar(g, n.id)
        },
      }, plus, hint))
    }

    const nm = svgEl('text', { class: 'nm', y: NODE_R + 17 })
    nm.textContent = n.name || n.id
    grp.append(nm)
    g.nodeEls.set(n.id, grp)
    graphPlaceNode(g, n.id)          // 必须写入初始位置，否则节点全叠在原点
    parts.push(grp)
  }

  g.svg.replaceChildren(...parts)
  g.inspector.replaceChildren(...graphInspectorKids(g).filter(Boolean))
}

/** 右侧检查器的内容：全图概况 / 选中节点详情两套视图。 */
function graphInspectorKids(g) {
  const { data, byId } = g
  const nameOf = (id) => (byId[id] || {}).name || id
  if (!g.selected) {
    const kids = [
      h('h2', { text: '关系网' }),
      h('p', { class: 'sub', text: `${data.nodes.length} 个节点　${data.edges.length} 条关系` }),
      h('div', { class: 'sec' },
        h('div', { class: 'sec-title', text: '数据来源' }),
        h('p', { class: 'sub', text: data.source === 'persona'
          ? '角色卡里的 relations 字段（制作者手填）'
          : '还没有任何关系数据' })),
      h('div', { class: 'sec' },
        h('div', { class: 'sec-title', text: '操作' }),
        h('p', { class: 'sub', text: '滚轮缩放 · 拖拽空白处平移 · 拖节点调位置 · 双击节点聚焦看它的邻居' })),
    ]
    if (g.focusId) {
      kids.push(h('div', { class: 'sec' },
        h('div', { class: 'sec-title', text: '聚焦中' }),
        h('div', { class: 'fld-row' },
          h('span', { class: 'chip on', text: `只显示「${nameOf(g.focusId)}」的邻居` }),
          h('button', {
            class: 'btn tiny', text: '取消聚焦',
            onclick: () => { g.focusId = null; renderGraphContent(g) },
          }))))
    }
    if (data.source === 'none') {
      kids.push(h('div', { class: 'sec' },
        h('div', { class: 'sec-title', text: '怎么加' }),
        h('div', { class: 'quiet-note' },
          h('div', { text: '在 personas/{id}.json 里加可选字段：' }),
          h('div', { class: 'dbg-box', text: '"relations": [\n  { "who": "ali", "how": "师徒", "weight": 0.8 }\n]' }))))
    }
    return kids
  }

  const node = byId[g.selected]
  const out = data.edges.filter((e) => e.source === g.selected)
  const inc = data.edges.filter((e) => e.target === g.selected)
  // 注意：replaceChildren 会把 null 变成字面量 "null" 文本节点 —— 调用方已统一滤掉
  return [
    h('h2', { text: node.name || node.id }),
    h('p', { class: 'sub', text: node.synthetic ? `${node.id}（合成节点：不在角色卡里）` : node.id }),
    node.identity
      ? h('div', { class: 'sec' },
          h('div', { class: 'sec-title', text: '身份' }), h('p', { class: 'sub', text: node.identity }))
      : null,
    h('div', { class: 'sec' },
      h('div', { class: 'sec-title', text: `发出的关系 ${out.length}` }),
      ...(out.length ? out.map((e) => h('div', { class: 'rel-row' },
        h('span', { class: 'arrow', text: '→' }),
        h('span', { class: 'chip', text: e.type }),
        h('span', { text: nameOf(e.target) })))
        : [h('div', { class: 'hint', text: '无' })])),
    h('div', { class: 'sec' },
      h('div', { class: 'sec-title', text: `收到的关系 ${inc.length}` }),
      ...(inc.length ? inc.map((e) => h('div', { class: 'rel-row' },
        h('span', { text: nameOf(e.source) }),
        h('span', { class: 'chip', text: e.type }),
        h('span', { class: 'arrow', text: '→' })))
        : [h('div', { class: 'hint', text: '无' })])),
    h('div', { class: 'inspector-actions' },
      h('button', {
        class: 'btn tiny', text: node.avatar ? '换头像' : '添加头像',
        onclick: () => pickAvatar(g, node.id),
      }),
      node.avatar
        ? h('button', { class: 'btn tiny danger', text: '移除头像', onclick: () => removeAvatar(g, node.id) })
        : null,
      h('button', {
        class: 'btn tiny', text: '只看它的邻居',
        onclick: () => { g.focusId = node.id; renderGraphContent(g) },
      }),
      h('button', {
        class: 'btn tiny', text: '去「角色卡」页编辑',
        onclick: () => { location.hash = '#/personas' },
      })),
  ]
}

function drawGraph(box, data) {
  const basePos = layeredLayout(data.nodes, data.edges)
  const byId = {}
  data.nodes.forEach((n) => { byId[n.id] = n })
  const g = {
    data, byId, basePos,
    base: tightViewBox(data.nodes, basePos),   // 初始视图（「复位」也回到这里）
    pos: {},
    view: null,
    selected: null,
    focusId: null,
    query: '',
    nodeEls: new Map(),
    edgeRefs: [],
    suppressClick: false,
    drag: null,
    svg: svgEl('svg', { class: 'graph-svg', preserveAspectRatio: 'xMidYMid meet' }),
    inspector: h('div', { class: 'inspector' }),
    canvas: h('div', { class: 'graph-canvas' }),
  }
  for (const id of Object.keys(basePos)) g.pos[id] = { ...basePos[id] }
  g.view = { ...g.base }

  const resetAll = () => {
    g.pos = {}
    for (const id of Object.keys(g.basePos)) g.pos[id] = { ...g.basePos[id] }
    g.view = { ...g.base }
    applyGraphView(g)
    renderGraphContent(g)
  }
  const toolbar = h('div', { class: 'toolbar' },
    h('input', {
      class: 'input', placeholder: '搜索角色…', style: 'width:180px',
      oninput: (ev) => { g.query = ev.target.value; renderGraphContent(g) },
    }),
    h('button', { class: 'btn', text: '复位', title: '视图与节点位置都回到初始', onclick: resetAll }),
    h('button', { class: 'btn', text: '刷新', onclick: () => route() }),
    h('span', { class: 'hint', text: '滚轮缩放 · 拖拽空白处平移 · 拖节点调位置 · 双击节点聚焦' }))

  bindGraphInteractions(g)
  g.canvas.replaceChildren(toolbar, g.svg)
  renderGraphContent(g)
  applyGraphView(g)
  box.replaceChildren(h('div', { class: 'graph-wrap' }, g.canvas, g.inspector))
}

// ── 页面：Mod / 能力清单 ──────────────────────────────────

async function renderMods(view) {
  view.replaceChildren(pageHead('Mod / 能力清单', '白名单即唯一闸门：只有这里声明过、且给了执行接口的动作，大脑才可能调用',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  view.append(box)

  const modName = h('input', { class: 'input narrow', placeholder: 'mod 名，如 mygame' })
  const executeUrl = h('input', {
    class: 'input',
    placeholder: 'http://127.0.0.1:8766/execute（执行接口；不填 = 该 mod 没有可执行的动作）',
  })
  const actionsJson = h('textarea', {
    class: 'input', rows: '6',
    placeholder: '[\n  {"name":"cook","desc":"用厨房做饭","params":{"dish":"菜名(字符串)"}}\n]',
  })
  const compose = h('div', { class: 'mem-compose' },
    h('div', { class: 'fld-row' },
      h('label', { class: 'fld' }, h('span', { text: '模拟报到（POST /api/capabilities）' }), modName),
      h('button', {
        class: 'btn primary', text: '报到',
        onclick: async (ev) => {
          try {
            const actions = JSON.parse(actionsJson.value || '[]')
            const body = { mod: modName.value.trim(), actions }
            if (executeUrl.value.trim()) body.execute_url = executeUrl.value.trim()
            const r = await api('/api/capabilities', { method: 'POST', body })
            bannerFlash(box, `已登记 ${r.mod}：${r.actions.join(' / ')}`, 'ok')
            await refreshGlobals()
            renderMods(view)
          } catch (e) { bannerFlash(box, e.message, 'error') }
        },
      })),
    h('label', { class: 'fld' }, h('span', { text: '执行接口地址（可选）' }), executeUrl),
    h('label', { class: 'fld' }, h('span', { text: '动作清单 JSON' }), actionsJson))

  try {
    const data = await api('/api/capabilities')
    const mods = Object.entries(data.mods || {})
    box.replaceChildren(
      compose,
      ...(mods.length ? mods.map(([name, m]) => card(
        h('div', { class: 'sec-title' },
          h('span', { class: 'dot', style: m.online ? 'background:var(--green)' : 'background:var(--text-faint)' }),
          ` ${name}　${m.online ? '在线' : '离线（心跳超时 → 不调任何动作）'}　最后心跳 ${fmtAgo(m.last_seen_s_ago)}`),
        h('div', { class: 'kv' },
          h('span', { class: 'k', text: '执行接口' }),
          h('span', { class: 'v', text: m.execute_url || '（没给执行接口 → 大脑不会调它的任何动作）' })),
        h('div', { class: 'chips' }, (m.actions || []).map((a) => h('span', { class: 'chip', text: a.name }))),
        ...(m.actions || []).map((a) => h('div', { class: 'kv' },
          h('span', { class: 'k', text: a.name }),
          h('span', { class: 'v', text: `${a.desc || ''}${a.params && Object.keys(a.params).length ? `　参数：${Object.entries(a.params).map(([k, v]) => `${k}=${v}`).join('，')}` : ''}` })))))
        : [card(
            h('div', { class: 'hint', text: `还没有 mod 报到（心跳超时 ${data.heartbeat_timeout_s}s）。上面表单可以模拟一次报到。` }))]))
  } catch (e) {
    box.replaceChildren(loadFail(e))
  }
}

const FLASH_MS = 4000        // 一次性提示的停留时长

function bannerFlash(host, text, kind) {
  const b = banner(text, kind)
  host.prepend(b)
  setTimeout(() => b.remove(), FLASH_MS)
}

// ── 页面：记忆卡 ──────────────────────────────────────────

async function renderMemory(view) {
  view.replaceChildren(pageHead('记忆卡', 'store/{id}_memory.json —— 「人工手改」的界面（条目级增删改在这）',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  const picker = h('div', { class: 'filter-row' },
    h('span', { class: 'hint', text: 'NPC：' }),
    npcSelect((id) => loadMemory(box, id), store.npcId))
  view.append(picker, box)
  await loadMemory(box, store.npcId)
}

async function loadMemory(box, npcId) {
  if (!npcId) { box.replaceChildren(banner('先建一张角色卡', 'error')); return }
  try {
    const data = await api(`/api/npcs/${npcId}/memory`)
    const compose = h('div', { class: 'mem-compose' },
      h('div', { class: 'fld-row' },
        h('label', { class: 'fld grow' }, h('span', { text: '内容' }),
          h('input', { class: 'input', id: 'mem-content', placeholder: '要记住的事' })),
        h('label', { class: 'fld' }, h('span', { text: '重要性 0-9' }),
          h('input', { class: 'input x-narrow', id: 'mem-imp', type: 'number', min: '0', max: '9', value: '5' })),
        h('label', { class: 'fld' }, h('span', { text: '分类' }),
          h('input', { class: 'input narrow', id: 'mem-cat', placeholder: 'general' })),
        h('label', { class: 'fld' }, h('span', { text: '钉住' }),
          h('input', { id: 'mem-pin', type: 'checkbox' })),
        h('button', {
          class: 'btn primary', text: '添加',
          onclick: async () => {
            try {
              await api(`/api/npcs/${npcId}/memory`, {
                method: 'POST',
                body: {
                  content: $('#mem-content', box).value,
                  importance: Number($('#mem-imp', box).value || 5),
                  category: $('#mem-cat', box).value || undefined,
                  pinned: $('#mem-pin', box).checked,
                },
              })
              loadMemory(box, npcId)
            } catch (e) { bannerFlash(box, e.message, 'error') }
          },
        })))

    const rows = data.entries.map((e) => memoryRow(box, npcId, e))
    box.replaceChildren(compose,
      h('p', { class: 'hint', text: `${data.count} 条` }),
      ...(rows.length ? rows : [h('div', { class: 'state' }, h('p', { text: '这张记忆卡还是空的' }))]))
  } catch (e) {
    box.replaceChildren(loadFail(e))
  }
}

function memoryRow(box, npcId, entry) {
  const meta = h('div', { class: 'mem-meta' },
    h('span', { class: impClass(entry.importance), text: `imp ${entry.importance}` }),
    h('span', { class: 'chip', text: entry.category }),
    entry.pinned ? h('span', { class: 'chip on', text: '置顶' }) : null,
    h('span', { text: `${entry.age_hours}h 前` }),
    h('span', { text: fmtClock(entry.created_at) }))

  const row = h('div', { class: 'card mem-row' },
    h('code', { class: 'mem-id', text: entry.id.slice(0, 6), title: `完整 id：${entry.id}` }),
    h('div', { class: 'mem-main' },
      h('div', { class: 'mem-content', text: entry.content }),
      meta),
    h('div', { class: 'mem-actions' },
      h('button', {
        class: 'btn tiny', text: '编辑',
        onclick: () => row.replaceWith(editRow(box, npcId, entry)),
      }),
      h('button', {
        class: 'btn tiny danger', text: '删除',
        onclick: async () => {
          if (!confirm('删除这条记忆？（不可撤销）')) return
          try {
            await api(`/api/npcs/${npcId}/memory/${entry.id}`, { method: 'DELETE' })
            loadMemory(box, npcId)
          } catch (e) { bannerFlash(box, e.message, 'error') }
        },
      })))
  return row
}

function editRow(box, npcId, entry) {
  const content = h('textarea', { class: 'input', rows: '3', value: entry.content })
  const imp = h('input', { class: 'input x-narrow', type: 'number', min: '0', max: '9', value: String(entry.importance) })
  const cat = h('input', { class: 'input narrow', value: entry.category })
  const pin = h('input', { type: 'checkbox', checked: entry.pinned })
  return h('div', { class: 'card mem-row editing' },
    h('div', { class: 'mem-main' },
      content,
      h('div', { class: 'fld-row' },
        h('label', { class: 'fld' }, h('span', { text: '重要性' }), imp),
        h('label', { class: 'fld' }, h('span', { text: '分类' }), cat),
        h('label', { class: 'fld' }, h('span', { text: '钉住' }), pin))),
    h('div', { class: 'mem-actions' },
      h('button', {
        class: 'btn tiny primary', text: '保存',
        onclick: async () => {
          try {
            await api(`/api/npcs/${npcId}/memory/${entry.id}`, {
              method: 'PUT',
              body: {
                content: content.value, importance: Number(imp.value),
                category: cat.value, pinned: pin.checked,
              },
            })
            loadMemory(box, npcId)
          } catch (e) { bannerFlash(box, e.message, 'error') }
        },
      }),
      h('button', { class: 'btn tiny', text: '取消', onclick: () => loadMemory(box, npcId) })))
}

// ── 页面：聊天记录 ────────────────────────────────────────

// 聊天记录每页条数（请求 limit 与「加载更早」的步进共用，别再各写一个 200）
const CHAT_PAGE_SIZE = 200

async function renderChat(view) {
  // 压缩阈值由 NPC_SUMMARY_RATIO / NPC_SUMMARY_KEEP_RECENT 决定（/api/state 下发），页面不写死
  const s = store.state || {}
  const threshold = (s.summary_ratio != null && s.summary_keep_recent != null)
    ? `提示用到上下文窗口 ${Math.round(s.summary_ratio * 100)}% 时旧轮次被压成摘要（保留最近 ${s.summary_keep_recent} 轮）`
    : '提示超压缩阈值时旧轮次被压成摘要（阈值见 /api/state）'
  view.replaceChildren(pageHead('聊天记录', `store/{id}_chat.jsonl —— 逐轮追加；${threshold}`,
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  view.append(h('div', { class: 'filter-row' },
    h('span', { class: 'hint', text: 'NPC：' }), npcSelect((id) => loadChat(box, id, 0), store.npcId)), box)
  await loadChat(box, store.npcId, 0)
}

async function loadChat(box, npcId, offset) {
  if (!npcId) { box.replaceChildren(banner('先建一张角色卡', 'error')); return }
  try {
    const d = await api(`/api/npcs/${npcId}/chat?limit=${CHAT_PAGE_SIZE}&offset=${offset}`)
    const pct = Math.min(100, (d.estimated_tokens / d.summary_threshold) * 100)
    const head = card(
      h('div', { class: 'sec-title', text: `摘要状态　${d.estimated_tokens} / ${d.summary_threshold} token（提示超限触发滚动摘要）` }),
      h('div', { class: 'bar' }, h('i', { style: `width:${pct}%` })),
      h('div', { class: 'kv' }, h('span', { class: 'k', text: '已折进摘要' }),
        h('span', { class: 'v', text: `${d.summary.covered} 条消息` })),
      d.summary.summary
        ? h('div', { class: 'dbg-box', text: d.summary.summary })
        : h('div', { class: 'hint', text: '还没有摘要（未到压缩阈值）' }))

    const turns = d.turns.map((m) => h('div', { class: 'turn' },
      h('div', { class: m.role === 'user' ? 'bubble me' : 'bubble npc', text: m.content }),
      h('div', { class: 'turn-meta', text: `${m.role === 'user' ? '玩家' : 'NPC'}　${fmtClock(m.ts)}` })))

    box.replaceChildren(head,
      h('p', { class: 'hint', text: `共 ${d.total} 条消息，本页显示第 ${Math.max(0, d.total - offset - turns.length) + 1}–${d.total - offset} 条` }),
      ...(turns.length ? turns : [h('div', { class: 'state' }, h('p', { text: '还没有聊天记录' }))]),
      offset + d.limit < d.total
        ? h('button', { class: 'btn load-more', text: '加载更早', onclick: () => loadChat(box, npcId, offset + d.limit) })
        : h('div', { class: 'act-end', text: '没有更早的了' }))
  } catch (e) {
    box.replaceChildren(loadFail(e))
  }
}

// ── 页面：试对话（真流式 + 回报闭环）──────────────────────

let playBusy = false

async function renderPlay(view) {
  view.replaceChildren(pageHead('试对话', 'POST /api/talk 真流式（delta / action / audio / done）；动作由大脑同步调 mod 执行、结果当场写卡',
    h('button', {
      class: 'btn', text: '清屏',
      // 只清对话与帧序列 —— 调试栏（NPC 选择 / observation）要留着
      onclick: () => {
        $('#play-turns')?.replaceChildren()
        const f = $('#play-frames')
        if (f) f.textContent = '帧序列会显示在这里'
      },
    })))

  const turns = h('div', { class: 'turns', id: 'play-turns' },
    h('div', { class: 'state' }, h('p', { text: '选好 NPC，发一句话试试。流式台词会实时出现在这里。' })))
  const input = h('textarea', { class: 'input', rows: '2', placeholder: '玩家说的话（Ctrl+Enter 发送）' })
  const sendBtn = h('button', { class: 'btn primary', text: '发送', onclick: () => send() })
  input.addEventListener('keydown', (ev) => {
    if (ev.key === 'Enter' && (ev.ctrlKey || ev.metaKey)) { ev.preventDefault(); send() }
  })

  const obs = h('textarea', { class: 'input', rows: '4', placeholder: '观测载荷（自由格式，可留空）\n{\n  "summary": "玩家在厨房，刚下班",\n  "nearby": ["玩家", "冰箱"]\n}' })
  const ttsReady = Boolean(store.state && store.state.tts)
  const voiceOn = h('input', { type: 'checkbox' })
  if (!ttsReady) voiceOn.disabled = true
  const debug = h('div', { class: 'play-debug', id: 'play-debug' },
    h('div', { class: 'sec-title', text: '调试' }),
    h('div', { class: 'kv' }, h('span', { class: 'k', text: 'NPC' }), h('span', { class: 'v' }, npcSelect(() => { }))),
    h('label', { class: 'fld' }, h('span', { text: 'observation' }), obs),
    h('label', { class: 'fld' },
      h('span', { text: `语音播报${ttsReady ? '' : '（未装 edge-tts）'}` }),
      h('div', { class: 'fld-row' }, voiceOn,
        h('span', { class: 'hint', text: ttsReady
          ? '台词流走完后整段合成；收到 audio 帧自动播放'
          : 'pip install edge-tts 后可用；缺依赖时纯文本保底' }))),
    h('div', { class: 'dbg-box', id: 'play-frames', text: '帧序列会显示在这里' }))

  view.append(h('div', { class: 'play-wrap' },
    h('div', { class: 'play-chat' }, turns, h('div', { class: 'composer' }, input, sendBtn)),
    debug))

  async function send() {
    if (playBusy) return
    const npcId = store.npcId
    const message = input.value.trim()
    if (!npcId) { alert('先在「角色卡」页建一个 NPC'); return }
    if (!message) return
    playBusy = true
    sendBtn.disabled = true
    input.value = ''
    const frames = $('#play-frames', debug)
    const frameLog = []
    let observation
    try { observation = obs.value.trim() ? JSON.parse(obs.value) : undefined }
    catch (e) { observation = obs.value.trim() }        // 非 JSON 就当纯文本观测

    let memBefore = 0
    try { memBefore = (await api(`/api/npcs/${npcId}/memory`)).count } catch (_) { }

    turns.querySelector('.state')?.remove()
    const me = h('div', { class: 'bubble me', text: message })
    const npc = h('div', { class: 'bubble npc', text: '…' })
    const meta = h('div', { class: 'turn-meta', text: '流式中…' })
    const turn = h('div', { class: 'turn' }, me, npc, meta)
    turns.append(turn)
    turns.scrollTop = turns.scrollHeight

    const t0 = performance.now()
    let text = ''
    const actions = []
    let audio = null
    try {
      await apiSSE('/api/talk',
        { npc_id: npcId, message, observation, voice: voiceOn.checked,
          mod: store.state?.mods && Object.keys(store.state.mods)[0] },
        (frame) => {
          frameLog.push(frame.type)
          if (frame.type === 'delta') { text += frame.text; npc.textContent = text; turns.scrollTop = turns.scrollHeight }
          else if (frame.type === 'action') actions.push(frame.action)
          else if (frame.type === 'audio') audio = frame.audio
        })
      meta.textContent = `${((performance.now() - t0) / 1000).toFixed(1)}s`
      if (!text) npc.textContent = '（这一轮没有台词）'
    } catch (e) {
      npc.append(h('span', { class: 'err', text: `\n[出错] ${e.message}` }))
      meta.textContent = '失败'
    } finally {
      playBusy = false
      sendBtn.disabled = false
      if (frames) frames.textContent = `帧序列：${frameLog.join(' → ') || '（无）'}`
    }

    if (audio) {
      const src = `data:audio/mpeg;base64,${audio}`
      const play = () => { new Audio(src).play().catch(() => { }) }
      play()
      turn.append(h('div', { class: 'turn-meta' },
        h('button', { class: 'btn tiny', text: '重播语音', onclick: play })))
      turns.scrollTop = turns.scrollHeight
    } else if (voiceOn.checked && frames) {
      frames.append(h('div', { class: 'hint', text: '这一轮没有 audio 帧（未装 edge-tts / 合成失败 / 超时）—— 纯文本保底' }))
    }

    let memAfter = memBefore
    try { memAfter = (await api(`/api/npcs/${npcId}/memory`)).count } catch (_) { }
    if (frames && memAfter !== memBefore) {
      frames.append(h('div', { class: 'hint', text: `记忆卡：${memBefore} → ${memAfter} 条（remember / 动作写卡生效）` }))
    }

    for (const act of actions) {
      turn.append(actionReportCard(npcId, act))
      turns.scrollTop = turns.scrollHeight
    }
  }
}

/** 试对话页：一条已执行动作的记录卡 + 协议 §4 的「长动作异步回报」按钮（可选通道）。 */
function actionReportCard(npcId, act) {
  const note = h('input', { class: 'input', placeholder: 'note（可选，会进记忆卡）' })
  const report = h('div', { class: 'card', style: 'padding:12px 14px;margin-top:10px' },
    h('div', { class: 'sec-title', text: `本回合已执行的动作记录：${act.name}` }),
    h('div', { class: 'dbg-box', text: JSON.stringify(act.params || {}, null, 2) }),
    h('div', { class: 'hint', text: '记忆卡已由大脑当场写入；下面两个按钮是协议 §4 的「长动作异步回报」通道（可选，只有 mod 回了 status:"running" 时才需要）' }),
    h('div', { class: 'fld-row', style: 'margin-top:8px' }, note,
      h('button', {
        class: 'btn tiny', text: '回报成功',
        onclick: () => reportResult(npcId, act.name, true, note.value, report),
      }),
      h('button', {
        class: 'btn tiny danger', text: '回报失败',
        onclick: () => reportResult(npcId, act.name, false, note.value, report),
      })))
  return report
}

/** 长动作回报（协议 §4）。matched=false 时 entry 为 null（没登记/已过期，不重复写卡）—— 不能直接读 .content。 */
async function reportResult(npcId, actionName, ok, note, host) {
  try {
    const r = await api('/api/action_result', {
      method: 'POST', body: { npc_id: npcId, action: actionName, ok, note },
    })
    host.replaceWith(banner(
      r.entry ? `已写进记忆卡：${r.entry.content}` : '该动作已销账',
      ok ? 'ok' : 'error'))
  } catch (e) { bannerFlash(host, e.message, 'error') }
}

// ── 页面：角色卡 ──────────────────────────────────────────

const NEW_PERSONA_TPL = {
  id: '', name: '', identity: '', personality: '', speech_style: '',
  voice_samples: [], taboos: [], voice: '', relations: [],
  rules: { replies: {}, fallback: '嗯。' },
  entity_synonyms: {},
}

// 角色卡字段的中文说明（同一份内容也在 personas/_角色卡说明.md 里）
const FIELD_HELP = [
  ['id', '编号', '本文件的名字（不含 .json），只能小写英文/数字/下划线，必须与文件名一致'],
  ['name', '显示名', '游戏里怎么称呼它，例如 哥特贝拉'],
  ['identity', '身份', '它是谁、干什么的、处在什么处境（一两句话）'],
  ['personality', '性格', '脾气秉性、在意什么、怕什么'],
  ['speech_style', '说话风格', '长短句、口癖、语气（"短句不废话"比"很酷"管用）'],
  ['voice_samples', '语气锚点', '写 2~3 句它真会说的话，让模型抓住语气'],
  ['taboos', '忌讳', '它绝不会做的事 / 不会说的话'],
  ['rules', '降级兜底', '只在没接上大模型时用：replies = 关键词→回复，fallback = 万能回复'],
  ['entity_synonyms', '同义词族', '同一东西的多种叫法归到一个标准词，供记忆检索（可留空）'],
  ['voice', '音色', 'TTS 音色名（要装 edge-tts 才有声），留空用默认'],
  ['relations', '关系网', '只画控制台关系图，不影响对话；[{who, how, weight}]'],
]

function fieldHelp() {
  return h('details', { class: 'fld-help' },
    h('summary', { text: '这些字段怎么填？（点开看中文说明）' }),
    h('table', { class: 'fld-table' },
      h('thead', {},
        h('tr', {},
          h('th', { text: '字段' }), h('th', { text: '中文名' }), h('th', { text: '怎么填' }))),
      h('tbody', {}, FIELD_HELP.map(([key, cn, desc]) => h('tr', {},
        h('td', {}, h('code', { text: key })),
        h('td', { text: cn }),
        h('td', { text: desc }))))))
}

/** 删卡「保留什么」的说明文案 —— 单源在后端（DELETE 响应的 kept.note 与 /api/state 的
 *  delete_kept_note 都会下发），前端只展示，别再抄一份。 */
const keptNote = () => (store.state && store.state.delete_kept_note)
  || '（数据在 store/ 保留，删卡不连带删记忆）'

async function renderPersonas(view) {
  view.replaceChildren(pageHead('角色卡', 'personas/{id}.json —— 一张卡一个 NPC；改完保存即生效（空白模板：personas/_模板.json）',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  const newId = h('input', { class: 'input narrow', placeholder: '新 NPC id，如 cang' })

  // 删除用的下拉（不复用 npcSelect —— 那个会顺手改掉当前选中的 NPC）
  const delSel = h('select', { class: 'input narrow' },
    store.personas.map((p) => h('option', {
      value: p.id, text: p.name ? `${p.id} · ${p.name}` : p.id,
    })))
  if (store.npcId) delSel.value = store.npcId

  view.append(
    h('div', { class: 'filter-row' },
      h('span', { class: 'hint', text: '打开：' }), npcSelect((id) => openPersona(box, id), store.npcId),
      h('span', { class: 'spacer' }), newId,
      h('button', {
        class: 'btn', text: '新建',
        onclick: async () => {
          const id = newId.value.trim()
          if (!id) return
          await openPersona(box, id, { ...NEW_PERSONA_TPL, id })
        },
      })),
    h('div', { class: 'filter-row' },
      h('span', { class: 'hint', text: '删除：' }), delSel,
      h('button', {
        class: 'btn tiny danger', text: '删除这张角色卡',
        onclick: async () => {
          const id = delSel.value
          if (!id) return
          // 二次确认必须说清"保留什么"（文案来自后端下发）—— 删卡不该连带删记忆
          const ok = confirm(
            `删掉角色卡「${id}」？\n\n${keptNote()}\n\n`
            + `只会删掉 personas/${id}.json，此操作不可撤销。`)
          if (!ok) return
          try {
            const res = await api(`/api/personas/${encodeURIComponent(id)}`, { method: 'DELETE' })
            const kept = res.kept || {}
            store.flash = {
              text: `已删除角色卡 ${id} —— ${kept.note || keptNote()}（保留了 `
                    + `${kept.memory_entries ?? '?'} 条记忆、${kept.chat_messages ?? '?'} 条聊天）`,
              kind: 'ok',
            }
            if (store.npcId === id) store.npcId = null
          } catch (e) {
            store.flash = { text: `删除失败：${e.message}`, kind: 'error' }
          }
          await refreshGlobals()
          route()
        },
      }),
      h('span', { class: 'hint', text: keptNote() })),
    box)
  await openPersona(box, store.npcId)
}

async function openPersona(box, npcId, tpl) {
  if (!npcId) { box.replaceChildren(banner('先建一张角色卡', 'error')); return }
  let persona = tpl
  if (!persona) {
    try { persona = (await api(`/api/personas/${npcId}`)).persona }
    catch (e) {
      // 只有 404（真没这张卡）才当「新建」进编辑器；其余失败（网络/500/坏响应）
      // 一律不进编辑器 —— 否则点保存会把真卡覆盖成空模板
      if (e.status !== 404) {
        box.replaceChildren(banner(`角色卡加载失败：${e.message}（已禁用保存，避免覆盖真卡）`, 'error'))
        return
      }
      persona = { ...NEW_PERSONA_TPL, id: npcId }
    }
  }
  const area = h('textarea', { class: 'input json-area', rows: '22', value: JSON.stringify(persona, null, 2) })
  box.replaceChildren(h('div', { class: 'editor-json' },
    h('div', { class: 'fld-row' },
      h('span', { class: 'hint', text: `personas/${npcId}.json` }),
      h('span', { class: 'spacer' }),
      h('button', {
        class: 'btn primary', text: '保存',
        onclick: async () => {
          try {
            const parsed = JSON.parse(area.value)
            const r = await api(`/api/personas/${npcId}`, { method: 'PUT', body: parsed })
            bannerFlash(box, `已保存 ${r.npc_id}`, 'ok')
            await refreshGlobals()
          } catch (e) { bannerFlash(box, e.message, 'error') }
        },
      })),
    area,
    h('p', { class: 'hint', text: '逐字段中文说明见下表；空白模板在 personas/_模板.json（复制 → 改名成你的 id → 替换【…】文字）。' }),
    fieldHelp()))
}

// ── 路由 ─────────────────────────────────────────────────

// ── 页面：模型（谁来当大脑）───────────────────────────────

// 档位与后端 _EFFORT_VALUES 同口径（minimal/xhigh 按端点方言归一，映射见 README）
const EFFORT_OPTIONS = [
  ['', '自动（不指定，服务端默认）'],
  ['off', '关闭（显式关掉思考）'],
  ['minimal', '开启 · minimal（最低）'],
  ['low', '开启 · 低'],
  ['medium', '开启 · 中'],
  ['high', '开启 · 高'],
  ['xhigh', '开启 · xhigh（超高）'],
  ['max', '开启 · 最高'],
]

const VENDOR_EXAMPLES = [
  ['DeepSeek', 'https://api.deepseek.com'],
  ['OpenAI', 'https://api.openai.com/v1'],
  ['通义千问（百炼）', 'https://dashscope.aliyuncs.com/compatible-mode/v1'],
  ['Kimi / Moonshot', 'https://api.moonshot.cn/v1'],
  ['智谱 GLM', 'https://open.bigmodel.cn/api/paas/v4'],
  ['本地 Ollama', 'http://localhost:11434/v1'],
  ['OpenRouter（含 Claude / Gemini）', 'https://openrouter.ai/api/v1'],
]

const ORIGIN_TEXT = {
  env: '环境变量',
  file: 'store/llm_config.json（上次保存的）',
  runtime: '本次改的（还没重启）',
}

async function renderModel(view) {
  view.replaceChildren(pageHead('模型', '谁来当大脑：模型名 / OpenAI 兼容端点 / 深度思考 —— 改完立即生效，并记进 store/llm_config.json',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  view.append(box)

  let cur
  try {
    cur = await api('/api/llm')
  } catch (e) {
    // 404 = 后端是加这个端点之前的旧进程 —— 直接告诉用户该干嘛，别让他自己猜
    const tip = e.status === 404
      ? '后端还是旧版本（没有 /api/llm）—— 重启服务（uvicorn server:app）后再打开本页'
      : e.message
    box.replaceChildren(banner(`加载失败：${tip}`, 'error'))
    return
  }

  const model = h('input', { class: 'input', placeholder: '模型名，例：deepseek-v4-flash' })
  model.value = cur.model || ''
  const base = h('input', { class: 'input', placeholder: 'OpenAI 兼容端点，例：https://api.deepseek.com' })
  base.value = cur.base_url || ''
  const effort = h('select', { class: 'input narrow' },
    EFFORT_OPTIONS.map(([v, t]) => h('option', { value: v, text: t })))
  effort.value = cur.reasoning_effort || ''
  const noThink = h('input', { type: 'checkbox' })
  noThink.checked = !!cur.thinking_unsupported
  // key 明文落盘：输入不回显（页面只回掩码），留空 = 不改
  const apiKey = h('input', {
    class: 'input', type: 'password', autocomplete: 'off',
    placeholder: cur.api_key && cur.api_key.masked
      ? `已保存 ${cur.api_key.masked} —— 留空不改，填写则替换`
      : '留空 = 用环境变量 / api_key.txt；填写则明文存 store/llm_config.json',
  })

  const reload = async () => { await refreshGlobals(); route() }

  // 三个操作共用一副骨架：执行 → 按结果写 flash → 刷新全局并重渲染（文案各异，形状相同）
  const runOp = async (op, failLabel = '保存失败') => {
    try { store.flash = await op() }
    catch (e) { store.flash = { text: `${failLabel}：${e.message}`, kind: 'error' } }
    await reload()
  }

  const apply = () => runOp(async () => {
    const next = await api('/api/llm', {
      method: 'PUT',
      body: {
        model: model.value, base_url: base.value,
        reasoning_effort: effort.value, thinking_unsupported: noThink.checked,
        ...(apiKey.value ? { api_key: apiKey.value } : {}),   // 留空 = 不动已保存的
      },
    })
    return next.ready
      ? { text: `已切到 ${next.model}（${next.base_url}）—— 下一个请求就用新的`, kind: 'ok' }
      : { text: `已保存，但仍缺 ${(next.missing || []).join('、')} —— /api/talk 走角色卡 rules 回复`, kind: 'error' }
  })

  const reset = () => runOp(async () => {
    await api('/api/llm', { method: 'DELETE' })
    return { text: '已清掉页面设置，回到环境变量那一层', kind: 'ok' }
  }, '清除失败')

  // 只删 key，模型/端点/思考档位留着
  const clearKey = () => runOp(async () => {
    await api('/api/llm', { method: 'PUT', body: { api_key: '' } })
    return { text: '已删掉明文 key，改回环境变量 / api_key.txt 那一把', kind: 'ok' }
  }, '清除失败')

  const kids = []
  if (store.flash) { kids.push(banner(store.flash.text, store.flash.kind)); store.flash = null }
  kids.push(
    modelStatusCard(cur),
    card(
      h('div', { class: 'sec-title', text: '改配置' }),
      h('div', { class: 'filter-row' }, h('span', { class: 'hint', text: '模型名：' }), model),
      h('div', { class: 'filter-row' }, h('span', { class: 'hint', text: '端点：' }), base),
      h('div', { class: 'filter-row' }, h('span', { class: 'hint', text: '深度思考：' }), effort),
      h('label', { class: 'filter-row' }, noThink,
        h('span', { class: 'hint', text: '该模型不认思考参数 —— 勾上后一律不下发（不支持深度思考的模型选这个，效果等于关闭且不会报错）' })),
      h('div', { class: 'filter-row' }, h('span', { class: 'hint', text: 'API Key：' }), apiKey),
      ...(cur.api_key && cur.api_key.from_page
        ? [banner('已保存的 key 是明文存在 store/llm_config.json —— 不要分享、不要提交这个文件（store/ 已在 .gitignore，不会进仓库）', 'error'),
           h('div', { class: 'filter-row' },
             h('button', { class: 'btn tiny danger', text: '删掉明文 key（改用环境变量）', onclick: clearKey }))]
        : [h('p', { class: 'hint', text: '填了就是明文存盘（store/ 不进仓库，但本机能读到）；更安全的是留空、用环境变量 NPC_API_KEY。' })]),
      h('div', { class: 'filter-row' },
        h('button', { class: 'btn primary', text: '保存并应用', onclick: apply }),
        h('button', { class: 'btn', text: '恢复环境变量默认', onclick: reset }))),
    vendorExamplesCard())
  box.replaceChildren(...kids)
}

/** 模型页「当前状态」卡片（GET /api/llm 的展示面）。 */
function modelStatusCard(cur) {
  return card(
    h('div', { class: 'sec-title', text: '当前状态' }),
    h('div', { class: 'kv' }, h('span', { class: 'k', text: '接上了吗' }),
      h('span', { class: 'v', text: cur.ready
        ? `是（端点为${cur.source === 'inferred' ? '按模型名推断值' : '配置值'}）`
        : `否 —— 缺 ${(cur.missing || []).join('、')}` })),
    h('div', { class: 'kv' }, h('span', { class: 'k', text: '值来自' }),
      h('span', { class: 'v', text: ORIGIN_TEXT[cur.origin] || cur.origin })),
    h('div', { class: 'kv' }, h('span', { class: 'k', text: '深度思考' }),
      h('span', { class: 'v', text: cur.reasoning_effort || '不指定' })),
    h('div', { class: 'kv' }, h('span', { class: 'k', text: '实际下发' }),
      h('span', { class: 'v', text: !cur.thinking_effort
        ? '一个思考参数都不发'
        : (['off', 'disabled', 'none'].includes(cur.thinking_effort)
          ? '关闭思考（thinking: disabled）' : `发 ${cur.thinking_effort}`) })),
    h('div', { class: 'kv' }, h('span', { class: 'k', text: 'API Key' }),
      h('span', { class: 'v', text: cur.api_key && cur.api_key.present
        ? `${cur.api_key.masked}（来自 ${cur.api_key.source || '未知'}）`
        : '未配置 —— 在本页上方填写，或放环境变量 NPC_API_KEY / 工程根 api_key.txt' })),
    ...(cur.api_key && cur.api_key.perm_hint
      ? [h('p', { class: 'hint', text: cur.api_key.perm_hint })]
      : []))
}

/** 模型页「常见厂商端点」卡片（静态参考资料）。 */
function vendorExamplesCard() {
  return card(
    h('div', { class: 'sec-title', text: '常见厂商的端点（以厂商最新文档为准）' }),
    ...VENDOR_EXAMPLES.map(([name, url]) => h('div', { class: 'kv' },
      h('span', { class: 'k', text: name }), h('span', { class: 'v', text: url }))),
    h('p', { class: 'hint', text: 'API Key 两种放法：① 在本页上方填写 —— 明文存 store/llm_config.json（store/ 不进仓库，但本机能读到）；② 留空 —— 用环境变量 NPC_API_KEY（旧名 DEEPSEEK_API_KEY / OPENAI_API_KEY / ZHIPU_API_KEY 也认）或工程根 api_key.txt。页面只显示掩码，接口绝不回原文。' }))
}

const PAGES = [
  { key: 'overview', label: '总览', render: renderOverview },
  { key: 'model', label: '模型', render: renderModel },
  { key: 'graph', label: '关系网', render: renderGraph },
  { key: 'mods', label: 'Mod / 能力', render: renderMods },
  { key: 'memory', label: '记忆卡', render: renderMemory },
  { key: 'chat', label: '聊天记录', render: renderChat },
  { key: 'play', label: '试对话', render: renderPlay },
  { key: 'personas', label: '角色卡', render: renderPersonas },
]

function currentKey() {
  const key = (location.hash || '').replace(/^#\/?/, '')
  return PAGES.some((p) => p.key === key) ? key : PAGES[0].key
}

function renderNav() {
  const key = currentKey()
  $('#nav').replaceChildren(...PAGES.map((p) => h('button', {
    class: `nav-item${p.key === key ? ' active' : ''}`,
    onclick: () => { location.hash = `#/${p.key}` },
  }, h('span', { class: 'label', text: p.label }))))
}

async function route() {
  renderNav()
  const page = PAGES.find((p) => p.key === currentKey())
  const view = $('#view')
  view.replaceChildren(stateBlock('', '加载中…'))
  try {
    await refreshGlobals()
    await page.render(view)
  } catch (e) {
    view.replaceChildren(pageHead(page.label, '出错了'), banner(e.message, 'error'))
  }
}

window.addEventListener('hashchange', route)
route()
