"""§16 数据驱动界面测试 — /api/state 的 panels 随世界 _hud 声明走（换游戏零改页面）。"""
import pytest
from fastapi.testclient import TestClient

from npc.npc import NPC
from npc.server import _DEFAULT_HUD_PANELS, create_npc_server


def _make(tmp_path, world_tweak=None):
    npc = NPC(store_dir=str(tmp_path))
    npc.use_llm = False
    if world_tweak:
        world_tweak(npc.world)
    return TestClient(create_npc_server({"cang": npc}))


class TestHudPanels:
    def test_default_world_all_panels(self, tmp_path):
        """旧世界不声明 _hud → 默认全面板（旧石器/存量记忆卡向后兼容，行为不变）。"""
        data = _make(tmp_path).get("/api/state").json()
        assert data["panels"] == list(_DEFAULT_HUD_PANELS)

    def test_world_declaration_wins(self, tmp_path):
        """世界声明了 _hud.panels 就按声明渲染。"""
        def tweak(w):
            w["_hud"] = {"panels": ["position", "activity"]}
        data = _make(tmp_path, tweak).get("/api/state").json()
        assert data["panels"] == ["position", "activity"]

    def test_empty_declaration_falls_back_to_default(self, tmp_path):
        """_hud 写了但 panels 空/缺 → 视为没写（防手滑全隐藏）。"""
        def tweak(w):
            w["_hud"] = {"panels": []}
        data = _make(tmp_path, tweak).get("/api/state").json()
        assert data["panels"] == list(_DEFAULT_HUD_PANELS)

    def test_pure_dialog_world_hud(self, tmp_path):
        """纯对话世界: _hud 只声明 位置/在做 — 不再出现耐力/背包/交付错位。

        骨架注: 原用例经某游戏适配器取世界；该适配器已移出仓库，
        改用内联声明 — 本条要钉的是"_hud 声明生效后的面板集合"，与具体游戏无关。
        """
        def tweak(w):
            w["_hud"] = {"panels": ["position", "activity"]}
        data = _make(tmp_path, tweak).get("/api/state").json()
        assert data["panels"] == ["position", "activity"]
        assert "stamina" not in data["panels"]
        assert "inventory" not in data["panels"]
        assert "delivered" not in data["panels"]

    def test_actor_payload_still_complete(self, tmp_path):
        """面板只是显示层契约 — actors 数据字段照旧完整（游戏端镜像不受影响）。"""
        data = _make(tmp_path).get("/api/state").json()
        actor = data["actors"]["cang"]
        for key in ("position", "inventory", "state", "stamina", "activity"):
            assert key in actor
