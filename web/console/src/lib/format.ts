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
