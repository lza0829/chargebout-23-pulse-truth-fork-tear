from __future__ import annotations
import os
from pathlib import Path

PROJECT_NAME = "充桩条"
ROOT = Path(__file__).resolve().parents[2]
DB_PATH = Path(os.environ.get("CHARGEBOUT_DB", str(ROOT / "data" / "chargebout.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# WAL + NORMAL：WAL 模式下 NORMAL 是 SQLite 官方推荐档，崩溃不损库，且不逐笔 fsync。
# 测试临时库可用 CHARGEBOUT_SYNCHRONOUS=OFF 关掉 fsync（WSL2 等 fsync 极慢的环境）。
DB_SYNCHRONOUS = os.environ.get("CHARGEBOUT_SYNCHRONOUS", "NORMAL").upper()

PILE_STATUSES = ("空闲", "占用", "故障", "维护")
GUN_STATUSES = ("可用", "占用", "故障")
SESSION_STATUSES = ("充电中", "结算中", "已关账", "已超时", "已中断")
OPEN_SESSIONS = ("充电中", "结算中")
