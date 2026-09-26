"""废液分桶规划（预览，不落库）。

输入：最多 12 股废液（复用试剂档案：名称 + 相容性类别）、每股体积、单桶容量；
规则：当前全局限定规则图中处于启用状态的规则（不读取方案规则版本，也不影响
      现有方案推演 / 规则版本逻辑）。

判定口径（与推演引擎一致，区别在于桶内混合与加入顺序无关，因此**两个方向都检查**）：

- 两股废液的类别集合之间，只要存在某个类别对使一条 severity=high 的规则在
  **任一方向**命中（有向规则按 source→target 判定，反向类别对也会被显式检查），
  这两股废液就不可同桶；
- 同桶所有废液总体积不得超过单桶容量；
- 同一试剂的两股废液不互相配对（与推演引擎"同名称试剂不与自身历史配对"一致）。

优化目标（全部精确求解，n ≤ 12 回溯即可）：

1. 桶数最少；
2. 桶数相同时，最大桶负载最小（尽量装得均衡）；
3. 仍相同时，按"桶内试剂名称序列"取字典序最小，保证结果稳定可复现。

单股体积超过单桶容量时直接给出明确原因，不生成任何部分方案。
"""
import json
import math
from dataclasses import dataclass

from sqlalchemy.orm import Session

from . import models, rules_store

EPS = 1e-9
MAX_STREAMS = 12
SEVERITY_LABEL = {"low": "提示", "medium": "警示", "high": "严重"}


class BucketError(ValueError):
    """分桶业务校验错误（路由层统一转 4xx，detail 为可直接返回的结构化原因）。"""

    def __init__(self, detail: dict, status_code: int = 422):
        super().__init__(detail.get("message", "分桶规划校验失败"))
        self.detail = detail
        self.status_code = status_code


@dataclass
class Stream:
    index: int          # 请求中的 0 基序号（稳定标识）
    reagent_id: int
    name: str
    categories: list[str]
    volume_ml: float


# ---------------------------------------------------------------- 输入解析

def resolve_streams(db: Session, items: list) -> list[Stream]:
    """把请求中的废液项解析为档案试剂流（类别以档案实时为准）。

    未知试剂统一收集后报错；同试剂允许多股（视为不同批次），不与彼此配对。
    """
    unknown_ids: list[int] = []
    streams: list[Stream] = []
    for pos, item in enumerate(items):
        reagent = db.get(models.Reagent, item.reagent_id)
        if reagent is None:
            unknown_ids.append(item.reagent_id)
            continue
        streams.append(
            Stream(
                index=pos,
                reagent_id=reagent.id,
                name=reagent.name,
                categories=json.loads(reagent.categories or "[]"),
                volume_ml=float(item.volume_ml),
            )
        )
    if unknown_ids:
        raise BucketError({
            "message": "以下试剂档案不存在或已被删除，无法作为废液选择："
                       + "、".join(f"#{i}" for i in unknown_ids),
            "unknown_reagent_ids": unknown_ids,
        })
    return streams


def _check_capacity(streams: list[Stream], capacity: float) -> None:
    """单股超容量 → 明确原因、直接拒绝（不生成部分方案）。"""
    over = [
        {
            "index": s.index,
            "reagent_id": s.reagent_id,
            "name": s.name,
            "volume_ml": s.volume_ml,
        }
        for s in streams
        if s.volume_ml > capacity + EPS
    ]
    if over:
        raise BucketError({
            "message": (
                f"有 {len(over)} 股废液体积超过单桶容量 {capacity:g} mL，"
                "无法分桶（任何方案都装不下），请更换更大容器或拆分该废液后重试；"
                "未生成任何部分方案。"
            ),
            "bucket_capacity_ml": capacity,
            "over_capacity_streams": over,
        })


# ---------------------------------------------------------------- 冲突判定

def _rule_matches_ordered(rule, cat_src: str, cat_tgt: str) -> bool:
    """与推演引擎 engine._rule_matches 同口径：

    无向边：正向或反向匹配均可；有向边：仅 source->target。
    """
    if rule.source == cat_src and rule.target == cat_tgt:
        return True
    if not rule.directional and rule.source == cat_tgt and rule.target == cat_src:
        return True
    return False


def _pair_rule_hits(rule, a: Stream, b: Stream) -> list[dict]:
    """两股废液在任一方向上命中某条规则的全部类别对。

    桶内混合没有加入顺序：对类别对 (ca∈A, cb∈B) 同时检查
    "A 为 source 侧 / B 为 target 侧" 与反向两种有序配对，
    任一命中即视为该规则促使 A、B 分隔。
    """
    hits: list[dict] = []
    seen: set[tuple] = set()
    for ca in a.categories:
        for cb in b.categories:
            if _rule_matches_ordered(rule, ca, cb):
                key = ("a->b", ca, cb)
            elif _rule_matches_ordered(rule, cb, ca):
                key = ("b->a", ca, cb)
            else:
                continue
            if key in seen:
                continue
            seen.add(key)
            direction, cat_a, cat_b = key
            hits.append({
                "rule_id": rule.id,
                "title": rule.title,
                "severity": rule.severity,
                "severity_label": SEVERITY_LABEL.get(rule.severity, rule.severity),
                "reaction": rule.reaction,
                "rationale": rule.rationale,
                "references": json.loads(rule.references or "[]"),
                "category_a": cat_a,
                "category_b": cat_b,
                "match_direction": direction,
                "rule_directional": bool(rule.directional),
            })
    return hits


def find_conflicts(streams: list[Stream], rules: list) -> dict[tuple[int, int], list[dict]]:
    """返回所有"任一方向命中 high 规则"的废液对及其规则依据（按流序号升序）。"""
    high_rules = [r for r in rules if r.severity == "high"]
    conflicts: dict[tuple[int, int], list[dict]] = {}
    for x in range(len(streams)):
        for y in range(x + 1, len(streams)):
            a, b = streams[x], streams[y]
            # 同试剂（同名称档案）的多股废液不互相配对，与推演引擎口径一致
            if a.reagent_id == b.reagent_id:
                continue
            hits: list[dict] = []
            for rule in high_rules:
                hits.extend(_pair_rule_hits(rule, a, b))
            if hits:
                hits.sort(key=lambda h: (h["rule_id"], h["category_a"], h["category_b"], h["match_direction"]))
                conflicts[(a.index, b.index)] = hits
    return conflicts


# ---------------------------------------------------------------- 求解

def _max_clique_size(conf: list[int]) -> int:
    """冲突图最大团大小（两两互斥 → 至少需要这么多桶）。n ≤ 12，位掩码回溯。"""
    best = 0

    def bk(cands: int, size: int) -> None:
        nonlocal best
        if size + cands.bit_count() <= best:
            return
        if cands == 0:
            best = max(best, size)
            return
        lsb = cands & -cands
        v = lsb.bit_length() - 1
        bk(cands & ~lsb & conf[v], size + 1)  # 把 v 选入团
        bk(cands & ~lsb, size)                # 不选 v

    bk((1 << len(conf)) - 1, 0)
    return best


def _solve_for_k(
    n: int,
    volumes: list[float],
    names: list[str],
    orig_index: list[int],
    conf: list[int],
    capacity: float,
    k: int,
):
    """在恰好使用不超过 k 个（无序）桶的前提下搜索最优完整分配。

    最优 = (最大桶负载, 桶内名称序列) 字典序最小；不可行返回 None。
    对称性剪枝：第 pos 股只能放入已开启的桶，或开启下一个新桶（编号 opened）。
    """
    assign = [-1] * n
    loads = [0.0] * k
    members_mask = [0] * k
    best = None  # (max_load, canonical_key, assignment_tuple)

    def canonical_key() -> tuple:
        buckets = []
        for b in range(k):
            if members_mask[b] == 0:
                continue
            members = tuple(
                sorted(
                    (names[p], orig_index[p])
                    for p in range(n)
                    if assign[p] == b
                )
            )
            buckets.append(members)
        return tuple(sorted(buckets))

    def bt(pos: int, cur_max: float) -> None:
        nonlocal best
        if best is not None and cur_max > best[0] + EPS:
            return
        if pos == n:
            key = canonical_key()
            if (
                best is None
                or cur_max < best[0] - EPS
                or (abs(cur_max - best[0]) <= EPS and key < best[1])
            ):
                best = (cur_max, key, tuple(assign))
            return

        opened = (max(assign[:pos]) + 1) if pos else 0
        for b in range(min(opened + 1, k)):
            new_load = loads[b] + volumes[pos]
            if new_load > capacity + EPS:
                continue
            if members_mask[b] & conf[pos]:
                continue
            if best is not None and max(cur_max, new_load) > best[0] + EPS:
                continue
            assign[pos] = b
            loads[b] = new_load
            members_mask[b] |= 1 << pos
            bt(pos + 1, max(cur_max, new_load))
            members_mask[b] &= ~(1 << pos)
            loads[b] -= volumes[pos]
            assign[pos] = -1

    bt(0, 0.0)
    if best is None:
        return None
    return best[0], best[2]


def solve(streams: list[Stream], capacity: float,
          conflicts: dict[tuple[int, int], list[dict]]):
    """求桶数最少 → 最大桶负载最小 → 名称序列字典序最小的方案。

    返回 (bucket_of_stream: dict[流序号 -> 桶序号(0 基)], 最大桶负载, 桶数)。
    每股体积均不超容量时，"每股一桶"必然可行，因此一定有解。
    """
    n = len(streams)
    # 搜索顺序：体积大、名称靠前的先放（剪枝更强）；结果顺序再按名称规范化
    order = sorted(range(n), key=lambda i: (-streams[i].volume_ml, streams[i].name, i))
    pos_of = {orig: p for p, orig in enumerate(order)}
    volumes = [streams[order[p]].volume_ml for p in range(n)]
    names = [streams[order[p]].name for p in range(n)]
    orig_index = [streams[order[p]].index for p in range(n)]

    conf = [0] * n
    for (i, j) in conflicts:
        pi, pj = pos_of[i], pos_of[j]
        conf[pi] |= 1 << pj
        conf[pj] |= 1 << pi

    total = sum(s.volume_ml for s in streams)
    lower_k = max(
        1,
        math.ceil((total - EPS) / capacity),
        _max_clique_size(conf),
    )
    for k in range(lower_k, n + 1):
        result = _solve_for_k(n, volumes, names, orig_index, conf, capacity, k)
        if result is not None:
            max_load, assignment = result
            # 搜索位置 → 原流序号 → 桶（先按桶内名称序列给桶定序，输出稳定）
            raw_buckets: dict[int, list[int]] = {}
            for p, b in enumerate(assignment):
                raw_buckets.setdefault(b, []).append(orig_index[p])
            ordered_buckets = sorted(
                raw_buckets.values(),
                key=lambda members: tuple(sorted((streams[i].name, i) for i in members)),
            )
            bucket_of: dict[int, int] = {}
            for bno, members in enumerate(ordered_buckets):
                for i in members:
                    bucket_of[i] = bno
            return bucket_of, max_load, len(ordered_buckets)
    raise BucketError({"message": "分桶求解失败：在给定规则与容量下不存在可行方案"})


# ---------------------------------------------------------------- 报告组装

def _stream_out(s: Stream) -> dict:
    return {
        "index": s.index,
        "reagent_id": s.reagent_id,
        "name": s.name,
        "categories": list(s.categories),
        "volume_ml": s.volume_ml,
    }


def build_plan(db: Session, items: list, capacity: float) -> dict:
    """完整预览：解析 → 容量校验 → 冲突图 → 精确求解 → 自包含报告（不落库）。"""
    streams = resolve_streams(db, items)
    _check_capacity(streams, capacity)

    rules = db.query(models.Rule).filter(models.Rule.active.is_(True)).all()
    revision = rules_store.get_rule_revision()
    conflicts = find_conflicts(streams, rules)
    bucket_of, max_load, bucket_count = solve(streams, capacity, conflicts)

    by_index = {s.index: s for s in streams}
    total = sum(s.volume_ml for s in streams)
    buckets_out = []
    for bno in range(bucket_count):
        members = sorted(
            (by_index[i] for i, b in bucket_of.items() if b == bno),
            key=lambda s: (s.name, s.index),
        )
        load = sum(s.volume_ml for s in members)
        buckets_out.append({
            "index": bno,
            "load_ml": round(load, 6),
            "capacity_ml": capacity,
            "utilization": round(min(load / capacity, 1.0), 6) if capacity > 0 else 0.0,
            "spare_ml": round(capacity - load, 6),
            "streams": [_stream_out(s) for s in members],
        })

    separations = [
        {
            "stream_a": i,
            "stream_b": j,
            "reagent_a": by_index[i].name,
            "reagent_b": by_index[j].name,
            "bucket_a": bucket_of[i],
            "bucket_b": bucket_of[j],
            "rules": hits,
        }
        for (i, j), hits in sorted(conflicts.items())
    ]

    return {
        "kind": "labmix-bucket-plan",
        "rule_revision": revision,
        "rule_scope": "global",
        "bucket_capacity_ml": capacity,
        "stream_count": len(streams),
        "total_volume_ml": round(total, 6),
        "bucket_count": bucket_count,
        "max_bucket_load_ml": round(max_load, 6),
        "streams": [_stream_out(s) for s in streams],
        "buckets": buckets_out,
        "separations": separations,
        "objective": {
            "primary": "min_bucket_count",
            "secondary": "min_max_bucket_load",
            "tertiary": "bucket_member_name_sequence",
            "min_bucket_count": bucket_count,
            "min_max_bucket_load_ml": round(max_load, 6),
            "note": "桶数最少；桶数相同时最大桶负载最小；仍相同时按桶内试剂名称序列字典序确定稳定结果。",
        },
    }
