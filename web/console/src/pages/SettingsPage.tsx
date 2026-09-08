import { useCallback, useEffect, useState } from 'react'
import { api } from '../api/client'
import { StateBlock } from '../components/StateBlock'
import type { ProvidersPayload } from '../types'

const BLANK = { id: '', name: '', base_url: '', model: '', api_key: '' }

/** Settings · AI Providers —— API Key 只在提交时发出，后端只回 masked。 */
export function SettingsPage() {
  const [data, setData] = useState<ProvidersPayload>()
  const [error, setError] = useState<string>()
  const [loading, setLoading] = useState(true)
  const [form, setForm] = useState({ ...BLANK })
  const [busy, setBusy] = useState(false)
  const [testOut, setTestOut] = useState<string>()

  const load = useCallback(async () => {
    try {
      setData(await api.providers())
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

  const submit = async () => {
    if (!form.id.trim()) return
    setBusy(true)
    try {
      await api.upsertProvider({
        id: form.id.trim(),
        name: form.name.trim() || form.id.trim(),
        base_url: form.base_url.trim(),
        model: form.model.trim(),
        api_key: form.api_key,
        activate: true,
      })
      setForm({ ...BLANK })
      await load()
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  if (loading && !data) return <StateBlock kind="loading" title="正在读取配置…" />
  if (error && !data) return <StateBlock kind="error" title="读取失败">{error}</StateBlock>

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1>AI Providers</h1>
          <p>
            OpenAI-compatible 接口 · 密钥存本地（
            {data?.encrypted ? '已加密' : '明文·已标注'}）· 只回显掩码
          </p>
        </div>
      </div>

      <div style={{ padding: '0 32px 32px', display: 'grid', gap: 18, maxWidth: 900 }}>
        <div className="card" style={{ padding: 18 }}>
          <div className="sec-title">当前生效</div>
          <div className="kv">
            <span className="k">Model</span>
            <span className="v">{data?.env.model || '—'}</span>
          </div>
          <div className="kv">
            <span className="k">Base URL</span>
            <span className="v">{data?.env.base_url || '（按模型名推断）'}</span>
          </div>
          <div className="kv">
            <span className="k">API Key</span>
            <span className="v">{data?.env.masked_key || '未配置'}</span>
          </div>
        </div>

        {(data?.providers ?? []).map((p) => (
          <div className="card" key={p.id} style={{ padding: 18 }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
              <b>{p.name}</b>
              {p.active ? <span className="chip">active</span> : null}
              <span className="chip">{p.model || 'no model'}</span>
              <div style={{ flex: 1 }} />
              <button
                className="btn"
                disabled={busy}
                onClick={async () => {
                  setBusy(true)
                  setTestOut(undefined)
                  const r = await api.testLlm({ provider_id: p.id })
                  setTestOut(
                    r.ok
                      ? `✓ ${r.model} · ${r.latency_ms}ms`
                      : `✗ ${r.error}`,
                  )
                  setBusy(false)
                }}
              >
                Test
              </button>
              <button
                className="btn"
                disabled={busy || p.active}
                onClick={async () => {
                  await api.activateProvider(p.id)
                  await load()
                }}
              >
                Activate
              </button>
              <button
                className="btn"
                disabled={busy}
                onClick={async () => {
                  await api.deleteProvider(p.id)
                  await load()
                }}
              >
                Delete
              </button>
            </div>
            <div className="kv" style={{ marginTop: 8 }}>
              <span className="k">Base URL</span>
              <span className="v">{p.base_url || '—'}</span>
            </div>
            <div className="kv">
              <span className="k">Key</span>
              <span className="v">{p.masked_key || '未配置'}</span>
            </div>
          </div>
        ))}
        {testOut ? <p style={{ color: 'var(--text-dim)' }}>{testOut}</p> : null}

        <div className="card" style={{ padding: 18 }}>
          <div className="sec-title">添加 / 更新 Provider</div>
          <div style={{ display: 'grid', gap: 10, gridTemplateColumns: '1fr 1fr' }}>
            <input
              className="input"
              placeholder="id（如 openai）"
              value={form.id}
              onChange={(e) => setForm({ ...form, id: e.target.value })}
            />
            <input
              className="input"
              placeholder="名称"
              value={form.name}
              onChange={(e) => setForm({ ...form, name: e.target.value })}
            />
            <input
              className="input"
              placeholder="Base URL（https://api.openai.com/v1）"
              value={form.base_url}
              onChange={(e) => setForm({ ...form, base_url: e.target.value })}
            />
            <input
              className="input"
              placeholder="Model"
              value={form.model}
              onChange={(e) => setForm({ ...form, model: e.target.value })}
            />
            <input
              className="input"
              type="password"
              placeholder="API Key（只存本地）"
              value={form.api_key}
              onChange={(e) => setForm({ ...form, api_key: e.target.value })}
              style={{ gridColumn: '1 / -1' }}
            />
          </div>
          <button
            className="btn primary"
            style={{ marginTop: 12 }}
            disabled={busy || !form.id.trim()}
            onClick={submit}
          >
            保存并激活
          </button>
        </div>
      </div>
    </div>
  )
}
