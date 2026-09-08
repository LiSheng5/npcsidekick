"""本地密钥存储（Web Console · 2026-09-07）。

目标（产品需求 §21/§22）:
  · API Key 只存本地，绝不进 Git、绝不出现在 API 响应里（只回 masked）
  · 不进 persona JSON（人设只定义角色，provider 配置独立管理）
  · 抽象成 SecretStore，将来可换 Windows 凭据管理器 / macOS Keychain / Secret Service

第一版实现（不为"上 OS 凭据库"过度复杂化）:
  · Windows: DPAPI（CryptProtectData，当前用户上下文）— 零第三方依赖，ctypes 直调
  · 其它平台 / DPAPI 失败: 受限权限文件（0600）明文，并在 store 里显式标注
    encrypted=false —— 宁可诚实标注，也不假装加密

存储结构（npc/config/providers.enc，JSON）:
  {"version": 1, "encrypted": true, "active": "openai", "providers": {
      "openai": {"id","name","base_url","model","key": "<base64>"}}}

外部只消费 public_view()：{id, name, base_url, model, configured, masked_key}。
"""
from __future__ import annotations

import base64
import json
import os
import stat
import sys
from pathlib import Path
from typing import Dict, Optional

_STORE_VERSION = 1
# 掩码样式: ••••••••8abc（只露最后 4 位，够用户辨认是哪把 key，不够拿去用）
_MASK_CHAR = "•"
_MASK_LEN = 8


# ── 加解密后端 ────────────────────────────────────────────

def _dpapi_available() -> bool:
    return sys.platform == "win32"


def _dpapi_protect(data: bytes) -> bytes:
    """Windows DPAPI 加密（当前用户上下文，CRYPTPROTECT_UI_FORBIDDEN）。"""
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), buf)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptProtectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(data: bytes) -> bytes:
    import ctypes
    import ctypes.wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.wintypes.DWORD),
                    ("pbData", ctypes.POINTER(ctypes.c_char))]

    buf = ctypes.create_string_buffer(data, len(data))
    blob_in = DATA_BLOB(len(data), buf)
    blob_out = DATA_BLOB()
    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0x01, ctypes.byref(blob_out))
    if not ok:
        raise OSError("CryptUnprotectData 失败")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def mask_key(key: str) -> str:
    """掩码: 只露最后 4 位。空 key → 空串（前端据此显示"未配置"）。"""
    if not key:
        return ""
    tail = key[-4:] if len(key) > 4 else ""
    return _MASK_CHAR * _MASK_LEN + tail


class SecretStore:
    """Provider 配置 + 密钥的本地持久化（加密优先，明文回退并标注）。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.encrypted = _dpapi_available()
        self._cache: Optional[Dict] = None

    # ── 读写 ──────────────────────────────────────────

    def load(self) -> Dict:
        """读存储。文件缺失/损坏 → 返回空结构（不炸服务）。"""
        if self._cache is not None:
            return self._cache
        data: Dict = {"version": _STORE_VERSION, "encrypted": self.encrypted,
                      "active": None, "providers": {}}
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    data.update({k: v for k, v in raw.items()
                                 if k in ("version", "encrypted", "active", "providers")})
                    if not isinstance(data.get("providers"), dict):
                        data["providers"] = {}
            except (json.JSONDecodeError, OSError):
                pass   # 损坏 = 空配置（用户重配即可，绝不因此起不来）
        self._cache = data
        return data

    def save(self, data: Dict) -> None:
        blob = json.dumps({**data, "version": _STORE_VERSION,
                           "encrypted": self.encrypted},
                          ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(blob, encoding="utf-8")
        # 明文回退时尽量收紧权限（Windows 上 chmod 效果有限，但仍做 best-effort）
        if not self.encrypted:
            try:
                os.chmod(self.path, stat.S_IRUSR | stat.S_IWUSR)
            except OSError:
                pass
        self._cache = data

    # ── 密钥编解码 ────────────────────────────────────

    def _encode(self, plain: str) -> str:
        if not plain:
            return ""
        raw = plain.encode("utf-8")
        if self.encrypted:
            try:
                raw = _dpapi_protect(raw)
            except Exception:
                self.encrypted = False      # DPAPI 不可用 → 诚实降级
        return base64.b64encode(raw).decode("ascii")

    def _decode(self, blob: str) -> str:
        if not blob:
            return ""
        raw = base64.b64decode(blob.encode("ascii"))
        if self.encrypted:
            try:
                raw = _dpapi_unprotect(raw)
            except Exception:
                return ""      # 解不开（换了用户/机器）→ 当作未配置，不猜
        return raw.decode("utf-8", errors="replace")

    # ── Provider CRUD ────────────────────────────────

    def list_providers(self) -> list:
        data = self.load()
        return [self._public(data, pid) for pid in data["providers"]]

    def get_provider(self, pid: str) -> Optional[Dict]:
        data = self.load()
        rec = data["providers"].get(pid)
        return self._public(data, pid) if rec else None

    def get_secret(self, pid: str) -> str:
        """取明文 key — 只在服务端内部用（建客户端 / 拨测），绝不外泄。"""
        rec = self.load()["providers"].get(pid)
        return self._decode(rec.get("key", "")) if rec else ""

    def upsert_provider(self, pid: str, *, name: str = "", base_url: str = "",
                        model: str = "", api_key: str = "") -> Dict:
        data = self.load()
        rec = dict(data["providers"].get(pid, {}))
        rec["id"] = pid
        if name:
            rec["name"] = name
        if base_url:
            rec["base_url"] = base_url
        if model:
            rec["model"] = model
        if api_key:
            rec["key"] = self._encode(api_key)
        data["providers"][pid] = rec
        self.save(data)
        return self._public(data, pid)

    def delete_provider(self, pid: str) -> bool:
        data = self.load()
        if pid not in data["providers"]:
            return False
        del data["providers"][pid]
        if data.get("active") == pid:
            data["active"] = None
        self.save(data)
        return True

    def set_active(self, pid: str) -> bool:
        data = self.load()
        if pid not in data["providers"]:
            return False
        data["active"] = pid
        self.save(data)
        return True

    def active_id(self) -> Optional[str]:
        return self.load().get("active")

    # ── 视图 ──────────────────────────────────────────

    def _public(self, data: Dict, pid: str) -> Dict:
        rec = data["providers"].get(pid, {})
        key = self._decode(rec.get("key", ""))
        return {
            "id": pid,
            "name": rec.get("name", pid),
            "base_url": rec.get("base_url", ""),
            "model": rec.get("model", ""),
            "configured": bool(key),
            "masked_key": mask_key(key),
            "active": data.get("active") == pid,
        }
