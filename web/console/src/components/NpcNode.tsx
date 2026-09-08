import { Handle, Position, type NodeProps } from '@xyflow/react'
import type { GraphNode } from '../types'
import { stateColor } from '../lib/format'

export type NpcNodeData = {
  node: GraphNode
  selected?: boolean
  dimmed?: boolean
}

/** 关系图节点: 只渲染 Runtime 给的字段，不假设头像/职业/阵营。 */
export function NpcNode({ data }: NodeProps) {
  const { node, dimmed } = data as unknown as NpcNodeData
  const identity = (node.metadata?.identity as string) || ''
  const activity = (node.metadata?.activity as string) || ''
  return (
    <div className={`node-card${dimmed ? ' dimmed' : ''}`}>
      <Handle type="target" position={Position.Left} style={{ opacity: 0 }} />
      <div className="nm">
        <span className="dot" style={{ background: stateColor(node.state) }} />
        <span>{node.name}</span>
      </div>
      <div className="rl">{identity || activity || node.id}</div>
      <div className="ty">{node.state ? `${node.state}` : node.type}</div>
      <Handle type="source" position={Position.Right} style={{ opacity: 0 }} />
    </div>
  )
}
