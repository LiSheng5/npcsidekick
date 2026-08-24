# Unreal 客户端骨架（占位）

模式与 `../unity/NPCSidekickClient.cs` 一一对应（协议唯一真相在
`../common/PROTOCOL.md`），差异点：

| 关注点 | Unreal 做法 |
|---|---|
| HTTP | `FHttpModule` + `OnProcessRequestComplete()`（天然异步，勿在 GameThread 阻塞） |
| 超时竞争 | `FTimerHandle` 每秒刷"思考中…已等 X 秒"，120s 兜底 |
| 请求防串台 | `int32 ReqSeq` 原子递增；回调里比对丢弃迟到响应 |
| SSE | UE 没有 EventSource — 先用 `/api/events` 2s 轮询，需要推送再起后台线程长连接 |
| JSON | `FJsonObject/FJsonSerializer`（中文记得 `ensure_ascii=false` 已由服务端保证） |

待首个 Unreal 用户接入后回填真实实现（含踩坑记录）——**实战代码 > 纸上骨架**。
