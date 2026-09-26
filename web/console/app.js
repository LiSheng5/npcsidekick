/* NPCSidekick v4 Console —— 零构建原生 JS。
   视觉风格照搬 1_NPCSidekick/web/console（styles.css 原样复用）；
   面板按 v4 真实数据重设计（无 world/scheduler 这些旧概念；关系网只由
   personas/*.json 的 relations 字段驱动，没有数据就显示空态）。 */
'use strict'

// ── 小工具 ────────────────────────────────────────────────

const $ = (sel, root = document) => root.querySelector(sel)

function h(tag, attrs, ...kids) {
  const node = document.createElement(tag)
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue
      if (k === 'class') node.className = v
      else if (k === 'text') node.textContent = v
      else if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v)
      else if (k === 'value') node.value = v
      else if (k === 'checked') node.checked = Boolean(v)
      else node.setAttribute(k, v)
    }
  }
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

function strengthBar(entry, threshold) {
  const pct = Math.max(0, Math.min(100, (entry.strength / (threshold * 4)) * 100))
  const cls = entry.exempt ? 'bar exempt' : entry.strength < threshold ? 'bar danger' : 'bar'
  return h('div', { class: cls, title: `强度 ${entry.strength}（< ${threshold} 会被修剪）` },
    h('i', { style: `width:${pct}%` }))
}

// ── 页面：总览 ────────────────────────────────────────────

async function renderOverview(view) {
  view.replaceChildren(pageHead('总览', '服务器概况、NPC 与记忆/聊天体量',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  view.append(box)
  try {
    const [state, npcs] = await Promise.all([api('/api/state'), api('/api/npcs')])
    store.state = state
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
      h('div', { class: 'card', style: 'padding:16px 18px' },
        h('div', { class: 'sec-title', text: '运行环境' }),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'store' }), h('span', { class: 'v', text: state.store_dir })),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'personas' }), h('span', { class: 'v', text: state.persona_dir })),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'LLM 端点' }), h('span', { class: 'v', text: llmEndpointText(state) })),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: '已运行' }), h('span', { class: 'v', text: `${state.uptime_s}s` })),
        h('div', { class: 'kv' }, h('span', { class: 'k', text: 'mod' }),
          h('span', { class: 'v', text: mods.length ? mods.map(([m, v]) => `${m}（${v.online ? '在线' : '离线'}，${v.actions} 动作）`).join('　') : '未报到' }))),
      h('div', { class: 'card', style: 'padding:16px 18px' },
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
        `未接上大脑：${llm.title || llm.text}。未配齐时 /api/talk 走角色卡 rules 回复、不提议动作。`,
        'error'))
    }
    box.replaceChildren(...cards)
  } catch (e) {
    box.replaceChildren(banner(`加载失败：${e.message}`, 'error'))
  }
}

// ── 页面：关系网（persona 的 relations 字段驱动，零依赖 SVG）──

const SVG_NS = 'http://www.w3.org/2000/svg'
const NODE_R = 36

function svgEl(tag, attrs, ...kids) {
  const node = document.createElementNS(SVG_NS, tag)
  if (attrs) {
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined || v === false) continue
      // on* 必须走 addEventListener：写成属性只会得到一个永不调用的函数表达式
      if (k.startsWith('on') && typeof v === 'function') node.addEventListener(k.slice(2), v)
      else node.setAttribute(k, v)
    }
  }
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
    box.replaceChildren(banner(`加载失败：${e.message}`, 'error'))
    return
  }
  if (!data.nodes.length) {
    box.replaceChildren(h('div', { class: 'state' },
      h('p', { text: '还没有角色卡 —— personas/ 下加一个 JSON 就多一个角色' })))
    return
  }
  drawGraph(box, data)
}

function drawGraph(box, data) {
  const basePos = layeredLayout(data.nodes, data.edges)
  const base = tightViewBox(data.nodes, basePos)   // 初始视图（「复位」也回到这里）
  let pos = {}
  for (const id of Object.keys(basePos)) pos[id] = { ...basePos[id] }
  let view = { ...base }
  const byId = {}
  data.nodes.forEach((n) => { byId[n.id] = n })
  let selected = null
  let focusId = null
  let query = ''

  const MIN_SCALE = 0.3
  const MAX_SCALE = 4
  const MAX_AVATAR_BYTES = 2 * 1024 * 1024

  const canvas = h('div', { class: 'graph-canvas' })
  const inspector = h('div', { class: 'inspector' })
  const svg = svgEl('svg', { class: 'graph-svg', preserveAspectRatio: 'xMidYMid meet' })
  const toolbar = h('div', { class: 'toolbar' },
    h('input', {
      class: 'input', placeholder: '搜索角色…', style: 'width:180px',
      oninput: (ev) => { query = ev.target.value; renderContent() },
    }),
    h('button', { class: 'btn', text: '复位', title: '视图与节点位置都回到初始', onclick: resetAll }),
    h('button', { class: 'btn', text: '刷新', onclick: () => route() }),
    h('span', { class: 'hint', text: '滚轮缩放 · 拖拽空白处平移 · 拖节点调位置 · 双击节点聚焦' }))

  let nodeEls = new Map()
  let edgeRefs = []

  // ── 查询 / 聚焦：两套淡化规则合起来用 ──────────────────

  function hit(n) {
    const q = query.trim().toLowerCase()
    return !q || `${n.name} ${n.id} ${n.identity || ''}`.toLowerCase().includes(q)
  }

  /** 聚焦时只留焦点节点和它的 1-hop 邻居。 */
  function inFocus(id) {
    if (!focusId) return true
    if (id === focusId) return true
    return data.edges.some((e) => (e.source === focusId && e.target === id)
      || (e.target === focusId && e.source === id))
  }

  function nodeDim(n) { return !hit(n) || !inFocus(n.id) }

  function edgeDim(e) {
    const q = query.trim()
    const queryDim = Boolean(q) && !(hit(byId[e.source] || {}) && hit(byId[e.target] || {}))
    const focusDim = Boolean(focusId) && e.source !== focusId && e.target !== focusId
    return queryDim || focusDim
  }

  // ── 视图 ─────────────────────────────────────────────

  function applyView() {
    svg.setAttribute('viewBox', `${view.x} ${view.y} ${view.w} ${view.h}`)
  }

  /** 屏幕坐标 → 图坐标 + 归一化锚点 + 当前像素缩放比。
   *  必须还原 preserveAspectRatio=meet 留的黑边，否则缩放锚点会漂、拖动会跟手不准。 */
  function anchorAt(clientX, clientY) {
    const r = svg.getBoundingClientRect()
    const s = Math.min(r.width / view.w, r.height / view.h) || 1
    const nx = clamp((clientX - r.left - (r.width - view.w * s) / 2) / (view.w * s), 0, 1)
    const ny = clamp((clientY - r.top - (r.height - view.h * s) / 2) / (view.h * s), 0, 1)
    return { gx: view.x + nx * view.w, gy: view.y + ny * view.h, nx, ny, s }
  }

  function zoomAt(clientX, clientY, factor) {
    const a = anchorAt(clientX, clientY)
    const scale = clamp((base.w / view.w) * factor, MIN_SCALE, MAX_SCALE)
    const w = base.w / scale
    const h = base.h / scale
    view = { x: a.gx - a.nx * w, y: a.gy - a.ny * h, w, h }
    applyView()
  }

  function resetAll() {
    pos = {}
    for (const id of Object.keys(basePos)) pos[id] = { ...basePos[id] }
    view = { ...base }
    applyView()
    renderContent()
  }

  // ── 几何：边的端点随节点坐标实时算，节点一拖线就跟着动 ──

  function edgeGeom(e) {
    const a = pos[e.source]
    const b = pos[e.target]
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

  function placeEdge(ref) {
    const g = edgeGeom(ref.e)
    if (!g) return
    ref.line.setAttribute('x1', g.x1)
    ref.line.setAttribute('y1', g.y1)
    ref.line.setAttribute('x2', g.x2)
    ref.line.setAttribute('y2', g.y2)
    ref.rect.setAttribute('x', g.mx - ref.tw / 2)
    ref.rect.setAttribute('y', g.my - 10)
    ref.text.setAttribute('x', g.mx)
    ref.text.setAttribute('y', g.my + 4)
  }

  function placeNode(id) {
    const g = nodeEls.get(id)
    if (g && pos[id]) g.setAttribute('transform', `translate(${pos[id].x},${pos[id].y})`)
  }

  // ── 头像：读文件 → data URI → POST → 节点改成图片 ────────

  function pickAvatar(npcId) {
    const input = h('input', {
      type: 'file', accept: 'image/png,image/jpeg,image/webp,image/gif',
      style: 'display:none',
    })
    const cleanup = () => input.remove()
    input.addEventListener('change', () => {
      const file = input.files && input.files[0]
      cleanup()
      if (!file) return
      if (file.size > MAX_AVATAR_BYTES) {
        bannerFlash(canvas, `图片太大（${(file.size / 1048576).toFixed(1)} MB，上限 2 MB）`, 'error')
        return
      }
      const reader = new FileReader()
      reader.onload = async () => {
        try {
          const r = await api(`/api/npcs/${encodeURIComponent(npcId)}/avatar`,
            { method: 'POST', body: { image: String(reader.result) } })
          if (byId[npcId]) byId[npcId].avatar = r.avatar
          renderContent()
          bannerFlash(canvas, `已设置「${(byId[npcId] || {}).name || npcId}」的头像`, 'ok')
        } catch (e) { bannerFlash(canvas, e.message, 'error') }
      }
      reader.onerror = () => bannerFlash(canvas, '读取图片失败', 'error')
      reader.readAsDataURL(file)
    })
    input.addEventListener('cancel', cleanup)      // 用户取消时 change 不触发
    document.body.append(input)
    input.click()
  }

  async function removeAvatar(npcId) {
    try {
      await api(`/api/npcs/${encodeURIComponent(npcId)}/avatar`, { method: 'DELETE' })
      if (byId[npcId]) byId[npcId].avatar = null
      renderContent()
      bannerFlash(canvas, '已移除头像', 'ok')
    } catch (e) { bannerFlash(canvas, e.message, 'error') }
  }

  // ── 指针：空白处 = 平移画布；节点上 = 拖节点 ─────────────

  let drag = null
  let suppressClick = false

  svg.addEventListener('wheel', (ev) => {
    ev.preventDefault()
    zoomAt(ev.clientX, ev.clientY, ev.deltaY < 0 ? 1.15 : 1 / 1.15)
  }, { passive: false })

  svg.addEventListener('pointerdown', (ev) => {
    if (ev.button !== 0) return
    suppressClick = false
    const nodeG = ev.target.closest && ev.target.closest('g.node')
    if (nodeG) {
      const id = nodeG.dataset.id
      drag = { kind: 'node', id, x: ev.clientX, y: ev.clientY, origin: { ...pos[id] }, moved: false }
    } else {
      drag = { kind: 'pan', x: ev.clientX, y: ev.clientY, vx: view.x, vy: view.y, moved: false }
    }
    svg.setPointerCapture(ev.pointerId)
  })

  svg.addEventListener('pointermove', (ev) => {
    if (!drag) return
    const dx = ev.clientX - drag.x
    const dy = ev.clientY - drag.y
    if (!drag.moved && Math.hypot(dx, dy) < 4) return        // 手抖不算拖拽
    drag.moved = true
    const s = anchorAt(ev.clientX, ev.clientY).s
    if (drag.kind === 'pan') {
      svg.classList.add('panning')
      view.x = drag.vx - dx / s
      view.y = drag.vy - dy / s
      applyView()
      return
    }
    if (!pos[drag.id]) return
    pos[drag.id] = { x: drag.origin.x + dx / s, y: drag.origin.y + dy / s }
    placeNode(drag.id)
    for (const ref of edgeRefs) {
      if (ref.e.source === drag.id || ref.e.target === drag.id) placeEdge(ref)
    }
  })

  function endDrag(ev) {
    if (!drag) return
    suppressClick = drag.moved
    drag = null
    svg.classList.remove('panning')
    if (ev && svg.hasPointerCapture(ev.pointerId)) svg.releasePointerCapture(ev.pointerId)
  }
  svg.addEventListener('pointerup', endDrag)
  svg.addEventListener('pointercancel', endDrag)

  // 双击节点 = 聚焦（只留它和它的邻居）；双击空白 = 取消聚焦
  svg.addEventListener('dblclick', (ev) => {
    if (ev.target.closest && ev.target.closest('g.node')) return
    if (!focusId) return
    focusId = null
    renderContent()
  })

  // ── 渲染 ─────────────────────────────────────────────

  /** 只重建图内容，**不动 view** —— 选中节点/搜索/聚焦时不该把缩放平移重置掉。 */
  function renderContent() {
    nodeEls = new Map()
    edgeRefs = []

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
      const geom = edgeGeom(e)
      if (!geom) continue
      const g = svgEl('g', { class: `edge${edgeDim(e) ? ' dimmed' : ''}` })
      const line = svgEl('line', { 'marker-end': 'url(#rel-arrow)' })
      const label = e.weight === null || e.weight === undefined
        ? e.type : `${e.type} ${e.weight}`
      const tw = label.length * 7 + 12
      const rect = svgEl('rect', { class: 'lbl-bg', width: tw, height: 20, rx: 6 })
      const text = svgEl('text', { class: 'lbl' })
      text.textContent = label
      g.append(line, rect, text)
      const ref = { e, line, rect, text, tw }
      placeEdge(ref)
      edgeRefs.push(ref)
      parts.push(g)
    }

    // 再画节点
    for (const n of data.nodes) {
      const g = svgEl('g', {
        class: `node${n.synthetic ? ' synthetic' : ''}${nodeDim(n) ? ' dimmed' : ''}`
          + `${n.id === selected ? ' on' : ''}${n.id === focusId ? ' focus' : ''}`,
        'data-id': n.id,
        onclick: () => {
          if (suppressClick) { suppressClick = false; return }   // 刚才是拖拽，不是点击
          selected = n.id === selected ? null : n.id
          renderContent()
        },
        ondblclick: () => {
          selected = n.id
          focusId = focusId === n.id ? null : n.id
          renderContent()
        },
      })
      const tip = svgEl('title')
      tip.textContent = `${n.name}（${n.id}）${n.identity ? ' · ' + n.identity : ''}`
      g.append(tip, svgEl('circle', { r: NODE_R }))

      if (n.avatar) {
        g.append(svgEl('image', {
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
        g.append(svgEl('g', {
          class: 'add-avatar',
          onclick: (ev) => {
            ev.stopPropagation()
            if (suppressClick) { suppressClick = false; return }
            pickAvatar(n.id)
          },
        }, plus, hint))
      }

      const nm = svgEl('text', { class: 'nm', y: NODE_R + 17 })
      nm.textContent = n.name || n.id
      g.append(nm)
      nodeEls.set(n.id, g)
      placeNode(n.id)          // 必须写入初始位置，否则节点全叠在原点
      parts.push(g)
    }

    svg.replaceChildren(...parts)
    renderInspector()
  }

  function renderInspector() {
    const nameOf = (id) => (byId[id] || {}).name || id
    if (!selected) {
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
      if (focusId) {
        kids.push(h('div', { class: 'sec' },
          h('div', { class: 'sec-title', text: '聚焦中' }),
          h('div', { class: 'fld-row' },
            h('span', { class: 'chip on', text: `只显示「${nameOf(focusId)}」的邻居` }),
            h('button', {
              class: 'btn tiny', text: '取消聚焦',
              onclick: () => { focusId = null; renderContent() },
            }))))
      }
      if (data.source === 'none') {
        kids.push(h('div', { class: 'sec' },
          h('div', { class: 'sec-title', text: '怎么加' }),
          h('div', { class: 'quiet-note' },
            h('div', { text: '在 personas/{id}.json 里加可选字段：' }),
            h('div', { class: 'dbg-box', text: '"relations": [\n  { "who": "ali", "how": "师徒", "weight": 0.8 }\n]' }))))
      }
      inspector.replaceChildren(...kids.filter(Boolean))
      return
    }

    const node = byId[selected]
    const out = data.edges.filter((e) => e.source === selected)
    const inc = data.edges.filter((e) => e.target === selected)
    // 注意：replaceChildren 会把 null 变成字面量 "null" 文本节点，必须先滤掉
    const kids = [
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
          onclick: () => pickAvatar(node.id),
        }),
        node.avatar
          ? h('button', { class: 'btn tiny danger', text: '移除头像', onclick: () => removeAvatar(node.id) })
          : null,
        h('button', {
          class: 'btn tiny', text: '只看它的邻居',
          onclick: () => { focusId = node.id; renderContent() },
        }),
        h('button', {
          class: 'btn tiny', text: '去「角色卡」页编辑',
          onclick: () => { location.hash = '#/personas' },
        })),
    ]
    inspector.replaceChildren(...kids.filter(Boolean))
  }

  canvas.replaceChildren(toolbar, svg)
  renderContent()
  applyView()
  box.replaceChildren(h('div', { class: 'graph-wrap' }, canvas, inspector))
}

// ── 页面：Mod / 能力清单 ──────────────────────────────────

async function renderMods(view) {
  view.replaceChildren(pageHead('Mod / 能力清单', '白名单即唯一闸门：只有这里声明过的动作，大脑才可能提议',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  view.append(box)

  const modName = h('input', { class: 'input narrow', placeholder: 'mod 名，如 sims4' })
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
            const r = await api('/api/capabilities', { method: 'POST', body: { mod: modName.value.trim(), actions } })
            bannerFlash(box, `已登记 ${r.mod}：${r.actions.join(' / ')}`, 'ok')
            await refreshGlobals()
            renderMods(view)
          } catch (e) { bannerFlash(box, e.message, 'error') }
        },
      })),
    h('label', { class: 'fld' }, h('span', { text: '动作清单 JSON' }), actionsJson))

  try {
    const data = await api('/api/capabilities')
    const mods = Object.entries(data.mods || {})
    box.replaceChildren(
      compose,
      ...(mods.length ? mods.map(([name, m]) => h('div', { class: 'card', style: 'padding:16px 18px' },
        h('div', { class: 'sec-title' },
          h('span', { class: 'dot', style: m.online ? 'background:var(--green)' : 'background:var(--text-faint)' }),
          ` ${name}　${m.online ? '在线' : '离线（心跳超时 → 不提议动作）'}　最后心跳 ${fmtAgo(m.last_seen_s_ago)}`),
        h('div', { class: 'chips' }, (m.actions || []).map((a) => h('span', { class: 'chip', text: a.name }))),
        ...(m.actions || []).map((a) => h('div', { class: 'kv' },
          h('span', { class: 'k', text: a.name }),
          h('span', { class: 'v', text: `${a.desc || ''}${a.params && Object.keys(a.params).length ? `　参数：${Object.entries(a.params).map(([k, v]) => `${k}=${v}`).join('，')}` : ''}` })))))
        : [h('div', { class: 'card', style: 'padding:16px 18px' },
            h('div', { class: 'hint', text: `还没有 mod 报到（心跳超时 ${data.heartbeat_timeout_s}s）。上面表单可以模拟一次报到。` }))]))
  } catch (e) {
    box.replaceChildren(banner(`加载失败：${e.message}`, 'error'))
  }
}

function bannerFlash(host, text, kind) {
  const b = banner(text, kind)
  host.prepend(b)
  setTimeout(() => b.remove(), 4000)
}

// ── 页面：记忆卡 ──────────────────────────────────────────

async function renderMemory(view) {
  view.replaceChildren(pageHead('记忆卡', 'store/{id}_memory.json —— 三个写入口之外，这里就是「人工手改」的界面',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  const picker = h('div', { class: 'filter-row' },
    h('span', { class: 'hint', text: 'NPC：' }),
    npcSelect((id) => loadMemory(box, id), store.npcId),
    h('span', { class: 'spacer' }),
    h('button', { class: 'btn', text: '手动修剪', title: '跑一次确定性半衰期修剪（与对话开始时自动跑的同一个函数）',
      onclick: async () => {
        try {
          const r = await api(`/api/npcs/${store.npcId}/prune`, { method: 'POST' })
          bannerFlash(box, `修剪完成：移除 ${r.removed} 条，剩 ${r.remaining} 条`, 'ok')
          loadMemory(box, store.npcId)
        } catch (e) { bannerFlash(box, e.message, 'error') }
      } }))
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

    const rows = data.entries.map((e) => memoryRow(box, npcId, e, data))
    box.replaceChildren(compose,
      h('p', { class: 'hint', text: `${data.count} 条　强度 < ${data.prune_threshold} 会被修剪；importance ≥ ${data.exempt_importance}、钉住、反思/合并产物豁免` }),
      ...(rows.length ? rows : [h('div', { class: 'state' }, h('p', { text: '这张记忆卡还是空的' }))]))
  } catch (e) {
    box.replaceChildren(banner(`加载失败：${e.message}`, 'error'))
  }
}

function memoryRow(box, npcId, entry, data) {
  const meta = h('div', { class: 'mem-meta' },
    h('span', { class: impClass(entry.importance), text: `imp ${entry.importance}` }),
    h('span', { class: 'chip', text: entry.category }),
    entry.pinned ? h('span', { class: 'chip on', text: '钉住' }) : null,
    entry.exempt ? h('span', { class: 'chip', text: '豁免修剪' }) : null,
    h('span', { text: `强度 ${entry.strength}` }),
    h('span', { text: `${entry.age_hours}h 前` }),
    h('span', { text: fmtClock(entry.created_at) }))

  const row = h('div', { class: 'card mem-row' },
    h('div', { class: 'mem-main' },
      h('div', { class: 'mem-content', text: entry.content }),
      strengthBar(entry, data.prune_threshold),
      meta),
    h('div', { class: 'mem-actions' },
      h('button', {
        class: 'btn tiny', text: '编辑',
        onclick: () => row.replaceWith(editRow(box, npcId, entry, data)),
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

function editRow(box, npcId, entry, data) {
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

async function renderChat(view) {
  view.replaceChildren(pageHead('聊天记录', 'store/{id}_chat.jsonl —— 逐轮追加；超阈值后最旧的轮次会被压成摘要',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  view.append(h('div', { class: 'filter-row' },
    h('span', { class: 'hint', text: 'NPC：' }), npcSelect((id) => loadChat(box, id, 0), store.npcId)), box)
  await loadChat(box, store.npcId, 0)
}

async function loadChat(box, npcId, offset) {
  if (!npcId) { box.replaceChildren(banner('先建一张角色卡', 'error')); return }
  try {
    const d = await api(`/api/npcs/${npcId}/chat?limit=200&offset=${offset}`)
    const pct = Math.min(100, (d.estimated_tokens / d.summary_threshold) * 100)
    const head = h('div', { class: 'card', style: 'padding:16px 18px' },
      h('div', { class: 'sec-title', text: `摘要状态　${d.estimated_tokens} / ${d.summary_threshold} token（超阈值触发滚动摘要）` }),
      h('div', { class: 'bar' }, h('i', { style: `width:${pct}%` })),
      h('div', { class: 'kv' }, h('span', { class: 'k', text: '已折进摘要' }),
        h('span', { class: 'v', text: `${d.summary.covered} 条消息` })),
      d.summary.summary
        ? h('div', { class: 'dbg-box', text: d.summary.summary })
        : h('div', { class: 'hint', text: '还没有摘要（未超阈值）' }))

    const turns = d.turns.map((m) => h('div', { class: 'turn' },
      h('div', { class: m.role === 'user' ? 'bubble me' : 'bubble npc', text: m.content }),
      h('div', { class: 'turn-meta', text: `${m.role === 'user' ? '玩家' : 'NPC'}　${fmtClock(m.ts)}` })))

    box.replaceChildren(head,
      h('p', { class: 'hint', text: `共 ${d.total} 条消息，本页显示第 ${Math.max(0, d.total - offset - turns.length) + 1}–${d.total - offset} 条` }),
      ...(turns.length ? turns : [h('div', { class: 'state' }, h('p', { text: '还没有聊天记录' }))]),
      offset + d.limit < d.total
        ? h('button', { class: 'btn load-more', text: '加载更早', onclick: () => loadChat(box, npcId, offset + 200) })
        : h('div', { class: 'act-end', text: '没有更早的了' }))
  } catch (e) {
    box.replaceChildren(banner(`加载失败：${e.message}`, 'error'))
  }
}

// ── 页面：试对话（真流式 + 回报闭环）──────────────────────

let playBusy = false

async function renderPlay(view) {
  view.replaceChildren(pageHead('试对话', 'POST /api/talk 真流式（delta / action / done）；动作只提议，执行由游戏回报',
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
    let action = null
    let audio = null
    try {
      const res = await fetch('/api/talk', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ npc_id: npcId, message, observation, voice: voiceOn.checked, mod: store.state?.mods && Object.keys(store.state.mods)[0] }),
      })
      if (!res.ok) {
        const t = await res.text()
        throw new Error(`HTTP ${res.status} ${t.slice(0, 200)}`)
      }
      const reader = res.body.getReader()
      const dec = new TextDecoder()
      let buf = ''
      for (; ;) {
        const { done, value } = await reader.read()
        if (done) break
        buf += dec.decode(value, { stream: true })
        let idx
        while ((idx = buf.indexOf('\n\n')) >= 0) {
          const raw = buf.slice(0, idx)
          buf = buf.slice(idx + 2)
          for (const line of raw.split('\n')) {
            if (!line.startsWith('data: ')) continue
            const frame = JSON.parse(line.slice(6))
            frameLog.push(frame.type)
            if (frame.type === 'delta') { text += frame.text; npc.textContent = text; turns.scrollTop = turns.scrollHeight }
            else if (frame.type === 'action') action = frame.action
            else if (frame.type === 'audio') audio = frame.audio
          }
        }
      }
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
      frames.append(h('div', { class: 'hint', text: `记忆卡：${memBefore} → ${memAfter} 条（remember 工具生效）` }))
    }

    if (action) {
      const note = h('input', { class: 'input', placeholder: 'note（可选，会进记忆卡）' })
      const report = h('div', { class: 'card', style: 'padding:12px 14px;margin-top:10px' },
        h('div', { class: 'sec-title', text: `动作提议：${action.name}` }),
        h('div', { class: 'dbg-box', text: JSON.stringify(action.params || {}, null, 2) }),
        h('div', { class: 'fld-row', style: 'margin-top:8px' }, note,
          h('button', {
            class: 'btn tiny', text: '回报成功',
            onclick: () => reportResult(npcId, action.name, true, note.value, report),
          }),
          h('button', {
            class: 'btn tiny danger', text: '回报失败',
            onclick: () => reportResult(npcId, action.name, false, note.value, report),
          })))
      turn.append(report)
      turns.scrollTop = turns.scrollHeight
    }
  }

  async function reportResult(npcId, actionName, ok, note, host) {
    try {
      const r = await api('/api/action_result', {
        method: 'POST', body: { npc_id: npcId, action: actionName, ok, note },
      })
      host.replaceWith(banner(`已写进记忆卡：${r.entry.content}`, ok ? 'ok' : 'error'))
    } catch (e) { bannerFlash(host, e.message, 'error') }
  }
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

async function renderPersonas(view) {
  view.replaceChildren(pageHead('角色卡', 'personas/{id}.json —— 一张卡一个 NPC；改完保存即生效（空白模板：personas/_模板.json）',
    h('button', { class: 'btn', text: '刷新', onclick: () => route() })))
  const box = h('div', { class: 'mem-list' }, stateBlock('', '加载中…'))
  const newId = h('input', { class: 'input narrow', placeholder: '新 NPC id，如 cang' })
  view.append(h('div', { class: 'filter-row' },
    h('span', { class: 'hint', text: '打开：' }), npcSelect((id) => openPersona(box, id), store.npcId),
    h('span', { class: 'spacer' }), newId,
    h('button', {
      class: 'btn', text: '新建',
      onclick: async () => {
        const id = newId.value.trim()
        if (!id) return
        await openPersona(box, id, { ...NEW_PERSONA_TPL, id })
      },
    })), box)
  await openPersona(box, store.npcId)
}

async function openPersona(box, npcId, tpl) {
  if (!npcId) { box.replaceChildren(banner('先建一张角色卡', 'error')); return }
  let persona = tpl
  if (!persona) {
    try { persona = (await api(`/api/personas/${npcId}`)).persona }
    catch (e) { persona = { ...NEW_PERSONA_TPL, id: npcId } }
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

const EFFORT_OPTIONS = [
  ['', '自动（不指定，服务端默认）'],
  ['off', '关闭（显式关掉思考）'],
  ['low', '开启 · 低'],
  ['medium', '开启 · 中'],
  ['high', '开启 · 高'],
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
    box.replaceChildren(banner(`加载失败：${e.message}`, 'error'))
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

  const reload = async () => { await refreshGlobals(); route() }

  const apply = async () => {
    try {
      const next = await api('/api/llm', {
        method: 'PUT',
        body: {
          model: model.value, base_url: base.value,
          reasoning_effort: effort.value, thinking_unsupported: noThink.checked,
        },
      })
      store.flash = next.ready
        ? { text: `已切到 ${next.model}（${next.base_url}）—— 下一个请求就用新的`, kind: 'ok' }
        : { text: `已保存，但仍缺 ${(next.missing || []).join('、')} —— /api/talk 走角色卡 rules 回复`, kind: 'error' }
    } catch (e) {
      store.flash = { text: `保存失败：${e.message}`, kind: 'error' }
    }
    await reload()
  }

  const reset = async () => {
    try {
      await api('/api/llm', { method: 'DELETE' })
      store.flash = { text: '已清掉页面设置，回到环境变量那一层', kind: 'ok' }
    } catch (e) {
      store.flash = { text: `清除失败：${e.message}`, kind: 'error' }
    }
    await reload()
  }

  const kids = []
  if (store.flash) { kids.push(banner(store.flash.text, store.flash.kind)); store.flash = null }
  kids.push(
    h('div', { class: 'card', style: 'padding:16px 18px' },
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
        h('span', { class: 'v', text: cur.thinking_effort ? `发 ${cur.thinking_effort}` : '一个思考参数都不发' }))),
    h('div', { class: 'card', style: 'padding:16px 18px' },
      h('div', { class: 'sec-title', text: '改配置' }),
      h('div', { class: 'filter-row' }, h('span', { class: 'hint', text: '模型名：' }), model),
      h('div', { class: 'filter-row' }, h('span', { class: 'hint', text: '端点：' }), base),
      h('div', { class: 'filter-row' }, h('span', { class: 'hint', text: '深度思考：' }), effort),
      h('label', { class: 'filter-row' }, noThink,
        h('span', { class: 'hint', text: '该模型不认思考参数 —— 勾上后一律不下发（不支持深度思考的模型选这个，效果等于关闭且不会报错）' })),
      h('div', { class: 'filter-row' },
        h('button', { class: 'btn primary', text: '保存并应用', onclick: apply }),
        h('button', { class: 'btn', text: '恢复环境变量默认', onclick: reset }))),
    h('div', { class: 'card', style: 'padding:16px 18px' },
      h('div', { class: 'sec-title', text: '常见厂商的端点（以厂商最新文档为准）' }),
      ...VENDOR_EXAMPLES.map(([name, url]) => h('div', { class: 'kv' },
        h('span', { class: 'k', text: name }), h('span', { class: 'v', text: url }))),
      h('p', { class: 'hint', text: 'API Key 不在这里填：放环境变量 NPC_API_KEY（旧名 DEEPSEEK_API_KEY / OPENAI_API_KEY / ZHIPU_API_KEY 也认）或工程根 api_key.txt —— 明文 key 不进页面、也不进 store/llm_config.json。' })))
  box.replaceChildren(...kids)
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
