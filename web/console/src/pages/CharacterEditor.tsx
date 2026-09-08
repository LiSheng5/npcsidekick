import { useState } from 'react'
import { api } from '../api/client'
import { RoutineEditor } from '../components/RoutineEditor'
import type { Persona } from '../types'

/** Character 编辑器 —— 覆盖 persona 全字段，routine 可增删改排序。

  两条硬约束（来自 Step 5 需求）:
    1. **不丢字段**: 始终基于完整 persona 对象做浅拷贝修改，表单没画的字段原样保留；
       结构化字段（desires/goals/rules）用 "key = value" 文本编辑，值是对象时直接显示 JSON。
    2. **表单与 JSON 同一数据源**: View JSON 只是同一份 draft 的另一种视图，
       切过去是序列化、切回来是解析，不存在"两份数据"。
*/
interface Props {
  persona: Persona
  /** 新建态: id 可编辑，保存前做本地必填预检（后端 PUT 本身是 upsert，创建/更新同一条路径）。 */
  isNew?: boolean
  onBack: () => void
  onSaved?: (p: Persona) => void
}

const ID_RE = /^[A-Za-z0-9_-]{1,32}$/
// 与后端 REQUIRED_FIELDS 对齐（taboos 允许空数组，故不在此列）
const REQUIRED_TEXT = ['identity', 'personality', 'speech_style'] as const

// ── 结构化字段 <-> 文本 ─────────────────────────────
function kvToText(obj: unknown): string {
  if (!obj || typeof obj !== 'object') return ''
  return Object.entries(obj as Record<string, unknown>)
    .map(([k, v]) => `${k} = ${typeof v === 'object' ? JSON.stringify(v) : String(v)}`)
    .join('\n')
}

function textToKv(text: string): Record<string, unknown> {
  const out: Record<string, unknown> = {}
  for (const line of text.split('\n')) {
    const s = line.trim()
    if (!s) continue
    const i = s.indexOf('=')
    const k = i > 0 ? s.slice(0, i).trim() : ''
    if (!k) continue // 没有 key 的行直接忽略 —— 不猜用户意图
    const raw = i > 0 ? s.slice(i + 1).trim() : ''
    if (raw.startsWith('{') || raw.startsWith('[')) {
      try {
        out[k] = JSON.parse(raw)
        continue
      } catch {
        /* 解析不了就当普通字符串，不吞掉用户输入 */
      }
    }
    const n = Number(raw)
    out[k] = raw !== '' && !Number.isNaN(n) ? n : raw
  }
  return out
}

const tagsToText = (a?: string[]) => (a ?? []).join('、')
const textToTags = (s: string) =>
  s
    .split(/[、,，\n]/)
    .map((x) => x.trim())
    .filter(Boolean)

export function CharacterEditor({ persona, isNew, onBack, onSaved }: Props) {
  const [draft, setDraft] = useState<Persona>(persona)
  const [mode, setMode] = useState<'form' | 'json'>('form')
  const [jsonText, setJsonText] = useState(() => JSON.stringify(persona, null, 2))
  const [saving, setSaving] = useState(false)
  const [msg, setMsg] = useState<string>()
  const [err, setErr] = useState<string>()

  const set = <K extends keyof Persona>(k: K, v: Persona[K]) =>
    setDraft((d) => ({ ...d, [k]: v }))

  const rules = draft.rules ?? {}

  const save = async () => {
    setSaving(true)
    setErr(undefined)
    setMsg(undefined)
    // 本地预检: 后端会 422，但在这里说人话更省事（尤其新建时 id 还没落盘）
    const missing: string[] = []
    if (!String(draft.id ?? '').trim()) missing.push('id')
    else if (!ID_RE.test(draft.id)) {
      setErr('id 不合法: 1~32 位字母/数字/_/-（id 会作为文件名，别用中文和空格）')
      setSaving(false)
      return
    }
    for (const k of REQUIRED_TEXT) {
      if (!String(draft[k] ?? '').trim()) missing.push(k)
    }
    if (missing.length) {
      setErr(`缺少必填字段: ${missing.join('、')}（taboos 允许为空）`)
      setSaving(false)
      return
    }
    try {
      const r = await api.updatePersona(draft.id, draft)
      const saved = r.persona ?? draft
      setDraft(saved)
      setJsonText(JSON.stringify(saved, null, 2))
      setMsg(isNew ? '已创建，并已热加载到运行中的世界' : '已保存，并已热加载到运行中的世界')
      onSaved?.(saved)
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setSaving(false)
    }
  }

  const toJson = () => {
    setJsonText(JSON.stringify(draft, null, 2))
    setErr(undefined)
    setMode('json')
  }

  const fromJson = () => {
    try {
      const parsed = JSON.parse(jsonText) as Persona
      if (!parsed || typeof parsed !== 'object') throw new Error('顶层必须是对象')
      setDraft(parsed)
      setErr(undefined)
      setMode('form')
    } catch (e) {
      setErr(`JSON 解析失败（草稿未改动）: ${e instanceof Error ? e.message : e}`)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>{isNew ? 'New Character' : draft.name || draft.id}</h1>
          <p>
            {isNew
              ? '填好必填字段后保存即创建（id、identity、personality、speech_style）'
              : draft.identity || '（未填写 identity）'}
            {!isNew ? (
              <>
                {' · '}
                <code>{draft.id}</code>
              </>
            ) : null}
          </p>
        </div>
        <div className="spacer" />
        <button className="btn" onClick={onBack}>
          ← Characters
        </button>
        <button
          className="btn"
          onClick={() => (mode === 'form' ? toJson() : fromJson())}
        >
          {mode === 'form' ? 'View JSON' : 'Back to form'}
        </button>
        <button className="btn primary" onClick={save} disabled={saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>

      {err ? <div className="banner error">{err}</div> : null}
      {msg ? <div className="banner ok">{msg}</div> : null}

      {mode === 'json' ? (
        <div className="editor-json">
          <textarea
            className="input json-area"
            value={jsonText}
            onChange={(e) => setJsonText(e.target.value)}
            spellCheck={false}
          />
          <p className="hint">
            编辑后点上方 “Back to form” 解析回表单。解析失败不会破坏当前草稿。
          </p>
        </div>
      ) : (
        <div className="editor-form">
          <section className="grp">
            <div className="grp-title">Identity</div>
            <label className="fld">
              <span>id</span>
              <input
                className="input"
                value={draft.id}
                readOnly={!isNew}
                placeholder={isNew ? '例如：blacksmith' : ''}
                onChange={(e) => set('id', e.target.value)}
              />
              <em>
                {isNew
                  ? '1~32 位字母/数字/_/-（会作为文件名）'
                  : '创建后不可改（改 id 等于新建角色）'}
              </em>
            </label>
            <label className="fld">
              <span>name</span>
              <input
                className="input"
                value={draft.name ?? ''}
                onChange={(e) => set('name', e.target.value)}
              />
            </label>
            <label className="fld wide">
              <span>identity</span>
              <textarea
                className="input"
                rows={2}
                value={draft.identity ?? ''}
                onChange={(e) => set('identity', e.target.value)}
              />
            </label>
          </section>

          <section className="grp">
            <div className="grp-title">Voice</div>
            <label className="fld wide">
              <span>personality</span>
              <textarea
                className="input"
                rows={3}
                value={draft.personality ?? ''}
                onChange={(e) => set('personality', e.target.value)}
              />
            </label>
            <label className="fld wide">
              <span>speech_style</span>
              <textarea
                className="input"
                rows={2}
                value={draft.speech_style ?? ''}
                onChange={(e) => set('speech_style', e.target.value)}
              />
            </label>
          </section>

          <section className="grp">
            <div className="grp-title">Boundaries</div>
            <label className="fld wide">
              <span>taboos</span>
              <input
                className="input"
                value={tagsToText(draft.taboos)}
                placeholder="用 、或逗号分隔"
                onChange={(e) => set('taboos', textToTags(e.target.value))}
              />
              <em>分隔：、，, 或换行</em>
            </label>
          </section>

          <section className="grp">
            <div className="grp-title">Motivation</div>
            <label className="fld wide">
              <span>desires</span>
              <textarea
                className="input"
                rows={3}
                placeholder={'每行一条，如：\n被需要 = 8'}
                value={kvToText(draft.desires)}
                onChange={(e) =>
                  set('desires', textToKv(e.target.value) as Persona['desires'])
                }
              />
            </label>
            <label className="fld wide">
              <span>goals</span>
              <textarea
                className="input"
                rows={3}
                placeholder={'每行一条，如：\n修好渡口 = 3'}
                value={kvToText(draft.goals)}
                onChange={(e) =>
                  set('goals', textToKv(e.target.value) as Persona['goals'])
                }
              />
              <em>值写成 {"{"}"progress": 1, "target": 3{"}"} 这类对象也支持</em>
            </label>
          </section>

          <section className="grp">
            <div className="grp-title">Rules</div>
            <label className="fld wide">
              <span>fallback</span>
              <input
                className="input"
                value={rules.fallback ?? ''}
                onChange={(e) => set('rules', { ...rules, fallback: e.target.value })}
              />
            </label>
            <label className="fld wide">
              <span>replies</span>
              <textarea
                className="input"
                rows={3}
                placeholder={'每行一条，如：\n给木材 = 好，我去砍'}
                value={kvToText(rules.replies)}
                onChange={(e) =>
                  set('rules', { ...rules, replies: textToKv(e.target.value) as never })
                }
              />
            </label>
          </section>

          <section className="grp">
            <div className="grp-title">
              Routine <span className="grp-hint">自主日常（可增删改排序）</span>
            </div>
            <RoutineEditor
              items={draft.routine ?? []}
              onChange={(items) => set('routine', items)}
            />
          </section>

          <section className="grp advanced">
            <div className="grp-title">
              Advanced <span className="grp-hint">不懂就别动</span>
            </div>
            <label className="fld wide">
              <span>context_extra</span>
              <textarea
                className="input"
                rows={2}
                value={draft.context_extra ?? ''}
                onChange={(e) => set('context_extra', e.target.value)}
              />
            </label>
            <label className="fld wide">
              <span>system_prompt_override</span>
              <textarea
                className="input"
                rows={4}
                placeholder="留空 = 用 schema 自动拼装的 system prompt"
                value={draft.system_prompt_override ?? ''}
                onChange={(e) => set('system_prompt_override', e.target.value)}
              />
              <em>填了就完全接管 system prompt（自动拼装失效）</em>
            </label>
          </section>
        </div>
      )}
    </div>
  )
}
