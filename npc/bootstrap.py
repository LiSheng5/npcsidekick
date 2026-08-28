"""服务启动引导（P2 拆分序③·2026-08-25 自 server.py main() 迁出）。

职责: CLI 解析、适配器/人格/动作清单装配、store 目录裁决、服务器拉起。
npc.server.main 保留薄壳委托至此（保住 npc-server 脚本与 python -m 契约）。
"""
from __future__ import annotations

import os
import webbrowser
from pathlib import Path

from agent.logging_config import log

_BASE_DIR = Path(__file__).resolve().parents[1]


def build_parser():
    import argparse
    parser = argparse.ArgumentParser(description="NPCSidekick Web 服务")
    parser.add_argument("--adapter", type=str, default="",
                        help="加载游戏适配器角色表，如 paleolithic（旧石器游戏村民）")
    parser.add_argument("--persona-dir", type=str, default="",
                        help="从目录加载人格 JSON 文件（制作者放文件即用，覆盖默认/适配器角色表）")
    parser.add_argument("--no-browser", action="store_true",
                        help="启动时不自动打开浏览器（游戏运行时用）")
    parser.add_argument("--manifest", type=str, default="",
                        help="游戏动作清单 JSON 路径(每游戏一份; 缺省=内置默认动作)")
    parser.add_argument("--port", type=int, default=8765,
                        help="监听端口(默认 8765; 多世界同开时各占一个端口)")
    parser.add_argument("--world-id", dest="world_id", default="",
                        help="世界命名空间(如 godot/gta): 独立 store 目录 + 请求 world_id 守卫")
    parser.add_argument("--store-dir", dest="store_dir", default="",
                        help="记忆卡目录(缺省: 有 world-id 用 npc/store_<id>, 否则 npc/store)")
    return parser


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)

    personas = None
    adapter_world = None   # 适配器自定义世界(有则全局共用)
    if args.persona_dir:
        from npc.persona_loader import load_personas_from_dir

        # 相对路径锚定代码位置（游戏从任意目录拉起时也能找到）
        p = Path(args.persona_dir)
        if not p.is_absolute():
            p = _BASE_DIR / p
        personas = load_personas_from_dir(str(p))
        print(f"已从 {args.persona_dir} 加载人格: {list(personas.keys())}")
        if not personas:
            print("警告: 目录没有有效 JSON，回退默认角色表")
    elif args.adapter:
        from importlib import import_module

        mod = import_module(f"npc.adapters.{args.adapter}")
        personas = mod.VILLAGERS
        print(f"已加载适配器角色表: {args.adapter}（{list(personas.keys())}）")
        # 适配器自定义世界(2026-08-22 GTA): 有 WORLD 属性就用 — 换游戏换世界,零代码
        if hasattr(mod, "WORLD"):
            adapter_world = mod.WORLD   # type: ignore[attr-defined]
            print(f"已加载适配器世界: {args.adapter}（{list(adapter_world['locations'].keys())}）")

    # 动作清单: 游戏接入点 — env NPC_MANIFEST 或 --manifest <json>, 缺省=默认动作
    _manifest_path = args.manifest or os.environ.get("NPC_MANIFEST", "")
    if _manifest_path:
        import json as _json
        from npc.reviewer import (load_manifest, parse_manifest_doc,
                                  set_manifest_places, set_manifest_resources)
        _p = Path(_manifest_path)
        if not _p.is_absolute():
            _p = _BASE_DIR / _p
        try:
            with open(_p, "r", encoding="utf-8") as _f:
                _parsed = parse_manifest_doc(_json.load(_f))
            load_manifest(_parsed["actions"])
            set_manifest_resources(_parsed.get("resources"))
            set_manifest_places(_parsed.get("places"))   # 任务书#05·C 地点词典
            print(f"已加载动作清单: {_p}"
                  + (f"（含资源词典 {len(_parsed['resources'])} 项）" if _parsed.get("resources") else "")
                  + (f"（含地点词典 {len(_parsed['places'])} 项）" if _parsed.get("places") else ""))
        except (OSError, ValueError) as _e:
            print(f"警告: 动作清单加载失败({_e}), 使用默认动作")

    port = args.port
    # 多世界隔离: 独立端口 + 独立记忆卡目录（GTA 和 Godot 同开互不打架）
    if args.store_dir:
        _store = Path(args.store_dir)
    elif args.world_id:
        _store = Path(f"npc/store_{args.world_id}")
    else:
        _store = Path("npc/store")
    if not _store.is_absolute():
        _store = _BASE_DIR / _store

    from npc.server import create_npc_server, load_village
    from npc.reviewer import (get_manifest_places, get_manifest_resources,
                              load_place_lexicon_from_world,
                              load_resource_lexicon_from_world)
    world, npcs = load_village(store_dir=str(_store), personas=personas,
                                world=adapter_world)
    # 动态资源词典(2026-08-23): 世界就绪后从 locations 收集 + 清单 resources 并名 —
    # 游戏加新资源改世界 JSON/清单表即可, 对话接单立刻认识, 零代码。
    lex = load_resource_lexicon_from_world(world, extra=get_manifest_resources())
    print(f"资源词典已就绪: {sorted(lex.keys())}")
    # 动态地点词典(任务书#05·C): 地标表 = world["locations"] 的 key + 清单 places 别名;
    # goto 接单走最长匹配归一, 长地名不再被截断成别的地名。
    places = load_place_lexicon_from_world(world, extra=get_manifest_places())
    print(f"地点词典已就绪: {sorted(places.keys())}" if places else "地点词典: 无(回退尾词清洗)")

    app = create_npc_server(npcs, world_id=args.world_id)
    log.info("npc_server_started", url=f"http://127.0.0.1:{port}/npc.html")
    print(f"NPCSidekick[{args.world_id or 'default'}]: http://127.0.0.1:{port}/npc.html"
          f"  (store={_store})")
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}/npc.html")

    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")