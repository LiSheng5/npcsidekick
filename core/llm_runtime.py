"""LLM 运行时设置 —— 三层合并 / 换脑 / key 状态与掩码 / 明文 key 文件权限检测。

设置的三层（优先级从高到低）：
  1. 页面改的（`_LLM_RUNTIME`，进程内有效）
  2. `store/llm_config.json`（页面保存的，落盘）
  3. 环境变量（`AGENT_MODEL` / `NPC_API_KEY` / `NPC_BASE_URL` / `NPC_REASONING_EFFORT`）

家规：**每次现读**（不 import 时缓存）—— 页面改完立即生效，环境变量改了也无需重启。
key 的明文只可能出现在两处：`api_key.txt` 与 `store/llm_config.json`（两者权限由
`key_perm_hint` 只检测、不代改）；接口与日志一律只给掩码（`mask_secret` / `scrub`）。

调用方：`server.py`（装配与 /api/state）、`turn.py`（思考档位与日志脱敏）、`console_api.py`
（模型页读写，经 server 注入的回调）。
"""
from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.client import LLMClient
from core.config_flags import env_text
from core.logging_config import log

import memory

# 思考模式档位：未设置 → 不指定，用服务端默认；
# off/disabled/none → 显式关闭；low/medium/high/max → 开启
_ENV_REASONING_EFFORT = "NPC_REASONING_EFFORT"


def reasoning_effort() -> Optional[str]:
    """思考档位（现读，可热切）：页面设置 > 落盘配置 > 环境变量。空白 → None（不指定）。"""
    raw = str(llm_settings().get("reasoning_effort") or "").strip()
    return raw or None


def thinking_effort() -> Optional[str]:
    """实际发给模型的档位 —— 勾了"该模型不认思考参数"就一律 None（一个参数都不发）。

    对不认这些字段的端点，连 `thinking:{type:disabled}` 都是未知参数会 400，
    所以"不支持思考的模型"要映射到"不指定"，而不是显式关闭。
    """
    if llm_settings().get("thinking_unsupported"):
        return None
    return reasoning_effort()


# ── LLM 设置（页面可改，落 store/llm_config.json；key 不在这里）──

_LLM_FIELDS = ("model", "base_url", "api_key", "reasoning_effort", "thinking_unsupported")
_EFFORT_VALUES = ("off", "disabled", "none", "low", "medium", "high", "max")

# 只有 api_key 是允许字段（页面可填，明文落盘，见 _PLAIN_KEY_WARNING）；
# 其余凭据类字段一律响亮拒绝，别被静默吞掉。
_FORBIDDEN_KEYS = ("token", "authorization", "secret", "password")

_PLAIN_KEY_WARNING = ("页面填的 key 以**明文**存在 store/llm_config.json —— "
                      "别分享、别提交这个文件；想更安全就清空它，改用环境变量 NPC_API_KEY。")


def mask_secret(value: str) -> str:
    """只留头尾：`sk-abc…wxyz`。页面与接口只给掩码，绝不回原文。"""
    value = value or ""
    if not value:
        return ""
    if len(value) <= 8:
        return f"{value[:2]}…"
    return f"{value[:3]}…{value[-4:]}"


def effective_api_key() -> str:
    """生效的 key：页面填的（store/llm_config.json）优先，其次环境变量 / api_key.txt。"""
    from core import config

    return str(llm_settings().get("api_key") or "").strip() or (config.api_key() or "")


def scrub(text: str) -> str:
    """日志/报错脱敏：把生效 key 与环境变量 key 的原文都换成掩码。"""
    from core import config

    out = text or ""
    for key in {effective_api_key(), config.api_key() or ""}:
        if len(key) >= 6 and key in out:
            out = out.replace(key, mask_secret(key))
    return out


def api_key_status() -> Dict[str, Any]:
    """key 的只读状态：有没有、来自哪、掩码 —— 内容一个字都不回。"""
    from core import config
    from core.settings import api_key_source

    # "页面填的"要看持久化层（文件 + 运行时），不能看合并值 —— 合并值已经回落过环境变量了
    from_page = str(_persisted_llm_config().get("api_key") or "").strip()
    env_key = config.api_key() or ""
    key = from_page or env_key
    return {
        "present": bool(key),
        "masked": mask_secret(key) or None,
        "source": "store/llm_config.json（页面填的）" if from_page else (api_key_source() or None),
        "from_page": bool(from_page),
        "perm_hint": key_perm_hint(),     # api_key.txt 权限太松时的提示（Windows 才有）
        "plain_warning": _PLAIN_KEY_WARNING if from_page else None,
    }


# ── api_key.txt 权限：只检测，不自动改 ────────────────────

_KEY_PERM_HINT: Optional[str] = None
_KEY_PERM_CHECKED = False


def _world_readable_lines(icacls_output: str, path: str = "") -> List[str]:
    """从 icacls 输出里挑"其他账户也能读/写"的行（Everyone / Users / Authenticated Users）。

    先把文件名本身从行首去掉 —— 否则 `C:\\Users\\...` 这种路径会被误判成 Users 账户。
    """
    hits: List[str] = []
    prefix = (path or "").strip().lower()
    for raw in (icacls_output or "").splitlines():
        line = raw.strip()
        if prefix and line.lower().startswith(prefix):
            line = line[len(prefix):]
        low = line.lower()
        if not any(name in low for name in ("everyone", "users", "authenticated users")):
            continue
        if any(right in low for right in ("(r)", "(rx)", "(rw)", "(w)", "(m)", "(f)")):
            hits.append(raw.strip())
    return hits


def scan_key_file_perm(path: Optional[Path] = None) -> Optional[str]:
    """看看 api_key.txt 是不是对其他账户也可读 —— **只检测，绝不改文件**。

    只在 Windows + 文件存在时跑一次 icacls；非 Windows / 没这文件 / icacls 失败 → None（静默）。
    返回一段可直接复制的收紧命令，由用户自己决定跑不跑。
    """
    if os.name != "nt":
        return None
    target = Path(path) if path else Path(__file__).resolve().parent.parent / "api_key.txt"
    if not target.exists():
        return None
    try:
        proc = subprocess.run(["icacls", str(target)],
                              capture_output=True, text=True, timeout=3)
    except Exception:                                   # 命令缺失/超时/被拦 → 一律闭嘴
        return None
    hits = _world_readable_lines(proc.stdout, str(target))
    if not hits:
        return None
    user = env_text("USERNAME") or env_text("USER") or "当前用户"
    return (f"{target.name} 对其他账户也可读（{hits[0]}）—— v4 不会自动改你的文件，"
            f'要收紧请自己跑：icacls "{target}" /inheritance:r '
            f'/grant:r "{user}:(R,W)" "SYSTEM:(F)" "Administrators:(F)"')


def key_perm_hint(refresh: bool = False) -> Optional[str]:
    """给日志与页面用的提示（只算一次，重启才刷新）。

    扫两处明文 key：工程根 `api_key.txt`、以及页面存下来的 `store/llm_config.json`
    （只在它真存了 key 时才提示）。
    """
    global _KEY_PERM_HINT, _KEY_PERM_CHECKED
    if refresh or not _KEY_PERM_CHECKED:
        hints: List[str] = []
        root = scan_key_file_perm()
        if root:
            hints.append(root)
        if _load_llm_file().get("api_key"):
            stored = scan_key_file_perm(llm_config_path())
            if stored:
                hints.append(stored)
        _KEY_PERM_HINT = "；".join(hints) or None
        _KEY_PERM_CHECKED = True
    return _KEY_PERM_HINT


_LLM_RUNTIME: Dict[str, Any] = {}          # 页面改过的值（优先级最高，进程内有效）


def llm_config_path() -> Path:
    """页面设置的落盘位置（与记忆卡同级的运行时目录，已 gitignore）。"""
    return memory.STORE_DIR / "llm_config.json"


def _load_llm_file() -> Dict[str, Any]:
    try:
        data = json.loads(llm_config_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_llm_file(cfg: Dict[str, Any]) -> None:
    path = llm_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {k: v for k, v in cfg.items() if k in _LLM_FIELDS}
    if blob.get("api_key"):
        # 明文 key 落盘 —— 文件自带一行警告：万一被拷走/误发，打开就看得见
        blob["_warning"] = ("本文件含明文 API Key：不要分享、不要提交到仓库；"
                            "想更安全就删掉它，改用环境变量 NPC_API_KEY。")
    path.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")


def _env_llm_config() -> Dict[str, Any]:
    """环境变量那一层 —— 页面与文件都没设时的起点。"""
    from core import config

    return {
        "model": env_text("AGENT_MODEL") or env_text("NPC_MODEL") or "",
        "base_url": env_text("NPC_BASE_URL") or config.base_url() or "",
        "api_key": config.api_key() or "",
        "reasoning_effort": env_text(_ENV_REASONING_EFFORT) or "",
        "thinking_unsupported": False,
    }


def llm_settings() -> Dict[str, Any]:
    """生效中的设置：页面改的 > store/llm_config.json > 环境变量。"""
    merged = _env_llm_config()
    merged.update({k: v for k, v in _load_llm_file().items() if k in _LLM_FIELDS})
    merged.update(_LLM_RUNTIME)
    return merged


def _persisted_llm_config() -> Dict[str, Any]:
    """要落盘的那份 = 文件里原有的 + 页面改的（**不含环境变量那一层**）。

    否则保存时会把环境变量里的 key 抄进文件 —— 用户没在页面填过也变成明文存一份。
    """
    saved = {k: v for k, v in _load_llm_file().items() if k in _LLM_FIELDS}
    saved.update(_LLM_RUNTIME)
    return saved


def _llm_origin() -> str:
    """当前值来自哪一层（页面上要说清，免得用户以为改了没生效）。"""
    if _LLM_RUNTIME:
        return "runtime"
    if _load_llm_file():
        return "file"
    return "env"


def llm_config_status() -> Dict[str, Any]:
    """当前 LLM 设置与状态：缺什么写什么，并标明值来自哪一层。"""
    from core import config
    from core.factory import resolve_llm_config

    cfg = llm_settings()
    status = resolve_llm_config(api_key=effective_api_key(), model_name=cfg["model"],
                                base_url=cfg["base_url"])
    status["reasoning_effort"] = reasoning_effort()      # 用户设的档位
    status["thinking_effort"] = thinking_effort()        # 实际下发的（None = 一个都不发）
    status["thinking_unsupported"] = bool(cfg["thinking_unsupported"])
    status["origin"] = _llm_origin()
    status["api_key"] = api_key_status()      # 只有掩码，没有原文
    return status


def apply_llm_settings(patch: Dict[str, Any],
                       holder: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """改设置 → 落盘 → 换脑。校验不通过抛 ValueError（调用方转 400）。

    api_key 是唯一允许的凭据字段：填了就明文存 store/llm_config.json（页面优先），
    传空串 = 删掉它、回到环境变量那一层。
    """
    for key in patch:                       # 护栏：凭据类字段（token/secret…）一律拒绝
        if str(key).lower() in _FORBIDDEN_KEYS:
            raise ValueError(f"字段 {key} 不接受 —— 要配 key 请用 api_key，或放环境变量 NPC_API_KEY")

    clear_key = False
    clean: Dict[str, Any] = {}
    for key in ("model", "base_url", "api_key", "reasoning_effort"):
        if key not in patch or patch[key] is None:
            continue
        value = str(patch[key]).strip()
        if key == "reasoning_effort" and value and value not in _EFFORT_VALUES:
            raise ValueError(f"思考档位只能是 {'/'.join(_EFFORT_VALUES)} 或留空（不指定）")
        if not value and key == "api_key":
            _LLM_RUNTIME.pop("api_key", None)    # 清空 = 删掉它，回到环境变量
            clear_key = True
            continue
        clean[key] = value
    if patch.get("thinking_unsupported") is not None:
        clean["thinking_unsupported"] = bool(patch["thinking_unsupported"])

    _LLM_RUNTIME.update(clean)
    saved = _persisted_llm_config()
    if clear_key:
        saved.pop("api_key", None)
    _save_llm_file(saved)
    if holder is not None:
        rebuild_llm(holder)
    return llm_config_status()


def reset_llm_settings(holder: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """清掉页面设置与落盘文件，回到环境变量那一层。"""
    _LLM_RUNTIME.clear()
    try:
        llm_config_path().unlink()
    except OSError:
        pass
    if holder is not None:
        rebuild_llm(holder)
    return llm_config_status()


def rebuild_llm(holder: Dict[str, Any]) -> None:
    """按生效设置重建大脑；没配齐 → None（/api/talk 走角色卡 rules 兜底）。"""
    holder["client"] = build_default_client()


def build_default_client() -> Optional[LLMClient]:
    """按用户配置创建 LLM 客户端；三件套缺任何一项 → None（走规则回复兜底）。"""
    status = llm_config_status()
    if not status["ready"]:
        log.warning("llm_not_configured", missing=",".join(status["missing"]),
                    hint="可在控制台「模型」页直接填；未配齐时 /api/talk 走角色卡 rules 回复")
        return None
    if status["source"] == "inferred":
        log.info("llm_base_url_inferred", model=status["model"], base_url=status["base_url"],
                 hint="想换厂商/网关请显式配 NPC_BASE_URL")
    from core import config
    from core.factory import create_provider

    provider = create_provider(
        api_key=effective_api_key(),        # 页面填的 key 优先，其次环境变量 / api_key.txt
        model_name=status["model"],
        base_url=status["base_url"],
        temperature=config.temperature(),
        max_tokens=config.max_tokens(),
    )
    return LLMClient(provider=provider)
