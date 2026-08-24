# Godot 客户端 — 实战参考（不重复造轮子）

已上线的完整实现就在游戏项目里（`C:\Users\Administrator\Documents\新建游戏项目`），
直接读源码，比任何骨架都真实：

| 模式 | 文件 | 看什么 |
|---|---|---|
| API+本地兜底+Timer 竞争超时 | `scripts/npc/DialogueUI.gd` | `_request()` 的 timeout 竞争与 fallback 文案 |
| 外部大脑镜像轮询 | `scripts/npc/Villager.gd` | 2s 轮询 `/api/state` 同步 position/activity |
| 自动拉起大脑服务器 | `scripts/world/WorldManager.gd` | 进世界检测 8765, 没开就拉 bat |

## 已知改进点（下一个版本）
- [ ] WorldManager 里大脑地址是硬编码 `D:\Projects\Dagent` → 应进项目设置
- [ ] Villager.gd 轮询逻辑可抽成共享单例（多 NPC 共用一个 Timer）
- [ ] 可选迁移到 SSE `/api/events/stream`（协议见 ../common/PROTOCOL.md §2.4）
