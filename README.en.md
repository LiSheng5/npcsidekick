# NPCSidekick v4

[简体中文](./README.md) | [English](./README.en.md)

The brain of a universal in-game AI NPC system — a local Python server that any
game with an HTTP-capable mod can plug into.

**Core principle: the brain proposes, the game executes.** The LLM only
*proposes* actions (structured fields, fully separated from conversation text);
execution belongs to the game. The only gate on the brain's side is the
**capability whitelist** the mod declares at registration — the brain can only
propose actions the mod said it can perform.

## Features

- 🧠 **Memory cards** — deterministic forgetting (strength decays with a
  half-life), pin to exempt, zero LLM cost
- 🔌 **Vendor-agnostic** — OpenAI-compatible endpoints only: DeepSeek / OpenAI /
  Qwen / Kimi / GLM / Ollama / OpenRouter via three environment variables
- 🎮 **Capability whitelist** — mods declare what they can do; the brain can
  only propose that
- 🖥️ **Zero-build console** — real streaming try-it-out chat, memory card
  management, relationship graph, simulated mod registration (test without
  launching the game)
- 🗣️ Optional TTS (edge-tts), rolling summarization, multi-persona (drop a JSON
  into `personas/` to add an NPC)

## Quick start

```bash
pip install -r requirements.txt

# Brain trio (the key can also go into api_key.txt at the project root)
export AGENT_MODEL=deepseek-v4-flash
export NPC_API_KEY=sk-xxx
export NPC_BASE_URL=https://api.deepseek.com

python -m uvicorn server:app --host 127.0.0.1 --port 8765
```

Open **http://127.0.0.1:8765/console/** for the debug console. The server also
starts without the trio configured — in that mode conversation falls back to
the persona's `rules` reply, no actions are proposed, and the console shows
exactly which variables are missing. The server binds to `127.0.0.1` only.

## The four endpoints

```bash
# 1) Mod registration: declare the capability whitelist + heartbeat
curl -X POST http://127.0.0.1:8765/api/capabilities -H "Content-Type: application/json" \
  -d '{"mod":"sims4","actions":[{"name":"cook","desc":"cook in the kitchen","params":{"dish":"string"}}]}'

# 2) Talk (SSE: delta / action / audio / done frames)
curl -X POST http://127.0.0.1:8765/api/talk -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","message":"Can you cook me something?","mod":"sims4",
       "observation":{"summary":"Player in the kitchen, just off work, a bit hungry"}}'

# 3) Action result feedback (deterministically written to memory: "done: …" / "failed: …")
curl -X POST http://127.0.0.1:8765/api/action_result -H "Content-Type: application/json" \
  -d '{"npc_id":"cang","action":"cook","ok":true,"note":"noodles ready, player liked them"}'

# 4) State
curl http://127.0.0.1:8765/api/state
```

## Docs

| Doc | Contents |
|---|---|
| [配置详解.md](./配置详解.md) | Full model-vendor table / API key protection / all environment variables / memory card format (Chinese) |
| [协议.md](./协议.md) | The mod ⇄ brain HTTP contract — authoritative endpoints & fields (Chinese) |
| [设计.md](./设计.md) | Architecture and design decisions (Chinese) |
| [MOD_交接简报.md](./MOD_交接简报.md) | Integration brief for mod developers (Chinese) |
| [待办.md](./待办.md) | Roadmap (Chinese) |

> The deep-dive docs are currently Chinese-only. The quick start above plus the
> four endpoints are enough to integrate; open an issue if you need an
> English contract document.

## Tests

```bash
python -m pytest tests -q
```

Fully offline: every LLM is injected via the `FakeProvider` in
`tests/conftest.py`.

## License

MIT
