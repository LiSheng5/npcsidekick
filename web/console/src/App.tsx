import { useState } from 'react'
import { GraphPage } from './pages/GraphPage'
import { SettingsPage } from './pages/SettingsPage'

const NAV = [
  { key: 'graph', label: 'Graph', ready: true },
  { key: 'characters', label: 'Characters', ready: false },
  { key: 'live', label: 'Live', ready: false },
  { key: 'memory', label: 'Memory', ready: false },
  { key: 'activity', label: 'Activity', ready: false },
  { key: 'playground', label: 'Playground', ready: false },
  { key: 'settings', label: 'Settings', ready: true },
] as const

export default function App() {
  const [page, setPage] = useState<string>('graph')

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
        {page === 'graph' ? <GraphPage /> : null}
        {page === 'settings' ? <SettingsPage /> : null}
      </main>
    </div>
  )
}
