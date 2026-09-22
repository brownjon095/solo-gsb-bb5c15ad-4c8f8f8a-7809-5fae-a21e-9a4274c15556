"""废液分桶规划：复用试剂档案与当前全局启用规则的预览接口（不落库）。"""
import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import models, rules_store
from ..bucketing import EPS, WasteStream, solve_bucketing
from ..database import get_db
from ..schemas import BucketingPreviewIn

router = APIRouter(prefix="/api/bucketing", tags=["bucketing"])


@router.post("/preview")
def preview_bucketing(payload: BucketingPreviewIn, db: Session = Depends(get_db)):
    """废液分桶预览（不落库，不改动任何方案 / 规则版本数据）。

    - 任一方向命中 high 级启用规则的两股废液不可同桶；
    - 同桶总体积不得超过单桶容量；
    - 目标依次为：桶数最少 → 最大桶负载最小 → 桶内试剂名称序列字典序最小；
    - 单股体积超过单桶容量时返回明确原因，且不生成任何部分方案。
    """
    ids = [s.reagent_id for s in payload.streams]
    if len(set(ids)) != len(ids):
        raise HTTPException(422, "同一试剂不能重复作为两股废液，请合并体积后作为一股")

    reagents: dict[int, models.Reagent] = {}
    for rid in ids:
        reagent = db.get(models.Reagent, rid)
        if reagent is None:
            raise HTTPException(404, f"试剂 id={rid} 不存在")
        reagents[rid] = reagent

    streams = [
        WasteStream(
            index=idx,
            reagent_id=s.reagent_id,
            name=reagents[s.reagent_id].name,
            categories=json.loads(reagents[s.reagent_id].categories or "[]"),
            volume_ml=s.volume_ml,
        )
        for idx, s in enumerate(payload.streams)
    ]

    capacity = payload.capacity_ml
    oversized = [st for st in streams if st.volume_ml > capacity + EPS]
    if oversized:
        names = "、".join(f"「{st.name}」{st.volume_ml:g} mL" for st in oversized)
        raise HTTPException(
            422,
            detail={
                "message": (
                    f"单股废液体积超过单桶容量 {capacity:g} mL：{names}。"
                    "请增大单桶容量或拆分该股废液；未生成任何分桶方案。"
                ),
                "oversized": [
                    {
                        "reagent_id": st.reagent_id,
                        "name": st.name,
                        "volume_ml": st.volume_ml,
                        "capacity_ml": capacity,
                    }
                    for st in oversized
                ],
            },
        )

    # 当前全局启用规则图（与方案规则版本无关，不读取任何版本快照）
    rules = db.query(models.Rule).filter(models.Rule.active.is_(True)).all()
    solved = solve_bucketing(streams, capacity, rules)
    assign = solved["assign"]
    conflicts = solved["conflicts"]

    barrels_out = []
    for b_idx, members in enumerate(assign, start=1):
        ordered = sorted(members, key=lambda m: (streams[m].name, streams[m].index))
        load = sum(streams[m].volume_ml for m in members)
        barrels_out.append(
            {
                "index": b_idx,
                "load_ml": round(load, 6),
                "utilization": round(load / capacity, 4),
                "items": [
                    {
                        "reagent_id": streams[m].reagent_id,
                        "name": streams[m].name,
                        "volume_ml": streams[m].volume_ml,
                        "categories": streams[m].categories,
                    }
                    for m in ordered
                ],
            }
        )

    separations = []
    for (i, j), basis in conflicts.items():
        separations.append(
            {
                "stream_a": {
                    "reagent_id": streams[i].reagent_id,
                    "name": streams[i].name,
                },
                "stream_b": {
                    "reagent_id": streams[j].reagent_id,
                    "name": streams[j].name,
                },
                "rules": basis,
            }
        )
    separations.sort(key=lambda x: (x["stream_a"]["name"], x["stream_b"]["name"]))

    return {
        "kind": "labmix-bucketing-preview",
        "rule_revision": rules_store.get_rule_revision(),
        "capacity_ml": capacity,
        "stream_count": len(streams),
        "total_volume_ml": round(sum(st.volume_ml for st in streams), 6),
        "barrel_count": len(barrels_out),
        "max_load_ml": max((b["load_ml"] for b in barrels_out), default=0.0),
        "conflict_pair_count": len(separations),
        "barrels": barrels_out,
        "separations": separations,
    }
