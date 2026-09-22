"""废液分桶规划求解。

复用试剂档案的类别标签与当前全局启用规则图：
两股废液只要在**任一方向**共同命中一条 high 级规则即不得同桶；
同桶总体积不得超过单桶容量。

优化目标（严格按序）：
  1. 桶数最少；
  2. 桶数并列时，最大桶负载最小；
  3. 仍并列时，按"桶内试剂名称序列"取字典序最小的方案，保证结果稳定可复现。

废液最多 12 股，采用分支限界精确搜索（限制增长序列枚举集合划分，
每个划分只访问一次），结果与请求顺序无关、可复现。
"""
from __future__ import annotations

import json
from dataclasses import dataclass

EPS = 1e-9
MAX_STREAMS = 12


@dataclass
class WasteStream:
    index: int  # 请求中的顺序（仅用于确定性排序）
    reagent_id: int
    name: str
    categories: list[str]
    volume_ml: float


def build_high_conflicts(streams: list[WasteStream], rules) -> dict[tuple[int, int], list[dict]]:
    """计算所有"任一方向命中 high 规则"的废液对及其规则依据。

    返回 {(i, j): [依据, ...]}（i < j，i/j 为 streams 下标）。
    判定口径与推演引擎一致：无向边双向等价；有向边仅 source->target 触发，
    但分桶是"混合"语义、与先后无关，因此两个方向都检查——任一股的类别
    作为 source、另一股的类别作为 target 命中即构成冲突。
    每条依据保留规则编号、标题、方程式、机理、文献与命中的类别对，
    供界面展示"促使两股分隔的规则依据"。
    """
    high_rules = [r for r in rules if r.severity == "high"]
    conflicts: dict[tuple[int, int], list[dict]] = {}
    n = len(streams)
    for i in range(n):
        cats_i = streams[i].categories
        for j in range(i + 1, n):
            cats_j = streams[j].categories
            basis: list[dict] = []
            seen: set[str] = set()
            for rule in high_rules:
                if rule.id in seen:
                    continue
                # 任一方向：i 的类别作 source 命中 j 的 target，或反之
                if rule.source in cats_i and rule.target in cats_j:
                    cat_a, cat_b = rule.source, rule.target
                elif rule.source in cats_j and rule.target in cats_i:
                    cat_a, cat_b = rule.target, rule.source
                else:
                    continue
                seen.add(rule.id)
                basis.append(
                    {
                        "rule_id": rule.id,
                        "title": rule.title,
                        "reaction": rule.reaction,
                        "rationale": rule.rationale,
                        "references": json.loads(rule.references or "[]"),
                        "severity": "high",
                        "category_a": cat_a,  # 属于第一股废液（streams[i]）的类别
                        "category_b": cat_b,  # 属于第二股废液（streams[j]）的类别
                        "directional": bool(rule.directional),
                    }
                )
            if basis:
                conflicts[(i, j)] = basis
    return conflicts


def solve_bucketing(streams: list[WasteStream], capacity_ml: float, rules) -> dict:
    """分支限界精确求解。调用方需保证单股体积均不超过单桶容量。

    返回 {"assign": [[成员下标...], ...], "conflicts": 冲突字典}；
    assign 已按桶内名称序列字典序排序，展示顺序稳定。
    """
    n = len(streams)
    conflicts = build_high_conflicts(streams, rules)
    conflict_with: list[set[int]] = [set() for _ in range(n)]
    for i, j in conflicts:
        conflict_with[i].add(j)
        conflict_with[j].add(i)

    # 搜索顺序：体积降序（利于剪枝），再按名称、原始序号保证确定性
    order = sorted(
        range(n), key=lambda i: (-streams[i].volume_ml, streams[i].name, streams[i].index)
    )

    barrels: list[list[int]] = []
    loads: list[float] = []
    best: dict = {"key": None, "assign": None}

    def name_key(assign) -> tuple:
        # 方案的规范标识：每桶内名称排序，桶间再按名称元组排序
        per = [tuple(sorted(streams[m].name for m in b)) for b in assign]
        return tuple(sorted(per))

    def record() -> None:
        key = (len(barrels), round(max(loads, default=0.0), 9), name_key(barrels))
        if best["key"] is None or key < best["key"]:
            best["key"] = key
            best["assign"] = [list(b) for b in barrels]

    # 贪心首次适配，先拿一个可行解作为上界，加速后续剪枝
    for i in order:
        for b in range(len(barrels)):
            if (
                loads[b] + streams[i].volume_ml <= capacity_ml + EPS
                and not (conflict_with[i] & set(barrels[b]))
            ):
                barrels[b].append(i)
                loads[b] += streams[i].volume_ml
                break
        else:
            barrels.append([i])
            loads.append(streams[i].volume_ml)
    record()
    barrels.clear()
    loads.clear()

    def recurse(pos: int) -> None:
        if len(barrels) > best["key"][0]:
            return  # 桶数已不可能优于当前最优
        if pos == n:
            record()
            return
        i = order[pos]
        v = streams[i].volume_ml
        best_k, best_max, _ = best["key"]
        # 尝试放入已有桶
        for b in range(len(barrels)):
            new_load = loads[b] + v
            if new_load > capacity_ml + EPS:
                continue
            if conflict_with[i] & set(barrels[b]):
                continue
            # 同桶数前提下，部分解最大负载已不可能更优则剪枝
            if len(barrels) == best_k and new_load > best_max + EPS:
                continue
            barrels[b].append(i)
            old = loads[b]
            loads[b] = new_load
            recurse(pos + 1)
            loads[b] = old
            barrels[b].pop()
        # 开新桶（桶数不超过当前最优才有意义）
        if len(barrels) + 1 <= best_k:
            barrels.append([i])
            loads.append(v)
            recurse(pos + 1)
            loads.pop()
            barrels.pop()

    recurse(0)
    assign = best["assign"]
    # 输出规范化：桶内成员按名称排序，桶间按名称序列字典序排序，展示顺序稳定
    for b in assign:
        b.sort(key=lambda m: (streams[m].name, streams[m].index))
    assign.sort(key=lambda b: tuple(streams[m].name for m in b))
    return {"assign": assign, "conflicts": conflicts}
