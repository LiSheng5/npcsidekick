// 展示层小工具 —— 关键约束: 不假设任何游戏内容。
// 状态/关系类型都是运行时给的字符串，颜色由字符串哈希决定(稳定但不硬编码)。

const PALETTE = ['#16a34a', '#2563eb', '#7c3aed', '#b45309', '#dc2626', '#0891b2']

export function stateColor(state?: string): string {
  if (!state) return '#9a9aa5'
  let h = 0
  for (const ch of state) h = (h * 31 + ch.charCodeAt(0)) >>> 0
  return PALETTE[h % PALETTE.length]
}

export function titleCase(s: string): string {
  return s.length ? s[0].toUpperCase() + s.slice(1) : s
}

export function relativeTime(ts?: number): string {
  if (!ts) return ''
  const diff = Date.now() / 1000 - ts
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)} 分钟前`
  if (diff < 86400) return `${Math.floor(diff / 3600)} 小时前`
  return `${Math.floor(diff / 86400)} 天前`
}

/** 整数千分位；缺失/非数 → 占位符 '—'（诚实边界: 不编造、不确定就显示破折号）。 */
export function fmtInt(n?: number | null): string {
  if (n === undefined || n === null || Number.isNaN(n)) return '—'
  return n.toLocaleString('en-US')
}

/** 比例(0..1) → 百分比字符串；缺失 → '—'。 */
export function fmtPct(ratio?: number | null, digits = 1): string {
  if (ratio === undefined || ratio === null || Number.isNaN(ratio)) return '—'
  const v = ratio * 100
  const fixed = v.toFixed(digits)
  return `${fixed.replace(/\.0+$/, '')}%`
}

/** ISO 时间字符串 → 本地可读时间；非法 → 原样返回（不假装能格式化）。 */
export function isoTime(s?: string): string {
  if (!s) return ''
  const d = new Date(s)
  if (Number.isNaN(d.getTime())) return s
  return d.toLocaleString('zh-CN', { hour12: false })
}
