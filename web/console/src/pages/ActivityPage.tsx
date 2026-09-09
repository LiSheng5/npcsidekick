import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { EventLine, eventText } from '../components/EventLine'
import type { LiveEvent } from '../types'

/** Activity —— 事件历史档案（与 Live 分工明确）。

    Live = "此刻"（2s 轮询 + SSE 实时流）；
    Activity = "过去"（可检索/筛选/翻页的历史，**手动刷新**，不接 SSE ——
    倒序列表再加实时插入会不停跳动，两种视图各司其职更清楚）。

    历史按**绝对游标**分段加载：先用超大 since 探总数（后端会把越界游标收拢，
    events 为空），再取尾部一页；"加载更早"则往前再取一页，只切[start, oldest) 那段。

    game-agnostic: 类型与角色列表一律**从已加载事件里观察**得出，不预设 7 种事件类型。 */
const PAGE = 200

export function ActivityPage() {
  const [events, setEvents] = useState<LiveEvent[]>([]) // 正序存储：旧 → 新
  const [total, setTotal] = useState(0)
  const [oldest, setOldest] = useState(0) // 已加载到的最早绝对位置
  const [loading, setLoading] = useState(true)
  const [more, setMore] = useState(false)
  const [error, setError] = useState<string>()
  const [q, setQ] = useState('')
  const [type, setType] = useState('')
  const [who, setWho] = useState('')

  const loadLatest = useCallback(async () => {
    setLoading(true)
    try {
      const probe = await api.events(1_000_000_000) // 只探总数，不拉全量
      const t = probe.log_count
      const start = Math.max(0, t - PAGE)
      const res = await api.events(start)
      setEvents(res.events)
      setTotal(t)
      setOldest(start)
      setError(undefined)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadLatest()
  }, [loadLatest])

  const loadOlder = async () => {
    if (oldest <= 0 || more) return
    setMore(true)
    try {
      const start = Math.max(0, oldest - PAGE)
      const res = await api.events(start)
      // 该区间是 [start, total)，只切出我们还没加载的 [start, oldest) 那段
      const older = res.events.slice(0, Math.max(0, oldest - start))
      setEvents((cur) => [...older, ...cur])
      setOldest(start)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setMore(false)
    }
  }

  // 类型 / 角色：从数据观察（不硬编码"标准事件类型"）
  const counts = useMemo(() => {
    const m: Record<string, number> = {}
    for (const e of events) m[e.type] = (m[e.type] ?? 0) + 1
    return m
  }, [events])
  const types = useMemo(() => Object.keys(counts).sort(), [counts])
  const actors = useMemo(
    () => [...new Set(events.map((e) => e.npc).filter(Boolean) as string[])].sort(),
    [events],
  )

  const shown = useMemo(() => {
    const kw = q.trim().toLowerCase()
    const hit = events.filter((e) => {
      if (type && e.type !== type) return false
      if (who && e.npc !== who) return false
      if (kw && !eventText(e).includes(kw)) return false
      return true
    })
    return hit.reverse() // 展示倒序：最新在最上
  }, [events, type, who, q])

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Activity</h1>
          <p>翻阅与检索事件历史 —— 手动刷新；实时事件流在 Live 页。</p>
        </div>
        <div className="spacer" />
        <div className="metrics">
          <span>
            <b>{total}</b>条在流上
          </span>
          <span>
            <b>{events.length}</b>条已加载
          </span>
          <span>
            <b>{shown.length}</b>条命中
          </span>
        </div>
      </div>

      <div className="chars-toolbar">
        <input
          className="input"
          placeholder="搜索事件内容…"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          style={{ flex: 1, maxWidth: 300 }}
        />
        <select
          className="input"
          value={who}
          onChange={(e) => setWho(e.target.value)}
          style={{ width: 150 }}
        >
          <option value="">全部角色</option>
          {actors.map((a) => (
            <option key={a} value={a}>
              {a}
            </option>
          ))}
        </select>
        <div className="spacer" />
        <button className="btn" onClick={loadLatest} disabled={loading}>
          {loading ? '刷新中…' : 'Refresh'}
        </button>
      </div>

      {error ? <div className="banner error">{error}</div> : null}

      {types.length > 0 ? (
        <div className="filter-row">
          <button className={`chip${type === '' ? ' on' : ''}`} onClick={() => setType('')}>
            全部 {events.length}
          </button>
          {types.map((t) => (
            <button
              key={t}
              className={`chip${type === t ? ' on' : ''}`}
              onClick={() => setType(t)}
            >
              {t} {counts[t]}
            </button>
          ))}
        </div>
      ) : null}

      {loading && events.length === 0 ? (
        <div className="state">
          <h3>Loading…</h3>
        </div>
      ) : events.length === 0 ? (
        <div className="state">
          <h3>还没有事件</h3>
          <p>世界推进（tick）或发生对话后，事件会记录进世界日志。</p>
        </div>
      ) : shown.length === 0 ? (
        <div className="state">
          <h3>没有匹配的事件</h3>
          <p>换个关键词，或清空筛选条件。</p>
        </div>
      ) : (
        <div className="act-list">
          {oldest > 0 ? (
            <button className="btn load-more" onClick={loadOlder} disabled={more}>
              {more ? '加载中…' : `加载更早（还有 ${oldest} 条）`}
            </button>
          ) : (
            <div className="act-end">已到事件流的开头</div>
          )}

          <div className="feed-list">
            {shown.map((ev, i) => (
              <EventLine ev={ev} key={`${i}-${ev.type}`} />
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
