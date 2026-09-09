import type { LiveEvent } from '../types'

/** 单条事件行 —— Live 与 Activity 共用。

    game-agnostic: 只做"类型徽章 + 角色 + 字段值"的渲染，**不解释语义**
    （不写"XX 采集了 YY"这种句式）。未知 type 兜底列出所有非空字段 ——
    换游戏 / 加新事件类型都不会显示成空白，也不必改这里。 */
export function EventLine({ ev }: { ev: LiveEvent }) {
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

/** 事件的"可搜索文本" —— 搜索/匹配用，把有语义的字段拼起来。 */
export function eventText(ev: LiveEvent): string {
  return [
    ev.type,
    ev.npc ?? '',
    ev.text ?? '',
    ev.dest ?? '',
    ev.resource ?? '',
    ev.product ?? '',
    ev.to ?? '',
    ev.desc ?? '',
    ev.agent ?? '',
  ]
    .join(' ')
    .toLowerCase()
}
