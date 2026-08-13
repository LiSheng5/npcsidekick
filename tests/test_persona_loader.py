"""人格 JSON 加载器测试 — 制作者放文件即用。"""
import json

import pytest

from npc.persona_loader import load_persona_from_json, load_personas_from_dir


def _write(tmp_path, name, data):
    p = tmp_path / name
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


class TestLoadSingle:
    def test_valid_persona(self, tmp_path):
        p = _write(tmp_path, "ye.json", {
            "id": "ye", "name": "叶",
            "identity": "采集者", "personality": "细心",
            "speech_style": "简短", "taboos": ["浪费食物"],
        })
        persona = load_persona_from_json(p)
        assert persona["id"] == "ye"
        assert persona["name"] == "叶"

    def test_missing_required_rejected(self, tmp_path):
        p = _write(tmp_path, "bad.json", {"id": "x"})
        with pytest.raises(ValueError):
            load_persona_from_json(p)

    def test_name_defaults_to_id(self, tmp_path):
        p = _write(tmp_path, "noname.json", {
            "id": "noname", "identity": "i", "personality": "p",
            "speech_style": "s", "taboos": [],
        })
        assert load_persona_from_json(p)["name"] == "noname"

    def test_taboos_must_be_list(self, tmp_path):
        p = _write(tmp_path, "t.json", {
            "id": "t", "identity": "i", "personality": "p",
            "speech_style": "s", "taboos": "偷东西",
        })
        with pytest.raises(ValueError):
            load_persona_from_json(p)

    def test_bad_json_raises(self, tmp_path):
        p = tmp_path / "broken.json"
        p.write_text("{不是json", encoding="utf-8")
        with pytest.raises(json.JSONDecodeError):
            load_persona_from_json(p)


class TestLoadDir:
    def test_loads_all_and_skips_bad(self, tmp_path):
        _write(tmp_path, "good1.json", {
            "id": "g1", "identity": "i", "personality": "p",
            "speech_style": "s", "taboos": [],
        })
        _write(tmp_path, "good2.json", {
            "id": "g2", "identity": "i", "personality": "p",
            "speech_style": "s", "taboos": [],
        })
        _write(tmp_path, "bad.json", {"id": "x"})  # 缺必需字段 → 跳过
        personas = load_personas_from_dir(str(tmp_path))
        assert set(personas.keys()) == {"g1", "g2"}

    def test_missing_dir_returns_empty(self, tmp_path):
        assert load_personas_from_dir(str(tmp_path / "不存在")) == {}

    def test_example_files_load(self):
        """npc/personas/ 下的苍/阿黎 JSON 必须能加载（游戏参考角色）。"""
        from pathlib import Path

        for name, pid in (("cang.json", "cang"), ("ali.json", "ali")):
            f = Path("npc/personas") / name
            assert f.exists(), f"{name} 缺失"
            persona = load_persona_from_json(f)
            assert persona["id"] == pid
            assert persona["rules"]["replies"]

    def test_example_files_have_routine(self):
        """苍/阿黎配了自主日常（游戏村民要活起来）。"""
        from pathlib import Path

        for name in ("cang.json", "ali.json"):
            persona = load_persona_from_json(Path("npc/personas") / name)
            assert persona["routine"], f"{name} 缺 routine"
            assert all(it["action"] in ("gather", "rest", "say") for it in persona["routine"])


class TestRoutineValidation:
    """routine（自主日常表）校验: 制作者友好 — 坏文件跳过、坏项丢弃、绝不炸服务。"""

    def _valid(self):
        return {
            "id": "r", "identity": "i", "personality": "p",
            "speech_style": "s", "taboos": [],
        }

    def test_routine_absent_ok(self, tmp_path):
        p = _write(tmp_path, "noroutine.json", self._valid())
        assert "routine" not in load_persona_from_json(p)   # 不配 = NPC 静止（向后兼容）

    def test_valid_routine_accepted(self, tmp_path):
        data = self._valid()
        data["routine"] = [
            {"action": "gather", "resource": "木材", "count": 2, "weight": 4},
            {"action": "rest", "ticks": 3},
            {"action": "say", "weight": 1},
        ]
        p = _write(tmp_path, "ok.json", data)
        assert load_persona_from_json(p)["routine"] == data["routine"]

    def test_routine_not_list_rejected(self, tmp_path):
        data = self._valid()
        data["routine"] = "每天砍柴"
        p = _write(tmp_path, "bad.json", data)
        with pytest.raises(ValueError):
            load_persona_from_json(p)

    def test_bad_items_skipped_with_warning(self, tmp_path):
        data = self._valid()
        data["routine"] = [
            {"action": "fly", "weight": 1},                    # action 非法 → 丢
            {"action": "gather", "weight": 1},                 # gather 缺 resource → 丢
            {"action": "rest", "ticks": 0},                    # ticks 非正 → 丢
            {"action": "gather", "resource": "木材", "count": 1},  # 合法 → 留
        ]
        p = _write(tmp_path, "mixed.json", data)
        persona = load_persona_from_json(p)
        assert persona["routine"] == [{"action": "gather", "resource": "木材", "count": 1}]

    def test_all_bad_becomes_empty_routine(self, tmp_path):
        data = self._valid()
        data["routine"] = [{"action": "rest", "ticks": -1}]
        p = _write(tmp_path, "allbad.json", data)
        assert load_persona_from_json(p)["routine"] == []     # 空 = 静止，安全默认
