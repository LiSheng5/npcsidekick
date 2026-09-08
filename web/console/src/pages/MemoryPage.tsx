import { useCallback, useEffect, useMemo, useState } from 'react'
import { api } from '../api/client'
import { relativeTime, stateColor } from '../lib/format'
import type {
  MemoryEntry,
  MemoryFacets,
  MemoryListPayload,
  NpcBrief,
} from '../types'

/** Memory —— 记忆卡查看器/编辑器。

  铁律: 一切读写都走 Browser → API → NPC.remember / NPCMemory → 记忆卡。
  React 不碰 JSON 文件，也不另造一套记忆结构（绕过现有架构 = 违规）。

  两个容易混淆的状态必须说清（否则用户会以为数据丢了）:
    · 检索模式: 有 query 时后端走加权检索，返回的是**召回 top-k**，不是全量过滤；
      所以 entries 会少于 total，页面顶部明确标注。
    · 写入三态: added / merged（同文日常被去重闸折叠计数）/ rejected（安全闸拒收）。 */
export function MemoryPage() {
  const [npcs, setNpcs] = useState<NpcBrief[]>([])
  const [pid, setPid] = useState('')
  const [data, setData] = useState<MemoryListPayload>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>()
  const [draft, setDraft] = useState('') // 搜索框（未提交）
  const [query, setQuery] = useState('') // 已提交的检索词
  const [cat, setCat] = useState('') // '' = 全部分类
  const [sort, setSort] = useState<'newest' | 'oldest' | 'importance'>('newest')
  const [notice, setNotice] = useState<{ kind: 'ok' | 'error'; text: string }>()
  const [adding, setAdding] = useState(false)

  const loadNpcs = useCallback(async () => {
    try {
      const n = await api.npcs()
      setNpcs(n.npcs)
      setPid((cur) => (cur && n.npcs.some((x) => x.id === cur) ? cur : n.npcs[0]?.id ?? ''))
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }, [])

  const loadMemory = useCallback(async () => {
    if (!pid) {
      setData(undefined)
      setLoading(false)
      return
    }
    setLoading(true)
    try {
      setData(await api.memory(pid, query))
      setError(undefined)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [pid, query])

  useEffect(() => {
    loadNpcs()
  }, [loadNpcs])

  useEffect(() => {
    loadMemory()
  }, [loadMemory])

  const entries = useMemo(() => {
    const list = [...(data?.entries ?? [])]
    const filtered = cat ? list.filter((e) => (e.category || 'general') === cat) : list
    if (sort === 'newest') filtered.sort((a, b) => b.created_at - a.created_at)
    else if (sort === 'oldest') filtered.sort((a, b) => a.created_at - b.created_at)
    else
      filtered.sort(
        (a, b) => b.importance - a.importance || b.created_at - a.created_at,
      )
    return filtered
  }, [data, cat, sort])

  const current = npcs.find((n) => n.id === pid)
  const facets: MemoryFacets = data?.facets ?? { categories: [], mtypes: [] }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Memory</h1>
          <p>
            读写与检索角色的记忆卡 —— 全部经 Runtime 的记忆系统落盘，UI 不直接改 JSON。
          </p>
        </div>
        <div className="spacer" />
        <div className="metrics">
          <span>
            <b>{data?.total ?? 0}</b>条在卡上
          </span>
          <span>
            <b>{entries.length}</b>条显示中
          </span>
        </div>
      </div>

      <div className="chars-toolbar">
        <select
          className="input"
          value={pid}
          onChange={(e) => {
            setPid(e.target.value)
            setQuery('')
            setDraft('')
            setCat('')
          }}
          style={{ width: 220 }}
        >
          {npcs.length === 0 ? <option value="">（没有运行中的角色）</option> : null}
          {npcs.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name || n.id}（{n.memory_count ?? 0}）
            </option>
          ))}
        </select>

        <input
          className="input"
          placeholder="检索记忆…（回车搜索）"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') setQuery(draft)
          }}
          style={{ flex: 1, maxWidth: 340 }}
        />
        {query ? (
          <button
            className="btn"
            onClick={() => {
              setDraft('')
              setQuery('')
            }}
          >
            Clear
          </button>
        ) : null}

        <select
          className="input"
          value={sort}
          onChange={(e) => setSort(e.target.value as typeof sort)}
          style={{ width: 150 }}
        >
          <option value="newest">最新的在前</option>
          <option value="oldest">最早的在前</option>
          <option value="importance">按重要度</option>
        </select>

        <div className="spacer" />
        <button className="btn" onClick={loadMemory}>
          Refresh
        </button>
        <button
          className="btn primary"
          disabled={!pid}
          onClick={() => {
            setAdding((v) => !v)
            setNotice(undefined)
          }}
        >
          {adding ? '取消' : '+ Add memory'}
        </button>
      </div>

      {query ? (
        <div className="hint search-hint">
          检索模式：按加权相关度（时效 × 0.5 + 相关度 × 3 + 重要度 × 2）召回最多 20 条，
          <b>不是</b>全量过滤 —— 显示条数少于卡上总数是正常的。
        </div>
      ) : null}
      {current?.ephemeral ? (
        <div className="hint search-hint">
          这个角色是流民（ephemeral），记忆不落盘 —— 改动只在本次运行内有效。
        </div>
      ) : null}

      {notice ? (
        <div className={`banner ${notice.kind === 'ok' ? 'ok' : 'error'}`}>{notice.text}</div>
      ) : null}
      {error ? (
        <div className="banner error">{error}</div>
      ) : null}

      {/* 全页共用的输入建议（datalist 不能重复 id，故只声明一次） */}
      <datalist id="mem-cats">
        {facets.categories.map((c) => (
          <option key={c} value={c} />
        ))}
      </datalist>
      <datalist id="mem-mtypes">
        {facets.mtypes.map((m) => (
          <option key={m} value={m} />
        ))}
      </datalist>

      {adding && pid ? (
        <MemoryComposer
          pid={pid}
          onClose={() => setAdding(false)}
          onDone={(kind, text) => {
            setNotice({ kind, text })
            setAdding(false)
            loadMemory()
            loadNpcs()
          }}
        />
      ) : null}

      {facets.categories.length > 1 ? (
        <div className="filter-row">
          <button
            className={`chip${cat === '' ? ' on' : ''}`}
            onClick={() => setCat('')}
          >
            全部
          </button>
          {facets.categories.map((c) => (
            <button
              key={c}
              className={`chip${cat === c ? ' on' : ''}`}
              onClick={() => setCat(c)}
            >
              <span className="dot" style={{ background: stateColor(c) }} />
              {c}
            </button>
          ))}
        </div>
      ) : null}

      {loading ? (
        <div className="state">
          <h3>Loading…</h3>
        </div>
      ) : !pid ? (
        <div className="state">
          <h3>还没有运行中的角色</h3>
          <p>先在 Characters 建一个角色，或启动 Runtime 让已有角色上线。</p>
        </div>
      ) : entries.length === 0 ? (
        <div className="state">
          <h3>{data?.total ? '没有匹配的记忆' : '这张卡还是空的'}</h3>
          <p>
            {data?.total
              ? '换个分类或清空检索词试试。'
              : '记忆由 Runtime 自动写入（对话、任务结果、反思），也可以手动 Add memory。'}
          </p>
        </div>
      ) : (
        <div className="mem-list">
          {entries.map((e) => (
            <MemoryRow
              key={e.id}
              pid={pid}
              entry={e}
              onChanged={() => {
                loadMemory()
                loadNpcs()
              }}
              onError={(text) => setNotice({ kind: 'error', text })}
            />
          ))}
        </div>
      )}
    </div>
  )
}

/** 新增记忆 —— 走 POST /api/npcs/{pid}/memory（后端再经 NPC.remember）。 */
function MemoryComposer({
  pid,
  onClose,
  onDone,
}: {
  pid: string
  onClose: () => void
  onDone: (kind: 'ok' | 'error', text: string) => void
}) {
  const [content, setContent] = useState('')
  const [importance, setImportance] = useState(5)
  const [category, setCategory] = useState('general')
  const [mtype, setMtype] = useState('')
  const [busy, setBusy] = useState(false)

  const submit = async () => {
    if (!content.trim() || busy) return
    setBusy(true)
    try {
      const r = await api.addMemory(pid, {
        content: content.trim(),
        importance,
        category: category.trim() || 'general',
        mtype: mtype.trim(),
      })
      if (r.status === 'rejected') onDone('error', r.message || '写入被拒收')
      else if (r.status === 'merged') onDone('ok', r.message || '已并入同文条目')
      else onDone('ok', '已写入记忆卡')
      setContent('')
    } catch (e) {
      onDone('error', e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="grp mem-compose">
      <div className="grp-title">新增记忆</div>
      <div className="fld">
        <span>
          内容 <em>写入即经安全闸与去重闸，和 Runtime 自动记忆同一入口</em>
        </span>
        <textarea
          className="input"
          rows={2}
          value={content}
          placeholder="例如：玩家把最后一块铁料让给了我"
          onChange={(e) => setContent(e.target.value)}
        />
      </div>
      <div className="fld-row">
        <div className="fld">
          <span>重要度 0–9</span>
          <input
            className="input x-narrow"
            type="number"
            min={0}
            max={9}
            value={importance}
            onChange={(e) => setImportance(Number(e.target.value))}
          />
        </div>
        <div className="fld">
          <span>分类</span>
          <input
            className="input narrow"
            list="mem-cats"
            value={category}
            onChange={(e) => setCategory(e.target.value)}
          />
        </div>
        <div className="fld">
          <span>
            类型 <em>可留空</em>
          </span>
          <input
            className="input narrow"
            list="mem-mtypes"
            value={mtype}
            placeholder="—"
            onChange={(e) => setMtype(e.target.value)}
          />
        </div>
        <div className="spacer" />
        <button className="btn" onClick={onClose}>
          收起
        </button>
        <button className="btn primary" disabled={busy || !content.trim()} onClick={submit}>
          {busy ? '写入中…' : '写入记忆'}
        </button>
      </div>
    </div>
  )
}

/** 单条记忆 —— 就地编辑（PUT）/ 删除（DELETE），都经 API 落回记忆卡。 */
function MemoryRow({
  pid,
  entry,
  onChanged,
  onError,
}: {
  pid: string
  entry: MemoryEntry
  onChanged: () => void
  onError: (msg: string) => void
}) {
  const [editing, setEditing] = useState(false)
  const [content, setContent] = useState(entry.content)
  const [importance, setImportance] = useState(entry.importance)
  const [category, setCategory] = useState(entry.category || 'general')
  const [mtype, setMtype] = useState(entry.mtype || '')
  const [busy, setBusy] = useState(false)

  const save = async () => {
    if (busy) return
    setBusy(true)
    try {
      await api.updateMemory(pid, entry.id, {
        content: content.trim(),
        importance,
        category: category.trim() || 'general',
        mtype: mtype.trim(),
      })
      setEditing(false)
      onChanged()
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const remove = async () => {
    if (!window.confirm('删除这条记忆？\n\n会立即从记忆卡移除，不可撤销。')) return
    setBusy(true)
    try {
      await api.deleteMemory(pid, entry.id)
      onChanged()
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const imp = entry.importance

  return (
    <div className={`mem-row card${editing ? ' editing' : ''}`}>
      <div className="mem-main">
        {editing ? (
          <textarea
            className="input"
            rows={2}
            value={content}
            onChange={(e) => setContent(e.target.value)}
          />
        ) : (
          <div className="mem-content">{entry.content}</div>
        )}

        <div className="mem-meta">
          <span className={`imp ${imp >= 8 ? 'hi' : imp >= 5 ? 'mid' : 'lo'}`}>
            imp {imp}
          </span>
          <span className="chip">
            <span className="dot" style={{ background: stateColor(entry.category) }} />
            {entry.category || 'general'}
          </span>
          {entry.mtype ? <span className="chip">{entry.mtype}</span> : null}
          {entry.count && entry.count > 1 ? (
            <span className="chip">× {entry.count}</span>
          ) : null}
          <span className="muted">{relativeTime(entry.created_at)}</span>
        </div>

        {editing ? (
          <div className="fld-row">
            <div className="fld">
              <span>重要度</span>
              <input
                className="input x-narrow"
                type="number"
                min={0}
                max={9}
                value={importance}
                onChange={(e) => setImportance(Number(e.target.value))}
              />
            </div>
            <div className="fld">
              <span>分类</span>
              <input
                className="input narrow"
                list="mem-cats"
                value={category}
                onChange={(e) => setCategory(e.target.value)}
              />
            </div>
            <div className="fld">
              <span>类型</span>
              <input
                className="input narrow"
                list="mem-mtypes"
                value={mtype}
                placeholder="—"
                onChange={(e) => setMtype(e.target.value)}
              />
            </div>
          </div>
        ) : null}
      </div>

      <div className="mem-actions">
        {editing ? (
          <>
            <button className="btn tiny" onClick={save} disabled={busy}>
              保存
            </button>
            <button
              className="btn tiny"
              onClick={() => {
                setContent(entry.content)
                setImportance(entry.importance)
                setCategory(entry.category || 'general')
                setMtype(entry.mtype || '')
                setEditing(false)
              }}
            >
              取消
            </button>
          </>
        ) : (
          <button className="btn tiny" onClick={() => setEditing(true)}>
            编辑
          </button>
        )}
        <button className="btn tiny danger" onClick={remove} disabled={busy}>
          ✕
        </button>
      </div>
    </div>
  )
}
