"""ADR-0001 API 层端到端：直接调用路由函数，验证脉冲写口、对齐诊断、chaos 路由的对外契约。"""
from __future__ import annotations

from fastapi import HTTPException
from pydantic import ValidationError

from support import EnergyTestCase

from app.db import db
from app.routers import sessions as sess_router
from app.routers import chaos as chaos_router
from app.routers.sessions import PulseIn
from app.services import energy


class TestApiContract(EnergyTestCase):
    def test_pulse_endpoint_returns_receipt_and_replay(self):
        out = sess_router.add_pulse(self.sid, PulseIn(电量=2.0, 功率=7))
        self.assertEqual(out["累计电量"], 2.0)
        self.assertEqual(out["重放电量"], 2.0)
        self.assertTrue(out["对齐"])
        self.assertIn("回执编号", out)
        # 第二笔，重放累计而非缓存累加
        out2 = sess_router.add_pulse(self.sid, PulseIn(电量=1.5, 功率=7))
        self.assertEqual(out2["累计电量"], 3.5)

    def test_pulse_endpoint_rejects_non_positive(self):
        # 入参边界由 pydantic Field(gt=0) 拦截
        with self.assertRaises(ValidationError):
            PulseIn(电量=0)
        # 服务层守卫对非正增量同样拒绝（绕过 pydantic 直调）
        from app.services import sessions as svc
        with self.assertRaises(HTTPException) as ctx:
            svc.add_pulse(self.sid, -1.0)
        self.assertEqual(ctx.exception.status_code, 400)

    def test_alignment_endpoints(self):
        sess_router.add_pulse(self.sid, PulseIn(电量=1.0))
        one = sess_router.get_alignment(self.sid)
        self.assertEqual(one["对齐状态"], "已对齐")
        g = sess_router.get_global_alignment()
        self.assertTrue(g["全部对齐"])

    def test_chaos_bypass_route_reports_rejected(self):
        sess_router.add_pulse(self.sid, PulseIn(电量=1.0))
        r = chaos_router.bypass_cached(chaos_router.TamperIn(会话编号=self.sid, 伪造电量=42))
        self.assertTrue(r["被拒绝"])
        self.assertIn("拒绝", r["拒因"])
        # 真源不动
        with db() as conn:
            self.assertEqual(energy.replay_kwh(conn, self.sid), 1.0)

    def test_chaos_tamper_then_alignment_flags_stale_and_heals(self):
        sess_router.add_pulse(self.sid, PulseIn(电量=1.0))
        chaos_router.inject_tamper(chaos_router.TamperIn(会话编号=self.sid, 伪造电量=7))
        stale = sess_router.get_alignment(self.sid, 自愈=False)
        self.assertEqual(stale["对齐状态"], "陈旧")
        self.assertEqual(stale["重放电量"], 1.0)
        # 自愈后
        healed = sess_router.get_alignment(self.sid, 自愈=True)
        self.assertEqual(healed["重放电量"], 1.0)
        audit = chaos_router.list_bypass_audit()
        kinds = [a["kind"] for a in audit]
        self.assertIn("夹具注入-改累计列", kinds)
        self.assertIn("自愈校准", kinds)

    def test_chaos_half_pulse_route(self):
        sess_router.add_pulse(self.sid, PulseIn(电量=1.0))
        r = chaos_router.inject_half(chaos_router.HalfIn(会话编号=self.sid, 增量=0.5))
        self.assertEqual(r["真源重放"], 1.5)
        self.assertEqual(r["缓存停留"], 1.0)
        diag = sess_router.get_alignment(self.sid, 自愈=False)
        self.assertIn("无回执脉冲", diag["原因"])

    def test_chaos_fake_snapshot_route(self):
        sess_router.add_pulse(self.sid, PulseIn(电量=1.0))
        r = chaos_router.inject_fake_snapshot(chaos_router.HalfIn(会话编号=self.sid))
        self.assertEqual(r["伪造快照"], "已对齐")
        diag = sess_router.get_alignment(self.sid, 自愈=False)
        self.assertEqual(diag["对齐状态"], "陈旧")

    def test_session_detail_endpoint_carries_diagnosis_and_receipts(self):
        sess_router.add_pulse(self.sid, PulseIn(电量=1.0, 功率=6))
        d = sess_router.get_session(self.sid)
        self.assertEqual(d["kwh"], 1.0)
        self.assertEqual(d["重放电量"], 1.0)
        self.assertEqual(d["对齐诊断"]["对齐状态"], "已对齐")
        self.assertEqual(len(d["脉冲"]), 1)
        self.assertEqual(len(d["回执"]), 1)
        self.assertEqual(d["回执"][0]["pulse_id"], d["脉冲"][0]["id"])

    def test_settle_endpoint_uses_replay(self):
        sess_router.add_pulse(self.sid, PulseIn(电量=2.0))
        # 关账前注入坏缓存
        chaos_router.inject_tamper(chaos_router.TamperIn(会话编号=self.sid, 伪造电量=9))
        out = sess_router.settle(self.sid)
        self.assertEqual(out["电量"], 2.0)  # 重放，不是 9
        self.assertEqual(out["状态"], "已关账")
