// common/fetch 示例 — 任意 JS 环境（浏览器 / Node）接大脑
// 用法在 docs/跨引擎适配.md §3 契约不变；这是最简接入。
async function talk(npcId, message, voice = true) {
  const resp = await fetch('http://127.0.0.1:8765/api/talk', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ npc_id: npcId, message, voice }),
  });
  const data = await resp.json();
  if (data.audio) {
    const bytes = Uint8Array.from(atob(data.audio), c => c.charCodeAt(0));
    return { reply: data.reply, audio: bytes }; // audio 可喂给 WebAudio
  }
  return { reply: data.reply };
}

async function pollState() {
  const resp = await fetch('http://127.0.0.1:8765/api/state');
  return resp.json(); // { actors, delivered, tick, log_tail }
}

// 事件协议（Codex core/界面协议化对照）: 界面只消费事件, 不做状态差分。
// 客户端持有 logCount 游标, 取 since 之后的新事件驱动气泡/表演/交付动画。
async function pollEvents(since = 0) {
  const resp = await fetch(`http://127.0.0.1:8765/api/events?since=${since}`);
  return resp.json(); // { events:[{type,npc,...}], logCount, tick, delivered }
}

// 事件轮询示例: 用返回的 logCount 当游标, 只处理新增事件。
let cursor = 0;
async function pollLoop() {
  const data = await pollEvents(cursor);
  for (const ev of data.events) {
    if (ev.type === 'say') console.log(`[气泡] ${ev.npc}: ${ev.text}`);
    else if (ev.type === 'deliver') console.log(`[交付] ${ev.npc} 给了主角 ${ev.resource}`);
    else console.log('[事件]', ev);
  }
  cursor = data.logCount; // 游标推进
  setTimeout(pollLoop, 2000);
}
pollLoop();
