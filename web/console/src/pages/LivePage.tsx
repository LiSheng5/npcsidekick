import { useEffect, useRef, useState } from 'react'
import { api } from '../api/client'
import { stateColor } from '../lib/format'
import type { LiveEvent, LiveState, LiveStats, NpcBrief } from '../types'

/** Live —— 运行时监视器（Step 7）。

  左侧 = 世界快照（/api/state，2s 轮询）；右侧 = 事件流（/api/events/stream，SSE 实时）。

  两个诚实的设计约束（别假装能解决）:
    · 事件**没有时间戳**（后端从 world.log 文本解析而来），只能按到达顺序展示；
    · SSE 断线重连会用同一个 since 游标，因此重连瞬间可能重放几条已显示过的事件 ——
      事件本身没有唯一 id，做内容去重会误杀"连着两次同样的动作"，故不做去重。

  game-agnostic: state / position / activity 都是运行时字符串，照原样渲染；
  显示哪些字段由 /api/state 的 `panels`（世界自己声明）决定，UI 不预设面板。 */
export function LivePage() {
  const [state, setState] = useState<LiveState>()
  const [stats, setStats] = useState<LiveStats>()
  const [names, setNames] = useState<Record<string, string>>({})
  const [events, setEvents] = useState<LiveEvent[]>([])
  const [sse, setSse] = useState<'connecting' | 'live' | 'off'>('off')
  const [paused, setPaused] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()
  const cursor = useRef(0)

  // 角色名（/api/npcs 是唯一带 name 的地方，state 里只有 id）
  useEffect(() => {
    api
      .npcs()
      .then((n) => setNames(Object.fromEntries(n.npcs.map((x: NpcBrief) => [x.id, x.name]))))
      .catch(() => undefined) // 名字拿不到就显示 id，不影响监控
  }, [])

  // ── 世界快照轮询 ────────────────────────────────
  useEffect(() => {
    if (paused) return
    let alive = true
    const poll = async () => {
      try {
        const [s, st] = await Promise.all([api.state(), api.stats()])
        if (!alive) return
        setState(s)
        setStats(st)
        setError(undefined)
      } catch (e) {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      }
    }
    poll()
    const id = setInterval(poll, 2000)
    return () => {
      alive = false
      clearInterval(id)
    }
  }, [paused])

  // ── 事件流: 先取历史定游标，再挂 SSE 收增量 ────────
  useEffect(() => {
    let cancelled = false
    let es: EventSource | null = null
    ;(async () => {
      try {
        // 别用 since=0 拉全量（世界跑久了是几万条）—— 先用超大游标只探总数
        // （后端会把越界游标收拢到 total，events 为空），再只取最后 60 条。
        const probe = await api.events(1_000_000_000)
        if (cancelled) return
        const total = probe.log_count
        const first = await api.events(Math.max(0, total - 60))
        if (cancelled) return
        setEvents(first.events)
        cursor.current = total
      } catch {
        /* 历史拉取失败也要能接实时流 */
      }
      if (cancelled) return
      setSse('connecting')
      es = new EventSource(`/api/events/stream?since=${cursor.current}`)
      es.onopen = () => !cancelled && setSse('live')
      es.onmessage = (msg) => {
        if (cancelled) return
        try {
          const ev = JSON.parse(msg.data) as LiveEvent
          setEvents((cur) => [...cur, ev].slice(-200))
        } catch {
          /* 心跳/注释帧不是 JSON，忽略 */
        }
      }
      es.onerror = () => !cancelled && setSse('off')
    })()
    return () => {
      cancelled = true
      es?.close()
    }
  }, [])

  const actors = Object.entries(state?.actors ?? {})
  const shown = (key: string) => (state?.panels ?? []).includes(key)
  const mode = stats?.mode ?? ''

  const switchMode = async (next: string) => {
    setBusy(true)
    try {
      await api.setMode(next)
      setStats(await api.stats())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const manualTick = async () => {
    setBusy(true)
    try {
      await api.tick()
      setState(await api.state())
      setStats(await api.stats())
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Live</h1>
          <p>观察运行中的世界 —— 状态每 2s 刷新，事件由 SSE 实时推送。</p>
        </div>
        <div className="spacer" />
        <div className="metrics">
          <span>
            <b>{stats?.tick ?? state?.tick ?? 0}</b>tick
          </span>
          <span>
            <b>{dur(stats?.uptime_sec ?? 0)}</b>uptime
          </span>
          <span>
            <b>{actors.length}</b>actors
          </span>
        </div>
      </div>

      <div className="chars-toolbar">
        <span className={`sse ${sse}`}>
          <span className="dot" />
          {sse === 'live' ? 'SSE 实时' : sse === 'connecting' ? '连接中…' : 'SSE 已断开'}
        </span>
        <span className="chip">模式 {mode || '—'}</span>
        <select
          className="input"
          value={mode}
          disabled={busy || !mode}
          onChange={(e) => switchMode(e.target.value)}
          style={{ width: 130 }}
        >
          <option value="rules">rules</option>
          <option value="llm">llm</option>
        </select>
        <div className="spacer" />
        <button className="btn" onClick={manualTick} disabled={busy}>
          Tick +1
        </button>
        <button className="btn" onClick={() => setPaused((v) => !v)}>
          {paused ? '继续' : '暂停'}
        </button>
      </div>

      {error ? <div className="banner error">{error}</div> : null}

      <div className="live-wrap">
        <div className="live-actors">
          <div className="sec-title">世界状态</div>
          {actors.length === 0 ? (
            <div className="state">
              <h3>还没有运行中的角色</h3>
              <p>启动 Runtime 或在 Characters 里建一个角色。</p>
            </div>
          ) : (
            actors.map(([id, a]) => (
              <div className="actor-card card" key={id}>
                <div className="actor-top">
                  <span className="dot" style={{ background: stateColor(a.state) }} />
                  <span className="actor-name">{names[id] || id}</span>
                  <code>{id}</code>
                  <span className="actor-state">{a.state ?? '—'}</span>
                </div>

                {a.activity ? <div className="actor-line">{a.activity}</div> : null}

                {shown('position') && a.position ? (
                  <div className="actor-kv">
                    <span>位置</span>
                    {a.position}
                  </div>
                ) : null}

                {shown('stamina') && a.stamina != null ? (
                  <div className="actor-kv">
                    <span>耐力</span>
                    <span className="bar">
                      <i style={{ width: `${Math.max(0, Math.min(100, a.stamina))}%` }} />
                    </span>
                    {Math.round(a.stamina)}
                  </div>
                ) : null}

                {shown('inventory') && a.inventory && Object.keys(a.inventory).length ? (
                  <div className="chips">
                    {Object.entries(a.inventory).map(([k, v]) => (
                      <span className="chip" key={k}>
                        {k} · {String(v)}
                      </span>
                    ))}
                  </div>
                ) : null}
              </div>
            ))
          )}
        </div>

        <div className="live-feed">
          <div className="sec-title">事件流（最新在下）</div>
          {events.length === 0 ? (
            <div className="state">
              <h3>还没有事件</h3>
              <p>世界推进（tick）或发生对话后会在这里出现。</p>
            </div>
          ) : (
            <div className="feed-list">
              {events.map((ev, i) => (
                <EventLine ev={ev} key={`${i}-${ev.type}`} />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  )
}

/** 单条事件 —— 只做"类型徽章 + 字段"的渲染，不解释语义。

    未知 type 走兜底: 把非空字段原样列出来。换游戏/加新事件类型也不会显示成空白。 */
function EventLine({ ev }: { ev: LiveEvent }) {
  const who = ev.npc ?? ''
  let main = ''
  if (ev.type === 'say') main = ev.text ?? ''
  else if (ev.type === 'move') main = ev.dest ?? ''
  else if (ev.type === 'gather') main = ev.resource ?? ''
  else if (ev.type === 'craft') main = ev.product ?? ''
  else if (ev.type === 'deliver') main = [ev.resource, ev.to].filter(Boolean).join(' → ')
  else if (ev.type === 'task') main = ev.desc ?? ''
  else if (ev.type === 'subagent') main = ev.text ?? ''

  const known = ['say', 'move', 'gather', 'craft', 'deliver', 'task', 'subagent']
  if (!known.includes(ev.type)) {
    // 兜底: 未知类型 —— 列出所有非空字段（不猜它是什么意思）
    const rest = Object.entries(ev)
      .filter(([k, v]) => k !== 'type' && v !== undefined && v !== '')
      .map(([k, v]) => `${k}=${String(v)}`)
      .join(' · ')
    return (
      <div className="feed-row">
        <span className="ev-type unknown">{ev.type}</span>
        <span className="ev-body">{rest || '（无字段）'}</span>
      </div>
    )
  }

  return (
    <div className="feed-row">
      <span className={`ev-type ${ev.type}`}>{ev.type}</span>
      {who ? <span className="ev-who">{who}</span> : null}
      <span className="ev-body">{main || '—'}</span>
      {ev.type === 'task' && ev.status ? (
        <span className={`chip ${ev.status}`}>{ev.status}</span>
      ) : null}
    </div>
  )
}

function dur(sec: number): string {
  if (!sec) return '0s'
  if (sec < 60) return `${sec}s`
  if (sec < 3600) return `${Math.floor(sec / 60)}m`
  return `${Math.floor(sec / 3600)}h${Math.floor((sec % 3600) / 60)}m`
}
