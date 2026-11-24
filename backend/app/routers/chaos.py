"""ADR-0001 chaos 夹具路由：仅用于复现四类电量真源故障，全部留痕，不参与正常业务。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.db import db
from app.services import energy
from app.services.energy import BypassRejected

router = APIRouter(prefix="/chaos")


class TamperIn(BaseModel):
    会话编号: int
    伪造电量: float


class HalfIn(BaseModel):
    会话编号: int
    增量: float = 1.0


@router.post("/旁路改累计列")
def bypass_cached(body: TamperIn):
    try:
        energy.chaos_bypass_update_cached(body.会话编号, body.伪造电量)
    except BypassRejected as exc:
        return {"被拒绝": True, "拒因": str(exc)}
    raise HTTPException(500, detail="旁路竟未被拒绝，守卫失效")


@router.post("/注入累计列篡改")
def inject_tamper(body: TamperIn):
    return energy.chaos_inject_cached_tamper(body.会话编号, body.伪造电量)


@router.post("/注入半截脉冲")
def inject_half(body: HalfIn):
    return energy.chaos_insert_half_pulse(body.会话编号, body.增量)


@router.post("/注入伪对齐快照")
def inject_fake_snapshot(body: HalfIn):
    # 该夹具只需会话编号，增量字段忽略
    return energy.chaos_fake_aligned_snapshot(body.会话编号)


@router.get("/旁路审计")
def list_bypass_audit():
    with db() as conn:
        rs = conn.execute(
            "SELECT * FROM bypass_audit ORDER BY id DESC LIMIT 200"
        ).fetchall()
        from app.db import rows
        return rows(rs)
