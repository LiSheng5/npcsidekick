import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { stateColor } from '../lib/format'
import type { NpcBrief, Persona } from '../types'
import { CharacterEditor } from './CharacterEditor'

/** Characters —— 角色清单（定义 + 运行时状态合并）与入口。

  左列 = 人设定义（/api/personas，制作者写的东西）；
  卡片上的 state / 记忆数 = 运行时快照（/api/npcs）。
  点卡片进编辑器；New 进编辑器的新建态（id 可填）。 */
export function CharactersPage() {
  const [personas, setPersonas] = useState<Persona[]>([])
  const [briefs, setBriefs] = useState<Record<string, NpcBrief>>({})
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>()
  const [query, setQuery] = useState('')
  const [editing, setEditing] = useState<{ persona: Persona; isNew: boolean }>()

  const load = useCallback(async () => {
    try {
      const [p, n] = await Promise.all([api.personas(), api.npcs()])
      setPersonas(p.personas)
      setBriefs(Object.fromEntries(n.npcs.map((x) => [x.id, x])))
      setError(undefined)
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const remove = async (p: Persona) => {
    const name = p.name || p.id
    if (!window.confirm(`删除角色「${name}」？\n\n人设文件会移进 .trash（可反悔），记忆卡默认保留。`)) {
      return
    }
    try {
      await api.deletePersona(p.id)
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    }
  }

  const startNew = () =>
    setEditing({
      isNew: true,
      persona: {
        id: '',
        name: '',
        identity: '',
        personality: '',
        speech_style: '',
        taboos: [],
        routine: [],
      },
    })

  const onSaved = useCallback(
    (saved: Persona) => {
      // 保存后刷新运行时快照（state/记忆数），并留在编辑器里看最新定义
      setEditing((cur) => (cur ? { ...cur, isNew: false, persona: saved } : cur))
      load()
    },
    [load],
  )

  if (editing) {
    return (
      <CharacterEditor
        persona={editing.persona}
        isNew={editing.isNew}
        onBack={() => {
          setEditing(undefined)
          load()
        }}
        onSaved={onSaved}
      />
    )
  }

  const filtered = personas.filter((p) => {
    const q = query.trim().toLowerCase()
    if (!q) return true
    return (
      p.id.toLowerCase().includes(q) ||
      (p.name ?? '').toLowerCase().includes(q) ||
      (p.identity ?? '').toLowerCase().includes(q)
    )
  })

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Characters</h1>
          <p>定义、观察与编辑每个角色的言行 —— 改动即热加载，无需重启。</p>
        </div>
        <div className="spacer" />
        <div className="metrics">
          <span>
            <b>{personas.length}</b>defined
          </span>
        </div>
      </div>

      <div className="chars-toolbar">
        <input
          className="input"
          placeholder="Search characters…"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          style={{ width: 260 }}
        />
        <div className="spacer" />
        <button className="btn" onClick={load}>
          Refresh
        </button>
        <button className="btn primary" onClick={startNew}>
          + New character
        </button>
      </div>

      {error ? (
        <div className="state error">
          <h3>加载失败</h3>
          <p>{error}</p>
        </div>
      ) : loading ? (
        <div className="state">
          <h3>Loading…</h3>
        </div>
      ) : filtered.length === 0 ? (
        <div className="state">
          <h3>{personas.length === 0 ? '还没有角色' : '没有匹配的角色'}</h3>
          <p>
            {personas.length === 0
              ? '在 npc/personas/ 放一个 <id>.json，或点右上角 New character。'
              : '换个关键词试试。'}
          </p>
        </div>
      ) : (
        <div className="chars-grid">
          {filtered.map((p) => {
            const brief = briefs[p.id]
            return (
              <div
                className="char-card card"
                key={p.id}
                onClick={() => setEditing({ persona: p, isNew: false })}
                role="button"
                tabIndex={0}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') setEditing({ persona: p, isNew: false })
                }}
              >
                <div className="char-top">
                  <span className="char-avatar" style={{ background: stateColor(brief?.state) }}>
                    {(p.name || p.id).slice(0, 1).toUpperCase()}
                  </span>
                  <div className="char-id">
                    <div className="char-name">{p.name || p.id}</div>
                    <code>{p.id}</code>
                  </div>
                  <button
                    className="btn tiny danger"
                    onClick={(e) => {
                      e.stopPropagation()
                      remove(p)
                    }}
                    title="删除"
                  >
                    ✕
                  </button>
                </div>

                <div className="char-role">{p.identity || '—'}</div>

                <div className="char-meta">
                  {brief ? (
                    <>
                      <span className="dot" style={{ background: stateColor(brief.state) }} />
                      <span>{brief.state ?? 'idle'}</span>
                    </>
                  ) : (
                    <span className="muted">未运行</span>
                  )}
                  <span className="muted">
                    {brief?.memory_count ?? 0} 条记忆
                    {brief?.ephemeral ? ' · 流民' : ''}
                  </span>
                </div>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
