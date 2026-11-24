"""ADR-0001 测试公共夹具：每个用例独立临时库 + 一个充电中会话。"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))


class EnergyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "test.db"
        os.environ["CHARGEBOUT_DB"] = str(self.db_path)
        # 临时测试库：关掉逐笔 fsync（WSL2 下 fsync 极慢，不影响正确性验证）
        os.environ["CHARGEBOUT_SYNCHRONOUS"] = "OFF"
        # 延迟导入，确保 config 读到本用例的 DB_PATH
        from app import config  # noqa: F401
        import app.config as cfg
        cfg.DB_PATH = self.db_path
        cfg.DB_SYNCHRONOUS = "OFF"
        import app.db as dbmod
        dbmod.DB_PATH = self.db_path
        dbmod.DB_SYNCHRONOUS = "OFF"
        dbmod.init_db()
        from app.services import sessions as svc  # noqa: F401
        from app.db import db
        with db() as conn:
            self._seed_basics(conn)
        with db() as conn:
            self.sid = self._open_session(conn)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _seed_basics(self, conn) -> None:
        conn.execute(
            "INSERT INTO rate_plans(id,name,peak_price,flat_price,valley_price) VALUES(1,'测试档',1.2,0.9,0.55)"
        )
        conn.execute("INSERT INTO sites(id,name,address,active) VALUES(1,'测试站','x',1)")
        conn.execute(
            "INSERT INTO piles(id,site_id,name,rated_kw,status,rate_plan_id) VALUES(1,1,'测试桩',7,'空闲',1)"
        )
        conn.execute("INSERT INTO guns(id,pile_id,name,status) VALUES(1,1,'左枪','可用')")
        conn.execute(
            "INSERT INTO day_seals(business_day,sealed,sealed_at,note) VALUES('2026-11-24',0,NULL,'')"
        )
        conn.execute("INSERT INTO settings(key,value) VALUES('会话序号','1000')")

    def _open_session(self, conn) -> int:
        from app.services.sessions import open_session
        # open_session 依赖 today()/is_day_sealed，给当天补封印行后直接调用
        cur = conn.execute(
            """INSERT INTO sessions(slip_no,site_id,pile_id,gun_id,rate_plan_id,business_day,
               status,kwh,amount,price_locked,started_at)
               VALUES('充1001',1,1,1,1,'2026-11-24','充电中',0,0,0.9,'2026-11-24 09:00:00')"""
        )
        conn.execute("UPDATE piles SET status='占用' WHERE id=1")
        conn.execute("UPDATE guns SET status='占用' WHERE id=1")
        return cur.lastrowid
