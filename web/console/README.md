# NPCSidekick Web Console（前端）

Runtime 的开发者工具层：Visualization + Character Editor + Runtime Monitor + LLM Playground。
**不是**新的 NPC 引擎，也不提供游戏协议——它只是和 Game、CLI 并列的第三个消费者。

## 跑起来

```bash
# 1) 先启动大脑（另一个终端）
python -m npc.server                 # 127.0.0.1:8765

# 2) 开发模式（5173，/api 代理到 8765）
npm install
npm run dev                          # http://127.0.0.1:5173
```

生产/日常用法不需要起 dev server：

```bash
npm run build                        # → dist/
python -m npc.server                 # 打开 http://127.0.0.1:8765/
```

`npc/server.py` 会把 `dist/` 挂在 `/console/` 下，根路径 `/` 重定向过去
（dist 不存在时返回提示 JSON，**不再回落旧 `npc.html`** —— 该页 2026-09-08 已下架）。`dist/` 与 `node_modules/` 都不入库。

## 结构

```
src/
  pages/         Graph · Characters · Live · Memory · Activity · Playground · Settings（7 页全开）
  components/    RelationshipGraph · NpcNode · NpcInspector · StateBlock
  api/client.ts  与 Runtime 的全部 HTTP 调用
  types.ts       与后端响应一一对应的类型
  lib/format.ts  展示层工具（状态色按字符串哈希，不硬编码任何状态类型）
  styles.css     设计系统
```

## 三条硬规矩

1. **零游戏内容**：不出现任何具体游戏名词；状态、关系类型、动作名一律当字符串渲染。
2. **Runtime 没给的数据就明说没有**：`/api/relationships` 返回 `source:"none"` 时，
   UI 明确提示"Runtime 未提供关系数据"，绝不用 demo 顶替。
3. **每个数据视图都有 loading / empty / error 三态**（`components/StateBlock.tsx`）。

## 视觉

白/灰白底、深字、细灰边、柔和阴影、大留白、大字号。
明确不做：neon、渐变堆叠、发光描边、暗色 cyberpunk、满屏图表。
只保留少量 accent 色，其余靠排版与留白分层次。
