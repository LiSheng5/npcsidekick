"""
NPCSidekick — 旧石器生存游戏适配器：部落村民角色表。

对接游戏: 新建游戏项目（Godot 4.7，晚期旧石器生存）。
村民角色与游戏侧 villager_config.gd 对齐（id: cang / ali），
性格从游戏内对话文案（dialogue_db.gd）提取，保持一致性。

角色数据唯一真相在 npc/personas/cang.json + ali.json（制作者改 JSON 即改一切）—
本适配器只是薄壳：VILLAGERS = 默认角色表（苍/阿黎）。
规则关键词与游戏对话话题对齐（狩猎/柴/冬天/狼 ↔ 苍；浆果/猛犸/天气/营地 ↔ 阿黎），
这样"规则模式（本地兜底）"与"LLM 模式（远端生成）"说的是同一套事。
"""
from npc.persona import SAMPLE_NPCS

# 适配器角色表（服务器 --adapter paleolithic 即加载此表）
VILLAGERS: dict = SAMPLE_NPCS
