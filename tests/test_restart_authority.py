"""T-02 重启权威锚（2026-09-15）：共享世界的权威 = `saved_at` 最新的记忆卡。

旧行为（`load_village`）：取 **cast 迭代顺序里第一个有卡的 NPC** 的世界当共享世界，
其余卡的 world 被静默丢弃。→ 增删/改名人设文件或目录排序变化就会换权威，可能把整村带回退
（复现：`scripts/probe_restart_consistency.py`，结论见 `docs/NPC大脑架构.md` §30.1）。

本文件钉住新契约 + 三条 fail-safe 路径（时间戳缺失退化 mtime / 显式世界仍优先 / 无卡仍可用）。
"""
import json
import os
from pathlib import Path

from npc.server import load_village
from npc.world import default_world


def _persona(pid: str) -> dict:
    return {
        "id": pid, "name": pid, "identity": "测试村民", "personality": "测试",
        "speech_style": "测试", "taboos": [],
        "rules": {"replies": {}, "fallback": "嗯。"},
    }


def _seed_card(store: Path, pid: str, *, tick: int, delivered: dict,
               saved_at, position: str = "村庄") -> None:
    """直接写一张记忆卡（模拟某时刻落盘的世界快照；saved_at=None = 时间戳缺失/非法）。"""
    world = default_world()
    world["_tick"] = tick
    world["delivered"] = dict(delivered)
    world["actors"] = {pid: {"position": position, "inventory": {}}}
    card = {
        "id": pid, "name": pid, "saved_at": saved_at, "persona": _persona(pid),
        "world": world, "task_log": [], "memory": [], "reflected_upto": 0,
    }
    (store / f"{pid}_memory.json").write_text(json.dumps(card, ensure_ascii=False), encoding="utf-8")


class TestRestartAuthority:
    """权威选择：按新鲜度，不看角色表顺序。"""

    def test_newest_card_wins_regardless_of_cast_order(self, tmp_path):
        """核心保证：不管角色表顺序怎么排，都是最新那张卡胜出。"""
        _seed_card(tmp_path, "old", tick=1, delivered={"木材": 0},
                   saved_at="2026-09-15T10:00:00", position="矿洞")
        _seed_card(tmp_path, "new", tick=99, delivered={"木材": 7},
                   saved_at="2026-09-15T12:00:00")
        cast = {"old": _persona("old"), "new": _persona("new")}

        w1, loaded1 = load_village(store_dir=str(tmp_path), personas=cast)
        rev = {k: cast[k] for k in reversed(list(cast))}
        w2, loaded2 = load_village(store_dir=str(tmp_path), personas=rev)

        for world, loaded in ((w1, loaded1), (w2, loaded2)):
            assert world["_tick"] == 99, "最新卡(tick=99)应当权威"
            assert world["delivered"]["木材"] == 7
            assert world["actors"]["old"]["position"] == "村庄", "旧卡的位置不该被带进来"
            assert loaded["old"].world is world and loaded["new"].world is world

    def test_stale_first_in_cast_does_not_win(self, tmp_path):
        """顺序第一但更旧的卡不许夺权威（旧行为正是栽在这里）。"""
        _seed_card(tmp_path, "a_stale", tick=1, delivered={"木材": 0},
                   saved_at="2026-09-15T09:00:00")
        _seed_card(tmp_path, "z_fresh", tick=42, delivered={"木材": 5},
                   saved_at="2026-09-15T13:00:00")
        cast = {"a_stale": _persona("a_stale"), "z_fresh": _persona("z_fresh")}

        world, _ = load_village(store_dir=str(tmp_path), personas=cast)
        assert world["_tick"] == 42
        assert world["delivered"]["木材"] == 5

    def test_missing_saved_at_falls_back_to_mtime(self, tmp_path):
        """两份卡都没有合法 saved_at → 退化用文件 mtime，仍可判新旧（不并列、不抛错）。"""
        _seed_card(tmp_path, "a", tick=5, delivered={"木材": 3}, saved_at=None)
        _seed_card(tmp_path, "b", tick=50, delivered={"木材": 0}, saved_at="not-a-date")
        os.utime(tmp_path / "a_memory.json", (2_000_000_000, 2_000_000_000))   # a 的文件更新
        os.utime(tmp_path / "b_memory.json", (1_000_000_000, 1_000_000_000))

        world, _ = load_village(store_dir=str(tmp_path),
                                personas={"a": _persona("a"), "b": _persona("b")})
        assert world["_tick"] == 5

    def test_explicit_world_still_overrides_cards(self, tmp_path):
        """零回归：传入适配器世界时，记忆卡的世界（再新也是）一律让位。"""
        _seed_card(tmp_path, "a", tick=99, delivered={"木材": 7},
                   saved_at="2026-09-15T12:00:00")
        custom = default_world()
        custom["_marker"] = "adapter"
        custom["locations"]["新地点"] = {"exits": {}, "resources": {}}

        world, loaded = load_village(store_dir=str(tmp_path),
                                     personas={"a": _persona("a")}, world=custom)
        assert world.get("_marker") == "adapter"
        assert "新地点" in world["locations"]
        assert world["delivered"] == {}, "卡里的交付数不该漏进来"
        assert loaded["a"].world is world

    def test_no_cards_builds_default_world(self, tmp_path):
        """零回归：一张卡都没有 → 默认世界 + 全员共享。"""
        world, loaded = load_village(store_dir=str(tmp_path),
                                     personas={"a": _persona("a"), "b": _persona("b")})
        assert set(loaded) == {"a", "b"}
        assert loaded["a"].world is world and loaded["b"].world is world
        assert set(world["actors"]) == {"a", "b"}
