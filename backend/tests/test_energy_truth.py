"""ADR-0001 电量真源联动测试。

覆盖：
1. 脉冲写口同刷（流水/累计列/回执/事件/诊断）且可重放
2. 并发双脉冲不漂格
3. 旁路改累计列被拒绝且不伪装成功
4. 夹具注入累计列篡改 -> 诊断标陈旧、禁止亮已对齐 -> 自愈回真源
5. 半截脉冲（只落流水）-> 诊断标陈旧 -> 自愈
6. 流水齐全累计未动
7. 伪对齐快照盖不住实时诊断
8. 总览/详情/列表/记脉冲页/关账 全链路同口径
"""
from __future__ import annotations

import sqlite3
import threading

from support import EnergyTestCase

from app.db import db
from app.services import energy
from app.services import sessions as svc
from app.services.core import list_sessions, overview, session_detail
from app.services.energy import BypassRejected


class TestReplaySourceOfTruth(EnergyTestCase):
    def test_pulse_write_atomic_and_replayable(self):
        r1 = energy.record_pulse(self.sid, 1.5, 7)
        r2 = energy.record_pulse(self.sid, 2.0, 7)
        # 回执返回重放口径
        self.assertEqual(r1["累计电量"], 1.5)
        self.assertEqual(r2["累计电量"], 3.5)
        self.assertTrue(r2["对齐"])
        with db() as conn:
            # 流水真源可重放
            self.assertEqual(energy.replay_kwh(conn, self.sid), 3.5)
            # 累计列缓存已在同事务校准
            cached = conn.execute(
                "SELECT kwh FROM sessions WHERE id=?", (self.sid,)
            ).fetchone()["kwh"]
            self.assertEqual(round(cached, 3), 3.5)
            # 流水行与回执一一对应
            flow = conn.execute(
                "SELECT COUNT(*) n FROM pulses WHERE session_id=?", (self.sid,)
            ).fetchone()["n"]
            recv = conn.execute(
                "SELECT COUNT(*) n FROM pulse_receipts WHERE session_id=?", (self.sid,)
            ).fetchone()["n"]
            self.assertEqual((flow, recv), (2, 2))
            diag = energy.session_alignment(conn, self.sid, auto_heal=False)
            self.assertEqual(diag["对齐状态"], "已对齐")
            self.assertEqual(diag["漂移电量"], 0.0)

    def test_concurrent_double_pulse_no_drift(self):
        errors: list[Exception] = []

        def fire(delta: float):
            try:
                energy.record_pulse(self.sid, delta, 7)
            except Exception as exc:  # pragma: no cover - 失败时断言可见
                errors.append(exc)

        t1 = threading.Thread(target=fire, args=(1.5,))
        t2 = threading.Thread(target=fire, args=(2.5,))
        t1.start(); t2.start(); t1.join(); t2.join()
        self.assertEqual(errors, [])
        with db() as conn:
            self.assertEqual(energy.replay_kwh(conn, self.sid), 4.0)
            cached = conn.execute(
                "SELECT kwh FROM sessions WHERE id=?", (self.sid,)
            ).fetchone()["kwh"]
            # 不漂一格：缓存严格等于重放，且两笔都在
            self.assertEqual(round(cached, 3), 4.0)
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM pulses WHERE session_id=?", (self.sid,)).fetchone()["n"],
                2,
            )
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM pulse_receipts WHERE session_id=?", (self.sid,)).fetchone()["n"],
                2,
            )

    def test_bypass_update_rejected_and_never_disguised(self):
        energy.record_pulse(self.sid, 1.0)
        # 旁路 SQL 直改累计列：必须抛 BypassRejected
        with self.assertRaises(BypassRejected):
            energy.chaos_bypass_update_cached(self.sid, 99.0)
        with db() as conn:
            # 真源与缓存都未被污染
            self.assertEqual(energy.replay_kwh(conn, self.sid), 1.0)
            self.assertEqual(
                conn.execute("SELECT kwh FROM sessions WHERE id=?", (self.sid,)).fetchone()["kwh"], 1.0
            )
            # 审计留痕，且没有产生任何回执/流水
            kind = conn.execute(
                "SELECT kind FROM bypass_audit ORDER BY id DESC LIMIT 1"
            ).fetchone()["kind"]
            self.assertEqual(kind, "旁路拒绝")
            self.assertEqual(
                conn.execute("SELECT COUNT(*) n FROM pulse_receipts WHERE session_id=?", (self.sid,)).fetchone()["n"],
                1,
            )
            # 原始 sqlite 直连同样被触发器挡住（RAISE(ABORT) 归为 IntegrityError）
            raw = sqlite3.connect(str(self.db_path))
            try:
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute("UPDATE sessions SET kwh=50 WHERE id=?", (self.sid,))
                    raw.commit()
            finally:
                raw.close()

    def test_cached_tamper_marks_stale_then_self_heals(self):
        energy.record_pulse(self.sid, 2.0)
        # 夹具放行注入：列被改、流水没动
        energy.chaos_inject_cached_tamper(self.sid, 8.0)
        with db() as conn:
            diag = energy.session_alignment(conn, self.sid, auto_heal=False)
            self.assertFalse(diag["对齐"])
            self.assertEqual(diag["对齐状态"], "陈旧")
            self.assertEqual(diag["重放电量"], 2.0)
            self.assertEqual(diag["缓存电量"], 8.0)
            self.assertEqual(diag["漂移电量"], -6.0)
            # 全局诊断同样不得亮已对齐
            g = energy.global_alignment(conn, auto_heal=False)
            self.assertFalse(g["全部对齐"])
            self.assertEqual(g["陈旧会话数"], 1)
        # 读侧自愈：展示仍先给重放值，缓存被校准回真源
        with db() as conn:
            healed = energy.session_alignment(conn, self.sid, auto_heal=True)
            self.assertTrue(healed["对齐"])
            self.assertEqual(healed["重放电量"], 2.0)
        with db() as conn:
            self.assertEqual(
                conn.execute("SELECT kwh FROM sessions WHERE id=?", (self.sid,)).fetchone()["kwh"], 2.0
            )
            kinds = [r["kind"] for r in conn.execute("SELECT kind FROM bypass_audit").fetchall()]
            self.assertIn("自愈校准", kinds)

    def test_half_pulse_flow_only_marks_stale_then_heals(self):
        energy.record_pulse(self.sid, 1.0)
        # 半截：只落流水一侧
        energy.chaos_insert_half_pulse(self.sid, 0.7)
        with db() as conn:
            diag = energy.session_alignment(conn, self.sid, auto_heal=False)
            self.assertEqual(diag["对齐状态"], "陈旧")
            self.assertEqual(diag["重放电量"], 1.7)
            self.assertEqual(diag["缓存电量"], 1.0)
            self.assertIn("无回执脉冲", diag["原因"])
        # 自愈后缓存追上重放；无回执的事实仍在原因中（回执不会被补发伪造）
        with db() as conn:
            healed = energy.session_alignment(conn, self.sid, auto_heal=True)
            self.assertEqual(healed["重放电量"], 1.7)
            recv = conn.execute(
                "SELECT COUNT(*) n FROM pulse_receipts WHERE session_id=?", (self.sid,)
            ).fetchone()["n"]
            flow = conn.execute(
                "SELECT COUNT(*) n FROM pulses WHERE session_id=?", (self.sid,)
            ).fetchone()["n"]
            self.assertEqual((flow, recv), (2, 1))
            self.assertFalse(healed["对齐"])  # 回执缺口仍显式标注，不伪装
        # 缓存已等于真源
        with db() as conn:
            self.assertEqual(
                conn.execute("SELECT kwh FROM sessions WHERE id=?", (self.sid,)).fetchone()["kwh"], 1.7
            )

    def test_flow_complete_but_cache_untouched(self):
        # 用夹具档模拟「流水齐全累计未动」：先正常一笔，再只动流水
        energy.record_pulse(self.sid, 1.0)
        energy.chaos_insert_half_pulse(self.sid, 2.0)
        with db() as conn:
            self.assertEqual(energy.replay_kwh(conn, self.sid), 3.0)
            self.assertEqual(
                conn.execute("SELECT kwh FROM sessions WHERE id=?", (self.sid,)).fetchone()["kwh"], 1.0
            )
            diag = energy.session_alignment(conn, self.sid, auto_heal=False)
            self.assertFalse(diag["对齐"])

    def test_fake_aligned_snapshot_cannot_cover_live_drift(self):
        energy.record_pulse(self.sid, 1.0)
        energy.chaos_fake_aligned_snapshot(self.sid)
        with db() as conn:
            # 历史快照里确有伪造的已对齐
            snap = conn.execute(
                "SELECT status FROM alignment_snapshots WHERE reason='chaos伪造快照'"
            ).fetchone()
            self.assertEqual(snap["status"], "已对齐")
            # 实时诊断只认真源重放，照样判陈旧
            diag = energy.session_alignment(conn, self.sid, auto_heal=False)
            self.assertEqual(diag["对齐状态"], "陈旧")
            self.assertNotEqual(diag["重放电量"], diag["缓存电量"])

    def test_pulses_append_only(self):
        energy.record_pulse(self.sid, 1.0)
        raw = sqlite3.connect(str(self.db_path))
        try:
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute("UPDATE pulses SET delta_kwh=99 WHERE session_id=?", (self.sid,))
                raw.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                raw.execute("DELETE FROM pulses WHERE session_id=?", (self.sid,))
                raw.commit()
        finally:
            raw.close()


class TestReadModelsSameTruth(EnergyTestCase):
    def test_overview_detail_list_all_follow_replay(self):
        energy.record_pulse(self.sid, 1.5)
        energy.record_pulse(self.sid, 2.5)
        # 制造漂移，证明所有读面都给重放值而非缓存
        energy.chaos_inject_cached_tamper(self.sid, 9.0)
        with db() as conn:
            # 不自愈口径下，诊断条必须先戳穿陈旧、禁止亮已对齐
            stale = energy.global_alignment(conn, auto_heal=False)
            self.assertFalse(stale["全部对齐"])
            self.assertEqual(stale["陈旧会话数"], 1)
        with db() as conn:
            ov = overview(conn)  # 总览读侧自愈
            self.assertEqual(ov["在充电量"], 4.0)       # 总览认真源
            self.assertTrue(ov["全部对齐"])             # 自愈后回到对齐
            detail = session_detail(conn, self.sid)     # 默认自愈
            self.assertEqual(detail["kwh"], 4.0)
            self.assertEqual(detail["重放电量"], 4.0)
            self.assertEqual(detail["对齐诊断"]["对齐状态"], "已对齐")
            lst = list_sessions(conn, "充电中")
            row = next(r for r in lst if r["id"] == self.sid)
            self.assertEqual(row["kwh"], 4.0)
            self.assertEqual(row["重放电量"], 4.0)

    def test_pulse_page_response_carries_replay_and_receipt(self):
        r = svc.add_pulse(self.sid, 3.0, 7)
        self.assertEqual(r["累计电量"], 3.0)
        self.assertEqual(r["重放电量"], 3.0)
        self.assertTrue(r["对齐"])
        self.assertIn("回执编号", r)

    def test_settle_replays_before_close(self):
        energy.record_pulse(self.sid, 2.0)
        # 关账前把缓存改坏
        energy.chaos_inject_cached_tamper(self.sid, 5.0)
        out = svc.settle  # 关账走带 conn 的服务函数
        with db() as conn:
            res = svc.settle(conn, self.sid)
        self.assertEqual(res["电量"], 2.0)              # 关账电量=重放，不被坏缓存带跑
        self.assertEqual(res["金额"], round(2.0 * 0.9, 2))
        with db() as conn:
            se = conn.execute("SELECT * FROM sessions WHERE id=?", (self.sid,)).fetchone()
            self.assertEqual(se["status"], "已关账")
            self.assertEqual(round(se["kwh"], 3), 2.0)

    def test_rejected_bypass_does_not_move_amount_or_flow(self):
        energy.record_pulse(self.sid, 1.0)
        with self.assertRaises(BypassRejected):
            energy.chaos_bypass_update_cached(self.sid, 10.0)
        with db() as conn:
            se = conn.execute("SELECT amount,kwh FROM sessions WHERE id=?", (self.sid,)).fetchone()
            self.assertEqual(round(se["kwh"], 3), 1.0)
            self.assertEqual(se["amount"], round(1.0 * 0.9, 2))


if __name__ == "__main__":
    unittest.main(verbosity=2)
