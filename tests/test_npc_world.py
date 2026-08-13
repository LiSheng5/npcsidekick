"""世界契约测试 — 文本世界参考实现的语义验证。"""
import pytest

from npc.world import default_world, observe, apply_action, find_path


@pytest.fixture
def world():
    return default_world()


class TestMove:
    def test_move_to_exit(self, world):
        new, ok, msg = apply_action(world, "move", {"dest": "森林"})
        assert ok
        assert world["actors"]["cang"]["position"] == "森林"
        assert "森林" in msg

    def test_move_to_unreachable(self, world):
        # 村庄不能直接去矿洞? 村庄 exits 含矿洞 — 用不存在的地点测试
        new, ok, msg = apply_action(world, "move", {"dest": "不存在"})
        assert not ok
        assert world["actors"]["cang"]["position"] == "村庄"

    def test_unknown_action(self, world):
        new, ok, msg = apply_action(world, "fly", {})
        assert not ok
        assert "未知行动" in msg


class TestGather:
    def test_gather_available(self, world):
        apply_action(world, "move", {"dest": "森林"})
        new, ok, msg = apply_action(world, "gather", {"resource": "木材"})
        assert ok
        assert world["actors"]["cang"]["inventory"]["木材"] == 1
        assert world["locations"]["森林"]["resources"]["木材"] == 998

    def test_gather_missing_resource(self, world):
        new, ok, msg = apply_action(world, "gather", {"resource": "铁矿石"})
        assert not ok
        assert "没有资源" in msg

    def test_gather_wrong_location(self, world):
        # 村庄没有木材
        new, ok, msg = apply_action(world, "gather", {"resource": "木材"})
        assert not ok


class TestRiverLocation:
    """河边（自主采集浆果的地点）+ _tick 扩展字段。"""

    def test_river_exists_and_connected(self, world):
        assert "河边" in world["locations"]
        assert "河边" in world["locations"]["村庄"]["exits"]
        assert world["locations"]["河边"]["exits"] == ["村庄"]
        assert find_path(world, "村庄", "河边") == ["河边"]

    def test_gather_berries_at_river(self, world):
        apply_action(world, "move", {"dest": "河边"})
        new, ok, msg = apply_action(world, "gather", {"resource": "浆果"})
        assert ok
        assert world["actors"]["cang"]["inventory"]["浆果"] == 1
        assert world["locations"]["河边"]["resources"]["浆果"] == 998

    def test_default_world_has_tick_field(self, world):
        assert world["_tick"] == 0


class TestDeliver:
    def test_deliver_success(self, world):
        apply_action(world, "move", {"dest": "森林"})
        apply_action(world, "gather", {"resource": "木材"})
        apply_action(world, "move", {"dest": "村庄"})
        new, ok, msg = apply_action(world, "deliver", {"resource": "木材"})
        assert ok
        assert world["delivered"]["木材"] == 1
        assert world["actors"]["cang"]["inventory"]["木材"] == 0

    def test_deliver_without_item(self, world):
        new, ok, msg = apply_action(world, "deliver", {"resource": "木材"})
        assert not ok
        assert "背包里没有" in msg

    def test_deliver_away_from_protagonist(self, world):
        apply_action(world, "move", {"dest": "森林"})
        apply_action(world, "gather", {"resource": "木材"})
        new, ok, msg = apply_action(world, "deliver", {"resource": "木材"})
        assert not ok
        assert "不在这里" in msg


class TestObserve:
    def test_observe_returns_description(self, world):
        text = observe(world)
        assert "村庄" in text
        assert "木材" not in text  # 村庄无资源

    def test_observe_shows_inventory(self, world):
        apply_action(world, "move", {"dest": "森林"})
        apply_action(world, "gather", {"resource": "木材"})
        text = observe(world)
        assert "木材 ×1" in text


class TestSay:
    def test_say_logs_to_world(self, world):
        new, ok, msg = apply_action(world, "say", {"text": "你好"})
        assert ok
        assert any("说: 你好" in entry for entry in world["log"])
