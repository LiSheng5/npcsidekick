# engine-clients — 各引擎客户端 SDK / 接入骨架

把两个实战游戏（Godot 旧石器生存 × GTA5 SHVDN 模组）踩出来的坑沉淀成可复用的
客户端资产。新游戏接入从 `common/PROTOCOL.md` 读协议开始。

```
engine-clients/
  common/PROTOCOL.md        ← 权威接入契约: 端点/载荷/错误语义/兜底铁律(先读这个)
  godot/reference.md        ← 实战参考: 指向已上线的 Godot 客户端代码
  unity/NPCSidekickClient.cs← C# 骨架(超时竞争/请求序号/本地兜底已内置, TODO 填游戏侧)
  unreal/README.md          ← Unreal 骨架占位(模式同 Unity, 待首个 Unreal 用户回填)
```

## 目录原则
- **协议唯一真相** = `common/PROTOCOL.md`；服务端改契约必须同步这里
- **实战代码 > 空想骨架**: Godot 目录不放假代码, 直接指向跑在生产的实现
- 每个引擎目录吸收该引擎特有的坑（WinForms/IME 是 GTA 的; Timer 竞争是 Godot 的）
