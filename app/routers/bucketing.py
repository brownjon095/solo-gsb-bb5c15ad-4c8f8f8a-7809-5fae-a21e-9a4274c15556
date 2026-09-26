"""废液分桶规划：不落库的预览接口。

复用试剂档案与当前全局启用规则（severity=high 的规则促使两股废液分隔），
在单桶容量约束下求桶数最少的完整方案；不读取方案规则版本，
也不改动现有方案推演与规则版本逻辑。
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from .. import bucketing
from ..database import get_db
from ..schemas import BucketPlanPreviewIn

router = APIRouter(prefix="/api/bucketing", tags=["bucketing"])


@router.post("/preview")
def preview_bucket_plan(payload: BucketPlanPreviewIn, db: Session = Depends(get_db)):
    """废液分桶预览（不落库）。

    - 任一方向命中 high 规则的两股废液不可同桶，同桶总体积不得超过单桶容量；
    - 返回桶数最少的完整方案；同为最少桶时最大桶负载最小，
      再按桶内试剂名称序列确定稳定结果；
    - 单股体积超过单桶容量时返回明确原因（422），不生成部分方案。
    """
    try:
        return bucketing.build_plan(db, payload.streams, payload.bucket_capacity_ml)
    except bucketing.BucketError as exc:
        raise HTTPException(exc.status_code, detail=exc.detail) from exc
