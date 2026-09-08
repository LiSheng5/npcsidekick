import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { RelationshipGraph as GraphCanvas } from '../components/RelationshipGraph'
import { NpcInspector } from '../components/NpcInspector'
import type { RelationshipGraph as GraphData } from '../types'

/** 首页: 关系图(真实数据) + 点节点出 Inspector。
 *  Runtime 没有关系数据时(source="none")明确告知，绝不用 demo 顶替。 */
export function GraphPage() {
  const [graph, setGraph] = useState<GraphData>()
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string>()
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<string>()

  const load = useCallback(async () => {
    try {
      const data = await api.relationships()
      setGraph(data)
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

  const selectedNode = graph?.nodes.find((n) => n.id === selected)

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>Relationship Graph</h1>
          <p>
            {graph?.source === 'none'
              ? 'Runtime 未提供关系数据 —— 下方节点为当前运行中的角色'
              : graph?.source === 'persona'
                ? '关系来自人设 JSON 的可选字段（制作者手填）'
                : '关系由 Runtime 提供'}
          </p>
        </div>
        <div className="spacer" />
        <div className="metrics">
          <span>
            <b>{graph?.nodes.length ?? 0}</b>Characters
          </span>
          <span>
            <b>{graph?.edges.length ?? 0}</b>Relationships
          </span>
        </div>
      </div>

      <div className="graph-wrap">
        <div className="graph-canvas">
          <div className="toolbar">
            <input
              className="input"
              placeholder="Search characters…"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              style={{ width: 240 }}
            />
            <button className="btn" onClick={load}>
              Refresh
            </button>
          </div>
          <GraphCanvas
            graph={graph}
            loading={loading}
            error={error}
            query={query}
            selectedId={selected}
            onSelect={setSelected}
          />
        </div>
        <NpcInspector
          node={selectedNode}
          edges={graph?.edges ?? []}
          nodes={graph?.nodes ?? []}
          onClose={() => setSelected(undefined)}
        />
      </div>
    </div>
  )
}
