import { useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { fmtInt, fmtPct, isoTime } from '../lib/format'
import { StateBlock } from './StateBlock'
import type {
  MemoryJournalEntry,
  MemoryReportNpc,
  MemoryReportPayload,
  NpcBrief,
  TidyRankItem,
  TopDuplicate,
} from '../types'

/** 记忆体检 —— Memory 页的「记忆体检」子视图（纯观测，只读两个端点，零写入）。
 *
 *  两块数据:
 *    · /api/memory/report          跨 NPC 全局体检（NPC 数 / 体积 / 重复 / 最该整理榜 / 流民）
 *    · /api/npcs/{id}/memory-journal  单个 NPC 的整理审计流水（trigger / at / actions）
 *
 *  铁律（与项目一致）:
 *    · 字段缺失或为 0 时如实显示，不确定的显示 '—'，绝不编造。
 *    · tidy_ranking 后端已排好序，UI **不重排**。
 *    · 流民(ephemeral)后端单列跳过，这里只给一条克制提示，不污染常驻体检。 */

// ── 取值: 字段名取实测后端（npc/memory_report.py 真实输出），只为"字段缺失"兜底 ──
function rankNpcId(r: TidyRankItem): string {
  return (r.actor_id ?? '') as string
}
function dupGroupsOf(s: MemoryReportNpc): { content: string; times: number }[] {
  if (!Array.isArray(s.duplicate_groups)) return []
  return s.duplicate_groups.map((g) => ({
    content: String(g.content ?? ''),
    times: Number(g.times ?? 0),
  }))
}
function dupCountOf(s: MemoryReportNpc): number {
  if (typeof s.duplicate_group_count === 'number') return s.duplicate_group_count
  return dupGroupsOf(s).length
}
function oldestHoursOf(s: MemoryReportNpc): number | null {
  const v = s.oldest_span_hours
  return typeof v === 'number' && !Number.isNaN(v) ? v : null
}

export function MemoryHealth({ npcs }: { npcs: NpcBrief[] }) {
  const [report, setReport] = useState<MemoryReportPayload>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>()

  const [openId, setOpenId] = useState<string>() // 展开/选中看细节 + journal 的 NPC

  const nameOf = useMemo(() => {
    const m = new Map(npcs.map((n) => [n.id, n.name || n.id]))
    return (id: string) => m.get(id) || id
  }, [npcs])

  useEffect(() => {
    let alive = true
    setLoading(true)
    setError(undefined)
    api
      .memoryReport()
      .then((r) => {
        if (alive) setReport(r)
      })
      .catch((e) => {
        if (alive) setError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setLoading(false)
      })
    return () => {
      alive = false
    }
  }, [])

  const g = report?.global
  const perNpc = report?.per_npc ?? {}

  if (loading) return <StateBlock kind="loading" title="正在体检记忆…" />
  if (error) return <StateBlock kind="error" title="体检数据取不到">{error}</StateBlock>
  if (!g && Object.keys(perNpc).length === 0) {
    return (
      <StateBlock kind="empty" title="还没有可体检的记忆">
        常驻角色的记忆卡为空，或 Runtime 还未上线。等角色写记忆后再来看这份体检。
      </StateBlock>
    )
  }

  const npcCount = g?.npc_count ?? Object.keys(perNpc).length
  const totalEntries = g?.total_entries
  const totalTokens = g?.total_tokens
  const threshold = g?.threshold_limit
  const overCount = g?.over_threshold_count ?? 0
  const ephemeralCount = g?.ephemeral_count ?? 0
  const ranking = g?.tidy_ranking ?? []
  const topDups = g?.top_duplicates ?? []

  return (
    <div className="report">
      {ephemeralCount > 0 ? (
        <div className="quiet-note">
          另有 <b>{fmtInt(ephemeralCount)}</b> 个流民（ephemeral）已跳过体检 ——
          它们不落盘、despawn 即忘，不计入常驻记忆健康度。
          {g?.ephemeral_ids?.length
            ? `（${g.ephemeral_ids.slice(0, 6).map((x) => nameOf(x)).join('、')}${
                g.ephemeral_ids.length > 6 ? '…' : ''
              }）`
            : ''}
        </div>
      ) : null}

      {/* 全局概览 */}
      <div className="stat-grid">
        <div className="stat-card">
          <div className="v">{fmtInt(npcCount)}</div>
          <div className="l">常驻 NPC</div>
        </div>
        <div className="stat-card">
          <div className="v">{fmtInt(totalEntries)}</div>
          <div className="l">总条数</div>
        </div>
        <div className="stat-card">
          <div className="v">{fmtInt(totalTokens)}</div>
          <div className="l">总体积（token 粗估）</div>
        </div>
        <div className={`stat-card${overCount > 0 ? ' warn' : ''}`}>
          <div className="v">{fmtInt(overCount)}</div>
          <div className="l">
            超阈值 NPC
            {typeof threshold === 'number' ? `（限 ${fmtInt(threshold)}）` : ''}
          </div>
        </div>
      </div>

      {/* 最该整理的 NPC 榜（后端已排序，不重排） */}
      <section>
        <div className="sec-title">最该整理的 NPC（后端已排序）</div>
        {ranking.length === 0 ? (
          <div className="hint">没有进入榜单的 NPC —— 都很 tidy。</div>
        ) : (
          <div className="rank-list">
            {ranking.map((r, i) => {
              const id = rankNpcId(r)
              const overage = Number(r.overage ?? 0)
              const ratio = r.untyped_ratio
              return (
                <button
                  key={id || String(i)}
                  className={`rank-row${openId === id ? ' on' : ''}`}
                  onClick={() => setOpenId((cur) => (cur === id ? undefined : id))}
                >
                  <span className="nm">
                    {nameOf(id)}
                    <code>{id}</code>
                  </span>
                  <span className="num">{fmtInt(r.tokens)} token</span>
                  {overage > 0 ? <span className="pill over">超 {fmtInt(overage)}</span> : null}
                  <span className="pill untyped">未分型 {fmtPct(ratio)}</span>
                </button>
              )
            })}
          </div>
        )}
      </section>

      {/* 全库重复最多的内容 top N */}
      <section>
        <div className="sec-title">全库重复最多的内容 Top {topDups.length || '—'}</div>
        {topDups.length === 0 ? (
          <div className="hint">没有检测到重复内容。</div>
        ) : (
          <div className="dup-list">
            {topDups.map((d: TopDuplicate, i) => (
              <div className="dup-row" key={`${d.content ?? ''}_${i}`}>
                <span className="x">×{fmtInt(d.times)}</span>
                <span className="nc">{fmtInt(d.npc_count)} NPC</span>
                <span className="ct">{d.content ? d.content : '（空内容）'}</span>
              </div>
            ))}
          </div>
        )}
      </section>

      {/* 每个 NPC 的体检（可展开看细节 + 整理流水） */}
      <section>
        <div className="sec-title">每个 NPC 的记忆体检（点开看细节 / 整理流水）</div>
        <div className="npc-health-list">
          {Object.keys(perNpc).map((id) => (
            <NpcHealthRow
              key={id}
              id={id}
              name={nameOf(id)}
              stat={perNpc[id]}
              threshold={typeof threshold === 'number' ? threshold : undefined}
              open={openId === id}
              onToggle={() => setOpenId((cur) => (cur === id ? undefined : id))}
            />
          ))}
        </div>
      </section>
    </div>
  )
}

/** 单个 NPC 的体检行: 摘要常驻，展开后拉整理流水 + 看细节。 */
function NpcHealthRow({
  id,
  name,
  stat,
  threshold,
  open,
  onToggle,
}: {
  id: string
  name: string
  stat: MemoryReportNpc
  threshold?: number
  open: boolean
  onToggle: () => void
}) {
  const [journal, setJournal] = useState<MemoryJournalEntry[]>([])
  const [jLoading, setJLoading] = useState(false)
  const [jError, setJError] = useState<string>()

  useEffect(() => {
    if (!open) return
    let alive = true
    setJLoading(true)
    setJError(undefined)
    api
      .memoryJournal(id, 50)
      .then((j) => {
        if (alive) setJournal(j)
      })
      .catch((e) => {
        if (alive) setJError(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (alive) setJLoading(false)
      })
    return () => {
      alive = false
    }
  }, [open, id])

  const dist = stat.mtype_distribution ?? {}
  const distEntries = Object.entries(dist).filter(([, v]) => v > 0)
  const activeN = stat.active ?? 0
  const totalN = stat.total ?? 0
  const archivedN = stat.archived ?? 0
  const tokens = stat.tokens
  const over = Boolean(stat.over_threshold)
  const untypedRatio = stat.untyped_ratio
  const dupGroups = dupGroupsOf(stat)
  const dupCount = dupCountOf(stat)
  const oldest = oldestHoursOf(stat)
  const entries = journal ?? []

  return (
    <div className={`npc-health-row${open ? ' open' : ''}`}>
      <button className="npc-health-summary" onClick={onToggle}>
        <span className={`over-dot${over ? ' on' : ''}`} />
        <span className="nm">{name}</span>
        <code className="muted">{id}</code>
        <span className="sp" />
        <span className="mini">
          {fmtInt(activeN)}/{fmtInt(totalN)} 活跃·总计
        </span>
        <span className="mini">{fmtInt(tokens)} token</span>
        <span className="mini">未分型 {fmtPct(untypedRatio)}</span>
        <span className="mini">重复 {fmtInt(dupCount)}</span>
        <span className="mini">{open ? '收起' : '展开'}</span>
      </button>

      {open ? (
        <div className="npc-health-detail">
          <div className="kv">
            <span className="k">条数</span>
            <span className="v">
              总 {fmtInt(totalN)} · 活跃 {fmtInt(activeN)} · 归档 {fmtInt(archivedN)}
            </span>
          </div>
          <div className="kv">
            <span className="k">体积</span>
            <span className="v">
              {fmtInt(tokens)} token
              {typeof threshold === 'number'
                ? over
                  ? ` · 已超阈值（限 ${fmtInt(threshold)}）`
                  : ` · 阈值 ${fmtInt(threshold)}`
                : ''}
            </span>
          </div>
          <div className="kv">
            <span className="k">未分型</span>
            <span className="v">
              {fmtInt(stat.untyped)} 条 · 占比 {fmtPct(untypedRatio)}
            </span>
          </div>
          <div className="kv">
            <span className="k">最旧跨度</span>
            <span className="v">
              {oldest === null ? '—（无时间戳）' : `${oldest.toFixed(2)} 小时前`}
            </span>
          </div>

          <div>
            <div className="sec-title" style={{ marginTop: 0 }}>
              三分类分布
            </div>
            {distEntries.length === 0 ? (
              <div className="hint">无已分型条目。</div>
            ) : (
              <div className="mtype-bars">
                {distEntries.map(([k, v]) => {
                  const pct = activeN ? (v / activeN) * 100 : 0
                  return (
                    <div className="mtype-bar" key={k}>
                      <span className="bl">{k}</span>
                      <span className="track">
                        <i style={{ width: `${pct}%` }} />
                      </span>
                      <span className="bv">{fmtInt(v)}</span>
                    </div>
                  )
                })}
              </div>
            )}
          </div>

          <div>
            <div className="sec-title" style={{ marginTop: 0 }}>
              重复组（{fmtInt(dupCount)}）
            </div>
            {dupGroups.length === 0 ? (
              <div className="hint">没有重复内容。</div>
            ) : (
              <div className="dup-list">
                {dupGroups.slice(0, 12).map((d, i) => (
                  <div className="dup-row" key={`${d.content}_${i}`}>
                    <span className="x">×{fmtInt(d.times)}</span>
                    <span className="ct">{d.content ? d.content : '（空内容）'}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* 整理流水（journal） */}
          <div>
            <div className="sec-title" style={{ marginTop: 0 }}>
              整理流水（最近 {fmtInt(entries.length)} 条）
            </div>
            {jLoading ? (
              <div className="hint">加载整理流水…</div>
            ) : jError ? (
              <div className="hint" style={{ color: 'var(--red)' }}>
                流水取不到：{jError}
              </div>
            ) : entries.length === 0 ? (
              <div className="hint">这个 NPC 没有整理流水记录。</div>
            ) : (
              <div className="journal">
                {entries.map((e: MemoryJournalEntry, i) => (
                  <div className="journal-row" key={i}>
                    <div className="jh">
                      <span className="jt">{e.trigger ? String(e.trigger) : '整理'}</span>
                      <span>{isoTime(e.at)}</span>
                    </div>
                    <div className="ja">
                      {(Array.isArray(e.actions) ? e.actions : []).map((a, j) => (
                        <code key={j}>
                          {typeof a === 'string' ? a : JSON.stringify(a)}
                        </code>
                      ))}
                    </div>
                  </div>
                ))}
              </div>
            )}
          </div>
        </div>
      ) : null}
    </div>
  )
}
