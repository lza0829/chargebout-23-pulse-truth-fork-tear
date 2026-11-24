from __future__ import annotations
from datetime import datetime, timedelta
from app.services import pulse_surface

from app.db import db, row_dict, rows
from app.services import energy


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def get_setting(conn, key: str, default: str = "") -> str:
    r = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


def set_setting(conn, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def current_band_price(conn, rate_plan_id: int) -> float:
    plan = conn.execute("SELECT * FROM rate_plans WHERE id=?", (rate_plan_id,)).fetchone()
    if not plan:
        return 0.0
    hour = datetime.now().hour
    if 8 <= hour < 11 or 18 <= hour < 21:
        return float(plan["peak_price"])
    if 23 <= hour or hour < 7:
        return float(plan["valley_price"])
    return float(plan["flat_price"])


def is_day_sealed(conn, day: str | None = None) -> bool:
    d = day or today()
    r = conn.execute("SELECT sealed FROM day_seals WHERE business_day=?", (d,)).fetchone()
    return bool(r and r["sealed"])


def overview(conn) -> dict:
    piles = rows(conn.execute("SELECT status, COUNT(*) AS n FROM piles GROUP BY status").fetchall())
    sess = rows(conn.execute("SELECT status, COUNT(*) AS n FROM sessions GROUP BY status").fetchall())
    pile_map = {p["status"]: p["n"] for p in piles}
    sess_map = {s["status"]: s["n"] for s in sess}
    open_kwh = energy.open_energy(conn)
    today_amount = conn.execute(
        "SELECT COALESCE(SUM(amount),0) AS a FROM sessions WHERE business_day=? AND status='已关账'",
        (today(),),
    ).fetchone()["a"]
    # 实时全局对齐诊断（读侧自愈后此处必为真源口径），陈旧时禁止总览亮「已对齐」
    alignment = energy.global_alignment(conn, auto_heal=True)
    return {
        "桩位空闲": pile_map.get("空闲", 0),
        "桩位占用": pile_map.get("占用", 0),
        "桩位故障": pile_map.get("故障", 0),
        "桩位维护": pile_map.get("维护", 0),
        "充电中": sess_map.get("充电中", 0),
        "结算中": sess_map.get("结算中", 0),
        "已关账": sess_map.get("已关账", 0),
        "已超时": sess_map.get("已超时", 0),
        "已中断": sess_map.get("已中断", 0),
        "在充电量": open_kwh["在充电量"],
        "在充会话数": open_kwh["在充会话数"],
        "电量真源": alignment["真源口径"],
        "全部对齐": alignment["全部对齐"],
        "陈旧会话数": alignment["陈旧会话数"],
        "本日关账金额": round(float(today_amount), 2),
        "营业日": today(),
        "充日已封印": is_day_sealed(conn),
    }


def list_sites(conn):
    return rows(conn.execute("SELECT * FROM sites ORDER BY id").fetchall())


def list_piles(conn, site_id: int | None = None):
    if site_id:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               WHERE p.site_id=? ORDER BY p.id""",
            (site_id,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT p.*, s.name AS site_name, r.name AS rate_name
               FROM piles p JOIN sites s ON s.id=p.site_id
               JOIN rate_plans r ON r.id=p.rate_plan_id
               ORDER BY p.id"""
        ).fetchall()
    return rows(rs)


def list_guns(conn, pile_id: int | None = None):
    if pile_id:
        return rows(
            conn.execute(
                "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id WHERE g.pile_id=? ORDER BY g.id",
                (pile_id,),
            ).fetchall()
        )
    return rows(
        conn.execute(
            "SELECT g.*, p.name AS pile_name FROM guns g JOIN piles p ON p.id=g.pile_id ORDER BY g.id"
        ).fetchall()
    )


def list_rates(conn):
    return rows(conn.execute("SELECT * FROM rate_plans ORDER BY id").fetchall())


def list_sessions(conn, status: str | None = None):
    if status:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               WHERE se.status=? ORDER BY se.id DESC""",
            (status,),
        ).fetchall()
    else:
        rs = conn.execute(
            """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name
               FROM sessions se
               JOIN piles p ON p.id=se.pile_id
               JOIN guns g ON g.id=se.gun_id
               JOIN sites s ON s.id=se.site_id
               ORDER BY se.id DESC LIMIT 200"""
        ).fetchall()
    # 列表统一由脉冲流水重放出电量并挂对齐状态，所有页面同口径
    return energy.decorate_session_rows(conn, rows(rs))


def session_detail(conn, session_id: int, auto_heal: bool = True):
    se = conn.execute(
        """SELECT se.*, p.name AS pile_name, g.name AS gun_name, s.name AS site_name, r.name AS rate_name
           FROM sessions se
           JOIN piles p ON p.id=se.pile_id
           JOIN guns g ON g.id=se.gun_id
           JOIN sites s ON s.id=se.site_id
           JOIN rate_plans r ON r.id=se.rate_plan_id
           WHERE se.id=?""",
        (session_id,),
    ).fetchone()
    if not se:
        return None
    pulses = rows(
        conn.execute(
            "SELECT * FROM pulses WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    receipts = rows(
        conn.execute(
            "SELECT * FROM pulse_receipts WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    events = rows(
        conn.execute(
            "SELECT * FROM session_events WHERE session_id=? ORDER BY id", (session_id,)
        ).fetchall()
    )
    # 详情电量只认真源重放；对齐诊断实时计算（不被历史快照盖住），必要时自愈
    diag = energy.session_alignment(conn, session_id, auto_heal=auto_heal)
    d = row_dict(se)
    d["脉冲"] = pulses
    d["回执"] = receipts
    d["事件"] = events
    d["对齐诊断"] = diag
    d["重放电量"] = diag["重放电量"]
    d["缓存电量"] = diag["缓存电量"]
    d["漂移电量"] = diag["漂移电量"]
    d["对齐状态"] = diag["对齐状态"]
    d["kwh"] = diag["重放电量"]
    return d

_overview_impl = overview


def overview(conn):
    return pulse_surface.overlay_overview(_overview_impl(conn), conn)

