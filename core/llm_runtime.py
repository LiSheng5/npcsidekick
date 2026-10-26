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
from core.paths import resolve_store

# 思考模式档位：未设置 → 不指定，用服务端默认；
# off/disabled/none → 显式关闭；minimal/low/medium/high/xhigh/max → 开启
# （minimal/xhigh 等高档位按端点方言归一，映射表见 openai_provider._thinking_params 与 README）
_ENV_REASONING_EFFORT = "NPC_REASONING_EFFORT"


def reasoning_effort(store_dir: Optional[Path] = None) -> Optional[str]:
    """思考档位（现读，可热切）：页面设置 > 落盘配置 > 环境变量。空白 → None（不指定）。"""
    raw = str(llm_settings(store_dir=store_dir).get("reasoning_effort") or "").strip()
    return raw or None


def thinking_effort(store_dir: Optional[Path] = None) -> Optional[str]:
    """实际发给模型的档位 —— 勾了"该模型不认思考参数"就一律 None（一个参数都不发）。

    对不认这些字段的端点，连 `thinking:{type:disabled}` 都是未知参数会 400，
    所以"不支持思考的模型"要映射到"不指定"，而不是显式关闭。
    """
    if llm_settings(store_dir=store_dir).get("thinking_unsupported"):
        return None
    return reasoning_effort(store_dir=store_dir)


# ── LLM 设置（页面可改，落 store/llm_config.json；key 不在这里）──

_LLM_FIELDS = ("model", "base_url", "api_key", "reasoning_effort", "thinking_unsupported",
               "context_window")
_EFFORT_VALUES = ("off", "disabled", "none", "minimal", "low", "medium", "high", "xhigh", "max")

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


def effective_api_key(store_dir: Optional[Path] = None) -> str:
    """生效的 key：页面填的（store/llm_config.json）优先，其次环境变量 / api_key.txt。"""
    from core import config

    return str(llm_settings(store_dir=store_dir).get("api_key") or "").strip() \
        or (config.api_key() or "")


def scrub(text: str, store_dir: Optional[Path] = None) -> str:
    """日志/报错脱敏：把生效 key 与环境变量 key 的原文都换成掩码。

    `store_dir` 与调用方的数据目录对齐（自定义 store 下页面存的 key 才能被脱掉）。
    """
    from core import config

    out = text or ""
    for key in {effective_api_key(store_dir=store_dir), config.api_key() or ""}:
        if len(key) >= 6 and key in out:
            out = out.replace(key, mask_secret(key))
    return out


def short_err(exc: BaseException, store_dir: Optional[Path] = None,
              limit: int = 120) -> str:
    """报错摘要（scrub 脱敏后截断）—— 日志口径统一走这里，别再手写 `[:120]`。"""
    return scrub(str(exc), store_dir=store_dir)[:limit]


def api_key_status(store_dir: Optional[Path] = None) -> Dict[str, Any]:
    """key 的只读状态：有没有、来自哪、掩码 —— 内容一个字都不回。"""
    from core import config
    from core.settings import api_key_source

    # "页面填的"要看持久化层（文件 + 运行时），不能看合并值 —— 合并值已经回落过环境变量了
    from_page = str(_persisted_llm_config(store_dir=store_dir).get("api_key") or "").strip()
    env_key = config.api_key() or ""
    key = from_page or env_key
    return {
        "present": bool(key),
        "masked": mask_secret(key) or None,
        "source": "store/llm_config.json（页面填的）" if from_page else (api_key_source() or None),
        "from_page": bool(from_page),
        "perm_hint": key_perm_hint(store_dir=store_dir),     # key 文件权限太松时的提示（Windows 才有）
        "plain_warning": _PLAIN_KEY_WARNING if from_page else None,
    }


# ── api_key.txt 权限：只检测，不自动改 ────────────────────

# 提示缓存以文件路径为键 —— 自定义 store 的 llm_config.json 各算各的，不跨 store 复用
_KEY_PERM_HINTS: Dict[str, Optional[str]] = {}


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


def key_perm_hint(refresh: bool = False, store_dir: Optional[Path] = None) -> Optional[str]:
    """给日志与页面用的提示（每个文件只算一次，refresh=True 强制重扫；缓存按路径分开）。

    扫两处明文 key：工程根 `api_key.txt`、以及页面存下来的 `store/llm_config.json`
    （只在它真存了 key 时才提示）。
    """
    cache_key = str(llm_config_path(store_dir))
    if refresh or cache_key not in _KEY_PERM_HINTS:
        hints: List[str] = []
        root = scan_key_file_perm()
        if root:
            hints.append(root)
        if _load_llm_file(store_dir=store_dir).get("api_key"):
            stored = scan_key_file_perm(llm_config_path(store_dir))
            if stored:
                hints.append(stored)
        _KEY_PERM_HINTS[cache_key] = "；".join(hints) or None
    return _KEY_PERM_HINTS[cache_key]


_LLM_RUNTIME: Dict[str, Any] = {}          # 页面改过的值（优先级最高，进程内有效）


def reset_runtime_overrides() -> None:
    """清掉页面改的运行时覆盖（回落到落盘文件 / 环境变量那一层）。

    `reset_llm_settings` 是给 HTTP 端点用的（连带删落盘文件并换脑）；
    这里只清进程内那层 —— 测试隔离用，别再直接摸 `_LLM_RUNTIME`。
    """
    _LLM_RUNTIME.clear()


def llm_config_path(store_dir: Optional[Path] = None) -> Path:
    """页面设置的落盘位置（与记忆卡同级的运行时目录，已 gitignore）。"""
    return resolve_store(store_dir) / "llm_config.json"


def _load_llm_file(store_dir: Optional[Path] = None) -> Dict[str, Any]:
    try:
        data = json.loads(llm_config_path(store_dir).read_text("utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save_llm_file(cfg: Dict[str, Any], store_dir: Optional[Path] = None) -> None:
    path = llm_config_path(store_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    blob = {k: v for k, v in cfg.items() if k in _LLM_FIELDS}
    if blob.get("api_key"):
        # 明文 key 落盘 —— 文件自带一行警告：万一被拷走/误发，打开就看得见
        blob["_warning"] = ("本文件含明文 API Key：不要分享、不要提交到仓库；"
                            "想更安全就删掉它，改用环境变量 NPC_API_KEY。")
    # tmp + os.replace 原子落盘：写一半崩溃也不会留下损坏的配置；
    # 含明文 key，POSIX 下顺手把权限收紧到仅属主
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(blob, ensure_ascii=False, indent=2), encoding="utf-8")
    if os.name == "posix":
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass
    os.replace(tmp, path)


def _env_llm_config() -> Dict[str, Any]:
    """环境变量那一层 —— 页面与文件都没设时的起点。

    env 回退链单源在 core/settings.AgentSettings（AGENT_MODEL→NPC_MODEL、
    NPC_BASE_URL→旧名），这里只走 core.config，不再自己 or 一遍。
    """
    from core import config

    return {
        "model": config.model_name(),
        "base_url": config.base_url(),
        "api_key": config.api_key() or "",
        "reasoning_effort": env_text(_ENV_REASONING_EFFORT) or "",
        "thinking_unsupported": False,
        # 窗口的环境层走 env_context_window（已校验）—— 绝不能走 context_window()，
        # 那个函数要经 llm_settings 绕回这里，成环
        "context_window": config.env_context_window(),
    }


def llm_settings(store_dir: Optional[Path] = None) -> Dict[str, Any]:
    """生效中的设置：页面改的 > store/llm_config.json > 环境变量。"""
    merged = _env_llm_config()
    merged.update({k: v for k, v in _load_llm_file(store_dir=store_dir).items()
                   if k in _LLM_FIELDS})
    merged.update(_LLM_RUNTIME)
    return merged


def _persisted_llm_config(store_dir: Optional[Path] = None) -> Dict[str, Any]:
    """要落盘的那份 = 文件里原有的 + 页面改的（**不含环境变量那一层**）。

    否则保存时会把环境变量里的 key 抄进文件 —— 用户没在页面填过也变成明文存一份。
    """
    saved = {k: v for k, v in _load_llm_file(store_dir=store_dir).items() if k in _LLM_FIELDS}
    saved.update(_LLM_RUNTIME)
    return saved


def _llm_origin(store_dir: Optional[Path] = None) -> str:
    """当前值来自哪一层（页面上要说清，免得用户以为改了没生效）。"""
    if _LLM_RUNTIME:
        return "runtime"
    if _load_llm_file(store_dir=store_dir):
        return "file"
    return "env"


def llm_config_status(store_dir: Optional[Path] = None) -> Dict[str, Any]:
    """当前 LLM 设置与状态：缺什么写什么，并标明值来自哪一层。"""
    from core import config
    from core.factory import resolve_llm_config

    cfg = llm_settings(store_dir=store_dir)
    status = resolve_llm_config(api_key=effective_api_key(store_dir=store_dir),
                                model_name=cfg["model"], base_url=cfg["base_url"])
    status["reasoning_effort"] = reasoning_effort(store_dir=store_dir)   # 用户设的档位
    status["thinking_effort"] = thinking_effort(store_dir=store_dir)     # 实际下发的（None = 一个都不发）
    status["thinking_unsupported"] = bool(cfg["thinking_unsupported"])
    status["context_window"] = config.context_window(store_dir=store_dir)  # 三层合并后的生效值
    status["origin"] = _llm_origin(store_dir=store_dir)
    status["api_key"] = api_key_status(store_dir=store_dir)      # 只有掩码，没有原文
    return status


def apply_llm_settings(patch: Dict[str, Any],
                       holder: Optional[Dict[str, Any]] = None,
                       store_dir: Optional[Path] = None) -> Dict[str, Any]:
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
        flag = patch["thinking_unsupported"]
        if not isinstance(flag, bool):
            # 字符串 "false" 转 bool 是 True —— 严格收布尔，别静默把语义翻过来
            raise ValueError("字段 thinking_unsupported 必须是布尔值")
        clean["thinking_unsupported"] = flag
    if patch.get("context_window") is not None:
        from core import config

        number = config.valid_context_window(patch["context_window"])
        if number is None:
            # D1：下限 8000，低于它/非整数一律拒绝并提示（调用方转 400）
            raise ValueError("上下文窗口必须是 ≥ 8000 的整数（token）")
        clean["context_window"] = number

    _LLM_RUNTIME.update(clean)
    saved = _persisted_llm_config(store_dir=store_dir)
    if clear_key:
        saved.pop("api_key", None)
    _save_llm_file(saved, store_dir=store_dir)
    if holder is not None:
        rebuild_llm(holder, store_dir=store_dir)
    return llm_config_status(store_dir=store_dir)


def reset_llm_settings(holder: Optional[Dict[str, Any]] = None,
                       store_dir: Optional[Path] = None) -> Dict[str, Any]:
    """清掉页面设置与落盘文件，回到环境变量那一层。"""
    _LLM_RUNTIME.clear()
    try:
        llm_config_path(store_dir).unlink()
    except OSError:
        pass
    if holder is not None:
        rebuild_llm(holder, store_dir=store_dir)
    return llm_config_status(store_dir=store_dir)


def rebuild_llm(holder: Dict[str, Any], store_dir: Optional[Path] = None) -> None:
    """按生效设置重建大脑；没配齐 → None（/api/talk 走角色卡 rules 兜底）。

    旧大脑的 HTTP 连接池顺手关掉（别等 GC），失败不影响换脑。
    `store_dir` 与调用方一致 —— 换脑要读同一份 llm_config.json，脱敏也认同一把 key。
    """
    old = holder.get("client")
    holder["client"] = build_default_client(store_dir=store_dir)
    if old is not None and old is not holder["client"]:
        try:
            old.close()
        except Exception as exc:
            log.warning("llm_client_close_failed",
                        error=short_err(exc, store_dir=store_dir))


def build_default_client(store_dir: Optional[Path] = None) -> Optional[LLMClient]:
    """按用户配置创建 LLM 客户端；三件套缺任何一项 → None（走规则回复兜底）。"""
    status = llm_config_status(store_dir=store_dir)
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
        api_key=effective_api_key(store_dir=store_dir),   # 页面填的 key 优先，其次环境变量 / api_key.txt
        model_name=status["model"],
        base_url=status["base_url"],
        temperature=config.temperature(),
        max_tokens=config.max_tokens(),
    )
    return LLMClient(provider=provider)
