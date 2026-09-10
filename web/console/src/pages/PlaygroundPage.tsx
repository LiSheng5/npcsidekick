import { useEffect, useState } from 'react'
import { api } from '../api/client'
import type { MemoryEntry, NpcBrief } from '../types'

/** Playground —— 对话测试 + 调试面板（Step 9）。

    走的是**游戏协议端点本身**（`POST /api/talk`，零改动），不是另开一套聊天接口 ——
    这里看到的答复，和游戏里拿到的一模一样。

    Debug 面板三个值的来源必须说清（否则就是编数据）:
      · 延迟 —— **前端实测**往返耗时（/api/talk 响应里没有 latency 字段）
      · 模式 —— 取自 /api/mode（全局生效模式，可能是 llm 配了却退化成 rules）
      · 召回预览 —— 拿这句话去检索记忆卡的 top-5，**是近似**：NPC.talk 内部
        有自己的检索与上下文拼装，这里只是"用同一句话能召回到什么"，不等于真实上下文。

    game-agnostic: 不预置任何提示词/示例句/资源名，输入什么完全由使用者决定。 */
interface Turn {
  id: number
  message: string
  reply: string
  thinking?: string
  latencyMs: number
  mode: string
  recalled: MemoryEntry[]
  error?: string
}

export function PlaygroundPage() {
  const [npcs, setNpcs] = useState<NpcBrief[]>([])
  const [pid, setPid] = useState('')
  const [draft, setDraft] = useState('')
  const [turns, setTurns] = useState<Turn[]>([])
  const [sel, setSel] = useState<number>() // 选中的轮次（右侧看它的调试信息）
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string>()

  useEffect(() => {
    api
      .npcs()
      .then((n) => {
        setNpcs(n.npcs)
        setPid((cur) => cur || n.npcs[0]?.id || '')
      })
      .catch((e) => setError(e instanceof Error ? e.message : String(e)))
  }, [])

  const send = async () => {
    const msg = draft.trim()
    if (!msg || !pid || busy) return
    setBusy(true)
    setError(undefined)
    const t0 = performance.now()
    try {
      const res = await api.talk(pid, msg)
      const latencyMs = Math.round(performance.now() - t0)
      // 召回预览与模式在答复之后取 —— 不污染上面的延迟测量
      const [rec, mode] = await Promise.all([
        api.memory(pid, msg, 5).catch(() => ({ entries: [] as MemoryEntry[] })),
        api.mode().catch(() => ({ mode: '', requested: '' })),
      ])
      const turn: Turn = {
        id: Date.now(),
        message: msg,
        reply: res.reply ?? '',
        thinking: res.thinking_text,
        latencyMs,
        mode: mode.mode,
        recalled: rec.entries,
      }
      setTurns((cur) => [...cur, turn])
      setSel(turn.id)
      setDraft('')
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  const current = turns.find((t) => t.id === sel)

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Playground</h1>
          <p>试一句话，看它怎么答 —— 走真实的 /api/talk，右侧是本轮的调试信息。</p>
        </div>
        <div className="spacer" />
        <div className="metrics">
          <span>
            <b>{turns.length}</b>轮
          </span>
        </div>
      </div>

      <div className="chars-toolbar">
        <select
          className="input"
          value={pid}
          onChange={(e) => setPid(e.target.value)}
          style={{ width: 200 }}
        >
          {npcs.length === 0 ? <option value="">（没有运行中的角色）</option> : null}
          {npcs.map((n) => (
            <option key={n.id} value={n.id}>
              {n.name || n.id}
            </option>
          ))}
        </select>
        <div className="spacer" />
        <button
          className="btn"
          disabled={turns.length === 0}
          onClick={() => {
            setTurns([])
            setSel(undefined)
          }}
        >
          清空对话
        </button>
      </div>

      {error ? <div className="banner error">{error}</div> : null}

      <div className="play-wrap">
        <div className="play-chat">
          {turns.length === 0 ? (
            <div className="state">
              <h3>还没有对话</h3>
              <p>在下面输入一句话试试 —— 答复与游戏里拿到的一致（同一个 /api/talk）。</p>
            </div>
          ) : (
            <div className="turns">
              {turns.map((t) => (
                <div
                  className={`turn${sel === t.id ? ' on' : ''}`}
                  key={t.id}
                  onClick={() => setSel(t.id)}
                >
                  <div className="bubble me">{t.message}</div>
                  <div className="bubble npc">
                    {t.error ? <span className="err">{t.error}</span> : t.reply || '（空答复）'}
                  </div>
                  <div className="turn-meta">
                    {t.latencyMs}ms · {t.mode || 'mode?'} · 召回 {t.recalled.length} 条
                  </div>
                </div>
              ))}
            </div>
          )}

          <div className="composer">
            <textarea
              className="input"
              rows={2}
              placeholder={pid ? '对角色说一句话…（Enter 发送）' : '先选一个角色'}
              value={draft}
              disabled={!pid}
              onChange={(e) => setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && !e.shiftKey) {
                  e.preventDefault()
                  send()
                }
              }}
            />
            <button className="btn primary" onClick={send} disabled={busy || !pid || !draft.trim()}>
              {busy ? '等待答复…' : '发送'}
            </button>
          </div>
        </div>

        <aside className="play-debug">
          <div className="sec-title">Debug</div>
          {!current ? (
            <p style={{ color: 'var(--text-dim)', fontSize: 13 }}>
              {turns.length ? '点左边任意一轮，看它的调试信息。' : '发一句话后这里会显示细节。'}
            </p>
          ) : (
            <>
              <div className="kv">
                <span className="k">延迟</span>
                <span className="v">{current.latencyMs} ms（前端实测往返）</span>
              </div>
              <div className="kv">
                <span className="k">模式</span>
                <span className="v">{current.mode || '—'}</span>
              </div>
              <div className="kv">
                <span className="k">角色</span>
                <span className="v">{pid}</span>
              </div>

              {current.thinking ? (
                <div className="sec">
                  <div className="sec-title">思考</div>
                  <div className="dbg-box">{current.thinking}</div>
                </div>
              ) : null}

              <div className="sec">
                <div className="sec-title">
                  召回预览（{current.recalled.length}）
                </div>
                <p className="hint">
                  按这句话检索记忆卡的 top-5 —— <b>是近似</b>，不等于 talk 内部真实上下文。
                </p>
                {current.recalled.length === 0 ? (
                  <div className="muted">没有召回（可能这张卡还是空的）</div>
                ) : (
                  current.recalled.map((m) => (
                    <div className="dbg-mem" key={m.id}>
                      <span className="chip">imp {m.importance}</span>
                      {m.content}
                    </div>
                  ))
                )}
              </div>

              <div className="sec">
                <div className="sec-title">原始答复</div>
                <div className="dbg-box">{current.reply || '（空）'}</div>
              </div>
            </>
          )}
        </aside>
      </div>
    </div>
  )
}
