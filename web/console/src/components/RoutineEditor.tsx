import { useEffect, useId, useState } from 'react'
import { api } from '../api/client'
import type { RoutineItem } from '../types'

/** routine（自主日常表）编辑器 —— 增删改 + 排序。

  game-agnostic 铁律:
    · action 是**自由文本**，下拉建议来自 GET /api/actions（Runtime 动态给），
      UI 不硬编码任何动作名；
    · 每项的未知字段（制作者自己加的）原样保留，只在下方标出来提示去 JSON 模式编辑，
      绝不静默丢弃 —— 表单与 JSON 看的是同一份数据。
*/
interface Props {
  items: RoutineItem[]
  onChange: (items: RoutineItem[]) => void
}

// 常见字段（只是排版顺序，不是 schema 限制；缺了就留空，多了照样保留）
const KNOWN = ['action', 'resource', 'count', 'ticks', 'weight'] as const

function extraKeys(item: RoutineItem): string[] {
  return Object.keys(item).filter((k) => !KNOWN.includes(k as (typeof KNOWN)[number]))
}

export function RoutineEditor({ items, onChange }: Props) {
  const [suggest, setSuggest] = useState<string[]>([])
  const listId = useId()

  useEffect(() => {
    let alive = true
    api
      .actions()
      .then((d) => {
        if (alive) setSuggest(Array.from(new Set([...d.runtime, ...d.observed])))
      })
      .catch(() => setSuggest([])) // 拿不到建议也不该挡住编辑
    return () => {
      alive = false
    }
  }, [])

  const patch = (i: number, part: Partial<RoutineItem>) =>
    onChange(items.map((it, idx) => (idx === i ? { ...it, ...part } : it)))

  const remove = (i: number) => onChange(items.filter((_, idx) => idx !== i))

  const move = (i: number, dir: -1 | 1) => {
    const j = i + dir
    if (j < 0 || j >= items.length) return
    const next = [...items]
    const a = next[i]
    const b = next[j]
    next[i] = b
    next[j] = a
    onChange(next)
  }

  return (
    <div className="routine">
      {items.length === 0 ? (
        <p className="hint">还没有日常安排 —— 空数组 = NPC 静止不动（合法状态）。</p>
      ) : null}

      {items.map((item, i) => (
        <div className="routine-row" key={i}>
          <div className="routine-move">
            <button
              className="btn tiny"
              onClick={() => move(i, -1)}
              disabled={i === 0}
              title="上移"
            >
              ↑
            </button>
            <button
              className="btn tiny"
              onClick={() => move(i, 1)}
              disabled={i === items.length - 1}
              title="下移"
            >
              ↓
            </button>
          </div>

          <label className="fld grow">
            <span>action</span>
            <input
              className="input"
              list={listId}
              value={item.action ?? ''}
              placeholder="任意动作名"
              onChange={(e) => patch(i, { action: e.target.value })}
            />
          </label>

          <label className="fld">
            <span>resource</span>
            <input
              className="input narrow"
              value={String(item.resource ?? '')}
              onChange={(e) => patch(i, { resource: e.target.value })}
            />
          </label>

          {(['count', 'ticks', 'weight'] as const).map((k) => (
            <label className="fld" key={k}>
              <span>{k}</span>
              <input
                className="input x-narrow"
                type="number"
                value={item[k] === undefined ? '' : String(item[k])}
                onChange={(e) => {
                  const raw = e.target.value.trim()
                  if (!raw) {
                    // 清空 = 删掉这个键（而不是留个空串/0，免得校验报"必须是正数"）
                    const next = { ...item }
                    delete next[k]
                    patch(i, next)
                    return
                  }
                  patch(i, { [k]: Number(raw) })
                }}
              />
            </label>
          ))}

          <button className="btn tiny danger" onClick={() => remove(i)} title="删除这一项">
            ✕
          </button>

          {extraKeys(item).length > 0 ? (
            <div className="routine-extra">
              额外字段（JSON 模式编辑）: {extraKeys(item).join('、')}
            </div>
          ) : null}
        </div>
      ))}

      <datalist id={listId}>
        {suggest.map((s) => (
          <option value={s} key={s} />
        ))}
      </datalist>

      <button
        className="btn"
        onClick={() => onChange([...items, { action: '' }])}
      >
        + Add step
      </button>
      {suggest.length > 0 ? (
        <p className="hint">
          建议（来自 Runtime，非白名单）: {suggest.join('、')}
          {' —— 可以填任意值，能否执行由 Runtime 决定。'}
        </p>
      ) : null}
    </div>
  )
}
