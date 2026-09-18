"""
NPCSidekick — NPC 记忆（加权检索）。

记忆加权公式参考 AI Town (a16z, MIT License, https://github.com/a16z-infra/ai-town):
  score = recency × gw[0] + relevance × gw[1] + importance × gw[2]
  gw = [0.5, 3, 2]; recency = 0.99^小时

设计点 #4: 记忆 = 可编辑文档 — 全部条目存 JSON，用户可打开直接改（改 importance/内容）。

v2026-08-23 中文分词: _relevance_score 切词从"按空格"升级为 jieba（可选依赖）—
中文整句不再糊成一个词。jieba 未安装时优雅回退空格切词（零惩罚哲学:
装不装 jieba 服务都能跑，只是召回质量差异）。
"""
from __future__ import annotations

import json
import math
import time
from collections import Counter
from typing import Dict, List, Optional

from agent.config_flags import env_flag

try:   # 可选依赖: pip install jieba 后自动启用（缺失不影响启动）
    import jieba  # type: ignore
except ImportError:   # pragma: no cover
    jieba = None  # type: ignore

# AI Town 记忆加权参数 (MIT, a16z-infra) — 见模块 docstring
_GW = (0.5, 3, 2, 2.0)   # (recency, relevance, importance, goal_relevance[P-6]) 权重
# 第 4 项只在 NPC_GOAL_RELEVANCE=1 且注入了活动目标文本时参与求和 —— 关着一字不加(逐字节同分)。
_DECAY = 0.99              # 每小时衰减
_HOUR_SECONDS = 3600

# ── 记忆卡事件文案单一来源(P0-2·2026-08-25)─────────────────
# 协议格式 = 半角冒号+空格。迁移脚本 migrate_memory_cards.R2 的垃圾签名
# 与本常量联动(旧全角存量兼容扫描)。imp 语义: 玩家正事>=8 / 日常=5 / 失败=4或6。
EV_DONE = "完成: "
EV_FAIL = "没做成: "

# ── TDAM 借鉴①(2026-08-26): 记忆三分类（内容维度, 与 category 生命周期维度正交）──
# persona=稳定特质偏好 / episodic=客观事件 / instruction=玩家长期要求。
# 旧卡无 mtype 字段 → 视作 episodic(读取方用 e.get("mtype") 兜底), 向下兼容。
MTYPES = ("persona", "episodic", "instruction")
MTYPE_DEFAULT = "episodic"


def goal_relevance_enabled() -> bool:
    """P-6 开关（现读现切，家规）：NPC_GOAL_RELEVANCE=1 时检索按"与活动目标的相关度"加分。"""
    return env_flag("NPC_GOAL_RELEVANCE")

# ── 反思 lesson 的可选字段（G1 另一半 · 2026-09-16）────────────────────
# 只有**同时**带 scope 与 recommendation 的条目才参与决策加权；老条目缺字段 → 不参与（零回归）。
LESSON_SCOPE_MAX_KEYS = 3      # scope 最多几个键（防模型/手改塞一大坨）
LESSON_REC_CLAMP = 1.0         # recommendation 夹取到 [-1, 1]


def clean_scope(raw) -> Optional[Dict[str, str]]:
    """`scope` 归一：非空 dict、键值皆非空短字符串、键数 ≤ LESSON_SCOPE_MAX_KEYS → 干净副本；否则 None。

    scope 的键 = **抽签项里的字段名**（如 action / resource）—— 引擎不预设语义，
    匹配用 `item.get(k) == v`（游戏无关：写什么由人设/世界声明决定，不写进引擎）。

    ⚠ 键数超限 → **整条拒绝**（不是截断）：截断会悄悄放宽作用域、把 lesson 施加到原意之外的活上；
    拒绝的后果只是"这条经验不参与决策"，安全得多（fail-closed）。
    """
    if not isinstance(raw, dict) or not raw:
        return None
    if len(raw) > LESSON_SCOPE_MAX_KEYS:
        return None
    out: Dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not k.strip():
            return None
        if not isinstance(v, str) or not v.strip():
            return None
        out[k.strip()] = v.strip()
    return out or None


def clean_recommendation(raw) -> Optional[float]:
    """`recommendation` 归一：数值 → 夹取 [-1, 1]；非法 → None（不猜）。"""
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val != val:                 # NaN
        return None
    return max(-LESSON_REC_CLAMP, min(LESSON_REC_CLAMP, val))



# 管家降级层(任务书#04): 流水账 general→archived 后退出检索上下文,
# 证据链仍在卡上永不物理删除。语义由 memory(卡 owner) 单点定义,
# 消费方(housekeeper/retrieve/format_for_context)只认本常量。
CATEGORY_ARCHIVED = "archived"

# 阶段② 轻量海马体: 规范词 → 同义词族（中文同义召回，不依赖 embedding）
# ⚠ 本表是**参考默认**：只在"未加载任何世界"时生效（态③，见 load_entity_synonyms_from_world）。
# 真正的真相在世界的 `_entity_synonyms` 声明里 —— 换游戏改世界 JSON，不要改这里。
_ENTITY_SYNONYMS: Dict[str, frozenset] = {
    "木材": frozenset({"木材", "木头", "柴", "柴火", "木料", "原木", "木", "树"}),
    "浆果": frozenset({"浆果", "果子", "野果", "果实"}),
    "石头": frozenset({"石头", "岩石", "石块", "石"}),
    "工具": frozenset({"工具", "木石工具", "石器"}),
    "麻绳": frozenset({"麻绳", "结实麻绳", "绳子", "绳"}),
    "村庄": frozenset({"村庄", "村子", "村里", "家"}),
    "森林": frozenset({"森林", "树林", "林子"}),
    "矿洞": frozenset({"矿洞", "矿山", "洞里"}),
    "河边": frozenset({"河边", "河岸", "河"}),
    "苍": frozenset({"苍", "老猎手", "猎手"}),
    "阿黎": frozenset({"阿黎", "采集者"}),
    "主角": frozenset({"主角", "玩家"}),
}
# 单字别名白名单（2026-08-28 review 修复）: 这两字语义单义、游戏语境稳定
# （"砍柴"=木材、村边那条"河"），误伤面小。其余单字（家/木/石/绳）不参与
# 子串匹配 —— "大家/了不起"这类无关词里的"家/木"污染过检索/合并/统计。
_SINGLE_CHAR_TERMS = frozenset({"柴", "河"})

# 当前生效的同义词表（声明驱动，2026-09-18）—— 三态语义，别简化：
#   ① 世界已声明         → 用声明值
#   ② 世界已加载但未声明  → **空表**（制作者的沉默 = 不要我的词表；别游戏不继承本游戏的地名/资源名）
#   ③ 未加载任何世界(None) → 回落上面的参考默认（保裸调用与既有测试）
_ACTIVE_ENTITY_SYNONYMS: Optional[Dict[str, frozenset]] = None
_ACTIVE_SINGLE_CHARS: Optional[frozenset] = None
# 一跳关联加分: 与 query 实体共现的实体所链接的记忆 +0.8/实体（HippoRAG 思想轻量版）
_ASSOCIATION_WEIGHT = 0.8
# 阶段③ 遗忘: 记忆强度半衰期（小时）— 强度 = importance × 0.5^(小时/半衰期)
_HALF_LIFE_HOURS = 72.0

# 切词时剔除的标点/分隔（中英文都收）
_PUNCT = ("，", "。", "？", "！", "、", "；", "：", ",", ".", "?", "!", ";", ":")


def _tokenize(text: str) -> List[str]:
    """中文感知切词: 有 jieba 用 jieba(整句不再糊成一个词), 没装退回空格切。

    返回去空白后的 token 列表; 纯标点 token 已剔除。
    """
    cleaned = text
    for p in _PUNCT:
        cleaned = cleaned.replace(p, " ")
    if jieba is not None:
        return [w.strip() for w in jieba.lcut(cleaned) if w.strip()]
    return [w for w in cleaned.split() if w]


def load_entity_synonyms_from_world(world: Optional[Dict]) -> Dict[str, frozenset]:
    """实体同义词族 —— **声明驱动**（`_entity_synonyms` / `_entity_single_chars`，2026-09-18）。

    世界用这两个键声明"哪些词归一成哪个规范名"（检索召回用）。**引擎层不持有游戏
    词表** —— 上面的 `_ENTITY_SYNONYMS` 只是"未加载世界"时的参考默认。

    三态（关键，别简化）:
      - `world=None`        → 复位到未加载态（态③）→ `_canonical_terms` 回落参考默认
      - 世界已声明该键       → 用声明值（态①）
      - 世界已加载但未声明    → **空表**（态②）—— 制作者的沉默 = 不要我的词表。
        没有这一态，"可覆盖"是假的：别的游戏漏声明一个键，就会从兜底继承本游戏的
        资源名与地名，污染它的检索召回。

    返回生效表。
    """
    global _ACTIVE_ENTITY_SYNONYMS, _ACTIVE_SINGLE_CHARS
    if world is None:
        _ACTIVE_ENTITY_SYNONYMS = None
        _ACTIVE_SINGLE_CHARS = None
        return dict(_ENTITY_SYNONYMS)
    syn: Dict[str, frozenset] = {}
    for canon, aliases in ((world.get("_entity_synonyms") or {})).items():
        if isinstance(aliases, (list, tuple, set, frozenset)):
            words = frozenset(str(a) for a in aliases if str(a))
            if words:
                syn[str(canon)] = words
    _ACTIVE_ENTITY_SYNONYMS = syn       # 空 dict 也算"已加载" → 态②（不回落参考默认）
    chars = world.get("_entity_single_chars")
    _ACTIVE_SINGLE_CHARS = (frozenset(str(c) for c in chars)
                            if isinstance(chars, (list, tuple, set, frozenset))
                            else frozenset())
    return dict(syn)


def _canonical_terms(text: str) -> set:
    """抽取文本里命中的规范词（同义词族归一到规范词）。阶段② 轻量海马体。

    词表来源见 `load_entity_synonyms_from_world`（声明驱动 + 三态兜底）。

    2026-08-28 修复: 单字别名默认不参与子串匹配 — "大家/了不起"里的
    "家/木"必误命中, 曾污染检索/关联/合并/画像统计四处（review 发现）。
    白名单里的单字（如"柴"）语义单义仍放行（"砍柴"→木材）。
    """
    syn = _ACTIVE_ENTITY_SYNONYMS if _ACTIVE_ENTITY_SYNONYMS is not None else _ENTITY_SYNONYMS
    single = _ACTIVE_SINGLE_CHARS if _ACTIVE_SINGLE_CHARS is not None else _SINGLE_CHAR_TERMS
    found = set()
    for canon, aliases in syn.items():
        if any((len(a) >= 2 or a in single) and a in text
               for a in aliases):
            found.add(canon)
    return found


def _recency_score(created_at: float, now: float) -> float:
    hours = max(0.0, (now - created_at) / _HOUR_SECONDS)
    return _DECAY ** hours


def _relevance_score(content: str, query: str,
                     query_tokens: Optional[List[str]] = None,
                     q_canon: Optional[set] = None,
                     content_canon: Optional[set] = None) -> float:
    """相关度: 原词命中 + 同义词召回（阶段② 轻量海马体）。

    原词命中比例保留（确定性基线）；叠加规范词(同义词族)命中，
    解决"柴" vs "木材"这类中文同义漏检。0-1。
    v2026-08-23: 切词经 _tokenize — 中文查询按词命中而不是整句,
    "昨天森林里的木头真粗" 能按 木头→木材 同义族+分词正常计分。
    任务书#03-A: query_tokens 可选 — retrieve 传入缓存, 批量打分时
    query 不再逐条重复分词(行为与旧版逐字节一致)。q_canon/content_canon
    同款缓存(简洁性 review): retrieve 的 assoc 趟已算好两侧规范词。
    """
    words = query_tokens if query_tokens is not None else _tokenize(query)
    raw = sum(1 for w in words if w in content) / len(words) if words else 0.0
    if q_canon is None:
        q_canon = _canonical_terms(query)
    syn = 0.0
    if q_canon:
        if content_canon is None:
            content_canon = _canonical_terms(content)
        syn = len(q_canon & content_canon) / len(q_canon)
    return min(1.0, raw * 0.6 + syn * 0.8)


def _idf_relevance(entry_tokens, query_tokens: List[str], df: Dict[str, int],
                   total: int, avgdl: Optional[float] = None) -> float:
    """任务书#03-B: 升级为真 BM25(与 TDAM 公式对齐, 名称保持兼容)。

    score = Σ_{t∈query} idf(t) × [tf(t)·(k1+1)] / [tf(t) + k1·(1-b+b·dl/avgdl)]
    idf(t) = ln(1 + (N - df_t + 0.5) / (df_t + 0.5)); k1=1.2, b=0.75。
    tf 保留 entry 词频(不做 set 去重 — 词频是 BM25 的 tf 分量)。
    avgdl 缺省 = dl(单文档视角, 旧单元测试 4 参调用兼容)。
    不再归一 0-1: 与旧公式取 max 时旧公式(0-1)成为自然下限, 稀有词/多词
    命中条目得分更高; 空 query / 空语料 / 全未命中仍返回 0。
    """
    if not query_tokens or total <= 0:
        return 0.0
    tf = Counter(entry_tokens)
    dl = float(sum(tf.values())) or 1.0
    if avgdl is None or avgdl <= 0:
        avgdl = dl
    k1, b = 1.2, 0.75
    score = 0.0
    for t in set(query_tokens):
        f = tf.get(t, 0)
        if f <= 0:
            continue
        dft = max(0, df.get(t, 0))
        idf = math.log(1.0 + (total - dft + 0.5) / (dft + 0.5))
        denom = f + k1 * (1.0 - b + b * dl / avgdl)
        score += idf * (f * (k1 + 1.0)) / denom
    return score


def _rrf_fuse(scored: List[tuple], sem: Dict[str, float]) -> List[tuple]:
    """任务书#03-B: RRF 倒数秩融合(k=60, 对齐 TDAM)。

    关键词路(加权总分+BM25)与向量语义路各自排名, 融合分 =
    1/(k+rank_kw) + 1/(k+rank_sem); 语义未命中的条目取最末秩(len+1)。
    替换旧的 "+2.0×语义分" 线性加分 —— 两条路的名次对等融合,
    不再让向量分数绝对值压过关键词排序。rank 均 1-based。
    """
    k = 60.0
    kw_order = sorted(scored, key=lambda x: x[0], reverse=True)
    kw_rank = {e["id"]: i for i, (_, e) in enumerate(kw_order, 1)}
    worst = len(scored) + 1
    sem_sorted = [e for _, e in sorted(
        scored, key=lambda p: sem.get(p[1].get("content", ""), 0.0),
        reverse=True)]
    sem_rank = {e["id"]: i for i, e in enumerate(sem_sorted, 1)
                if sem.get(e.get("content", ""), 0.0) > 0}
    return [(1.0 / (k + kw_rank[e["id"]]) + 1.0 / (k + sem_rank.get(e["id"], worst)), e)
            for _, e in scored]


class NPCMemory:
    """NPC 记忆: 条目 + 加权检索 + 持久化。"""

    def __init__(self, anchor_dir: Optional[str] = None,
                 vector_store: Optional[object] = None) -> None:
        self.entries: List[Dict] = []
        # ── 温层向量锚点(P1·2026-08-25): 可选语义检索增强 ──
        # anchor_dir: 向量库持久目录(None=禁用真实构建); vector_store: 测试注入口。
        # 生效需 env NPC_VECTOR_ANCHOR=1 且存储 available(chromadb 未装则永久降级)。
        self._anchor_dir = anchor_dir
        self._injected_vs = vector_store
        self._anchor_vs = None
        self._anchor_tried = False
        self._backfilled = False   # [{id, content, importance, created_at}]
        # P-6(2026-09-16): 当前活动目标的文本（由代码注入，**不进卡、不落盘**）——
        # 只有当 NPC_GOAL_RELEVANCE=1 时才参与检索打分；空 = 等于没有。
        self._goal_terms: List[str] = []

    # ── 写入 ─────────────────────────────────────────

    def add(self, content: str, importance: int = 5, category: str = "general",
            mtype: str = "", scope: Optional[Dict] = None,
            recommendation: Optional[float] = None) -> str:
        """记一条记忆。importance 0-9（可由 LLM 评分，Day 2 起默认手动/规则）。

        TDAM 借鉴①(2026-08-26): mtype 三分类标记(persona/episodic/instruction)。
        默认空串 → 完全不写该字段，落盘与旧版字节一致（零回归锚）。
        """
        entry = {
            "id": f"mem_{len(self.entries) + 1}_{int(time.time())}",
            "content": content,
            "importance": max(0, min(9, importance)),
            "category": category,
            "created_at": time.time(),
        }
        if mtype:
            entry["mtype"] = mtype
        # G1 另一半(2026-09-16): lesson 可选字段 —— **两个都给且合法**才写，
        # 否则完全不写这两个键（落盘与旧版字节一致，老条目照旧不参与决策）。
        _scope = clean_scope(scope)
        _rec = clean_recommendation(recommendation)
        if _scope and _rec is not None:
            entry["scope"] = _scope
            entry["recommendation"] = _rec
        self.entries.append(entry)
        # 温层向量锚点: 同步入语义索引(开关关闭/不可用时静默跳过)
        vs = self._anchor()
        if vs is not None:
            try:
                vs.add(entry["id"], content,
                       {"category": category, "importance": importance})
            except Exception:
                pass
        return entry["id"]

    # ── 检索（AI Town 加权公式）────────────────────────

    def _anchor(self):
        """温层向量锚点存取口(NPC_VECTOR_ANCHOR=1 时启用)。

        注入实例优先(测试)；否则惰性构建 ChromaDB 封装(chromadb 未安装→None)。
        首次命中且索引为空 → 批量回填既有记忆(旧卡自愈迁移)。任何故障→None。
        """
        if not env_flag("NPC_VECTOR_ANCHOR"):
            return None          # 家规开关: 默认关 —— 关闭时零接触零开销
        if not self._anchor_tried:
            self._anchor_tried = True
            vs = None
            if self._injected_vs is not None:
                vs = self._injected_vs
            else:
                try:
                    from agent.memory.vector_store import HAS_CHROMADB, VectorStore
                    if HAS_CHROMADB and self._anchor_dir:
                        vs = VectorStore(persist_dir=self._anchor_dir)
                except Exception:
                    vs = None
            self._anchor_vs = vs
        vs = self._anchor_vs
        if vs is None or not getattr(vs, "available", False):
            return None
        if not self._backfilled:
            self._backfilled = True
            try:
                if vs.count() == 0 and self.entries:
                    vs.add_batch([(e.get("id"), e.get("content", ""),
                                   {"category": e.get("category", "general"),
                                    "importance": e.get("importance", 5)})
                                  for e in self.entries])
            except Exception:
                pass
        return vs
    def active(self) -> List[Dict]:
        """可见条目(管家的 archived 降级层除外) — 检索/上下文/量算的唯一口径。

        无 archived 时直接返回内部列表(零拷贝快路径, 管家关时的默认状态)。
        """
        if any(e.get("category") == CATEGORY_ARCHIVED for e in self.entries):
            return [e for e in self.entries
                    if e.get("category") != CATEGORY_ARCHIVED]
        return self.entries


    def lessons_for(self, item: Dict) -> List[Dict]:
        """作用域命中该抽签项的反思 lesson（**绝对值最大的在前**）。

        G1 另一半(2026-09-16)。只认**同时带**合法 scope 与 recommendation 的条目
        （老条目缺字段 → 不参与，零回归）；匹配 = `item.get(k) == v` 全中。
        """
        hits = []
        for e in self.active():
            scope, rec = e.get("scope"), e.get("recommendation")
            if not isinstance(scope, dict) or not isinstance(rec, (int, float)):
                continue
            if all(item.get(k) == v for k, v in scope.items()):
                hits.append(e)
        return sorted(hits, key=lambda e: -abs(float(e["recommendation"])))

    def set_goal_terms(self, terms) -> None:
        """注入"当前活动目标"文本（P-6 · 2026-09-16）—— 检索时据此给相关记忆加分。

        由代码注入（scheduler 在刷新目标队列后调用）。**不落盘**（运行时派生状态，
        进卡就会破坏"关时逐字节一致"的锚）。任何非字符串/空白项一律丢弃。
        """
        if isinstance(terms, (list, tuple)):
            self._goal_terms = [t for t in (s.strip() if isinstance(s, str) else ""
                                            for s in terms) if t]
        else:
            self._goal_terms = []

    def retrieve(self, query: str = "", top_k: int = 5) -> List[Dict]:
        """加权检索 + 一跳关联（阶段② 轻量海马体）。

        score = recency×0.5 + relevance×3 + importance×2 + 关联加分。
        关联: 与 query 实体共现的实体所链接的记忆 +0.8/实体（拐弯找相关）。
        TDAM 借鉴④(NPC_BM25_RECALL=1): 相关度取 max(旧公式, BM25) — 开关关时
        与旧版逐字节同分(回归锚)。向量锚点(NPC_VECTOR_ANCHOR)开启时以 RRF
        倒数秩融合替换旧线性加分(任务书#03-B)。
        """
        now = time.time()
        # 任务书#04: archived(管家降级层)不参与检索 — 腾上下文空间, 证据链仍在卡上
        active = self.active()
        q_canon = _canonical_terms(query)
        # P-6(2026-09-16): 活动目标 → 规范词集合（开关关 / 没注入 → 空集 = 不参与打分）
        goal_canon: set = set()
        if goal_relevance_enabled():
            for _t in self._goal_terms:
                goal_canon |= _canonical_terms(_t)
        assoc: set = set()
        # 简洁性 review: entry 规范词只算一遍 — assoc 趟顺手缓存, 打分趟复用
        canon_by_id: Dict[str, set] = {}
        if q_canon or goal_canon:
            for e in active:
                e_canon = _canonical_terms(e["content"])
                canon_by_id[e["id"]] = e_canon
                if q_canon and (q_canon & e_canon):
                    assoc |= e_canon
            assoc -= q_canon
        # BM25 兜底(默认关): 一次 retrieve 一遍分词与词档频, 零依赖零 IO。
        # 任务书#03-A: query 与每条 entry 各只分词一次 —— q_tokens 供
        # _relevance_score 复用; tok_by_id 缓存供 df 统计与逐条打分共享
        # (旧版 jieba 共跑 1 + 1 + N 遍)。
        use_idf = env_flag("NPC_BM25_RECALL")
        df: Dict[str, int] = {}
        tok_by_id: Dict[str, List[str]] = {}
        q_tokens: List[str] = _tokenize(query)
        total = len(active)
        avgdl = 1.0
        if use_idf and active:
            for e in active:
                toks = _tokenize(e.get("content", ""))
                tok_by_id[e["id"]] = toks
                for t in set(toks):
                    df[t] = df.get(t, 0) + 1
            avgdl = sum(len(v) for v in tok_by_id.values()) / total
        scored = []
        for e in active:
            rel = _relevance_score(e["content"], query, q_tokens, q_canon,
                                   canon_by_id.get(e["id"]))
            if use_idf:
                rel = max(rel, _idf_relevance(tok_by_id.get(e["id"], []),
                                              q_tokens, df, total, avgdl))
            score = (
                _recency_score(e["created_at"], now) * _GW[0]
                + rel * _GW[1]
                + e["importance"] * _GW[2]
            )
            if goal_canon:
                # P-6: 与活动目标的规范词重合度(0~1) × 权重 —— 目标相关的事更容易被想起来
                _hit = len(canon_by_id.get(e["id"], set()) & goal_canon)
                score += (_hit / len(goal_canon)) * _GW[3]
            if assoc:
                score += len(canon_by_id.get(e["id"], set()) & assoc) * _ASSOCIATION_WEIGHT
            scored.append((score, e))
        # 温层向量锚点(P1, 任务书#03-B): 语义路与关键词路 RRF 倒数秩融合
        # (替换旧 "+2.0×语义分" 线性加分) —— "几点"能捞起"时间"类记忆
        try:
            vs = self._anchor()
            if vs is not None:
                sem = {}
                for h in vs.search(query, top_k=8):
                    sem[getattr(h, "text", "")] = float(getattr(h, "score", 0.0))
                if sem:
                    scored = _rrf_fuse(scored, sem)
        except Exception:
            pass
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def all(self) -> List[Dict]:
        return list(self.entries)

    # ── 持久化（记忆卡 = 可编辑文档）──────────────────

    def to_dict(self) -> List[Dict]:
        return self.entries

    def load(self, data: List[Dict]) -> None:
        self.entries = list(data)

    def consolidate(self, min_group: int = 3, prune_strength: float = 1.0) -> int:
        """遗忘合并（阶段③）: 重复主题合并成一条 + 弱旧记忆修剪。

        - 合并: 同主题(规范词交集)的非反思记忆 ≥ min_group 条 → 合并成一条
          （"模式留存，流水账淡去" — hippo-memory sleep 思想）。只复述事实（次数+最近一次），不编造。
        - 修剪: 强度 = importance × 0.5^(小时/半衰期)，低于 prune_strength 的旧条目移除；
          反思/合并条目与高重要度(≥8)保留。
        - 返回被移除（含被合并）的条目数。
        """
        now = time.time()
        removed = 0
        _ids_before = [e.get("id") for e in self.entries]
        # ── 1. 同主题合并 ──
        groups: Dict[frozenset, List[int]] = {}
        for i, e in enumerate(self.entries):
            # 任务书#06: archived(管家降级层)不进分组 → 不被合并, 也不会被
            # 同主题的合并顺手卷走(证据链永存红线)。只跳过, 不删除。
            if e.get("category") == CATEGORY_ARCHIVED:
                continue
            key = frozenset(_canonical_terms(e["content"]))
            if key:
                groups.setdefault(key, []).append(i)
        drop = set()
        for key, idxs in groups.items():
            cand = [i for i in idxs if self.entries[i].get("category") != "reflection"]
            if len(cand) < min_group:
                continue
            top = max(cand, key=lambda i: self.entries[i].get("importance", 5))
            topic = "、".join(sorted(key))
            summary = f"（已合并）关于{topic}的经历共 {len(cand)} 次；最近一次：{self.entries[top]['content']}"
            self.entries.append({
                "id": f"mem_c_{len(self.entries) + 1}_{int(now)}",
                "content": summary,
                "importance": max(8, self.entries[top].get("importance", 5)),
                "category": "consolidated",
                "created_at": self.entries[top]["created_at"],
            })
            for i in cand:
                drop.add(i)
            removed += len(cand)
        if drop:
            self.entries = [e for i, e in enumerate(self.entries) if i not in drop]
        # ── 2. 弱旧修剪 ──
        keep = []
        for e in self.entries:
            # 任务书#06: archived 与反思/合并条目同款免修剪 —— 降级层只增不减,
            # 强度衰减再低也不动它(管家降级时已判定过, 此处不二次裁决)。
            if e.get("category") in ("reflection", "consolidated", CATEGORY_ARCHIVED):
                keep.append(e)
                continue
            if e.get("importance", 5) >= 8:
                keep.append(e)
                continue
            hours = max(0.0, (now - e["created_at"]) / _HOUR_SECONDS)
            strength = e.get("importance", 5) * (0.5 ** (hours / _HALF_LIFE_HOURS))
            if strength >= prune_strength:
                keep.append(e)
            else:
                removed += 1
        self.entries = keep
        # 温层向量锚点: 被合并/修剪的条目同步出索引
        try:
            vs = self._anchor()
            if vs is not None:
                gone = set(_ids_before) - {e.get("id") for e in self.entries}
                for gid in sorted(gone):
                    try:
                        vs.delete(gid)
                    except Exception:
                        pass
        except Exception:
            pass
        return removed

    def format_for_context(self, entries: Optional[List[Dict]] = None) -> str:
        """把记忆条目格式化成模型上下文文本。"""
        items = [e for e in (entries if entries is not None else self.entries[-5:])
                 if e.get("category") != CATEGORY_ARCHIVED]
        if not items:
            return "（还没有记忆）"
        return "\n".join(f"- {e['content']}" for e in items)
