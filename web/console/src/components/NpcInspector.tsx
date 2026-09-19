import type { GraphEdge, GraphNode } from '../types'
import { stateColor } from '../lib/format'

/** 点节点后的右侧 Inspector —— 数据全部来自 /api/relationships，无一句游戏内容。 */
export function NpcInspector({
  node,
  edges,
  nodes,
  onClose,
  onNavigate,
}: {
  node?: GraphNode
  edges: GraphEdge[]
  nodes: GraphNode[]
  onClose: () => void
  /** 跳到别的页并聚焦该角色（Console 内部导航，无路由库）。 */
  onNavigate?: (page: string, npcId: string) => void
}) {
  if (!node) {
    return (
      <aside className="inspector">
        <div className="sec-title">Inspector</div>
        <p style={{ color: 'var(--text-dim)', fontSize: 13 }}>
          点击画布上的角色，查看它的身份、状态与关系。
        </p>
      </aside>
    )
  }

  const nameOf = (id: string) => nodes.find((n) => n.id === id)?.name ?? id
  const outgoing = edges.filter((e) => e.source === node.id)
  const incoming = edges.filter((e) => e.target === node.id)
  const meta = node.metadata ?? {}

  return (
    <aside className="inspector">
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 10 }}>
        <div style={{ minWidth: 0, flex: 1 }}>
          <h2>{node.name}</h2>
          <div className="sub">
            {node.state ? (
              <>
                <span
                  className="dot"
                  style={{ background: stateColor(node.state), marginRight: 6 }}
                />
                {node.state}
              </>
            ) : (
              node.type
            )}
          </div>
        </div>
        <button className="btn" onClick={onClose} aria-label="关闭">
          ✕
        </button>
      </div>

      <div className="sec">
        <div className="sec-title">Identity</div>
        <div className="kv">
          <span className="k">ID</span>
          <span className="v">{node.id}</span>
        </div>
        {Object.entries(meta)
          .filter(([k, v]) => !k.startsWith('_') && typeof v !== 'object' && Boolean(v))
          .map(([k, v]) => (
            <div className="kv" key={k}>
              <span className="k">{k}</span>
              <span className="v">{String(v)}</span>
            </div>
          ))}
      </div>

      <div className="sec">
        <div className="sec-title">Relationships · {outgoing.length + incoming.length}</div>
        {outgoing.length + incoming.length === 0 ? (
          <p style={{ color: 'var(--text-dim)', fontSize: 13, margin: 0 }}>
            Runtime 没有为它提供关系数据。
          </p>
        ) : (
          <>
            {outgoing.map((e, i) => (
              <div className="rel-row" key={`o${i}`}>
                <span className="arrow">→</span>
                <span>{e.type}</span>
                <span style={{ marginLeft: 'auto', color: 'var(--text-dim)' }}>
                  {nameOf(e.target)}
                  {e.weight != null ? ` · ${e.weight}` : ''}
                </span>
              </div>
            ))}
            {incoming.map((e, i) => (
              <div className="rel-row" key={`i${i}`}>
                <span className="arrow">←</span>
                <span>{e.type}</span>
                <span style={{ marginLeft: 'auto', color: 'var(--text-dim)' }}>
                  {nameOf(e.source)}
                  {e.weight != null ? ` · ${e.weight}` : ''}
                </span>
              </div>
            ))}
          </>
        )}
      </div>

      <div className="inspector-actions">
        <button
          className="btn"
          disabled={!onNavigate}
          title={onNavigate ? '编辑这个人设' : '不可用'}
          onClick={() => onNavigate?.('characters', node.id)}
        >
          Character
        </button>
        <button
          className="btn"
          disabled={!onNavigate}
          title={onNavigate ? '查看这个角色的记忆卡' : '不可用'}
          onClick={() => onNavigate?.('memory', node.id)}
        >
          Memory
        </button>
      </div>
    </aside>
  )
}
