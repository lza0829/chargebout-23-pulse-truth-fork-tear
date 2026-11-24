"""ADR-0001 电量真源域：以脉冲流水重放为准，累计列仅作加速缓存。

本模块是全局唯一允许触碰 sessions.kwh 缓存的业务代码，且只能通过
write_guard 的「内部校准」模式触碰；任何外部旁路 SQL 在守卫 0 档下被触发器回滚。
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager

from app.db import connect


def _now() -> str:
    # 延迟依赖 core.now_str，避免 core <-> energy 顶层循环导入
    from app.services.core import now_str
    return now_str()


# write_guard.kwh_write_mode 三档
MODE_BLOCK = 0       # 旁路一律回滚
MODE_FIXTURE = 1     # 仅 chaos 夹具放行（复现故障用，全程留痕）
MODE_INTERNAL = 2    # 内部校准：脉冲写口 / 读侧自愈 / 关账校准

OPEN_STATUSES = ("充电中", "结算中")
_ROUND = 3


class BypassRejected(RuntimeError):
    """旁路改累计列被库层触发器拒绝（整笔回滚）。"""


def _q3(x: float) -> float:
    return round(float(x), _ROUND)


# ---------- 真源：重放 ----------

def replay_kwh(conn: sqlite3.Connection, session_id: int) -> float:
    """由脉冲流水重放会话累计电量。这是唯一权威口径。"""
    row = conn.execute(
        "SELECT COALESCE(SUM(delta_kwh),0) AS k FROM pulses WHERE session_id=?",
        (session_id,),
    ).fetchone()
    return _q3(row["k"])


def replay_map(conn: sqlite3.Connection, session_ids: list[int] | None = None) -> dict[int, float]:
    """批量重放，避免列表页 N+1。"""
    if session_ids is not None:
        if not session_ids:
            return {}
        marks = ",".join("?" * len(session_ids))
        rs = conn.execute(
            f"SELECT session_id, SUM(delta_kwh) AS k FROM pulses "
            f"WHERE session_id IN ({marks}) GROUP BY session_id",
            tuple(session_ids),
        ).fetchall()
    else:
        rs = conn.execute(
            "SELECT session_id, SUM(delta_kwh) AS k FROM pulses GROUP BY session_id"
        ).fetchall()
    return {r["session_id"]: _q3(r["k"]) for r in rs}


def pulse_count_map(conn: sqlite3.Connection) -> dict[int, int]:
    rs = conn.execute("SELECT session_id, COUNT(*) AS n FROM pulses GROUP BY session_id").fetchall()
    return {r["session_id"]: r["n"] for r in rs}


def receipt_count_map(conn: sqlite3.Connection) -> dict[int, int]:
    rs = conn.execute(
        "SELECT session_id, COUNT(*) AS n FROM pulse_receipts GROUP BY session_id"
    ).fetchall()
    return {r["session_id"]: r["n"] for r in rs}


# ---------- 守卫与审计 ----------

def _set_mode(conn: sqlite3.Connection, mode: int) -> None:
    conn.execute("UPDATE write_guard SET kwh_write_mode=? WHERE id=1", (mode,))


def _audit(conn: sqlite3.Connection, kind: str, session_id: int | None,
           attempted: float | None, replayed: float | None, detail: str) -> None:
    conn.execute(
        """INSERT INTO bypass_audit(session_id,kind,attempted_kwh,replayed_kwh,detail,created_at)
           VALUES(?,?,?,?,?,?)""",
        (session_id, kind, attempted, replayed, detail, _now()),
    )


def _write_snapshot(conn: sqlite3.Connection, session_id: int, replayed: float,
                    cached_before: float, status: str, reason: str) -> None:
    conn.execute(
        """INSERT INTO alignment_snapshots
           (session_id,replayed_kwh,cached_kwh,drift_kwh,status,reason,created_at)
           VALUES(?,?,?,?,?,?,?)""",
        (session_id, replayed, cached_before, _q3(replayed - cached_before),
         status, reason, _now()),
    )


# ---------- 内部校准（唯一合法的累计列写入方式） ----------

def _calibrate_cache(conn: sqlite3.Connection, session_id: int, replayed: float,
                     reason: str, recalc_amount: bool) -> float:
    se = conn.execute("SELECT kwh, status, price_locked, rate_plan_id FROM sessions WHERE id=?",
                      (session_id,)).fetchone()
    cached_before = float(se["kwh"])
    if _q3(cached_before) == _q3(replayed) and not recalc_amount:
        return cached_before
    _set_mode(conn, MODE_INTERNAL)
    try:
        if recalc_amount and se["status"] in OPEN_STATUSES:
            from app.services.core import current_band_price
            price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
            amount = round(replayed * price, 2)
            conn.execute(
                "UPDATE sessions SET kwh=?, amount=? WHERE id=?",
                (replayed, amount, session_id),
            )
        else:
            conn.execute("UPDATE sessions SET kwh=? WHERE id=?", (replayed, session_id))
    finally:
        _set_mode(conn, MODE_BLOCK)
    status = "已对齐" if _q3(cached_before) == _q3(replayed) else "陈旧"
    _write_snapshot(conn, session_id, replayed, cached_before, status, reason)
    return cached_before


# ---------- 事务封装：BEGIN IMMEDIATE 串行化，双脉冲不漂格 ----------

@contextmanager
def immediate_tx(retries: int = 30, delay: float = 0.05):
    conn = connect()
    attempt = 0
    try:
        while True:
            try:
                conn.execute("BEGIN IMMEDIATE")
                break
            except sqlite3.OperationalError as exc:  # database is locked
                attempt += 1
                if attempt > retries:
                    raise
                time.sleep(delay)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------- 脉冲写口：流水 / 缓存 / 回执 / 事件 / 诊断 同一事务同刷 ----------

def record_pulse(session_id: int, delta_kwh: float, power_kw: float = 0,
                 note: str = "") -> dict:
    if delta_kwh <= 0:
        raise ValueError("脉冲电量须大于零")
    delta = float(delta_kwh)
    with immediate_tx() as conn:
        se = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if not se:
            raise LookupError("会话不存在")
        if se["status"] != "充电中":
            raise RuntimeError(f"会话状态为{se['status']}，不可记脉冲")
        ts = _now()

        # 1) 真源先行：写脉冲流水（只追加）
        cur = conn.execute(
            "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at,note) VALUES(?,?,?,?,?)",
            (session_id, delta, float(power_kw), ts, note),
        )
        pulse_id = cur.lastrowid

        # 2) 由流水重放，绝不基于旧缓存累加
        replayed = replay_kwh(conn, session_id)

        # 3) 内部校准累计列缓存 + 预估金额
        from app.services.core import current_band_price
        price = float(se["price_locked"] or current_band_price(conn, se["rate_plan_id"]))
        amount = round(replayed * price, 2)
        cached_before = _calibrate_cache(conn, session_id, replayed, "脉冲写口校准", False)
        conn.execute("UPDATE sessions SET amount=? WHERE id=?", (amount, session_id))

        # 4) 脉冲回执：与流水行一一对应；旁路永远拿不到回执
        conn.execute(
            """INSERT INTO pulse_receipts(pulse_id,session_id,delta_kwh,replayed_kwh,created_at)
               VALUES(?,?,?,?,?)""",
            (pulse_id, session_id, delta, replayed, ts),
        )

        # 5) 事件 + 对齐诊断快照（已对齐）
        conn.execute(
            "INSERT INTO session_events(session_id,action,created_at,note) VALUES(?,?,?,?)",
            (session_id, "脉冲", ts, f"+{_q3(delta)} => 重放{replayed}"),
        )
        _write_snapshot(conn, session_id, replayed, cached_before, "已对齐", "脉冲写口同刷")

        return {
            "会话编号": session_id,
            "脉冲编号": pulse_id,
            "回执编号": conn.execute(
                "SELECT id FROM pulse_receipts WHERE pulse_id=?", (pulse_id,)
            ).fetchone()["id"],
            "本次增量": _q3(delta),
            "累计电量": replayed,
            "重放电量": replayed,
            "缓存电量": replayed,
            "预估金额": amount,
            "对齐": True,
            "脉冲时间": ts,
        }


# ---------- 读侧：对齐诊断 + 自愈 ----------

def session_alignment(conn: sqlite3.Connection, session_id: int,
                      auto_heal: bool = True) -> dict:
    se = conn.execute("SELECT kwh, status FROM sessions WHERE id=?", (session_id,)).fetchone()
    if not se:
        raise LookupError("会话不存在")
    replayed = replay_kwh(conn, session_id)
    cached = float(se["kwh"])
    flow_n = conn.execute(
        "SELECT COUNT(*) AS n FROM pulses WHERE session_id=?", (session_id,)
    ).fetchone()["n"]
    receipt_n = conn.execute(
        "SELECT COUNT(*) AS n FROM pulse_receipts WHERE session_id=?", (session_id,)
    ).fetchone()["n"]

    reasons: list[str] = []
    if _q3(replayed) != _q3(cached):
        reasons.append("累计列与脉冲流水重放不一致")
    if flow_n != receipt_n:
        reasons.append(f"流水{flow_n}行/回执{receipt_n}条，存在无回执脉冲（半截写流水）")

    aligned = not reasons
    diag = {
        "会话编号": session_id,
        "重放电量": replayed,
        "缓存电量": _q3(cached),
        "漂移电量": _q3(replayed - cached),
        "流水行数": flow_n,
        "回执条数": receipt_n,
        "对齐": aligned,
        "对齐状态": "已对齐" if aligned else "陈旧",
        "原因": "；".join(reasons),
    }

    if not aligned and auto_heal:
        # 自愈：以重放为准校准缓存，留诊断快照与旁路审计痕迹；展示值仍取自重放
        before = _calibrate_cache(conn, session_id, replayed, "读侧自愈校准", True)
        _audit(conn, "自愈校准", session_id, before, replayed, diag["原因"])
        diag["自愈"] = {"校准前缓存": _q3(before), "校准后": replayed}
        # 自愈后实时重算：列漂移可消除；流水/回执缺口（半截脉冲）仍须显式标注
        cached_after = float(conn.execute(
            "SELECT kwh FROM sessions WHERE id=?", (session_id,)
        ).fetchone()["kwh"])
        remaining: list[str] = []
        if _q3(replayed) != _q3(cached_after):
            remaining.append("累计列与脉冲流水重放不一致")
        if flow_n != receipt_n:
            remaining.append(f"流水{flow_n}行/回执{receipt_n}条，存在无回执脉冲（半截写流水）")
        diag["对齐"] = not remaining
        diag["对齐状态"] = "已对齐" if not remaining else "陈旧"
        diag["原因"] = "；".join(remaining)
        diag["缓存电量"] = _q3(cached_after)
        diag["漂移电量"] = _q3(replayed - cached_after)
    return diag


def global_alignment(conn: sqlite3.Connection, auto_heal: bool = True) -> dict:
    ids = [r["id"] for r in conn.execute("SELECT id FROM sessions ORDER BY id").fetchall()]
    items = [session_alignment(conn, sid, auto_heal=auto_heal) for sid in ids]
    stale = [d for d in items if not d["对齐"]]
    return {
        "真源口径": "脉冲流水重放（sessions.kwh 仅为缓存）",
        "会话总数": len(items),
        "陈旧会话数": len(stale),
        "全部对齐": not stale,
        "明细": items,
    }


def open_energy(conn: sqlite3.Connection) -> dict:
    """总览在充电量：只认真源重放，不读缓存列。"""
    row = conn.execute(
        """
        SELECT COALESCE(SUM(r.k),0) AS k, COUNT(*) AS n FROM (
            SELECT p.session_id AS sid, SUM(p.delta_kwh) AS k
            FROM pulses p JOIN sessions s ON s.id = p.session_id
            WHERE s.status IN ('充电中','结算中')
            GROUP BY p.session_id
        ) r
        """
    ).fetchone()
    return {"在充电量": _q3(row["k"]), "在充会话数": row["n"]}


def decorate_session_rows(conn: sqlite3.Connection, rows_: list[dict]) -> list[dict]:
    """会话册/记脉冲/结算列表统一挂重放电量与对齐状态，杜绝各页面口径分裂。"""
    ids = [r["id"] for r in rows_]
    rmap = replay_map(conn, ids)
    pmap = pulse_count_map(conn)
    rcmap = receipt_count_map(conn)
    for r in rows_:
        replayed = rmap.get(r["id"], 0.0)
        cached = float(r["kwh"])
        r["重放电量"] = replayed
        r["缓存电量"] = _q3(cached)
        r["漂移电量"] = _q3(replayed - cached)
        r["对齐"] = _q3(replayed) == _q3(cached) and pmap.get(r["id"], 0) == rcmap.get(r["id"], 0)
        r["对齐状态"] = "已对齐" if r["对齐"] else "陈旧"
        r["kwh"] = replayed  # 列表展示电量一律取真源重放值
    return rows_


def close_replay(conn: sqlite3.Connection, session_id: int) -> tuple[float, float]:
    """关账前强制重放并校准缓存，返回（重放电量, 预估金额）。"""
    replayed = replay_kwh(conn, session_id)
    _calibrate_cache(conn, session_id, replayed, "关账前重放校准", True)
    se = conn.execute("SELECT amount FROM sessions WHERE id=?", (session_id,)).fetchone()
    return replayed, float(se["amount"])


# ---------- chaos 夹具：复现四类故障，全部留痕，与脉冲写口物理隔离 ----------

def chaos_bypass_update_cached(session_id: int, fake_kwh: float) -> None:
    """夹具1：旁路改累计列（守卫0档必须被拒绝并审计）。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replayed = replay_kwh(conn, session_id)
        try:
            conn.execute("UPDATE sessions SET kwh=? WHERE id=?", (float(fake_kwh), session_id))
            raise AssertionError("旁路更新竟然成功，触发器未生效")
        except sqlite3.IntegrityError as exc:  # RAISE(ABORT) 在 py 端为 IntegrityError
            _audit(conn, "旁路拒绝", session_id, float(fake_kwh), replayed, str(exc))
            conn.commit()
            raise BypassRejected(str(exc)) from exc
    finally:
        conn.close()


def chaos_inject_cached_tamper(session_id: int, fake_kwh: float) -> dict:
    """夹具2：守卫切夹具档，强行制造「累计列被改、流水没动」的陈旧态。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replayed = replay_kwh(conn, session_id)
        _set_mode(conn, MODE_FIXTURE)
        try:
            conn.execute("UPDATE sessions SET kwh=? WHERE id=?", (float(fake_kwh), session_id))
        finally:
            _set_mode(conn, MODE_BLOCK)
        _audit(conn, "夹具注入-改累计列", session_id, float(fake_kwh), replayed,
               "chaos 夹具放行，仅用于复现陈旧态")
        conn.commit()
        return {"会话编号": session_id, "注入缓存值": float(fake_kwh), "真源重放": replayed}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def chaos_insert_half_pulse(session_id: int, delta_kwh: float) -> dict:
    """夹具3：半截脉冲——只插流水行，不写回执、不动累计列。"""
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        ts = _now()
        cur = conn.execute(
            "INSERT INTO pulses(session_id,delta_kwh,power_kw,created_at,note) VALUES(?,?,?,?,?)",
            (session_id, float(delta_kwh), 0.0, ts, "chaos半截脉冲"),
        )
        replayed = replay_kwh(conn, session_id)
        cached = conn.execute(
            "SELECT kwh FROM sessions WHERE id=?", (session_id,)
        ).fetchone()["kwh"]
        _audit(conn, "夹具注入-半截流水", session_id, float(delta_kwh), replayed,
               f"只落流水行 pulse_id={cur.lastrowid}，无回执无缓存更新")
        conn.commit()
        return {
            "会话编号": session_id,
            "脉冲编号": cur.lastrowid,
            "真源重放": replayed,
            "缓存停留": float(cached),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def chaos_fake_aligned_snapshot(session_id: int) -> dict:
    """夹具4：写一条谎称「已对齐」的诊断快照，再制造实际漂移。

    证明读侧诊断条只认真时重放、不被历史快照盖住（诊断亮齐但页面对不上必须被戳穿）。
    """
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        replayed = replay_kwh(conn, session_id)
        cached = float(conn.execute(
            "SELECT kwh FROM sessions WHERE id=?", (session_id,)
        ).fetchone()["kwh"])
        # 先放假快照（值按当时缓存伪造为已对齐）
        conn.execute(
            """INSERT INTO alignment_snapshots
               (session_id,replayed_kwh,cached_kwh,drift_kwh,status,reason,created_at)
               VALUES(?,?,?,0,'已对齐','chaos伪造快照',?)""",
            (session_id, cached, cached, _now()),
        )
        # 再让累计列与真源分叉
        fake = _q3(cached + 99.0)
        _set_mode(conn, MODE_FIXTURE)
        try:
            conn.execute("UPDATE sessions SET kwh=? WHERE id=?", (fake, session_id))
        finally:
            _set_mode(conn, MODE_BLOCK)
        _audit(conn, "夹具注入-伪诊断", session_id, fake, replayed,
               "历史快照谎称已对齐，实时重放应判陈旧")
        conn.commit()
        return {"会话编号": session_id, "伪造快照": "已对齐", "实时真源": replayed, "缓存值": fake}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
