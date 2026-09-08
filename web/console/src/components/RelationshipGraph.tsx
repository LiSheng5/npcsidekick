import { useMemo } from 'react'
import {
  Background,
  BackgroundVariant,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
} from '@xyflow/react'
import '@xyflow/react/dist/style.css'
import { NpcNode, type NpcNodeData } from './NpcNode'
import { StateBlock } from './StateBlock'
import type { GraphEdge, GraphNode, RelationshipGraph } from '../types'

const nodeTypes = { npc: NpcNode }

/** 环形布局: Runtime 不提供坐标时给一个稳定的初始摆位（可自由拖动）。 */
function layout(nodes: GraphNode[]): Record<string, { x: number; y: number }> {
  const n = nodes.length
  const out: Record<string, { x: number; y: number }> = {}
  if (!n) return out
  const radius = Math.max(220, n * 42)
  nodes.forEach((node, i) => {
    const angle = (2 * Math.PI * i) / n
    out[node.id] = {
      x: 420 + radius * Math.cos(angle),
      y: 300 + radius * Math.sin(angle),
    }
  })
  return out
}

export function RelationshipGraph({
  graph,
  loading,
  error,
  query,
  selectedId,
  onSelect,
}: {
  graph?: RelationshipGraph
  loading: boolean
  error?: string
  query: string
  selectedId?: string
  onSelect: (id: string) => void
}) {
  const pos = useMemo(() => layout(graph?.nodes ?? []), [graph?.nodes])

  const nodes = useMemo<Node[]>(() => {
    const q = query.trim().toLowerCase()
    return (graph?.nodes ?? []).map((n) => ({
      id: n.id,
      type: 'npc',
      position: pos[n.id] ?? { x: 0, y: 0 },
      data: {
        node: n,
        dimmed: Boolean(q) && !`${n.name} ${n.id} ${n.state ?? ''}`.toLowerCase().includes(q),
      } satisfies NpcNodeData,
      selected: n.id === selectedId,
    }))
  }, [graph?.nodes, pos, query, selectedId])

  const edges = useMemo<Edge[]>(
    () =>
      (graph?.edges ?? []).map((e: GraphEdge, i) => ({
        id: `e${i}-${e.source}-${e.target}`,
        source: e.source,
        target: e.target,
        label: e.type,
        animated: false,
        style: { stroke: '#c9c9d4', strokeWidth: 1.5 },
        labelStyle: { fontSize: 11, fill: '#6b6b76' },
        labelBgStyle: { fill: '#ffffff' },
        labelBgPadding: [6, 3] as [number, number],
        labelBgBorderRadius: 6,
        markerEnd: { type: MarkerType.ArrowClosed, width: 14, height: 14, color: '#c9c9d4' },
      })),
    [graph?.edges],
  )

  if (loading && !graph) return <StateBlock kind="loading" title="正在连接 Runtime…" />
  if (error) return <StateBlock kind="error" title="连不上 Runtime" >{error}</StateBlock>
  if (!graph || graph.nodes.length === 0)
    return (
      <StateBlock kind="empty" title="没有发现角色">
        启动 NPCSidekick 后把人设放进 npc/personas/，这里会自动列出。
      </StateBlock>
    )

  return (
    <ReactFlow
      nodes={nodes}
      edges={edges}
      nodeTypes={nodeTypes}
      onNodeClick={(_, n) => onSelect(n.id)}
      fitView
      fitViewOptions={{ padding: 0.25 }}
      minZoom={0.2}
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={22} size={1} color="#e4e4ec" />
      <Controls showInteractive={false} />
    </ReactFlow>
  )
}
