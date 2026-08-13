# NPCSidekick ↔ 旧石器游戏（Godot 4.7）对接蓝图

> 状态：游戏侧砍树/采集开发中（用户自做），大脑侧已就绪。
> 原则：游戏持全部权威状态（Godot 是唯一真理），NPC 大脑只提议意图与对话。

## 1. 世界契约映射表

| 世界契约（文本世界） | 旧石器游戏 | 状态 |
|---------------------|-----------|------|
| 位置（地点名） | 3D 坐标 → 区域（营地/东边枯木堆/西边兽道林…） | 待定，游戏侧定义区域 |
| 资源（木材/石头） | 砍树产出、物种库（HuntableSpeciesDB）、野菜点 | 砍树：用户开发中 |
| 背包（inventory） | BackpackSystem（`store(type_id)`） | ✅ 已有 |
| 交付（deliver 到主角） | 交给玩家/放入营地仓库 | ✅ 可行（背包接口现成） |
| 行动集 move/gather/craft/deliver | 走到区域 / 砍树·采集·狩猎 / 制作（旧石器=打制石器？） / 交付 | 砍树等：用户开发中 |
| 世界日志 | 游戏事件日志（可选） | 可选 |

## 2. 村民角色表

见 `paleolithic.py`：叶（采集者）、山（猎人）。
制作者随意改：人格字段（身份/性格/禁忌/欲望/目标/规则对话）+ 扩展口（system_prompt_override / context_extra）。

## 3. Godot 端最小接入（NpcBrain.gd 骨架）

```gdscript
# NpcBrain.gd — 村民的 NPCSidekick 客户端（挂在村民 CharacterBody3D 上）
extends Node

@export var npc_id : String = "ye"
@export var server_url : String = "http://127.0.0.1:8765"

var http : HTTPRequest

func _ready() -> void:
    http = HTTPRequest.new()
    add_child(http)
    http.request_completed.connect(_on_completed)

func _talk(text : String) -> void:
    var body := JSON.stringify({"npc_id": npc_id, "message": text})
    http.request(server_url + "/api/talk", ["Content-Type: application/json"],
                 HTTPClient.METHOD_POST, body)

func _on_completed(_result : int, code : int, _headers : PackedStringArray, body : PackedByteArray) -> void:
    if code == 200:
        var data : Dictionary = JSON.parse_string(body.get_string_from_utf8())
        # 显示气泡 / 调用对话 UI
        print("村民说: ", data.reply)
```

交互触发：用你已有的 Interactable/InteractionSystem —— 玩家走近按 E → `_talk(输入框文字)`。
行动指令（砍树/狩猎）：由大脑 /api/task 返回步骤 → Godot 端翻译成游戏内动画/产出（树倒、猎物掉落）。

## 4. 接入步骤

1. `python -m npc.server`（大脑侧，村民=叶/山 需服务器加载适配器角色表）
2. Godot 场景放村民节点（CharacterBody3D + NpcBrain.gd + 交互标记）
3. 玩家按 E 对话 → 真 LLM 回复（规则模式无 key 也能跑）
4. 派任务（对话或 UI 按钮）→ 村民走向目标 → 游戏内砍树/狩猎 → 物品进背包

## 5. 已知注意事项

- 服务器当前只绑 127.0.0.1：Godot 同机运行没问题；未来跨机/打包需放开绑定 + token
- LLM 延迟 1-10 秒：对话回合制没问题；任务行动在游戏内时间推进，不用实时
- 权威状态在 Godot：大脑的建议（想去哪/想干什么）由游戏侧校验后执行
