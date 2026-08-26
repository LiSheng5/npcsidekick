"""LLM 客户端装配（P2 拆分序①·2026-08-25 自 npc.py R2 迁出）。

纯装配逻辑：key 解析(env 按 provider 匹配 → api_key.txt 兜底)、模型名解析、
provider 工厂封装。NPC._get_llm 保留槽位缓存职责，装配细节委托本模块 ——
调用方(npc 内部/subagent/server)零改动。
"""
from __future__ import annotations

import os
from pathlib import Path

from agent.llm.client import LLMClient
from agent.providers.factory import create_provider


def read_api_key_file() -> str:
    """直接从项目根 api_key.txt 读 key（不受环境变量污染）。"""
    f = Path(__file__).resolve().parent.parent / "api_key.txt"
    if f.exists():
        k = f.read_text(encoding="utf-8").strip()
        if k:
            return k
    return ""


def model_of(role: str) -> str:
    """角色→模型名（NPC_DIALOGUE_MODEL / NPC_REVIEW_MODEL，默认 flash）。"""
    dialogue = os.environ.get("NPC_DIALOGUE_MODEL", "deepseek-v4-flash")
    return {
        "dialogue": dialogue,
        "review": os.environ.get("NPC_REVIEW_MODEL", "deepseek-v4-flash"),
    }.get(role, dialogue)


def resolve_api_key(model_name: str) -> str:
    """key 按模型 provider 匹配 env；LLM_API_KEY 显式通道优先；
    均未设置则读 api_key.txt（绕过被污染的 config）。"""
    lower = model_name.lower()
    env_key = os.environ.get("LLM_API_KEY", "")
    if not env_key:
        if "deepseek" in lower:
            env_key = os.environ.get("DEEPSEEK_API_KEY", "")
        elif "glm" in lower or "zhipu" in lower or "chatglm" in lower:
            env_key = os.environ.get("ZHIPU_API_KEY", "")
        else:
            env_key = os.environ.get("OPENAI_API_KEY", "")
    return env_key or read_api_key_file()


def build_client(api_key: str, model_name: str, base_url: str = "") -> LLMClient:
    """base_url 空 → create_provider 按模型名推断（勿用 config.BASE_URL 兜底，
    它默认 deepseek 会把 OpenRouter/本地模型发错地方）。"""
    provider = create_provider(api_key=api_key, model_name=model_name,
                               base_url=base_url)
    return LLMClient(provider=provider)