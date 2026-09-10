import { useState } from 'react'
import { GraphPage } from './pages/GraphPage'
import { CharactersPage } from './pages/CharactersPage'
import { MemoryPage } from './pages/MemoryPage'
import { LivePage } from './pages/LivePage'
import { ActivityPage } from './pages/ActivityPage'
import { PlaygroundPage } from './pages/PlaygroundPage'
import { SettingsPage } from './pages/SettingsPage'

const NAV = [
  { key: 'graph', label: 'Graph', ready: true },
  { key: 'characters', label: 'Characters', ready: true },
  { key: 'live', label: 'Live', ready: true },
  { key: 'memory', label: 'Memory', ready: true },
  { key: 'activity', label: 'Activity', ready: true },
  { key: 'playground', label: 'Playground', ready: true },
  { key: 'settings', label: 'Settings', ready: true },
] as const

export default function App() {
  const [page, setPage] = useState<string>('graph')
  /** 跨页聚焦的角色 id —— Graph 里点"Memory"就带着它跳过去，不用再选一遍。
      seq 让"连续两次点同一个角色"也能被目标页感知（值相同也要触发一次）。 */
  const [focus, setFocus] = useState<{ id: string; seq: number }>()

  const navigate = (next: string, npcId?: string) => {
    if (npcId) setFocus({ id: npcId, seq: Date.now() })
    setPage(next)
  }

  return (
    <div className="app">
      <nav className="sidebar">
        <div className="brand">
          <span className="brand-mark">N</span>
          <span className="brand-text">
            NPCSidekick
            <div className="brand-sub">Console</div>
          </span>
        </div>
        {NAV.map((item) => (
          <button
            key={item.key}
            className={`nav-item${page === item.key ? ' active' : ''}${
              item.ready ? '' : ' disabled'
            }`}
            disabled={!item.ready}
            onClick={() => setPage(item.key)}
          >
            <span className="label">{item.label}</span>
            {!item.ready ? <span className="tag">规划中</span> : null}
          </button>
        ))}
      </nav>
      <main className="main">
        {page === 'graph' ? <GraphPage onNavigate={navigate} /> : null}
        {page === 'characters' ? <CharactersPage focus={focus} /> : null}
        {page === 'memory' ? <MemoryPage focus={focus?.id} /> : null}
        {page === 'live' ? <LivePage /> : null}
        {page === 'activity' ? <ActivityPage /> : null}
        {page === 'playground' ? <PlaygroundPage /> : null}
        {page === 'settings' ? <SettingsPage /> : null}
      </main>
    </div>
  )
}
